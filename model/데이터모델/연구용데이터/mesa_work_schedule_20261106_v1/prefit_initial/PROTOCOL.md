# MESA work-schedule evaluation — locked before fitting

Date: 2026-11-06 (Asia/Seoul). Retrospective predictive comparison, not a causal study or an independent external validation. The historical Test has already been used in previous research.

## Question and unchanged contracts

Does usual work schedule (wrksched5), optionally with extra workdays/month (extrahrs5), improve prediction of the next main-sleep onset and final wake time over the exact v8 gradual_v1 50/50 CMI/MESA hybrid ensemble? ACT-derived interval endpoints are not PSG stages, total sleep time, optimal sleep need, or recommendation benefit.

Use the existing MESA 1,525/299/315 participant Train/Validation/Test split without moving or dropping participants. Preserve existing sleep selection and time support. Reconstruct history chronologically, predicting before appending the target night. Available-at is assumed equal to wake, and the baseline questionnaire is assumed known at enrollment; exact questionnaire-to-actigraphy timing is unavailable. Fixed +09:00 serialization preserves source wall-clock fields, not measurement location.

Link original metadata many-to-one with the existing hashed mesaid contract. Keep unmatched and missing rows; never infer work schedule from student/worker/other. DATA33 retains its meaning and feature order. Do not include aggregate future sleep survey/actigraphy outcomes as inputs.

## Locked procedures

1. Replay both shipped Android JSON artifacts with their own Train priors, explicit gradual_v1 and the canonical Python provider. The deployed comparison is the mean of both constrained hybrid predictions.
2. Fit three MESA hybrid models with identical fixed HGB settings: absolute_error, max_leaf_nodes=7, min_samples_leaf=20, learning_rate=.05, max_iter=120, l2_regularization=10, early_stopping=False, seed=20261106. Weight each training participant equally (weights normalized to mean one).
3. Matched control: Hybrid39 only. Candidate S: Hybrid39 plus seven fixed one-hot work-schedule categories and an unknown flag. Candidate SE: S plus extra workdays/31 and a missing flag. Codes are nominal, never ordinal. Valid extra workdays are integers 0..31; missing/invalid values receive the Train-only median and explicit missing flag. No new dependencies.
4. Train-only median imputation and 1st/99th percentile clipping apply to the original 39 continuous features for all arms; new one-hot/extra-day fields are not quantile-clipped. Same prior, features, training rows, hyperparameters and weights across refitted arms. Unknown context is never imputed to a known schedule.
5. Main deployable comparison has a common fallback mask for all three refitted arms: known schedule with at least 50 distinct Train participants and age 60..80 inclusive. This matches the app's entirely supported age band; other age bands, rare schedules, missing/unmatched records retain the exact shipped prediction. Do not infer support for students or young adults. A supplementary unguarded comparison on all known schedules is descriptive only and cannot override the deployment gate.
6. Only the MESA contribution is replaced; the shipped CMI contribution and 50/50 weighting remain fixed. Apply the existing endpoint support projection before averaging.
7. Choose S or SE using the smallest Validation participant-average joint MAE; exact ties prefer S. Persist selection before Test metrics. Do not refit on Validation or adjust candidates after seeing results. Report both prespecified candidates even when they fail. Training all three candidates is the complete search budget.

## Metrics and gate

Primary: per-person mean of absolute onset and wake errors, then equal-person average, in minutes. Secondary: onset, wake, interval-length MAE; first-night, 1–2 prior-night, 3+ prior-night strata; schedule and age support. Use 5,000 paired participant bootstrap replicates (seed 20261106); preserve all nights within the person. Positive paired difference means comparator minus candidate, hence improvement. Confidence intervals are conditional retrospective uncertainty, not protection against historical Test reuse or future distribution shift.

The Validation-selected candidate is eligible for v8 only if all conditions hold:
- At least 1.0 minute primary improvement over the shipped ensemble on both Validation and Test (engineering threshold inherited from the recent project comparison; not a clinical threshold).
- Test paired 95% CI lower bound is above zero versus both the shipped ensemble and matched no-context refit.
- Validation joint MAE is no worse than the matched no-context refit.
- Neither endpoint nor first-night joint MAE worsens by more than 1.0 minute on either split.
- No supported schedule subgroup with at least 20 held-out participants worsens by more than 1.0 minute joint MAE on either split; smaller cells are reported as inconclusive and cannot establish their own benefit.
- Source integrity, no split leakage, causal history, missing fallback, feature/Android baseline parity and model checks pass.

If the gate fails, do not change v8 models or add a model-use input. Preserve linked research artifacts, exact procedure, all metrics and a readable report. A failure means the tested procedures have not demonstrated sufficient benefit, not that work schedule has no relationship with sleep. If it passes, integrate only the validated scope with optional inputs, exact missing fallback, versioned artifact, persistence/backward-compatibility and Kotlin parity tests, then build/lint/run.

## Evidence

- Local NSRR 0.8.0 variable dictionary and source CSV, SHA-256 recorded with the run.
- Official questionnaire: https://www.sleepdata.org/datasets/mesa/files/documentation/dataset-descriptions/MESAe5a113SleepQuestionnaire_20140617.pdf
- Group-aware evaluation and Train-only transforms: https://scikit-learn.org/stable/modules/cross_validation.html
- Scientific Agent Skills `statistical-analysis` informed the paired-participant analysis and complete reporting.

Raw inputs and existing results remain unchanged. Participant-level derived artifacts stay local in the new research folder and are not packaged into the app or uploaded.
