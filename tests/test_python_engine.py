import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from evaluation import inference as inf
from evaluation import __main__ as cli
from test_inference import Server

PLUGIN = '''
LOADS = 0
CLOSES = 0
CALLS = []
class ModelEngine:
    provenance = {"repo": "model-owned", "revision": "model-rev-a"}
    def __init__(self, model_id="test-model", fail=None):
        global LOADS
        LOADS += 1
        self.model_id = model_id
        self.fail = fail
    def runtime(self):
        return {"backend": "test", "version": "1"}
    def __call__(self, state, questions):
        if self.fail == "oom":
            raise RuntimeError("CUDA out of memory.")
        if self.fail == "regular":
            raise ValueError("model rejected the input")
        if self.fail == "bad-result":
            return {"answers": {}}
        CALLS.append((state, questions))
        answers = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.6}
            else:
                keys = list(q["criteria"]) if q["type"] == "choice" else [str(i) for i in range(len(q["criteria"]))]
                answers[qid] = {"type": q["type"], "probabilities": {k: 1/len(keys) for k in keys}}
        return {"model": self.model_id, "answers": answers}, {"source": "python-model", "question_count": len(questions)}
    def close(self):
        global CLOSES
        CLOSES += 1
'''


class PythonEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.module_name = 'hjb_plugin_' + self.root.name
        self.source = self.root/(self.module_name+'.py')
        self.source.write_text(PLUGIN)
        self.config_path = self.root/'models.json'
        self.config = {'engine': self.module_name+':ModelEngine', 'model_id': 'test-model',
                       'module_paths': [str(self.root)], 'options': {}, 'concurrency': 1,
                       'expected_returned_models': ['test-model']}
        self.write_config()

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        while str(self.root) in sys.path:
            sys.path.remove(str(self.root))
        self.temp.cleanup()

    def write_config(self):
        self.config_path.write_text(json.dumps({'models': {'test': self.config}}))

    def test_doctor_is_lazy_and_validates_without_import_or_model_load(self):
        self.source.write_text('raise AssertionError("plugin must not be imported by doctor")\n')
        result = inf.doctor(self.config_path, 'test')
        self.assertFalse(result['issues'])
        self.assertFalse(result['python_plugin_imported'])
        self.assertNotIn(self.module_name, sys.modules)
        self.assertNotIn(str(self.root), sys.path)

    def test_python_and_http_agree_on_all_questions_scoring_and_inputs(self):
        with Server() as server:
            http_config = {'name':'test','model_id':'test-model','endpoint':server.endpoint,
                           'concurrency':1,'timeout_seconds':3,'transport_retries':0}
            path = self.root/'http.json';path.write_text(json.dumps({'models':{'test':http_config}}))
            inf.predict(self.config_path, 'test', self.root/'python')
            inf.predict(path, 'test', self.root/'http')
        module = sys.modules[self.module_name]
        self.assertEqual(module.LOADS, 1)  # Same configured model is reused across Hard and Soft.
        self.assertEqual(module.CLOSES, 1)
        self.assertNotIn(str(self.root), sys.path)
        for phase in ['hard', 'soft']:
            self.assertEqual(cli.read(self.root/'python'/('predictions-'+phase+'.jsonl')),
                             cli.read(self.root/'http'/('predictions-'+phase+'.jsonl')))
            py_score=json.loads((self.root/'python'/('scores-'+phase+'.json')).read_text())
            http_score=json.loads((self.root/'http'/('scores-'+phase+'.json')).read_text())
            self.assertEqual(py_score, http_score)
        self.assertEqual([(r['state'],r['questions']) for r in server.requests], module.CALLS)
        receipt=cli.read(self.root/'python/receipts.jsonl')[0]
        self.assertEqual(receipt['response']['raw_output']['source'], 'python-model')
        identities=json.loads((self.root/'python/service-identities.json').read_text())
        self.assertIn('python_source_sha256', identities['hard']['metadata'])

    def test_call_contract_preserves_types_order_and_caller_objects(self):
        questions={'n':{'type':'noul','instructions':{'scope':'visible'}},
                   'c':{'type':'choice','instructions':['choose'], 'criteria':{'a':'A','b':'B'}, 'option_order':['b','a']},
                   's':{'type':'score','instructions':'level','criteria':['low','high']}}
        state={'observations':['original']};before=copy.deepcopy((state,questions))
        engine=inf.engine_for(inf.config_for(self.config_path,'test','hard'))
        try:
            response, raw=engine(state,questions)
            self.assertEqual(set(response['answers']), {'n','c','s'})
            self.assertEqual(list(sys.modules[self.module_name].CALLS[-1][1]['c']['criteria']), ['b','a'])
            self.assertEqual((state,questions), before)
            self.assertIn('raw_output',raw)
        finally:engine.close()

    def test_source_change_stops_run_and_resume_does_not_switch_model(self):
        inf.predict(self.config_path,'test',self.root/'run',limit=2)
        p=self.root/'run/predictions-hard.jsonl';p.write_text(p.read_text().splitlines()[0]+'\n')
        self.source.write_text(PLUGIN+'\n# source revision changed\n')
        with self.assertRaises(inf.InferenceFailure) as context:
            inf.predict(self.config_path,'test',self.root/'run',limit=2,resume=True)
        self.assertTrue(context.exception.critical)
        self.assertEqual(sys.modules[self.module_name].CLOSES,2)

    def test_completed_resume_skips_plugin_import_and_calls(self):
        inf.predict(self.config_path,'test',self.root/'run',limit=2)
        module=sys.modules[self.module_name];loads=module.LOADS;calls=len(module.CALLS)
        inf.predict(self.config_path,'test',self.root/'run',limit=2,resume=True)
        self.assertEqual(module.LOADS,loads)
        self.assertEqual(len(module.CALLS),calls)

    def test_fatal_model_failure_stops_and_closes(self):
        self.config['options']={'fail':'oom'};self.write_config()
        with self.assertRaises(inf.InferenceFailure) as context:
            inf.predict(self.config_path,'test',self.root/'run',limit=2)
        self.assertTrue(context.exception.critical)
        self.assertEqual(sys.modules[self.module_name].CLOSES,1)
        self.assertEqual(len(cli.read(self.root/'run/predictions-hard.jsonl')),1)

    def test_invalid_plugin_return_is_a_failed_answer_without_transport_retries(self):
        self.config['options']={'fail':'bad-result'};self.write_config()
        inf.predict(self.config_path,'test',self.root/'run',split='hard',limit=2)
        predictions=cli.read(self.root/'run/predictions-hard.jsonl')
        self.assertTrue(all(q['status']=='failed' for row in predictions for q in row['question_results'].values()))
        self.assertEqual(sys.modules[self.module_name].CLOSES,1)

    def test_cli_engine_override_and_relative_module_path(self):
        self.config.pop('engine');self.config['module_paths']=['.'];self.write_config()
        c=inf.config_for(self.config_path,'test','hard',self.module_name+':ModelEngine')
        self.assertEqual(c['module_paths'],[str(self.root)])
        engine=inf.engine_for(c)
        engine.close()
        self.assertEqual(sys.modules[self.module_name].CLOSES,1)

    def test_module_cache_cannot_silently_select_a_different_repository(self):
        engine=inf.engine_for(inf.config_for(self.config_path,'test','hard'));engine.close()
        other=self.root/'other';other.mkdir();(other/(self.module_name+'.py')).write_text(PLUGIN)
        c={**self.config,'module_paths':[str(other)]}
        with self.assertRaisesRegex(ValueError,'another repository'):
            inf.engine_for(c)
        self.assertEqual(sys.modules[self.module_name].LOADS,1)

    def test_cleanup_failure_does_not_report_complete(self):
        self.source.write_text(PLUGIN.replace('CLOSES += 1', 'CLOSES += 1; raise RuntimeError("cleanup failed")'))
        with self.assertRaisesRegex(RuntimeError,'cleanup failed'):
            inf.predict(self.config_path,'test',self.root/'run',split='hard',limit=1)
        self.assertEqual(json.loads((self.root/'run/status.json').read_text())['status'],'failed')

    def test_rejects_parallel_python_execution_and_http_only_settings(self):
        for update in [{'concurrency':2},{'timeout_seconds':3},{'options':[]},{'engine':'python'},{'engine':'os:system'}]:
            config={**self.config,**update}
            if update=={'engine':'os:system'}:
                with self.assertRaises(ValueError):inf.engine_for(config)
            else:
                with self.assertRaises(ValueError):inf.validate_config(config)

if __name__ == '__main__':unittest.main()
