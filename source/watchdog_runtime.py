"""Threaded quota display and opt-in continuation; no UI or interactive setup."""
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace


class WatchdogService:
    def __init__(self, core=None, poll_interval=60, thread_ids=None,
                 directories=None, lookback_hours=72, display_mode=True, read_timeout=25,
                 max_concurrent_tasks=3):
        if core is None:
            import codex_watchdog as core
        self.core = core
        self.poll_interval = max(.05, float(poll_interval))
        self.args = SimpleNamespace(thread=thread_ids, cwd=directories,
                                    lookback_hours=lookback_hours)
        self.display_mode = display_mode
        self.read_timeout = max(.01, float(read_timeout))
        self._inflight = None
        self._mutex = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._thread = None
        self._instance = None
        self._generation = 0
        self._max_concurrent_tasks = self._validate_max_concurrent_tasks(max_concurrent_tasks)
        self._task_store = None
        self._task_scheduler = None
        self._capabilities_requested = False
        self._dispatch_owner = str(uuid.uuid4())
        self._dispatch_control_initialized = False
        self._data = dict(quota=None, quota_updated_at=None, quota_error=None,
                          refreshing=False, enabled=False, monitor_state='off',
                          status='自动接续已关闭', pending_count=0, queued_count=0,
                          uncertain_count=0, confirmed_count=0, superseded_count=0,
                          observed_count=0, recent_events=[], last_action='', phase='idle',
                          phase_started_at=None, last_cycle_at=None,
                          maxConcurrentTasks=self._max_concurrent_tasks,
                          running_task_count=0, reserved_task_count=0, capacity_error=None,
                          new_tasks=[], task_capabilities=None, task_capabilities_loading=False,
                          task_capabilities_error=None)
        self._root = os.path.dirname(os.path.abspath(core.STATE_DB))
        self._logger = logging.getLogger('watchdog.runtime.%s' % id(self))
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = None

    def start(self):
        with self._mutex:
            if self._thread and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._thread = threading.Thread(target=self._run, name='quota-watchdog', daemon=True)
            self._thread.start()

    def set_enabled(self, enabled):
        with self._mutex:
            self._generation += 1
            self._data['enabled'] = bool(enabled) and not self._shutdown.is_set()
            self._data['monitor_state'] = 'starting' if self._data['enabled'] else (
                'stopping' if self._instance else 'off')
            self._data['status'] = '正在启动自动接续' if self._data['enabled'] else '自动接续已关闭'
            self._publish_dispatch_control()
        self._wake.set()

    def request_refresh(self):
        self._wake.set()

    @staticmethod
    def _validate_max_concurrent_tasks(value):
        from task_queue import validate_concurrency
        return validate_concurrency(value)

    def set_max_concurrent_tasks(self, value):
        value = self._validate_max_concurrent_tasks(value)
        with self._mutex:
            self._max_concurrent_tasks = value
            self._data['maxConcurrentTasks'] = value
            self._publish_dispatch_control()
        self._wake.set()

    def _tasks(self):
        with self._mutex:
            if self._task_store is None:
                from task_queue import TaskStore
                self._task_store = TaskStore(path=self.core.STATE_DB)
            return self._task_store

    def _scheduler(self):
        if self._task_scheduler is None:
            from task_scheduler import TaskScheduler
            self._task_scheduler = TaskScheduler(self.core, self._tasks(), prepare_dispatch=self._prepare_task_dispatch)
        return self._task_scheduler

    def _publish_dispatch_control(self):
        if not self._dispatch_control_initialized:
            return
        quota = self._data.get('quota') or {}
        data = dict(owner=self._dispatch_owner, generation=self._generation,
                    enabled=self._active(), maxConcurrentTasks=self._max_concurrent_tasks,
                    quota_available=quota.get('available') is True and not self._data.get('quota_error'),
                    quota_confirmed_at=self._data.get('quota_updated_at'),
                    expires_at=time.time()+max(120, self.poll_interval*3))
        self._tasks().publish_dispatch_control(data)

    def _prepare_task_dispatch(self, task):
        with self._mutex:
            self._dispatch_control_initialized = True
            self._publish_dispatch_control()
            return self._tasks().update(task['id'], dict(dispatch_owner=self._dispatch_owner,
                dispatch_generation=self._generation), expected_status='starting',
                expected_fields={'worker_token':task['worker_token']})

    def queue_submit(self, spec):
        task = self._tasks().submit(spec)
        self._refresh_tasks()
        self._wake.set()
        return task

    def queue_update(self, task_id, patch):
        task = self._tasks().edit(task_id, patch)
        self._refresh_tasks()
        self._wake.set()
        return task

    def queue_delete(self, task_id):
        result = self._tasks().delete(task_id)
        self._refresh_tasks()
        self._wake.set()
        return result

    def queue_reprioritize(self, task_id, priority):
        result = self._tasks().reprioritize(task_id, priority)
        self._refresh_tasks()
        self._wake.set()
        return result

    def queue_retry(self, task_id):
        result = self._tasks().retry(task_id)
        self._refresh_tasks()
        self._wake.set()
        return result

    def queue_respond_approval(self, task_id, displayed_request, decision):
        result = self._tasks().respond_approval(task_id, displayed_request, decision)
        self._refresh_tasks()
        self._wake.set()
        return result

    def request_task_capabilities(self):
        with self._mutex:
            self._capabilities_requested = True
            self._data.update(task_capabilities_loading=True, task_capabilities_error=None)
        self._wake.set()

    def _read_task_capabilities(self):
        with self._mutex:
            requested = self._capabilities_requested
            self._capabilities_requested = False
        if not requested:
            return
        try:
            from codex_app_server import read_capabilities
            self._update(task_capabilities=read_capabilities(self.core), task_capabilities_error=None)
        except Exception as exc:
            self._update(task_capabilities_error=self._safe(exc))
        finally:
            self._update(task_capabilities_loading=False)

    def _refresh_tasks(self):
        # Opening the old HUD in view-only mode must not create its database.
        if self._task_store is None:
            if not os.path.isfile(self.core.STATE_DB):
                return []
            con = self.core.readonly_connection(self.core.STATE_DB)
            try:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='new_tasks'").fetchone():
                    return []
            finally:
                con.close()
        tasks = self._tasks().list_tasks()
        self._update(new_tasks=tasks)
        return tasks

    def _capacity(self, con, sending_thread=None):
        try:
            # Injected legacy cores may predate scanning; production always has it.
            scan = getattr(self.core, 'execution_snapshot', None)
            current = scan() if scan else {'active_thread_ids': set()}
            active = set(current['active_thread_ids'])
            if sending_thread in active:
                return False
            reserved = {'thread:' + tid for tid in active}
            for (tid,) in con.execute("SELECT DISTINCT thread_id FROM resume_events WHERE state IN ('sending','queued','uncertain')"):
                reserved.add('thread:' + tid)
            if self._task_scheduler:
                reserved.update(self._task_scheduler.reservation_tokens())
            running = {'thread:' + tid for tid in active}
            if self._task_scheduler:
                running.update(self._task_scheduler.running_tokens())
            self._update(running_task_count=len(running), reserved_task_count=len(reserved), capacity_error=None)
            if self._task_scheduler:
                confirmed = self._data.get('quota_updated_at') or 0
                if any(task.get('error_kind') == 'quota' and task['updated_at'] > confirmed
                       for task in self._tasks().list_tasks()):
                    return False
            if sending_thread:
                reserved.discard('thread:' + sending_thread)
            return len(reserved) < self._max_concurrent_tasks
        except Exception as exc:
            self._update(capacity_error=self._safe(exc))
            self._log('capacity scan failed; dispatch paused: %s' % exc)
            return False

    def stop(self, timeout=2):
        self._shutdown.set()
        self.set_enabled(False)
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def snapshot(self):
        with self._mutex:
            data = copy.deepcopy(self._data)
            data['service_alive'] = bool(self._thread and self._thread.is_alive())
            return data

    def _update(self, **values):
        with self._mutex:
            self._data.update(values)

    def _active(self, generation=None):
        with self._mutex:
            return (self._data['enabled'] and not self._shutdown.is_set() and
                    (generation is None or generation == self._generation))

    def _log(self, message):
        self._logger.info('pid=%s %s', os.getpid(), self._safe(message))

    @staticmethod
    def _safe(value):
        value = str(value).replace('\n', ' ').replace('\r', ' ')
        value = re.sub(r'''(?i)(["']?(?:access_token|refresh_token|id_token|token|authorization|api[_-]?key|password)["']?\s*[:=]\s*)(["'])(.*?)\2''', r'\1\2[redacted]\2', value)
        value = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[redacted]', value)
        value = re.sub(r'(?i)((?:access_token|refresh_token|id_token|token|authorization|api[_-]?key|password)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', value)
        return value[:600]

    @contextmanager
    def _stage(self, phase):
        started = time.monotonic()
        self._update(phase=phase, phase_started_at=time.time())
        self._log('stage=%s begin' % phase)
        self._heartbeat()
        try:
            yield
        except Exception as exc:
            self._log('stage=%s failed elapsed=%.3fs %s: %s' % (
                phase,time.monotonic()-started,type(exc).__name__,exc))
            raise
        else:
            self._log('stage=%s end elapsed=%.3fs' % (phase,time.monotonic()-started))

    def _bounded_quota_call(self, function):
        # At most one quota call may remain outstanding, even across stop/start.
        # Late results are discarded; they cannot authorize sending or fallback.
        if self._inflight and not self._inflight['done'].is_set():
            raise TimeoutError('previous quota call still in flight; no new probe started')
        call = dict(done=threading.Event())
        def invoke():
            try:
                call['result'] = function()
            except Exception as exc:
                call['error'] = exc
            finally:
                call['done'].set()
        call['thread'] = threading.Thread(target=invoke,name='quota-single-flight',daemon=True)
        self._inflight = call
        call['thread'].start()
        deadline = time.monotonic() + self.read_timeout
        while not call['done'].wait(min(.05,max(0,deadline-time.monotonic()))):
            if self._shutdown.is_set():
                raise RuntimeError('quota call abandoned during shutdown')
            if time.monotonic() >= deadline:
                raise TimeoutError('quota call exceeded %.2fs; still in flight' % self.read_timeout)
        if 'error' in call:
            raise call['error']
        return call['result']

    def _heartbeat(self):
        # Only the lock owner may publish the automation heartbeat.
        if not self._instance:
            return
        data = self.snapshot()
        data.update(pid=os.getpid(), updated_at=time.time())
        filename = os.path.join(self._root, 'monitor-status.json')
        temporary = filename + '.%s.tmp' % os.getpid()
        try:
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False)
            os.replace(temporary, filename)
        except OSError as exc:
            self._update(last_action='心跳写入失败: ' + self._safe(exc))
            self._log('heartbeat write failed: %s' % exc)

    def _release(self):
        if self._instance:
            self._update(monitor_state='off', status='自动接续已关闭')
            try:
                self._log('automation stopped')
                self._heartbeat()
            finally:
                self._instance.release()
                self._instance = None

    def _ensure_owner(self):
        if self._instance:
            return True
        instance = self.core.SingleInstance()
        if not instance.acquire():
            self._update(monitor_state='blocked', status='另一个看门狗正在运行')
            return False
        self._instance = instance
        self._log('automation lock acquired')
        self._update(monitor_state='running', status='自动接续运行中')
        self._heartbeat()
        return True

    def _eligible(self, tid):
        con = self.core.readonly_connection(os.path.join(self.core.CODEX_DIR, 'state_5.sqlite'))
        try:
            row = con.execute('SELECT archived,agent_path,source FROM threads WHERE id=?', (tid,)).fetchone()
            return bool(row and not row[0] and not row[1] and 'subagent' not in (row[2] or '').lower())
        finally:
            con.close()

    def _reconcile(self, con):
        # Tracked incidents have no lookback cutoff. Never mistake age for resolution.
        rows = con.execute("SELECT thread_id,failed_turn_id,cwd FROM resume_events WHERE state='pending'").fetchall()
        for tid, turn, cwd in rows:
            if not self.core.matches_scope(tid, cwd, self.args.thread, self.args.cwd):
                continue
            if not self._eligible(tid):
                self.core.set_event(con, tid, turn, 'ignored', '会话已归档、删除或为子代理')
                continue
            latest = self.core.latest_turn(tid)
            if latest and latest['turn_id'] != turn:
                self.core.set_event(con, tid, turn, 'superseded', '会话已有后续 turn')
            elif latest and (latest.get('status') != 'failed' or not self.core.quota_error(latest.get('error'))):
                self.core.set_event(con, tid, turn, 'ignored', '最新 turn 不再是额度失败')

    def _counts(self, con):
        counts = dict(pending_count=0, queued_count=0, uncertain_count=0,
                      confirmed_count=0, superseded_count=0, observed_count=0)
        recent = []
        rows = con.execute("""SELECT state,thread_id,cwd,failed_turn_id,title,
            attempts,updated_at,last_error FROM resume_events ORDER BY updated_at DESC""")
        for state, tid, cwd, turn, title, attempts, updated, error in rows:
            if not self.core.matches_scope(tid, cwd, self.args.thread, self.args.cwd):
                continue
            key = state + '_count'
            if key in counts:
                counts[key] += 1
            elif state == 'sending':
                counts['uncertain_count'] += 1
            if len(recent) < 30:
                recent.append(dict(thread_id=self._safe(tid), turn_id=self._safe(turn),
                                   title=self._safe(title or ''), state=self._safe(state),
                                   attempts=attempts, updated_at=updated,
                                   error=self._safe(error) if error else None))
        self._update(recent_events=recent, **counts)

    def _read_history(self):
        # Existing own database only: no schema creation, discovery or reconciliation.
        if os.path.isfile(self.core.STATE_DB):
            con = self.core.readonly_connection(self.core.STATE_DB)
            try:
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resume_events'").fetchone():
                    self._counts(con)
                    return
            finally:
                con.close()
        self._update(pending_count=0, queued_count=0, uncertain_count=0,
                     confirmed_count=0, superseded_count=0, observed_count=0,
                     recent_events=[])

    @staticmethod
    def _stale_block(quota):
        if quota.get('available') is not False:
            return False
        now = time.time()
        expired = False
        # Any still-exhausted future window is enough to prevent probing.
        for used_key, reset_key in [('used','reset_at'), ('secondary_used','secondary_reset_at')]:
            try:
                exhausted = float(quota.get(used_key)) >= 100
            except (TypeError, ValueError):
                exhausted = False
            if exhausted:
                try:
                    reset = float(quota.get(reset_key))
                except (TypeError, ValueError):
                    return False
                if reset > now:
                    return False
                expired = True
        return expired

    def _read_quota(self, pending, generation):
        self._update(refreshing=True)
        try:
            try:
                with self._stage('quota_readonly'):
                    quota = self._bounded_quota_call(self.core.quota_from_app_server)
                readonly_error = None
            except Exception as exc:
                quota, readonly_error = None, self._safe(exc)
            may_probe = pending and self._active(generation)
            needs_confirmation = getattr(self.core, 'needs_reset_confirmation', self._stale_block)
            if may_probe and (quota is None or needs_confirmation(quota)):
                self._log('read-only quota unavailable or exhausted reset expired; verifying fallback')
                # A timed-out read must finish before any new network call.
                if self._inflight and not self._inflight['done'].is_set():
                    raise RuntimeError(readonly_error or 'quota call still in flight')
                with self._stage('quota_fallback'):
                    quota = self._bounded_quota_call(self.core.quota_from_ping)
                if readonly_error:
                    quota['fallback_reason'] = readonly_error
            if quota is None:
                raise RuntimeError(readonly_error or '额度不可用')
            self._update(quota=copy.deepcopy(quota), quota_updated_at=time.time(), quota_error=None)
            self._log('quota available=%s used=%s secondary_used=%s source=%s' % (
                quota.get('available'),quota.get('used'),quota.get('secondary_used'),quota.get('source')))
            return quota
        except Exception as exc:
            self._update(quota_error=self._safe(exc))
            self._log('quota query failed: %s: %s' % (type(exc).__name__,exc))
            return None
        finally:
            self._update(refreshing=False)

    def _send(self, con, pending, generation):
        for item in pending:
            if not self._active(generation):
                return
            tid, turn = item['thread_id'], item['turn_id']
            if self._task_scheduler and tid in self._task_scheduler.owned_thread_ids():
                self.core.set_event(con, tid, turn, 'queue_owned', '由新任务队列使用原会话 UUID 接续')
                continue
            if not self._eligible(tid):
                self.core.set_event(con,tid,turn,'ignored','会话已归档或为子代理')
                continue
            current = self.core.latest_turn(tid)
            if not current:
                continue
            if current['turn_id'] != turn:
                self.core.set_event(con,tid,turn,'superseded')
                continue
            if current.get('status') != 'failed' or not self.core.quota_error(current.get('error')):
                continue
            # Serialize the final permission check and commit of sending intent.
            # Once committed, this call is in flight; disabling still prevents the next item.
            with self._mutex:
                if not self._active(generation):
                    return
                if not self._capacity(con):
                    return
                self.core.set_event(con,tid,turn,'sending')
                self._update(monitor_state='sending',status='正在投递原会话续接')
            self._heartbeat()
            if not self._active(generation):
                # Sending intent was persisted but no queue call began.
                self.core.set_event(con,tid,turn,'pending','自动接续已关闭，未投递')
                return
            try:
                with self._stage('queue_resume'):
                    guarded = getattr(self.core, 'queue_resume_guarded', None)
                    if guarded:
                        ok, detail = guarded(item, lambda: self._active(generation) and self._capacity(con, tid))
                    else:
                        ok, detail = self.core.queue_resume(item)
            except Exception as exc:
                ok, detail = None, '投递异常，结果不明确: %s' % type(exc).__name__
            state = 'queued' if ok is True else 'pending' if ok is False else 'uncertain'
            self.core.set_event(con,tid,turn,state,detail,attempt_delta=1)
            action = '续接已排队' if ok is True else '续接失败，稍后重试' if ok is False else '投递结果不明确，需人工核查'
            self._update(last_action=action)
            self._log('thread=%s turn=%s outcome=%s' % (tid,turn,state))

    def _dispatch_tasks(self, con, generation):
        if not self._task_scheduler:
            return
        can_submit = lambda: self._active(generation)
        has_capacity = lambda: self._capacity(con)
        self._task_scheduler.dispatch_recoveries(can_submit, has_capacity, self._mutex)
        # Even an old incident in backoff keeps precedence over a brand new task.
        owned = self._task_scheduler.owned_thread_ids()
        old_waiting = any(tid not in owned for (tid,) in con.execute(
            "SELECT thread_id FROM resume_events WHERE state='pending'"))
        if not old_waiting:
            self._task_scheduler.dispatch_new(can_submit, has_capacity, self._mutex)

    def _cycle(self):
        self._update(last_cycle_at=time.time())
        enabled = self._active()
        if not enabled:
            self._release()
        owner = enabled and self._ensure_owner()
        with self._mutex:
            generation = self._generation
        con = None
        try:
            self._read_task_capabilities()
            tasks = self._refresh_tasks()
            if tasks:
                self._scheduler().reconcile()
                self._refresh_tasks()
            pending = []
            if owner:
                # sqlite connections are opened and closed exclusively on this worker.
                con = self.core.state_connection()
                with self._stage('discover_events'):
                    self.core.discover_events(con, self.args)
                with self._stage('sync_event_states'):
                    self.core.sync_event_states(con)
                with self._stage('reconcile_pending'):
                    self._reconcile(con)
                    pending = self.core.pending_events(con, self.args)
                    if self._task_scheduler:
                        owned = self._task_scheduler.owned_thread_ids()
                        for item in pending:
                            if item['thread_id'] in owned:
                                self.core.set_event(con, item['thread_id'], item['turn_id'], 'queue_owned',
                                                    '由新任务队列使用原会话 UUID 接续')
                        pending = [item for item in pending if item['thread_id'] not in owned]
                self._counts(con)
                self._heartbeat()
            if not owner:
                self._read_history()
            queued_intent = any(item['status'] in ('pending', 'waiting_quota', 'waiting_resume')
                                for item in self.snapshot().get('new_tasks', []))
            quota = self._read_quota(bool(pending) or queued_intent, generation) if self.display_mode or owner else None
            with self._mutex:
                self._publish_dispatch_control()
            if owner and self._active(generation):
                if quota and quota.get('available') is True:
                    self._send(con,pending,generation)
                    if self._active(generation):
                        self._dispatch_tasks(con, generation)
                self._capacity(con)
                self._refresh_tasks()
                self._counts(con)
                if self._active(generation):
                    failed = self.snapshot()['quota_error']
                    self._update(monitor_state='error' if failed else 'waiting' if pending and not (quota and quota.get('available')) else 'running',
                                 status='额度读取失败，将重试' if failed else '等待额度恢复' if pending and not (quota and quota.get('available')) else '自动接续运行中')
            if not self._active():
                self._release()
        finally:
            if con:
                con.close()
            self._heartbeat()

    def _run(self):
        previous_sink = getattr(self.core,'LOG_SINK',None)
        sink = self._log
        try:
            os.makedirs(self._root, exist_ok=True)
            self._handler = RotatingFileHandler(os.path.join(self._root,'watchdog.log'),
                                                maxBytes=1024*1024, backupCount=3, encoding='utf-8')
            self._handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self._logger.addHandler(self._handler)
            self.core.LOG_SINK = sink
            self._log('service started display_mode=%s' % self.display_mode)
            while not self._shutdown.is_set():
                self._wake.clear()
                try:
                    self._cycle()
                except Exception as exc:
                    self._update(monitor_state='error',status='本轮监控失败，将重试',last_action=type(exc).__name__)
                    self._log('monitor cycle failed: %s: %s' % (type(exc).__name__,exc))
                    try:
                        self._heartbeat()
                    except Exception:
                        self._log('heartbeat write failed')
                self._update(phase='idle',phase_started_at=time.time())
                self._heartbeat()
                deadline = time.monotonic() + self.poll_interval
                while not self._wake.wait(min(.5, max(0, deadline-time.monotonic()))):
                    if self._shutdown.is_set() or time.monotonic() >= deadline:
                        break
                    if self._task_store is not None:
                        try:
                            before = {item['id']: item['status'] for item in self.snapshot()['new_tasks']}
                            tasks = self._refresh_tasks()
                            # Approval/details reach the HUD promptly. Only a turn
                            # outcome wakes the existing quota/scheduling cycle.
                            if any(item['status'] in ('completed', 'failed', 'cancelled', 'waiting_quota')
                                   and before.get(item['id']) != item['status'] for item in tasks):
                                break
                        except Exception as exc:
                            self._log('queue refresh failed: %s' % exc)
        finally:
            try:
                self._release()
                self._update(refreshing=False,monitor_state='off',status='自动接续已关闭',phase='stopped',phase_started_at=time.time())
                self._log('service exited')
            finally:
                if getattr(self.core,'LOG_SINK',None) is sink:
                    self.core.LOG_SINK = previous_sink
                if self._handler:
                    self._logger.removeHandler(self._handler)
                    self._handler.close()
