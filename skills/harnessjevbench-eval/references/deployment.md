# Model service examples / 模型服务示例

The runnable wrappers and gateways are included in [`examples/model-services/`](../../../examples/model-services/). They load native author implementations or forward to an existing service, and expose `POST /v1/systemone`. Model weights and upstream runtimes remain external dependencies. Use a separate model environment for each native model; gateways require only Python 3.10+.

Scope: these are the historical ten service profiles. The current leaderboard retains nine of those models and adds three Blink checkpoints. The OneShot profile remains a compatibility example. Blink model runtimes are external dependencies and are not bundled in this release; connect an owner-provided service or Python Engine with the existing contract.

中文：下表保留历史十个部署配置，并非当前十二模型榜单的完整部署清单。OneShot 只保留兼容入口；三个 Blink checkpoint 的模型侧实现需由模型仓库提供，再接入现有 HTTP 或 Python Engine 接口。原生模型由包装服务加载官方实现；托管模型通过网关转发；StartLux 和历史 OneShot 先启动模型侧服务。Benchmark 客户端统一调用本机 `/v1/systemone`。权重、CUDA 环境和提供方密钥由部署方配置。

## 1. Prepare the service configuration

From the Benchmark repository root:

```bash
cd examples/model-services
cp systemone_services/profiles.json profiles.local.json
```

Set the environment variables listed below, or edit the corresponding paths/endpoints in the ignored `profiles.local.json`. Keep credential values in environment variables or an existing private `--env-file`; profiles store only the credential variable's name.

| Model / exact selector | Required configuration | Local port |
|---|---|---:|
| `OneShot` | `ONESHOT_HARD_ENDPOINT`, `ONESHOT_SOFT_ENDPOINT`: model-owner services, including `/v1/systemone` | 8001 / 8011 |
| `vllm-sr/d3` | `D3_CHECKPOINT`: old d3 checkpoint directory with `d3_runtime.py` | 8002 |
| `Instant` | `INSTANT_CHECKPOINT`, `INSTANT_ENDPOINT`: StartLux service URL including `/v1/systemone` | 8003 |
| `perplexity-ai/pplx-decider-v1.2` | `PPLX_CHECKPOINT`: checkpoint including `source/src/pplx_decider` | 8004 |
| `inception/mercury-decide` | `OPENROUTER_API_KEY` | 8005 |
| `JEV1.14` | `OPENROUTER_API_KEY` | 8006 |
| `openai/gpt-6-luna-decisions` | `OPENROUTER_API_KEY` | 8007 |
| `microsoft/microsoft-decision-1` | `OPENROUTER_API_KEY` | 8008 |
| `Clef-27B-original` | `CLEF_CHECKPOINT`: original checkpoint including `joint_schema_model.py` | 8009 |
| `strands-decider-26B-A4B-gemma4-v1-2610` | `STRANDS_CHECKPOINT`, `STRANDS_BASE_CHECKPOINT`, `STRANDS_IMPLEMENTATION_PATH` | 8010 |

`STRANDS_IMPLEMENTATION_PATH` is the import root containing the `strands_decider/` package. Paths are absolute local directories. Prepare weights from the model owner's release, install that release's dependencies in its environment, and preserve the recorded revision. `--check` verifies paths, configured small-file hashes and required credential presence without loading weights or sending inference requests. It does not verify every weight shard or upstream availability.

## 2. Native models: D3, Perplexity, Clef, Strands

Use each model's Python environment in a separate terminal. Run these commands from `examples/model-services/` after configuring the relevant paths.

```bash
# vLLM-SR d3: D3.system_one(); this profile is d3, not d3-ultra
python -m systemone_services --model vllm-sr/d3 --config profiles.local.json --check
python -m systemone_services --model vllm-sr/d3 --config profiles.local.json --gpu 0 --port 8002

# Perplexity: Decider.decide()
python -m systemone_services --model perplexity-ai/pplx-decider-v1.2 \
  --config profiles.local.json --gpu 1 --port 8004

# Clef: original joint_schema_model, with historical candidate order/readout
python -m systemone_services --model Clef-27B-original \
  --config profiles.local.json --gpu 2 --port 8009

# Strands: SystemOneEngine, local base model and evaluated-profile calibration
python -m systemone_services --model strands-decider-26B-A4B-gemma4-v1-2610 \
  --config profiles.local.json --gpu 3 --port 8010
```

The wrapper loads weights once, preserves the model's prompt/readout and serializes native calls. Native environments need PyTorch, Transformers and the model-specific dependencies. Source requirements:

| Model | Upstream implementation | Pinned checkpoint |
|---|---|---|
| [d3](https://huggingface.co/vllm-sr/d3) | checkpoint's `d3_runtime.py` | `cf15549a78fc52598ddd1951f321c58372bbd64f` |
| [Perplexity Decider v1.2](https://huggingface.co/perplexity-ai/pplx-decider-v1.2) | checkpoint's `source/src/pplx_decider` | `cce0f3f80033e9d89d80831d5281fd72124aacce` |
| [Clef](https://huggingface.co/Cloudflare/clef) | original checkpoint's `joint_schema_model.py` | identify by the small-file hashes in the profile |
| [Strands](https://huggingface.co/StrandsAgents/strands-decider-26B-A4B-gemma4-v1-2610) | `strands_decider` source plus its Gemma base weights | `3ca5dfe2d5aa758ae162b6249729181f1d13ed65` |

The Clef profile needs no Strands source path. The Strands evaluated profile uses temperature 1 and no additional calibration, matching the recorded protocol. Use an unmodified copy of the profile and prepare the specified checkpoint for historical comparisons; record changed model files or environments as a separate deployment.

## 3. Hosted models: Mercury, Jev, Luna, Microsoft

Set `OPENROUTER_API_KEY` in the service terminal. The included gateway translates the public `/v1/systemone` route to the provider's Decisions endpoint; it preserves the actual returned model and applies no probability calibration. The four profiles retain their recorded model IDs and version checks.

Run each selected command in its own terminal from `examples/model-services/`:

```bash
python -m systemone_services --model inception/mercury-decide --config profiles.local.json --port 8005
python -m systemone_services --model JEV1.14 --config profiles.local.json --port 8006
python -m systemone_services --model openai/gpt-6-luna-decisions --config profiles.local.json --port 8007
python -m systemone_services --model microsoft/microsoft-decision-1 --config profiles.local.json --port 8008
```

Alternatively append `--env-file /private/keys.env`. The key is read by the gateway. A changed returned model ID stops evaluation; select a new profile explicitly for a new provider version. Stable IDs cannot prove that hosted weights remain unchanged. Current model availability and account access depend on the provider.

## 4. StartLux Instant: start the author server, then the gateway

Install the [StartLux implementation](https://github.com/StartLuxLabs/StartLux-Decision) at `cb1d11f1ac9e9b43b95ef3a6468fa29b607c40fd` in its own model environment. The recorded service used vLLM 0.19.1; follow that source revision's installation instructions. Set `INSTANT_CHECKPOINT` to the pinned model directory and start the upstream from that source repository:

```bash
python -m startlux_decide.cli serve --model "$INSTANT_CHECKPOINT" --launch-vllm \
  --gpus 0 --port 28640 --vllm-base-port 28740 --max-model-len 65536 \
  --no-images --gate 0.9 --think-budget 4096 --think-mode never
```

In a second terminal, from `examples/model-services/`:

```bash
export INSTANT_ENDPOINT=http://127.0.0.1:28640/v1/systemone
python -m systemone_services --model Instant --config profiles.local.json --port 8003
```

The model directory must match the revision and file hashes recorded in the profile. Keep the gateway's recorded `think_mode`, `gate`, `think_budget`, `mix`, `allow_none` and `none_check` controls when comparing historical results.

## 5. Historical OneShot route

This selector identifies the old 27B checkpoint in the published snapshot. New Blink checkpoints are separate model identities. Obtain the checkpoint and matching backend from the model owner, start its native decision service, then configure its complete SystemOne URLs. The original Hard profile used custom PPU vLLM; Soft used HF Torch SDPA + FLA. Those upstream runtimes are not supplied by this example.

```bash
# Set ONESHOT_HARD_ENDPOINT and ONESHOT_SOFT_ENDPOINT to running owner services first.
python -m systemone_services --model OneShot --config profiles.local.json --port 8001
```

In another terminal:

```bash
python -m systemone_services --model OneShot --split soft --config profiles.local.json --port 8011
```

Both upstreams must return model ID `jev-policy`. If using an HF reference service for both, explicitly update the Hard profile's backend description and record the backend change. The gateway alone does not recreate a private PPU deployment or establish numerical equivalence.

## 6. Check the route and run the Benchmark

From a separate terminal, check the running gateway (port 8006 here):

```bash
curl --fail http://127.0.0.1:8006/health
curl --fail http://127.0.0.1:8006/v1/systemone \
  -H 'Content-Type: application/json' \
  -d '{"model":"JEV1.14","state":"The job finished successfully.","questions":{"decision":{"type":"noul","instructions":"Did the job finish successfully?"}}}'
```

`/health` confirms wrapper/native readiness; the second request checks upstream inference and may incur provider charges. To protect the wrapper, set a separate service credential and append `--token-env SYSTEMONE_API_KEY`; send it as `Authorization: Bearer ...` and set the same `key_env` on the Benchmark client.

From the Benchmark repository root:

```bash
cp configs/models.json configs/models.systemone.local.json
python -m evaluation doctor --model JEV1.14 --config configs/models.systemone.local.json
python -m evaluation predict --model JEV1.14 --config configs/models.systemone.local.json \
  --output runs/jev-evaluation
```

Update the client endpoints if ports or hosts differ; OneShot has a separate `splits.soft.endpoint`. Use a new output directory for each model. The client applies transport retries; the gateway forwards each request once. Check that direct and wrapped answers agree before a full run. Preserve source/checkpoint versions, runtime, configuration and coverage with the results.
