import copy
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import codex_watchdog as core
from watchdog_runtime import WatchdogService


class ExecutionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(core, 'CODEX_DIR', self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.s = sqlite3.connect(os.path.join(self.tmp.name, 'state_5.sqlite'))
        self.h = sqlite3.connect(os.path.join(self.tmp.name, 'thread_history_1.sqlite'))
        self.addCleanup(self.s.close)
        self.addCleanup(self.h.close)
        self.s.execute('CREATE TABLE threads(id TEXT, archived INT, agent_path TEXT, source TEXT)')
        self.h.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT,completed_at REAL)')

    def thread(self, tid, status, archived=0, agent_path=None, source='cli'):
        self.s.execute('INSERT INTO threads VALUES(?,?,?,?)', (tid, archived, agent_path, source))
        self.s.commit()
        self.turn(tid, status)

    def turn(self, tid, status):
        self.h.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?)', (tid, str(time.time()), status, None, None))
        self.h.commit()

    def test_latest_root_turns_only_and_unknown_status_fail_closed(self):
        self.thread('running', 'inProgress')
        self.thread('done', 'inProgress')
        self.turn('done', 'completed')
        self.thread('unknown', 'futureNewStatus')
        self.thread('cancelled', 'interrupted')
        self.thread('archived', 'inProgress', archived=1)
        self.thread('child', 'inProgress', agent_path='/root/sub')
        snap = core.execution_snapshot()
        self.assertEqual(snap['active_thread_ids'], {'running', 'unknown'})
        self.assertEqual(snap['unknown_thread_ids'], {'unknown'})

    def test_incomplete_history_fails_instead_of_reporting_zero(self):
        self.h.execute('DROP TABLE thread_turns')
        self.h.commit()
        with self.assertRaises(sqlite3.Error):
            core.execution_snapshot()


class CapacityTests(unittest.TestCase):
    def setUp(self):
        from test_watchdog_runtime import FakeCore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = FakeCore(self.tmp.name)
        self.core.execution_snapshot = lambda: {'active_thread_ids': set(self.active), 'unknown_thread_ids': set()}
        self.active = set()
        self.service = WatchdogService(core=self.core)
        def launch(task, path, core):
            core.sent.append(task['turn_id'])
            self.service._tasks().update(task['id'], dict(status='running', phase='turn_started',
                                         turn_id='started-' + task['turn_id']))
        self.service._scheduler().launcher = launch
        self.service._scheduler().worker_alive = lambda task: True
        self.service.set_enabled(True)
        self.addCleanup(self.service.stop)

    def pending(self, count):
        self.core.items = []
        con = sqlite3.connect(os.path.join(self.tmp.name, 'state_5.sqlite'))
        for i in range(count):
            tid = 'task-%s' % i
            con.execute('INSERT INTO threads VALUES(?,0,NULL,?)', (tid, 'cli'))
            self.core.turns[tid] = {'turn_id': tid, 'status': 'failed', 'error': '{"type":"usageLimitExceeded"}'}
            self.core.items.append(dict(thread_id=tid, turn_id=tid, title=tid, cwd=self.tmp.name))
        con.commit()
        con.close()

    def test_old_queued_and_live_threads_share_limit(self):
        self.active = {'manual-running'}
        self.pending(5)
        self.service._cycle()
        self.assertEqual(len(self.core.sent), 2)
        self.service._cycle()
        self.assertEqual(len(self.core.sent), 2)
        self.assertEqual(self.service.snapshot()['reserved_task_count'], 3)

    def test_invalid_and_dynamic_lower_limit_never_interrupt(self):
        for value in [True, 2, 11, 3.1, '4', None]:
            with self.assertRaises(ValueError):
                self.service.set_max_concurrent_tasks(value)
        self.service.set_max_concurrent_tasks(5)
        self.active = {'a','b','c','d'}
        self.pending(2)
        self.service.set_max_concurrent_tasks(3)
        self.service._cycle()
        self.assertEqual(self.core.sent, [])
        self.assertEqual(self.active, {'a','b','c','d'})
        self.service.set_max_concurrent_tasks(5)
        self.service._cycle()
        self.assertEqual(len(self.core.sent), 1)

    def test_scan_error_prevents_old_dispatch(self):
        self.pending(1)
        self.core.execution_snapshot = lambda: (_ for _ in ()).throw(OSError('history unavailable'))
        self.service._cycle()
        self.assertEqual(self.core.sent, [])
        self.assertIn('history unavailable', self.service.snapshot()['capacity_error'])

    def test_background_final_preflight_rechecks_lowered_limit(self):
        self.pending(1)
        self.active = {'a', 'b'}
        self.service.set_max_concurrent_tasks(4)
        def guarded(task, path, core):
            from task_scheduler import worker_dispatch_allowed
            self.active.add('manual-started-during-preflight')
            self.service.set_max_concurrent_tasks(3)
            allowed, reason = worker_dispatch_allowed(task, self.service._tasks(), core)
            self.assertFalse(allowed)
            self.assertIn('最大并发', reason)
        self.service._scheduler().launcher = guarded
        self.service._cycle()
        self.assertEqual(self.core.sent, [])


class QueueSchedulingTests(unittest.TestCase):
    def setUp(self):
        from test_watchdog_runtime import FakeCore
        from task_queue import TaskStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = FakeCore(self.tmp.name)
        self.active = set()
        self.core.execution_snapshot = lambda: {'active_thread_ids': set(self.active), 'unknown_thread_ids': set()}
        self.store = TaskStore(self.core.STATE_DB)
        self.started = []
        self.alive = set()
        self.service = WatchdogService(core=self.core)
        self.service._task_store = self.store
        self.addCleanup(self.service.stop)

    def configure(self):
        from task_scheduler import TaskScheduler
        def launch(task, db_path, core):
            self.started.append(task['id'])
            self.alive.add(task['id'])
            self.store.update(task['id'], dict(status='running', phase='turn_started',
                              thread_id=task.get('thread_id') or 'new-'+task['id'], worker_pid=123))
            return 123
        self.scheduler = TaskScheduler(self.core, self.store, launcher=launch,
                                       worker_alive=lambda task: task['id'] in self.alive)
        self.service._task_scheduler = self.scheduler

    def submit(self):
        return self.service.queue_submit({'description':'test task', 'cwd':self.tmp.name})

    def test_zero_quota_accepts_queue_restart_and_disabled_never_launches(self):
        self.configure()
        task = self.submit()
        self.core.quota = {'available':False,'used':100}
        self.service._cycle()
        self.assertEqual(self.service.snapshot()['quota']['used'], 100)
        self.assertEqual(self.service.snapshot()['recent_events'], [])
        self.assertEqual(self.started, [])
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(self.started, [])

        self.assertEqual(self.store.get(task['id'])['status'], 'pending')
        from task_queue import TaskStore
        self.assertEqual(TaskStore(self.core.STATE_DB).get(task['id'])['description'], 'test task')
        self.service.set_enabled(False)
        self.core.quota = {'available':True}
        self.service._cycle()
        self.assertEqual(self.started, [])

    def test_new_only_stale_reset_uses_existing_confirmation_but_future_reset_does_not(self):
        self.configure()
        self.submit()
        self.core.quota = {'available':False, 'used':100, 'reset_at':time.time()+3600}
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(self.core.pings, 0)
        self.assertEqual(self.started, [])
        self.core.quota['reset_at'] = time.time()-60
        self.service._cycle()
        self.assertEqual(self.core.pings, 1)
        self.assertEqual(len(self.started), 1)

    def test_available_launches_up_to_capacity_and_completion_releases_slot(self):
        self.configure()
        tasks = [self.submit() for _ in range(4)]
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(len(self.started), 3)
        finished = self.store.get(tasks[0]['id'])
        self.alive.remove(finished['id'])
        self.core.turns[finished['thread_id']] = dict(turn_id='done', status='completed', error=None)
        self.service._cycle()
        self.assertEqual(len(self.started), 4)
        self.assertEqual(self.store.get(finished['id'])['status'], 'completed')

    def test_legacy_backoff_reserves_precedence_and_owned_resumes_never_queue(self):
        self.configure()
        self.submit()
        con = self.core.state_connection()
        self.core.items = [dict(thread_id='thread',turn_id='first',title='old',cwd='')]
        self.core.discover_events(con, None)
        self.core.set_event(con, 'thread', 'first', 'pending', attempt_delta=1)
        con.close()
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(self.started, [])
        owned = self.submit()
        self.store.update(owned['id'], dict(thread_id='thread', status='waiting_quota', phase='settled'))
        self.service._cycle()
        self.assertEqual(self.started[0], owned['id'])
        self.assertEqual(self.core.sent, [])

    def test_uncertain_creation_retains_slot_and_is_not_automatically_retried(self):
        self.configure()
        task = self.submit()
        self.store.update(task['id'], dict(status='failed',phase='uncertain',error_kind='uncertain'))
        for _ in range(4):
            self.submit()
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(len(self.started), 2)
        self.assertNotIn(task['id'], self.started)

    def test_quota_failure_after_recovery_blocks_further_new_starts(self):
        self.configure()
        def quota_failure(task, path, core):
            self.started.append(task['id'])
            self.store.update(task['id'], dict(status='waiting_quota', phase='settled',
                              thread_id='owned-'+task['id'], retry_at=time.time()+60))
        self.scheduler.launcher = quota_failure
        for _ in range(3):
            self.submit()
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(len(self.started), 1)

    def test_restart_does_not_duplicate_detached_worker_or_uncertain_turn(self):
        self.configure()
        task = self.submit()
        self.service.set_enabled(True)
        self.service._cycle()
        from task_scheduler import TaskScheduler
        restarted = TaskScheduler(self.core, self.store, launcher=self.scheduler.launcher,
                                  worker_alive=lambda task: task['id'] in self.alive)
        restarted.reconcile()
        self.assertEqual(self.store.get(task['id'])['status'], 'running')
        self.assertEqual(len(self.started), 1)
        self.alive.clear()
        restarted.reconcile()
        current = self.store.get(task['id'])
        self.assertEqual(current['error_kind'], 'uncertain')
        self.assertTrue(restarted.reservation_tokens())
        with self.assertRaises(ValueError):
            self.store.retry(task['id'])

    def test_crash_after_uuid_before_turn_requeues_only_the_empty_attempt(self):
        self.configure()
        task = self.submit()
        claimed = self.store.claim_next()
        self.store.update(task['id'], dict(thread_id='empty-thread',phase='thread_created'))
        self.scheduler.reconcile()
        restored = self.store.get(task['id'])
        self.assertEqual((restored['status'],restored['thread_id']), ('pending',None))
        self.assertEqual(restored['discarded_thread_id'], 'empty-thread')
        self.assertIsNone(self.store.update(task['id'], {'status':'running'},
            expected_fields={'worker_token':claimed['worker_token']}))
        self.assertEqual(self.store.claim_next()['id'], task['id'])

    def test_client_cancel_and_deleted_workspace_do_not_retry(self):
        self.configure()
        cancelled = self.submit()
        self.store.update(cancelled['id'], dict(status='running',phase='turn_started', thread_id='cancelled'))
        self.core.turns['cancelled'] = dict(turn_id='cancel',status='interrupted')
        gone = os.path.join(self.tmp.name,'gone')
        os.mkdir(gone)
        deleted = self.service.queue_submit({'description':'deleted directory','cwd':gone})
        os.rmdir(gone)
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(self.store.get(cancelled['id'])['status'], 'cancelled')
        self.assertEqual(self.store.get(deleted['id'])['error_kind'], 'workspace')
        self.assertEqual(self.started, [])

    def test_live_authorization_reserves_slot_but_confirmed_timeout_releases(self):
        self.configure()
        waiting = self.submit()
        self.store.update(waiting['id'], dict(status='waiting_authorization',phase='turn_started',
                                             thread_id='auth', worker_pid=123))
        self.alive.add(waiting['id'])
        for _ in range(3):
            self.submit()
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(len(self.started), 2)
        self.alive.remove(waiting['id'])
        self.store.update(waiting['id'], dict(phase='approval_timeout',worker_pid=None))
        self.service._cycle()
        self.assertEqual(len(self.started), 3)

    def test_owned_failed_record_recovers_in_background_after_manual_turn(self):
        # A terminal task record must not permanently suppress later client turns.
        self.configure()
        task = self.submit()
        self.store.update(task['id'], dict(status='failed',phase='settled', thread_id='thread'))
        self.core.items = [dict(thread_id='thread',turn_id='first',title='continued manually',cwd=self.tmp.name)]
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(self.started, [task['id']])
        self.assertEqual(self.core.sent, [])

    def test_manual_completion_while_waiting_quota_prevents_auto_resume(self):
        self.configure()
        task = self.submit()
        self.store.update(task['id'], dict(status='waiting_quota',phase='settled',
                         thread_id='thread',turn_id='first',error_kind='quota'))
        self.core.turns['thread'] = dict(turn_id='manual',status='completed')
        self.service.set_enabled(True)
        self.service._cycle()
        self.assertEqual(self.started, [])
        current = self.store.get(task['id'])
        self.assertEqual((current['status'],current['turn_id']),('completed','manual'))

    def test_lost_resume_ack_does_not_treat_old_quota_turn_as_new_outcome(self):
        self.configure()
        task = self.submit()
        self.store.update(task['id'], dict(status='starting', phase='turn_start_sent',
                         thread_id='thread', turn_id='first', previous_turn_id='first', worker_pid=987))
        self.service.set_enabled(True)
        for _ in range(3):
            self.service._cycle()
        record = self.store.get(task['id'])
        self.assertEqual(record['error_kind'], 'uncertain')
        self.assertEqual(self.started, [])
        self.assertEqual(self.core.sent, [])

    def test_fresh_worker_quota_failure_invalidates_dispatch_permit(self):
        self.configure()
        task = self.submit()
        self.service.set_enabled(True)
        claimed = self.store.claim_next()
        self.service._update(quota={'available':True},quota_updated_at=time.time()-1)
        claimed = self.service._prepare_task_dispatch(claimed)
        blocked = self.submit()
        self.store.update(blocked['id'],dict(status='waiting_quota',error_kind='quota',phase='settled'))
        from task_scheduler import worker_dispatch_allowed
        allowed, reason = worker_dispatch_allowed(claimed,self.store,self.core)
        self.assertFalse(allowed)
        self.assertIn('额度',reason)

    def test_worker_final_permit_rechecks_generation_and_lowered_limit(self):
        self.configure()
        self.service.set_enabled(True)
        self.service.set_max_concurrent_tasks(5)
        self.service._update(quota={'available':True},quota_updated_at=time.time())
        claimed = []
        for _ in range(5):
            self.submit()
            claimed.append(self.service._prepare_task_dispatch(self.store.claim_next()))
        self.service.set_max_concurrent_tasks(3)
        from task_scheduler import worker_dispatch_allowed
        self.assertFalse(worker_dispatch_allowed(claimed[4],self.store,self.core)[0])
        self.assertTrue(worker_dispatch_allowed(claimed[0],self.store,self.core)[0])
        self.service.set_enabled(False)
        self.assertFalse(worker_dispatch_allowed(claimed[1],self.store,self.core)[0])

    def test_worker_honors_control_change_during_execution_scan(self):
        import threading
        from task_scheduler import worker_dispatch_allowed

        self.configure()
        self.service.set_enabled(True)
        self.service.set_max_concurrent_tasks(5)
        self.service._update(quota={'available':True},quota_updated_at=time.time())
        self.submit()
        claimed = self.service._prepare_task_dispatch(self.store.claim_next())
        for change in ('lower_limit', 'disable'):
            with self.subTest(change=change):
                entered, release = threading.Event(), threading.Event()
                def scan():
                    entered.set()
                    release.wait(3)
                    return {'active_thread_ids': {'one', 'two', 'three'}}
                self.core.execution_snapshot = scan
                decisions = []
                worker = threading.Thread(target=lambda: decisions.append(
                    worker_dispatch_allowed(claimed, self.store, self.core)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    if change == 'lower_limit':
                        self.service.set_max_concurrent_tasks(3)
                    else:
                        self.service.set_enabled(False)
                finally:
                    release.set()
                    worker.join(4)
                self.assertEqual([allowed for allowed, _ in decisions], [False])
                self.assertNotEqual(self.store.get(claimed['id'])['phase'], 'turn_start_sent')
                self.service.set_max_concurrent_tasks(5)


if __name__ == '__main__':
    unittest.main()
