"""Distribution validation, argmax and ordinal expected-value scoring.

All scoring is deterministic and pure. Malformed distributions fail closed:
they are invalid and count as incorrect; callers must not invent calibration.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

SUM_TOL = 1e-3
# v1 froze SUM_TOL = 1e-3 before the run. The run then showed what that
# actually measures: several models round their probabilities to three
# decimals, so a nine-option answer lands on 0.999 or 1.005 and would be
# thrown out for arithmetic rather than for judgement. RENORM_TOL is the band
# inside which a distribution is treated as a rounded one - rescaled to sum to
# 1 and scored normally. Outside it, the answer is still invalid and still
# counts as wrong. Both tolerances are reported for every model, so the
# pre-registered strict number stays visible next to the headline.
RENORM_TOL = 2e-2


class InvalidDistribution(ValueError):
    pass


def validate_probs(probs: dict, labels: list, sum_tol: float = SUM_TOL) -> dict:
    """Validate a probability map against the exact label set.

    Keys must match labels exactly (no missing, no extra). Values must be
    finite floats in [0, 1] and sum to 1 within sum_tol. Returns a sanitized
    copy with plain float values.
    """
    if not isinstance(probs, dict):
        raise InvalidDistribution("probs is not a dict")
    want = set(labels)
    got = set(probs.keys())
    if got != want:
        raise InvalidDistribution(
            f"label keys mismatch: missing={sorted(want - got)} extra={sorted(got - want)}"
        )
    out = {}
    total = 0.0
    for k, v in probs.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise InvalidDistribution(f"prob[{k!r}] is not a number: {v!r}")
        f = float(v)
        if not math.isfinite(f):
            raise InvalidDistribution(f"prob[{k!r}] is not finite")
        if f < 0.0 or f > 1.0:
            raise InvalidDistribution(f"prob[{k!r}] out of [0,1]: {f}")
        out[str(k)] = f
        total += f
    if abs(total - 1.0) > sum_tol:
        raise InvalidDistribution(f"probs sum to {total}, tolerance {sum_tol}")
    return out


def argmax_label(probs: dict) -> str:
    """Deterministic argmax: ties broken by lexicographically smallest label."""
    best, best_p = None, -1.0
    for k in sorted(probs.keys()):
        if probs[k] > best_p:
            best, best_p = k, probs[k]
    return best


def expected_value(probs: dict) -> float:
    """Expected value over ordinal score levels keyed by string indices."""
    return sum(float(k) * v for k, v in probs.items())


def top_label_confidence(probs: dict) -> float:
    return max(probs.values()) if probs else 0.0


def score_task(probs: dict, task) -> dict:
    """Score one distribution against a canonical task.

    Returns: valid (inside RENORM_TOL, the headline rule), strict_valid
    (inside SUM_TOL, the pre-registered rule), renormalized, correct (None
    when expected is None), predicted, ordinal_ev (score family only).
    Distributions outside RENORM_TOL are invalid and count as wrong; they are
    never repaired into a distribution.
    """
    try:
        clean = validate_probs(probs, task.labels)
        strict_valid, renormalized = True, False
    except InvalidDistribution as strict_error:
        try:
            clean = validate_probs(probs, task.labels, sum_tol=RENORM_TOL)
        except InvalidDistribution:
            return {"valid": False, "strict_valid": False, "renormalized": False,
                    "error": str(strict_error), "correct": False, "predicted": None}
        total = sum(clean.values())
        if total <= 0:
            return {"valid": False, "strict_valid": False, "renormalized": False,
                    "error": "probabilities sum to zero", "correct": False,
                    "predicted": None}
        clean = {k: v / total for k, v in clean.items()}
        strict_valid, renormalized = False, True

    # `probs` is the distribution every downstream metric uses: the model's own
    # numbers, rescaled only when they were inside the rounding band.
    base = {"valid": True, "strict_valid": strict_valid,
            "renormalized": renormalized, "probs": clean}
    if task.expected is None:
        pred = argmax_label(clean) if task.question["type"] != "score" else None
        out = {**base, "correct": None, "predicted": pred}
        if task.question["type"] == "score":
            out["ordinal_ev"] = expected_value(clean)
        return out

    out = dict(base)
    if task.question["type"] == "score":
        ev = expected_value(clean)
        out["ordinal_ev"] = ev
        out["predicted"] = argmax_label(clean)
        out["correct"] = out["predicted"] == str(task.expected)
    else:
        pred = argmax_label(clean)
        out["predicted"] = pred
        out["correct"] = pred == task.expected
    return out


def score_label(label, task) -> dict:
    """Score a label-only answer (a system with no distribution, e.g. Needle 3).

    Valid when the label is in the exact label set. No probabilities are
    produced here or anywhere downstream, so calibration metrics skip it.
    An abstention (label None) is a valid-but-empty answer that counts wrong.
    """
    ok = label is not None and str(label) in [str(x) for x in task.labels]
    out = {"valid": ok, "strict_valid": ok, "renormalized": False, "probs": None,
           "predicted": str(label) if ok else None}
    if task.expected is None:
        out["correct"] = None
    else:
        out["correct"] = ok and str(label) == str(task.expected)
    if not ok:
        out["error"] = "abstained" if label is None else "label outside the label set"
    return out


def support(question):
    if question['type'] == 'choice':
        return question['option_order']
    if question['type'] == 'score':
        return [str(i) for i in range(len(question['criteria']))]
    return ['yes', 'no']

def distribution_metrics(target, predicted):
    floor = 1e-12
    kl = lambda a, b: sum(x * math.log(x / max(y, floor)) for x, y in zip(a, b) if x > 0)
    middle = [(x + y) / 2 for x, y in zip(target, predicted)]
    return {'tv': sum(abs(x - y) for x, y in zip(target, predicted)) / 2,
            'brier': sum((x - y) ** 2 for x, y in zip(target, predicted)),
            'kl_gold_prediction': kl(target, predicted),
            'js': (kl(target, middle) + kl(predicted, middle)) / 2}


def evaluate(record,qid,observation):
 q=record['questions'][qid];label=record['labels'][qid]
 base={'task_id':record['id'],'question_id':qid,'label_type':label['label_type'],'status':'missing','correct':False}
 if observation is None:return base
 if observation.get('status')!='valid':return dict(base,status=observation.get('status','failed'))
 try:
  a=observation['answer']
  if a['type']!=q['type']:raise ValueError('answer_type_mismatch')
  keys=support(q)
  if q['type']=='noul':
   p=a['noul']
   if isinstance(p,bool) or not isinstance(p,(int,float)) or not math.isfinite(p) or not 0<=p<=1:raise ValueError('invalid_noul')
   probs={'yes':p,'no':1-p}
  else:probs=a.get('probabilities')
  gold=('yes' if label['value'] else 'no') if q['type']=='noul' and label['label_type']=='hard' else str(label['value']) if label['label_type']=='hard' else None
  task=SimpleNamespace(labels=keys,question=q,expected=gold)
  if probs is None:
   if label['label_type']!='hard':raise ValueError('soft_requires_probabilities')
   v=a.get('choice') if q['type']=='choice' else a.get('score')
   if q['type']=='score' and isinstance(v,(int,float)) and float(v).is_integer():v=str(int(v))
   s=score_label(v,task)
   if not s['valid']:raise ValueError(s['error'])
   pred=int(s['predicted']) if q['type']=='score' else s['predicted']
   return dict(base,status='valid',correct=bool(s['correct']),predicted=pred,strict_valid=False,renormalized=False,probability_source='label_only')
  s=score_task(probs,task)
  if not s['valid']:raise ValueError(s['error'])
  clean=s['probs'];pred=s['predicted']
  if pred is None:pred=min(clean,key=lambda k:(-clean[k],k))
  pred=(pred=='yes') if q['type']=='noul' else int(pred) if q['type']=='score' else pred
  out=dict(base,status='valid',correct=bool(s['correct']),predicted=pred,strict_valid=s['strict_valid'],renormalized=s['renormalized'])
  if q['type']=='score':
   out['api_score']=a.get('score');out['probability_expected_score']=s['ordinal_ev']
   if isinstance(a.get('score'),(int,float)):out['api_score_delta']=a['score']-s['ordinal_ev']
  if label['label_type']=='soft':
   target=[label['value'],1-label['value']] if q['type']=='noul' else [label['value'][k] for k in keys]
   distribution=[clean[k] for k in keys];out.update(distribution=distribution,**distribution_metrics(target,distribution))
  return out
 except (ValueError,KeyError,TypeError) as e:return dict(base,status='invalid',error=str(e))
