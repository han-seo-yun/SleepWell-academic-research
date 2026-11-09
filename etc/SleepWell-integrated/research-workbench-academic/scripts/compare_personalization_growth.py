"""Compare prespecified personal-weight growth rules on frozen predictions.

All alternatives retain the production half-life, residual clip, correction
cap, and causal eligibility rules. Selection uses Validation only; the chosen
rule is locked before Test replay. Outputs are aggregate and contain no IDs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[2]
ACADEMIC = ROOT / "research_workbench_academic"
NOV4_SCRIPT = ACADEMIC / "scripts" / "compare_personalization_settings.py"
NOV4_RESULT = ROOT / "reports" / "personalization_settings_20261104" / "comparison.json"
DEFAULT_PROTOCOL = ROOT / "reports" / "personalization_growth_20261105" / "protocol.json"
DEFAULT_OUTPUT = ROOT / "reports" / "personalization_growth_20261105"
DEFAULT_INPUT = Path("E:/학술제/model/데이터모델/연구용데이터/gradual_v1_reassessment_20261103_input_v2/normalized_cmi_mesa_private.csv")
DEFAULT_PREDICTIONS = Path("E:/학술제/model/데이터모델/연구용데이터/gradual_v1_reassessment_20261103_results_final/private_predictions.csv")
FROZEN_EVALUATION = ACADEMIC / "deliverables" / "gradual_v1_20261103" / "evaluation.json"
CANONICAL = ROOT / "work" / "dayone-personalization-20261027" / "web-app"
CURRENT_ID = "current_mass_lambda14"
DISABLED_ID = "disabled_correction"
ARMS = ("data", "hybrid")
COHORTS = ("cmi", "mesa")
HISTORY_LABELS = ("0", "1-6", "7-13", "14-27", "28-55", "56+")


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BASE = _load_module("sleepwell_nov4_comparison", NOV4_SCRIPT)


@dataclass(frozen=True)
class GrowthSetting:
    id: str
    kind: str


CURRENT = GrowthSetting(CURRENT_ID, "current")
CANDIDATES = (
    GrowthSetting("count_weight", "count"),
    GrowthSetting("lambda_linear14", "linear14"),
    GrowthSetting("lambda_linear28", "linear28"),
    GrowthSetting("lambda_decay28", "decay28"),
)
DISABLED = GrowthSetting(DISABLED_ID, "disabled")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def alpha_for(setting: GrowthSetting, n: int, mass: float) -> tuple[float, float | None]:
    if n < 1 or mass <= 0:
        return 0.0, None
    if setting.kind == "current":
        effective_lambda = 14.0
        return mass / (mass + effective_lambda), effective_lambda
    if setting.kind == "count":
        return n / (n + 14.0), None
    if setting.kind == "linear14":
        effective_lambda = 14.0 / (1.0 + n / 14.0)
    elif setting.kind == "linear28":
        effective_lambda = 14.0 / (1.0 + n / 28.0)
    elif setting.kind == "decay28":
        effective_lambda = 14.0 * 2.0 ** (-n / 28.0)
    elif setting.kind == "disabled":
        return 0.0, None
    else:
        raise ValueError(f"unknown growth setting: {setting.kind}")
    return mass / (mass + effective_lambda), effective_lambda


def growth_adjustments(history: list[dict[str, Any]], cutoff: datetime,
                       settings: Iterable[GrowthSetting], adaptive: Any) -> dict[str, dict[str, Any]]:
    production = adaptive.compute_adjustment(history, cutoff)
    n = int(production["eligible_count"])
    mass = float(production["decayed_mass"])
    raw_onset = production["raw_onset_residual_mean_min"] or 0.0
    raw_wake = production["raw_wake_residual_mean_min"] or 0.0
    output = {}
    for setting in settings:
        if setting.kind == "current":
            alpha = float(production["weight"])
            onset = float(production["onset_correction_min"])
            wake = float(production["wake_correction_min"])
            effective_lambda = 14.0 if n else None
            pre_onset, pre_wake = alpha * raw_onset, alpha * raw_wake
        elif setting.kind == "disabled":
            alpha, effective_lambda = 0.0, None
            pre_onset = pre_wake = onset = wake = 0.0
        else:
            alpha, effective_lambda = alpha_for(setting, n, mass)
            pre_onset, pre_wake = alpha * raw_onset, alpha * raw_wake
            onset = min(60.0, max(-60.0, pre_onset))
            wake = min(60.0, max(-60.0, pre_wake))
        output[setting.id] = {
            "onset": onset, "wake": wake, "alpha": alpha,
            "effective_lambda": effective_lambda, "eligible": n, "mass": mass,
            "gap_days": production["last_outcome_gap_days"],
            "cap_hits": int(abs(pre_onset) > 60.0) + int(abs(pre_wake) > 60.0),
        }
    return output


def replay(rows: list[dict[str, Any]], settings: Iterable[GrowthSetting],
           *, production_modules: tuple[Any, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = tuple(settings)
    adaptive, night_core = production_modules or BASE._load_production_modules()
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["cohort"], row["participant_id"], row["arm"])].append(row)
    results = []
    parity_max = 0.0
    for _, subject_rows in sorted(grouped.items()):
        subject_rows.sort(key=lambda row: row["cutoff"])
        history: list[dict[str, Any]] = []
        for row in subject_rows:
            changes = growth_adjustments(history, row["cutoff"], settings, adaptive)
            for setting in settings:
                change = changes[setting.id]
                bed, wake = BASE.constrain(row["base_bed"] + change["onset"],
                                           row["base_wake"] + change["wake"])
                if setting.kind == "current":
                    prod_bed, prod_wake = night_core.constrain(
                        row["base_bed"] + change["onset"], row["base_wake"] + change["wake"])
                    parity_max = max(parity_max, abs(bed - prod_bed), abs(wake - prod_wake))
                bed_error = abs(bed - row["actual_bed"])
                wake_error = abs(wake - row["actual_wake"])
                results.append({
                    "cohort": row["cohort"], "participant_id": row["participant_id"],
                    "split": row["split"], "arm": row["arm"], "setting": setting.id,
                    "joint_error": (bed_error + wake_error) / 2.0,
                    "bed_error": bed_error, "wake_error": wake_error,
                    "eligible": change["eligible"], "mass": change["mass"],
                    "alpha": change["alpha"], "gap_days": change["gap_days"],
                    "cap_hits": change["cap_hits"], "clipped_values": 0, "history_values": 0,
                })
            history.append({
                "night_id": row["row_id"], "available_at": row["observed_at"].isoformat(),
                "outcome_at": row["wake_time"].isoformat(),
                "onset_residual_min": row["actual_bed"] - row["base_bed"],
                "wake_residual_min": row["actual_wake"] - row["base_wake"],
            })
    return results, {"verified": parity_max <= 1e-12,
                     "max_abs_difference_min": parity_max, "tolerance_min": 1e-12}


def metric_rows(records: list[dict[str, Any]], split: str, setting: str) -> list[dict[str, Any]]:
    cells = BASE.participant_values(records, split, setting)
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
        no_worsening = all(value >= -1e-12 for value in gains)
        comparisons.append({"setting": setting.id, "aggregate_gain_vs_current_min": aggregate,
                            "no_cell_worsening": no_worsening,
                            "validation_gate": aggregate >= 1.0 and no_worsening,
                            "cells": cells})
    selected = max(comparisons, key=lambda item: (item["aggregate_gain_vs_current_min"], item["setting"]))
    return selected["setting"], comparisons, bool(selected["validation_gate"])


def bootstrap_gains(records: list[dict[str, Any]], selected: str, *, replicates: int, seed: int):
    translated = [{**row, "setting": BASE.CURRENT_ID if row["setting"] == CURRENT_ID else row["setting"]}
                  for row in records if row["setting"] in {CURRENT_ID, selected}]
    return BASE.bootstrap_gains(translated, selected, replicates=replicates, seed=seed)


def history_label(n: int) -> str:
    if n == 0: return "0"
    if n <= 6: return "1-6"
    if n <= 13: return "7-13"
    if n <= 27: return "14-27"
    if n <= 55: return "28-55"
    return "56+"


def stratum_metrics(records: list[dict[str, Any]], split: str,
                    settings: Iterable[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if row["split"] == split and row["setting"] in settings:
            grouped[(row["setting"], row["cohort"], row["arm"], history_label(row["eligible"]))].append(row)
    output = []
    for setting in settings:
        for cohort in COHORTS:
            for arm in ARMS:
                for label in HISTORY_LABELS:
                    rows = grouped.get((setting, cohort, arm, label), [])
                    by_person: dict[str, list[float]] = defaultdict(list)
                    for row in rows:
                        by_person[row["participant_id"]].append(row["joint_error"])
                    output.append({
                        "split": split, "setting": setting, "cohort": cohort, "arm": arm,
                        "history_stratum": label, "n_nights": len(rows),
                        "n_participants": len(by_person),
                        "participant_macro_joint_mae_min":
                            mean(mean(values) for values in by_person.values()) if by_person else None,
                    })
    return output


def exploratory_subset_intervals(records: list[dict[str, Any]], selected: str,
                                 *, replicates: int, seed: int) -> list[dict[str, Any]]:
    conditions: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
        ("eligible_ge_14", lambda row: row["eligible"] >= 14),
        ("eligible_ge_28", lambda row: row["eligible"] >= 28),
        ("last_outcome_gap_ge_7_days", lambda row: row["gap_days"] is not None and row["gap_days"] >= 7),
    )
    output = []
    for condition_index, (label, predicate) in enumerate(conditions):
        subset = [row for row in records if row["setting"] in {CURRENT_ID, selected} and predicate(row)]
        current = BASE.participant_values(subset, "test", CURRENT_ID)
        alternative = BASE.participant_values(subset, "test", selected)
        for cell_index, (cohort, arm) in enumerate((c, a) for c in COHORTS for a in ARMS):
            ids = sorted(set(current[(cohort, arm)]) & set(alternative[(cohort, arm)]))
            if not ids or set(current[(cohort, arm)]) != set(alternative[(cohort, arm)]):
                output.append({"subset": label, "cohort": cohort, "arm": arm,
                               "status": "not_evaluable", "n_participants": len(ids), "n_nights": 0})
                continue
            differences = [current[(cohort, arm)][participant] - alternative[(cohort, arm)][participant]
                           for participant in ids]
            rng = random.Random(seed + condition_index * 100 + cell_index)
            draws = [mean(rng.choice(differences) for _ in ids) for _ in range(replicates)]
            nights = sum(1 for row in subset if row["setting"] == CURRENT_ID and
                         row["cohort"] == cohort and row["arm"] == arm)
            output.append({
                "subset": label, "cohort": cohort, "arm": arm, "status": "exploratory",
                "n_participants": len(ids), "n_nights": nights,
                "gain_vs_current_min": mean(differences),
                "ci95_low": BASE._quantile(draws, 0.025), "ci95_high": BASE._quantile(draws, 0.975),
            })
    return output


def alpha_diagnostics(records: list[dict[str, Any]], split: str,
                      settings: Iterable[str]) -> list[dict[str, Any]]:
    output = []
    for setting in settings:
        for cohort in COHORTS:
            rows = [row for row in records if row["split"] == split and
                    row["setting"] == setting and row["cohort"] == cohort]
            output.append({
                "split": split, "setting": setting, "cohort": cohort,
                "n_predictions_across_two_arms": len(rows),
                "max_eligible_history": max((row["eligible"] for row in rows), default=0),
                **{f"alpha_gt_{str(threshold).replace('.', '_')}_rate":
                   sum(row["alpha"] > threshold for row in rows) / len(rows) if rows else None
                   for threshold in (0.60, 0.75, 0.90)},
                "correction_cap_hit_rate": sum(row["cap_hits"] for row in rows) /
                                           (2 * len(rows)) if rows else None,
            })
    return output


def structural_table() -> list[dict[str, Any]]:
    rows = []
    for n in (1, 7, 14, 28, 56, 90, 180, 365):
        for gap in (0, 14, 56):
            mass = sum(2.0 ** (-(age + gap) / 14.0) for age in range(n))
            for setting in (CURRENT, *CANDIDATES):
                alpha, effective_lambda = alpha_for(setting, n, mass)
                rows.append({"n_daily_records": n, "gap_days_after_latest": gap,
                             "setting": setting.id, "decayed_mass": mass,
                             "effective_lambda": effective_lambda, "alpha": alpha})
    return rows


def _protocol_matches(protocol: dict[str, Any]) -> None:
    expected = {
        "schema": "sleepwell.personalization_growth_protocol.v1",
        "status": "locally_prespecified_before_comparison",
        "cohorts": list(COHORTS), "arms": list(ARMS),
        "current": {"id": CURRENT_ID, "alpha": "M/(M+14)"},
        "alternatives": [
            {"id": "count_weight", "alpha": "n/(n+14)"},
            {"id": "lambda_linear14", "lambda_effective": "14/(1+n/14)", "alpha": "M/(M+lambda_effective)"},
            {"id": "lambda_linear28", "lambda_effective": "14/(1+n/28)", "alpha": "M/(M+lambda_effective)"},
            {"id": "lambda_decay28", "lambda_effective": "14*2^(-n/28)", "alpha": "M/(M+lambda_effective)"},
        ],
        "fixed_parameters": {"half_life_days": 14.0, "minimum_history": 1,
                             "residual_clip_min": 180.0, "correction_cap_min": 60.0},
        "selection": {"split": "validation",
                      "metric": "equal mean of participant-macro joint MAE across cmi/mesa and data/hybrid",
                      "rule": "largest aggregate gain versus current",
                      "validation_gate": "aggregate gain >= 1 minute and no worsening in any of four cohort-arm cells"},
        "locked_test_gate": {"split": "test",
            "bootstrap_unit": "participant with the same sampled indices shared by data and hybrid within cohort",
            "replicates": 10000, "seed": 20261105,
            "multiplicity": "Bonferroni familywise 95% across four cohort-arm cells",
            "cell_interval": "two-sided 98.75% paired participant bootstrap CI",
            "pass": "validation gate passes, test aggregate gain >= 1 minute, and every cell CI lower bound > 0"},
    }
    for name, value in expected.items():
        if protocol.get(name) != value:
            raise ValueError(f"protocol contract mismatch: {name}")


def verify_frozen_baseline(disabled_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    frozen = json.loads(FROZEN_EVALUATION.read_text(encoding="utf-8"))
    lookup = {(row["split"], row["cohort"], row["arm"]): row for row in frozen["metrics"]
              if row["policy"] == "gradual_v1" and row["weighting"] == "participant" and
              row["metric"] == "joint_mae_min" and row["arm"] in ARMS and row["split"] in {"validation", "test"}}
    cells = []
    max_difference = 0.0
    for row in disabled_metrics:
        expected = lookup[(row["split"], row["cohort"], row["arm"])]
        difference = abs(row["participant_macro_joint_mae_min"] - expected["value"])
        max_difference = max(max_difference, difference)
        cells.append({"split": row["split"], "cohort": row["cohort"], "arm": row["arm"],
                      "difference_min": difference})
    return {"verified": max_difference <= 1e-9, "max_abs_difference_min": max_difference,
            "tolerance_min": 1e-9, "cells": cells}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def run(input_path: Path, prediction_path: Path, protocol_path: Path, output_dir: Path,
        *, replicates: int = 10000, seed: int = 20261105) -> dict[str, Any]:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    _protocol_matches(protocol)
    if protocol["locked_test_gate"]["replicates"] != replicates or protocol["locked_test_gate"]["seed"] != seed:
        raise ValueError("bootstrap parameters differ from protocol")
    nov4 = json.loads(NOV4_RESULT.read_text(encoding="utf-8"))
    if sha256(NOV4_SCRIPT) != nov4["provenance"]["comparison_script"]["sha256"]:
        raise ValueError("reused Nov4 comparison script changed")
    if sha256(input_path) != nov4["provenance"]["input_csv"]["sha256"]:
        raise ValueError("input differs from Nov4 anchored input")
    if sha256(prediction_path) != nov4["provenance"]["frozen_predictions"]["sha256"]:
        raise ValueError("predictions differ from Nov4 anchored predictions")
    expected_sources = {Path(row["path"]).name: row["sha256"]
                        for row in nov4["provenance"]["production_sources_at_evaluation"]}
    for name in ("adaptive_personalization.py", "night_core.py"):
        if sha256(CANONICAL / name) != expected_sources[name]:
            raise ValueError(f"production source changed since Nov4 baseline: {name}")

    rows = BASE.load_rows(input_path, prediction_path)
    validation_rows = [row for row in rows if row["split"] == "validation"]
    test_rows = [row for row in rows if row["split"] == "test"]
    validation_settings = (CURRENT, *CANDIDATES, DISABLED)
    validation_predictions, validation_parity = replay(validation_rows, validation_settings)
    if not validation_parity["verified"]:
        raise ValueError("validation current-policy parity failed")
    selected, comparisons, validation_gate = choose_candidate(validation_predictions)

    output_dir.mkdir(parents=True, exist_ok=True)
    selection_lock = {"schema": "sleepwell.personalization_growth_selection_lock.v1",
                      "locked_before_test_replay": True,
                      "selected_validation_alternative": selected,
                      "validation_gate_passed": validation_gate,
                      "protocol_sha256": sha256(protocol_path),
                      "input_sha256": sha256(input_path),
                      "frozen_predictions_sha256": sha256(prediction_path)}
    lock_path = output_dir / "selection_lock.json"
    lock_path.write_text(json.dumps(selection_lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    selected_setting = next(setting for setting in CANDIDATES if setting.id == selected)
    test_predictions, test_parity = replay(test_rows, (CURRENT, selected_setting, DISABLED))
    if not test_parity["verified"]:
        raise ValueError("test current-policy parity failed")

    validation_metrics = [cell for setting in validation_settings
                          for cell in metric_rows(validation_predictions, "validation", setting.id)]
    test_metrics = [cell for setting in (CURRENT, selected_setting, DISABLED)
                    for cell in metric_rows(test_predictions, "test", setting.id)]
    cell_bootstrap, aggregate_bootstrap = bootstrap_gains(
        test_predictions, selected, replicates=replicates, seed=seed)
    test_gate = (validation_gate and aggregate_bootstrap["gain_vs_current_min"] >= 1.0 and
                 all(row["simultaneous_cell_pass"] for row in cell_bootstrap))
    disabled_metrics = [row for row in (*validation_metrics, *test_metrics) if row["setting"] == DISABLED_ID]
    baseline_parity = verify_frozen_baseline(disabled_metrics)
    if not baseline_parity["verified"]:
        raise ValueError("disabled correction does not reproduce frozen gradual_v1 metrics")

    structural = structural_table()
    result = {
        "schema": "sleepwell.personalization_growth_comparison.v1",
        "status": "retrospective_reused_holdout",
        "scope": protocol["scope"],
        "selected_validation_alternative": selected,
        "validation_gate_passed": validation_gate, "locked_test_gate_passed": test_gate,
        "decision": ("growth_change_supported_for_residual_layer_only" if test_gate else
                     "retain_current_growth_no_verified_improvement"),
        "production_current_parity": {"validation": validation_parity, "test": test_parity},
        "frozen_disabled_baseline_parity": baseline_parity,
        "validation_selection": comparisons,
        "validation_metrics": validation_metrics,
        "test_metrics_opened_after_selection_lock": test_metrics,
        "test_paired_participant_bootstrap": {"replicates": replicates, "seed": seed,
            "cell_intervals": cell_bootstrap, "equal_cell_aggregate": aggregate_bootstrap},
        "validation_history_strata_all_settings": stratum_metrics(
            validation_predictions, "validation", [setting.id for setting in validation_settings]),
        "test_history_strata_current_selected": stratum_metrics(
            test_predictions, "test", [CURRENT_ID, selected]),
        "exploratory_test_subsets": exploratory_subset_intervals(
            test_predictions, selected, replicates=replicates, seed=seed),
        "alpha_diagnostics": [
            *alpha_diagnostics(validation_predictions, "validation", [setting.id for setting in validation_settings]),
            *alpha_diagnostics(test_predictions, "test", [CURRENT_ID, selected]),
        ],
        "structural_alpha_table": structural,
        "row_counts": {"all_input_rows_schema_checked": len(rows) // len(ARMS),
                       "validation_base_prediction_rows": len(validation_rows),
                       "validation_setting_prediction_rows": len(validation_predictions),
                       "test_base_prediction_rows_after_selection_lock": len(test_rows),
                       "test_setting_prediction_rows": len(test_predictions),
                       "train_rows_used_for_metrics_or_selection": 0},
        "provenance": {
            "input_csv": {"path": str(input_path), "sha256": sha256(input_path)},
            "frozen_predictions": {"path": str(prediction_path), "sha256": sha256(prediction_path),
                "anchored_to_nov4_comparison": True},
            "protocol": {"path": str(protocol_path), "sha256": sha256(protocol_path)},
            "selection_lock": {"path": str(lock_path), "sha256": sha256(lock_path)},
            "comparison_script": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
            "reused_nov4_script": {"path": str(NOV4_SCRIPT), "sha256": sha256(NOV4_SCRIPT), "unchanged": True},
            "nov4_comparison": {"path": str(NOV4_RESULT), "sha256": sha256(NOV4_RESULT)},
            "production_sources": [{"path": str(CANONICAL / name), "sha256": sha256(CANONICAL / name),
                                    "matches_nov4": True}
                                   for name in ("adaptive_personalization.py", "night_core.py")],
        },
        "interpretation_limits": protocol["interpretation_limits"],
        "methodology_acknowledgement": {
            "title": "Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents",
            "authors": "Timothy Kassis, Vinayak Agarwal, Yuhuan He, Darshil Patel, Aubrey M. Brueckner",
            "year": 2026, "arxiv": "2609.00065", "version_checked": "v2 (2026-09-02)",
            "doi": "https://doi.org/10.48550/arXiv.2609.00065"
        },
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flat_validation = []
    for comparison in comparisons:
        for cell in comparison["cells"]:
            flat_validation.append({"setting": comparison["setting"],
                "aggregate_gain_vs_current_min": comparison["aggregate_gain_vs_current_min"],
                "no_cell_worsening": comparison["no_cell_worsening"],
                "validation_gate": comparison["validation_gate"], "cohort": cell["cohort"],
                "arm": cell["arm"], "joint_mae_min": cell["participant_macro_joint_mae_min"],
                "gain_vs_current_min": cell["gain_vs_current_min"],
                "n_participants": cell["n_participants"], "n_nights": cell["n_nights"]})
    _write_csv(output_dir / "validation_candidates.csv", flat_validation)
    _write_csv(output_dir / "structural_alpha.csv", structural)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--predictions-csv", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bootstrap-replicates", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261105)
    args = parser.parse_args()
    result = run(args.input_csv.resolve(), args.predictions_csv.resolve(), args.protocol.resolve(),
                 args.output_dir.resolve(), replicates=args.bootstrap_replicates, seed=args.seed)
    print(json.dumps({"selected": result["selected_validation_alternative"],
                      "validation_gate_passed": result["validation_gate_passed"],
                      "locked_test_gate_passed": result["locked_test_gate_passed"],
                      "decision": result["decision"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
