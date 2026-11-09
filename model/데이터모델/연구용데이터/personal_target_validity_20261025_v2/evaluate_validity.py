#!/usr/bin/env python3
"""Post-hoc target-validity audit over frozen v1 personal predictions.

Reads v1 artifacts without modifying them, delegates calendar baselines and
centered diagnostics to existing helpers, and scores only preserved test nights.
No model/survey fitting, raw-sensor ingestion, deployment, or download occurs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence

VERSION = "2026-10-25-v2"
V1_DIR = "personal_watch_survey_20261025_v1"
V2_DIR = "personal_target_validity_20261025_v2"
REFERENCE_ARM = "baseline_calendar_14d_mean"
LEGACY_RECENT7_ARM = "baseline_trailing_7_median"
LOW_N, CENTERED_MIN_N, EPS = 30, 3, 1e-8
EXTERNAL_COMMIT = "627b1006a95b698a0f9f74ff19d499a8a0ef5a60"
EXTERNAL_REPO = "https://github.com/Reaper2403/passive-sensing-target-validity"
EXTERNAL_SOURCES = {
    "README": f"{EXTERNAL_REPO}/blob/{EXTERNAL_COMMIT}/README.md",
    "report_abstract_and_scope": f"{EXTERNAL_REPO}/blob/{EXTERNAL_COMMIT}/report/main.tex",
    "claim_source_map": f"{EXTERNAL_REPO}/blob/{EXTERNAL_COMMIT}/report/CLAIM_SOURCE_MAP.md",
    "personalization_contract": f"{EXTERNAL_REPO}/blob/{EXTERNAL_COMMIT}/experiments/personalization/contract.md",
}
RESULTS = ("heldout_predictions.csv", "participant_metrics.csv", "target_validity_report.json",
           "claim_source_map.json", "verification.json")

# Public names deliberately describe the participant TRAIN prior and exact-weekday
# contract; the imported helper retains older internal keys.
CALENDAR_ARMS = {
    "population": "baseline_participant_train_prior",
    "mean7": "baseline_calendar_7d_mean",
    "mean14": REFERENCE_ARM,
    "weighted28": "baseline_calendar_28d_recency_weighted",
    "weekday": "baseline_exact_weekday_28d",
}
NEW_ARM_META = {
    "baseline_participant_train_prior": {"family": "same_channel_history_baseline",
        "description": "Participant TRAIN-only mean onset/wake; not a population estimate."},
    "baseline_calendar_7d_mean": {"family": "same_channel_history_baseline",
        "description": "Mean within 7 calendar days; participant TRAIN prior fallback."},
    REFERENCE_ARM: {"family": "same_channel_history_baseline",
        "description": "Predesignated 14-calendar-day mean; participant TRAIN prior fallback."},
    "baseline_calendar_28d_recency_weighted": {"family": "same_channel_history_baseline",
        "description": "28-calendar-day mean with 14-day half-life; participant TRAIN prior fallback."},
    "baseline_exact_weekday_28d": {"family": "same_channel_history_baseline",
        "description": "Exact weekday within 28 days (not weekday/weekend); 14-day mean fallback."},
}
REQUIRED = {"participant_id", "target_id", "split", "cutoff", "sleep_start", "sleep_end",
            "observed_at", "arm", "predicted_onset_offset_min", "predicted_wake_offset_min",
            "predicted_interval_min", "actual_onset_offset_min", "actual_wake_offset_min",
            "actual_interval_min", "onset_abs_error_min", "wake_abs_error_min",
            "interval_abs_error_min", "joint_abs_error_min", "history_count",
            "prior_bed_offset_min", "prior_wake_offset_min"}
PRED_FIELDS = ("participant_id", "target_id", "split", "cutoff", "sleep_start", "sleep_end",
               "observed_at", "arm", "predicted_onset_offset_min", "predicted_wake_offset_min",
               "predicted_interval_min", "actual_onset_offset_min", "actual_wake_offset_min",
               "actual_interval_min", "onset_abs_error_min", "wake_abs_error_min",
               "interval_abs_error_min", "joint_abs_error_min", "complete_pre_cutoff_history_n",
               "recent_14_calendar_day_history_n", "history_gap_days", "weekday_supported",
               "participant_train_prior_onset_offset_min", "participant_train_prior_wake_offset_min",
               "prediction_source")
METRIC_FIELDS = ("participant_id", "arm", "n_nights", "onset_mae_min", "wake_mae_min",
                 "interval_mae_min", "joint_onset_wake_mae_min", "low_n", "low_n_threshold",
                 "short_for_centered_evaluation", "centered_min_n", "constant_onset_target",
                 "constant_wake_target", "constant_interval_target", "status")
ERRORS = {"onset": "onset_abs_error_min", "wake": "wake_abs_error_min",
          "interval": "interval_abs_error_min", "joint_onset_wake": "joint_abs_error_min"}


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def rel(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def csv_text(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})
    return stream.getvalue()


def number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} is not finite")
    return result


def close(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=EPS, rel_tol=0)


def paths_for(workspace: Path) -> dict[str, Path]:
    v1 = workspace / "데이터모델" / "연구용데이터" / V1_DIR
    paths = {
        "v1_evaluate": v1 / "evaluate.py", "v1_predictions": v1 / "predictions.csv",
        "v1_metrics": v1 / "metrics.json", "v1_verification": v1 / "verification.json",
        "target_validity_helpers": workspace / "기타" / "SleepWell-integrated" / "tools" /
            "model" / "target_validity_experiments.py",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing required input(s): " + ", ".join(missing))
    return paths


def input_hashes(paths: Mapping[str, Path], workspace: Path) -> dict[str, dict[str, Any]]:
    return {name: {"path": rel(path, workspace), "bytes": path.stat().st_size, "sha256": sha(path)}
            for name, path in sorted(paths.items())}


def assert_same_hashes(before: Mapping[str, Any], after: Mapping[str, Any], stage: str) -> None:
    changed = [key for key in before if key not in after or before[key]["sha256"] != after[key]["sha256"]]
    if set(before) != set(after) or changed:
        raise RuntimeError(f"Inputs changed during {stage}: {changed}")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def group(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row[field])].append(row)
    return dict(result)


def load_v1(path: Path, metrics: Mapping[str, Any], verification: Mapping[str, Any]):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Missing v1 prediction columns: {sorted(missing)}")
        rows = [dict(row) for row in reader]
    if not rows or len(rows) != int(verification.get("prediction_rows", -1)):
        raise ValueError("v1 prediction row count does not match verification")

    targets: dict[tuple[str, str], dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    priors_seen: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"bed_offset": [], "wake_offset": []})
    arms: set[str] = set()
    for row in rows:
        pid, tid, arm, split = (row[key].strip() for key in ("participant_id", "target_id", "arm", "split"))
        if split not in {"train", "validation", "test"} or (pid, tid, arm) in seen:
            raise ValueError(f"Invalid split or duplicate prediction: {(pid, tid, arm)}")
        seen.add((pid, tid, arm)); arms.add(arm)
        po, pw = number(row["predicted_onset_offset_min"], "predicted onset"), number(row["predicted_wake_offset_min"], "predicted wake")
        ao, aw = number(row["actual_onset_offset_min"], "actual onset"), number(row["actual_wake_offset_min"], "actual wake")
        pi, ai = number(row["predicted_interval_min"], "predicted interval"), number(row["actual_interval_min"], "actual interval")
        if not close(pi, pw - po) or not close(ai, aw - ao):
            raise ValueError(f"Interval identity failed: {(pid, tid, arm)}")
        expected = (abs(po - ao), abs(pw - aw), abs(pi - ai), (abs(po - ao) + abs(pw - aw)) / 2)
        stored = tuple(number(row[key], key) for key in ("onset_abs_error_min", "wake_abs_error_min",
                                                          "interval_abs_error_min", "joint_abs_error_min"))
        if any(not close(a, b) for a, b in zip(expected, stored)):
            raise ValueError(f"Stored error arithmetic failed: {(pid, tid, arm)}")
        target = {"participant_id": pid, "target_id": tid, "split": split, "cutoff": row["cutoff"],
                  "sleep_start": row["sleep_start"], "sleep_end": row["sleep_end"],
                  "observed_at": row["observed_at"], "actual_onset_offset_min": ao,
                  "actual_wake_offset_min": aw, "actual_interval_min": ai}
        old = targets.get((pid, tid))
        if old is not None and old != target:
            raise ValueError(f"Target fields changed across arms: {(pid, tid)}")
        targets[(pid, tid)] = target
        priors_seen[pid]["bed_offset"].append(number(row["prior_bed_offset_min"], "TRAIN prior onset"))
        priors_seen[pid]["wake_offset"].append(number(row["prior_wake_offset_min"], "TRAIN prior wake"))

    ordered = sorted(targets.values(), key=lambda r: (r["participant_id"], r["cutoff"], r["target_id"]))
    priors: dict[str, dict[str, float]] = {}
    for pid, participant_rows in sorted(group(ordered, "participant_id").items()):
        train = [row for row in participant_rows if row["split"] == "train"]
        if not train:
            raise ValueError(f"No TRAIN targets for {pid}")
        prior = {"bed_offset": statistics.mean(row["actual_onset_offset_min"] for row in train),
                 "wake_offset": statistics.mean(row["actual_wake_offset_min"] for row in train)}
        for key in prior:
            values = priors_seen[pid][key]
            if max(values) - min(values) > EPS or not close(values[0], prior[key]):
                raise ValueError(f"Participant TRAIN prior mismatch for {pid}")
        priors[pid] = prior

    if len(ordered) != int(verification.get("unique_target_nights", -1)):
        raise ValueError("v1 target count mismatch")
    if metrics.get("protocol", {}).get("primary_evaluation_split") != "test" or set(metrics.get("arms", {})) != arms:
        raise ValueError("v1 evaluation/arm contract mismatch")
    test_counts = Counter(row["participant_id"] for row in ordered if row["split"] == "test")
    verified = {str(k): int(v) for k, v in verification.get("heldout_nights_by_person", {}).items()}
    if dict(test_counts) != verified or sum(test_counts.values()) != int(verification.get("heldout_test_nights", -1)):
        raise ValueError("v1 held-out counts mismatch")
    return rows, ordered, priors, arms


def survey_gate(metrics: Mapping[str, Any], verification: Mapping[str, Any]) -> dict[str, Any]:
    try:
        n = int(metrics["survey_incorporation"]["strict_watch_join_rows"])
        verified = int(verification["strictly_available_linked_survey_nights"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Missing survey feasibility evidence") from exc
    if n != verified:
        raise ValueError("Survey feasibility counts disagree")
    stopped = n == 0
    return {
        "evaluated_before_any_survey_fit": True, "strictly_linked_pre20_rows": n,
        "gate_passed": not stopped,
        "status": ("stopped_before_fit_no_strictly_linked_pre20_rows" if stopped
                   else "eligible_rows_present_but_survey_fitting_out_of_scope"),
        "survey_model_fit_attempted": False, "effect_estimate": None, "null_effect_claimed": False,
        "interpretation": ("Zero eligible linked rows makes the effect non-estimable; it is not a null-effect result."
                           if stopped else "Fitting remains outside this frozen-prediction follow-up."),
    }


def load_helpers(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_sleepwell_target_validity_helpers", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if not callable(getattr(module, "baseline_predictions", None)) or not callable(getattr(module, "within_person_diagnostic", None)):
        raise ImportError("Required target-validity helpers are absent")
    return module


def prediction(target: Mapping[str, Any], arm: str, onset: float, wake: float,
               prior: Mapping[str, float], source: str, history_n: int,
               recent14_n: int | None = None, gap: float | None = None,
               weekday_supported: bool | None = None) -> dict[str, Any]:
    onset, wake = number(onset, "predicted onset"), number(wake, "predicted wake")
    interval, ao, aw, ai = wake - onset, float(target["actual_onset_offset_min"]), float(target["actual_wake_offset_min"]), float(target["actual_interval_min"])
    oe, we = abs(onset - ao), abs(wake - aw)
    return {**{key: target[key] for key in ("participant_id", "target_id", "split", "cutoff", "sleep_start", "sleep_end", "observed_at")},
            "arm": arm, "predicted_onset_offset_min": onset, "predicted_wake_offset_min": wake,
            "predicted_interval_min": interval, "actual_onset_offset_min": ao,
            "actual_wake_offset_min": aw, "actual_interval_min": ai,
            "onset_abs_error_min": oe, "wake_abs_error_min": we,
            "interval_abs_error_min": abs(interval - ai), "joint_abs_error_min": (oe + we) / 2,
            "complete_pre_cutoff_history_n": int(history_n),
            "recent_14_calendar_day_history_n": recent14_n, "history_gap_days": gap,
            "weekday_supported": weekday_supported,
            "participant_train_prior_onset_offset_min": float(prior["bed_offset"]),
            "participant_train_prior_wake_offset_min": float(prior["wake_offset"]),
            "prediction_source": source}


def reuse_test(rows: Sequence[Mapping[str, str]], targets: Sequence[Mapping[str, Any]],
               priors: Mapping[str, Mapping[str, float]]) -> list[dict[str, Any]]:
    lookup = {(row["participant_id"], row["target_id"]): row for row in targets}
    output = []
    for row in rows:
        if row["split"] != "test":
            continue
        target = lookup[(row["participant_id"], row["target_id"])]
        item = prediction(target, row["arm"], number(row["predicted_onset_offset_min"], "onset"),
                          number(row["predicted_wake_offset_min"], "wake"), priors[row["participant_id"]],
                          "reused_v1_predictions_no_refit", int(float(row["history_count"])))
        if any(not close(item[field], number(row[field], field)) for field in ERRORS.values()):
            raise ValueError("Reused v1 error changed")
        output.append(item)
    return output


def calendar_test(targets: Sequence[Mapping[str, Any]], priors: Mapping[str, Mapping[str, float]],
                  helper: ModuleType) -> list[dict[str, Any]]:
    """Pass complete participant histories; imported code applies strict cutoffs/windows."""
    output = []
    for pid, raw in sorted(group(targets, "participant_id").items()):
        rows = sorted(raw, key=lambda row: (row["cutoff"], row["target_id"]))
        records = [{"id": row["target_id"], "source": "research", "bed": row["sleep_start"],
                    "wake": row["sleep_end"], "observed_at": row["observed_at"]} for row in rows]
        for target in rows:
            if target["split"] != "test":
                continue
            estimates, diag = helper.baseline_predictions(records, target["cutoff"], priors[pid], rows[0]["cutoff"])
            if not set(CALENDAR_ARMS).issubset(estimates):
                raise ValueError("Imported helper omitted a baseline")
            for internal, arm in CALENDAR_ARMS.items():
                onset, wake = estimates[internal]
                output.append(prediction(target, arm, onset, wake, priors[pid],
                    "computed_by_existing_baseline_predictions_helper", int(diag["prior_records"]),
                    int(diag["recent14_records"]), None if diag["gap_days"] is None else number(diag["gap_days"], "gap"),
                    bool(diag["weekday_supported"])))
    return output


def merge_predictions(*collections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output, seen = [], set()
    for row in (item for rows in collections for item in rows):
        key = (row["participant_id"], row["target_id"], row["arm"])
        if row["split"] != "test" or key in seen:
            raise ValueError(f"Non-test or duplicate output: {key}")
        seen.add(key); output.append(dict(row))
    return sorted(output, key=lambda row: (row["participant_id"], row["cutoff"], row["arm"]))


def constant(values: Sequence[float]) -> bool:
    if not values:
        return True
    center = statistics.mean(values)
    return sum((value - center) ** 2 for value in values) <= EPS


def target_flags(targets: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output = {}
    for pid, rows in sorted(group([row for row in targets if row["split"] == "test"], "participant_id").items()):
        output[pid] = {"n_nights": len(rows), "low_n": len(rows) < LOW_N, "low_n_threshold": LOW_N,
            "short_for_centered_evaluation": len(rows) < CENTERED_MIN_N, "centered_min_n": CENTERED_MIN_N,
            "constant_onset_target": constant([float(row["actual_onset_offset_min"]) for row in rows]),
            "constant_wake_target": constant([float(row["actual_wake_offset_min"]) for row in rows]),
            "constant_interval_target": constant([float(row["actual_interval_min"]) for row in rows])}
    return output


def participant_metrics(predictions: Sequence[Mapping[str, Any]], flags: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in predictions:
        if row["split"] != "test":
            raise ValueError("Metrics must be test-only")
        grouped[(row["participant_id"], row["arm"])].append(row)
    output = []
    for (pid, arm), rows in sorted(grouped.items()):
        output.append({"participant_id": pid, "arm": arm, "n_nights": len(rows),
            "onset_mae_min": statistics.mean(row["onset_abs_error_min"] for row in rows),
            "wake_mae_min": statistics.mean(row["wake_abs_error_min"] for row in rows),
            "interval_mae_min": statistics.mean(row["interval_abs_error_min"] for row in rows),
            "joint_onset_wake_mae_min": statistics.mean(row["joint_abs_error_min"] for row in rows),
            **flags[pid], "status": "descriptive_heldout_test_only"})
    return output


def macro_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output = {}
    for arm, values in sorted(group(rows, "arm").items()):
        output[arm] = {"n_people": len(values),
            **{field: statistics.mean(float(row[field]) for row in values)
               for field in ("onset_mae_min", "wake_mae_min", "interval_mae_min", "joint_onset_wake_mae_min")},
            "weighting": "equal participant", "status": "descriptive_heldout_test_only"}
    return output


def paired_skill(predictions: Sequence[Mapping[str, Any]], reference_arm: str = REFERENCE_ARM):
    indexed: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = defaultdict(dict)
    for row in predictions:
        key = (row["participant_id"], row["target_id"])
        if key in indexed[row["arm"]]:
            raise ValueError("Duplicate pair key")
        indexed[row["arm"]][key] = row
    if reference_arm not in indexed:
        raise ValueError("Predesignated reference absent")
    reference, output = indexed[reference_arm], {}
    for arm, candidate in sorted(indexed.items()):
        common = sorted(set(candidate) & set(reference)); people = sorted({key[0] for key in common})
        metrics = {}
        for label, field in ERRORS.items():
            candidate_person, reference_person = [], []
            for pid in people:
                keys = [key for key in common if key[0] == pid]
                for key in keys:
                    if any(not close(float(candidate[key][actual]), float(reference[key][actual]))
                           for actual in ("actual_onset_offset_min", "actual_wake_offset_min", "actual_interval_min")):
                        raise ValueError(f"Paired actual mismatch: {key}")
                candidate_person.append(statistics.mean(float(candidate[key][field]) for key in keys))
                reference_person.append(statistics.mean(float(reference[key][field]) for key in keys))
            cm = statistics.mean(candidate_person) if candidate_person else None
            rm = statistics.mean(reference_person) if reference_person else None
            metrics[label] = {"candidate_macro_equal_person_mae_min": cm,
                "reference_macro_equal_person_mae_min": rm,
                "gain_reference_minus_candidate_min": None if cm is None else rm - cm,
                "skill_one_minus_candidate_over_reference": None if rm is None or rm <= 0 else 1 - cm / rm}
        output[arm] = {"reference_arm": reference_arm, "pair_key": ["participant_id", "target_id"],
            "same_night_pairing": True, "n_pairs": len(common), "n_people": len(people),
            "candidate_only_n": len(set(candidate) - set(reference)),
            "reference_only_n": len(set(reference) - set(candidate)), "metrics": metrics,
            "status": "descriptive_only_no_confidence_interval"}
    return output


def centered_diagnostics(predictions: Sequence[Mapping[str, Any]], helper: ModuleType):
    """Use held-out means only inside the imported post-prediction diagnostic."""
    output = {}
    for arm, raw in sorted(group(predictions, "arm").items()):
        rows = sorted(raw, key=lambda row: (row["participant_id"], row["target_id"]))
        frame = helper.pd.DataFrame({"subject_id": [row["participant_id"] for row in rows],
            "bed_offset": [row["actual_onset_offset_min"] for row in rows],
            "wake_offset": [row["actual_wake_offset_min"] for row in rows]})
        predicted = helper.np.asarray([[row["predicted_onset_offset_min"], row["predicted_wake_offset_min"]] for row in rows])
        boundary = helper.within_person_diagnostic(frame, predicted)
        interval_frame = helper.pd.DataFrame({"subject_id": frame.subject_id,
            "bed_offset": [row["actual_interval_min"] for row in rows],
            "wake_offset": [row["actual_interval_min"] for row in rows]})
        interval = helper.np.asarray([row["predicted_interval_min"] for row in rows])
        interval_result = helper.within_person_diagnostic(interval_frame, helper.np.column_stack((interval, interval)))
        output[arm] = {"onset": boundary["bed_offset"], "wake": boundary["wake_offset"],
            "interval": interval_result["bed_offset"], "diagnostic_only": True,
            "heldout_participant_means_used_as_prediction_inputs": False,
            "interpretation": "Centered R² separates within-person variation from level differences."}
    return output


def report(targets, arms, flags, macro, paired, within, gate):
    test = [row for row in targets if row["split"] == "test"]
    people = sorted({row["participant_id"] for row in test})
    return {
        "schema_version": 2, "artifact_type": "sleepwell_personal_target_validity_followup", "version": VERSION,
        "analysis_scope": {"design_status": "post_hoc_reanalysis_of_already_inspected_personal_test",
            "independently_prespecified_confirmation": False, "heldout_split_only_for_metrics": True,
            "n_people": len(people), "n_heldout_nights": len(test), "participants": people,
            "new_model_training": False, "model_or_policy_selection_on_test": False,
            "deployment_change": False, "dataset_download": False},
        "target": {"decision": "local 20:00 before the modeled night",
            "onset": "consumer-watch main interval start", "wake": "consumer-watch main interval end",
            "interval": "wake boundary minus onset boundary", "not_psg_tst": True,
            "not_sleep_opportunity": True, "not_optimal_sleep_need": True},
        "protocol": {"ids_and_splits": "Preserved exactly from v1; no resplitting or new IDs.",
            "history": "Complete cross-split participant history; imported helper requires wake and observed_at strictly before cutoff.",
            "prior": "participant TRAIN-only mean onset and wake offsets", "primary_reference": REFERENCE_ARM,
            "primary_reference_definition": "14 calendar days of completed same-channel targets",
            "recent_7_observations": {"arm": LEGACY_RECENT7_ARM,
                "definition": "Median of last seven observations regardless of calendar span.",
                "distinct_from_calendar_7_days": True},
            "weekday_baseline": "Exact next-night weekday within 28 days, not weekday/weekend; 14-day fallback.",
            "pairing": "Identical participant_id + target_id nights, then equal-person macro averaging."},
        "arms": dict(sorted(arms.items())), "participant_target_flags": dict(sorted(flags.items())),
        "macro_equal_person_metrics": dict(sorted(macro.items())),
        "paired_skill_vs_predesignated_reference": dict(sorted(paired.items())),
        "within_person_centered_diagnostic": dict(sorted(within.items())),
        "interval_target_sensitivity": {"role": "Sensitivity only; not PSG TST, opportunity, or optimal need.",
            "macro_equal_person_interval_mae_min": {arm: value["interval_mae_min"] for arm, value in macro.items()},
            "paired_skill_vs_14_calendar_day_mean": {arm: value["metrics"]["interval"] for arm, value in paired.items()}},
        "survey_feasibility": dict(gate),
        "uncertainty": {"participant_count": len(people), "bootstrap_performed": False,
            "confidence_interval": None, "status": "insufficient_participants_for_meaningful_confidence_interval",
            "interpretation": "Three participants cannot support meaningful participant-bootstrap inference; descriptive only."},
        "measurement_and_availability_boundaries": {
            "watch_available_at_wake_assumption": "Assumed available at recorded wake; no prospective receipt log.",
            "latest_snapshot_revision_uncertainty": "Latest export snapshot used; edit/backfill/revision timing unavailable.",
            "production_daily_personalization_validation": "unavailable_not_evaluated",
            "physiological_constant_validation": "unavailable_not_evaluated",
            "survey_features_fabricated": False, "raw_heart_rate_incorporated": False,
            "raw_activity_incorporated": False},
        "methodological_reference": {"repository": EXTERNAL_REPO, "commit": EXTERNAL_COMMIT,
            "sources": EXTERNAL_SOURCES, "role": "methodological principles only",
            "principles_applied": ["strong same-channel history baselines",
                "between-person versus centered within-person separation", "target/metric validity audit",
                "outcome-blind feasibility stop before personalization fitting", "claim-to-artifact mapping"],
            "external_code_data_coefficients_numbers_or_gates_used": False},
        "limitations": ["Post-hoc reuse of an already inspected personal test; not independent confirmation.",
            "Three people and short held-out series support descriptive results only.",
            "Watch boundaries do not validate stages, TST, optimal need, or causal benefit.",
            "Held-out means enter only the diagnostic, never prediction inputs.",
            "Zero eligible survey links stopped fitting and is not a null-effect result.",
            "Production daily personalization and physiological constants remain unvalidated."]}


def claim_map(artifact_hashes: Mapping[str, str], inputs: Mapping[str, Any]) -> dict[str, Any]:
    source = lambda path, pointer: {"path": path, "json_pointer": pointer}
    return {"schema_version": 1, "artifact_type": "sleepwell_claim_source_map", "version": VERSION,
        "scope": "Local SleepWell computations only; external work supplies principles, not result values.",
        "computed_artifact_sha256": dict(sorted(artifact_hashes.items())),
        "input_lineage": {name: {"path": item["path"], "sha256": item["sha256"]} for name, item in sorted(inputs.items())},
        "claims": [
            {"id": "heldout_mae", "claim": "Per-person onset/wake/interval MAE and equal-person macro use test only.",
             "sources": [{"path": "participant_metrics.csv", "columns": list(METRIC_FIELDS)},
                         source("target_validity_report.json", "/macro_equal_person_metrics")]},
            {"id": "paired_skill", "claim": "Skill is paired on identical nights against the predesignated 14-day mean.",
             "sources": [{"path": "heldout_predictions.csv", "key": ["participant_id", "target_id", "arm"]},
                         source("target_validity_report.json", "/paired_skill_vs_predesignated_reference")]},
            {"id": "within_person", "claim": "Centered evaluation is diagnostic only with short/constant flags.",
             "sources": [source("target_validity_report.json", "/within_person_centered_diagnostic"),
                         source("target_validity_report.json", "/participant_target_flags")]},
            {"id": "survey_stop", "claim": "Zero strictly linked pre-20 rows means no fit, not a null effect.",
             "sources": [source("target_validity_report.json", "/survey_feasibility"),
                         source(inputs["v1_metrics"]["path"], "/survey_incorporation/strict_watch_join_rows"),
                         source(inputs["v1_verification"]["path"], "/strictly_available_linked_survey_nights")]},
            {"id": "scope", "claim": "This is descriptive post-hoc reanalysis with availability/revision limits.",
             "sources": [source("target_validity_report.json", "/analysis_scope"),
                         source("target_validity_report.json", "/uncertainty"),
                         source("target_validity_report.json", "/measurement_and_availability_boundaries")]},
        ], "external_methodology": {"repository": EXTERNAL_REPO, "commit": EXTERNAL_COMMIT,
            "sources": EXTERNAL_SOURCES, "coefficients_numbers_or_transfer_gates_imported": False}}


def ensure_absent(output: Path) -> None:
    conflicts = [output / name for name in RESULTS if (output / name).exists()]
    if conflicts:
        raise FileExistsError("Refusing to overwrite existing result(s): " + ", ".join(path.name for path in conflicts))


def write_x(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)


def default_output(workspace: Path) -> Path:
    return workspace / "데이터모델" / "연구용데이터" / V2_DIR


def run(workspace: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True); ensure_absent(output)
    paths = paths_for(workspace); before = input_hashes(paths, workspace)
    metrics, verification_v1 = load_json(paths["v1_metrics"]), load_json(paths["v1_verification"])
    gate = survey_gate(metrics, verification_v1)  # outcome-blind gate before any fitting/prediction work
    raw, targets, priors, old_arms = load_v1(paths["v1_predictions"], metrics, verification_v1)
    helper = load_helpers(paths["target_validity_helpers"])
    predictions = merge_predictions(reuse_test(raw, targets, priors), calendar_test(targets, priors, helper))
    test_keys = {(row["participant_id"], row["target_id"]) for row in targets if row["split"] == "test"}
    for arm, rows in group(predictions, "arm").items():
        if {(row["participant_id"], row["target_id"]) for row in rows} != test_keys:
            raise ValueError(f"Held-out IDs differ for {arm}")
    flags = target_flags(targets); people_metrics = participant_metrics(predictions, flags)
    macro = macro_metrics(people_metrics); paired = paired_skill(predictions)
    within = centered_diagnostics(predictions, helper)
    arms = dict(metrics["arms"])
    if set(arms) & set(NEW_ARM_META):
        raise ValueError("New arm name collides with v1")
    arms.update(NEW_ARM_META)
    report_value = report(targets, arms, flags, macro, paired, within, gate)
    payloads = {"heldout_predictions.csv": csv_text(predictions, PRED_FIELDS),
                "participant_metrics.csv": csv_text(people_metrics, METRIC_FIELDS),
                "target_validity_report.json": json_text(report_value)}
    payloads["claim_source_map.json"] = json_text(claim_map({name: text_sha(text) for name, text in payloads.items()}, before))
    after_analysis = input_hashes(paths, workspace); assert_same_hashes(before, after_analysis, "analysis")
    ensure_absent(output)
    for name in RESULTS[:-1]:
        write_x(output / name, payloads[name])
    after_write = input_hashes(paths, workspace); assert_same_hashes(before, after_write, "output write")
    output_hashes = {name: sha(output / name) for name in RESULTS[:-1]}
    if output_hashes != {name: text_sha(payloads[name]) for name in RESULTS[:-1]}:
        raise RuntimeError("Output payload hash mismatch")
    all_pairs = all(item["n_pairs"] == len(test_keys) and item["candidate_only_n"] == 0 and item["reference_only_n"] == 0
                    for item in paired.values())
    check = {"schema_version": 1, "version": VERSION, "status": "verified_in_process_with_scope_limitations",
        "design_status": "post_hoc_reanalysis_of_already_inspected_personal_test",
        "independently_prespecified_confirmation": False,
        "input_hashes_before": before, "input_hashes_after_analysis": after_analysis,
        "input_hashes_after_output_write": after_write, "inputs_unchanged": True,
        "v1_prediction_rows_read": len(raw), "v1_unique_targets_read": len(targets),
        "heldout_unique_targets": len(test_keys), "heldout_prediction_rows_written": len(predictions),
        "heldout_only_metrics": all(row["split"] == "test" for row in predictions),
        "ids_and_splits_preserved": all(row["split"] == "test" and (row["participant_id"], row["target_id"]) in test_keys for row in predictions),
        "participant_train_prior_recomputed_from_train_only": True, "complete_history_passed_across_splits": True,
        "reused_helpers": ["baseline_predictions", "within_person_diagnostic"],
        "paired_same_night_reference_complete": all_pairs, "reference_arm": REFERENCE_ARM,
        "survey_feasibility_gate_before_fit": gate, "bootstrap_or_confidence_interval_computed": False,
        "descriptive_only": True, "new_model_training": False, "survey_model_fit": False,
        "raw_heart_rate_or_activity_ingested": False,
        "output_write_mode": "exclusive create; refuse any registered existing result", "output_sha256": output_hashes}
    write_x(output / "verification.json", json_text(check))
    return {"version": VERSION, "participants": len(flags), "heldout_nights": len(test_keys),
            "prediction_rows": len(predictions), "reference_arm": REFERENCE_ARM,
            "survey_fit_status": gate["status"], "outputs": [rel(output / name, workspace) for name in RESULTS]}


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output-dir", type=Path, default=None,
                        help=f"default: 데이터모델/연구용데이터/{V2_DIR} under --workspace")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv); workspace = args.workspace.resolve()
    output = args.output_dir.resolve() if args.output_dir else default_output(workspace).resolve()
    print(json.dumps(run(workspace, output), ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
