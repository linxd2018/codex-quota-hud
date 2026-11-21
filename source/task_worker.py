"""Detached task owner. UI lifetime is independent of a running Codex turn."""
import argparse
import collections
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from codex_app_server import AppServer, RpcError, TransportError, list_models, read_capabilities

APPROVAL_TIMEOUT = 15 * 60


class WorkerSuperseded(RuntimeError):
    """The scheduler revoked this worker's claim before it could dispatch."""


class TaskLease:
    def __init__(self, path, token):
        self.path, self.token, self.file = path, token, None

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.file = open(self.path, 'a+b')
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b' ')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            return False
        self.file.seek(1)
        self.file.write(json.dumps({'token': self.token, 'pid': os.getpid()}).encode('utf-8'))
        self.file.truncate()
        self.file.flush()
        os.fsync(self.file.fileno())
        return True

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def worker_is_alive(task):
    path = task.get('log_path')
    if not path:
        return False
    starting = (task.get('status') == 'starting' and task.get('phase') in ('claimed', 'resume_claimed')
                and time.time() - task.get('updated_at', 0) < 15)
    try:
        # A lock belongs to this task, not merely to a possibly recycled PID.
        stream = open(path + '.lease', 'r+b')
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(stream, fcntl.LOCK_UN)
            return None if starting else False
        finally:
            stream.close()
    except FileNotFoundError:
        return None if starting else False
    except OSError:
        return None


def thread_source(task):
    return 'codex-quota-hud:%s:%s' % (task['id'], task.get('attempts', 1))


def recover_receipts(task):
    """Recover only correlated, durable official replies, never invent IDs.

    A clientUserMessageId is evidence correlation, not a retry/idempotency promise.
    Called only after the per-task lease proves the old worker has exited.
    """
    if not task.get('log_path'):
        return {}
    path = task['log_path'] + '.receipts'
    pending, recovered = {}, {}
    try:
        stream = open(path, encoding='utf-8')
    except FileNotFoundError:
        return {}
    with stream:
        for line in stream:
            try:
                record = json.loads(line)
                message = record['payload']
                if not isinstance(message, dict):
                    continue
                request_id = message.get('id')
                if record['direction'] == 'sending':
                    method, params = message.get('method'), message.get('params', {})
                    source = params.get('threadSource')
                    source_matches = source == thread_source(task) or (
                        task.get('attempts', 1) == 1 and source == 'codex-quota-hud:' + task['id'])
                    if (method == 'thread/start' and source_matches
                            and params.get('serviceName') == 'codex-quota-hud'):
                        pending[request_id] = (method, params)
                    elif method == 'turn/start' and params.get('clientUserMessageId') == task['id'] + ':' + str(task.get('attempts', 1)):
                        expected = recovered.get('thread_id') or task.get('thread_id')
                        if expected and params.get('threadId') == expected:
                            pending[request_id] = (method, params)
                            recovered['phase'] = 'turn_start_sent'
                    else:
                        pending.pop(request_id, None)
                elif record['direction'] == 'received' and request_id in pending and 'method' not in message:
                    method, params = pending.pop(request_id)
                    result = message.get('result', {})
                    if method == 'thread/start':
                        thread = result.get('thread', {})
                        tid = thread.get('id')
                        if tid and str(uuid.UUID(tid)) == tid and (not task.get('thread_id') or task['thread_id'] == tid):
                            recovered.update(thread_id=tid, phase='thread_created')
                    elif method == 'turn/start':
                        turn = result.get('turn', {})
                        if isinstance(turn.get('id'), str) and turn['id']:
                            recovered.update(turn_id=turn['id'], phase='turn_started')
            except (ValueError, TypeError, KeyError, AttributeError):
                # An interrupted final JSONL write is not evidence.
                continue
    return recovered


def launch_worker(task, db_path, core):
    from task_queue import TaskStore
    store = TaskStore(db_path)
    token = task.get('worker_token') or str(uuid.uuid4())
    log_path = task.get('log_path') or str(Path(db_path).parent / 'logs' / (task['id'] + '.log'))
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    if store.update(task['id'], {'worker_token': token, 'log_path': log_path}, expected_status='starting',
                    expected_fields={'worker_token': task.get('worker_token')}) is None:
        raise RuntimeError('任务已不处于启动中，已取消启动进程')
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command.append(str(Path(__file__).with_name('hud_entry.py')))
    command += ['--task-worker', task['id'], '--queue-db', str(db_path), '--worker-token', token,
                '--node', core.CODEX_RUNTIME['node'], '--codex-js', core.CODEX_RUNTIME['codex_js']]
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with open(log_path + '.launcher', 'ab') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=output, close_fds=True, **options)
    # The child also saves its PID before doing any protocol work.
    with store._connection(write=True) as con:
        current = store._require(con, task['id'])
        if current['status'] in ('starting', 'running') and current.get('worker_token') == token:
            current.update(worker_pid=process.pid, updated_at=time.time())
            store._write(con, current)
    return process.pid


def error_kind(error):
    text = str(error).lower()
    if any(word in text for word in ('usagelimitexceeded', 'usage_limit_exceeded', 'ratelimitreached',
                                     'rate_limit_reached', 'usage limit', 'rate limit', 'quota exceeded',
                                     'insufficient_quota', '额度不足', '额度耗尽')):
        return 'quota'
    if any(word in text for word in ('network', 'connection', 'timeout', 'temporar', 'unavailable', 'http 5', 'httpconnection', 'streamconnection')):
        return 'network'
    return 'config'


def validate_task(task, models):
    if not task.get('description', '').strip():
        raise ValueError('任务描述不能为空')
    if not os.path.isdir(task['cwd']) or not os.access(task['cwd'], os.R_OK):
        raise ValueError('工作目录不存在或无法读取: ' + task['cwd'])
    selected = task.get('model')
    if selected:
        model = next((m for m in models if selected in (m.get('id'), m.get('model'))), None)
        if model is None:
            raise ValueError('当前 Codex 模型目录不支持所选模型: ' + selected)
    else:
        model = next((m for m in models if m.get('isDefault')), None)
    effort = task.get('reasoning_effort')
    if effort and model is not None:
        supported = [x['reasoningEffort'] for x in model.get('supportedReasoningEfforts', [])]
        if effort not in supported:
            raise ValueError('所选模型不支持推理强度: ' + effort)


def approval_result(approval, response):
    decision = response.get('decision')
    if decision not in ('accept', 'decline', 'cancel'):
        raise ValueError('审批决定必须为 accept / decline / cancel')
    method = approval['method']
    if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
        return {'decision': decision}
    if method == 'item/permissions/requestApproval':
        return {'permissions': approval['params'].get('permissions', {}) if decision == 'accept' else {}, 'scope': 'turn'}
    if method == 'mcpServer/elicitation/request' and decision != 'accept':
        return {'action': decision}
    raise ValueError('此请求需要具体输入；请在任务详情处理或中断后继续，不能一键批准')


class TaskWorker:
    def __init__(self, store, task_id, core, server_factory=AppServer, now=time.time, permission_check=None):
        self.store, self.task_id, self.core = store, task_id, core
        self.server_factory, self.now = server_factory, now
        self.server = None
        self.task = store.get(task_id)
        self.ambiguous_operation = False
        self.pending_requests = collections.deque()
        self.permission_check = permission_check
        self.last_external_check = 0
        self.created_thread = False

    def allowed(self, stage):
        check = self.permission_check
        if check is None:
            from task_scheduler import worker_dispatch_allowed
            check = worker_dispatch_allowed
        allowed, reason = check(self.task, self.store, self.core, stage=stage)
        if not allowed:
            if self.created_thread:
                self.archive_empty_thread()
            changes = dict(status='waiting_resume' if self.task.get('thread_id') else 'pending',
                           phase='permit_revoked', error=reason, worker_pid=None,
                           retry_at=self.now() + 5)
            if not self.task.get('thread_id'):
                changes['started_at'] = None
            self.save(**changes)
        return allowed

    def archive_empty_thread(self):
        thread_id = self.task['thread_id']
        self.server.request('thread/archive', {'threadId': thread_id})
        self.save(thread_id=None, discarded_thread_id=thread_id, phase='empty_thread_archived')
        self.created_thread = False

    def save(self, **changes):
        if self.store.update(self.task_id, changes,
                             expected_fields={'worker_token': self.task.get('worker_token')}) is None:
            raise WorkerSuperseded()
        self.task.update(changes)

    def finish(self, status, error='', kind='', **extra):
        changes = dict(status=status, error=error, error_kind=kind, phase='settled',
                       approval=None, approval_response=None, worker_pid=None)
        if status in ('completed', 'failed', 'cancelled'):
            changes['completed_at'] = self.now()
        changes.update(extra)
        self.save(**changes)
        self.store.append_event(self.task_id, status, error)

    def request_mutation(self, phase, method, params):
        self.save(phase=phase)
        self.ambiguous_operation = True
        try:
            result = self.server.request(method, params)
        except RpcError:
            self.ambiguous_operation = False
            raise
        return result

    def run(self):
        if self.task['status'] != 'starting':
            return
        try:
            self.save(worker_pid=os.getpid())
            if not os.path.isdir(self.task['cwd']):
                raise ValueError('工作目录不存在: ' + self.task['cwd'])
            self.server = self.server_factory(self.core, self.task.get('log_path'))
            external = self.task.get('external_recovery', False)
            models = [] if external else list_models(self.server)
            config = {} if external else self.server.request('config/read', {'includeLayers': False}).get('config', {})
            effective = dict(self.task)
            effective['model'] = self.task.get('model') or config.get('model') or next(
                (m['model'] for m in models if m.get('isDefault')), '')
            selected_model = next((m for m in models if effective['model'] in (m.get('model'),m.get('id'))), {})
            effective['reasoning_effort'] = self.task.get('reasoning_effort') or (
                config.get('model_reasoning_effort') if not self.task.get('model') else '') or selected_model.get('defaultReasoningEffort') or ''
            if not external:
                validate_task(effective, models)
            self.save(effective_model=effective['model'], effective_reasoning_effort=effective['reasoning_effort'])
            common = {'cwd': self.task['cwd'], 'approvalPolicy': self.task['approval_policy'],
                      'sandbox': self.task['sandbox'], 'approvalsReviewer': 'user'}
            if effective['model']:
                common['model'] = effective['model']
            if effective['reasoning_effort']:
                common['config'] = {'model_reasoning_effort': effective['reasoning_effort']}
            thread_id = self.task.get('thread_id')
            if thread_id:
                result = self.server.request('thread/resume', dict({} if external else common, threadId=thread_id, excludeTurns=True))
                status = result.get('thread', {}).get('status', {}).get('type')
                if status == 'active':
                    raise TransportError('会话已有运行轮次，必须先核实，不能重复投递')
                text = self.core.resume_message({'thread_id': thread_id, 'turn_id': self.task.get('turn_id'), 'cwd': self.task['cwd']}) if self.task.get('turn_id') else self.task['description']
            else:
                if not self.allowed('thread'):
                    return
                result = self.request_mutation('thread_start_sent', 'thread/start', dict(
                    common, ephemeral=False, threadSource=thread_source(self.task),
                    serviceName='codex-quota-hud'))
                thread_id = result['thread']['id']
                # This commit must succeed BEFORE sending the first user turn.
                self.save(thread_id=thread_id, phase='thread_created')
                self.ambiguous_operation = False
                self.created_thread = True
                text = self.task['description']
            params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': text}],
                      'clientUserMessageId': self.task_id + ':' + str(self.task.get('attempts', 1))}
            if not external:
                params.update(cwd=self.task['cwd'], approvalPolicy=self.task['approval_policy'])
            if effective['model'] and not external:
                params['model'] = effective['model']
            if effective['reasoning_effort'] and not external:
                params['effort'] = effective['reasoning_effort']
            if not self.allowed('turn'):
                return
            self.save(previous_turn_id=self.task.get('turn_id'))
            try:
                result = self.request_mutation('turn_start_sent', 'turn/start', params)
            except RpcError:
                if self.created_thread:
                    try:
                        thread = self.server.request('thread/read', {
                            'threadId': thread_id, 'includeTurns': True}).get('thread', {})
                        if thread.get('turns') == [] and thread.get('status', {}).get('type') != 'active':
                            self.archive_empty_thread()
                    except (RpcError, TransportError):
                        pass  # Keep the UUID when emptiness or archival cannot be confirmed.
                raise
            turn = result['turn']
            self.save(turn_id=turn['id'], status='running', phase='turn_started',
                      started_at=self.task.get('started_at') or self.now(), error='', error_kind='',
                      turn_started_at=self.now(),
                      approval=None, approval_response=None)
            self.ambiguous_operation = False
            self.store.append_event(self.task_id, 'running', thread_id)
            if turn.get('status') in ('completed', 'failed', 'interrupted'):
                self.complete_turn(turn)
            else:
                self.monitor()
        except WorkerSuperseded:
            return
        except BaseException as error:
            uncertain = self.ambiguous_operation or isinstance(error, TransportError) and bool(self.task.get('thread_id'))
            kind = 'uncertain' if uncertain else error_kind(error)
            if (not uncertain and self.task.get('external_recovery') and isinstance(error, RpcError)
                    and 'already has an active writer' in str(error).lower()):
                self.finish('waiting_resume', '原会话仍被 Codex 占用，稍后自动重试', 'session_busy',
                            retry_at=self.now() + 60)
            elif kind == 'uncertain':
                self.finish('failed', str(error), kind, phase='uncertain')
            elif kind == 'quota':
                self.finish('waiting_quota', str(error), kind, retry_at=self.now() + 60)
            elif kind == 'network':
                self.finish('failed', str(error), kind, retry_at=self.now() + min(900, 30 * 2 ** min(5, self.task.get('attempts', 1))))
            else:
                self.finish('failed', str(error), kind)
        finally:
            if self.server:
                self.server.close()

    def complete_turn(self, turn):
        status = turn.get('status')
        if status == 'completed':
            self.finish('completed')
        elif status == 'interrupted':
            self.finish('cancelled', 'Codex 会话已被中断或取消')
        else:
            error = json.dumps(turn.get('error') or {}, ensure_ascii=False)
            kind = error_kind(error)
            self.finish('waiting_quota' if kind == 'quota' else 'failed', error, kind,
                        retry_at=self.now() + 60 if kind == 'quota' else None)

    def interrupt(self):
        self.request_mutation('interrupt_sent', 'turn/interrupt', {
            'threadId': self.task['thread_id'], 'turnId': self.task['turn_id']})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = self.server.next_event(.5)
            if message and message.get('method') == 'turn/completed':
                params = message.get('params', {})
                turn = params.get('turn', {})
                if params.get('threadId') == self.task['thread_id'] and turn.get('id') == self.task['turn_id']:
                    self.ambiguous_operation = False
                    return turn
        raise TransportError('中断请求已发出，但未确认轮次停止；继续保留名额')

    def monitor(self):
        while True:
            if self.observe_external_turn():
                return
            latest = self.store.get(self.task_id)
            if not self.task.get('approval') and self.pending_requests:
                request = self.pending_requests.popleft()
                self.save(status='waiting_authorization', approval=request, approval_response=None)
            approval = self.task.get('approval')
            if approval:
                response = latest.get('approval_response')
                if response:
                    if response.get('decision') == 'cancel':
                        self.complete_turn(self.interrupt())
                        return
                    try:
                        reply = approval_result(approval, response)
                    except ValueError as error:
                        self.save(approval_response=None, error=str(error))
                    else:
                        self.server.respond(approval['id'], reply)
                        self.save(status='running', approval=None, approval_response=None, error='')
                elif self.now() - approval['pending_since'] >= APPROVAL_TIMEOUT:
                    turn = self.interrupt()
                    if turn.get('status') != 'interrupted':
                        self.complete_turn(turn)
                        return
                    self.finish('waiting_authorization', '授权等待超过 15 分钟，已中断释放名额；请手动重试',
                                'authorization', phase='approval_timeout')
                    return
            message = self.server.next_event(.5)
            if message is False:
                continue
            params = message.get('params') or {}
            if params.get('threadId') and params['threadId'] != self.task['thread_id']:
                continue
            if 'id' in message and 'method' in message:
                request = {
                    'id': message['id'], 'method': message['method'], 'params': params,
                    'pending_since': self.now()}
                if self.task.get('approval'):
                    self.pending_requests.append(request)
                else:
                    self.save(status='waiting_authorization', approval=request, approval_response=None)
            elif message.get('method') == 'turn/completed':
                turn = params.get('turn', {})
                if turn.get('id') == self.task['turn_id']:
                    self.complete_turn(turn)
                    return
            elif message.get('method') == 'serverRequest/resolved':
                approval = self.task.get('approval')
                if approval and params.get('requestId') == approval['id']:
                    self.save(status='running', approval=None, approval_response=None)

    def observe_external_turn(self):
        if time.monotonic() - self.last_external_check < 5:
            return False
        self.last_external_check = time.monotonic()
        try:
            latest = self.core.latest_turn(self.task['thread_id'])
        except Exception:
            return False
        if not latest:
            return False
        turn_id = latest.get('turn_id')
        if turn_id == self.task.get('previous_turn_id'):
            return False  # A lagging history projection is not an external turn.
        if turn_id == self.task['turn_id']:
            if latest.get('status') in ('interrupted', 'cancelled'):
                self.interrupt()
                self.finish('cancelled', '任务已在 Codex 客户端取消')
                return True
            return False
        if turn_id:
            # The user's newer turn owns the conversation now. Stop only our
            # owned turn, then let the scheduler follow that newer real turn.
            self.interrupt()
            self.save(turn_id=turn_id, status='running', phase='external_turn', worker_pid=None,
                      approval=None, approval_response=None, error='已交由客户端中的新轮次继续')
            return True
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-worker', required=True)
    parser.add_argument('--queue-db', required=True)
    parser.add_argument('--worker-token', required=True)
    parser.add_argument('--node', required=True)
    parser.add_argument('--codex-js', required=True)
    args = parser.parse_args(argv)
    import codex_watchdog as core
    from task_queue import TaskStore
    core.CODEX_RUNTIME = {'node': args.node, 'codex_js': args.codex_js}
    store = TaskStore(args.queue_db)
    task = store.get(args.task_worker)
    if not task or task.get('worker_token') != args.worker_token or task['status'] != 'starting':
        return 2
    lease = TaskLease(task['log_path'] + '.lease', args.worker_token)
    if not lease.acquire():
        return 3
    try:
        task = store.get(args.task_worker)
        if task.get('worker_token') != args.worker_token or task['status'] != 'starting':
            return 2
        TaskWorker(store, args.task_worker, core).run()
    finally:
        lease.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
