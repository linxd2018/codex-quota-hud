from pathlib import Path
import json, os, sqlite3, tempfile, time, unittest
from unittest.mock import patch
import codex_watchdog as core
from watchdog_runtime import WatchdogService

class DeliveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.p=patch.multiple(core,CODEX_DIR=self.tmp.name,STATE_DB=os.path.join(self.tmp.name,'own.sqlite')); self.p.start(); self.addCleanup(self.p.stop)
  self.h=sqlite3.connect(os.path.join(self.tmp.name,'thread_history_1.sqlite')); self.addCleanup(self.h.close)
  self.h.executescript('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT,completed_at REAL); CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_type TEXT,item_json TEXT);')
  self.turn('t','f'); self.c=core.state_connection(); self.addCleanup(self.c.close)
  self.c.execute("INSERT INTO resume_events VALUES('t','f','title','','queued',1,0,0,NULL)"); self.c.commit()
 def turn(self,tid,turn):
  self.h.execute("INSERT INTO thread_turns VALUES(?,?,'failed',NULL,0)",(tid,turn)); self.h.commit()
 def message(self,tid,turn,kind='userMessage',text=None):
  self.h.execute('INSERT INTO thread_items VALUES(?,?,?,?)',(tid,turn,kind,json.dumps({'type':kind,'content':[{'type':'text','text':text or core.resume_marker({'thread_id':'t','turn_id':'f'})}]}))); self.h.commit()
 def state(self): return self.c.execute('SELECT state FROM resume_events').fetchone()[0]
 def test_marker_stable_distinct_and_in_message(self):
  item={'thread_id':'t','turn_id':'f'}; m=core.resume_marker(item)
  self.assertIn(m,core.resume_message(item)); self.assertEqual(m,core.resume_marker(item)); self.assertNotEqual(m,core.resume_marker(dict(item,turn_id='g')))
 def test_new_turn_is_superseded(self):
  self.turn('t','n'); core.sync_event_states(self.c); self.assertEqual(self.state(),'superseded')
 def test_matching_user_message_confirms(self):
  self.turn('t','n'); self.message('t','n'); self.assertEqual(core.confirmation_turn('t','f'),'n'); core.sync_event_states(self.c); self.assertEqual(self.state(),'confirmed')
 def test_wrong_thread_type_failed_or_orphan_never_confirms(self):
  self.turn('other','n'); self.message('other','n'); self.message('t','f'); self.message('t','orphan'); self.turn('t','n'); self.message('t','n','agentMessage')
  self.assertIsNone(core.confirmation_turn('t','f'))
 def test_old_turn_does_not_confirm(self):
  self.turn('t','later'); self.message('t','f',text=core.resume_marker({'thread_id':'t','turn_id':'later'}))
  self.assertIsNone(core.confirmation_turn('t','later'))
 def test_uncertain_stays_no_pending(self):
  self.c.execute("UPDATE resume_events SET state='uncertain'"); self.c.commit(); core.sync_event_states(self.c)
  self.assertEqual(self.state(),'uncertain')
 def test_disabled_history_readonly_sanitized_bounded(self):
  for i in range(40): self.c.execute('INSERT INTO resume_events VALUES(?,?,?,?,?,1,0,?,?)',('t'+str(i),'f','Bearer secret\nname','','observed',i,'token=supersecret'))
  self.c.commit(); before=Path(core.STATE_DB).read_bytes()
  service=WatchdogService(core=core); service._read_history(); snap=service.snapshot()
  self.assertEqual(snap['observed_count'],40); self.assertEqual(len(snap['recent_events']),30); self.assertEqual(before,Path(core.STATE_DB).read_bytes())
  self.assertNotIn('supersecret',str(snap['recent_events'])); self.assertNotIn('Bearer secret',str(snap['recent_events']))
 def test_disabled_missing_history_does_not_create(self):
  self.c.close(); os.remove(core.STATE_DB); service=WatchdogService(core=core); service._read_history(); self.assertFalse(os.path.exists(core.STATE_DB))

 def test_unavailable_history_keeps_queued_fail_closed(self):
  self.h.execute('DROP TABLE thread_items'); self.h.commit(); core.sync_event_states(self.c)
  self.assertEqual(self.state(),'queued')
 def test_legacy_observed_preserved(self):
  self.c.execute("UPDATE resume_events SET state='observed'"); self.c.commit()
  self.turn('t','n'); self.message('t','n'); core.sync_event_states(self.c); self.assertEqual(self.state(),'observed')
 def test_wrong_marker_and_metadata_not_confirmation(self):
  self.turn('t','n'); self.message('t','n',text=core.resume_marker({'thread_id':'t','turn_id':'other'}))
  self.h.execute('INSERT INTO thread_items VALUES(?,?,?,?)',('t','n','userMessage',json.dumps({'type':'userMessage','content':[], 'metadata':core.resume_marker({'thread_id':'t','turn_id':'f'})}))); self.h.commit()
  self.assertIsNone(core.confirmation_turn('t','f'))
 def test_disabled_cycle_does_not_discover_sync_or_send(self):
  service=WatchdogService(core=core)
  with patch.object(core,'discover_events',side_effect=AssertionError('discovery forbidden')), patch.object(core,'sync_event_states',side_effect=AssertionError('sync forbidden')), patch.object(core,'queue_resume',side_effect=AssertionError('send forbidden')), patch.object(core,'quota_from_app_server',return_value={'available':True}):
   service._cycle()
  self.assertEqual(service.snapshot()['queued_count'],1)
 def test_summary_redacts_json_credentials(self):
  self.c.execute('UPDATE resume_events SET last_error=?', ('{"access_token": "secret-value", "authorization":"Bearer xyz"}',)); self.c.commit()
  service=WatchdogService(core=core); service._read_history()
  self.assertNotIn('secret-value', str(service.snapshot()['recent_events']))

 def test_delayed_marker_upgrades_without_resend(self):
  from types import SimpleNamespace
  for initial in ('queued','uncertain'):
   self.c.execute('UPDATE resume_events SET state=?,attempts=1',(initial,)); self.c.commit()
   if not self.h.execute("SELECT 1 FROM thread_turns WHERE turn_id='n'").fetchone(): self.turn('t','n')
   self.h.execute('DELETE FROM thread_items'); self.h.commit()
   core.sync_event_states(self.c); self.assertEqual(self.state(),'superseded')
   self.assertEqual(core.pending_events(self.c,SimpleNamespace(thread=None,cwd=None)),[])
   self.message('t','n')
   with patch.object(core,'queue_resume',side_effect=AssertionError('no resend')):
    core.sync_event_states(self.c)
   self.assertEqual(self.state(),'confirmed')
   self.assertEqual(core.pending_events(self.c,SimpleNamespace(thread=None,cwd=None)),[])
 def test_manual_superseded_without_attempt_not_upgraded(self):
  self.c.execute("UPDATE resume_events SET state='superseded',attempts=0"); self.c.commit()
  self.turn('t','n'); self.message('t','n',text='manual continuation'); core.sync_event_states(self.c)
  self.assertEqual(self.state(),'superseded')
 def test_crash_before_attempt_increment_late_marker_confirms(self):
  from types import SimpleNamespace
  self.c.execute("UPDATE resume_events SET state='sending',attempts=0"); self.c.commit()
  self.turn('t','n'); core.sync_event_states(self.c)
  self.assertEqual(self.state(),'superseded')
  self.assertEqual(core.pending_events(self.c,SimpleNamespace(thread=None,cwd=None)),[])
  self.message('t','n')
  with patch.object(core,'queue_resume',side_effect=AssertionError('no resend')):
   core.sync_event_states(self.c)
  self.assertEqual(self.state(),'confirmed')
  self.assertEqual(core.pending_events(self.c,SimpleNamespace(thread=None,cwd=None)),[])
 def test_app_server_preserves_actual_window_minutes(self):
  import io
  from types import SimpleNamespace
  response={'id':2,'result':{'ordinaryUsageAllowed':True,'rateLimits':{
   'primary':{'usedPercent':7,'windowDurationMins':180},
   'secondary':{'usedPercent':13,'windowDurationMins':20160}}}}
  process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(json.dumps({'id':1})+'\n'+json.dumps(response)+'\n'),terminate=lambda:None,wait=lambda timeout:None,poll=lambda:None)
  with patch.object(core,'codex_command',return_value=['unused']),patch.object(core.subprocess,'Popen',return_value=process):
   result=core.quota_from_app_server(timeout=1)
  self.assertEqual(result['window_minutes'],180)
  self.assertEqual(result['secondary_window_minutes'],20160)

if __name__=='__main__': unittest.main()
