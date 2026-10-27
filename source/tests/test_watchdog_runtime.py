import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
import codex_watchdog as original
from watchdog_runtime import WatchdogService


def wait_for(check, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('condition not reached')


class FakeCore:
    def __init__(self, root):
        self.STATE_DB = os.path.join(root, 'watchdog.sqlite')
        self.CODEX_DIR = root
        self.turns = {'thread': {'turn_id': 'first', 'status': 'failed', 'error': '{"type":"usageLimitExceeded"}'}}
        self.items = []
        self.sent = []
        self.pings = 0
        self.reads = 0
        self.locked = False
        self.refuse = False
        self.quota = {'available': True, 'used': 3, 'secondary_used': 40}
        self.read_error = None
        self.block = None
        con = sqlite3.connect(os.path.join(root, 'state_5.sqlite'))
        con.execute('CREATE TABLE threads(id TEXT, archived INT, agent_path TEXT, source TEXT)')
        con.execute("INSERT INTO threads VALUES('thread',0,NULL,'cli')")
        con.commit()
        con.close()
        outer = self
        class Lock:
            def acquire(self):
                if outer.locked or outer.refuse:
                    return False
                outer.locked = True
                return True
            def release(self):
                outer.locked = False
        self.SingleInstance = Lock
    def state_connection(self):
        con = sqlite3.connect(self.STATE_DB)
        con.execute('CREATE TABLE IF NOT EXISTS resume_events(thread_id TEXT, failed_turn_id TEXT,title TEXT,cwd TEXT,state TEXT,attempts INTEGER DEFAULT 0,created_at REAL,updated_at REAL,last_error TEXT,PRIMARY KEY(thread_id,failed_turn_id))')
        con.commit()
        return con
    def read_latest_events(self, *args):
        return copy.deepcopy(self.items)
    def discover_events(self, con, args):
        for x in self.items:
            con.execute("INSERT OR IGNORE INTO resume_events VALUES(?,?,?,?, 'pending',0,?,?,NULL)", (x['thread_id'],x['turn_id'],x['title'],x['cwd'],time.time(),time.time()))
        con.commit()
        return []
    def latest_turn(self, tid):
        return copy.deepcopy(self.turns.get(tid))
    def sync_event_states(self, con):
        for tid, turn in con.execute("SELECT thread_id,failed_turn_id FROM resume_events WHERE state IN ('queued','uncertain','sending')").fetchall():
            if self.turns[tid]['turn_id'] != turn:
                self.set_event(con,tid,turn,'observed')
    def quota_from_app_server(self):
        self.reads += 1
        if self.block:
            self.block.wait(3)
        if self.read_error:
            raise RuntimeError(self.read_error)
        return copy.deepcopy(self.quota)
    def quota_from_ping(self):
        self.pings += 1
        return {'available': True, 'source': 'ping'}
    def queue_resume(self, item):
        self.sent.append(item['turn_id'])
        return True, 'accepted'
    pending_events = staticmethod(original.pending_events)
    set_event = staticmethod(original.set_event)
    matches_scope = staticmethod(original.matches_scope)
    quota_error = staticmethod(original.quota_error)
    readonly_connection = staticmethod(original.readonly_connection)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = FakeCore(self.tmp.name)
        self.service = WatchdogService(core=self.core, poll_interval=30)
    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()
    def incident(self, turn='first'):
        self.core.items = [{'thread_id':'thread','turn_id':turn,'title':'test','cwd':''}]
        self.core.turns['thread']['turn_id'] = turn
    def start_enabled(self):
        self.service.start()
        self.service.set_enabled(True)
    def test_two_distinct_failure_cycles(self):
        self.incident()
        self.start_enabled()
        wait_for(lambda: self.core.sent == ['first'])
        self.core.turns['thread']['turn_id'] = 'continued'
        self.service.request_refresh()
        wait_for(lambda: self.service.snapshot()['queued_count'] == 0)
        self.incident('second')
        self.service.request_refresh()
        wait_for(lambda: self.core.sent == ['first','second'])
    def test_stop_during_blocking_quota_prevents_send(self):
        self.incident()
        self.core.block = threading.Event()
        self.start_enabled()
        wait_for(lambda: self.core.reads > 0)
        self.service.set_enabled(False)
        self.core.block.set()
        wait_for(lambda: not self.core.locked)
        self.assertEqual(self.core.sent, [])

    def test_shutdown_during_blocking_quota_prevents_send(self):
        self.incident()
        self.core.block=threading.Event()
        self.start_enabled()
        wait_for(lambda: self.core.reads>0)
        self.service.stop(timeout=.01)
        self.core.block.set()
        wait_for(lambda: not self.service._thread.is_alive())
        self.assertEqual(self.core.sent,[])
        self.assertFalse(self.core.locked)

    def test_readonly_failure_fallback_requires_enabled_pending(self):
        self.incident()
        self.core.read_error='read-only unavailable'
        self.start_enabled()
        wait_for(lambda: self.core.sent==['first'])
        self.assertEqual(self.core.pings,1)
        self.service.request_refresh()
        wait_for(lambda: self.core.reads>=2)
        self.assertEqual(self.core.pings,1)
    def test_manual_advance_reconciled_before_quota(self):
        self.incident()
        con = self.core.state_connection()
        self.core.discover_events(con,None)
        con.close()
        self.core.items = []
        self.core.turns['thread']['turn_id'] = 'manual'
        self.start_enabled()
        wait_for(lambda: self.core.reads > 0)
        self.assertEqual(self.core.sent, [])
        self.assertEqual(self.service.snapshot()['pending_count'],0)
    def test_view_only_failure_no_ping_or_heartbeat_clobber(self):
        path = os.path.join(self.tmp.name,'monitor-status.json')
        with open(path,'w') as f: f.write('owner')
        self.core.read_error = 'offline'
        self.service.start()
        wait_for(lambda: self.service.snapshot()['quota_error'])
        self.assertEqual(self.core.pings,0)
        with open(path) as f: self.assertEqual(f.read(),'owner')
    def test_lock_refusal(self):
        self.core.refuse = True
        self.start_enabled()
        wait_for(lambda: self.service.snapshot()['monitor_state']=='blocked')
        self.assertEqual(self.core.sent,[])
    def test_error_loop_survives_and_keeps_last_data(self):
        self.service.start()
        wait_for(lambda: self.service.snapshot()['quota'] is not None)
        self.core.read_error = 'offline'
        self.service.request_refresh()
        wait_for(lambda: self.service.snapshot()['quota_error'])
        self.assertEqual(self.service.snapshot()['quota']['used'],3)
        self.core.read_error = None
        self.service.request_refresh()
        wait_for(lambda: self.service.snapshot()['quota_error'] is None)
    def test_stale_primary_probe_but_future_weekly_wait(self):
        self.incident()
        self.core.quota = {'available':False,'used':100,'reset_at':time.time()-5,'secondary_used':100,'secondary_reset_at':time.time()+1000}
        self.start_enabled()
        wait_for(lambda: self.core.reads > 0)
        self.assertEqual(self.core.pings,0)
        self.core.quota['secondary_used'] = 40
        self.service.request_refresh()
        wait_for(lambda: self.core.sent == ['first'])
        self.assertEqual(self.core.pings,1)
    def test_release_and_heartbeat_log(self):
        self.start_enabled()
        path = os.path.join(self.tmp.name,'monitor-status.json')
        wait_for(lambda: os.path.isfile(path))
        self.service.set_enabled(False)
        wait_for(lambda: not self.core.locked)
        with open(path,encoding='utf-8') as f: data=json.load(f)
        self.assertEqual(data['pid'],os.getpid())
        self.assertIn('updated_at',data)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name,'watchdog.log')))
        snap=self.service.snapshot()
        snap['quota']={'bad':True}
        self.assertNotEqual(snap,self.service.snapshot())

    def test_uncertain_delivery_never_blindly_resent(self):
        self.incident()
        def ambiguous(item):
            self.core.sent.append(item['turn_id'])
            raise RuntimeError('transport lost after submission')
        self.core.queue_resume = ambiguous
        self.start_enabled()
        wait_for(lambda: self.service.snapshot()['uncertain_count'] == 1)
        self.service.request_refresh()
        wait_for(lambda: self.core.reads >= 2)
        self.assertEqual(self.core.sent,['first'])

    def test_archived_pending_removed_before_quota_dispatch(self):
        self.incident()
        con=sqlite3.connect(os.path.join(self.tmp.name,'state_5.sqlite'))
        con.execute('UPDATE threads SET archived=1')
        con.commit()
        con.close()
        self.start_enabled()
        wait_for(lambda: self.core.reads > 0)
        self.assertEqual(self.service.snapshot()['pending_count'],0)
        self.assertEqual(self.core.sent,[])

    def test_tracked_old_incident_not_lost_to_lookback(self):
        self.incident()
        con=self.core.state_connection()
        self.core.discover_events(con,None)
        con.execute('UPDATE resume_events SET created_at=?, updated_at=?',(1,1))
        con.commit()
        con.close()
        self.core.items=[]
        self.start_enabled()
        wait_for(lambda: self.core.sent==['first'])

    def test_cycle_exception_keeps_worker_alive(self):
        original_discover=self.core.discover_events
        self.core.discover_events=lambda *args: (_ for _ in ()).throw(RuntimeError('db unavailable'))
        self.start_enabled()
        wait_for(lambda: self.service.snapshot()['monitor_state']=='error')
        self.core.discover_events=original_discover
        self.incident()
        self.service.request_refresh()
        wait_for(lambda: self.core.sent==['first'])

    def test_real_shared_lock_exclusion_and_release(self):
        previous=original.LOCK_FILE
        original.LOCK_FILE=os.path.join(self.tmp.name,'shared.lock')
        self.core.SingleInstance=original.SingleInstance
        outside=original.SingleInstance()
        try:
            self.assertTrue(outside.acquire())
            self.start_enabled()
            wait_for(lambda: self.service.snapshot()['monitor_state']=='blocked')
            outside.release()
            self.service.request_refresh()
            wait_for(lambda: self.service.snapshot()['monitor_state']=='running')
            self.assertFalse(outside.acquire())
            self.service.stop()
            self.assertTrue(outside.acquire())
        finally:
            outside.release()
            original.LOCK_FILE=previous

    def test_log_has_rotation_handler(self):
        self.service.start()
        wait_for(lambda: self.service._handler is not None)
        self.assertEqual(self.service._handler.backupCount,3)
        self.service._handler.maxBytes=100
        self.service.request_refresh()
        wait_for(lambda: os.path.isfile(os.path.join(self.tmp.name,'watchdog.log.1')))

    def test_stuck_read_has_bounded_worker_and_live_diagnostics(self):
        self.service=WatchdogService(core=self.core,poll_interval=.05,read_timeout=.08)
        self.incident()
        gate=threading.Event()
        def stuck_read():
            self.core.reads += 1
            gate.wait()
            return {'available':True}
        self.core.quota_from_app_server=stuck_read
        self.start_enabled()
        try:
            wait_for(lambda: bool(self.service.snapshot()['quota_error']))
            first=self.service.snapshot()['last_cycle_at']
            wait_for(lambda: self.service.snapshot()['last_cycle_at']>first)
            self.assertTrue(self.service.snapshot()['service_alive'])
            self.assertEqual(self.core.pings,0)
            self.assertEqual(self.core.reads,1)
            self.assertIn('phase_started_at',self.service.snapshot())
            def heartbeat_has_error():
                try:
                    with open(os.path.join(self.tmp.name,'monitor-status.json'),encoding='utf-8') as f:
                        return json.load(f)['quota_error'] is not None
                except (OSError,ValueError):
                    return False
            wait_for(heartbeat_has_error)
            started=time.monotonic()
            self.service.stop(timeout=.5)
            self.assertLess(time.monotonic()-started,.7)
            self.assertFalse(self.service.snapshot()['service_alive'])
            self.assertEqual(self.core.sent,[])
        finally:
            gate.set()

    def test_durable_error_details_and_core_sink_restore(self):
        previous=lambda message: None
        self.core.LOG_SINK=previous
        self.core.read_error='connection reset token=secret123 Authorization: Bearer abc.def.ghi'
        self.service.start()
        wait_for(lambda: self.service.snapshot()['quota_error'])
        self.assertIsNot(self.core.LOG_SINK,previous)
        self.service.stop()
        self.assertIs(self.core.LOG_SINK,previous)
        with open(os.path.join(self.tmp.name,'watchdog.log'),encoding='utf-8') as f:
            text=f.read()
        self.assertIn('connection reset',text)
        self.assertNotIn('secret123',text)
        self.assertNotIn('abc.def.ghi',text)

    def test_disable_during_guarded_preflight_never_submits(self):
        self.incident()
        entered, release=threading.Event(),threading.Event()
        def guarded(item,can_submit):
            entered.set()
            release.wait(2)
            if not can_submit():
                return False,'自动接续已关闭，未投递'
            return self.core.queue_resume(item)
        self.core.queue_resume_guarded=guarded
        self.start_enabled()
        try:
            self.assertTrue(entered.wait(1))
            self.service.set_enabled(False)
        finally:
            release.set()
        wait_for(lambda: not self.core.locked)
        self.assertEqual(self.core.sent,[])
        con=self.core.state_connection()
        try:
            self.assertEqual(con.execute('SELECT state FROM resume_events').fetchone()[0],'pending')
        finally:
            con.close()

if __name__ == '__main__': unittest.main()
