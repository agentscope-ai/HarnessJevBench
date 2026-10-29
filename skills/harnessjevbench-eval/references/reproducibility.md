# Data and reproduction notes / 数据与复现说明

Use this reference when inspecting task schemas, reproducing the published snapshot, or reporting limitations. Model deployment is documented in [deployment.md](deployment.md).

## Metric names and code identifiers

| Public metric | 中文 | Existing split / result field |
|---|---|---|
| Decision Accuracy ↑ | 决策准确率 | `hard`, `hard_task_score` |
| Distribution Distance (TV) ↓ | 概率分布距离（TV） | `soft`, `soft_tv_valid_only` |

The names describe the measured quantities; they do not change scoring or imply easy/hard difficulty levels. Decision Accuracy averages question correctness within each task, then across 295 tasks. Invalid/missing answers count as zero. Distribution Distance is mean total variation distance over the 60 fixed views, with valid/expected coverage reported separately. Incomplete valid-only distance is diagnostic and ineligible for complete-coverage ranking. The metrics are not combined.

## Data format

[`benchmark/records.jsonl`](../../../benchmark/records.jsonl) is the sole task file. Each record retains `id`, `group_id`, `state`, `questions`, `labels` and `metadata.scenario`. Each label retains `label_type` and `value`; `label_type` remains `hard` or `soft`. Noul references are Boolean or P(yes); Choice uses an option ID or probability map; Score uses a level index or probability map.

`group_id` tracks related task families and supports contamination analysis. The public format is `harnessjevbench.public.v1`, recorded in [MANIFEST.json](../../../MANIFEST.json). It is a compact publication format, not the original full training schema. Use `export` to produce model inputs and send only `state` and `questions`. Task identifiers and reference labels remain outside the request.

The two fixed distribution views reorder finite weighted worlds in the final state message, not answer options. Choice option order and Score level indices remain unchanged. Embedded file paths are task evidence; evaluating a record does not require accessing those paths.

## Saved predictions and the published table

The optional `HarnessJevBench-predictions-v0.5-12models.zip` contains `predictions.jsonl` with twelve JSONL lines, one per model. It is distributed separately from the source package; its manifest records the prediction, task-data and leaderboard hashes. Each line has `model`, `hard` (295 task records / 303 questions) and `soft` (60 view records). View records include `task_id`, `validation_view`, `visible_input_sha256` and `observation`. Reference labels are read from the task file.

Failures remain records with non-valid statuses. Luna has 60 saved distribution-view rows, of which 46 are valid. The README therefore shows “—” for its Distribution Distance; the original valid-only value remains available in the result file. Saved row counts do not establish successful coverage. These files contain normalized historical predictions rather than full HTTP logs.

[`results/leaderboard.json`](../../../results/leaderboard.json) stores the fixed V0.5 historical scores. Its rows were measured at different times: nine historical rows are retained unchanged, and the three distinct Blink checkpoints were evaluated with actual new inference on 2026-10-28. The old OneShot row is retired rather than renamed. Each Blink row records its checkpoint/config hashes, runtime and inference controls. All three have 303/303 valid definite-answer questions and 60/60 valid distribution views. README ranks use Decision Accuracy, with shared ranks for ties. Use `replay --predictions /path/to/predictions.jsonl` for the extracted twelve-model bundle; `score` expects one model's individual task/view rows.

The previous ten-model archive is a different historical snapshot and cannot reproduce the current twelve-row table. The three Blink rows use the measured rerun outputs, not the training team's baseline metrics.

## Construction and evaluation scope

Development used model feedback, related task families and OneShot-aware task selection. All released tasks are public; no independent hidden test split is provided. Historical development/test/diagnostic markers described construction lineage rather than official train/test splits. Retain family identifiers when assessing overlap.

Some tasks are authored derivatives of prior frozen tasks. Inherited provenance records lineage and does not establish production collection or executed agent runs. Source proofs, scoped reviews, removed metadata and the original records remain in separate publication archives. Publication preparation did not add an independent blind review. The prior quality review retained minor issues and an incomplete difficulty-gradient gate.

Distribution references describe explicit finite weighted worlds. They do not establish calibration on production distributions. The benchmark evaluates decisions at supplied runtime control points; end-to-end agent success, deployment safety, speed and cost require separate measurements.

中文要点：题目构建使用过模型反馈和同源变体，含 OneShot 相关选题；没有独立隐藏测试集。分布参考答案来自显式有限世界，不代表生产概率校准。原始构建材料另行归档，整理发布不等于新增独立盲审。

## Historical inference protocols

- The four hosted API profiles used concurrency 4 for Decision Accuracy and 1 for Distribution Distance. Other client profiles generally use concurrency 1. Provider IDs do not prove unchanged hidden weights.
- Historical OneShot Decision Accuracy used custom PPU vLLM; Distribution Distance used HF Torch SDPA + FLA. Numerical equivalence is not established. The later HF run explicitly changes the former backend; old and new Blink checkpoints are separate identities.
- Model-specific prompting, readout, calibration, context limits, attention and thinking controls belong to the model service. Included wrappers preserve the historical implementations; weights, full upstream runtimes and private settings are external dependencies.
- Resume verifies inputs, configuration and implementation identities and preserves recorded failures. Keep raw responses and coverage. No extra model-probability calibration is applied by the Benchmark client.
- Report benchmark version, publication revision, record hash, model/checkpoint revision, backend/runtime, inference controls, measurement date and coverage with each fresh result.

## Version identity

Benchmark content **V0.5** was frozen on **2026-10-22**: 295 definite-answer tasks and 30 distribution-reference tasks, across 29 scenarios. Older V0.4 reports described 30 scenarios and ten distribution-reference tasks; those figures do not describe this release.

Publication 0.5.1 removed non-scoring metadata while retaining task IDs, group IDs, state, questions, labels, scenarios and record order. Source and public record hashes therefore differ; generated inputs and all 60 view hashes remain identical to the original exports. Regression tests enforce these identities.

Publication **0.5.12** updates metric presentation, shortens the bilingual README, redesigns the bilingual six-responsibility coverage figure, moves these notes into the evaluation skill and includes runnable model-service examples. Task bytes, Benchmark inference/scoring code and published prediction/result files remain unchanged. Source/public SHA256 values and all released file hashes are in [MANIFEST.json](../../../MANIFEST.json).

Publication **0.5.13** simplifies the release file layout with clearer leaderboard and deployment-guide filenames, a standard archive root, and removal of an intermediate rerun report. The replay loader follows the renamed leaderboard file. The bilingual coverage figures use a two-row, three-column layout of representative decision flows, without a central summary panel. Data, predictions, scores, inference, scoring and model-service implementations are unchanged; unpublished model integrations are excluded.

Publication **0.5.14** moves historical predictions to a separate optional archive. `replay` requires an explicit `--predictions` file. The source test suite and CI use generated reference-answer fixtures instead of historical model outputs. Full historical replay is verified separately against the optional archive. Data, reference labels, frozen scorer, inference implementations and published leaderboard values remain unchanged.

Publication **0.5.15** replaces the retired OneShot leaderboard row with measured Blink 4B, 9B and 27B results, updates the bilingual tables/model count and supplies a matching optional twelve-model replay archive. The other nine historical rows, task data, scorer, inference client and model-service implementations are unchanged. Unpublished local model integration changes remain excluded.

Data use the [MIT license](../../../LICENSE.md#benchmark-data--mit). Code/documentation licensing remains as recorded in [LICENSE.md](../../../LICENSE.md); upstream models and dependencies keep their own terms.
