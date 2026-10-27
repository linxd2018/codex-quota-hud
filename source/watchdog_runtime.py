"""Threaded quota display and opt-in continuation; no UI or interactive setup."""
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace


class WatchdogService:
    def __init__(self, core=None, poll_interval=60, thread_ids=None,
                 directories=None, lookback_hours=72, display_mode=True, read_timeout=25):
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
        self._data = dict(quota=None, quota_updated_at=None, quota_error=None,
                          refreshing=False, enabled=False, monitor_state='off',
                          status='自动接续已关闭', pending_count=0, queued_count=0,
                          uncertain_count=0, confirmed_count=0, superseded_count=0,
                          observed_count=0, recent_events=[], last_action='', phase='idle',
                          phase_started_at=None, last_cycle_at=None)
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
        self._wake.set()

    def request_refresh(self):
        self._wake.set()

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
        if not os.path.isfile(self.core.STATE_DB):
            self._update(pending_count=0, queued_count=0, uncertain_count=0,
                         confirmed_count=0, superseded_count=0, observed_count=0,
                         recent_events=[])
            return
        con = self.core.readonly_connection(self.core.STATE_DB)
        try:
            self._counts(con)
        finally:
            con.close()

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
                        ok, detail = guarded(item, lambda: self._active(generation))
                    else:
                        ok, detail = self.core.queue_resume(item)
            except Exception as exc:
                ok, detail = None, '投递异常，结果不明确: %s' % type(exc).__name__
            state = 'queued' if ok is True else 'pending' if ok is False else 'uncertain'
            self.core.set_event(con,tid,turn,state,detail,attempt_delta=1)
            action = '续接已排队' if ok is True else '续接失败，稍后重试' if ok is False else '投递结果不明确，需人工核查'
            self._update(last_action=action)
            self._log('thread=%s turn=%s outcome=%s' % (tid,turn,state))

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
                self._counts(con)
                self._heartbeat()
            if not owner:
                self._read_history()
            quota = self._read_quota(pending, generation) if self.display_mode or owner else None
            if owner and self._active(generation):
                if quota and quota.get('available') is True:
                    self._send(con,pending,generation)
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
                self._wake.wait(self.poll_interval)
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
