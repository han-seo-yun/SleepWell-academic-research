# Repeated-measures evaluation

When multiple observations come from one participant, the number of rows must be distinguished from the number of independent participants. Uncertainty resampling should follow the independent unit in the study design.

## Data leakage

Imputation, encoding, scaling, and feature selection must be fitted within each training split. Final test data must not be reused for model selection.
