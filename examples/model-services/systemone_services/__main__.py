"""Serve a model-owned inference implementation through POST /v1/systemone."""
import argparse
import copy
import hashlib
import hmac
import json
import os
import sys
import platform
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .adapters import HTTPModel, InferenceFailure, NativeModel, load_env, resolve

ROOT = Path(__file__).resolve().parent
HTTP_ADAPTERS = {'http_decisions', 'startlux_http', 'systemone_http'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def profile_for(path, model, split):
    config = copy.deepcopy(json.loads(Path(path).read_text())['models'][model])
    config.update(config.pop('splits', {}).get(split, {}))
    config = resolve(config)
    config.setdefault('routing_model', model)
    # Retry only in the Benchmark client; nesting retries multiplies attempts.
    config['transport_retries'] = 0
    return config


def validate_profile(config):
    issues = []
    for key in ('checkpoint', 'base_checkpoint'):
        if config.get(key) and not Path(config[key]).exists():
            issues.append('missing ' + key)
    for path in config.get('source_paths', []):
        if not Path(path).exists():
            issues.append('missing source path: ' + path)
    for filename, expected in config.get('identity_sha256', {}).items():
        p = Path(config.get('checkpoint', '')) / filename
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            issues.append('identity mismatch: ' + filename)
    if config['adapter'] in HTTP_ADAPTERS:
        if not config.get('endpoint') or '${' in config['endpoint']:
            issues.append('upstream endpoint not configured')
        if config.get('key_env') and not os.environ.get(config['key_env']):
            issues.append('missing credential variable: ' + config['key_env'])
    return issues


def validate_request(body, config):
    if not isinstance(body, dict) or set(body) != {'model', 'state', 'questions'}:
        raise ValueError('Request fields must be model, state, questions')
    aliases = {config['routing_model'], config.get('model_id'), config.get('name')}
    if not isinstance(body['model'], str) or body['model'] not in aliases:
        raise LookupError('Unknown model')
    if not isinstance(body['questions'], dict) or not body['questions']:
        raise ValueError('questions must be a nonempty object')
    for qid, q in body['questions'].items():
        if not isinstance(qid, str) or not isinstance(q, dict):
            raise ValueError('Invalid question')
        if q.get('type') not in {'noul', 'choice', 'score'} or 'instructions' not in q:
            raise ValueError('Question requires type and instructions')
        if set(q) - {'type', 'instructions', 'criteria'}:
            raise ValueError('Unknown question fields')
        if q['type'] == 'choice' and (not isinstance(q.get('criteria'), dict) or not q['criteria']):
            raise ValueError('Choice requires nonempty criteria object')
        if q['type'] == 'score' and (not isinstance(q.get('criteria'), list) or not q['criteria']):
            raise ValueError('Score requires nonempty criteria list')


def make_server(config, host='127.0.0.1', port=8000, engine=None, token_env=None):
    """Initialize one deployment. Native calls serialize within the model process."""
    config = copy.deepcopy(config)
    config['transport_retries'] = 0
    if engine is None:
        issues = validate_profile(config)
        if issues:
            raise ValueError('; '.join(issues))
        engine = HTTPModel(config) if config['adapter'] in HTTP_ADAPTERS else NativeModel(config)
    token = os.environ.get(token_env, '') if token_env else ''
    if token_env and not token:
        raise ValueError('Missing service credential variable: ' + token_env)
    code = Path(__file__).read_bytes() + (ROOT/'adapters.py').read_bytes()
    runtime = {'python': platform.python_version(), 'packages': {
        name: str(getattr(sys.modules[name], '__version__', 'unknown'))
        for name in ('torch', 'transformers', 'vllm') if name in sys.modules}}
    if config['adapter'] not in HTTP_ADAPTERS and 'torch' in sys.modules:
        torch = sys.modules['torch']
        runtime['cuda'] = torch.version.cuda
        runtime['device'] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu'
    metadata = {
        'runtime': runtime,
        'backend': config.get('service_backend', config['adapter']),
        'checkpoint_revision': config.get('revision'),
        'backend_revision': config.get('source_commit'),
        'service_profile_sha256': digest(config),
        'inference_config_id': digest(config),
        'server_revision': hashlib.sha256(code).hexdigest(),
        'weight_identity_sha256': digest(config['identity_sha256']) if config.get('identity_sha256') else None,
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request bodies, headers or credentials in access logs.

        def reply(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def authorized(self):
            return not token or hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def do_GET(self):
            if not self.authorized():
                return self.reply(401, {'error': 'Service authentication required'})
            if self.path.rstrip('/') == '/health':
                return self.reply(200, {'status': 'ready', 'model': config['routing_model'], 'metadata': metadata})
            if self.path.rstrip('/') == '/v1/models':
                return self.reply(200, {'data': [{'id': config['routing_model'], 'metadata': metadata}]})
            self.reply(404, {'error': 'Unknown endpoint'})

        def do_POST(self):
            if self.path.rstrip('/') != '/v1/systemone':
                return self.reply(404, {'error': 'Expected /v1/systemone'})
            if not self.authorized():
                return self.reply(401, {'error': 'Service authentication required'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0:
                    raise ValueError('Nonempty JSON body required')
                body = json.loads(self.rfile.read(length), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
                validate_request(body, config)
            except LookupError:
                return self.reply(404, {'error': 'Unknown model'})
            except (ValueError, TypeError, UnicodeError) as exc:
                return self.reply(422, {'error': str(exc)})
            try:
                if config['adapter'] in HTTP_ADAPTERS:
                    result = engine.predict(body['state'], body['questions'])
                else:
                    with lock:
                        result = engine.predict(body['state'], body['questions'])
                response = copy.deepcopy(result.get('raw_response', {'answers': result['answers']}))
                if not isinstance(response, dict) or not isinstance(response.get('answers'), dict):
                    raise ValueError('Backend did not return answers')
                if set(response['answers']) != set(body['questions']):
                    raise ValueError('Backend question IDs differ')
                response.setdefault('model', config.get('model_id') or config['routing_model'])
                if 'metadata' in result:
                    response.setdefault('backend_observations', result['metadata'])
                upstream_meta = response.get('metadata')
                if isinstance(upstream_meta, dict):
                    response['upstream_metadata'] = copy.deepcopy(upstream_meta)
                response['metadata'] = {**(upstream_meta if isinstance(upstream_meta, dict) else {}), **metadata}
                self.reply(200, response)
            except InferenceFailure as exc:
                attempts = exc.attempts or []
                upstream_status = attempts[-1].get('http_status') if attempts else None
                status = 409 if exc.critical and upstream_status == 200 else (upstream_status or 502)
                self.reply(status, {'error': str(exc), 'metadata': metadata})
            except ValueError as exc:
                self.reply(422, {'error': str(exc), 'metadata': metadata})
            except Exception as exc:
                # Exception text may include private upstream content. Keep details on the operator side.
                self.reply(500, {'error': type(exc).__name__, 'metadata': metadata})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'profiles.json')
    parser.add_argument('--model', required=True)
    parser.add_argument('--split', choices=['hard', 'soft'], default='hard')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--gpu', default='0')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--token-env')
    parser.add_argument('--check', action='store_true', help='Validate paths and credentials without loading a model or making inference calls')
    args = parser.parse_args()
    load_env(args.env_file)
    config = profile_for(args.config, args.model, args.split)
    if args.check:
        issues = validate_profile(config)
        print(json.dumps({'model': args.model, 'issues': issues, 'model_loaded': False, 'inference_calls': 0}))
        if issues:
            raise SystemExit(1)
        return
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    server = make_server(config, args.host, args.port, token_env=args.token_env)
    print(json.dumps({'status': 'ready', 'model': args.model, 'endpoint': 'http://' + args.host + ':' + str(server.server_port) + '/v1/systemone'}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
