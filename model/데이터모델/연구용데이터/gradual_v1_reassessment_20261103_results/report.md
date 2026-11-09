# Frozen gradual_v1 retrospective reassessment

Status: `retrospective_reused_holdout`. This is a reused holdout analysis, not fresh external or prospective validation.

## Primary reused-Test results

| Cohort | Policy | Arm | Participant-weighted joint MAE, min (95% cluster CI) | Nights | Participants |
|---|---|---:|---:|---:|---:|
| CMI | legacy14 | data | 47.51 (42.00, 54.21) | 673 | 44 |
| CMI | legacy14 | hybrid | 49.23 (43.32, 55.67) | 673 | 44 |
| CMI | legacy14 | physical | 66.25 (59.92, 73.15) | 673 | 44 |
| CMI | legacy14 | recent_median | 53.54 (46.07, 61.60) | 673 | 44 |
| CMI | gradual_v1 | data | 47.49 (41.98, 53.80) | 673 | 44 |
| CMI | gradual_v1 | hybrid | 49.03 (43.13, 55.62) | 673 | 44 |
| CMI | gradual_v1 | physical | 60.96 (55.53, 67.28) | 673 | 44 |
| CMI | gradual_v1 | recent_median | 53.54 (45.78, 61.53) | 673 | 44 |
| MESA | legacy14 | data | 68.11 (63.40, 73.46) | 2175 | 315 |
| MESA | legacy14 | hybrid | 68.24 (63.73, 73.26) | 2175 | 315 |
| MESA | legacy14 | physical | 90.35 (84.54, 96.41) | 2175 | 315 |
| MESA | legacy14 | recent_median | 73.15 (67.93, 78.67) | 2175 | 315 |
| MESA | gradual_v1 | data | 68.11 (63.22, 73.25) | 2175 | 315 |
| MESA | gradual_v1 | hybrid | 68.24 (63.57, 73.04) | 2175 | 315 |
| MESA | gradual_v1 | physical | 83.82 (78.66, 89.81) | 2175 | 315 |
| MESA | gradual_v1 | recent_median | 73.15 (68.39, 78.67) | 2175 | 315 |

## Interpretation boundary

The policy comparison changes inference-time personalization only. Model parameters, cohort splits, and target rows are frozen.
Targets are cohort-specific event/ACT main-sleep timing, not PSG sleep stages or demonstrated clinical benefit.
Demographic generalization, true production delivery timestamps, prospective performance, and clinical utility remain unverified.
