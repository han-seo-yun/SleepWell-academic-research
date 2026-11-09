"""Compare Bayesian and partial-coefficient midpoint updates on one long record.

This is a behavioral sleep-midpoint proxy analysis. It does not estimate
physiological parameters, reproduce the production recommendation, or establish
population benefit. Hyperparameters are learned from Train only, candidates are
selected on Validation, and Test is opened only after a selection lock is written.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LONG_SCRIPT = ROOT / "research_workbench_academic" / "scripts" / "compare_personalization_long_history.py"
DEFAULT_INPUT = Path("E:/학술제/model/데이터모델/미래예측모델/output/causal_daily_features.parquet")
DEFAULT_OUTPUT = ROOT / "reports" / "personalization_bayes_20261105" / "long_history"
DEFAULT_PROTOCOL = DEFAULT_OUTPUT / "protocol.json"
CURRENT = "current_mass_lambda14"
CANDIDATES = ("eb_constant_offset", "kalman_qr_0_001", "kalman_qr_0_01",
              "kalman_qr_0_1", "affine_bayes")
KALMAN_RATIOS = {"kalman_qr_0_001": 0.001, "kalman_qr_0_01": 0.01,
                  "kalman_qr_0_1": 0.1}
VARIANCE_FLOOR = 25.0
SEAM_GUARD_MIN = 600.0
OFFSET_RANGE_GUARD_MIN = 720.0


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LONG = _load_module("sleepwell_long_history_base", LONG_SCRIPT)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def population_variance(values: list[float]) -> float:
    if not values:
        raise ValueError("variance requires observations")
    center = mean(values)
    return mean((value - center) ** 2 for value in values)


def prior_dates(rows: list[dict[str, Any]]) -> set[datetime]:
    train = [row for row in rows if row["split"] == "train"]
    if len(train) < 7:
        raise ValueError("seven Train nights are required")
    return {row["forecast"] for row in train[:7]}


def post_prior_history(rows: list[dict[str, Any]], cutoff: datetime,
                       excluded_dates: set[datetime]) -> list[dict[str, Any]]:
    return [row for row in LONG.eligible_history(rows, cutoff)
            if row["forecast"] not in excluded_dates]


def weighted_history_feature(rows: list[dict[str, Any]], target: dict[str, Any],
                             prior: float) -> tuple[float, float, float]:
    history = LONG.eligible_history(rows, target["cutoff"])
    ages = [(target["cutoff"] - row["end"]).total_seconds() / 86400.0 for row in history]
    if not history or any(age <= 0 for age in ages):
        raise ValueError("target must have positive-age history")
    weights = [2.0 ** (-age / 14.0) for age in ages]
    summary = LONG.circular_mean([row["target_midpoint"] for row in history], weights)
    return LONG.circular_delta(summary, prior), sum(weights), min(ages)


def affine_pair(rows: list[dict[str, Any]], row: dict[str, Any], prior: float) -> tuple[float, float]:
    x, _, _ = weighted_history_feature(rows, row, prior)
    y = LONG.circular_delta(row["target_midpoint"], prior)
    return x, y


def calibrate_train(rows: list[dict[str, Any]], prior: float,
                    excluded_dates: set[datetime]) -> dict[str, float]:
    later_train = [row for row in rows if row["split"] == "train" and
                   row["forecast"] not in excluded_dates]
    if len(later_train) < 3:
        raise ValueError("insufficient later Train observations")
    ys = [LONG.circular_delta(row["target_midpoint"], prior) for row in later_train]
    max_abs_y = max(abs(value) for value in ys)
    offset_range = max(ys) - min(ys)
    if max_abs_y >= SEAM_GUARD_MIN or offset_range >= OFFSET_RANGE_GUARD_MIN:
        raise ValueError(
            "Gaussian circular approximation is unsupported near the circular seam: "
            f"max_abs={max_abs_y:.3f}, range={offset_range:.3f}"
        )
    observation_variance_raw = 0.5 * mean(
        (right - left) ** 2 for left, right in zip(ys, ys[1:]))
    observation_variance = max(VARIANCE_FLOOR, observation_variance_raw)
    state_variance_raw = population_variance(ys) - observation_variance
    state_variance = max(VARIANCE_FLOOR, state_variance_raw)
    pairs = [affine_pair(rows, row, prior) for row in later_train]
    xs = [pair[0] for pair in pairs]
    identity_residuals = [y - x for x, y in pairs]
    affine_observation_variance_raw = mean(value * value for value in identity_residuals)
    affine_observation_variance = max(VARIANCE_FLOOR, affine_observation_variance_raw)
    x_variance_raw = population_variance(xs)
    x_variance = max(VARIANCE_FLOOR, x_variance_raw)
    slope_variance_raw = state_variance / x_variance
    slope_variance = max(0.01, slope_variance_raw)
    return {
        "observation_variance_min2": observation_variance,
        "observation_variance_raw_min2": observation_variance_raw,
        "observation_variance_floor_applied": observation_variance_raw < VARIANCE_FLOOR,
        "constant_state_variance_min2": state_variance,
        "constant_state_variance_raw_min2": state_variance_raw,
        "constant_state_variance_floor_applied": state_variance_raw < VARIANCE_FLOOR,
        "affine_observation_variance_min2": affine_observation_variance,
        "affine_observation_variance_raw_min2": affine_observation_variance_raw,
        "affine_observation_variance_floor_applied": (
            affine_observation_variance_raw < VARIANCE_FLOOR),
        "affine_intercept_prior_variance_min2": state_variance,
        "affine_slope_prior_variance": slope_variance,
        "affine_slope_prior_variance_raw": slope_variance_raw,
        "affine_slope_variance_floor_applied": slope_variance_raw < 0.01,
        "train_post_prior_n": len(later_train),
        "train_max_abs_offset_min": max_abs_y,
        "train_offset_range_min": offset_range,
        "train_x_variance_min2": x_variance_raw,
        "affine_x_variance_floor_applied": x_variance_raw < VARIANCE_FLOOR,
        "variance_floor_min2": VARIANCE_FLOOR,
    }


def eb_constant(history: list[dict[str, Any]], prior: float,
                calibration: dict[str, float]) -> tuple[float, dict[str, float]]:
    ys = [LONG.circular_delta(row["target_midpoint"], prior) for row in history]
    state_variance = calibration["constant_state_variance_min2"]
    observation_variance = calibration["observation_variance_min2"]
    precision = 1.0 / state_variance + len(ys) / observation_variance
    posterior_variance = 1.0 / precision
    posterior_mean = posterior_variance * sum(ys) / observation_variance
    return posterior_mean, {
        "posterior_variance": posterior_variance, "gain": posterior_variance / observation_variance,
        "beta0": posterior_mean, "beta1": 0.0,
    }


def kalman_offset(history: list[dict[str, Any]], cutoff: datetime, prior: float,
                  calibration: dict[str, float], ratio: float,
                  initial_time: datetime) -> tuple[float, dict[str, float]]:
    state = 0.0
    variance = calibration["constant_state_variance_min2"]
    observation_variance = calibration["observation_variance_min2"]
    process_per_day = ratio * observation_variance
    last_time = initial_time
    last_gain = 0.0
    for row in history:
        elapsed = max(0.0, (row["end"] - last_time).total_seconds() / 86400.0)
        variance += process_per_day * elapsed
        gain = variance / (variance + observation_variance)
        observation = LONG.circular_delta(row["target_midpoint"], prior)
        state += gain * (observation - state)
        variance *= 1.0 - gain
        last_gain = gain
        last_time = row["end"]
    variance += process_per_day * max(0.0, (cutoff - last_time).total_seconds() / 86400.0)
    return state, {
        "posterior_variance": variance, "gain": last_gain,
        "beta0": state, "beta1": 0.0,
    }


def affine_posterior(pairs: list[tuple[float, float]], x_target: float,
                     calibration: dict[str, float]) -> tuple[float, dict[str, float]]:
    intercept_variance = calibration["affine_intercept_prior_variance_min2"]
    slope_variance = calibration["affine_slope_prior_variance"]
    observation_variance = calibration["affine_observation_variance_min2"]
    a00 = 1.0 / intercept_variance
    a01 = 0.0
    a11 = 1.0 / slope_variance
    b0 = 0.0
    b1 = 1.0 / slope_variance
    for x, y in pairs:
        a00 += 1.0 / observation_variance
        a01 += x / observation_variance
        a11 += x * x / observation_variance
        b0 += y / observation_variance
        b1 += x * y / observation_variance
    determinant = a00 * a11 - a01 * a01
    if determinant <= 0 or not math.isfinite(determinant):
        raise ValueError("affine posterior is not identifiable")
    covariance00 = a11 / determinant
    covariance01 = -a01 / determinant
    covariance11 = a00 / determinant
    beta0 = covariance00 * b0 + covariance01 * b1
    beta1 = covariance01 * b0 + covariance11 * b1
    prediction = beta0 + beta1 * x_target
    prediction_variance = (covariance00 + 2.0 * x_target * covariance01 +
                           x_target * x_target * covariance11)
    return prediction, {
        "posterior_variance": max(0.0, prediction_variance),
        "gain": float("nan"), "beta0": beta0, "beta1": beta1,
    }


def predict(rows: list[dict[str, Any]], target: dict[str, Any], prior: float,
            setting: str, calibration: dict[str, float],
            excluded_dates: set[datetime]) -> dict[str, Any]:
    x_target, mass, gap = weighted_history_feature(rows, target, prior)
    full_history = LONG.eligible_history(rows, target["cutoff"])
    history = [row for row in full_history if row["forecast"] not in excluded_dates]
    if setting == CURRENT:
        weight = mass / (mass + 14.0)
        delta = weight * x_target
        diagnostics = {"posterior_variance": None, "gain": weight,
                       "beta0": 0.0, "beta1": weight}
    elif setting == "eb_constant_offset":
        delta, diagnostics = eb_constant(history, prior, calibration)
    elif setting in KALMAN_RATIOS:
        prior_rows = [row for row in rows if row["forecast"] in excluded_dates]
        delta, diagnostics = kalman_offset(
            history, target["cutoff"], prior, calibration, KALMAN_RATIOS[setting],
            max(row["end"] for row in prior_rows),
        )
    elif setting == "affine_bayes":
        pairs = [affine_pair(rows, row, prior) for row in history]
        delta, diagnostics = affine_posterior(pairs, x_target, calibration)
    else:
        raise ValueError(f"unknown setting: {setting}")
    predicted = (prior + delta) % 1440.0
    error = abs(LONG.circular_delta(predicted, target["target_midpoint"]))
    return {
        "setting": setting, "split": target["split"], "error_min": error,
        "history_count": len(full_history), "update_count": len(history),
        "decayed_mass": mass, "last_gap_days": gap,
        "predicted_offset_min": LONG.circular_delta(predicted, prior),
        **diagnostics,
    }


def replay(rows: list[dict[str, Any]], split: str, prior: float,
           settings: tuple[str, ...], calibration: dict[str, float],
           excluded_dates: set[datetime]) -> list[dict[str, Any]]:
    return [predict(rows, target, prior, setting, calibration, excluded_dates)
            for target in rows if target["split"] == split for setting in settings]


def metrics(records: list[dict[str, Any]], setting: str) -> dict[str, Any]:
    selected = [row for row in records if row["setting"] == setting]
    if not selected:
        raise ValueError(f"missing records for {setting}")
    finite_variances = [row["posterior_variance"] for row in selected
                        if row["posterior_variance"] is not None]
    finite_gains = [row["gain"] for row in selected if math.isfinite(row["gain"])]
    return {
        "setting": setting, "n_nights": len(selected),
        "mean_absolute_error_min": mean(row["error_min"] for row in selected),
        "median_absolute_error_min": median(row["error_min"] for row in selected),
        "mean_update_count": mean(row["update_count"] for row in selected),
        "mean_decayed_mass": mean(row["decayed_mass"] for row in selected),
        "mean_model_variance_diagnostic": mean(finite_variances) if finite_variances else None,
        "mean_update_gain": mean(finite_gains) if finite_gains else None,
        "mean_beta0": mean(row["beta0"] for row in selected),
        "mean_beta1": mean(row["beta1"] for row in selected),
    }


def paired_summary(records: list[dict[str, Any]], alternative: str,
                   predicate=lambda row: True) -> dict[str, Any]:
    current = [row for row in records if row["setting"] == CURRENT and predicate(row)]
    candidate = [row for row in records if row["setting"] == alternative and predicate(row)]
    if len(current) != len(candidate):
        raise ValueError("paired coverage mismatch")
    gains = [left["error_min"] - right["error_min"] for left, right in zip(current, candidate)]
    return {
        "n_nights": len(gains), "mean_gain_min": mean(gains) if gains else None,
        "median_gain_min": median(gains) if gains else None,
        "improved_nights": sum(value > 1e-12 for value in gains),
        "equal_nights": sum(abs(value) <= 1e-12 for value in gains),
        "worsened_nights": sum(value < -1e-12 for value in gains),
    }


def choose_candidate(validation: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    if not validation or any(row["split"] != "validation" for row in validation):
        raise ValueError("candidate selection requires validation only")
    current = metrics(validation, CURRENT)
    comparisons = []
    for setting in CANDIDATES:
        item = metrics(validation, setting)
        item["gain_vs_current_min"] = (current["mean_absolute_error_min"] -
                                        item["mean_absolute_error_min"])
        comparisons.append(item)
    selected = min(comparisons, key=lambda row: (row["mean_absolute_error_min"],
                                                  CANDIDATES.index(row["setting"])))
    return selected["setting"], comparisons


def diagnostics(records: list[dict[str, Any]], selected: str) -> dict[str, Any]:
    current = [row for row in records if row["setting"] == CURRENT]
    gaps = {
        "gap_lt_7": lambda row: row["last_gap_days"] < 7,
        "gap_7_to_27": lambda row: 7 <= row["last_gap_days"] < 28,
        "gap_ge_28": lambda row: row["last_gap_days"] >= 28,
    }
    return {
        "history_count": {"min": min(row["history_count"] for row in current),
                          "max": max(row["history_count"] for row in current)},
        "update_count": {"min": min(row["update_count"] for row in current),
                         "max": max(row["update_count"] for row in current)},
        "last_gap_days": {"min": min(row["last_gap_days"] for row in current),
                          "median": median(row["last_gap_days"] for row in current),
                          "max": max(row["last_gap_days"] for row in current)},
        "gap_strata": {name: paired_summary(records, selected, predicate)
                       for name, predicate in gaps.items()},
    }


def _protocol_matches(protocol: dict[str, Any]) -> None:
    if protocol.get("schema") != "sleepwell.bayes_long_history_protocol.v1":
        raise ValueError("protocol schema mismatch")
    if protocol.get("status") != "locally_prespecified_before_comparison":
        raise ValueError("protocol status mismatch")
    if [row.get("id") for row in protocol.get("alternatives", [])] != list(CANDIDATES):
        raise ValueError("protocol candidates mismatch")
    expected_variance = "max(25, 0.5*mean consecutive squared differences of y)"
    if protocol.get("train_only_hyperparameters", {}).get("observation_variance") != expected_variance:
        raise ValueError("protocol variance mismatch")
    if protocol.get("selection", {}).get("split") != "validation":
        raise ValueError("protocol selection split mismatch")
    if protocol.get("decision", {}).get("production_promotion") != "prohibited from this one-person companion analysis alone":
        raise ValueError("protocol decision boundary mismatch")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(input_path: Path, protocol_path: Path, output_dir: Path) -> dict[str, Any]:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    _protocol_matches(protocol)
    rows = LONG.load_rows(input_path)
    excluded_dates = prior_dates(rows)
    prior = LONG.fixed_prior(rows, "first7_train")
    calibration = calibrate_train(rows, prior, excluded_dates)
    validation = replay(rows, "validation", prior, (CURRENT, *CANDIDATES),
                        calibration, excluded_dates)
    selected, candidates = choose_candidate(validation)

    output_dir.mkdir(parents=True, exist_ok=True)
    lock = {
        "schema": "sleepwell.bayes_long_history_selection_lock.v1",
        "locked_before_test_replay": True, "selected_validation_alternative": selected,
        "protocol_sha256": sha256(protocol_path), "input_sha256": sha256(input_path),
    }
    lock_path = output_dir / "selection_lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    test = replay(rows, "test", prior, (CURRENT, selected), calibration, excluded_dates)
    paired = paired_summary(test, selected)
    sensitivity_prior = LONG.fixed_prior(rows, "all_train")
    sensitivity_calibration = calibrate_train(rows, sensitivity_prior, excluded_dates)
    sensitivity = replay(rows, "test", sensitivity_prior, (CURRENT, selected),
                         sensitivity_calibration, excluded_dates)
    result = {
        "schema": "sleepwell.bayes_long_history_comparison.v1",
        "status": "retrospective_single_person_previously_reused_test",
        "decision": "companion_evidence_only_no_production_promotion",
        "selected_validation_alternative": selected,
        "train_only_calibration": calibration,
        "validation": {"current": metrics(validation, CURRENT), "alternatives": candidates,
                       "selected_paired": paired_summary(validation, selected)},
        "test_opened_after_selection_lock": {
            "metrics": [metrics(test, CURRENT), metrics(test, selected)],
            "selected_paired": paired, "diagnostics": diagnostics(test, selected),
        },
        "all_train_prior_sensitivity_after_selection": {
            "calibration": sensitivity_calibration,
            "metrics": [metrics(sensitivity, CURRENT), metrics(sensitivity, selected)],
            "selected_paired": paired_summary(sensitivity, selected),
        },
        "change_recommendation": {
            "selected_improved_locked_test_mean_error": paired["mean_gain_min"] > 0,
            "action": "no_production_change_from_single_person_companion",
            "reason": "one-person proxy evidence cannot promote a production policy",
        },
        "data_contract": {
            "rows": len(rows), "people": 1,
            "calendar_span_days": (rows[-1]["forecast"] - rows[0]["forecast"]).days,
            "split_rows": {split: sum(row["split"] == split for row in rows)
                           for split in LONG.SPLITS},
            "primary_prior_train_nights": 7, "primary_prior_is_enrollment_input": False,
            "availability_assumption": "sleep_end is treated as availability because observed_at is absent",
        },
        "provenance": {
            "input": {"path": str(input_path), "sha256": sha256(input_path)},
            "protocol": {"path": str(protocol_path), "sha256": sha256(protocol_path)},
            "selection_lock": {"path": str(lock_path), "sha256": sha256(lock_path)},
            "base_long_history_script": {"path": str(LONG_SCRIPT), "sha256": sha256(LONG_SCRIPT)},
            "script": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
        },
        "interpretation_limits": [
            "The first-seven Train prior is a proxy built from the same person's observations, not an enrollment input.",
            "Gaussian offsets are a local approximation on a circular outcome; the Train seam guard does not validate Gaussian errors.",
            "Train-conditioned variance estimates reuse the one person's historical observations and do not form a population empirical prior.",
            "Posterior variances are model diagnostics, not validated uncertainty or prediction intervals.",
            "The previously reused Test is an out-of-time sensitivity period, not a fresh cohort.",
            "No observed_at exists; sleep_end availability may be optimistic if synchronization was delayed.",
            "This analysis does not estimate physiological time constants, circadian phase, sleep need, or recommendation benefit.",
        ],
    }
    comparison_path = output_dir / "comparison.json"
    comparison_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(output_dir / "validation_candidates.csv", candidates)
    return result


def render_report(result: dict[str, Any], path: Path) -> None:
    selected = result["selected_validation_alternative"]
    validation = result["validation"]
    test = result["test_opened_after_selection_lock"]
    current_test, selected_test = test["metrics"]
    paired = test["selected_paired"]
    sensitivity = result["all_train_prior_sensitivity_after_selection"]
    calibration = result["train_only_calibration"]
    test_diagnostics = test["diagnostics"]
    lines = [
        "# 베이지안 갱신·일부 계수 적합 장기 보조 비교 — 2026-11-05", "",
        "## 판정", "",
        (f"Validation에서 `{selected}`가 선택되었다. 잠긴 Test에서 현재 방식의 평균 절대오차는 "
         f"{current_test['mean_absolute_error_min']:.2f}분, 선택 방식은 "
         f"{selected_test['mean_absolute_error_min']:.2f}분으로 평균 이득은 {paired['mean_gain_min']:.2f}분이었다."), "",
        "이 결과만으로 운영 설정을 수정하지 않는다. 한 사람의 희소한 행동 중간시각 대리지표이며, 실제 가입 초기값·생리 위상·추천 효용을 검증하지 않는다.", "",
        "## Train-only 적합성·척도 점검", "",
        "| 항목 | 값 |", "|---|---:|",
        f"| 첫 7회 제외 후 적합 관측 | {calibration['train_post_prior_n']} |",
        f"| 최대 절대 원형 offset | {calibration['train_max_abs_offset_min']:.2f}분 / 사전 기준 600분 |",
        f"| 원형 offset 범위 | {calibration['train_offset_range_min']:.2f}분 / 사전 기준 720분 |",
        f"| 관측분산 R | {calibration['observation_variance_min2']:.2f}분² |",
        f"| 상수상태분산 T | {calibration['constant_state_variance_min2']:.2f}분² |",
        f"| affine 관측분산 | {calibration['affine_observation_variance_min2']:.2f}분² |",
        f"| Var(x) | {calibration['train_x_variance_min2']:.2f}분² |",
        f"| affine 기울기 사전분산 | {calibration['affine_slope_prior_variance']:.4f} |", "",
        ("분산 하한 25분²와 affine 기울기 사전분산 하한 0.01은 지표 계산 전에 고정했다. "
         "이번 Train 적합에서는 관측분산·상태분산·affine 관측분산·x 분산·기울기 분산 어느 하한도 작동하지 않았다. "
         "R과 T는 시간변화와 측정/행동 잡음을 식별해 분리한 값이 아니라 같은 한 사람의 Train 변동에서 얻은 근사 척도다."), "",
        ("Train offset 범위 697.50분은 720분 배제 기준 바로 아래로 매우 넓다. 따라서 계산은 가능하지만 "
         "단순 Gaussian offset 해석은 취약하며, 사후분산을 실제 확률·보정된 신뢰구간으로 해석하지 않는다."), "",
        "## Validation 후보", "",
        "| 방법 | 평균 절대오차(분) | 현재 대비 이득(분) | 평균 갱신 gain | 평균 β0 | 평균 β1 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in validation["alternatives"]:
        gain = "NA" if item["mean_update_gain"] is None else f"{item['mean_update_gain']:.3f}"
        lines.append(f"| {item['setting']} | {item['mean_absolute_error_min']:.2f} | {item['gain_vs_current_min']:.2f} | {gain} | {item['mean_beta0']:.2f} | {item['mean_beta1']:.3f} |")
    lines += [
        "", "Train에서만 관측분산·사전분산 척도를 계산했다. Validation 결과로 후보를 고정하고 selection lock을 쓴 뒤 Test를 계산했다.", "",
        "## 잠긴 Test", "",
        "| 항목 | 값 |", "|---|---:|",
        f"| 밤 수 | {paired['n_nights']} |",
        f"| 현재 평균 절대오차 | {current_test['mean_absolute_error_min']:.2f}분 |",
        f"| 선택 방식 평균 절대오차 | {selected_test['mean_absolute_error_min']:.2f}분 |",
        f"| 평균 이득 | {paired['mean_gain_min']:.2f}분 |",
        f"| 중앙값 이득 | {paired['median_gain_min']:.2f}분 |",
        f"| 개선 / 동일 / 악화 | {paired['improved_nights']} / {paired['equal_nights']} / {paired['worsened_nights']} |",
        f"| 전체 과거 이력 수 범위 | {test_diagnostics['history_count']['min']}–{test_diagnostics['history_count']['max']} |",
        f"| 첫 7회 제외 갱신 관측 수 범위 | {test_diagnostics['update_count']['min']}–{test_diagnostics['update_count']['max']} |",
        "", "공백 구간별 기술통계:", "",
        "| 구간 | 밤 | 평균 이득(분) | 개선 / 동일 / 악화 |",
        "|---|---:|---:|---:|",
    ]
    for name, item in test["diagnostics"]["gap_strata"].items():
        lines.append(f"| {name} | {item['n_nights']} | {item['mean_gain_min']:.2f} | {item['improved_nights']} / {item['equal_nights']} / {item['worsened_nights']} |")
    sensitivity_metrics = sensitivity["metrics"]
    lines += [
        "", "## 전체 Train 대리 초기값 민감도", "",
        "후보 선택을 바꾸지 않고 초기 기준만 전체 Train 원형평균으로 바꿨다.", "",
        "| 현재 오차 | 선택 방식 오차 | 평균 이득 |", "|---:|---:|---:|",
        (f"| {sensitivity_metrics[0]['mean_absolute_error_min']:.2f} | "
         f"{sensitivity_metrics[1]['mean_absolute_error_min']:.2f} | "
         f"{sensitivity['selected_paired']['mean_gain_min']:.2f} |"),
        "", "## 방법의 의미와 한계", "",
        "- `eb_constant_offset`은 개인 편향이 고정됐다고 가정한 정규-정규 갱신이다.",
        "- Kalman 후보는 개인 편향이 달력시간에 따라 확률적으로 변한다고 가정하며, 세 Q/R 비율은 결과 전에 고정했다.",
        "- `affine_bayes`는 최근 중간시각 요약에 대한 절편과 기울기만 적합하고 `(0,1)`을 중심으로 수축한다.",
        "- 원형 시각을 고정 기준 주위의 최단 차이로 펼친 Gaussian 근사다. Train 범위가 697.50분으로 넓어 국소 근사가 특히 취약하며, 사전 절단점 검사 통과가 정규성이나 불확실성 보정을 검증하지 않는다.",
        "- Train 분산은 같은 한 사람의 이력을 조건으로 한 척도이며 모집단 경험적 베이지안 사전분포가 아니다.",
        "- Train 관측은 분산 척도 추정과 이후 상태·계수 갱신에 조건부로 재사용되므로, 사전분포와 likelihood가 독립적으로 확보된 완전한 Bayesian 검증이 아니다.",
        "- 사후분산은 모델 내부 진단값이다. 참여자가 한 명이므로 밤을 독립 참여자처럼 부트스트랩하거나 임상 신뢰구간을 제시하지 않았다.",
        "- Test는 이전 분석에서도 사용된 기간이므로 새로운 외부 검증 표본이 아니라 잠긴 후보의 시간외 민감도 점검이다.",
        "- 196개 밤은 1,212일에 흩어져 있고 `observed_at`이 없어 기상 시각을 이용 가능 시각으로 가정했다.",
        "- 생리 시간상수, DLMO, 수면 필요량, 권고 효과 또는 생산 엔진 전체를 검증하지 않는다.",
        "", "재현 명령:", "", "```powershell",
        "C:\\Python312\\python.exe -X utf8 -B research_workbench_academic\\scripts\\compare_personalization_bayes_long_history.py",
        "Set-Location research_workbench_academic",
        "C:\\Python312\\python.exe -X utf8 -B -m unittest tests.test_personalization_bayes_long_history -v",
        "```", "", "판정: **장기 단일인 보조 근거로만 기록하며, 이 결과만으로 운영 코드를 변경하지 않는다.**", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(args.input.resolve(), args.protocol.resolve(), args.output_dir.resolve())
    render_report(result, args.output_dir.resolve() / "REPORT.md")
    print(json.dumps({"selected": result["selected_validation_alternative"],
                      "test_gain_min": result["test_opened_after_selection_lock"]["selected_paired"]["mean_gain_min"],
                      "decision": result["decision"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
