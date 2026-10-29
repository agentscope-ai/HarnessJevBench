---
name: harnessjevbench-eval
description: Run HarnessJevBench fresh inference through HTTP services or external model-owned Python engines, reproduce the published leaderboard, generate label-free inputs, or score supplied predictions. Use for HarnessJevBench evaluation requests in this repository.
---

# HarnessJevBench evaluation

Run commands from this repository's root: two directories above the folder containing this file. Confirm `benchmark/records.jsonl` and `evaluation/__main__.py` exist there. Python 3.10+ and the standard library suffice for offline scoring and HTTP inference. HTTP mode keeps model dependencies in a separate service. Optional Python mode imports a model-owned engine in the evaluation process and needs that model's environment; the Benchmark does not vendor full upstream runtimes or weights. Read [README](../../README.md) for the overview and [reproduction notes](references/reproducibility.md) for data contracts, limitations and historical protocols.

Public metric names are **Decision Accuracy** (`hard`, `hard_task_score`) and **Distribution Distance (TV)** (`soft`, `soft_tv_valid_only`). Keep the existing split names, output fields and scoring rules. Use [model deployment](references/deployment.md) to start the historical ten-route examples (the OneShot route is retained for compatibility, not in the current table); read [reproduction notes](references/reproducibility.md) for schema inspection or historical comparisons.

## Choose the requested operation

**Reproduce the published results** when asked to evaluate the release without another model or prediction file:

```bash
python -m evaluation verify
python -m evaluation replay --predictions /path/to/predictions.jsonl --output runs/replay-UNIQUE_RUN_ID
```

Replace `UNIQUE_RUN_ID` with a new identifier; every output path must be absent. Replay reads the file explicitly supplied by `--predictions`, recomputes all twelve models and verifies their scores/coverage against `results/leaderboard.json`. Obtain and extract the separately distributed `HarnessJevBench-predictions-v0.5-12models.zip` first, then replace `/path/to/predictions.jsonl` with its extracted file. If that optional archive is unavailable, report the missing input rather than starting model inference. It makes no inference calls. Report it as offline scoring validation, not a new model run.

**Score supplied predictions** when the user provides model outputs:

```bash
python -m evaluation verify
python -m evaluation score --split hard --predictions /path/to/hard.jsonl --output runs/UNIQUE_RUN_ID/hard.json
python -m evaluation score --split soft --soft-views --predictions /path/to/soft.jsonl --output runs/UNIQUE_RUN_ID/soft.json
```

Run the relevant split(s). The Soft leaderboard requires both fixed views for all 30 tasks. If the user only supplies original-view outputs, omit `--soft-views` and describe Soft as an original-order diagnostic, not the published two-view comparison. Missing predictions must remain missing.

**Generate model inputs** before a new inference run:

```bash
python -m evaluation export --split hard --output runs/UNIQUE_RUN_ID/hard-inputs.jsonl
python -m evaluation export --split soft --soft-views --output runs/UNIQUE_RUN_ID/soft-inputs.jsonl
```

To export all 325 original-order requests, omit `--split hard` in the first command. Exports are derived from the sole data file `records.jsonl`, never from hand-edited fixtures. Submit only `state` and `questions` to the model. The exported IDs identify responses locally; labels and metadata are excluded.

## Fresh model inference

The unified `Engine` interface is callable as `engine(state, questions) -> (response, raw_output)`. HTTP is the default; a configured `module:Class` loads a model-owned Python engine in process. Neither route receives reference labels. Use [configs/models.json](../../configs/models.json), the sole public client configuration, and make a local copy for deployment endpoints. Legacy built-in native adapters and top-level checkpoint/rendering fields remain unsupported. HTTP runtime settings belong in the model service; Python model arguments belong in its `options` object. Direct `/api/alpha/decisions` endpoints still require a gateway in HTTP mode. Existing archived run artifacts remain historical evidence, not resumable runs for the new client.

The model-owned `systemone_services` examples are included in [examples/model-services/](../../examples/model-services/). They contain wrappers for four native models and gateways for OneShot, Instant and four hosted APIs. Run them there or copy the package into the corresponding model repository; see the [deployment guide](references/deployment.md) for model-by-model requirements and commands. The Benchmark does not start or terminate model servers. Any existing service implementing the contract can be used without this integration bundle.

The HTTP request contains only `model`, `state` and `questions`. Python engines receive the same visible `state` and projected `questions`, with candidate order preserved and no task IDs or labels. Both routes keep the historical one-question call granularity and aggregate answers into tasks. Require the original question IDs in `answers`, complete Choice/Score distributions, and P(yes) for Noul. Keep model prompt rendering, calibration, thinking controls and GPU placement on the server side.

## Ten-model SystemOne examples

These are the original ten deployment profiles, not the current twelve-row leaderboard roster. The retired `OneShot` selector is kept only for historical compatibility. Blink has three separate checkpoints (`Blink-4B-1028`, `Blink-9B-1028`, `Blink-27B-1028`); its model-owned runtime must be supplied separately through the existing HTTP or Python Engine contract. Do not point a Blink checkpoint at the old OneShot profile or reuse the old OneShot score.

The ports in `configs/models.json` are example local addresses. Launch the appropriate services before calling them, and replace endpoints with actual deployment addresses.

| Historical example / CLI selector | Port | Model-side integration |
|---|---:|---|
| `OneShot` | Hard 8001; Soft 8011 | Hard historical PPU vLLM upstream; Soft HF upstream |
| `vllm-sr/d3` | 8002 | Native `D3.system_one()` wrapper |
| `Instant` | 8003 | StartLux HTTP gateway with historical controls |
| `perplexity-ai/pplx-decider-v1.2` | 8004 | Native `Decider.decide()` wrapper |
| `inception/mercury-decide` | 8005 | OpenRouter gateway |
| `JEV1.14` | 8006 | OpenRouter gateway |
| `openai/gpt-6-luna-decisions` | 8007 | OpenRouter gateway |
| `microsoft/microsoft-decision-1` | 8008 | OpenRouter gateway |
| `Clef-27B-original` | 8009 | Native `joint_schema_model` wrapper |
| `strands-decider-26B-A4B-gemma4-v1-2610` | 8010 | Native `SystemOneEngine` wrapper |

### Prepare the service and client

From `examples/model-services/`, copy `systemone_services/profiles.json` to ignored `profiles.local.json`, configure paths/upstream endpoints and credentials, and start the selected model in its own environment. The [deployment guide](references/deployment.md) covers all ten routes. Run the following commands there:

```bash
# Native model: its Python environment and weights must be available
python -m systemone_services --model vllm-sr/d3 --config profiles.local.json --gpu 0 --port 8002

# Hosted gateway: OPENROUTER_API_KEY is read by this process
python -m systemone_services --model JEV1.14 --config profiles.local.json --port 8006
```

These model-side modules live under `examples/model-services/`; the Benchmark client imports no model dependencies in HTTP mode. OneShot and Instant gateways require their original upstream services first. OneShot Hard PPU availability is not implied; substituting HF is a disclosed backend change. Native routing aliases are wrapper names, not claims about upstream public model IDs. Preserve Instant's historical `think_mode=never` and all other controls in the server profile.

Then, from the Benchmark repository root, copy the public client configuration once, preserving any existing local file:

```bash
cp configs/models.json configs/models.systemone.local.json
```

Edit `endpoint` and any `splits` endpoint overrides. Four hosted models retain Hard concurrency 4 and Soft concurrency 1; the other profiles use 1. Gateway forwarding makes one upstream attempt; only the Benchmark applies configured transport retries. Changing concurrency, grouping, rendering or inference controls creates a different measurement protocol.

If the wrapper requires authentication, add `"key_env": "SYSTEMONE_API_KEY"` to the client profile and set that variable or supply an existing private `--env-file`. Hosted-provider keys belong on the gateway, not in the Benchmark configuration. Use `expected_returned_models` and optional `expected_metadata` to pin available model/service revision fields; never silently remove pins to accept another model.

### Check, smoke-test and evaluate

```bash
# Offline configuration check; does not establish service health
python -m evaluation doctor --model JEV1.14 --config configs/models.systemone.local.json

# Real inference on two Hard tasks: partial coverage is diagnostic
python -m evaluation predict --model JEV1.14 --config configs/models.systemone.local.json \
  --split hard --limit 2 --output runs/systemone-jev-smoke

# Full evaluation: 295 Hard tasks and 60 Soft views, followed by scoring
python -m evaluation predict --model JEV1.14 --config configs/models.systemone.local.json \
  --output runs/systemone-jev-full

# A different model uses the same client
python -m evaluation predict --model vllm-sr/d3 --config configs/models.systemone.local.json \
  --output runs/systemone-d3-full
```

Replace `--model` with any exact table selector. `--split` selects a phase, `--limit` caps rows for diagnostics, and `--resume` continues the same fingerprinted run. GPU selection belongs to the model implementation; the Benchmark has no `--gpu` option. For Python engines, select the environment/device before running the CLI or through model-owned options. `doctor` makes no network calls and exits nonzero for invalid configuration. `predict` performs real inference and may incur provider charges.

Each run writes `protocol.json`, `receipts.jsonl`, predictions, phase scores, `status.json` and `service-identities.json` (the retained filename stores both HTTP and Python engine identities). Available returned model/provider, checkpoint/backend/runtime and service code/profile fingerprints are fixed on the first response per phase. A changed declared identity during a run or resume stops it. This cannot detect a provider changing hidden weights without changing its declared identifiers. Unknown internal revisions stay unknown.

Resume skips existing rows including failures. Authentication/access/quota failures, unknown endpoints/models and explicit version/identity conflicts stop the run; transient retryable network errors follow the configured bounded retry policy. Do not overwrite published results with fresh outputs or silently change a backend to recover a failed run.

### Evaluate the configured ten-model roster

Use only after all configured services are ready and fresh inference for the roster is requested. Choose a new `run_root`; each output directory must be absent. Models run sequentially; phase concurrency remains as configured.

```python
import json
import subprocess
import sys
from pathlib import Path

config = Path("configs/models.systemone.local.json")
run_root = Path("runs/systemone-ten-models")
for model in json.loads(config.read_text())["models"]:
    subprocess.run([
        sys.executable, "-m", "evaluation", "predict",
        "--model", model, "--config", str(config),
        "--output", str(run_root / model.replace("/", "--")),
    ], check=True)
```

Compare coverage and per-question probabilities/decisions with the archived predictions before claiming historical equivalence. Distinguish gateway-byte checks, native direct-versus-HTTP validation, partial smoke tests, offline replay and a full fresh ten-model run in the report.

## Python in-process engines (optional)

Use this route when a model repository exposes a Python callable and the user wants to evaluate it directly. HTTP remains the default when `engine` is omitted or set to `http`. Set `engine` to `package.module:Class` in the model configuration, or use `--engine package.module:Class` with a compatible Python configuration. The CLI does not silently discard HTTP-only fields when an override selects Python.

### Model-owned contract

The class is constructed once with `Class(**options)`. Implement `__call__(state, questions)` returning **`(response, raw_output)`**. `response` must identify the actual `model` and contain `answers` keyed by the original question IDs, using the existing Noul/Choice/Score formats. `raw_output` must be JSON-safe or `None`. Return probabilities without introducing Benchmark-side calibration. Malformed probabilities still follow the frozen scorer rules; do not substitute Decision Index's validation tolerances.

The class can optionally subclass `evaluation.inference.Engine`, but duck typing avoids a Benchmark dependency in the model repository. Optional `provenance` (stable revision information), `runtime()` (stable dependency/device information) and `close()` are supported. Model code is responsible for loading weights, rendering prompts, preserving historical readout/calibration and releasing resources. Do not put credential values in `options`; read them from the model process's environment when needed.

The included model-service examples provide a working model-owned class, `systemone_services.engine:NativeEngine`, for Clef, Strands, D3 and Perplexity. It reuses the historical native implementations without running an HTTP server. Configure its model-side `profiles.local.json` as described in the [deployment guide](references/deployment.md). Other models can implement their own class using the same contract; no model-specific import is added to this Benchmark.

### Configuration and commands

Create an ignored `configs/models.python.local.json` with the following example, after setting `D3_MODEL_REPO` to the absolute path of `examples/model-services/` and `D3_MODEL_PROFILES` to its configured `profiles.local.json`:

```json
{
  "models": {
    "D3-Python": {
      "engine": "systemone_services.engine:NativeEngine",
      "model_id": "vllm-sr/d3",
      "module_paths": ["${D3_MODEL_REPO}"],
      "options": {
        "profile_path": "${D3_MODEL_PROFILES}",
        "model": "vllm-sr/d3"
      },
      "concurrency": 1
    }
  }
}
```

`module_paths` are import roots; relative paths resolve against the configuration file's directory. `options` are passed unchanged after environment-variable expansion. Existing ten-model HTTP profiles do not need modification. Python configurations omit HTTP-only fields such as `endpoint`, `adapter`, `transport_retries` and `timeout_seconds`.

Activate the model's own Python/CUDA environment, then run from the Benchmark root:

```bash
# Configuration only: does not import the plugin or load weights
python -m evaluation doctor --model D3-Python --config configs/models.python.local.json

# Real in-process smoke test; no HTTP server is needed
CUDA_VISIBLE_DEVICES=0 python -m evaluation predict --model D3-Python \
  --config configs/models.python.local.json --split hard --limit 2 --output runs/d3-python-smoke

# Full Hard + Soft evaluation with the same engine
CUDA_VISIBLE_DEVICES=0 python -m evaluation predict --model D3-Python \
  --config configs/models.python.local.json --output runs/d3-python-full
```

The same native adapter supports the other three native models by changing `model_id` and `options.model` to their exact selectors and preparing the matching environment. OneShot/Instant/upstream API profiles continue to use their HTTP services. Using another Python implementation for them is possible through a model-owned class and requires disclosing any backend change.

### Execution and reproduction

Python engines use `concurrency=1`; calls are not automatically retried, timed out or warmed up. This preserves call order and avoids replaying stateful/GPU failures. The same effective configuration reuses its loaded engine across Hard/Soft; a changed split configuration closes the old engine before loading another. `close()` runs on completion and on failures. Fatal out-of-memory or device-assert errors stop the run; ordinary per-question errors remain failed observations. Resume preserves existing failures as before.

The runner records the module/class source hashes, declared provenance/runtime, actual response model and available revision fields in receipts and `service-identities.json`. A changed source or declared identity stops an unfinished run/resume. These hashes cover the entry module and class source, not every imported dependency or weight file: pin model revisions and dependencies in the model-owned configuration/provenance too. The supplied `NativeEngine` additionally records its resolved model-profile hash and native-adapter source hash. Use a fresh CLI process and the appropriate environment per model; conflicting cached module names from different repositories are rejected.

`doctor` validates configuration and import-directory presence without importing a plugin. It cannot certify missing dependencies, a valid class constructor or a working GPU. Python source changes create a different experiment, and current task bytes, reference labels, view generation, task weighting, probability tolerances and frozen snapshots must remain unchanged.

## Prediction contracts

Hard predictions: one JSONL row per task, keyed by the unchanged task ID. Include all answered question IDs, which are not always `decision`:

```json
{"id":"TASK_ID","question_results":{"QUESTION_ID":{"status":"valid","answer":{"type":"noul","noul":0.9}}}}
```

Answer formats:

- Noul: `{"type":"noul","noul":0.9}` means probability of yes.
- Choice: `{"type":"choice","probabilities":{"OPTION_A":0.7,"OPTION_B":0.3}}`, using every actual option ID.
- Score: `{"type":"score","probabilities":{"0":0.1,"1":0.6,"2":0.3}}`, using all actual level indices.

Historical label-only answers may score Hard where the scorer permits them. Soft requires a complete distribution. For a failed call, retain a non-valid `status` without manufacturing an answer.

Soft predictions: one JSONL row per task/view:

```json
{"task_id":"TASK_ID","validation_view":0,"visible_input_sha256":"SHA256","observation":{"status":"valid","answer":{"type":"noul","noul":0.9}}}
```

Compute `visible_input_sha256` over UTF-8 JSON of `{"state":view["state"],"questions":view["questions"]}` with `ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False`. `evaluation.__main__.digest` implements it. Include the hash even for failed views. Unknown/duplicate task-view IDs or mismatched hashes stop scoring; correct the export/mapping rather than suppressing the check.

The separately distributed historical `predictions.jsonl` is a **bundle**, with one row per model and nested `hard` and `soft` arrays. Use `replay --predictions /path/to/predictions.jsonl` for the bundle. The `score` command expects a single model's individual task/view rows, not the bundle itself. To rescore a bundled model separately, select the exact `model` ID and export its `hard` or `soft` array as JSONL to a fresh run directory.

## Fixed rules to preserve

- Hard: question correctness averaged within each task, then across 295 tasks. Invalid/missing outputs count as zero.
- Soft: mean total variation distance across 60 task-views; smaller is better. Incomplete valid-only TV is diagnostic and ineligible for the full Soft ranking. Do not combine Hard and Soft.
- Frozen probability validation: finite non-Boolean numbers in [0,1], exact support, strict sum tolerance 0.001; the existing 0.02 rounding band permits renormalization. Do not retune it.
- Historical ties use lexical option IDs; Noul at 0.5 ties to `no`. Hard Score correctness uses probability argmax; expected value is diagnostic.
- Soft views reorder `worlds` in the final state message, not answer options. The generator sorts by SHA-256 of `quality-repair-revision-03-unseen-order-{view}|{task_id}|{world_id}`, then serializes the fixture compactly. Use the existing generator; keep the original input hashes pinned in tests.

For code or dataset changes, run `python -m unittest discover -s tests -v` and `verify`; replay when scoring compatibility is affected. Do not edit published result snapshots to conceal a mismatch. Keep outputs under `runs/`, not `results/`, unless updating the published results is explicitly part of the task.

## Report the outcome

State benchmark/content version and publication revision from `MANIFEST.json`, records hash, operation (offline replay vs supplied predictions vs actual new inference), Hard task score, Soft TV with valid/expected coverage and rank eligibility, and output path. For new inference, also record model/checkpoint/revision, backend, readout/temperature, thinking controls, context limit and recovery policy. If an operation fails, report the concrete failure and any completed export; do not present a partial run as complete.
