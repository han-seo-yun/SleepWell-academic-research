# Frozen gradual_v1 retrospective reassessment

Status: `retrospective_reused_holdout`. This is a reused holdout analysis, not fresh external or prospective validation.

## Primary reused-Test results

| Cohort | Policy | Arm | Participant-weighted joint MAE, min (95% cluster CI) | Nights | Participants |
|---|---|---:|---:|---:|---:|
| CMI | legacy14 | data | 47.51 (41.88, 54.26) | 673 | 44 |
| CMI | legacy14 | hybrid | 49.23 (43.53, 56.43) | 673 | 44 |
| CMI | legacy14 | physical | 66.25 (60.19, 72.99) | 673 | 44 |
| CMI | legacy14 | recent_median | 53.54 (46.54, 62.06) | 673 | 44 |
| CMI | gradual_v1 | data | 47.49 (41.87, 54.23) | 673 | 44 |
| CMI | gradual_v1 | hybrid | 49.03 (43.35, 56.19) | 673 | 44 |
| CMI | gradual_v1 | physical | 60.96 (55.53, 67.28) | 673 | 44 |
| CMI | gradual_v1 | recent_median | 53.54 (46.54, 62.06) | 673 | 44 |
| MESA | legacy14 | data | 68.11 (63.61, 73.12) | 2175 | 315 |
| MESA | legacy14 | hybrid | 68.24 (63.73, 73.36) | 2175 | 315 |
| MESA | legacy14 | physical | 90.35 (85.08, 96.36) | 2175 | 315 |
| MESA | legacy14 | recent_median | 73.15 (68.41, 78.68) | 2175 | 315 |
| MESA | gradual_v1 | data | 68.11 (63.61, 73.12) | 2175 | 315 |
| MESA | gradual_v1 | hybrid | 68.24 (63.75, 73.36) | 2175 | 315 |
| MESA | gradual_v1 | physical | 83.82 (78.89, 89.44) | 2175 | 315 |
| MESA | gradual_v1 | recent_median | 73.15 (68.41, 78.68) | 2175 | 315 |

## Interpretation boundary

The policy comparison changes inference-time personalization only. Model parameters, cohort splits, and target rows are frozen.
Targets are cohort-specific event/ACT main-sleep timing, not PSG sleep stages or demonstrated clinical benefit.
Demographic generalization, true production delivery timestamps, prospective performance, and clinical utility remain unverified.
