"""Scheduling policy over HUD-owned durable records; never calls a model itself."""
import os
import time
import uuid
from task_queue import UNCERTAIN_PHASES, validate_concurrency


RECOVERY_STATES = ('waiting_quota', 'waiting_resume')
ACTIVE_STATES = ('starting', 'running', 'waiting_authorization')


def worker_dispatch_allowed(task, store, core, stage='turn'):
    """Commit a final send intent under the same SQLite writer lock for all workers.

    A permit is specific to one HUD enable generation. This final gate is after
    slow CLI initialization and model validation, immediately before the RPC.
    """
    if stage not in ('thread', 'turn'):
        raise ValueError('Unknown dispatch stage')
    if not task.get('dispatch_owner'):
        return False, '缺少自动调度授权，未启动模型'
    # The client history scan can block. Read the current settings only after
    # it finishes, under the same writer lock as control updates and grants.
    scan = core.execution_snapshot()
    with store._connection(write=True) as con:
        current = store._require(con, task['id'])
        if current.get('worker_token') != task.get('worker_token') or current['status'] != 'starting':
            return False, '任务启动授权已被替换'
        control = store.read_dispatch_control(con, current.get('dispatch_owner'))
        if (not control or control.get('generation') != current.get('dispatch_generation') or
                not control.get('enabled') or control.get('expires_at', 0) < time.time()):
            return False, '自动接续已关闭或本次启动授权已过期'
        if not control.get('quota_available'):
            return False, '当前没有已确认可用的额度'
        maximum = validate_concurrency(control.get('maxConcurrentTasks'))
        tasks = [store._decode(row) for row in con.execute('SELECT * FROM new_tasks')]
        confirmed_at = control.get('quota_confirmed_at') or 0
        if any(row.get('error_kind') == 'quota' and row['updated_at'] > confirmed_at for row in tasks):
            return False, '实际调用已报告额度不足，等待下一次额度确认'
        latest = scan.get('turns', {}).get(current.get('thread_id'))
        if latest and current.get('turn_id') and latest['turn_id'] != current['turn_id']:
            return False, '客户端已有新轮次，等待核对会话状态'
        occupied = {'thread:' + tid for tid in scan['active_thread_ids']}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resume_events'").fetchone():
            occupied.update('thread:' + row[0] for row in con.execute(
                "SELECT DISTINCT thread_id FROM resume_events WHERE state IN ('sending','queued','uncertain')"))
        waiting = []
        for row in tasks:
            key = 'thread:' + row['thread_id'] if row.get('thread_id') else 'task:' + row['id']
            if row['id'] == current['id']:
                continue
            # thread/start creates an empty shell. Only turn intents consume
            # execution capacity; earlier starting tasks still retain FIFO rank.
            if (row['status'] in ('running', 'waiting_authorization') or
                    row.get('phase') in ('turn_start_sent', 'interrupt_sent') or
                    TaskScheduler._uncertain(row) and row['status'] == 'failed'):
                if not (row.get('phase') == 'approval_timeout' and not row.get('worker_pid')):
                    occupied.add(key)
            elif row['status'] == 'starting':
                waiting.append(row)
        current_key = 'thread:' + current['thread_id'] if current.get('thread_id') else 'task:' + current['id']
        if current_key in occupied:
            return False, '该会话已有运行或待核查的执行，未重复启动'
        order = lambda row: (row.get('started_at') or row['created_at'], row['created_at'], row['id'])
        earlier = sum(1 for row in waiting if order(row) < order(current))
        if len(occupied) + earlier >= maximum:
            return False, '最大并发任务数已达到，保留任务等待空位'
        if stage == 'turn':
            current['previous_turn_id'] = current.get('turn_id')
        current.update(phase='thread_start_sent' if stage == 'thread' else 'turn_start_sent',
                       updated_at=time.time())
        store._write(con, current)
        store._recovery_receipt(con, current, {'phase': current['phase']})
        return True, ''


class TaskScheduler:
    def __init__(self, core, store, launcher=None, worker_alive=None, prepare_dispatch=None):
        self.core, self.store = core, store
        self.launcher = launcher
        self.worker_alive = worker_alive
        self.prepare_dispatch = prepare_dispatch

    def _alive(self, task):
        if self.worker_alive is not None:
            return self.worker_alive(task)
        from task_worker import worker_is_alive
        return worker_is_alive(task)

    def owned_thread_ids(self):
        return {task['thread_id'] for task in self.store.list_tasks() if task.get('thread_id')
                and (task['status'] not in ('completed', 'cancelled', 'failed') or self._uncertain(task))}

    @staticmethod
    def _uncertain(task):
        return task.get('error_kind') == 'uncertain' or task.get('phase') in UNCERTAIN_PHASES

    def reservation_tokens(self):
        reservations = set()
        for task in self.store.list_tasks():
            active = task['status'] in ACTIVE_STATES
            if task.get('phase') == 'approval_timeout' and not task.get('worker_pid'):
                active = False
            if active or self._uncertain(task):
                reservations.add('thread:' + task['thread_id'] if task.get('thread_id') else 'task:' + task['id'])
        return reservations

    def running_tokens(self):
        return {('thread:' + task['thread_id'] if task.get('thread_id') else 'task:' + task['id'])
                for task in self.store.list_tasks()
                if task['status'] in ('running', 'waiting_authorization')
                and not (task.get('phase') == 'approval_timeout' and not task.get('worker_pid'))}

    def reconcile(self):
        """Observe detached workers and terminal evidence without issuing any turn.

        A lost create/start acknowledgement is not permission to create again.
        Unprovable outcomes retain their reservation and expose a failure to the UI.
        """
        for task in self.store.list_tasks():
            waiting = task['status'] in RECOVERY_STATES or task.get('phase') == 'approval_timeout'
            if task['status'] not in ACTIVE_STATES and not waiting and not self._uncertain(task):
                continue
            alive = self._alive(task)
            if alive is not False:
                continue
            old_phase = task.get('phase')
            expected = {'phase': old_phase, 'worker_token': task.get('worker_token')}
            if self._uncertain(task):
                from task_worker import recover_receipts
                receipts = recover_receipts(task)
                if receipts.get('phase') == 'thread_created' and old_phase not in ('thread_start_sent', 'thread_created'):
                    receipts.pop('phase')
                if receipts:
                    recovered = self.store.update(task['id'], receipts, expected_status=task['status'],
                                                  expected_fields=expected)
                    if recovered is None:
                        continue
                    task = recovered
                    expected['phase'] = task.get('phase')
            latest = None
            if task.get('thread_id'):
                # If the history mirror is unavailable, preserve the reservation.
                try:
                    latest = self.core.latest_turn(task['thread_id'])
                except Exception:
                    continue
            if waiting and (not latest or not task.get('turn_id') or latest['turn_id'] == task['turn_id']):
                continue
            if (self._uncertain(task) and latest and
                    (not task.get('turn_id') or
                     task.get('previous_turn_id') is not None and
                     task.get('turn_id') == task.get('previous_turn_id'))):
                # The previous turn is not an acknowledgement of a new attempt.
                # Keep the uncertain intent even when that previous turn failed
                # with quota or completed successfully.
                latest = None
            changes = {'worker_pid': None}
            if latest:
                changes['turn_id'] = latest['turn_id']
            if latest and latest.get('status') == 'completed':
                changes.update(status='completed', completed_at=latest.get('completed_at') or time.time(),
                               phase='settled', error=None, error_kind=None)
            elif latest and latest.get('status') in ('interrupted', 'cancelled'):
                changes.update(status='cancelled', completed_at=latest.get('completed_at') or time.time(),
                               phase='settled', error='任务已在 Codex 中停止', error_kind='cancelled')
            elif latest and latest.get('status') == 'failed':
                if self.core.quota_error(latest.get('error')):
                    changes.update(status='waiting_quota', phase='settled',
                                   turn_id=latest.get('turn_id'), retry_at=time.time()+60,
                                   error='额度不足，等待恢复后接续原会话', error_kind='quota')
                else:
                    changes.update(status='failed', phase='settled',
                                   error=latest.get('error') or 'Codex 执行失败', error_kind='execution')
            elif task['status'] == 'starting' and task.get('phase') in (
                    'claimed', 'resume_claimed', 'thread_created', 'empty_thread_archived'):
                # The stopped attempt never reached the durable turn/send intent.
                changes.update(status='pending', phase='pending', worker_token=None, retry_at=0,
                               error=None, error_kind=None, started_at=None)
                if task.get('phase') == 'thread_created':
                    changes.update(thread_id=None, discarded_thread_id=task.get('thread_id'))
                elif task.get('thread_id'):
                    changes.update(status='waiting_resume', started_at=task.get('started_at'))
            elif latest and latest.get('status') == 'inProgress':
                if waiting or task.get('phase') == 'external_turn':
                    changes.update(status='running', phase='external_turn', error=None, error_kind=None)
                else:
                    changes.update(status='failed', phase='uncertain', error_kind='uncertain',
                                   error='执行进程已退出，但会话仍显示运行中；保留名额等待终态核查')
            else:
                changes.update(status='failed', phase='uncertain', error_kind='uncertain',
                               error='任务启动或执行结果不明确；未自动重试，避免重复会话或重复执行')
            self.store.update(task['id'], changes, expected_status=task['status'], expected_fields=expected)

    def _launch(self, task):
        try:
            if not os.path.isdir(task['cwd']):
                self.store.update(task['id'], dict(status='failed', phase='settled',
                                  error='工作目录不存在或无法访问', error_kind='workspace', worker_pid=None),
                                  expected_status='starting')
                return
            launcher = self.launcher
            if launcher is None:
                from task_worker import launch_worker
                launcher = launch_worker
            if self.prepare_dispatch is not None:
                task = self.prepare_dispatch(task)
                if task is None:
                    return
            launcher(task, self.store.path, self.core)
        except Exception as exc:
            # Popen may have succeeded immediately before a transport/disk failure.
            # The lease + history reconcile can later resolve this; never relaunch blindly.
            self.store.update(task['id'], dict(status='failed', phase='uncertain',
                              error='启动结果未确认: %s' % exc, error_kind='uncertain'),
                              expected_status='starting')

    def dispatch_recoveries(self, can_submit, has_capacity, lock):
        attempted = set()
        for task in self.store.list_tasks():
            if task['status'] not in RECOVERY_STATES or task['id'] in attempted:
                continue
            if task.get('retry_at', 0) > time.time():
                continue
            if task.get('worker_pid') and self._alive(task) is not False:
                continue
            with lock:
                if not can_submit() or not has_capacity():
                    return
                claimed = self.store.update(task['id'], dict(status='starting', phase='resume_claimed',
                    worker_pid=None, worker_token=str(uuid.uuid4()), attempts=task.get('attempts', 0)+1,
                    **({'recovery_turn_id': task['turn_id']} if task.get('external_recovery') else {}),
                    started_at=task.get('started_at') or time.time(), error=None, error_kind=None),
                    expected_status=task['status'])
                if claimed is None:
                    continue
                attempted.add(task['id'])
                if not can_submit():
                    self.store.update(task['id'], dict(status=task['status'], phase='settled'), expected_status='starting')
                    return
                self._launch(claimed)
            current = self.store.get(task['id'])
            if current and current['status'] == 'waiting_quota':
                return

    def dispatch_new(self, can_submit, has_capacity, lock):
        while True:
            # A failed resumed turn, including one in backoff, takes precedence.
            if any(task['status'] in RECOVERY_STATES for task in self.store.list_tasks()):
                return
            with lock:
                if not can_submit() or not has_capacity():
                    return
                task = self.store.claim_next()
                if task is None:
                    return
                if not can_submit():
                    self.store.update(task['id'], dict(status='pending', phase='pending'), expected_status='starting')
                    return
                self._launch(task)
            current = self.store.get(task['id'])
            if current and current['status'] == 'waiting_quota':
                return
