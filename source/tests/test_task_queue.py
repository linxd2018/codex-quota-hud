import concurrent.futures
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

try:
    from task_queue import TaskStore, normalize_concurrency, validate_concurrency
except ImportError:
    TaskStore = None


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TaskStore, 'durable task queue has not been implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'watchdog.sqlite')
        self.store = TaskStore(self.path)
        self.spec = dict(description='检查项目\n并给出结果', cwd=self.tmp.name)

    def test_submit_is_durable_without_quota_or_model(self):
        task = self.store.submit(self.spec)
        restored = TaskStore(self.path).get(task['id'])
        self.assertEqual(restored['description'], self.spec['description'])
        self.assertEqual(restored['status'], 'pending')
        self.assertEqual(restored['sandbox'], 'workspace-write')
        self.assertEqual(restored['approval_policy'], 'on-request')
        self.assertIsNone(restored['thread_id'])
        self.assertIsNone(restored['started_at'])

    def test_constructor_and_empty_read_do_not_create_database(self):
        self.assertEqual(self.store.list_tasks(), [])
        self.assertFalse(Path(self.path).exists())

    def test_adopt_recovery_is_atomic_and_never_requeues_uncertain_delivery(self):
        import codex_watchdog as core
        with patch.object(core, 'STATE_DB', self.path):
            con = core.state_connection()
        item = dict(thread_id='existing-thread', turn_id='failed-turn', title='Original task', cwd=self.tmp.name)
        con.execute("INSERT INTO resume_events(thread_id,failed_turn_id,title,cwd,state,created_at,updated_at) VALUES(?,?,?,?, 'pending',1,1)",
                    (item['thread_id'], item['turn_id'], item['title'], item['cwd']))
        con.commit()
        try:
            task = self.store.adopt_recovery(item)
            self.assertEqual(task['thread_id'], item['thread_id'])
            self.assertEqual(task['turn_id'], item['turn_id'])
            self.assertTrue(task['external_recovery'])
            self.assertEqual(task['status'], 'waiting_resume')
            self.assertEqual(con.execute('SELECT state FROM resume_events').fetchone()[0], 'queue_owned')
            self.assertIsNone(self.store.adopt_recovery(item))
            self.assertEqual(len(self.store.list_tasks()), 1)
            con.execute("UPDATE resume_events SET state='uncertain'")
            con.commit()
            self.assertIsNone(self.store.adopt_recovery(item))
            self.assertEqual(self.store.get(task['id'])['status'], 'waiting_resume')
        finally:
            con.close()

    def test_validation_preserves_previous_tasks(self):
        first = self.store.submit(self.spec)
        for patch in ({'description':' \n'}, {'cwd':self.tmp.name+'/missing'},
                      {'sandbox':'unsafe'}, {'approval_policy':'on-failure'},
                      {'description':42}, {'priority':True}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.store.submit(dict(self.spec, **patch))
        self.assertEqual([t['id'] for t in self.store.list_tasks()], [first['id']])

    def test_priority_then_fifo_and_atomic_claim(self):
        first = self.store.submit(self.spec)
        second = self.store.submit(dict(self.spec, description='second'))
        self.store.reprioritize(second['id'], 5)
        self.assertEqual(self.store.claim_next()['id'], second['id'])
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda _: TaskStore(self.path).claim_next(), range(2)))
        self.assertEqual([t['id'] for t in claims if t], [first['id']])
        self.assertEqual(self.store.get(first['id'])['attempts'], 1)

    def test_cas_does_not_overwrite_new_worker_state(self):
        task = self.store.submit(self.spec)
        self.store.claim_next()
        updated = self.store.update(task['id'], {'status':'running','thread_id':'real-id'}, expected_status='starting')
        self.assertEqual(updated['thread_id'], 'real-id')
        self.assertIsNone(self.store.update(task['id'], {'status':'failed'}, expected_status='starting'))
        self.assertEqual(self.store.get(task['id'])['status'], 'running')

    def test_edit_delete_restricted_to_never_started(self):
        task = self.store.submit(self.spec)
        self.store.edit(task['id'], {'description':'edited'})
        self.assertEqual(self.store.get(task['id'])['description'], 'edited')
        with self.assertRaises(ValueError):
            self.store.edit(task['id'], {'thread_id':'forged'})
        self.store.claim_next()
        for fn in (lambda:self.store.edit(task['id'], {'description':'new'}),
                   lambda:self.store.delete(task['id'])):
            with self.assertRaises(ValueError): fn()

    def test_delete_keeps_cancelled_audit_record(self):
        task = self.store.submit(self.spec)
        self.store.delete(task['id'])
        self.assertEqual(self.store.get(task['id'])['status'], 'cancelled')
        self.assertIsNone(self.store.claim_next())

    def test_manual_retry_same_uuid_and_ambiguous_never_retries(self):
        task = self.store.submit(self.spec)
        self.store.claim_next()
        self.store.update(task['id'], {'status':'failed','thread_id':'real-id','error_kind':'network','phase':'finished'})
        self.assertEqual(self.store.retry(task['id'])['status'], 'waiting_resume')
        self.assertEqual(self.store.get(task['id'])['thread_id'], 'real-id')
        self.store.update(task['id'], {'status':'failed','error_kind':'uncertain','phase':'turn_sending'})
        with self.assertRaises(ValueError): self.store.retry(task['id'])

    def test_retry_backoff_not_claimed_and_bad_directory_rechecked(self):
        task = self.store.submit(self.spec)
        self.store.update(task['id'], {'retry_at':1e15})
        self.assertIsNone(self.store.claim_next())
        folder = Path(self.tmp.name)/'work'
        folder.mkdir()
        other = self.store.submit(dict(self.spec,cwd=str(folder)))
        self.store.update(other['id'], {'status':'failed','error_kind':'config'})
        folder.rmdir()
        with self.assertRaises(ValueError): self.store.retry(other['id'])

    def test_rollback_and_corruption_never_silently_reset(self):
        task = self.store.submit(self.spec)
        con = sqlite3.connect(self.path)
        con.execute("UPDATE new_tasks SET status='cancelled' WHERE id=?", (task['id'],))
        con.close()  # uncommitted abrupt exit
        self.assertEqual(self.store.get(task['id'])['status'], 'pending')
        corrupt = Path(self.tmp.name)/'corrupt.sqlite'
        corrupt.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError): TaskStore(str(corrupt)).submit(self.spec)
        self.assertEqual(corrupt.read_bytes(), b'not a sqlite database')

    def test_concurrency_strict_input_and_legacy_default(self):
        for value in (3,4,10): self.assertEqual(validate_concurrency(value),value)
        for value in (None,True,2,11,3.0,'3','bad',float('nan')):
            with self.subTest(value=value):
                self.assertEqual(normalize_concurrency(value),3)
                with self.assertRaises(ValueError): validate_concurrency(value)

    def test_approval_applies_only_to_the_displayed_request(self):
        task_id = self.store.submit(self.spec)['id']
        shown = {'id': 7, 'pending_since': 100, 'method': 'item/commandExecution/requestApproval'}
        current = dict(shown, pending_since=200)
        self.store.update(task_id, {'status': 'waiting_authorization', 'approval': current})
        with self.assertRaises(ValueError):
            self.store.respond_approval(task_id, shown, 'accept')
        self.assertIsNone(self.store.get(task_id)['approval_response'])
        self.store.respond_approval(task_id, current, 'decline')
        self.assertEqual(self.store.get(task_id)['approval_response'], {'decision': 'decline'})
        with self.assertRaises(ValueError):
            self.store.respond_approval(task_id, current, 'accept')

    def test_restart_after_claim_keeps_intent_and_cannot_reclaim(self):
        task = self.store.submit(self.spec)
        claimed = self.store.claim_next()
        restored = TaskStore(self.path)
        self.assertEqual(restored.get(task['id'])['worker_token'], claimed['worker_token'])
        self.assertEqual(restored.get(task['id'])['status'], 'starting')
        self.assertIsNone(restored.claim_next())

    def test_partial_enqueue_failure_rolls_back_entire_record(self):
        first = self.store.submit(self.spec)
        with patch.object(self.store, '_event', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.store.submit(self.spec)
        self.assertEqual([t['id'] for t in self.store.list_tasks()], [first['id']])

    def test_invalid_state_and_duplicate_session_are_atomic(self):
        first, second = self.store.submit(self.spec), self.store.submit(self.spec)
        self.store.update(first['id'], {'thread_id':'real-session'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update(second['id'], {'thread_id':'real-session','status':'running'})
        self.assertIsNone(self.store.get(second['id'])['thread_id'])
        with self.assertRaises(ValueError):
            self.store.update(first['id'], {'status':'invalid'})
        self.assertEqual(self.store.get(first['id'])['status'], 'pending')


if __name__ == '__main__': unittest.main()
