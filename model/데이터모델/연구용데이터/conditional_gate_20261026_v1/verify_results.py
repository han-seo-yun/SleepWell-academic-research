"""Independent artifact verification; does not import experiment/gate scoring helpers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold, TimeSeriesSplit


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ids_digest(values):
    return hashlib.sha256(json.dumps(list(values), ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def close(actual, expected):
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-8)


def error_frame(rows):
    onset = (rows.predicted_bed_offset - rows.actual_bed_offset).abs()
    wake = (rows.predicted_wake_offset - rows.actual_wake_offset).abs()
    interval = ((rows.predicted_wake_offset - rows.predicted_bed_offset)
                - (rows.actual_wake_offset - rows.actual_bed_offset)).abs()
    return pd.DataFrame({"onset": onset.to_numpy(), "wake": wake.to_numpy(),
                         "interval": interval.to_numpy(), "joint": ((onset + wake) / 2).to_numpy(),
                         "subject_id": rows.subject_id.to_numpy()})


def complexity(arm):
    if arm == "baseline_population_median":
        return 0
    if arm.startswith("baseline_") or arm == "physical":
        return 1
    if arm.endswith("gate_depth1"):
        return 4
    if arm.endswith("gate_depth2"):
        return 5
    return 2 if arm.endswith("_data") else 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace, output = args.workspace.resolve(), args.output.resolve()
    study = Path(__file__).resolve().parent
    research = workspace / "데이터모델/연구용데이터"
    v1 = research / "next_night_run_20261020_v1/analysis"
    v2 = research / "robust_next_night_run_20261020_v2/analysis"
    tools = workspace / "기타/SleepWell-integrated/tools/model"
    web = workspace / "기타/SleepWell-integrated/web-app"
    lock = load(output / "selection_lock.json")
    verification = load(output / "verification.json")
    protocol = load(output / "protocol.json")
    before = load(output / "source_hashes_before.json")["files"]
    after = load(output / "source_hashes_after.json")["files"]
    assert before == after == lock["source_hashes"]
    for name, expected in before.items():
        if name.startswith("experiment/"):
            source = study / name.split("/", 1)[1]
        elif name.startswith("helpers/"):
            filename = name.split("/", 1)[1]
            source = (web if filename == "night_core.py" else tools) / filename
        elif name.startswith("inputs/"):
            _, version, relative = name.split("/", 2)
            source = (v1 if version == "v1" else v2) / relative
        elif name == "v2/protocol.json":
            source = v2 / "protocol.json"
        else:
            raise AssertionError(f"Unknown provenance source {name}")
        assert digest(source) == expected, name
    for group in (lock["data_hashes"], lock["model_hashes"], verification["artifact_hashes"]):
        for relative, expected in group.items():
            assert digest(output / relative) == expected, relative
    stages = verification["checks"]["selection_lock_written_before_test"]
    assert pd.Timestamp(lock["created_at_utc"]) <= pd.Timestamp(stages["lock_written_at_utc"]) <= pd.Timestamp(stages["phase2_started_at_utc"])
    assert (output / "selection_lock.json").stat().st_mtime_ns <= (output / "predictions.csv").stat().st_mtime_ns

    tables = {
        "validation": pd.read_csv(output / "validation_predictions.csv", dtype={"subject_id": "string"}),
        "test": pd.read_csv(output / "predictions.csv", dtype={"subject_id": "string"}),
    }
    oof = pd.read_csv(output / "oof_predictions.csv", dtype={"subject_id": "string"})
    assert not oof.duplicated(["cohort", "model_family", "row_id"]).any()
    folds = load(output / "fold_audits.json")["oof"]
    field_names = {"onset": "onset_mae_min", "wake": "wake_mae_min",
                   "interval": "interval_mae_min", "joint": "joint_mae_min"}
    recomputed = {}
    predictions = {}
    receipts = {}
    person_metrics_verified = 0
    paired_comparisons_verified = 0
    bootstrap_intervals_verified = 0
    for cohort in ("cmi", "mesa", "samsung"):
        source = pd.read_parquet(v1 / cohort / "episodes.parquet")
        training = source.loc[source.split.eq("train")].copy()
        if cohort == "samsung":
            training = training.sort_values(["cutoff", "row_id"], kind="mergesort")
            split_iterator = TimeSeriesSplit(n_splits=3, gap=1).split(training)
        else:
            split_iterator = GroupKFold(n_splits=3).split(training, groups=training.subject_id)
        all_held = []
        for number, (fit_ids, held_ids) in enumerate(split_iterator, 1):
            fit, held = training.iloc[fit_ids].copy(), training.iloc[held_ids].copy()
            if cohort == "samsung":
                cutoff = held.cutoff.min()
                fit = fit[(fit.cutoff < cutoff) & (fit.wake < cutoff) & (fit.observed_at < cutoff)]
                assert fit.observed_at.max() < cutoff and fit.wake.max() < cutoff
            else:
                assert not set(fit.subject_id) & set(held.subject_id)
            audit = next(item for item in folds[cohort]["folds"] if item["fold"] == number)
            assert audit["fit_row_ids_sha256"] == ids_digest(fit.row_id.tolist())
            assert audit["held_row_ids_sha256"] == ids_digest(held.row_id.tolist())
            expected_prior = fit.groupby("subject_id")[["bed_offset", "wake_offset"]].mean().mean()
            close([audit["prior"]["bed_offset"], audit["prior"]["wake_offset"]], expected_prior.to_numpy())
            assert audit["prior"]["n_nights"] == len(fit)
            for family in ("hgb", "catboost"):
                rows = oof[oof.cohort.eq(cohort) & oof.model_family.eq(family) & oof.fold.eq(number)]
                assert set(rows.row_id) == set(held.row_id)
                expected_labels = held.set_index("row_id").loc[rows.row_id, ["bed_offset", "wake_offset"]]
                close(rows[["actual_bed_offset", "actual_wake_offset"]], expected_labels)
            all_held.extend(held.row_id.tolist())
        assert len(set(all_held)) == len(all_held)
        if cohort != "samsung":
            assert set(all_held) == set(training.row_id)
        for family in ("hgb", "catboost"):
            rows = oof[oof.cohort.eq(cohort) & oof.model_family.eq(family)]
            weights = 1 / rows.groupby("subject_id").subject_id.transform("size").to_numpy(float)
            data = rows[["data_pred_bed_offset", "data_pred_wake_offset"]].to_numpy(float)
            physical = rows[["physical_pred_bed_offset", "physical_pred_wake_offset"]].to_numpy(float)
            actual = rows[["actual_bed_offset", "actual_wake_offset"]].to_numpy(float)
            grid = protocol["gate"]["grid"]
            losses = [np.average(np.abs(data + a * (physical - data) - actual).mean(axis=1), weights=weights) for a in grid]
            calibration = load(output / "models" / cohort / "calibration.json")
            alpha = calibration["fixed_weights"][family]
            assert alpha in grid
            close(losses[grid.index(alpha)], min(losses))
            assert rows.fixed_blend_weight.eq(alpha).all()
        recomputed[cohort] = {}
        predictions[cohort] = {}
        for split, table in tables.items():
            expected = source[source.split.eq(split)].set_index("row_id")
            cohort_rows = table[table.cohort.eq(cohort)]
            assert set(cohort_rows.arm) == set(protocol["arms"])
            assert len(cohort_rows) == len(expected) * len(protocol["arms"])
            assert not cohort_rows.duplicated(["arm", "row_id"]).any()
            recomputed[cohort][split] = {}
            predictions[cohort][split] = {}
            for arm in protocol["arms"]:
                rows = cohort_rows[cohort_rows.arm.eq(arm)].sort_values("row_id").reset_index(drop=True)
                assert set(rows.row_id) == set(expected.index)
                original = expected.loc[rows.row_id]
                assert rows.subject_id.astype(str).tolist() == original.subject_id.astype(str).tolist()
                assert np.array_equal(pd.to_datetime(rows.cutoff, utc=True).to_numpy(), pd.to_datetime(original.cutoff, utc=True).to_numpy())
                close(rows[["actual_bed_offset", "actual_wake_offset"]], original[["bed_offset", "wake_offset"]])
                errors = error_frame(rows)
                people = errors.groupby("subject_id").mean()
                means = people.mean() if cohort != "samsung" else errors.drop(columns="subject_id").mean()
                payload = load(output / "metrics" / cohort / arm / "metrics.json")
                for name, field in field_names.items():
                    close(means[name], payload[split][field])
                    if split == "validation":
                        close(means[name], lock["cohorts"][cohort]["validation_metrics"][arm][field])
                assert payload[split]["n_nights"] == len(expected)
                assert payload[split]["n_people"] == len(people)
                if split == "test":
                    assert len(payload["per_person"]["test"]) == len(people)
                    for person in payload["per_person"]["test"]:
                        for name, field in field_names.items():
                            close(people.loc[str(person["subject_id"]), name], person[field])
                        person_metrics_verified += 1
                recomputed[cohort][split][arm] = float(means["joint"])
                predictions[cohort][split][arm] = rows
            for family in ("hgb", "catboost"):
                data = predictions[cohort][split][family + "_data"][["predicted_bed_offset", "predicted_wake_offset"]].to_numpy()
                physical = predictions[cohort][split]["physical"][["predicted_bed_offset", "predicted_wake_offset"]].to_numpy()
                for suffix in ("fixed_blend", "gate_depth1", "gate_depth2"):
                    rows = predictions[cohort][split][family + "_" + suffix]
                    weights = rows.fusion_weight.to_numpy()
                    assert np.isfinite(weights).all() and ((weights >= 0) & (weights <= 1)).all()
                    close(rows[["predicted_bed_offset", "predicted_wake_offset"]], data + weights[:, None] * (physical - data))
        metrics = lock["cohorts"][cohort]["validation_metrics"]
        gates = [arm for arm in protocol["arms"] if "_gate_depth" in arm]
        controls = [arm for arm in protocol["arms"] if arm not in gates]
        key = lambda arm: (float(metrics[arm]["joint_mae_min"]), complexity(arm), arm)
        best_control, best_gate = min(controls, key=key), min(gates, key=key)
        gain = metrics[best_control]["joint_mae_min"] - metrics[best_gate]["joint_mae_min"]
        selected = best_gate if gain >= 5 else best_control
        choice = lock["cohorts"][cohort]["choice"]
        assert (choice["best_nonconditional"], choice["best_gate"], choice["selected_arm"]) == (best_control, best_gate, selected)
        close(gain, choice["gate_gain_min"])
        person_order = source[source.split.eq("test")].groupby("subject_id").size().index.astype(str).tolist()
        for arm in gates:
            payload = load(output / "metrics" / cohort / arm / "metrics.json")
            changed = error_frame(predictions[cohort]["test"][arm]).set_index("subject_id")
            for comparison in payload["paired_test_gains"].values():
                baseline = error_frame(predictions[cohort]["test"][comparison["baseline_arm"]]).set_index("subject_id")
                delta = baseline - changed
                grouped = delta.groupby(level=0).mean().loc[person_order]
                observed = grouped.mean().to_numpy() if cohort != "samsung" else delta.mean().to_numpy()
                for i, name in enumerate(field_names):
                    close(observed[i], comparison["metrics"][name]["error_reduction_min"])
                paired_comparisons_verified += 1
                if cohort != "samsung":
                    matrix = grouped.to_numpy()
                    rng = np.random.default_rng(20261020)
                    samples = matrix[rng.integers(0, len(matrix), size=(2000, len(matrix)))].mean(axis=1)
                    bounds = np.quantile(samples, [.025, .975], axis=0)
                    for i, name in enumerate(field_names):
                        close(bounds[:, i], comparison["metrics"][name]["ci95"])
                        bootstrap_intervals_verified += 1
                else:
                    assert all("ci95" not in metric for metric in comparison["metrics"].values())
        receipts[cohort] = {"selected_arm": selected, "best_validation_gate": best_gate,
                            "validation_gate_gain_over_best_control_min": gain,
                            "test_joint_mae_min": recomputed[cohort]["test"],
                            "oof_rows_per_family": len(all_held)}
    result = {"status": "independently_verified", "metric_groups": 108,
              "source_hashes": len(before), "model_and_calibration_hashes": len(lock["model_hashes"]),
              "test_prediction_rows": len(tables["test"]), "validation_prediction_rows": len(tables["validation"]),
              "oof_rows": len(oof), "person_metrics_verified": person_metrics_verified,
              "paired_comparisons_verified": paired_comparisons_verified,
              "bootstrap_intervals_verified": bootstrap_intervals_verified,
              "selection_derived_only_from_validation": True, "cohorts": receipts,
              "verification_script_sha256": digest(__file__)}
    with (output / "independent_verification.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
