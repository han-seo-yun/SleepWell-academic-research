# Statistical methods and supported interpretation

This document describes calculations implemented by the upgraded desktop application. It is a software-method record, not evidence that a model has external validity or is suitable for a particular industry or clinical use.

## Frozen external evaluation

- `freeze_general` accepts a completed, locked general-engine configuration. A final-test configuration is accepted by default; a validation configuration requires explicit opt-in and is recorded as a newly trained artifact.
- The preprocessor and estimator are fitted once using development rows only. Final-test feature or target values are not used for that fit.
- External evaluation loads the frozen artifact, checks its manifest, model hash, schema, library versions, and evaluator/engine hashes, and performs no refit.
- External classification targets must belong to the frozen development classes. Probability columns are aligned to the recorded class order. Binary Brier score and reliability bins use the recorded positive class.
- Regression predictions are inverse-transformed before metrics are calculated. MAE and RMSE use the original target unit; MSE uses the squared target unit; R² is dimensionless.
- A result marked `selection_after_external=true` is not described as untouched external validation. Inspecting the external data and then changing the model requires a new experiment version.
- The external-evaluation screen requires an explicit data-origin designation. Its safe default is `unknown`; users may select `declared_synthetic` or `declared_observed` without editing JSON. This records a user declaration only and does not prove external validity or genuine prospective collection.
- The current frozen path supports the model and preprocessing components already implemented by the general CPU engine. It does not claim GPU execution, arbitrary custom estimators, or executable user-defined target transformations.
- Model serialization uses `skops`. Loading fails if the artifact contains a type outside the source-controlled trusted-type allow-list or if an artifact, schema, code, or version hash differs.

A successful software evaluation on an example or synthetic file is not proof of performance in an independent external cohort.

## Paired model comparison

Paired comparison requires raw predictions from both results and the same unique evaluation-unit IDs. Duplicate IDs, unequal ID sets, missing or non-finite outcomes/predictions, inconsistent observed outcomes, incompatible tasks, and incompatible target units are rejected. Aggregate reports or fold summary statistics are not converted into confidence intervals.

The reported difference is:

`right metric - left metric`

Accuracy is higher-is-better. MAE, MSE, and RMSE are lower-is-better, so their displayed improvement reverses the sign of the raw difference. MSE differences retain squared target units.

### Repeated observations from participants

When a mapping from evaluation-unit ID to participant ID is supplied, participants—not rows—are resampled with replacement. All rows from a selected participant remain together. The percentile interval is therefore a paired participant-cluster bootstrap interval for the requested metric. The output records paired-row count, independent-participant count, repeat count, seed, and the hash of paired IDs.

At least two independent participants are required for a bootstrap interval. With one participant, the application returns the paired point estimate, `uncertainty_available=false`, and no interval. It does not treat repeated rows from that participant as independent observations.

The metric remains row-weighted within each bootstrap sample. This estimates performance over observations while accounting for participant clustering; it is not the same estimand as an equal-participant-weighted mean of participant-specific metrics.

### One ordered series

For a single ordered series, the user must explicitly choose ordered-series mode and provide a block length of at least two. Contiguous moving blocks are sampled until the original series length is reached. At least two distinct moving-block start positions are required. The block length, seed, repeat count, paired-row count, and number of possible block starts are recorded.

The moving-block interval depends on the chosen block length and assumes that contiguous blocks adequately preserve relevant serial dependence. The software does not select a scientifically optimal block length or turn a single series into participant-level evidence.

Validation predictions concatenated across folds are accepted only when each evaluation-unit ID occurs exactly once. Repeated-CV predictions with duplicate IDs must first be represented with genuinely unique repeat/fold/unit identifiers or analyzed by a method designed for repeated predictions.

## Subgroup results

External subgroup tables report the subgroup sample size, point estimates, unavailable-metric reasons, and a small-group flag. They do not currently report subgroup confidence intervals because the external API does not require an independent participant or cluster identifier. The result records `independent_resampling_unit_not_provided` rather than creating row-IID intervals that could be invalid for repeated observations.

Subgroup analyses are descriptive unless their hypotheses, multiplicity handling, independent units, and evaluation design were specified separately. A small subgroup flag is an interpretation warning, not a statistical significance test.

## SleepWell multi-participant prospective wrapper

- Real prospective records use one append-only SQLite journal per hashed participant identifier.
- Declared-synthetic multi-participant files are split by participant and replayed independently through the unchanged single-participant policy.
- The wrapper removes any in-memory `records` setting before synthetic replay so it cannot override the participant-isolated split file.
- Aggregate metric means weight participants equally. Night counts and the number of participants contributing to each metric remain separately reported.
- Missing or unscored participants are listed and are not assigned a zero score.

Synthetic chronological playback is not genuine prospective validation. A stored historical report without raw predictions is not given a newly fabricated interval or recalculated metric.
