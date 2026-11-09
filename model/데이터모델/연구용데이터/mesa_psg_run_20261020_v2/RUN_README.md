# MESA PSG run — 2026-10-20 v2

Status: complete, independently rechecked. This folder contains private participant-level research data. No automatic upload or service promotion.

- Official ACT/PSG anchor intersection: 1,036 participants.
- Included: 1,034; excluded: one stable empty XML and one with no eligible complete blocks.
- Split: Train 739 / Validation 144 / Test 151. Existing participant assignments retained.
- Selected before Test: `300s:activity_light_hgb` (60 boosting iterations), fitted on Train only.
- Test subject-macro balanced accuracy: 0.7914286718. Frozen ACT model on identical PSG Test rows: 0.7110279322.
- Retrospective classification at the end of each five-minute window. Not next-night prediction or evidence of recommendation benefit.

`protocol.json` records the frozen selection policy, code hashes, baseline hashes, environment and explicit exclusion policy. `manifest.json` contains private source/output hashes, splits and QC. `experiment_report.json` and `verification.json` contain the results and independent verification summary.

The preceding `mesa_psg_run_20261020` folder stopped before fitting or evaluation because an XML was empty. It is explicitly marked incomplete, with the original code snapshot preserved. It is not a prior Test evaluation.

Read-only input/model checks and regenerated verification summary:

```powershell
& 'E:/학술제/연구용데이터/.venv/Scripts/python.exe' -B -X utf8 'E:/학술제/연구용데이터/mesa_psg_run_20261020_v2/verify_full_run.py'
```

Do not run verification with `-O` or `PYTHONOPTIMIZE`; it rejects disabled assertions. The script does not retrain or change source/model/prediction files. It writes `verification.json` only after all checks pass.

The Korean report, model comparison table, figure and current interface document are in `E:/학술제/SleepWell-integrated/docs/`. The existing current ACT audit is stored here as `current_ACT_audit_private.json`. Its per-participant contents stay local.
