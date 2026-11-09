"""Package supplementary references and explicitly restricted MMASH features.

This does not claim a prospective/full-night MMASH performance evaluation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform

import numpy as np
import pandas as pd

from research_tabular import digest, dump


MMASH_FEATURES = [
    "hr_act_mean_30s", "hr_act_std_30s", "hr_act_median_30s", "hr_act_slope_30s",
    "steps_sum_30s", "vm_mean_30s", "vm_std_30s", "vm_max_30s",
    "axis1_std_30s", "axis2_std_30s", "axis3_std_30s",
    "posture_off_frac_30s", "posture_standing_frac_30s",
    "posture_sitting_frac_30s", "posture_lying_frac_30s",
]


def small_cohort_split(ids):
    """Freeze this initial small-cohort roster; do not repartition on updates."""
    ordered = sorted(set(ids), key=lambda s: hashlib.sha256(f"20261010:{s}".encode()).hexdigest())
    if len(ordered) < 7:
        raise ValueError("Need at least seven participants for three-way split")
    a, b = int(len(ordered)*.70), int(len(ordered)*.85)
    return {sid: "train" if i < a else "validation" if i < b else "test"
            for i, sid in enumerate(ordered)}


def prepare_mmash(d):
    required = ["subject_id", "epoch_start_sec", "epoch_end_sec", "row_quality_ok",
                "minutes_to_out_bed", *MMASH_FEATURES]
    if not set(required).issubset(d):
        raise ValueError("Missing MMASH columns")
    if d.duplicated(["subject_id", "epoch_start_sec"]).any():
        raise ValueError("Duplicate MMASH subject epochs")
    good = (d.row_quality_ok.eq(True) & d.minutes_to_out_bed.between(0, 240)
            & (d.epoch_end_sec-d.epoch_start_sec).eq(30))
    out = d.loc[good, required].copy()
    if not np.isfinite(out[MMASH_FEATURES].to_numpy(dtype=float)).all():
        raise ValueError("MMASH curated features must be finite; inspect rather than silently impute")
    out["subject_id"] = "mmash_"+out.subject_id.astype(str)
    out["split"] = out.subject_id.map(small_cohort_split(out.subject_id))
    out["label_source"] = "device_derived_out_bed_not_PSG"
    out["usage"] = "conditional_preselected_4h_window_only_not_full_night"
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    pre = a.workspace/"데이터셋/전처리데이터/MMASH_전처리/mmash_model_preout_30s_clean.csv.gz"
    d = prepare_mmash(pd.read_csv(pre))
    mm = a.output/"mmash_conditional"
    mm.mkdir()
    d.to_parquet(mm/"model_data.parquet", index=False)
    d[["subject_id", "split"]].drop_duplicates().to_csv(mm/"subject_splits.csv", index=False)
    for split in ["train", "validation", "test"]:
        d.loc[d.split == split].to_parquet(mm/f"{split}.parquet", index=False)
    mm_info = {"rows": len(d), "subjects": int(d.subject_id.nunique()),
               "split_policy": "initial hash-ranked fixed 70/15/15 roster; preserve saved roster on future updates",
               "split_rows": d.split.value_counts().to_dict(),
               "split_subjects": d.groupby("split").subject_id.nunique().to_dict(),
               "features": MMASH_FEATURES, "target": "minutes_to_out_bed",
               "metadata_not_features": ["subject_id", "epoch_start_sec", "epoch_end_sec", "split"],
               "source_sha256": digest(pre), "performance_evaluated": False,
               "restrictions": [
                   "Original inclusion is conditioned on future Out Bed (-4h to 0h). Not an unconditional prediction dataset.",
                   "RR interpolation uses both endpoints; all RR and HRV-derived features deliberately excluded here.",
                   "Device Out Bed is a weak target, not a PSG wake event.",
                   "No accuracy or wake-time forecasting claim until full recording evaluation."]}
    dump(mm/"manifest.json", mm_info)
    refs = a.output/"population_references"
    refs.mkdir()
    reference_info = {}
    for name in ["age_activity_sleep_reference", "age_steps_reference",
                 "age_light_activity_reference", "age_hourly_light_reference"]:
        path = a.workspace/f"보조데이터셋/processed/{name}.csv"
        table = pd.read_csv(path)
        table.to_parquet(refs/f"{name}.parquet", index=False)
        reference_info[name] = {"rows": len(table), "columns": list(table.columns),
                                "sha256": digest(path), "used_as_training_rows": False}
    candidate_path = a.workspace/"보조데이터셋/processed/candidate_age_priors.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["auto_apply"] = False
    dump(refs/"candidate_age_priors.json", candidate)
    summary = {"mmash": mm_info, "population_references": reference_info,
               "code_sha256": digest(__file__),
               "environment": {"python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__},
               "candidate_age_priors_source_sha256": digest(candidate_path),
               "auto_apply": False,
               "restrictions": ["Do not join people across NHANES cycles or external cohorts.",
                                "Full-population reference tables include held-out survey subjects; not training priors.",
                                "Actual train-only NHANES prior ablation is in tabular_run_20261010/nhanes.",
                                "Korean report retained as reference only, not converted into invented microdata."]}
    dump(a.output/"manifest.json", summary)
    print(json.dumps({"mmash_rows": len(d), "mmash_subjects": int(d.subject_id.nunique()),
                      "reference_rows": sum(t["rows"] for t in reference_info.values())}))


if __name__ == "__main__":
    main()
