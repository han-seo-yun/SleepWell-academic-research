# Walch PSG sleep/wake experiment

Research-only result; not a clinical or production model.
This v2 corrects HGB fixed-iteration behavior after v1 test inspection; its test result is an exploratory repeat, not an independent or pristine holdout.

## Frozen design

- Subject split: {'train': {'subjects': 22}, 'validation': {'subjects': 5}, 'test': {'subjects': 4}}
- Raw PSG annotations supply all targets; cached label columns are ignored.
- Features use only sensor observations inside the current non-overlapping bin.
- Predictions become available at bin end, so latency equals 30/60/120/300 seconds.
- No centered/future interpolation, PSG-relative clock, or label-derived feature is used.
- Every model and resolution is scored on the same intersection of eligible native 30-second rows.
- HGB uses exactly 60 boosting iterations with early stopping disabled.

## Coverage

- Raw scored PSG epochs: 26,773 (sleep rate 0.909)
- Complete sensor epochs: 25,497 (95.2% of scored PSG)
- Common cross-resolution support: 24,654 (92.1%; sleep rate 0.920)

## Validation-selected winner

- Model: `activity_logistic` at 120 seconds
- Validation subject-macro balanced accuracy: 0.7735
- Held-out test subject-macro balanced accuracy: 0.7363
- Test macro F1: 0.6432; wake specificity: 0.5993; sleep sensitivity: 0.8699
- Winner minus dummy paired-subject difference: 0.2363, 95% bootstrap CI [0.2007, 0.2653] (4 subjects; descriptive)

All test candidates are descriptive only; the winner was frozen from validation results first.
HGB boosting iterations are optimization steps, distinct from the sensor epoch duration.

## Method reference

Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents. arXiv:2609.00065. https://doi.org/10.48550/arXiv.2609.00065
