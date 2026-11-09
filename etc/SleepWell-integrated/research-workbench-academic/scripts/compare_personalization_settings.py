"""Compare residual-personalization settings on frozen CMI/MESA predictions.

The candidate set and decision gate are fixed in the protocol JSON.  This
script selects on validation participants, opens Test only for the selected
alternative, and writes aggregate results without participant identifiers.
It evaluates the additive residual layer, not the recommendation policy.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = Path("E:/학술제/model/데이터모델/연구용데이터/gradual_v1_reassessment_20261103_input_v2/normalized_cmi_mesa_private.csv")
DEFAULT_PREDICTIONS = Path("E:/학술제/model/데이터모델/연구용데이터/gradual_v1_reassessment_20261103_results_final/private_predictions.csv")
DEFAULT_PROTOCOL = ROOT / "reports" / "personalization_settings_20261104" / "protocol.json"
DEFAULT_OUTPUT = ROOT / "reports" / "personalization_settings_20261104"
CANONICAL = ROOT / "work" / "dayone-personalization-20261027" / "web-app"
ARMS = ("data", "hybrid")
COHORTS = ("cmi", "mesa")
CURRENT_ID = "current_h14_l14_m1_clip180_cap60"
DISABLED_ID = "disabled_correction"


@dataclass(frozen=True)
class Setting:
    id: str
    half_life_days: float = 14.0
    shrinkage_lambda: float = 14.0
    minimum_history: int = 1
    residual_clip_min: float = 180.0
    correction_cap_min: float = 60.0
    disabled: bool = False


CURRENT = Setting(CURRENT_ID)
CANDIDATES = (
    replace(CURRENT, id="half_life_7", half_life_days=7.0),
    replace(CURRENT, id="half_life_28", half_life_days=28.0),
    replace(CURRENT, id="lambda_7", shrinkage_lambda=7.0),
    replace(CURRENT, id="lambda_28", shrinkage_lambda=28.0),
    replace(CURRENT, id="minimum_history_3", minimum_history=3),
    replace(CURRENT, id="minimum_history_7", minimum_history=7),
    replace(CURRENT, id="minimum_history_14", minimum_history=14),
    replace(CURRENT, id="residual_clip_90", residual_clip_min=90.0),
    replace(CURRENT, id="residual_clip_360", residual_clip_min=360.0),
    replace(CURRENT, id="correction_cap_30", correction_cap_min=30.0),
    replace(CURRENT, id="correction_cap_90", correction_cap_min=90.0),
)
DISABLED = replace(CURRENT, id=DISABLED_ID, disabled=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _time(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result


def constrain(bed: float, wake: float) -> tuple[float, float]:
    bed = min(960.0, max(0.0, float(bed)))
    wake = min(1440.0, max(bed + 30.0, min(bed + 960.0, float(wake))))
    return bed, wake


def adjustment(history: list[dict[str, Any]], cutoff: datetime, setting: Setting) -> dict[str, Any]:
    """Production-equivalent correction with setting-specific clip and cap."""
    if setting.disabled:
        return {"onset": 0.0, "wake": 0.0, "eligible": 0, "cap_hits": 0,
                "clipped_values": 0, "history_values": 0}
    latest: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(history):
        try:
            available = _time(row["available_at"])
            outcome = _time(row.get("outcome_at", row["available_at"]))
            onset = float(row["onset_residual_min"])
            wake = float(row["wake_residual_min"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (onset, wake)):
            continue
        if available >= cutoff or outcome > available:
            continue
        key = str(row.get("night_id", f"__row_{index}"))
        prior = latest.get(key)
        item = {"available": available, "outcome": outcome, "onset": onset, "wake": wake}
        if prior is None or available > prior["available"]:
            latest[key] = item
    rows = sorted(latest.values(), key=lambda row: row["available"])
    weights = [2.0 ** (-((cutoff - row["outcome"]).total_seconds() / 86400.0) /
                       setting.half_life_days) for row in rows]
    mass = sum(weights)
    clipped = []
    clipped_values = 0
    for row in rows:
        values = []
        for name in ("onset", "wake"):
            value = row[name]
            if abs(value) > setting.residual_clip_min:
                clipped_values += 1
            values.append(min(setting.residual_clip_min, max(-setting.residual_clip_min, value)))
        clipped.append(values)
    raw_onset = sum(w * values[0] for w, values in zip(weights, clipped)) / mass if mass else 0.0
    raw_wake = sum(w * values[1] for w, values in zip(weights, clipped)) / mass if mass else 0.0
    active = len(rows) >= setting.minimum_history and mass > 0
    shrinkage = mass / (mass + setting.shrinkage_lambda) if active else 0.0
    pre_onset, pre_wake = shrinkage * raw_onset, shrinkage * raw_wake
    onset = min(setting.correction_cap_min, max(-setting.correction_cap_min, pre_onset))
    wake = min(setting.correction_cap_min, max(-setting.correction_cap_min, pre_wake))
    return {
        "onset": onset, "wake": wake, "eligible": len(rows),
        "cap_hits": int(abs(pre_onset) > setting.correction_cap_min) +
                    int(abs(pre_wake) > setting.correction_cap_min),
        "clipped_values": clipped_values, "history_values": 2 * len(rows),
    }


def _load_production_modules() -> tuple[Any, Any]:
    def load(name: str, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return (load("sleepwell_adaptive_for_comparison", CANONICAL / "adaptive_personalization.py"),
            load("sleepwell_night_core_for_comparison", CANONICAL / "night_core.py"))


def load_rows(input_path: Path, prediction_path: Path) -> list[dict[str, Any]]:
    inputs: dict[tuple[str, str], dict[str, Any]] = {}
    splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    participant_cutoffs: set[tuple[str, str, datetime]] = set()
    with input_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["cohort"] not in COHORTS or row["split"] not in {"train", "validation", "test"}:
                raise ValueError("unrecognized input cohort or split")
            if not row["participant_id"]:
                raise ValueError("empty input participant_id")
            key = (row["cohort"], row["row_id"])
            if key in inputs:
                raise ValueError(f"duplicate input key: {key}")
            cutoff, bed, wake, observed = map(_time, (row["cutoff"], row["bed"], row["wake"], row["observed_at"]))
            if not (cutoff <= bed < wake <= observed):
                raise ValueError(f"invalid outcome chronology: {key}")
            participant_cutoff = (row["cohort"], row["participant_id"], cutoff)
            if participant_cutoff in participant_cutoffs:
                raise ValueError("duplicate participant-cutoff input")
            participant_cutoffs.add(participant_cutoff)
            actual_bed, actual_wake = float(row["bed_offset"]), float(row["wake_offset"])
            if (not all(math.isfinite(value) for value in (actual_bed, actual_wake)) or
                    not (0 <= actual_bed <= 960 and actual_bed + 30 <= actual_wake <= 1440)):
                raise ValueError(f"invalid finite input targets: {key}")
            splits[(row["cohort"], row["participant_id"])].add(row["split"])
            inputs[key] = {
                "cohort": row["cohort"], "participant_id": row["participant_id"],
                "split": row["split"], "row_id": row["row_id"], "cutoff": cutoff,
                "wake_time": wake, "observed_at": observed,
                "actual_bed": actual_bed, "actual_wake": actual_wake,
            }
    overlap = [key for key, values in splits.items() if len(values) != 1]
    if overlap:
        raise ValueError("participant-disjoint split violation")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    with prediction_path.open(encoding="utf-8-sig", newline="") as stream:
        for prediction in csv.DictReader(stream):
            if prediction["policy"] != "gradual_v1" or prediction["arm"] not in ARMS:
                continue
            if prediction["cohort"] not in COHORTS or prediction["split"] not in {"train", "validation", "test"}:
                raise ValueError("unrecognized prediction cohort or split")
            key = (prediction["cohort"], prediction["row_id"])
            source = inputs.get(key)
            if source is None:
                raise ValueError(f"prediction lacks input: {key}")
            pred_cutoff = _time(prediction["cutoff"])
            if pred_cutoff != source["cutoff"]:
                raise ValueError(f"cutoff mismatch: {key}")
            if prediction["participant_id"] != source["participant_id"]:
                raise ValueError(f"participant mismatch: {key}")
            if prediction["split"] != source["split"]:
                raise ValueError(f"split mismatch: {key}")
            try:
                target_bed, target_wake = float(prediction["bed_offset"]), float(prediction["wake_offset"])
                base_bed = float(prediction["predicted_bed_offset"])
                base_wake = float(prediction["predicted_wake_offset"])
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"non-numeric prediction values: {key}") from error
            if not all(math.isfinite(value) for value in (target_bed, target_wake, base_bed, base_wake)):
                raise ValueError(f"non-finite prediction values: {key}")
            if target_bed != source["actual_bed"] or target_wake != source["actual_wake"]:
                raise ValueError(f"target mismatch: {key}")
            full_key = (*key, prediction["arm"])
            if full_key in seen:
                raise ValueError(f"duplicate prediction key: {full_key}")
            seen.add(full_key)
            rows.append({**source, "arm": prediction["arm"],
                         "base_bed": base_bed, "base_wake": base_wake})
    expected = len(inputs) * len(ARMS)
    if len(rows) != expected or len(seen) != expected:
        raise ValueError(f"incomplete prediction coverage: {len(rows)} != {expected}")
    if set(row["cohort"] for row in rows) != set(COHORTS):
        raise ValueError("required cohorts missing")
    return rows


def replay(rows: list[dict[str, Any]], settings: Iterable[Setting], *, verify_production: bool = False
           ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = tuple(settings)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["cohort"], row["participant_id"], row["arm"])].append(row)
    results: list[dict[str, Any]] = []
    parity_max = 0.0
    production_adaptive = production_night = None
    if verify_production:
        production_adaptive, production_night = _load_production_modules()
    for key, subject_rows in sorted(grouped.items()):
        subject_rows.sort(key=lambda row: row["cutoff"])
        history: list[dict[str, Any]] = []
        for row in subject_rows:
            for setting in settings:
                change = adjustment(history, row["cutoff"], setting)
                if verify_production and setting.id == CURRENT_ID:
                    prod = production_adaptive.compute_adjustment(history, row["cutoff"])
                    parity_max = max(parity_max, abs(change["onset"] - prod["onset_correction_min"]),
                                     abs(change["wake"] - prod["wake_correction_min"]))
                predicted_bed, predicted_wake = constrain(
                    row["base_bed"] + change["onset"], row["base_wake"] + change["wake"])
                if verify_production and setting.id == CURRENT_ID:
                    prod_bed, prod_wake = production_night.constrain(
                        row["base_bed"] + prod["onset_correction_min"],
                        row["base_wake"] + prod["wake_correction_min"])
                    parity_max = max(parity_max, abs(predicted_bed - prod_bed), abs(predicted_wake - prod_wake))
                bed_error = abs(predicted_bed - row["actual_bed"])
                wake_error = abs(predicted_wake - row["actual_wake"])
                results.append({
                    "cohort": row["cohort"], "participant_id": row["participant_id"],
                    "split": row["split"], "arm": row["arm"], "setting": setting.id,
                    "joint_error": (bed_error + wake_error) / 2.0,
                    "bed_error": bed_error, "wake_error": wake_error,
                    "eligible": change["eligible"], "cap_hits": change["cap_hits"],
                    "clipped_values": change["clipped_values"],
                    "history_values": change["history_values"],
                })
            # Current outcome becomes available only after this prediction.
            history.append({
                "night_id": row["row_id"], "available_at": row["observed_at"].isoformat(),
                "outcome_at": row["wake_time"].isoformat(),
                "onset_residual_min": row["actual_bed"] - row["base_bed"],
                "wake_residual_min": row["actual_wake"] - row["base_wake"],
            })
    return results, {"verified": verify_production and parity_max <= 1e-12,
                     "max_abs_difference_min": parity_max, "tolerance_min": 1e-12}


def participant_values(records: list[dict[str, Any]], split: str, setting: str
                       ) -> dict[tuple[str, str], dict[str, float]]:
    nights: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in records:
        if row["split"] == split and row["setting"] == setting:
            nights[(row["cohort"], row["arm"], row["participant_id"])].append(row["joint_error"])
    cells: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for (cohort, arm, participant), values in nights.items():
        cells[(cohort, arm)][participant] = mean(values)
    return cells


def metric_rows(records: list[dict[str, Any]], split: str, setting: str) -> list[dict[str, Any]]:
    cells = participant_values(records, split, setting)
    output = []
    for cohort in COHORTS:
        for arm in ARMS:
            values = cells[(cohort, arm)]
            matching = [row for row in records if row["split"] == split and row["setting"] == setting
                        and row["cohort"] == cohort and row["arm"] == arm]
            output.append({
                "split": split, "setting": setting, "cohort": cohort, "arm": arm,
                "participant_macro_joint_mae_min": mean(values.values()),
                "n_participants": len(values), "n_nights": len(matching),
                "correction_cap_hit_rate": sum(row["cap_hits"] for row in matching) /
                                           (2 * len(matching)) if matching else 0.0,
                "history_residual_clip_rate": sum(row["clipped_values"] for row in matching) /
                                              sum(row["history_values"] for row in matching)
                                              if sum(row["history_values"] for row in matching) else 0.0,
            })
    return output


def choose_candidate(records: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    current = {(row["cohort"], row["arm"]): row for row in metric_rows(records, "validation", CURRENT_ID)}
    comparisons = []
    for setting in CANDIDATES:
        cells = metric_rows(records, "validation", setting.id)
        gains = []
        for row in cells:
            gain = current[(row["cohort"], row["arm"])]["participant_macro_joint_mae_min"] - row["participant_macro_joint_mae_min"]
            gains.append(gain)
            row["gain_vs_current_min"] = gain
        aggregate = mean(gains)
        comparisons.append({"setting": setting.id, "aggregate_gain_vs_current_min": aggregate,
                            "no_cell_worsening": all(value >= -1e-12 for value in gains),
                            "validation_gate": aggregate >= 1.0 and all(value >= -1e-12 for value in gains),
                            "cells": cells})
    selected = max(comparisons, key=lambda item: (item["aggregate_gain_vs_current_min"], item["setting"]))
    return selected["setting"], comparisons, bool(selected["validation_gate"])


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap_gains(records: list[dict[str, Any]], selected: str, *, replicates: int, seed: int
                    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
    current = participant_values(records, "test", CURRENT_ID)
    alternative = participant_values(records, "test", selected)
    rng = random.Random(seed)
    cell_results = []
    differences_by_cell: dict[tuple[str, str], list[float]] = {}
    participants_by_cohort: dict[str, list[str]] = {}
    for cohort in COHORTS:
        cohort_ids: list[str] | None = None
        for arm in ARMS:
            key = (cohort, arm)
            ids = sorted(set(current[key]) & set(alternative[key]))
            if set(current[key]) != set(alternative[key]) or not ids:
                raise ValueError(f"paired participant coverage mismatch: {key}")
            if cohort_ids is None:
                cohort_ids = ids
            elif cohort_ids != ids:
                raise ValueError(f"arm participant alignment mismatch: {cohort}")
            differences_by_cell[key] = [current[key][participant] - alternative[key][participant]
                                        for participant in ids]
        participants_by_cohort[cohort] = cohort_ids or []

    draws_by_cell: dict[tuple[str, str], list[float]] = {key: [] for key in differences_by_cell}
    for _ in range(replicates):
        for cohort in COHORTS:
            size = len(participants_by_cohort[cohort])
            indices = [rng.randrange(size) for _ in range(size)]
            for arm in ARMS:
                values = differences_by_cell[(cohort, arm)]
                draws_by_cell[(cohort, arm)].append(mean(values[index] for index in indices))

    for cohort in COHORTS:
        for arm in ARMS:
            key = (cohort, arm)
            ids = participants_by_cohort[cohort]
            differences = differences_by_cell[key]
            draws = draws_by_cell[key]
            point = mean(differences)
            cell_results.append({
                "cohort": cohort, "arm": arm, "n_participants": len(ids),
                "gain_vs_current_min": point,
                "ci95_low": _quantile(draws, 0.025), "ci95_high": _quantile(draws, 0.975),
                "ci98_75_low": _quantile(draws, 0.00625),
                "ci98_75_high": _quantile(draws, 0.99375),
                "simultaneous_cell_pass": _quantile(draws, 0.00625) > 0.0,
            })
    aggregate_draws = [mean(draws_by_cell[key][index] for key in sorted(draws_by_cell))
                       for index in range(replicates)]
    aggregate = {
        "gain_vs_current_min": mean(row["gain_vs_current_min"] for row in cell_results),
        "ci95_low": _quantile(aggregate_draws, 0.025),
        "ci95_high": _quantile(aggregate_draws, 0.975),
        "ci98_75_low": _quantile(aggregate_draws, 0.00625),
        "ci98_75_high": _quantile(aggregate_draws, 0.99375),
    }
    return cell_results, aggregate


def _protocol_matches(protocol: dict[str, Any]) -> None:
    expected_top = {
        "schema": "sleepwell.residual_personalization_protocol.v1",
        "status": "locally_prespecified_before_comparison",
        "scope": "inference-time residual correction only; not recommendation-policy or physiological-constant validation",
        "data_status": "retrospective_reused_holdout",
        "base_policy": "gradual_v1 frozen predictions",
        "cohorts": list(COHORTS), "arms": list(ARMS),
        "comparator": {"id": DISABLED_ID, "correction": 0.0},
        "selection": {
            "split": "validation",
            "metric": "equal mean of participant-macro joint MAE across cmi/mesa and data/hybrid",
            "candidate_set": "one-factor alternatives only; disabled comparator is not selectable",
            "rule": "largest aggregate gain versus current",
            "validation_gate": "aggregate gain >= 1 minute and no worsening in any of four cohort-arm cells",
        },
        "locked_test_gate": {
            "split": "test", "bootstrap_unit": "participant", "replicates": 10000,
            "seed": 20261104,
            "multiplicity": "Bonferroni familywise 95% across four cohort-arm cells",
            "cell_interval": "two-sided 98.75% paired participant bootstrap CI",
            "pass": "validation gate passes, test aggregate gain >= 1 minute, and every cell CI lower bound > 0",
        },
    }
    for name, expected in expected_top.items():
        if protocol.get(name) != expected:
            raise ValueError(f"protocol contract mismatch: {name}")
    declared = protocol["current"]
    for name, value in asdict(CURRENT).items():
        if name == "disabled":
            continue
        if declared[name] != value:
            raise ValueError(f"protocol current setting mismatch: {name}")
    expanded = []
    current = {key: value for key, value in asdict(CURRENT).items() if key != "disabled"}
    for override in protocol["one_factor_alternatives"]:
        values = {**current, **override}
        expanded.append(Setting(**values))
    if expanded != list(CANDIDATES):
        raise ValueError("protocol candidate settings mismatch")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, columns)
        writer.writeheader()
        writer.writerows(rows)


def run(input_path: Path, prediction_path: Path, protocol_path: Path, output_dir: Path,
        *, replicates: int = 10000, seed: int = 20261104) -> dict[str, Any]:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    _protocol_matches(protocol)
    if protocol["locked_test_gate"]["replicates"] != replicates or protocol["locked_test_gate"]["seed"] != seed:
        raise ValueError("bootstrap parameters differ from protocol")
    rows = load_rows(input_path, prediction_path)
    validation_rows = [row for row in rows if row["split"] == "validation"]
    test_rows = [row for row in rows if row["split"] == "test"]
    all_settings = (CURRENT, *CANDIDATES, DISABLED)
    validation_predictions, validation_parity = replay(
        validation_rows, all_settings, verify_production=True)
    if not validation_parity["verified"]:
        raise ValueError(f"production validation parity failed: {validation_parity}")
    selected, comparisons, validation_gate = choose_candidate(validation_predictions)

    output_dir.mkdir(parents=True, exist_ok=True)
    selection_lock = {
        "schema": "sleepwell.residual_personalization_selection_lock.v1",
        "locked_before_test_replay": True,
        "selected_validation_alternative": selected,
        "validation_gate_passed": validation_gate,
        "protocol_sha256": sha256(protocol_path),
        "input_sha256": sha256(input_path),
        "frozen_predictions_sha256": sha256(prediction_path),
    }
    selection_lock_path = output_dir / "selection_lock.json"
    selection_lock_path.write_text(
        json.dumps(selection_lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    selected_setting = next(setting for setting in CANDIDATES if setting.id == selected)
    test_predictions, test_parity = replay(
        test_rows, (CURRENT, selected_setting, DISABLED), verify_production=True)
    if not test_parity["verified"]:
        raise ValueError(f"production test parity failed: {test_parity}")
    parity = {
        "verified": True,
        "max_abs_difference_min": max(validation_parity["max_abs_difference_min"],
                                        test_parity["max_abs_difference_min"]),
        "tolerance_min": 1e-12,
        "splits_checked": ["validation", "test"],
    }

    validation_metrics = []
    for setting in all_settings:
        validation_metrics.extend(metric_rows(validation_predictions, "validation", setting.id))
    test_settings = (CURRENT_ID, DISABLED_ID, selected)
    test_metrics = []
    for setting in test_settings:
        test_metrics.extend(metric_rows(test_predictions, "test", setting))
    cell_bootstrap, aggregate_bootstrap = bootstrap_gains(
        test_predictions, selected, replicates=replicates, seed=seed)
    test_gate = (validation_gate and aggregate_bootstrap["gain_vs_current_min"] >= 1.0 and
                 all(row["simultaneous_cell_pass"] for row in cell_bootstrap))

    frozen_evaluation = ROOT / "research_workbench_academic" / "deliverables" / "gradual_v1_20261103" / "evaluation.json"
    frozen = json.loads(frozen_evaluation.read_text(encoding="utf-8"))
    model_sources = [row for row in frozen.get("sources", []) if row.get("role", "").startswith("frozen_model_")]
    model_verification = []
    for source in model_sources:
        cohort = source["role"].removeprefix("frozen_model_")
        actual_path = ROOT / "research_workbench_academic" / "vendor" / "sleepwell" / "models" / f"robust_{cohort}_v2.json"
        actual_hash = sha256(actual_path)
        if actual_hash != source["sha256"]:
            raise ValueError(f"frozen model hash mismatch: {cohort}")
        model_verification.append({"role": source["role"], "path": str(actual_path),
                                   "expected_sha256": source["sha256"],
                                   "actual_sha256": actual_hash, "verified": True})
    provenance = {
        "input_csv": {"path": str(input_path), "sha256": sha256(input_path)},
        "frozen_predictions": {"path": str(prediction_path), "sha256": sha256(prediction_path)},
        "protocol": {"path": str(protocol_path), "sha256": sha256(protocol_path)},
        "selection_lock": {"path": str(selection_lock_path), "sha256": sha256(selection_lock_path)},
        "comparison_script": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
        "production_sources_at_evaluation": [
            {"path": str(CANONICAL / name), "sha256": sha256(CANONICAL / name)}
            for name in ("adaptive_personalization.py", "night_core.py")
        ],
        "frozen_model_hash_verification": model_verification,
        "frozen_prediction_prior_hash": {
            "available": False,
            "limitation": "The 2026-11-03 aggregate evaluation did not record a hash for private_predictions.csv; the current file hash is captured but cannot be compared with a prior manifest hash."
        },
    }
    frozen_input = next(row for row in frozen["sources"] if row["role"] == "normalized_private_input")
    if provenance["input_csv"]["sha256"] != frozen_input["sha256"]:
        raise ValueError("input hash differs from frozen evaluation metadata")

    result = {
        "schema": "sleepwell.residual_personalization_comparison.v1",
        "status": "retrospective_reused_holdout",
        "scope": protocol["scope"], "current": asdict(CURRENT),
        "selected_validation_alternative": selected,
        "validation_gate_passed": validation_gate,
        "locked_test_gate_passed": test_gate,
        "decision": ("change_supported_for_residual_layer_only" if test_gate
                     else "retain_current_settings_no_verified_improvement"),
        "production_parity": parity,
        "selection": comparisons,
        "validation_metrics": validation_metrics,
        "test_metrics_opened_after_selection": test_metrics,
        "test_paired_participant_bootstrap": {
            "replicates": replicates, "seed": seed,
            "cell_intervals": cell_bootstrap, "equal_cell_aggregate": aggregate_bootstrap,
        },
        "row_counts": {
            "all_input_rows_schema_checked": len(rows) // len(ARMS),
            "validation_base_prediction_rows": len(validation_rows),
            "validation_setting_prediction_rows": len(validation_predictions),
            "test_base_prediction_rows_after_selection_lock": len(test_rows),
            "test_setting_prediction_rows": len(test_predictions),
            "train_rows_used_for_metrics_or_selection": 0,
        },
        "provenance": provenance,
        "interpretation_limits": protocol["interpretation_limits"],
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flat_validation = []
    for comparison in comparisons:
        for cell in comparison["cells"]:
            flat_validation.append({
                "setting": comparison["setting"], "aggregate_gain_vs_current_min": comparison["aggregate_gain_vs_current_min"],
                "validation_gate": comparison["validation_gate"], "cohort": cell["cohort"], "arm": cell["arm"],
                "joint_mae_min": cell["participant_macro_joint_mae_min"],
                "gain_vs_current_min": cell["gain_vs_current_min"],
                "n_participants": cell["n_participants"], "n_nights": cell["n_nights"],
            })
    _write_csv(output_dir / "validation_candidates.csv", flat_validation)
    _write_csv(output_dir / "test_selected.csv", cell_bootstrap)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--predictions-csv", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bootstrap-replicates", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261104)
    args = parser.parse_args()
    result = run(args.input_csv.resolve(), args.predictions_csv.resolve(), args.protocol.resolve(),
                 args.output_dir.resolve(), replicates=args.bootstrap_replicates, seed=args.seed)
    print(json.dumps({
        "selected": result["selected_validation_alternative"],
        "validation_gate_passed": result["validation_gate_passed"],
        "locked_test_gate_passed": result["locked_test_gate_passed"],
        "decision": result["decision"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
