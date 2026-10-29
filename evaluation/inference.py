"""Unified HTTP and model-owned Python engines with frozen offline scoring."""
import copy
import hashlib
import importlib
import inspect
import platform
import re
import sys
import json
import math
import http.client
import os
import time
import threading
import urllib.error
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from . import __main__ as cli


class InferenceFailure(RuntimeError):
    def __init__(self, message, attempts=None, critical=False):
        super().__init__(message)
        self.attempts = attempts or []
        self.critical = critical


def load_env(path):
    """Load operator-provided credentials without logging or overriding existing values."""
    if path:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not key.replace('_', '').isalnum():
                raise ValueError('Invalid environment-file syntax')
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def resolve(value):
    if isinstance(value, dict):
        return {k:resolve(v) for k,v in value.items()}
    if isinstance(value, list):
        return [resolve(v) for v in value]
    return os.path.expandvars(value) if isinstance(value, str) else value


COMMON_FIELDS = {'name', 'engine', 'model_id', 'concurrency',
                 'expected_returned_models', 'expected_metadata'}
HTTP_FIELDS = {'adapter', 'endpoint', 'key_env', 'timeout_seconds',
               'transport_retries', 'retry_delay_seconds', 'compact_json'}
PYTHON_FIELDS = {'module_paths', 'options'}
ENTRYPOINT = re.compile(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$')
IDENTITY_FIELDS = ('checkpoint_revision', 'backend', 'backend_revision',
                   'service_profile_sha256', 'inference_config_id', 'server_revision',
                   'weight_identity_sha256', 'runtime', 'model_revision', 'revision', 'system_fingerprint',
                   'python_entrypoint', 'python_source_sha256', 'python_provenance', 'python_runtime')


def validate_config(config):
    kind = config.get('engine', 'http')
    python = isinstance(kind, str) and ENTRYPOINT.fullmatch(kind)
    if kind != 'http' and not python:
        raise ValueError('engine must be http or an external module:Class')
    allowed = COMMON_FIELDS | (PYTHON_FIELDS if python else HTTP_FIELDS)
    unknown = set(config) - allowed
    if unknown:
        raise ValueError('Fields not supported by this engine: ' + ', '.join(sorted(unknown)))
    if not isinstance(config.get('model_id'), str) or not config['model_id']:
        raise ValueError('Configure a nonempty model_id')
    workers = config.get('concurrency')
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError('Invalid concurrency')
    pins = config.get('expected_returned_models')
    if pins is not None and (not isinstance(pins, list) or not pins or not all(isinstance(x, str) and x for x in pins)):
        raise ValueError('expected_returned_models must be a nonempty list of model IDs')
    if not isinstance(config.get('expected_metadata', {}), dict):
        raise ValueError('expected_metadata must be an object')
    if python:
        if workers != 1:
            raise ValueError('Python in-process engines require concurrency=1')
        if not isinstance(config.get('options', {}), dict):
            raise ValueError('Python engine options must be an object')
        paths = config.get('module_paths', [])
        if not isinstance(paths, list) or not all(isinstance(p, str) and '${' not in p and Path(p).is_dir() for p in paths):
            raise ValueError('module_paths must contain existing model-repository directories')
        json.dumps(config.get('options', {}), allow_nan=False)
        return
    if config.get('adapter', 'systemone_http') != 'systemone_http':
        raise ValueError('HTTP engine requires systemone_http; model-specific implementations belong in an external engine')
    endpoint = config.get('endpoint', '')
    parsed = urllib.parse.urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid endpoint port') from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid endpoint port')
    if (not endpoint or '${' in endpoint or parsed.scheme not in ('http', 'https')
            or not parsed.hostname or parsed.path.rstrip('/') != '/v1/systemone'
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('Configure an HTTP(S) endpoint ending in /v1/systemone without credentials or query parameters')
    retries = config.get('transport_retries')
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError('Invalid transport_retries')
    for field in ('timeout_seconds', 'retry_delay_seconds'):
        value = config.get(field, 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (field == 'timeout_seconds' and value == 0):
            raise ValueError('Invalid ' + field)


def config_for(path, model, split, engine_override=None):
    rows = json.loads(Path(path).read_text())['models']
    if model not in rows:
        raise ValueError('Unknown model: ' + model)
    config = copy.deepcopy(rows[model])
    config.update(config.pop('splits', {}).get(split, {}))
    config = resolve(config)
    config.setdefault('name', model)
    config.setdefault('engine', 'http')
    if engine_override is not None:
        config['engine'] = engine_override
    config.setdefault('concurrency', 1)
    if config['engine'] == 'http':
        config.setdefault('adapter', 'systemone_http')
        config.setdefault('timeout_seconds', 900)
        config.setdefault('transport_retries', 1)
    elif isinstance(config.get('module_paths'), list):
        if not all(isinstance(p, str) for p in config['module_paths']):
            raise ValueError('module_paths must be a list of directories')
        config['module_paths'] = [str((Path(path).resolve().parent / p).resolve()) for p in config['module_paths']]
    validate_config(config)
    return config


def wire_question(question):
    q = {k:copy.deepcopy(question[k]) for k in ('type', 'instructions', 'criteria') if k in question}
    if q['type'] == 'choice':
        order = question.get('option_order', list(question['criteria']))
        q['criteria'] = {k:q['criteria'][k] for k in order}
    return q


def scrub(value, secrets):
    if isinstance(value, dict):
        return {k:scrub(v,secrets) for k,v in value.items()}
    if isinstance(value, list):
        return [scrub(v,secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
    return value


class Engine:
    """External model contract: (state, questions) -> (response, JSON-safe raw)."""
    provenance = {}

    def __call__(self, state, questions):
        raise NotImplementedError

    def runtime(self):
        return {}

    def close(self):
        pass


class _ResponseEngine(Engine):
    def __init__(self, config, expected_identity=None, on_identity=None):
        validate_config(config)
        self.c = config
        self.secret = os.environ.get(config.get('key_env', ''), '')
        self.reset_identity(expected_identity, on_identity)

    def reset_identity(self, expected_identity=None, on_identity=None):
        self.identity = expected_identity
        self.on_identity = on_identity
        self.identity_lock = threading.Lock()

    def __call__(self, state, questions):
        result = self.predict(state, questions)
        return result['raw_response'], result

    def result(self, body, questions, attempts=None, **extra):
        attempts = attempts or []
        if not isinstance(body, dict) or not isinstance(body.get('model'), str):
            raise InferenceFailure('Response requires model and answers', attempts)
        if self.c.get('expected_returned_models') and body['model'] not in self.c['expected_returned_models']:
            raise InferenceFailure('Returned model version differs: ' + body['model'], attempts, True)
        if not isinstance(body.get('answers'), dict) or set(body['answers']) != set(questions):
            raise InferenceFailure('Returned question IDs differ', attempts)
        identity = self.check_identity(body, attempts)
        return {'answers': body['answers'], 'raw_response': scrub(body, [self.secret]),
                'attempts': attempts, 'service_identity': identity, **extra}

    def check_identity(self, body, attempts):
        metadata = body.get('metadata', {})
        if not isinstance(metadata, dict):
            raise InferenceFailure('Response metadata must be an object', attempts)
        for field, expected in self.c.get('expected_metadata', {}).items():
            if metadata.get(field) != expected:
                raise InferenceFailure('Engine metadata differs: ' + field, attempts, True)
        identity = {'model': body['model'], 'provider': body.get('provider'),
                    'metadata': {k:metadata[k] for k in IDENTITY_FIELDS if k in metadata},
                    'system_fingerprint': body.get('system_fingerprint'),
                    'upstream_metadata': {k:v for k,v in body.get('upstream_metadata', {}).items()
                                          if k in IDENTITY_FIELDS} if isinstance(body.get('upstream_metadata', {}), dict) else {}}
        with self.identity_lock:
            if self.identity is not None and self.identity != identity:
                raise InferenceFailure('Engine identity changed during this run or resume', attempts, True)
            if self.identity is None:
                if self.on_identity:
                    self.on_identity(identity)
                self.identity = identity
        return identity

class HTTPModel(_ResponseEngine):
    def __init__(self, config, expected_identity=None, on_identity=None):
        if config.get('engine', 'http') != 'http':
            raise ValueError('HTTPModel requires the http engine')
        super().__init__(config, expected_identity, on_identity)
        if config.get('key_env') and not self.secret:
            raise ValueError('Missing credential variable: ' + config['key_env'])

    def predict(self, state, questions):
        c = self.c
        payload = {'model': c['model_id'], 'state': state,
                   'questions': {k:wire_question(q) for k,q in questions.items()}}
        data = json.dumps(payload, ensure_ascii=False,
                          separators=(',', ':') if c.get('compact_json', True) else None,
                          allow_nan=False).encode()
        attempts = []
        for i in range(c['transport_retries'] + 1):
            started = time.monotonic()
            receipt = {'attempt': i, 'request_sha256': hashlib.sha256(data).hexdigest()}
            headers = {'Content-Type': 'application/json'}
            if self.secret:
                headers['Authorization'] = 'Bearer ' + self.secret
            try:
                request = urllib.request.Request(c['endpoint'], data=data, headers=headers)
                with urllib.request.urlopen(request, timeout=c['timeout_seconds']) as response:
                    raw = response.read().decode()
                    receipt.update(http_status=response.status, raw_response_text=raw)
                body = json.loads(raw)
                receipt['seconds'] = time.monotonic()-started
                attempts.append(scrub(receipt, [self.secret]))
                return self.result(body, questions, attempts=attempts,
                                   request_sha256=hashlib.sha256(data).hexdigest())
            except InferenceFailure:
                raise
            except (OSError, http.client.HTTPException, ValueError) as exc:
                code = getattr(exc, 'code', None)
                receipt.update(http_status=code, error=str(exc), seconds=time.monotonic()-started)
                if isinstance(exc, urllib.error.HTTPError):
                    receipt['raw_response_text'] = exc.read().decode(errors='replace')
                attempts.append(scrub(receipt, [self.secret]))
                critical = code in (401, 402, 403, 404, 409, 451)
                retry = not isinstance(exc, ValueError) and (code is None or code in (408, 429, 500, 502, 503, 504))
                if critical or not retry or i == c['transport_retries']:
                    raise InferenceFailure(scrub(str(exc), [self.secret]), attempts, critical) from None
                time.sleep(c.get('retry_delay_seconds', 1) * 2**i)


class PythonEngine(_ResponseEngine):
    """Load a model-owned callable class lazily; the Benchmark vendors no model code."""
    def __init__(self, config, expected_identity=None, on_identity=None):
        if config.get('engine', 'http') == 'http':
            raise ValueError('PythonEngine requires a module:Class engine')
        super().__init__(config, expected_identity, on_identity)
        self.plugin = None
        self.added_paths = []
        self.closed = False
        try:
            for path in reversed(config.get('module_paths', [])):
                if path not in sys.path:
                    sys.path.insert(0, path)
                    self.added_paths.append(path)
            module_name, attribute = config['engine'].split(':', 1)
            importlib.invalidate_caches()
            candidates = [Path(path)/relative for path in config.get('module_paths', [])
                          for relative in (module_name.replace('.', '/') + '.py',
                                           module_name.replace('.', '/') + '/__init__.py')]
            expected_file = next((path.resolve() for path in candidates if path.is_file()), None)
            cached = sys.modules.get(module_name)
            if expected_file is not None and cached is not None and getattr(cached, '__file__', None):
                if Path(cached.__file__).resolve() != expected_file:
                    raise ValueError('Engine module already imported from another repository; use a fresh process or unique module name')
            module = importlib.import_module(module_name)
            if expected_file is not None and Path(module.__file__).resolve() != expected_file:
                raise ValueError('Engine module resolved outside the configured repository')
            target = module
            for component in attribute.split('.'):
                target = getattr(target, component)
            if not inspect.isclass(target):
                raise ValueError('Python engine entrypoint must name a callable class')
            self.source_files = {p.resolve() for p in (
                Path(module.__file__) if getattr(module, '__file__', None) else None,
                Path(inspect.getsourcefile(target)) if inspect.getsourcefile(target) else None,
            ) if p is not None}
            if not self.source_files:
                raise ValueError('Python engine requires an inspectable source module')
            self.source_hashes = self.hash_sources()
            self.plugin = target(**copy.deepcopy(config.get('options', {})))
            if not callable(self.plugin):
                raise ValueError('Python engine instance must implement __call__(state, questions)')
            provenance = getattr(self.plugin, 'provenance', {})
            runtime = getattr(self.plugin, 'runtime', None)
            self.metadata = {
                'python_entrypoint': config['engine'],
                'python_source_sha256': self.source_hashes,
                'python_provenance': copy.deepcopy(provenance),
                'python_runtime': {'python': platform.python_version(),
                                   'model': runtime() if callable(runtime) else {}},
            }
            json.dumps(self.metadata, allow_nan=False)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def hash_sources(self):
        return {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(self.source_files)}

    def predict(self, state, questions):
        if self.closed:
            raise InferenceFailure('Python engine is closed', critical=True)
        try:
            unchanged = self.hash_sources() == self.source_hashes
        except OSError:
            unchanged = False
        if not unchanged:
            raise InferenceFailure('Python engine source changed during the run', critical=True)
        try:
            # Match the HTTP-visible question projection and isolate task objects from mutation.
            visible = {qid:wire_question(q) for qid,q in questions.items()}
            result = self.plugin(copy.deepcopy(state), visible)
            if not isinstance(result, tuple) or len(result) != 2:
                raise ValueError('Python engine must return (response, JSON-safe raw_output)')
            body, raw = result
            if not isinstance(body, dict):
                raise ValueError('Python engine response must be an object')
            body = copy.deepcopy(body)
            metadata = body.get('metadata', {})
            if not isinstance(metadata, dict):
                raise ValueError('Response metadata must be an object')
            body['metadata'] = {**metadata, **self.metadata}
            json.dumps((body, raw), ensure_ascii=False, allow_nan=False)
            return self.result(body, questions, raw_output=raw)
        except InferenceFailure:
            raise
        except Exception as exc:
            message = str(exc)
            fatal = (isinstance(exc, MemoryError) or type(exc).__name__ == 'OutOfMemoryError'
                     or any(marker in message.lower() for marker in ('cuda out of memory', 'device-side assert')))
            raise InferenceFailure(type(exc).__name__ + ': ' + message, critical=fatal) from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            close = getattr(self.plugin, 'close', None)
            if callable(close):
                close()
        finally:
            for path in self.added_paths:
                if path in sys.path:
                    sys.path.remove(path)


def engine_for(config, expected_identity=None, on_identity=None):
    cls = HTTPModel if config.get('engine', 'http') == 'http' else PythonEngine
    return cls(config, expected_identity=expected_identity, on_identity=on_identity)


def doctor(config_path, model, split='both', env_file=None, engine_override=None):
    load_env(env_file)
    phases = ('hard', 'soft') if split == 'both' else (split,)
    issues = []
    for phase in phases:
        try:
            config = config_for(config_path, model, phase, engine_override)
            # Never import external plugins here: importing can load weights as a side effect.
            if config['engine'] == 'http':
                HTTPModel(config)
        except (ValueError, KeyError, TypeError) as exc:
            issues.append(phase + ': ' + str(exc))
    return {'model': model, 'issues': issues, 'network_calls': 0, 'gpu_loaded': False,
            'python_plugin_imported': False}


def predict(config_path, model, output, split='both', limit=None, resume=False, env_file=None, engine_override=None):
    from collections import deque
    load_env(env_file)
    if limit is not None and limit < 1:
        raise ValueError('limit must be positive')
    phases = ('hard','soft') if split == 'both' else (split,)
    configs = {phase:config_for(config_path,model,phase,engine_override) for phase in phases}
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    records = cli.read(cli.ROOT/'benchmark/records.jsonl')
    inputs = {phase:(cli.make_requests(records,'hard') if phase=='hard' else cli.make_soft_views(records)) for phase in phases}
    if limit is not None:inputs = {k:v[:limit] for k,v in inputs.items()}
    protocol = {'model':model,'configs':configs,'input_hashes':{k:cli.digest(v) for k,v in inputs.items()},
                'records_sha256':hashlib.sha256((cli.ROOT/'benchmark/records.jsonl').read_bytes()).hexdigest(),
                'adapter_sha256':code_hash,'engines':{phase:c['engine'] for phase,c in configs.items()},'limit':limit,'probability_postprocessing':'raw',
                'labels_transmitted':False,'selected_rows':{k:len(v) for k,v in inputs.items()}}
    fingerprint = cli.digest(protocol)
    output = Path(output)
    if output.exists():
        if not resume:raise FileExistsError('Use a fresh output directory or explicit --resume')
        stored = json.loads((output/'protocol.json').read_text())
        if stored['fingerprint']!=fingerprint:raise ValueError('Resume protocol/input/code differs')
    else:
        if resume:raise ValueError('Cannot resume a nonexistent run')
        output.mkdir(parents=True)
        cli.save(output/'protocol.json',dict(protocol,fingerprint=fingerprint,started_utc=datetime.now(timezone.utc).isoformat()))
    def status(value):
        p = output/'status.tmp';p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');p.replace(output/'status.json')
    identity_path = output/'service-identities.json'
    identities = json.loads(identity_path.read_text()) if identity_path.exists() else {}
    def record_identity(phase, identity):
        identities[phase] = identity
        temporary = output/'service-identities.tmp'
        temporary.write_text(json.dumps(identities,ensure_ascii=False,indent=2)+'\n')
        temporary.replace(identity_path)
    engine = None
    engine_key = None
    try:
        for phase in phases:
            config = configs[phase]
            status({'status':'initializing','model':model,'phase':phase,'completed':0,'expected':len(inputs[phase])})
            path = output/('predictions-'+phase+'.jsonl')
            key = (lambda x:(x['task_id'],x['validation_view'])) if phase=='soft' else (lambda x:x['id'])
            existing = cli.read(path) if path.exists() else []
            done = cli.keyed(existing,key)
            wanted = {key(x) for x in inputs[phase]}
            if set(done)-wanted:raise ValueError('Unexpected IDs in resume output')
            todo = [v for v in inputs[phase] if key(v) not in done]
            if not todo:continue
            current_key = cli.digest(config)
            if engine is None or current_key != engine_key:
                if engine is not None:
                    engine.close()
                    engine = None
                engine = engine_for(config, expected_identity=identities.get(phase),
                                    on_identity=lambda identity:record_identity(phase, identity))
                engine_key = current_key
            else:
                engine.reset_identity(identities.get(phase), lambda identity:record_identity(phase, identity))
            def call(row):
                task_id = row['id'] if phase=='hard' else row['task_id']
                question_results = {};receipts = [];critical = False
                for qid,q in row['questions'].items():
                    started = time.monotonic()
                    try:
                        body, response = engine(row['state'],{qid:q})
                        answer = body['answers'][qid]
                        observation = {'status':'valid','answer':answer}
                        receipt = {'task_id':task_id,'question_id':qid,'phase':phase,
                                   'validation_view':row.get('validation_view'),'response':response}
                    except Exception as exc:
                        observation = {'status':'failed'}
                        receipt = {'task_id':task_id,'question_id':qid,'phase':phase,
                                   'validation_view':row.get('validation_view'),'error':str(exc),
                                   'attempts':getattr(exc,'attempts',[])}
                        critical = getattr(exc,'critical',False)
                    receipt['seconds'] = time.monotonic()-started
                    receipts.append(receipt);question_results[qid]=observation
                    if critical:break
                if phase=='hard':prediction = {'id':task_id,'question_results':question_results}
                else:prediction = {'task_id':task_id,'validation_view':row['validation_view'],
                                    'visible_input_sha256':cli.digest({'state':row['state'],'questions':row['questions']}),
                                    'observation':question_results['decision']}
                return prediction,receipts,critical
            count = len(done)
            workers = config['concurrency']
            with path.open('a' if path.exists() else 'x') as stream, (output/'receipts.jsonl').open('a') as receipt_stream:
                pool = ThreadPoolExecutor(max_workers=workers);pending = deque();it = iter(todo)
                try:
                    for _ in range(workers):
                        row = next(it,None)
                        if row is not None:pending.append(pool.submit(call,row))
                    while pending:
                        prediction,receipts,critical = pending.popleft().result()
                        stream.write(json.dumps(prediction,ensure_ascii=False,allow_nan=False)+'\n');stream.flush()
                        for receipt in receipts:receipt_stream.write(json.dumps(receipt,ensure_ascii=False,allow_nan=False)+'\n')
                        receipt_stream.flush();count+=1
                        status({'status':'critical_stop' if critical else 'running','model':model,'phase':phase,'completed':count,'expected':len(inputs[phase])})
                        if critical:raise InferenceFailure('Critical inference error or engine identity change; stopped without switching model',critical=True)
                        row = next(it,None)
                        if row is not None:pending.append(pool.submit(call,row))
                finally:
                    pool.shutdown(wait=True,cancel_futures=True)
        if engine is not None:
            engine.close()
            engine = None
        metrics = {}
        for phase in phases:
            scores = cli.score(output/('predictions-'+phase+'.jsonl'),phase,phase=='soft')
            score_path = output/('scores-'+phase+'.json')
            if not score_path.exists():cli.save(score_path,scores)
            metrics[phase] = scores['metrics']
        status({'status':'complete','model':model,'metrics':metrics,'fresh_inference':True,'selected_rows':{k:len(v) for k,v in inputs.items()},'finished_utc':datetime.now(timezone.utc).isoformat()})
        return {'model':model,'output':str(output),'metrics':metrics,'fresh_inference':True}
    except Exception as exc:
        status({'status':'failed','model':model,'error':str(exc),'critical':getattr(exc,'critical',False)})
        raise
    finally:
        if engine is not None:
            primary_error = sys.exc_info()[1]
            try:
                engine.close()
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                existing_status = json.loads((output/'status.json').read_text())
                existing_status['cleanup_error'] = str(cleanup_error)
                status(existing_status)
