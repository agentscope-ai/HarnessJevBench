"""Optional model-owned in-process engine; no Benchmark dependency."""
import copy
import gc
import hashlib
import json
from pathlib import Path
import sys
from .adapters import NativeModel
from .__main__ import HTTP_ADAPTERS, profile_for, validate_profile


class NativeEngine:
    """Decision Index-style callable for the four historical native implementations."""
    def __init__(self, profile_path, model, split='hard'):
        self.config = profile_for(profile_path, model, split)
        if self.config['adapter'] in HTTP_ADAPTERS:
            raise ValueError('NativeEngine requires a native model profile; use HTTP for upstream services')
        issues = validate_profile(self.config)
        if issues:
            raise ValueError('; '.join(issues))
        self.provenance = {
            'model': model,
            'backend': self.config['adapter'],
            'checkpoint_revision': self.config.get('revision'),
            'backend_revision': self.config.get('source_commit'),
            'identity_sha256': self.config.get('identity_sha256', {}),
            'profile_sha256': hashlib.sha256(json.dumps(self.config, sort_keys=True,
                ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
            'adapter_source_sha256': hashlib.sha256(Path(__file__).with_name('adapters.py').read_bytes()).hexdigest(),
        }
        self.model_id = self.config.get('model_id') or model
        self.impl = NativeModel(self.config)

    def __call__(self, state, questions):
        if self.impl is None:
            raise RuntimeError('NativeEngine is closed')
        result = self.impl.predict(state, questions)
        response = copy.deepcopy(result.get('raw_response', {'answers': result['answers']}))
        response.setdefault('model', self.model_id)
        return response, result

    def runtime(self):
        torch = self.impl.torch
        versions = {name: str(getattr(sys.modules[name], '__version__', 'unknown'))
                    for name in ('torch', 'transformers') if name in sys.modules}
        return {'packages': versions, 'cuda': torch.version.cuda,
                'device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu'}

    def close(self):
        if self.impl is None:
            return
        torch = self.impl.torch
        self.impl = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
