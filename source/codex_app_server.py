"""Small stdlib client for the installed Codex app-server JSONL protocol.

Request IDs correlate replies; they are deliberately NOT treated as idempotency keys.
No methods in capability discovery start an inference turn.
"""
import collections
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time


class RpcError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False))


class TransportError(RuntimeError):
    pass


def safe_log_payload(value):
    """Logs support recovery, not a second copy of prompts/tool output/secrets."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if any(part in key.lower() for part in ('token', 'authorization', 'password', 'secret', 'apikey', 'api_key')):
                result[key] = '[redacted]'
            elif key in ('input', 'text', 'aggregatedOutput', 'output', 'content', 'instructions', 'developerInstructions'):
                result[key] = '[omitted]'
            else:
                result[key] = safe_log_payload(item)
        return result
    if isinstance(value, list):
        return [safe_log_payload(item) for item in value[:30]]
    if isinstance(value, str):
        value = re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [redacted]', value)
        value = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', value)
        return value[:2000]
    return value


class AppServer:
    def __init__(self, core, log_path=None, timeout=45):
        self.core = core
        self.timeout = timeout
        self.log_path = log_path
        self.incoming = queue.Queue()
        self.events = collections.deque()
        self.serial = 0
        self.log_lock = threading.Lock()
        self.process = subprocess.Popen(
            core.codex_command('app-server', '--stdio'), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', bufsize=1,
            **getattr(core, 'SUBPROCESS_OPTIONS', {}))
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.info = self.request('initialize', {'clientInfo': {
                'name': 'codex-quota-hud', 'title': 'Codex Quota HUD', 'version': '1.2.0'}})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def journal(self, direction, payload):
        if not self.log_path:
            return
        method = payload.get('method', '') if isinstance(payload, dict) else ''
        if method.endswith('/delta') or method.startswith('item/') and 'Approval' not in method and 'UserInput' not in method:
            return
        record = {'at': time.time(), 'direction': direction, 'payload': safe_log_payload(payload)}
        encoded = json.dumps(record, ensure_ascii=False) + '\n'
        with self.log_lock:
            # UUID/turn acknowledgements have an independent, bounded durable
            # receipt journal so routine log rotation cannot remove recovery proof.
            result = payload.get('result', {}) if isinstance(payload, dict) else {}
            receipt = (direction == 'sending' and method in ('thread/start', 'turn/start')) or (
                direction == 'received' and isinstance(result, dict) and (
                    isinstance(result.get('thread'), dict) or isinstance(result.get('turn'), dict)))
            if receipt:
                receipt_path = self.log_path + '.receipts'
                if os.path.exists(receipt_path) and os.path.getsize(receipt_path) > 1024 * 1024:
                    raise OSError('回执日志已达上限，停止发送并保留现有回执')
                with open(receipt_path, 'a', encoding='utf-8') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 2 * 1024 * 1024:
                os.replace(self.log_path, self.log_path + '.1')
            with open(self.log_path, 'a', encoding='utf-8') as stream:
                stream.write(encoded)
                stream.flush()
                if direction != 'stderr':
                    os.fsync(stream.fileno())

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                # Persist received UUID/turn evidence before exposing it to the worker.
                self.journal('received', message)
                self.incoming.put(message)
        except Exception as error:
            self.incoming.put(TransportError('协议日志或读取失败: %s' % error))
        finally:
            self.incoming.put(None)

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                self.journal('stderr', line.rstrip())
        except (OSError, ValueError):
            pass

    def send(self, payload):
        self.journal('sending', payload)
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise TransportError('Codex 连接关闭；发送结果待核实') from error

    def request(self, method, params=None, timeout=None):
        self.serial += 1
        request_id = self.serial
        self.send({'id': request_id, 'method': method, 'params': params or {}})
        end = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < end:
            message = self._next(min(.5, max(.01, end - time.monotonic())))
            if message is False:
                continue
            if message.get('id') == request_id and 'method' not in message:
                if 'error' in message:
                    raise RpcError(message['error'])
                return message.get('result', {})
            self.events.append(message)
        raise TransportError('%s 响应超时；发送结果待核实' % method)

    def _next(self, timeout):
        try:
            message = self.incoming.get(timeout=timeout)
        except queue.Empty:
            return False
        if message is None:
            raise TransportError('Codex app-server 已退出')
        if isinstance(message, Exception):
            raise message
        return message

    def next_event(self, timeout=.5):
        return self.events.popleft() if self.events else self._next(timeout)

    def respond(self, request_id, result):
        self.send({'id': request_id, 'result': result})

    def close(self):
        # EOF lets the official wrapper stop its child. If it cannot exit, only
        # terminate this owned process tree, never an arbitrary Codex instance.
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, **getattr(self.core, 'SUBPROCESS_OPTIONS', {}))
            else:
                self.process.terminate()
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _permission_modes(core):
    """Ask this installed CLI for its protocol, not a stale UI mode list."""
    with tempfile.TemporaryDirectory(prefix='codex-hud-schema-') as directory:
        result = subprocess.run(core.codex_command('app-server', 'generate-json-schema',
                                                   '--out', directory),
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=30,
                                **getattr(core, 'SUBPROCESS_OPTIONS', {}))
        if result.returncode:
            raise RuntimeError('无法读取本机 Codex 权限协议: ' + result.stderr[-500:])
        with open(os.path.join(directory, 'v2', 'ThreadStartParams.json'), encoding='utf-8') as f:
            definitions = json.load(f)['definitions']
        sandboxes = definitions['SandboxMode']['enum']
        approvals = definitions['AskForApproval']
        modes = approvals.get('enum', [])
        for variant in approvals.get('oneOf', []):
            modes += variant.get('enum', [])
        return sandboxes, modes


def read_capabilities(core):
    server = AppServer(core)
    try:
        models = list_models(server)
        config = server.request('config/read', {'includeLayers': False}).get('config', {})
        sandboxes, approvals = _permission_modes(core)
        return {'models': models, 'sandbox_modes': sandboxes, 'approval_modes': approvals,
                'default_model': config.get('model') or next(
                    (m['model'] for m in models if m.get('isDefault')), ''),
                'default_reasoning_effort': config.get('model_reasoning_effort') or '',
                'version': server.info.get('userAgent', ''), 'fetched_at': time.time()}
    finally:
        server.close()


def list_models(server):
    models, cursor, seen = [], None, set()
    while True:
        params = {'limit': 100}
        if cursor:
            params['cursor'] = cursor
        result = server.request('model/list', params)
        models.extend(result.get('data', []))
        cursor = result.get('nextCursor')
        if not cursor:
            return models
        if cursor in seen:
            raise RuntimeError('模型目录分页游标重复，无法验证模型')
        seen.add(cursor)
