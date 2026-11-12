import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import codex_watchdog as core
from codex_app_server import RpcError, TransportError, list_models, safe_log_payload
from task_queue import TaskStore
from task_worker import TaskWorker, TaskLease, approval_result, error_kind, recover_receipts, validate_task, worker_is_alive


MODEL = {'model': 'test-model', 'id': 'test-model', 'isDefault': True,
         'defaultReasoningEffort': 'low',
         'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}


class FakeServer:
    def __init__(self, store, task_id, events=None, fail=None):
        self.store, self.task_id = store, task_id
        self.calls, self.replies = [], []
        self.events = list(events or [])
        self.fail = fail or {}
        self.closed = False
        self.observed = []

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        self.observed.append((method, self.store.get(self.task_id)))
        if method in self.fail:
            raise self.fail[method]
        if method == 'model/list': return {'data': [MODEL]}
        if method == 'config/read': return {'config': {'model': 'test-model','model_reasoning_effort':'low'}}
        if method == 'thread/start': return {'thread': {'id': 'real-thread'}}
        if method == 'thread/resume': return {'thread': {'id': params['threadId'], 'status': {'type': 'idle'}}}
        if method == 'thread/read': return {'thread': {'id': params['threadId'], 'turns': []}}
        if method == 'thread/archive': return {}
        if method == 'turn/start': return {'turn': {'id': 'real-turn', 'status': 'inProgress'}}
        if method == 'turn/interrupt':
            self.events.append({'method':'turn/completed', 'params': {'threadId':'real-thread', 'turn': {'id':'real-turn', 'status':'interrupted'}}})
            return {}
        raise AssertionError(method)

    def next_event(self, timeout):
        event = self.events.pop(0)
        if callable(event): return event()
        return event

    def respond(self, request_id, result): self.replies.append((request_id, result))
    def close(self): self.closed = True


def completion(status='completed', error=None):
    return {'method': 'turn/completed', 'params': {'threadId': 'real-thread', 'turn': {
        'id': 'real-turn', 'status': status, 'error': error}}}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(Path(self.temp.name) / 'queue.sqlite')
        self.task = self.store.submit({'description': 'Do the requested work', 'cwd': self.temp.name,
                                      'model': 'test-model', 'reasoning_effort': 'low'})
        self.task = self.store.claim_next()

    def run_worker(self, events=None, fail=None, now=None):
        server = FakeServer(self.store, self.task['id'], events or [completion()], fail)
        worker = TaskWorker(self.store, self.task['id'], core,
                            server_factory=lambda *args: server,
                            permission_check=lambda *args,**kw:(True,''), **({'now': now} if now else {}))
        worker.run()
        return self.store.get(self.task['id']), server

    def test_uuid_is_committed_before_turn_and_ends_completed(self):
        task, server = self.run_worker()
        record = next(t for m,t in server.observed if m == 'turn/start')
        self.assertEqual(record['thread_id'], 'real-thread')
        self.assertEqual(record['phase'], 'turn_start_sent')
        self.assertEqual(task['status'], 'completed')
        self.assertIsNotNone(task['completed_at'])
        self.assertIsNone(task['worker_pid'])
        self.assertTrue(server.closed)

    def test_start_timeout_is_uncertain_no_second_start(self):
        task, server = self.run_worker(fail={'thread/start': TransportError('lost response')})
        self.assertEqual((task['status'],task['error_kind']), ('failed','uncertain'))
        self.assertIsNone(task['thread_id'])
        self.assertEqual([m for m,p in server.calls].count('thread/start'), 1)
        with self.assertRaises(ValueError): self.store.retry(task['id'])

    def test_turn_timeout_keeps_uuid_and_blocks_retry(self):
        task, server = self.run_worker(fail={'turn/start': TransportError('lost response')})
        self.assertEqual(task['thread_id'], 'real-thread')
        self.assertEqual(task['error_kind'], 'uncertain')
        with self.assertRaises(ValueError): self.store.retry(task['id'])

    def test_quota_rpc_failure_waits_without_loop(self):
        task, server = self.run_worker(fail={'turn/start': RpcError({'type':'usageLimitExceeded'})})
        self.assertEqual(task['status'], 'waiting_quota')
        self.assertIsNone(task['thread_id'])
        self.assertEqual(task['discarded_thread_id'], 'real-thread')
        self.assertGreater(task['retry_at'], 0)

    def test_quota_completion_waits(self):
        task, server = self.run_worker([completion('failed', {'codexErrorInfo':'usageLimitExceeded'})])
        self.assertEqual(task['status'], 'waiting_quota')

    def test_resume_reuses_uuid_and_original_scope(self):
        self.store.update(self.task['id'], {'thread_id':'real-thread', 'turn_id':'failed-turn', 'phase':'resume_claimed'})
        task, server = self.run_worker()
        self.assertNotIn('thread/start', [m for m,p in server.calls])
        resume = next(p for m,p in server.calls if m == 'thread/resume')
        self.assertEqual(resume['sandbox'], 'workspace-write')
        self.assertEqual(resume['approvalPolicy'], 'on-request')
        turn = next(p for m,p in server.calls if m == 'turn/start')
        self.assertIn(core.resume_marker({'thread_id':'real-thread','turn_id':'failed-turn'}), turn['input'][0]['text'])

    def test_user_cancelled_turn_does_not_auto_resume(self):
        task, server = self.run_worker([completion('interrupted')])
        self.assertEqual(task['status'], 'cancelled')

    def test_invalid_effort_stops_before_creation(self):
        self.store.update(self.task['id'], {'reasoning_effort':'impossible'})
        task, server = self.run_worker()
        self.assertEqual(task['status'], 'failed')
        self.assertNotIn('thread/start', [m for m,p in server.calls])

    def test_deleted_cwd_stops_before_protocol(self):
        self.store.update(self.task['id'], {'cwd':str(Path(self.temp.name)/'missing')})
        task, server = self.run_worker()
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(server.calls, [])

    def test_explicit_approval_is_answered(self):
        request = {'id':99, 'method':'item/commandExecution/requestApproval', 'params':{'threadId':'real-thread','command':'echo test'}}
        def approve():
            self.assertEqual(self.store.get(self.task['id'])['status'],'waiting_authorization')
            self.store.update(self.task['id'], {'approval_response':{'decision':'accept'}})
            return False
        task, server = self.run_worker([request, approve, completion()])
        self.assertEqual(server.replies, [(99, {'decision':'accept'})])
        self.assertEqual(task['status'], 'completed')

    def test_approval_timeout_waits_for_interruption_then_frees_slot(self):
        request = {'id':99, 'method':'item/fileChange/requestApproval', 'params':{'threadId':'real-thread'}}
        clock = [0]
        def expire():
            clock[0] = 1000
            return False
        task, server = self.run_worker([request, expire], now=lambda:clock[0])
        self.assertEqual(task['status'], 'waiting_authorization')
        self.assertEqual(task['phase'], 'approval_timeout')
        self.assertIsNone(task['worker_pid'])
        self.assertEqual(server.replies, [])

    def test_lease_survives_pid_reuse_and_detects_release(self):
        path = str(Path(self.temp.name)/'task.log')
        lease = TaskLease(path+'.lease','token')
        self.assertTrue(lease.acquire())
        try:
            self.assertTrue(worker_is_alive({'log_path':path,'worker_pid':999999}))
            second = TaskLease(path+'.lease','other')
            self.assertFalse(second.acquire())
        finally: lease.close()
        self.assertFalse(worker_is_alive({'log_path':path,'worker_pid':os.getpid()}))

    def test_no_automatic_accept_for_input_requests(self):
        with self.assertRaises(ValueError):
            approval_result({'method':'item/tool/requestUserInput'}, {'decision':'accept'})

    def test_error_classification(self):
        self.assertEqual(error_kind('usageLimitExceeded'), 'quota')
        self.assertEqual(error_kind('streamConnectionFailed'), 'network')
        self.assertEqual(error_kind('model invalid'), 'config')
        self.assertEqual(error_kind('Cannot open C:/CodexQuotaHUD/path'), 'config')

    def test_gate_revoked_before_create_preserves_pending(self):
        server = FakeServer(self.store, self.task['id'])
        worker = TaskWorker(self.store,self.task['id'],core,server_factory=lambda *a:server,
                            permission_check=lambda *a,**k:(False,'自动接续已关闭'))
        worker.run()
        task = self.store.get(self.task['id'])
        self.assertEqual(task['status'],'pending')
        self.assertNotIn('thread/start',[m for m,p in server.calls])

    def test_gate_revoked_after_create_archives_empty_thread_and_can_restart(self):
        server = FakeServer(self.store, self.task['id'])
        worker = TaskWorker(self.store,self.task['id'],core,server_factory=lambda *a:server,
                            permission_check=lambda *a,**k:(k['stage']=='thread','上限已调整'))
        worker.run()
        task = self.store.get(self.task['id'])
        self.assertEqual((task['status'],task['thread_id']),('pending',None))
        self.assertEqual(task['discarded_thread_id'], 'real-thread')
        self.assertIn(('thread/archive', {'threadId':'real-thread'}), server.calls)
        self.assertNotIn('turn/start',[m for m,p in server.calls])
        self.store.update(task['id'], {'retry_at':0})
        self.assertEqual(self.store.claim_next()['id'], task['id'])
        result, second = self.run_worker()
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('thread/resume', [method for method, _ in second.calls])

    def test_default_model_uses_config_not_catalog_default(self):
        self.store.update(self.task['id'],{'model':'','reasoning_effort':'low'})
        server=FakeServer(self.store,self.task['id'],[completion()])
        original=server.request
        def request(method,params):
            if method=='config/read':return {'config':{'model':'configured-model'}}
            if method=='model/list':return {'data':[dict(MODEL,supportedReasoningEfforts=[]),dict(MODEL,model='configured-model',id='configured-model',isDefault=False)]}
            return original(method,params)
        server.request=request
        TaskWorker(self.store,self.task['id'],core,server_factory=lambda *a:server,
                   permission_check=lambda *a,**k:(True,'')).run()
        self.assertEqual(self.store.get(self.task['id'])['effective_model'],'configured-model')
        self.assertEqual(self.store.get(self.task['id'])['status'],'completed')

    def test_model_pagination(self):
        class Pages:
            def request(self,method,params):
                return {'data':[{'id':'second'}]} if params.get('cursor') else {'data':[{'id':'first'}],'nextCursor':'next'}
        self.assertEqual([m['id'] for m in list_models(Pages())],['first','second'])

    def test_logs_omit_prompt_and_redact_secrets(self):
        value=safe_log_payload({'input':[{'text':'private prompt'}],'api_key':'abc','error':'token=secret Bearer sensitive'})
        self.assertNotIn('private prompt',str(value))
        self.assertNotIn('abc',str(value))
        self.assertNotIn('sensitive',str(value))

    def test_receipts_recover_only_correlated_real_ids(self):
        tid=str(uuid.uuid4()); log_path=str(Path(self.temp.name)/'task.log')
        task=dict(self.task,log_path=log_path)
        records=[
            {'direction':'sending','payload':{'id':1,'method':'thread/start','params':{'threadSource':'codex-quota-hud:'+task['id'],'serviceName':'codex-quota-hud'}}},
            {'direction':'received','payload':{'id':1,'result':{'thread':{'id':tid}}}},
            {'direction':'sending','payload':{'id':2,'method':'turn/start','params':{'threadId':tid,'clientUserMessageId':task['id']+':1'}}},
            {'direction':'received','payload':{'id':2,'result':{'turn':{'id':'actual-turn'}}}}]
        with open(log_path+'.receipts','w',encoding='utf-8') as f:
            for record in records:f.write(json.dumps(record)+'\n')
            f.write('{partial')
        self.assertEqual(recover_receipts(task),{'thread_id':tid,'turn_id':'actual-turn','phase':'turn_started'})
        wrong=dict(task,id='other-task')
        self.assertEqual(recover_receipts(wrong),{})

    def test_external_user_cancel_interrupts_owned_turn(self):
        with patch.object(core,'latest_turn',return_value={'turn_id':'real-turn','status':'interrupted'}):
            task,server=self.run_worker([completion('interrupted')])
        self.assertEqual(task['status'],'cancelled')
        self.assertIn('turn/interrupt',[m for m,p in server.calls])

    def test_external_new_turn_transfers_tracking_without_new_start(self):
        with patch.object(core,'latest_turn',return_value={'turn_id':'new-user-turn','status':'inProgress'}):
            task,server=self.run_worker([completion('interrupted')])
        self.assertEqual((task['turn_id'],task['status'],task['phase']),('new-user-turn','running','external_turn'))
        self.assertIsNone(task['worker_pid'])
        self.assertEqual([m for m,p in server.calls].count('turn/start'),1)


if __name__ == '__main__': unittest.main()
