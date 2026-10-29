import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'examples/model-services'))
from systemone_services import __main__ as service
from systemone_services.adapters import wire_question
from systemone_services.engine import NativeEngine


class FakeEngine:
    def __init__(self):
        self.requests = []

    def predict(self, state, questions):
        self.requests.append((state, questions))
        answers = {}
        for qid, q in questions.items():
            if q['type'] == 'noul':
                answers[qid] = {'type': 'noul', 'noul': 0.7}
            else:
                keys = list(q['criteria']) if q['type'] == 'choice' else [str(i) for i in range(len(q['criteria']))]
                answers[qid] = {'type': q['type'], 'probabilities': {k: 1/len(keys) for k in keys}}
        return {'answers': answers, 'raw_response': {'answers': answers}}


class Running:
    def __init__(self, config=None, engine=None, token_env=None):
        self.engine = engine or FakeEngine()
        self.config = config or {'adapter': 'clef', 'routing_model': 'test', 'model_id': None}
        self.token_env = token_env

    def __enter__(self):
        self.server = service.make_server(self.config, port=0, engine=self.engine, token_env=self.token_env)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        return self

    def __exit__(self, *args):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


def post(url, body, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with urllib.request.urlopen(urllib.request.Request(url+'/v1/systemone', data=json.dumps(body).encode(), headers=headers), timeout=3) as response:
        return json.load(response)


class ServiceTests(unittest.TestCase):
    def request(self):
        return {'model': 'test', 'state': {'input': 'value'}, 'questions': {
            'yes': {'type': 'noul', 'instructions': 'yes?'},
            'pick': {'type': 'choice', 'instructions': 'pick', 'criteria': {'b': 'B', 'a': 'A'}},
            'level': {'type': 'score', 'instructions': 'level', 'criteria': ['low', 'high']},
        }}

    def test_three_primitives_and_service_identity(self):
        with Running() as server:
            body = self.request(); response = post(server.url, body)
            self.assertEqual(response['model'], 'test')
            self.assertEqual(set(response['answers']), set(body['questions']))
            self.assertEqual(server.engine.requests, [(body['state'], body['questions'])])
            self.assertEqual(list(response['answers']['pick']['probabilities']), ['b', 'a'])
            self.assertIn('service_profile_sha256', response['metadata'])
            self.assertIn('server_revision', response['metadata'])
            with urllib.request.urlopen(server.url+'/health') as r:
                self.assertEqual(json.load(r)['status'], 'ready')

    def test_rejects_labels_unknown_model_and_missing_questions(self):
        with Running() as server:
            cases = [(dict(self.request(), labels={'yes': True}), 422),
                     (dict(self.request(), model='other'), 404),
                     (dict(self.request(), questions={}), 422)]
            for body, code in cases:
                with self.assertRaises(urllib.error.HTTPError) as context:
                    post(server.url, body)
                self.assertEqual(context.exception.code, code)
            self.assertFalse(server.engine.requests)

    def test_frontend_authentication(self):
        with patch.dict(os.environ, {'SERVICE_TEST_KEY': 'secret-test'}):
            with Running(token_env='SERVICE_TEST_KEY') as server:
                with self.assertRaises(urllib.error.HTTPError) as context:
                    post(server.url, self.request())
                self.assertEqual(context.exception.code, 401)
                self.assertEqual(post(server.url, self.request(), 'secret-test')['model'], 'test')

    def test_model_side_historical_rendering(self):
        q = {'type': 'choice', 'instructions': {'task': 'choose'},
             'criteria': {'a': 'A', 'b': 'B'}, 'option_order': ['b', 'a']}
        rendered = wire_question(q, 'oneshot_hard')
        self.assertEqual(rendered['instructions'], '{"task":"choose"}')
        self.assertEqual(list(rendered['criteria']), ['b', 'a'])
        self.assertEqual(q['instructions'], {'task': 'choose'})
        self.assertNotIn('criteria', wire_question({'type': 'noul', 'instructions': 'yes?', 'criteria': {}}, 'oneshot'))

    def test_gateway_preserves_upstream_bytes_identity_and_retry_status(self):
        requests=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers['Content-Length'])))
                code=429 if len(requests)==1 else 200
                response={'error':'rate limited'} if code==429 else {'model':'remote-rev','provider':'Azure',
                    'answers':{'yes':{'type':'noul','noul':0.7}},'usage':{'input_tokens':12},
                    'metadata':{'checkpoint_revision':'upstream-rev'}}
                self.send_response(code);self.end_headers();self.wfile.write(json.dumps(response).encode())
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        config={'name':'test','routing_model':'test','adapter':'http_decisions','model_id':'remote',
            'endpoint':'http://127.0.0.1:'+str(upstream.server_port)+'/native',
            'timeout_seconds':3,'transport_retries':0,'compact_json':True,'expected_returned_models':['remote-rev']}
        try:
            with Running(config,service.HTTPModel(config)) as gateway:
                request={'model':'test','state':{'input':'value'},'questions':{'yes':{'type':'noul','instructions':'yes?'}}}
                with self.assertRaises(urllib.error.HTTPError) as context:post(gateway.url,request)
                self.assertEqual(context.exception.code,429)
                self.assertEqual(len(requests),1)  # The gateway did not retry.
                result=post(gateway.url,request)
                self.assertEqual(result['model'],'remote-rev')
                self.assertEqual(result['provider'],'Azure')
                self.assertEqual(result['usage']['input_tokens'],12)
                self.assertEqual(result['upstream_metadata']['checkpoint_revision'],'upstream-rev')
                self.assertEqual(json.loads(requests[1])['model'],'remote')
                self.assertEqual(requests[0],requests[1])
        finally:
            upstream.shutdown();upstream.server_close();thread.join()

    def test_native_callable_contract_and_cleanup_without_benchmark_import(self):
        from types import SimpleNamespace
        fake=FakeEngine()
        fake.torch=SimpleNamespace(version=SimpleNamespace(cuda=None),cuda=SimpleNamespace(is_available=lambda:False))
        config={'adapter':'d3','routing_model':'test','model_id':None}
        with patch('systemone_services.engine.profile_for',return_value=config), \
             patch('systemone_services.engine.validate_profile',return_value=[]), \
             patch('systemone_services.engine.NativeModel',return_value=fake):
            engine=NativeEngine('model-owned.json','test')
            request=self.request();response,raw=engine(request['state'],request['questions'])
            self.assertEqual(response['model'],'test')
            self.assertEqual(set(response['answers']),set(request['questions']))
            self.assertIn('raw_response',raw)
            self.assertEqual(engine.runtime()['device'],'cpu')
            self.assertEqual(len(engine.provenance['profile_sha256']),64)
            self.assertEqual(len(engine.provenance['adapter_source_sha256']),64)
            engine.close();engine.close()
            with self.assertRaisesRegex(RuntimeError,'closed'):engine({}, {})

    def test_native_callable_rejects_upstream_http_profiles(self):
        with patch('systemone_services.engine.profile_for',return_value={'adapter':'http_decisions'}):
            with self.assertRaisesRegex(ValueError,'native model profile'):
                NativeEngine('model-owned.json','hosted')

    def test_profile_resolves_split_and_disables_upstream_retries(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'profiles.json'
            p.write_text(json.dumps({'models': {'x': {'adapter': 'systemone_http', 'endpoint': '${TEST_ENDPOINT}',
                'transport_retries': 3, 'splits': {'soft': {'endpoint': 'http://localhost:2/v1/systemone'}}}}}))
            with patch.dict(os.environ, {'TEST_ENDPOINT': 'http://localhost:1/v1/systemone'}):
                hard = service.profile_for(p, 'x', 'hard'); soft = service.profile_for(p, 'x', 'soft')
            self.assertEqual(hard['transport_retries'], 0)
            self.assertEqual(soft['transport_retries'], 0)
            self.assertNotEqual(hard['endpoint'], soft['endpoint'])
            self.assertEqual(hard['routing_model'], 'x')


if __name__ == '__main__':
    unittest.main()
