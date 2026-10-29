import hashlib
import copy
import json
import tempfile
import shutil
import subprocess
import sys
from unittest.mock import patch
import unittest
from pathlib import Path
from evaluation import __main__ as cli
from evaluation.scoring import evaluate

ROOT = Path(__file__).resolve().parents[1]

class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = cli.read(ROOT / 'benchmark/records.jsonl')

    def record(self, kind='noul', soft=False):
        return copy.deepcopy(next(r for r in self.records if
            next(iter(r['questions'].values()))['type'] == kind and
            all(l['label_type'] == ('soft' if soft else 'hard') for l in r['labels'].values())))

    def test_frozen_scoring_sources(self):
        expected = {'scoring.py': 'fc356e4d9c924c848a22fa415c0af22deab70c4a837df24c4b1ea5d67d11323b'}
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((ROOT/'evaluation'/name).read_bytes()).hexdigest(), digest)

    def test_export_inputs_match_original_frozen_bytes(self):
        # Hashes recorded from the original formal V0.5 files before removing duplicates.
        expected = {
            False: '540193afee21accb78587c70194dd5af8151d4ac146a5ab2ff472cff505bad17',
            True: '2fc29129fab2cafe2cf171a3cdae6b1e700daf0285c2b263a27c94ca799507a9',
        }
        before = copy.deepcopy(self.records)
        with tempfile.TemporaryDirectory() as d:
            for soft_views, digest in expected.items():
                p = Path(d) / str(soft_views)
                cli.export_inputs(p, 'soft' if soft_views else 'all', soft_views)
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest)
                rows = cli.read(p)
                self.assertEqual(len(rows), 60 if soft_views else 325)
                for row in rows:
                    self.assertEqual(set(row), {'task_id','validation_view','state','questions'}
                                     if soft_views else {'id','state','questions'})
                with self.assertRaises(FileExistsError):
                    cli.export_inputs(p, 'soft' if soft_views else 'all', soft_views)
        self.assertEqual(self.records, before)

    def test_export_split_and_view_validation(self):
        self.assertEqual(len(cli.make_requests(self.records, 'hard')), 295)
        self.assertEqual(len(cli.make_requests(self.records, 'soft')), 30)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'bad.jsonl'
            with self.assertRaises(ValueError):
                cli.export_inputs(p, 'hard', True)
            self.assertFalse(p.exists())

    def test_compact_public_record_format(self):
        for record in self.records:
            self.assertEqual(set(record), {'id','group_id','state','questions','labels','metadata'})
            self.assertEqual(set(record['metadata']), {'scenario'})
            for label in record['labels'].values():
                self.assertEqual(set(label), {'label_type','value'})
        payload = json.dumps(self.records, ensure_ascii=False, sort_keys=True,
                             separators=(',',':'), allow_nan=False).encode()
        self.assertEqual(hashlib.sha256(payload).hexdigest(),
                         'b6a245f73bb20828805a05997a3aa6c57e9f23312f5240999908b8e113afc8ae')

    def soft_prediction(self, view, valid=True):
        record = next(r for r in self.records if r['id'] == view['task_id'])
        qid = next(iter(record['questions']))
        kind = record['questions'][qid]['type']
        value = record['labels'][qid]['value']
        answer = {'type': kind, 'noul': float(value)} if kind == 'noul' else {'type': kind, 'probabilities': value}
        return {'task_id': view['task_id'], 'validation_view': view['validation_view'],
                'visible_input_sha256': cli.digest({'state': view['state'], 'questions': view['questions']}),
                'observation': {'status': 'valid', 'answer': answer} if valid else {'status': 'failed'}}

    def test_replay_reads_explicit_external_bundle(self):
        # Independent reference-answer fixture: no downloaded model outputs are needed in CI.
        hard = []
        for record in self.records:
            if not all(v['label_type'] == 'hard' for v in record['labels'].values()):
                continue
            answers = {}
            for qid, label in record['labels'].items():
                kind = record['questions'][qid]['type']
                field = {'noul': 'noul', 'choice': 'choice', 'score': 'score'}[kind]
                value = float(label['value']) if kind == 'noul' else label['value']
                answers[qid] = {'status': 'valid', 'answer': {'type': kind, field: value}}
            hard.append({'id': record['id'], 'question_results': answers})
        soft = [self.soft_prediction(v) for v in cli.make_soft_views(self.records)]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/'benchmark-repo'
            (root/'benchmark').mkdir(parents=True)
            (root/'results').mkdir()
            shutil.copyfile(ROOT/'benchmark/records.jsonl', root/'benchmark/records.jsonl')
            board = {'models': [{'model': 'reference-fixture', 'hard_task_score': 1.0,
                                'soft_tv_valid_only': 0.0, 'soft_valid': 60, 'hard_valid_questions': 303}]}
            (root/'results/leaderboard.json').write_text(json.dumps(board))
            external = Path(d)/'external-predictions.jsonl'
            external.write_text(json.dumps({'model': 'reference-fixture', 'hard': hard, 'soft': soft})+'\n')
            with patch.object(cli, 'ROOT', root):
                result = cli.replay(Path(d)/'replay', external)
            self.assertEqual(result['models_verified'], 1)
            self.assertEqual(result['new_model_calls'], 0)
            self.assertFalse((root/'results/predictions.jsonl').exists())

    def test_replay_missing_external_file_creates_no_output(self):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d)/'replay'
            with self.assertRaisesRegex(FileNotFoundError, 'distributed separately'):
                cli.replay(output, Path(d)/'missing.jsonl')
            self.assertFalse(output.exists())

    def test_replay_cli_requires_predictions_argument(self):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d)/'replay'
            result = subprocess.run([sys.executable, '-m', 'evaluation', 'replay', '--output', str(output)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('--predictions', result.stderr)
            self.assertFalse(output.exists())

    def test_noul_tie_is_historical_no(self):
        r=self.record();qid=next(iter(r['questions']))
        x=evaluate(r,qid,{'status':'valid','answer':{'type':'noul','noul':0.5}})
        self.assertIs(x['predicted'],False)

    def test_invalid_probability_inputs(self):
        r=self.record();qid=next(iter(r['questions']))
        for p in (True,float('nan'),float('inf'),-0.1,1.1):
            with self.subTest(p=p):
                x=evaluate(r,qid,{'status':'valid','answer':{'type':'noul','noul':p}})
                self.assertEqual(x['status'],'invalid')
                self.assertFalse(x['correct'])

    def test_probability_tolerances_and_missing_support(self):
        r=self.record('choice');qid=next(iter(r['questions']));keys=r['questions'][qid]['option_order']
        for total,valid,renorm in ((1.0,True,False),(1.01,True,True),(1.03,False,None)):
            probs={k:total/len(keys) for k in keys}
            x=evaluate(r,qid,{'status':'valid','answer':{'type':'choice','probabilities':probs}})
            self.assertEqual(x['status']=='valid',valid)
            if valid:self.assertEqual(x['renormalized'],renorm)
        x=evaluate(r,qid,{'status':'valid','answer':{'type':'choice','probabilities':{keys[0]:1.0}}})
        self.assertEqual(x['status'],'invalid')

    def test_missing_predictions_count_as_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'empty.jsonl';p.write_text('')
            hard=cli.score(p,'hard')['metrics']
            self.assertEqual(hard['hard_task_score'],0)
            self.assertEqual(hard['hard_tasks'],295)
            soft=cli.score(p,'soft',True)['metrics']
            self.assertEqual(soft['soft_expected'],60)
            self.assertEqual(soft['soft_valid'],0)
            self.assertFalse(soft['soft_rank_eligible'])
            self.assertIsNone(soft['soft_tv_valid_only'])

    def test_duplicate_unknown_and_wrong_hash_rejected(self):
        row=self.soft_prediction(cli.make_soft_views(self.records)[0])
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.jsonl'
            for rows in ([row,row],[dict(row,task_id='unknown')],[dict(row,visible_input_sha256='bad')]):
                p.write_text(''.join(json.dumps(x)+'\n' for x in rows))
                with self.assertRaises(ValueError):cli.score(p,'soft',True)

    def test_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'score.json';cli.save(p,{'first':True})
            with self.assertRaises(FileExistsError):cli.save(p,{'first':False})
            self.assertEqual(json.loads(p.read_text()),{'first':True})

    def test_partial_soft_coverage_excluded(self):
        rows = [self.soft_prediction(view, valid=i < 46)
                for i, view in enumerate(cli.make_soft_views(self.records))]
        metrics = cli.score_rows(rows, 'soft', True)['metrics']
        self.assertEqual(metrics['soft_expected'], 60)
        self.assertEqual(metrics['soft_valid'], 46)
        self.assertFalse(metrics['soft_rank_eligible'])

    def test_hard_task_mean_not_question_micro(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'one.jsonl'
            r=next(r for r in self.records if len(r['questions'])>1)
            qid=next(iter(r['questions']));q=r['questions'][qid];label=r['labels'][qid]
            if q['type']=='noul':answer={'type':'noul','noul':float(label['value'])}
            elif q['type']=='choice':answer={'type':'choice','choice':label['value']}
            else:answer={'type':'score','score':label['value']}
            p.write_text(json.dumps({'id':r['id'],'question_results':{qid:{'status':'valid','answer':answer}}})+'\n')
            result=cli.score(p,'hard')['metrics']
            self.assertAlmostEqual(result['hard_task_score'],1/len(r['questions'])/295)

if __name__ == '__main__':
    unittest.main()
