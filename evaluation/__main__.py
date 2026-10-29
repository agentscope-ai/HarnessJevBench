"""Offline scoring and replay; Python standard library only."""
import argparse
import copy
import hashlib
import json
import math
import statistics
from pathlib import Path
from .scoring import evaluate

ROOT = Path(__file__).resolve().parents[1]

def read(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

def keyed(rows, key):
    result = {}
    for row in rows:
        k = key(row)
        if k in result:
            raise ValueError(f'Duplicate prediction: {k}')
        result[k] = row
    return result

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def make_requests(records, split='all'):
    """Project model inputs without labels, metadata or reference derivations."""
    return [copy.deepcopy({k:r[k] for k in ('id', 'state', 'questions')})
            for r in records if split == 'all' or
            all(label['label_type'] == split for label in r['labels'].values())]

def make_soft_views(records):
    """Reproduce the frozen revision-03 validation world orders exactly."""
    soft = [r for r in records if all(l['label_type'] == 'soft' for l in r['labels'].values())]
    views = []
    for view in range(2):
        for record in soft:
            visible = copy.deepcopy({k:record[k] for k in ('state', 'questions')})
            fixture = json.loads(visible['state']['messages'][-1]['content'])
            seed = 'quality-repair-revision-03-unseen-order-' + str(view)
            fixture['worlds'] = sorted(fixture['worlds'], key=lambda world:
                hashlib.sha256((seed + '|' + record['id'] + '|' + world['id']).encode()).hexdigest())
            visible['state']['messages'][-1]['content'] = json.dumps(
                fixture, ensure_ascii=False, separators=(',', ':'))
            views.append(dict(task_id=record['id'], validation_view=view, **visible))
    return views

def export_inputs(output, split='all', soft_views=False):
    if soft_views and split != 'soft':
        raise ValueError('--soft-views requires --split soft')
    records = read(ROOT / 'benchmark/records.jsonl')
    rows = make_soft_views(records) if soft_views else make_requests(records, split)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as f:
        for row in rows:
            kwargs = {'separators': (',', ':')} if soft_views else {}
            f.write(json.dumps(row, ensure_ascii=False, **kwargs) + '\n')
    return {'exported_inputs': len(rows), 'soft_views': soft_views, 'labels_transmitted': False}

def score(predictions, split='all', soft_views=False):
    return score_rows(read(predictions), split, soft_views)

def score_rows(rows, split='all', soft_views=False):
    records = read(ROOT / 'benchmark/records.jsonl')
    if split != 'all':
        records = [r for r in records if all(l['label_type'] == split for l in r['labels'].values())]
    scores = []
    hard = []
    if soft_views:
        if split != 'soft':
            raise ValueError('--soft-views requires --split soft')
        by = keyed(rows, lambda x: (x['task_id'], x['validation_view']))
        views = keyed(make_soft_views(records),
                      lambda x: (x['task_id'], x['validation_view']))
        if set(by) - set(views):
            raise ValueError('Unexpected task/view IDs')
        records_by = {r['id']: r for r in records}
        for key, view in views.items():
            x = by.get(key)
            if x and x.get('visible_input_sha256') != digest({'state': view['state'], 'questions': view['questions']}):
                raise ValueError(f'Input hash mismatch: {key}')
            result = evaluate(records_by[key[0]], 'decision', x.get('observation') if x else None)
            scores.append(dict(result, validation_view=key[1]))
    else:
        by = keyed(rows, lambda x: x['id'])
        if set(by) - {r['id'] for r in records}:
            raise ValueError('Unexpected task IDs')
        for r in records:
            hs = []
            for qid, label in r['labels'].items():
                result = evaluate(r, qid, by.get(r['id'], {}).get('question_results', {}).get(qid))
                scores.append(result)
                if label['label_type'] == 'hard':
                    hs.append(float(result['correct']))
            if hs:
                hard.append(statistics.mean(hs))
    soft = [s['tv'] for s in scores if s['label_type'] == 'soft' and s['status'] == 'valid']
    soft_expected = sum(s['label_type'] == 'soft' for s in scores)
    return {'metrics': {
        'hard_task_score': statistics.mean(hard) if hard else None,
        'hard_tasks': len(hard),
        'hard_valid_questions': sum(s['label_type'] == 'hard' and s['status'] == 'valid' for s in scores),
        'soft_tv_valid_only': statistics.mean(soft) if soft else None,
        'soft_valid': len(soft), 'soft_expected': soft_expected,
        'soft_rank_eligible': soft_views and len(soft) == 60,
        'view': 'fixed two-view Soft' if soft_views else 'original only; Soft diagnostic'},
        'questions': scores}

def verify():
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text())
    for name, expected in manifest['files'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Checksum mismatch: {name}')
    records = read(ROOT / 'benchmark/records.jsonl')
    keyed(records, lambda r: r['id'])
    if len(records) != 325 or sum(len(r['questions']) for r in records) != 333:
        raise ValueError('Dataset counts differ')
    for split, expected in [('hard', 295), ('soft', 30)]:
        subset = [r for r in records if all(l['label_type'] == split for l in r['labels'].values())]
        if len(subset) != expected:
            raise ValueError(f'{split} split mismatch')
    return {'files_verified': len(manifest['files']), 'tasks': 325, 'questions': 333}

def replay(output, predictions_path):
    if output.exists():
        raise FileExistsError('Use a fresh output directory')
    predictions_path = Path(predictions_path)
    if not predictions_path.is_file():
        raise FileNotFoundError('Historical predictions are distributed separately; pass the extracted predictions.jsonl file with --predictions: ' + str(predictions_path))
    board = json.loads((ROOT / 'results/leaderboard.json').read_text())
    result = []
    predictions = keyed(read(predictions_path), lambda x: x['model'])
    for entry in board['models']:
        name = entry['model'].replace('/', '--')
        prediction = predictions[entry['model']]
        hard = score_rows(prediction['hard'], 'hard')
        soft = score_rows(prediction['soft'], 'soft', True)
        for metric, expected in [('hard_task_score', entry['hard_task_score']),
                                  ('soft_tv_valid_only', entry['soft_tv_valid_only'])]:
            actual = (hard if metric.startswith('hard') else soft)['metrics'][metric]
            if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
                raise ValueError(f'{name}: {metric}: {actual} != {expected}')
        if soft['metrics']['soft_valid'] != entry['soft_valid']:
            raise ValueError(f'{name}: Soft coverage differs')
        if hard['metrics']['hard_valid_questions'] != entry['hard_valid_questions']:
            raise ValueError(f'{name}: Hard coverage differs')
        save(output / name / 'hard.json', hard)
        save(output / name / 'soft.json', soft)
        result.append({'model': entry['model'], **hard['metrics'],
                       **{k:v for k,v in soft['metrics'].items() if k.startswith('soft_')}})
    save(output / 'leaderboard.json', {'snapshot':'current', 'models':result, 'new_model_calls':0})
    return {'models_verified':len(result), 'snapshot':'current', 'new_model_calls':0}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('verify')
    p = sub.add_parser('replay', help='Recompute the leaderboard from a separately supplied historical prediction bundle')
    p.add_argument('--predictions', type=Path, required=True, help='Extracted historical predictions.jsonl; distributed separately from source')
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('export', help='Export label-free inference inputs from records')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--split', choices=['hard','soft','all'], default='all')
    p.add_argument('--soft-views', action='store_true')
    p = sub.add_parser('score')
    p.add_argument('--predictions', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--split', choices=['hard','soft','all'], default='all')
    p.add_argument('--soft-views', action='store_true')
    for command in ('doctor','predict'):
        p = sub.add_parser(command)
        p.add_argument('--model', required=True)
        p.add_argument('--config', type=Path, default=ROOT/'configs/models.json')
        p.add_argument('--split', choices=['hard','soft','both'], default='both')
        p.add_argument('--env-file', type=Path)
        p.add_argument('--engine', help='http (default) or model-owned module:Class; may also be set in config')
        if command == 'predict':
            p.add_argument('--output', type=Path, required=True)
            p.add_argument('--limit', type=int)
            p.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.command in ('doctor','predict'):
        from . import inference
        if args.command == 'doctor':
            result = inference.doctor(args.config,args.model,args.split,args.env_file,args.engine)
        else:
            result = inference.predict(args.config,args.model,args.output,args.split,args.limit,args.resume,args.env_file,args.engine)
    elif args.command == 'verify':
        result = verify()
    elif args.command == 'replay':
        result = replay(args.output, args.predictions)
    elif args.command == 'export':
        result = export_inputs(args.output, args.split, args.soft_views)
    else:
        result = score(args.predictions, args.split, args.soft_views)
        save(args.output, result)
        result = result['metrics']
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if args.command == 'doctor' and result['issues']:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
