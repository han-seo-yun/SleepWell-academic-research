from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    analysis = args.analysis.resolve()
    protocol = json.loads((analysis.parent / "protocol.json").read_text(encoding="utf-8"))
    metrics = json.loads((analysis / "metrics.json").read_text(encoding="utf-8"))
    lock = json.loads((analysis / "selection_lock.json").read_text(encoding="utf-8"))
    frame = pd.read_csv(analysis / "participant_metrics.csv")
    failures = []

    if lock.get("test_consulted") is not False:
        failures.append("selection lock consulted Test")
    raw = lock["raw_validation"]["brier"]
    isotonic = lock["isotonic_validation"]["brier"]
    expected_selection = "validation_isotonic_occupancy_probability" if isotonic < raw - 1e-15 else "raw_hgb_probability"
    if lock["selected_before_test_process_s_scoring"] != expected_selection:
        failures.append("validation selection mismatch")
    if metrics["probability_selection"]["selected_before_test_process_s_scoring"] != expected_selection:
        failures.append("metrics selection mismatch")

    primary = frame[(frame.support == "primary") & np.isclose(frame.initial_s, 0.5)]
    secondary = frame[(frame.support == "secondary") & np.isclose(frame.initial_s, 0.5)]
    if primary.token.nunique() != metrics["counts"]["primary_people"]:
        failures.append("primary count mismatch")
    if secondary.token.nunique() != metrics["counts"]["secondary_people"]:
        failures.append("secondary count mismatch")
    if secondary.token.duplicated().any() or primary.token.duplicated().any():
        failures.append("duplicate participant/support rows")
    if not ((frame.psg_reference_s_20 >= 0) & (frame.psg_reference_s_20 <= 1)).all():
        failures.append("reference S outside bounds")
    if not (pd.to_datetime(frame.segment_end) < pd.to_datetime(frame.cutoff)).all():
        failures.append("cutoff chronology failure")
    if not (frame.gap_to_cutoff_h > 0).all():
        failures.append("nonpositive cutoff gap")

    selected_method = ("classifier_calibrated_probability" if expected_selection.startswith("validation_isotonic")
                       else "classifier_raw_probability")
    checks = {
        "primary_selected_s20_mean": (
            primary[f"{selected_method}_s_20_abs_error"].mean(),
            metrics["primary_single_contiguous"][selected_method]["s_20_abs_error"]["mean"],
        ),
        "secondary_diary_trajectory_mean": (
            secondary["diary_like_psg_first_to_last_sleep_trajectory_mae"].mean(),
            metrics["secondary_longest_contiguous"]["diary_like_psg_first_to_last_sleep"]["trajectory_mae"]["mean"],
        ),
        "primary_binary_end_mean": (
            primary["classifier_binary_s_end_abs_error"].mean(),
            metrics["primary_single_contiguous"]["classifier_binary"]["s_end_abs_error"]["mean"],
        ),
    }
    for name, (actual, expected) in checks.items():
        if not np.isclose(actual, expected, rtol=0, atol=1e-12):
            failures.append(name)

    hashes = json.loads((analysis / "source_hashes.json").read_text(encoding="utf-8"))
    for relative, expected in hashes.items():
        path = workspace / relative
        if not path.is_file() or sha256(path) != expected:
            failures.append(f"source hash mismatch: {relative}")
    expected_inputs = protocol["inputs"]
    psg_root = workspace / expected_inputs["psg_run"]
    if sha256(psg_root / "selected_model.joblib") != expected_inputs["classifier_sha256"]:
        failures.append("classifier input changed")
    if sha256(psg_root / "manifest.json") != expected_inputs["manifest_sha256"]:
        failures.append("manifest input changed")

    report = {
        "schema_version": 1,
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "rows": int(len(frame)),
        "primary_people": int(primary.token.nunique()),
        "secondary_people": int(secondary.token.nunique()),
        "selected_probability": expected_selection,
        "recomputed": {name: {"actual": float(actual), "expected": float(expected)}
                       for name, (actual, expected) in checks.items()},
    }
    output = analysis / "independent_verification.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
