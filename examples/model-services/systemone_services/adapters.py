"""Model-owned historical inference adapters; no Benchmark imports or labels."""
import copy
import hashlib
import importlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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


def wire_question(question, rendering='historical'):
    q = {k:copy.deepcopy(question[k]) for k in ('type','instructions','criteria') if k in question}
    if q['type'] == 'choice':
        order = question.get('option_order', list(question['criteria']))
        q['criteria'] = {k:q['criteria'][k] for k in order}
    if rendering in ('oneshot','oneshot_hard'):
        if not isinstance(q['instructions'], str):
            q['instructions'] = json.dumps(q['instructions'], ensure_ascii=False, **({'separators':(',',':')} if rendering=='oneshot_hard' else {}))
        if q['type'] == 'noul' and q.get('criteria') == {}:
            q.pop('criteria')
        if q['type'] == 'score':
            q['criteria'] = [v if isinstance(v,str) else json.dumps(v,ensure_ascii=False) for v in q['criteria']]
    elif rendering == 'native_string_instructions' and not isinstance(q['instructions'], str):
        q['instructions'] = json.dumps(q['instructions'], ensure_ascii=False)
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


class HTTPModel:
    def __init__(self, config):
        self.c = config
        endpoint = config.get('endpoint','')
        if not endpoint or '${' in endpoint:
            raise ValueError('Configure endpoint for ' + config['name'])
        self.secret = os.environ.get(config.get('key_env',''), '')
        if config.get('key_env') and not self.secret:
            raise ValueError('Missing credential variable: ' + config['key_env'])

    def predict(self, state, questions):
        c = self.c
        # Preserve the original transport's root insertion order as well as JSON values.
        payload = {'model':c['model_id']} if c.get('model_id') else {}
        payload.update(state=state, questions={k:wire_question(q,c.get('rendering','historical')) for k,q in questions.items()})
        controls = c.get('controls',{})
        if set(controls) - {'think_mode','gate','think_budget','mix','allow_none','none_check','enable_thinking'}:
            raise ValueError('Only historical inference controls are allowed')
        payload.update(controls)
        # Configuration may only add top-level controls, never labels or metadata.
        if set(payload) - {'model','state','questions',*c.get('controls',{})}:
            raise ValueError('Unexpected inference payload fields')
        data = json.dumps(payload,ensure_ascii=False, separators=(',',':') if c.get('compact_json', True) else None,allow_nan=False).encode()
        attempts = []
        for i in range(c['transport_retries'] + 1):
            started = time.monotonic()
            receipt = {'attempt':i, 'request_sha256':hashlib.sha256(data).hexdigest()}
            headers = {'Content-Type':'application/json'}
            if self.secret:
                headers['Authorization'] = 'Bearer ' + self.secret
            try:
                request = urllib.request.Request(c['endpoint'],data=data,headers=headers)
                with urllib.request.urlopen(request,timeout=c['timeout_seconds']) as response:
                    raw = response.read().decode()
                    receipt.update(http_status=response.status,raw_response_text=raw)
                body = json.loads(raw)
                receipt['seconds'] = time.monotonic()-started
                attempts.append(scrub(receipt,[self.secret]))
                if c.get('expected_returned_models') and body.get('model') not in c['expected_returned_models']:
                    raise InferenceFailure('Returned model version differs: '+str(body.get('model')),attempts,True)
                if set(body.get('answers',{})) != set(questions):
                    raise InferenceFailure('Returned question IDs differ',attempts)
                return {'answers':body['answers'],'raw_response':scrub(body,[self.secret]),'attempts':attempts,
                        'request_sha256':hashlib.sha256(data).hexdigest()}
            except InferenceFailure:
                raise
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                code = getattr(exc,'code',None)
                receipt.update(http_status=code,error=str(exc),seconds=time.monotonic()-started)
                if isinstance(exc,urllib.error.HTTPError):
                    receipt['raw_response_text'] = exc.read().decode(errors='replace')
                attempts.append(scrub(receipt,[self.secret]))
                critical = code in (401,402,403,451)
                retry = not isinstance(exc,ValueError) and (code is None or code in (408,429,500,502,503,504))
                if critical or not retry or i == c['transport_retries']:
                    raise InferenceFailure(scrub(str(exc),[self.secret]),attempts,critical) from None
                time.sleep(c.get('retry_delay_seconds',1) * 2**i)


class NativeModel:
    """Reuse native model APIs and the audited historical head adapters, never chat substitutes."""
    def __init__(self, c):
        self.c = c
        for path in reversed(c.get('source_paths',[])):
            if not Path(path).exists():
                raise FileNotFoundError('Missing native source path: '+path)
            sys.path.insert(0,path)
        import torch
        self.torch = torch
        torch.set_num_threads(c.get('cpu_threads',2))
        torch.manual_seed(c.get('seed',20261009))
        checkpoint = c['checkpoint']
        if not Path(checkpoint).exists():
            raise FileNotFoundError('Missing checkpoint: '+checkpoint)
        if c['adapter'] in ('clef','strands'):
            # Methods below are a verbatim extraction of the historical evaluated adapters.
            self.engine = {'clef':ClefAdapter,'strands':StrandsAdapter}[c['adapter']](c)
        elif c['adapter'] == 'd3':
            sys.path.insert(0,checkpoint)
            from d3_runtime import D3
            self.engine = D3.from_pretrained(checkpoint,device='cuda:0',batch_size=8,verify='fast',local_files_only=True,max_length=65536)
        elif c['adapter'] == 'pplx':
            sys.path.insert(0,str(Path(checkpoint)/'source/src'))
            from pplx_decider import Decider
            self.engine = Decider.from_pretrained(checkpoint,device='cuda',fast=False,max_length=65536,cpu_threads=4)
        else:
            raise ValueError('Unsupported native adapter: '+c['adapter'])

    def predict(self,state,questions):
        if self.c['adapter'] in ('clef','strands'):
            return self.engine.predict(state,questions)
        q = {k:wire_question(v,'native_string_instructions') for k,v in questions.items()}
        request = {'state':state,'questions':q}
        body = self.engine.system_one(**request) if self.c['adapter']=='d3' else self.engine.decide(request)
        return {'answers':body['answers'],'raw_response':body}


def canonical(x):
    return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def support(q):
    if q['type']=='noul':return ['true','false']
    if q['type']=='score':return [str(i) for i in range(len(q['criteria']))]
    return list(q.get('option_order',q['criteria']))

def from_probs(q,p):
    if q['type']=='noul':return {'type':'noul','noul':float(p.get('true',p.get('yes')))}
    if q['type']=='choice':return {'type':'choice','choice':max(p,key=p.get),'probabilities':p}
    return {'type':'score','score':sum(int(k)*v for k,v in p.items()),'probabilities':p}


def bootstrap():
    import torch
    torch.set_num_threads(2)
    return torch


class ClefAdapter:
 def __init__(self,c):
  self.c=c;self.torch=bootstrap();self.device='cuda:0';self.torch.manual_seed(c['settings']['seed']);sys.path.insert(0,c['checkpoint'])
  self.lib=importlib.import_module('joint_schema_model');self.model,self.processor=self.lib.load_release_model(c['checkpoint'],device=self.device,attn_implementation='sdpa');self.model.eval()
  default=self.lib.question_options
  def options(q):
   pairs=default(q);order=q.get('_eval_order');return sorted(pairs,key=lambda pair:order.index(pair[0])) if order else pairs
  self.lib.question_options=options
 def predict(self,state,questions):
  answers={};meta={}
  for qid,q in questions.items():
   question={**wire_question(q),'_eval_order':support(q)};req={'state':state,'questions':{qid:question}}
   full=self.lib.encode_record(self.processor.tokenizer,req,max_length=10**9,processor=self.processor)
   maximum=self.c['settings']['max_input_tokens']
   if len(full.input_ids)>maximum:raise ValueError('context_limit_exceeded:'+str(len(full.input_ids)))
   encoded=self.lib.encode_record(self.processor.tokenizer,req,max_length=maximum,processor=self.processor)
   if encoded.input_ids!=full.input_ids:raise ValueError('silent_truncation_rejected')
   with self.torch.inference_mode():logits=self.model(self.lib.collate_records([encoded],self.processor.tokenizer.pad_token_id,self.torch.device(self.device)))[0][0]
   p=dict(zip(encoded.questions[0].option_ids,logits.float().softmax(-1).tolist()));answers[qid]=from_probs(q,p);meta[qid]={'input_tokens':len(full.input_ids),'raw_logits':logits.float().tolist(),'option_order':support(q)}
  return {'answers':answers,'metadata':meta,'raw_response':{'answers':answers}}

class StrandsAdapter:
 def __init__(self,c):
  self.c=c;self.torch=bootstrap();self.torch.manual_seed(c['settings']['seed'])
  from strands_decider.modeling import StrandsDeciderModel
  from strands_decider.infer import SystemOneEngine,EngineConfig
  self.loader=StrandsDeciderModel;original=StrandsDeciderModel._load_torso
  def local(config,device_map,attn_implementation):
   config.base_model=c['base_checkpoint'];config.base_revision=None if Path(c['base_checkpoint']).exists() else c.get('base_revision');return original(config,device_map,attn_implementation)
  StrandsDeciderModel._load_torso=staticmethod(local)
  try:self.model=StrandsDeciderModel.load(c['checkpoint'],attn_implementation='sdpa')
  finally:StrandsDeciderModel._load_torso=original
  s=c['settings'];self.model.config.max_length=s['max_input_tokens']
  if c['profile']=='evaluated':self.model.config.temperature=1.;self.model.config.temperature_by_kind={}
  # Keep checkpoint's calibrated temperatures in release mode, never trust stale evidence values.
  self.engine=SystemOneEngine(self.model,EngineConfig(device='cuda:0',use_prefix_cache=False,strict_window=True,model_name=c['id']))
  self.defaults={'temperature':self.model.config.temperature,'temperature_by_kind':self.model.config.temperature_by_kind}
 def predict(self,state,questions):
  from strands_decider.schema import SystemOneRequest
  from strands_decider.prompting import render_state,render_question
  answers={};metadata={}
  flatten=lambda v:v if isinstance(v,str) else '' if v is None else canonical(v)
  for qid,q in questions.items():
   w=wire_question(q)
   if q['type']=='score':w['criteria']=[flatten(v) for v in w['criteria']]
   else:w['criteria']={k:flatten(v) for k,v in w.get('criteria',{}).items()}
   req=SystemOneRequest.model_validate({'state':state,'questions':{qid:w}});rq=render_question(req.questions[qid],option_order=list(range(len(support(q)))))
   st=render_state(req.state);ids=self.engine.tok(st,add_special_tokens=True)['input_ids'];qids=self.engine.tok(rq.text,add_special_tokens=False)['input_ids']
   if len(ids)+len(qids)>self.c['settings']['max_input_tokens']:raise ValueError('context_limit_exceeded:'+str(len(ids)+len(qids)))
   fitted,question_ids=self.engine._fit(st,[rq.text])
   if fitted!=ids or question_ids!=[qids]:raise ValueError('silent_truncation_rejected')
   with self.torch.inference_mode():probs,tokens=self.engine._slot_probs_batched(fitted,question_ids,[rq.n_slots],[rq.kind],rendered=[rq])
   p=dict(zip(rq.slot_labels,probs[0,:rq.n_slots].tolist()));answers[qid]=from_probs(q,p);metadata[qid]={'input_tokens':len(ids)+len(qids),'calibration':self.defaults,'option_order':list(rq.slot_labels)}
  return {'answers':answers,'metadata':metadata,'raw_response':{'answers':answers}}



