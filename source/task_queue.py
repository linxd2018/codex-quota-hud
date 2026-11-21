"""Durable HUD-owned task records; submitting never contacts Codex.

Connections are short lived and never shared between threads/processes. WAL and
FULL synchronous commits protect enqueue and dispatch intents across crashes.
The JSON payload carries worker details while indexed columns own scheduling.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


STATES = frozenset(('pending', 'starting', 'running', 'waiting_quota',
                    'waiting_resume', 'waiting_authorization', 'completed',
                    'failed', 'cancelled'))
SANDBOX_MODES = ('read-only', 'workspace-write', 'danger-full-access')
APPROVAL_MODES = ('untrusted', 'on-request', 'never')
SPEC_FIELDS = frozenset(('description', 'cwd', 'model', 'reasoning_effort',
                         'sandbox', 'approval_policy'))
UNCERTAIN_PHASES = frozenset(('creating', 'thread_starting', 'turn_sending',
                             'uncertain', 'launching', 'thread_start_sent',
                             'turn_start_sent', 'interrupt_sent'))


def validate_concurrency(value):
    if type(value) is not int or not 3 <= value <= 10:
        raise ValueError('最大并发任务数必须是 3～10 的整数')
    return value


def normalize_concurrency(value):
    try:
        return validate_concurrency(value)
    except ValueError:
        return 3


def validate_spec(spec):
    result = {key: spec.get(key, '') for key in SPEC_FIELDS}
    for key in SPEC_FIELDS:
        if not isinstance(result[key], str):
            raise ValueError('%s 必须是文本' % key)
    if not result['description'].strip():
        raise ValueError('任务描述不能为空')
    cwd = os.path.expandvars(os.path.expanduser(result['cwd'].strip().strip('"')))
    if not cwd or not os.path.isdir(cwd):
        raise ValueError('工作目录不存在或无法访问')
    result['cwd'] = os.path.abspath(cwd)
    result['sandbox'] = result['sandbox'] or 'workspace-write'
    result['approval_policy'] = result['approval_policy'] or 'on-request'
    if result['sandbox'] not in SANDBOX_MODES:
        raise ValueError('不支持的沙箱模式')
    if result['approval_policy'] not in APPROVAL_MODES:
        raise ValueError('不支持的审批模式')
    result['model'] = result['model'].strip()
    result['reasoning_effort'] = result['reasoning_effort'].strip()
    return result


class TaskStore:
    def __init__(self, path):
        self.path = os.path.abspath(os.fspath(path))

    @contextmanager
    def _connection(self, write=False):
        if write:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            con = sqlite3.connect(self.path, timeout=15)
        else:
            con = sqlite3.connect(Path(self.path).as_uri() + '?mode=ro', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            if write:
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('PRAGMA synchronous=FULL')
                con.executescript('''
                    CREATE TABLE IF NOT EXISTS new_tasks (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        thread_id TEXT,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS new_tasks_dispatch
                        ON new_tasks(status, priority DESC, created_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS new_tasks_thread
                        ON new_tasks(thread_id) WHERE thread_id IS NOT NULL;
                    CREATE TABLE IF NOT EXISTS new_task_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        event TEXT NOT NULL,
                        detail TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS task_dispatch_control (
                        owner TEXT PRIMARY KEY,
                        expires_at REAL NOT NULL,
                        payload TEXT NOT NULL
                    );
                ''')
                con.execute('BEGIN IMMEDIATE')
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        record = json.loads(row['payload'])
        if not isinstance(record, dict):
            raise ValueError('任务数据损坏，请保留数据库并检查备份')
        record.update({key: row[key] for key in ('id', 'status', 'priority',
                                               'created_at', 'updated_at', 'thread_id')})
        return record

    @staticmethod
    def _write(con, record):
        if record['status'] not in STATES:
            raise ValueError('无效任务状态: %s' % record['status'])
        if type(record['priority']) is not int:
            raise ValueError('优先级必须是整数')
        con.execute('''INSERT INTO new_tasks
            (id,status,priority,created_at,updated_at,thread_id,payload)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            status=excluded.status, priority=excluded.priority,
            updated_at=excluded.updated_at, thread_id=excluded.thread_id,
            payload=excluded.payload''',
            (record['id'], record['status'], record['priority'], record['created_at'],
             record['updated_at'], record.get('thread_id'), json.dumps(record, ensure_ascii=False)))

    @staticmethod
    def _event(con, task_id, event, detail=''):
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        con.execute('INSERT INTO new_task_events(task_id,created_at,event,detail) VALUES(?,?,?,?)',
                    (task_id, time.time(), event, text))

    @staticmethod
    def _require(con, task_id):
        record = TaskStore._decode(con.execute('SELECT * FROM new_tasks WHERE id=?', (task_id,)).fetchone())
        if record is None:
            raise ValueError('任务不存在')
        return record

    def list_tasks(self):
        if not os.path.isfile(self.path):
            return []
        with self._connection() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='new_tasks'").fetchone():
                return []
            return [self._decode(row) for row in con.execute(
                'SELECT * FROM new_tasks ORDER BY priority DESC,created_at,id')]

    def get(self, task_id):
        if not os.path.isfile(self.path):
            return None
        with self._connection() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='new_tasks'").fetchone():
                return None
            return self._decode(con.execute('SELECT * FROM new_tasks WHERE id=?', (task_id,)).fetchone())

    def submit(self, spec):
        values = validate_spec(spec)
        priority = spec.get('priority', 0)
        if type(priority) is not int:
            raise ValueError('优先级必须是整数')
        now = time.time()
        record = dict(values, id=str(uuid.uuid4()), status='pending', priority=priority,
                      created_at=now, updated_at=now, started_at=None, completed_at=None,
                      thread_id=None, turn_id=None, phase='pending', attempts=0, retry_at=0,
                      error=None, error_kind=None, log_path=None, worker_pid=None,
                      worker_token=None, approval=None, approval_response=None)
        with self._connection(write=True) as con:
            self._write(con, record)
            self._event(con, record['id'], 'submitted')
        return record

    def adopt_recovery(self, item):
        """Atomically hand an unsent incident to the detached turn executor.

        Desktop queued/uncertain messages must never be adopted: they may still
        execute later. One durable record per original UUID owns all recoveries.
        """
        if not os.path.isdir(item['cwd']):
            raise ValueError('原会话工作目录不存在或无法访问')
        now = time.time()
        with self._connection(write=True) as con:
            event = con.execute('SELECT state FROM resume_events WHERE thread_id=? AND failed_turn_id=?',
                                (item['thread_id'], item['turn_id'])).fetchone()
            if not event or event['state'] != 'pending':
                return None
            old = self._decode(con.execute('SELECT * FROM new_tasks WHERE thread_id=?',
                                          (item['thread_id'],)).fetchone())
            if old and (old['status'] not in ('completed', 'failed', 'cancelled') or
                        old.get('error_kind') == 'uncertain' or old.get('phase') in UNCERTAIN_PHASES):
                return None
            record = dict(id=old['id'] if old else str(uuid.uuid4()),
                          created_at=old['created_at'] if old else now, updated_at=now,
                          status='waiting_resume', priority=0, description='自动接续 · ' + (item.get('title') or item['thread_id']),
                          cwd=item['cwd'], model='', reasoning_effort='', sandbox='', approval_policy='',
                          thread_id=item['thread_id'], turn_id=item['turn_id'], recovery_turn_id=item['turn_id'],
                          external_recovery=True, phase='settled', attempts=0, retry_at=0,
                          started_at=now, completed_at=None, error=None, error_kind=None,
                          log_path=None, worker_pid=None, worker_token=None,
                          approval=None, approval_response=None)
            self._write(con, record)
            con.execute("UPDATE resume_events SET state='queue_owned',updated_at=?,last_error=? WHERE thread_id=? AND failed_turn_id=?",
                        (now, '由后台执行器直接启动原会话', item['thread_id'], item['turn_id']))
            self._event(con, record['id'], 'recovery_adopted', item['turn_id'])
            return record

    @staticmethod
    def _recovery_receipt(con, record, changes):
        if not record.get('external_recovery') or not record.get('recovery_turn_id'):
            return
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resume_events'").fetchone():
            return
        identity = (record['thread_id'], record['recovery_turn_id'])
        if changes.get('phase') == 'turn_start_sent':
            con.execute("UPDATE resume_events SET attempts=attempts+1,last_error=?,updated_at=? WHERE thread_id=? AND failed_turn_id=? AND state='queue_owned' AND last_error!=?",
                        ('后台正在启动轮次', time.time(), *identity, '后台正在启动轮次'))
        if changes.get('phase') == 'turn_started' and record.get('turn_id') != record['recovery_turn_id']:
            con.execute("UPDATE resume_events SET state='confirmed',last_error=NULL,updated_at=? WHERE thread_id=? AND failed_turn_id=? AND state IN ('queue_owned','uncertain')",
                        (time.time(), *identity))
        elif changes.get('error_kind') == 'uncertain':
            con.execute("UPDATE resume_events SET state='uncertain',last_error=?,updated_at=? WHERE thread_id=? AND failed_turn_id=? AND state='queue_owned'",
                        (record.get('error'), time.time(), *identity))

    def update(self, task_id, changes, expected_status=None, expected_fields=None):
        if any(key in changes for key in ('id', 'created_at')):
            raise ValueError('任务标识和创建时间不可修改')
        with self._connection(write=True) as con:
            record = self._require(con, task_id)
            accepted = (expected_status,) if isinstance(expected_status, str) else expected_status
            if accepted is not None and record['status'] not in accepted:
                return None
            if expected_fields and any(record.get(key) != value for key, value in expected_fields.items()):
                return None
            previous = record['status']
            record.update(changes, updated_at=time.time())
            self._write(con, record)
            self._recovery_receipt(con, record, changes)
            if record['status'] != previous:
                self._event(con, task_id, 'state', {'from':previous, 'to':record['status'],
                                                   'error':record.get('error')})
            return record

    @staticmethod
    def _editable(record):
        if (record.get('started_at') is not None or record.get('thread_id') or
                record['status'] not in ('pending', 'waiting_quota', 'failed')):
            raise ValueError('仅可修改或删除尚未启动的任务')

    def edit(self, task_id, patch):
        if set(patch) - SPEC_FIELDS:
            raise ValueError('只能修改任务描述、目录、模型、推理及权限')
        with self._connection(write=True) as con:
            record = self._require(con, task_id)
            self._editable(record)
            record.update(validate_spec(dict(record, **patch)), updated_at=time.time())
            self._write(con, record)
            self._event(con, task_id, 'edited')
            return record

    def delete(self, task_id):
        # Retain an audit tombstone: removing from dispatch must not erase intent.
        with self._connection(write=True) as con:
            record = self._require(con, task_id)
            if not (record['status'] == 'failed' and record.get('phase') == 'settled'
                    and not record.get('worker_pid') and record.get('error_kind') != 'uncertain'):
                self._editable(record)
            record.update(status='cancelled', completed_at=time.time(), updated_at=time.time())
            self._write(con, record)
            self._event(con, task_id, 'cancelled')
            return record

    def reprioritize(self, task_id, priority):
        if type(priority) is not int or not -(2**31) <= priority < 2**31:
            raise ValueError('优先级必须是有效整数')
        with self._connection(write=True) as con:
            record = self._require(con, task_id)
            if record['status'] not in ('pending','waiting_quota','waiting_resume','failed'):
                raise ValueError('此任务当前无法调整优先级')
            record.update(priority=priority, updated_at=time.time())
            self._write(con, record)
            self._event(con, task_id, 'priority', str(priority))
            return record

    def retry(self, task_id):
        with self._connection(write=True) as con:
            record = self._require(con, task_id)
            if record['status'] not in ('failed', 'waiting_authorization'):
                raise ValueError('只能重试失败或已停止等待授权的任务')
            if record.get('worker_pid'):
                raise ValueError('任务进程尚未退出，请先处理当前请求')
            if record.get('error_kind') == 'uncertain' or record.get('phase') in UNCERTAIN_PHASES:
                raise ValueError('上次启动结果不明确，请先核对会话；为避免重复启动，不能直接重试')
            if not record.get('external_recovery'):
                validate_spec(record)
            record.update(status='waiting_resume' if record.get('thread_id') else 'pending',
                          updated_at=time.time(), retry_at=0, error=None, error_kind=None,
                          completed_at=None, approval=None, approval_response=None,
                          phase='retry_pending')
            self._write(con, record)
            self._event(con, task_id, 'retry')
            return record

    def respond_approval(self, task_id, displayed_request, decision):
        if decision not in ('accept', 'decline', 'cancel'):
            raise ValueError('无效的授权决定')
        with self._connection(write=True) as con:
            record = self._require(con, task_id)
            if (record['status'] != 'waiting_authorization' or not displayed_request
                    or record.get('approval') != displayed_request or record.get('approval_response')):
                raise ValueError('该授权请求已变化，请重新核对当前请求')
            record.update(approval_response={'decision': decision}, updated_at=time.time())
            self._write(con, record)
            return record

    def publish_dispatch_control(self, control):
        with self._connection(write=True) as con:
            con.execute('DELETE FROM task_dispatch_control WHERE expires_at < ?', (time.time(),))
            con.execute('''INSERT INTO task_dispatch_control(owner,expires_at,payload)
                VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET
                expires_at=excluded.expires_at, payload=excluded.payload''',
                (control['owner'], control['expires_at'], json.dumps(control)))

    @staticmethod
    def read_dispatch_control(con, owner):
        row = con.execute('SELECT payload FROM task_dispatch_control WHERE owner=?', (owner,)).fetchone()
        return json.loads(row['payload']) if row else None

    def claim_next(self):
        if not os.path.isfile(self.path):
            return None
        with self._connection(write=True) as con:
            now = time.time()
            for row in con.execute("SELECT * FROM new_tasks WHERE status='pending' ORDER BY priority DESC,created_at,id"):
                record = self._decode(row)
                if record.get('retry_at', 0) > now:
                    continue
                record.update(status='starting', phase='claimed', updated_at=now,
                              started_at=record.get('started_at') or now,
                              attempts=record.get('attempts', 0)+1, worker_token=str(uuid.uuid4()),
                              error=None, error_kind=None)
                self._write(con, record)
                self._event(con, record['id'], 'claimed')
                return record
            return None

    def append_event(self, task_id, event, detail=''):
        with self._connection(write=True) as con:
            self._require(con, task_id)
            self._event(con, task_id, event, detail)

    def events(self, task_id, limit=100):
        if not os.path.isfile(self.path):
            return []
        with self._connection() as con:
            return [dict(row) for row in con.execute(
                'SELECT * FROM new_task_events WHERE task_id=? ORDER BY sequence DESC LIMIT ?',
                (task_id, max(1,min(1000,int(limit)))))]
