# Research platform extension v1

The existing .swa schema 1 and general project format_version 1 remain unchanged. The original engines, copied vendor source, input snapshots and terminal run records are immutable. New services write only `research/` sidecars inside a user-selected project. Removing that directory rolls back the extension without removing experiments (back it up first). No in-place migration is required.

## Common result boundary

`result_adapter.load_runs(project_path)` reads completed original records without recovering or changing active runs. `adapt_run(record, engine, project_path)` creates a JSON-safe view with schema=1, run_id, engine, name, config, data_hash, result_hash, source_file, evaluation, provenance, metrics and predictions. Metrics have stable id, model_id, metric, value, unit, direction, n_rows, n_participants, variation, reason, and field_path into the original record. Evaluation separates task/target/split/population/synthetic/calculation/evaluation_type. Missing facts remain unknown; they are never inferred as real/external validation.

Comparisons require compatible task, target, metric definition, units, data hash, evaluation design, actual evaluation IDs and split where applicable. Incomparable runs do not receive a delta. Partial evidence yields reference comparison only. Fold SD is variation, not a confidence interval.

## Evidence/report boundary

Experiment numbers are resolved from immutable records, never retrieval output. EvidenceLibrary is project-scoped under `research/evidence.sqlite3`. Documents/chunks have stable versioned IDs and source anchors. Revoked, deleted or changed sources cannot validate a citation. Retrieval scores are relevance only. Deterministic lexical fallback is explicitly distinct from multilingual semantic embeddings; local model use never downloads silently.

Reports are versioned structured JSON before rendering. Every experimental claim references run+result hash+field path; citations reference source+version+chunk+anchor+exact short excerpt+search ID. Edits invalidate prior checks and review status. Draft, automatic-check-passed and user-review-recorded are distinct. Automatic checks do not establish semantic support, expert validation or certification. A report language change creates a new version from the same facts and evidence.

All external calls are opt-in and separate from core experiments/search/templates. No retrieved text is executed as an instruction. User review and export previews disclose data leaving the computer. No raw participant records are sent by default. API-less fixtures and actual provider tests are reported separately.

## Delivery order and limits

1. Baseline + common schema; 2. multi-run comparison and deterministic templates; 3. local parsing/search/anchors/evaluation; 4. claim validation/review/DOCX/PDF/optional LLM; 5. frozen external assessment, participant wrapper and independent-unit uncertainty; 6. real bilingual GUI, compatibility and exports.

GPU, nested CV and true future external validation are not silently claimed. Holdout remains the supported selection/final-evaluation design. Synthetic examples verify software behavior only.
