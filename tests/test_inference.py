import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from evaluation import inference as inf
from evaluation import __main__ as cli

class Server:
    def __init__(self, status=200, model='test-model'):
        self.requests=[];self.status=status;self.model=model;self.metadata={"checkpoint_revision":"rev-a","backend":"test"}
    def __enter__(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                obj=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(obj)
                if owner.status!=200:
                    self.send_response(owner.status);self.end_headers();self.wfile.write(b'rejected secret-for-test');return
                answers={}
                for qid,q in obj['questions'].items():
                    if q['type']=='noul':a={'type':'noul','noul':0.6}
                    else:
                        keys=list(q['criteria']) if q['type']=='choice' else [str(i) for i in range(len(q['criteria']))]
                        a={'type':q['type'],'probabilities':{k:1/len(keys) for k in keys}}
                    answers[qid]=a
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'model':owner.model,'answers':answers,'metadata':owner.metadata}).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.endpoint='http://127.0.0.1:'+str(self.server.server_port)+'/v1/systemone'
        return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join()

class InferenceTests(unittest.TestCase):
    def config(self,endpoint):return {'name':'test','adapter':'systemone_http','model_id':'test-model','endpoint':endpoint,'expected_returned_models':['test-model'],'concurrency':1,'timeout_seconds':3,'transport_retries':1,'retry_delay_seconds':0}

    def test_actual_http_export_and_resume(self):
        with Server() as server,tempfile.TemporaryDirectory() as d:
            root=Path(d);config=root/'models.json';config.write_text(json.dumps({'models':{'test':self.config(server.endpoint)}}))
            out=root/'run';result=inf.predict(config,'test',out,limit=2)
            self.assertTrue(result['fresh_inference'])
            self.assertEqual(len(cli.read(out/'predictions-hard.jsonl')),2)
            self.assertEqual(len(cli.read(out/'predictions-soft.jsonl')),2)
            for request in server.requests:
                self.assertEqual(list(request),['model','state','questions'])
            count=len(server.requests);inf.predict(config,'test',out,limit=2,resume=True)
            self.assertEqual(count,len(server.requests))
            with self.assertRaises(FileExistsError):inf.predict(config,'test',out,limit=2)
            with self.assertRaises(ValueError):inf.predict(config,'test',out,limit=3,resume=True)
            self.assertFalse(result['metrics']['soft']['soft_rank_eligible'])

    def test_api_auth_error_stops_without_retry_and_redacts(self):
        with Server(status=402) as server,patch.dict('os.environ',{'TEST_TOKEN':'secret-for-test'}):
            c=self.config(server.endpoint);c['key_env']='TEST_TOKEN'
            with self.assertRaises(inf.InferenceFailure) as context:
                inf.HTTPModel(c).predict({}, {'q':{'type':'noul','instructions':'yes?'}})
            self.assertTrue(context.exception.critical)
            self.assertEqual(len(server.requests),1)
            self.assertNotIn('secret-for-test',str(context.exception.attempts))

    def test_model_version_change_fails_closed(self):
        with Server(model='different') as server:
            with self.assertRaises(inf.InferenceFailure) as context:
                inf.HTTPModel(self.config(server.endpoint)).predict({}, {'q':{'type':'noul','instructions':'yes?'}})
            self.assertTrue(context.exception.critical)
            self.assertEqual(len(server.requests),1)

    def test_no_payload_override_or_candidate_reordering(self):
        q={'type':'choice','instructions':{'task':'choose'},'criteria':{'a':'A','b':'B'},'option_order':['b','a']}
        before=copy.deepcopy(q)
        self.assertEqual(list(inf.wire_question(q)['criteria']),['b','a'])
        self.assertEqual(q,before)
        self.assertEqual(inf.wire_question(q)['instructions'], {'task':'choose'})
        with Server() as server:
            c=self.config(server.endpoint);c['controls']={'labels':{'q':True}}
            with self.assertRaises(ValueError):inf.HTTPModel(c).predict({}, {'q':q})
            self.assertEqual(len(server.requests),0)

    def test_service_revision_pin_and_drift(self):
        with Server() as server:
            c=self.config(server.endpoint)
            c['expected_metadata']={'checkpoint_revision':'rev-a'}
            engine=inf.HTTPModel(c)
            question={'q':{'type':'noul','instructions':'yes?'}}
            engine.predict({},question)
            server.metadata['checkpoint_revision']='rev-b'
            with self.assertRaises(inf.InferenceFailure) as context:
                engine.predict({},question)
            self.assertTrue(context.exception.critical)
            self.assertEqual(len(server.requests),2)

    def test_unpinned_declared_identity_drift_stops(self):
        with Server() as server:
            engine=inf.HTTPModel(self.config(server.endpoint))
            request={'q':{'type':'noul','instructions':'yes?'}}
            engine.predict({},request)
            server.metadata['backend']='different-backend'
            with self.assertRaises(inf.InferenceFailure) as context:
                engine.predict({},request)
            self.assertTrue(context.exception.critical)

    def test_identity_preserved_on_partial_resume(self):
        with Server() as server,tempfile.TemporaryDirectory() as d:
            root=Path(d);config=root/'models.json'
            config.write_text(json.dumps({'models':{'test':self.config(server.endpoint)}}))
            out=root/'run';inf.predict(config,'test',out,limit=2)
            identities=json.loads((out/'service-identities.json').read_text())
            self.assertEqual(identities['hard']['metadata']['checkpoint_revision'],'rev-a')
            # Emulate an interrupted run with one durable Hard row still outstanding.
            path=out/'predictions-hard.jsonl'
            path.write_text(path.read_text().splitlines()[0]+'\n')
            server.metadata['checkpoint_revision']='rev-b'
            with self.assertRaises(inf.InferenceFailure) as context:
                inf.predict(config,'test',out,limit=2,resume=True)
            self.assertTrue(context.exception.critical)
            self.assertEqual(json.loads((out/'status.json').read_text())['status'],'failed')

    def test_only_systemone_transport_and_no_model_runtime_settings(self):
        for updates in [
            {'adapter':'clef'}, {'checkpoint':'/private/weights'},
            {'endpoint':'http://localhost:8000/v1/decisions'},
            {'endpoint':'http://token@localhost:8000/v1/systemone'},
            {'concurrency':0}, {'transport_retries':True}, {'timeout_seconds':float('inf')},
        ]:
            with self.subTest(updates=updates):
                c=self.config('http://localhost:8000/v1/systemone');c.update(updates)
                with self.assertRaises(ValueError):inf.HTTPModel(c)

if __name__=='__main__':unittest.main()
