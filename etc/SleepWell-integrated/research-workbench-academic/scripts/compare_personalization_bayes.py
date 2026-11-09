"""Compare Train-estimated empirical-Bayes residual calibration with production.

The protocol fixes all candidates before metrics are calculated. Population
hyperparameters are fitted on Train only, selection uses Validation only, and
the selected candidate is locked before Test is replayed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, variance
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
ACADEMIC = ROOT / "research_workbench_academic"
BASE_SCRIPT = ACADEMIC / "scripts" / "compare_personalization_settings.py"
NOV4_RESULT = ROOT / "reports" / "personalization_settings_20261104" / "comparison.json"
GROWTH_RESULT = ROOT / "reports" / "personalization_growth_20261105" / "comparison.json"
FROZEN_EVALUATION = ACADEMIC / "deliverables" / "gradual_v1_20261103" / "evaluation.json"
CANONICAL = ROOT / "work" / "dayone-personalization-20261027" / "web-app"
DEFAULT_INPUT = Path("E:/학술제/model/데이터모델/연구용데이터/gradual_v1_reassessment_20261103_input_v2/normalized_cmi_mesa_private.csv")
DEFAULT_PREDICTIONS = Path("E:/학술제/model/데이터모델/연구용데이터/gradual_v1_reassessment_20261103_results_final/private_predictions.csv")
DEFAULT_OUTPUT = ROOT / "reports" / "personalization_bayes_20261105"
DEFAULT_PROTOCOL = DEFAULT_OUTPUT / "protocol.json"
CURRENT = "current_h14_lambda14"
DISABLED = "disabled_correction"
POPULATION = "train_population_mean"
CANDIDATES = (
    "eb_static_intercept", "eb_h14_intercept", "kalman_random_walk_q01",
    "kalman_ou_h14", "train_global_affine", "individual_bayes_affine_h14",
)
COHORTS = ("cmi", "mesa")
ARMS = ("data", "hybrid")
CHANNELS = ("onset", "wake")
CLIP = 180.0
CAP = 60.0


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BASE = _load_module("sleepwell_nov4_bayes_base", BASE_SCRIPT)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clip(value: float) -> float:
    return min(CLIP, max(-CLIP, float(value)))


def cap(value: float) -> float:
    return min(CAP, max(-CAP, float(value)))


def _solve2(a: float, b: float, d: float, u: float, v: float) -> tuple[float, float]:
    determinant = a * d - b * b
    if not all(math.isfinite(x) for x in (a, b, d, u, v, determinant)) or determinant <= 1e-12:
        raise ValueError("non-positive or non-finite 2x2 precision")
    return ((d * u - b * v) / determinant, (a * v - b * u) / determinant)


def _channel_values(row: dict[str, Any], channel: str) -> tuple[float, float]:
    if channel == "onset":
        return float(row["base_bed"]), clip(row["actual_bed"] - row["base_bed"])
    return float(row["base_wake"]), clip(row["actual_wake"] - row["base_wake"])


def fit_hyperparameters(train_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit participant-balanced population quantities using Train only."""
    if not train_rows or any(row.get("split") != "train" for row in train_rows):
        raise ValueError("hyperparameters require non-empty Train-only rows")
    groups: dict[tuple[str, str, str], dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for row in train_rows:
        for channel in CHANNELS:
            base, residual = _channel_values(row, channel)
            groups[(row["cohort"], row["arm"], channel)][row["participant_id"]].append((base, residual))
    output: dict[str, Any] = {}
    for (cohort, arm, channel), people in sorted(groups.items()):
        key = f"{cohort}|{arm}|{channel}"
        person_means = [mean(v for _, v in values) for values in people.values()]
        counts = [len(values) for values in people.values()]
        within = [variance([v for _, v in values]) for values in people.values() if len(values) >= 2]
        r_raw = mean(within) if within else 0.0
        r = max(1.0, r_raw)
        between = variance(person_means) if len(person_means) >= 2 else 0.0
        t_raw = between - r * mean(1.0 / count for count in counts)
        t = max(1.0, t_raw)
        person_base_means = [mean(base for base, _ in values) for values in people.values()]
        person_base_seconds = [mean(base * base for base, _ in values) for values in people.values()]
        centre = mean(person_base_means)
        base_var = max(0.0, mean(person_base_seconds) - centre * centre)
        scale_raw = math.sqrt(base_var)
        scale = max(60.0, scale_raw)

        # Sum of participant-average squared errors + one participant-unit L2.
        s00 = s01 = s11 = q0 = q1 = 0.0
        for values in people.values():
            inv_n = 1.0 / len(values)
            for base, residual in values:
                x = (base - centre) / scale
                s00 += inv_n
                s01 += inv_n * x
                s11 += inv_n * x * x
                q0 += inv_n * residual
                q1 += inv_n * x * residual
        beta0, beta1 = _solve2(s00 + 1.0, s01, s11 + 1.0, q0, q1)
        output[key] = {
            "cohort": cohort, "arm": arm, "channel": channel,
            "mu": mean(person_means), "R_min2": r, "T_min2": t,
            "R_raw_min2": r_raw, "T_raw_min2": t_raw,
            "R_floor_applied": r_raw < 1.0, "T_floor_applied": t_raw < 1.0,
            "R_no_within_variance_fallback": not within,
            "n_participants": len(people), "n_rows": sum(counts),
            "n_participants_within_variance": len(within),
            "mean_inverse_n": mean(1.0 / count for count in counts),
            "intercept_prior_equivalent_full_weight_observations": r / t,
            "first_full_weight_observation_fraction": t / (t + r),
            "base_centre_min": centre, "base_scale_min": scale,
            "base_scale_raw_min": scale_raw, "base_scale_floor_applied": scale_raw < 60.0,
            "affine_beta0_min": beta0, "affine_beta1_per_sd": beta1,
            "affine_ridge_participant_units": 1.0,
        }
    if len(output) != len(COHORTS) * len(ARMS) * len(CHANNELS):
        raise ValueError("incomplete Train hyperparameter groups")
    return output


def group_hyper(hyper: dict[str, Any], cohort: str, arm: str, channel: str) -> dict[str, Any]:
    return hyper[f"{cohort}|{arm}|{channel}"]


def latest_eligible(history: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(history):
        try:
            available = BASE._time(row["available_at"])
            outcome = BASE._time(row.get("outcome_at", row["available_at"]))
            onset = float(row["onset_residual_min"])
            wake = float(row["wake_residual_min"])
            base_bed = float(row["base_bed"])
            base_wake = float(row["base_wake"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(v) for v in (onset, wake, base_bed, base_wake)):
            continue
        if available >= cutoff or outcome > available:
            continue
        night_id = str(row.get("night_id", f"__row_{index}"))
        item = {"available": available, "outcome": outcome, "onset": clip(onset), "wake": clip(wake),
                "base_bed": base_bed, "base_wake": base_wake, "night_id": night_id}
        prior = latest.get(night_id)
        if prior is None or (available, night_id) > (prior["available"], prior["night_id"]):
            latest[night_id] = item
    return sorted(latest.values(), key=lambda row: (row["outcome"], row["available"], row["night_id"]))


def eb_mean(mu: float, t: float, r: float, values: list[float], weights: list[float]) -> tuple[float, float]:
    precision = 1.0 / t + sum(weights) / r
    posterior_mean = (mu / t + sum(w * value for w, value in zip(weights, values)) / r) / precision
    return posterior_mean, 1.0 / precision


def kalman_mean(mu: float, t: float, r: float, rows: list[dict[str, Any]], channel: str,
                cutoff: datetime, *, mean_reverting: bool) -> tuple[float, float]:
    state, covariance = mu, t
    last_time: datetime | None = None
    for row in rows:
        if last_time is not None:
            dt = max(0.0, (row["outcome"] - last_time).total_seconds() / 86400.0)
            if mean_reverting:
                f = 2.0 ** (-dt / 14.0)
                state = mu + f * (state - mu)
                covariance = f * f * covariance + t * (1.0 - f * f)
            else:
                covariance += 0.01 * r * dt
        innovation_variance = covariance + r
        gain = covariance / innovation_variance
        state += gain * (row[channel] - state)
        covariance = (1.0 - gain) * covariance
        last_time = row["outcome"]
    if last_time is not None:
        dt = max(0.0, (cutoff - last_time).total_seconds() / 86400.0)
        if mean_reverting:
            f = 2.0 ** (-dt / 14.0)
            state = mu + f * (state - mu)
            covariance = f * f * covariance + t * (1.0 - f * f)
        else:
            covariance += 0.01 * r * dt
    return state, covariance


def affine_individual(h: dict[str, Any], rows: list[dict[str, Any]], channel: str,
                      cutoff: datetime, target_base: float) -> tuple[float, float, float, float, float]:
    t, r = h["T_min2"], h["R_min2"]
    b0, b1 = h["affine_beta0_min"], h["affine_beta1_per_sd"]
    a = d = 1.0 / t
    b = 0.0
    u, v = b0 / t, b1 / t
    xs = []
    for row in rows:
        age = max(0.0, (cutoff - row["outcome"]).total_seconds() / 86400.0)
        weight = 2.0 ** (-age / 14.0)
        base = row["base_bed"] if channel == "onset" else row["base_wake"]
        x = (base - h["base_centre_min"]) / h["base_scale_min"]
        y = row[channel]
        a += weight / r
        b += weight * x / r
        d += weight * x * x / r
        u += weight * y / r
        v += weight * x * y / r
        xs.append((x, weight))
    p0, p1 = _solve2(a, b, d, u, v)
    x_target = (target_base - h["base_centre_min"]) / h["base_scale_min"]
    determinant = a * d - b * b
    correction_variance = (d - 2.0 * b * x_target + a * x_target * x_target) / determinant
    weighted_mass = sum(weight for _, weight in xs)
    x_sd = None
    if weighted_mass > 0:
        x_mean = sum(x * weight for x, weight in xs) / weighted_mass
        x_sd = math.sqrt(max(0.0, sum(weight * (x - x_mean) ** 2 for x, weight in xs) / weighted_mass))
    return p0 + p1 * x_target, p0, p1, x_sd if x_sd is not None else 0.0, correction_variance


def corrections(history: list[dict[str, Any]], cutoff: datetime, row: dict[str, Any],
                setting: str, hyper: dict[str, Any], adaptive: Any) -> dict[str, Any]:
    eligible = latest_eligible(history, cutoff)
    gap_days = ((cutoff - eligible[-1]["outcome"]).total_seconds() / 86400.0) if eligible else None
    if setting == CURRENT:
        production_history = [{key: value for key, value in item.items()
                               if key not in {"base_bed", "base_wake"}} for item in history]
        prod = adaptive.compute_adjustment(production_history, cutoff)
        return {"onset": prod["onset_correction_min"], "wake": prod["wake_correction_min"],
                "eligible": prod["eligible_count"], "mass": prod["decayed_mass"],
                "variance": None, "cap_hits": int(abs(prod["onset_correction_min"]) >= CAP) +
                int(abs(prod["wake_correction_min"]) >= CAP), "affine_x_sd": None,
                "gap_days": gap_days}
    if setting == DISABLED:
        return {"onset": 0.0, "wake": 0.0, "eligible": len(eligible), "mass": 0.0,
                "variance": None, "cap_hits": 0, "affine_x_sd": None, "gap_days": gap_days}

    output: dict[str, Any] = {"eligible": len(eligible), "mass": 0.0, "cap_hits": 0,
                              "variance": [], "affine_x_sd": [], "gap_days": gap_days}
    for channel in CHANNELS:
        h = group_hyper(hyper, row["cohort"], row["arm"], channel)
        target_base = row["base_bed"] if channel == "onset" else row["base_wake"]
        if setting == POPULATION:
            raw, posterior_var = h["mu"], h["T_min2"]
        elif setting == "train_global_affine":
            x = (target_base - h["base_centre_min"]) / h["base_scale_min"]
            raw = h["affine_beta0_min"] + h["affine_beta1_per_sd"] * x
            posterior_var = h["R_min2"]
        elif setting in {"eb_static_intercept", "eb_h14_intercept"}:
            values = [item[channel] for item in eligible]
            if setting == "eb_static_intercept":
                weights = [1.0] * len(eligible)
            else:
                weights = [2.0 ** (-max(0.0, (cutoff - item["outcome"]).total_seconds() / 86400.0) / 14.0)
                           for item in eligible]
            raw, posterior_var = eb_mean(h["mu"], h["T_min2"], h["R_min2"], values, weights)
            output["mass"] = max(output["mass"], sum(weights))
        elif setting in {"kalman_random_walk_q01", "kalman_ou_h14"}:
            raw, posterior_var = kalman_mean(h["mu"], h["T_min2"], h["R_min2"], eligible,
                                             channel, cutoff, mean_reverting=setting == "kalman_ou_h14")
        elif setting == "individual_bayes_affine_h14":
            raw, p0, p1, x_sd, posterior_var = affine_individual(
                h, eligible, channel, cutoff, target_base)
            output["affine_x_sd"].append(x_sd)
            output.setdefault("posterior_beta", []).append([p0, p1])
        else:
            raise ValueError(f"unknown setting: {setting}")
        bounded = cap(raw)
        output[channel] = bounded
        output["cap_hits"] += int(abs(raw) > CAP)
        output["variance"].append(posterior_var)
    if output["affine_x_sd"] == []:
        output["affine_x_sd"] = None
    return output


def replay(rows: list[dict[str, Any]], settings: Iterable[str], hyper: dict[str, Any],
           *, verify_production: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    adaptive, night_core = BASE._load_production_modules()
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["cohort"], row["participant_id"], row["arm"])].append(row)
    results = []
    parity_max = 0.0
    for subject_rows in grouped.values():
        subject_rows.sort(key=lambda item: item["cutoff"])
        history: list[dict[str, Any]] = []
        for row in subject_rows:
            for setting in settings:
                change = corrections(history, row["cutoff"], row, setting, hyper, adaptive)
                bed, wake = BASE.constrain(row["base_bed"] + change["onset"], row["base_wake"] + change["wake"])
                if verify_production and setting == CURRENT:
                    prod_history = [{key: value for key, value in item.items()
                                     if key not in {"base_bed", "base_wake"}} for item in history]
                    prod = adaptive.compute_adjustment(prod_history, row["cutoff"])
                    pb, pw = night_core.constrain(row["base_bed"] + prod["onset_correction_min"],
                                                   row["base_wake"] + prod["wake_correction_min"])
                    parity_max = max(parity_max, abs(bed - pb), abs(wake - pw))
                bed_error, wake_error = abs(bed - row["actual_bed"]), abs(wake - row["actual_wake"])
                results.append({"cohort": row["cohort"], "participant_id": row["participant_id"],
                    "split": row["split"], "arm": row["arm"], "setting": setting,
                    "joint_error": (bed_error + wake_error) / 2.0, "bed_error": bed_error,
                    "wake_error": wake_error, "eligible": change["eligible"], "mass": change["mass"],
                    "cap_hits": change["cap_hits"], "variance": change["variance"],
                    "affine_x_sd": change["affine_x_sd"], "gap_days": change["gap_days"]})
            history.append({"night_id": row["row_id"], "available_at": row["observed_at"].isoformat(),
                "outcome_at": row["wake_time"].isoformat(),
                "onset_residual_min": row["actual_bed"] - row["base_bed"],
                "wake_residual_min": row["actual_wake"] - row["base_wake"],
                "base_bed": row["base_bed"], "base_wake": row["base_wake"]})
    return results, {"verified": verify_production and parity_max <= 1e-12,
                     "max_abs_difference_min": parity_max, "tolerance_min": 1e-12}


def metric_rows(records: list[dict[str, Any]], split: str, setting: str) -> list[dict[str, Any]]:
    cells = BASE.participant_values(records, split, setting)
    output = []
    for cohort in COHORTS:
        for arm in ARMS:
            matches = [row for row in records if row["split"] == split and row["setting"] == setting
                       and row["cohort"] == cohort and row["arm"] == arm]
            values = cells[(cohort, arm)]
            output.append({"split": split, "setting": setting, "cohort": cohort, "arm": arm,
                "participant_macro_joint_mae_min": mean(values.values()),
                "n_participants": len(values), "n_nights": len(matches),
                "correction_cap_hit_rate": sum(row["cap_hits"] for row in matches) / (2 * len(matches))})
    return output


def choose_candidate(records: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    current = {(row["cohort"], row["arm"]): row for row in metric_rows(records, "validation", CURRENT)}
    comparisons = []
    for setting in CANDIDATES:
        cells = metric_rows(records, "validation", setting)
        gains = []
        for row in cells:
            gain = current[(row["cohort"], row["arm"])]["participant_macro_joint_mae_min"] - row["participant_macro_joint_mae_min"]
            gains.append(gain)
            row["gain_vs_current_min"] = gain
        aggregate = mean(gains)
        no_worsening = all(gain >= -1e-12 for gain in gains)
        comparisons.append({"setting": setting, "aggregate_gain_vs_current_min": aggregate,
            "no_cell_worsening": no_worsening,
            "validation_gate": aggregate >= 1.0 and no_worsening, "cells": cells})
    selected = max(comparisons, key=lambda item: (item["aggregate_gain_vs_current_min"], item["setting"]))
    return selected["setting"], comparisons, bool(selected["validation_gate"])


def history_label(n: int) -> str:
    if n == 0: return "0"
    if n <= 6: return "1-6"
    if n <= 13: return "7-13"
    if n <= 27: return "14-27"
    if n <= 55: return "28-55"
    return "56+"


def gap_label(days: float | None) -> str:
    if days is None: return "no_history"
    if days < 2: return "<2d"
    if days < 7: return "2-<7d"
    if days < 14: return "7-<14d"
    if days < 28: return "14-<28d"
    return "28d+"


def diagnostics(records: list[dict[str, Any]], split: str, settings: Iterable[str]) -> dict[str, Any]:
    strata = []
    gaps = []
    uncertainty = []
    affine_coverage = []
    for setting in settings:
        for cohort in COHORTS:
            rows = [row for row in records if row["split"] == split and row["setting"] == setting
                    and row["cohort"] == cohort]
            for label in ("0", "1-6", "7-13", "14-27", "28-55", "56+"):
                chosen = [row for row in rows if history_label(row["eligible"]) == label]
                strata.append({"split": split, "setting": setting, "cohort": cohort,
                               "history_stratum": label, "n_predictions_across_arms": len(chosen)})
            for label in ("no_history", "<2d", "2-<7d", "7-<14d", "14-<28d", "28d+"):
                chosen = [row for row in rows if gap_label(row["gap_days"]) == label]
                gaps.append({"split": split, "setting": setting, "cohort": cohort,
                             "last_outcome_gap_stratum": label,
                             "n_predictions_across_arms": len(chosen)})
            learned = [value for row in rows if isinstance(row["variance"], list)
                       for value in row["variance"] if value is not None and math.isfinite(value)]
            uncertainty.append({"split": split, "setting": setting, "cohort": cohort,
                "n_channel_variances": len(learned),
                "mean_posterior_correction_variance_min2": mean(learned) if learned else None,
                "interpretation": "model-based uncalibrated variance diagnostic; not a probability guarantee"})
            if setting == "individual_bayes_affine_h14":
                affine_coverage.append({"split": split, "cohort": cohort,
                    "n_predictions_across_arms": len(rows),
                    "n_history_ge14": sum(row["eligible"] >= 14 for row in rows),
                    "n_history_ge14_and_both_channel_weighted_x_sd_ge_0_5": sum(
                        row["eligible"] >= 14 and row["affine_x_sd"] is not None and
                        min(row["affine_x_sd"]) >= 0.5 for row in rows)})
    return {"history_counts": strata, "last_outcome_gap_counts": gaps,
            "learned_uncertainty": uncertainty,
            "individual_affine_design_coverage": affine_coverage}


def _protocol_matches(protocol: dict[str, Any]) -> None:
    expected = {"schema": "sleepwell.personalization_bayes_protocol.v1",
                "status": "locally_prespecified_before_comparison",
                "cohorts": list(COHORTS), "arms": list(ARMS), "current": CURRENT,
                "selectable_candidates": list(CANDIDATES), "controls": [DISABLED, POPULATION]}
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"protocol mismatch: {key}")
    required = {
        "train_hyperparameters": {
            "residual_clip_min": 180.0, "variance_floor_min2": 1.0,
            "R": "equal mean of within-participant sample variances among participants with n>=2; if none exist use the 1 min^2 floor and flag the fallback",
            "T": "max(sample variance of participant means - R*mean(1/n), 1)",
            "individual_affine_prior": "Train global affine mean with diagonal covariance diag(T,T); engineering prior, not a slope variance-component estimate",
        },
        "fixed_bounds": {"residual_clip_min": 180.0, "correction_cap_min": 60.0,
                         "final_schedule_constraint": "production night_core.constrain"},
        "selection": {"split": "validation",
                      "metric": "equal mean of participant-macro joint MAE across cmi/mesa and data/hybrid",
                      "rule": "largest aggregate gain versus current",
                      "validation_gate": "aggregate gain >= 1 minute and no worsening in any of four cohort-arm cells"},
        "locked_test_gate": {"split": "test", "replicates": 10000, "seed": 20261105,
            "cell_interval": "two-sided 98.75% paired participant bootstrap CI",
            "pass": "validation gate passes, Test aggregate gain >= 1 minute, and every cell CI lower bound > 0"},
    }
    for section, values in required.items():
        actual = protocol.get(section, {})
        for key, value in values.items():
            if actual.get(key) != value:
                raise ValueError(f"protocol mismatch: {section}.{key}")
    if set(protocol.get("candidate_formulas", {})) != set(CANDIDATES):
        raise ValueError("protocol mismatch: candidate_formulas")
    expected_formulas = {
        "eb_static_intercept": "Gaussian posterior N(mu,T) with unweighted eligible personal residual likelihood variance R",
        "eb_h14_intercept": "Gaussian posterior with 14-day exponential weights treated as fractional likelihood",
        "kalman_random_walk_q01": "scalar state prior N(mu,T), process variance 0.01*R*elapsed calendar days, observation variance R",
        "kalman_ou_h14": "mean-reverting scalar state F=2^(-elapsed_days/14), process variance T*(1-F^2), observation variance R",
        "train_global_affine": "Train-only ridge correction beta0 + beta1*x",
        "individual_bayes_affine_h14": "Train affine coefficient prior updated with eligible personal z=[1,x] residuals using 14-day fractional likelihood",
    }
    if protocol["candidate_formulas"] != expected_formulas:
        raise ValueError("protocol mismatch: candidate formula text")


def verify_frozen_baseline(disabled_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    frozen = json.loads(FROZEN_EVALUATION.read_text(encoding="utf-8"))
    lookup = {(row["split"], row["cohort"], row["arm"]): row for row in frozen["metrics"]
              if row["policy"] == "gradual_v1" and row["weighting"] == "participant" and
              row["metric"] == "joint_mae_min" and row["arm"] in ARMS and
              row["split"] in {"validation", "test"}}
    cells, maximum = [], 0.0
    for row in disabled_metrics:
        difference = abs(row["participant_macro_joint_mae_min"] -
                         lookup[(row["split"], row["cohort"], row["arm"])]["value"])
        maximum = max(maximum, difference)
        cells.append({"split": row["split"], "cohort": row["cohort"], "arm": row["arm"],
                      "difference_min": difference})
    return {"verified": maximum <= 1e-9, "max_abs_difference_min": maximum,
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
        raise ValueError("bootstrap arguments differ from protocol")
    nov4 = json.loads(NOV4_RESULT.read_text(encoding="utf-8"))
    if sha256(input_path) != nov4["provenance"]["input_csv"]["sha256"]:
        raise ValueError("input hash changed")
    if sha256(prediction_path) != nov4["provenance"]["frozen_predictions"]["sha256"]:
        raise ValueError("frozen prediction hash changed")
    if sha256(BASE_SCRIPT) != nov4["provenance"]["comparison_script"]["sha256"]:
        raise ValueError("reused Nov4 evaluator changed")
    expected_sources = {Path(item["path"]).name: item["sha256"]
                        for item in nov4["provenance"]["production_sources_at_evaluation"]}
    for name in ("adaptive_personalization.py", "night_core.py"):
        if sha256(CANONICAL / name) != expected_sources[name]:
            raise ValueError(f"production source changed since Nov4 baseline: {name}")
    frozen = json.loads(FROZEN_EVALUATION.read_text(encoding="utf-8"))
    model_verification = []
    for source in frozen.get("sources", []):
        if not source.get("role", "").startswith("frozen_model_"):
            continue
        cohort = source["role"].removeprefix("frozen_model_")
        path = ACADEMIC / "vendor" / "sleepwell" / "models" / f"robust_{cohort}_v2.json"
        actual_hash = sha256(path)
        if actual_hash != source["sha256"]:
            raise ValueError(f"frozen model hash mismatch: {cohort}")
        model_verification.append({"role": source["role"], "path": str(path),
            "expected_sha256": source["sha256"], "actual_sha256": actual_hash, "verified": True})
    rows = BASE.load_rows(input_path, prediction_path)
    train_rows = [row for row in rows if row["split"] == "train"]
    validation_rows = [row for row in rows if row["split"] == "validation"]
    test_rows = [row for row in rows if row["split"] == "test"]
    hyper = fit_hyperparameters(train_rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    hyper_path = output_dir / "hyperparameters.json"
    hyper_path.write_text(json.dumps({"schema": "sleepwell.personalization_bayes_hyperparameters.v1",
        "fit_split": "train", "groups": hyper}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    validation_settings = (CURRENT, *CANDIDATES, DISABLED, POPULATION)
    validation_predictions, val_parity = replay(validation_rows, validation_settings, hyper, verify_production=True)
    if not val_parity["verified"]:
        raise ValueError("validation production parity failed")
    selected, selection, validation_gate = choose_candidate(validation_predictions)
    lock = {"schema": "sleepwell.personalization_bayes_selection_lock.v1", "locked_before_test_replay": True,
        "selected_validation_alternative": selected, "validation_gate_passed": validation_gate,
        "protocol_sha256": sha256(protocol_path), "hyperparameters_sha256": sha256(hyper_path),
        "input_sha256": sha256(input_path), "frozen_predictions_sha256": sha256(prediction_path)}
    lock_path = output_dir / "selection_lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    test_settings = [CURRENT, selected, DISABLED, POPULATION]
    if selected == "individual_bayes_affine_h14":
        test_settings.append("train_global_affine")
    test_predictions, test_parity = replay(test_rows, tuple(dict.fromkeys(test_settings)), hyper, verify_production=True)
    if not test_parity["verified"]:
        raise ValueError("test production parity failed")

    val_metrics = [cell for setting in validation_settings for cell in metric_rows(validation_predictions, "validation", setting)]
    test_metrics = [cell for setting in dict.fromkeys(test_settings) for cell in metric_rows(test_predictions, "test", setting)]
    translated = [{**row, "setting": BASE.CURRENT_ID if row["setting"] == CURRENT else row["setting"]}
                  for row in test_predictions if row["setting"] in {CURRENT, selected}]
    cell_bootstrap, aggregate_bootstrap = BASE.bootstrap_gains(translated, selected, replicates=replicates, seed=seed)
    test_gate = (validation_gate and aggregate_bootstrap["gain_vs_current_min"] >= 1.0 and
                 all(row["simultaneous_cell_pass"] for row in cell_bootstrap))
    disabled_metrics = [row for row in (*val_metrics, *test_metrics) if row["setting"] == DISABLED]
    # Reuse the independently checked frozen aggregate contract from growth evaluator.
    disabled_anchor = verify_frozen_baseline(disabled_metrics)
    if not disabled_anchor["verified"]:
        raise ValueError("new disabled replay differs from frozen evaluation")
    availability = {}
    for cohort in COHORTS:
        cohort_rows = [row for row in rows if row["cohort"] == cohort]
        unique_observations = {(row["row_id"], row["observed_at"], row["wake_time"])
                               for row in cohort_rows}
        availability[cohort] = {"n_base_prediction_rows_across_arms": len(cohort_rows),
            "n_unique_source_observations": len(unique_observations),
            "n_unique_observed_at_equals_wake": sum(observed == wake
                for _, observed, wake in unique_observations),
            "all_observed_at_equals_wake": all(row["observed_at"] == row["wake_time"] for row in cohort_rows),
            "interpretation": "offline wake-time availability assumption, not measured reporting or synchronization delay"}
    result = {"schema": "sleepwell.personalization_bayes_comparison.v1",
        "status": "retrospective_reused_holdout", "scope": protocol["scope"],
        "selected_validation_alternative": selected, "validation_gate_passed": validation_gate,
        "locked_test_gate_passed": test_gate,
        "decision": "change_supported_for_residual_layer_only" if test_gate else "retain_current_no_verified_improvement",
        "production_current_parity": {"validation": val_parity, "test": test_parity},
        "disabled_baseline_anchor": disabled_anchor,
        "availability_contract_audit": availability,
        "validation_selection": selection, "validation_metrics": val_metrics,
        "test_metrics_opened_after_selection_lock": test_metrics,
        "test_paired_participant_bootstrap": {"replicates": replicates, "seed": seed,
            "cell_intervals": cell_bootstrap, "equal_cell_aggregate": aggregate_bootstrap},
        "diagnostics": {"validation": diagnostics(validation_predictions, "validation", validation_settings),
                        "test": diagnostics(test_predictions, "test", test_settings)},
        "row_counts": {"train_base_prediction_rows": len(train_rows),
                       "validation_base_prediction_rows": len(validation_rows),
                       "test_base_prediction_rows_after_selection_lock": len(test_rows)},
        "provenance": {"input_csv": {"path": str(input_path), "sha256": sha256(input_path)},
            "frozen_predictions": {"path": str(prediction_path), "sha256": sha256(prediction_path)},
            "protocol": {"path": str(protocol_path), "sha256": sha256(protocol_path)},
            "hyperparameters": {"path": str(hyper_path), "sha256": sha256(hyper_path)},
            "selection_lock": {"path": str(lock_path), "sha256": sha256(lock_path)},
            "comparison_script": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
            "reused_nov4_script": {"path": str(BASE_SCRIPT), "sha256": sha256(BASE_SCRIPT), "unchanged": True},
            "production_sources_at_evaluation": [
                {"path": str(CANONICAL / name), "sha256": sha256(CANONICAL / name), "unchanged": True}
                for name in ("adaptive_personalization.py", "night_core.py")],
            "frozen_model_hash_verification": model_verification,
            "frozen_evaluation": {"path": str(FROZEN_EVALUATION), "sha256": sha256(FROZEN_EVALUATION)},
            "nov4_comparison": {"path": str(NOV4_RESULT), "sha256": sha256(NOV4_RESULT)},
            "growth_comparison": {"path": str(GROWTH_RESULT), "sha256": sha256(GROWTH_RESULT)}},
        "interpretation_limits": protocol["interpretation_limits"]}
    comparison_path = output_dir / "comparison.json"
    comparison_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flat = []
    for comparison in selection:
        for cell in comparison["cells"]:
            flat.append({"setting": comparison["setting"], "aggregate_gain_vs_current_min": comparison["aggregate_gain_vs_current_min"],
                "no_cell_worsening": comparison["no_cell_worsening"], "validation_gate": comparison["validation_gate"],
                "cohort": cell["cohort"], "arm": cell["arm"], "joint_mae_min": cell["participant_macro_joint_mae_min"],
                "gain_vs_current_min": cell["gain_vs_current_min"], "n_participants": cell["n_participants"], "n_nights": cell["n_nights"]})
    _write_csv(output_dir / "validation_candidates.csv", flat)
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
    print(json.dumps({key: result[key] for key in ("selected_validation_alternative", "validation_gate_passed",
                                                    "locked_test_gate_passed", "decision")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
