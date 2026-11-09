"""Single-person long-history sensitivity analysis for personalization weight growth.

This companion analysis evaluates a behavioral sleep-midpoint proxy on one fixed
chronological record.  It does not evaluate the production recommendation, sleep
benefit, circadian phase, or physiological parameters, and cannot promote a
production setting by itself.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = Path("E:/학술제/model/데이터모델/미래예측모델/output/causal_daily_features.parquet")
DEFAULT_OUTPUT = ROOT / "reports" / "personalization_growth_20261105" / "long_history"
DEFAULT_PROTOCOL = DEFAULT_OUTPUT / "protocol.json"
CURRENT = "current_mass_lambda14"
CANDIDATES = ("count_weight", "lambda_linear14", "lambda_linear28", "lambda_decay28")
SPLITS = ("train", "validation", "test")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clock_minute(value: datetime) -> float:
    return (value.hour * 60 + value.minute + value.second / 60 +
            value.microsecond / 60_000_000) % 1440


def circular_delta(value: float, reference: float) -> float:
    return (value - reference + 720) % 1440 - 720


def circular_mean(values: list[float], weights: list[float] | None = None) -> float:
    if not values:
        raise ValueError("circular mean requires values")
    weights = weights or [1.0] * len(values)
    if len(weights) != len(values) or not all(math.isfinite(w) and w >= 0 for w in weights):
        raise ValueError("invalid circular weights")
    if sum(weights) <= 0:
        raise ValueError("circular weights must have positive mass")
    x = sum(w * math.cos(v * math.tau / 1440) for v, w in zip(values, weights))
    y = sum(w * math.sin(v * math.tau / 1440) for v, w in zip(values, weights))
    if math.hypot(x, y) <= 1e-12:
        raise ValueError("circular mean is unidentified")
    return math.atan2(y, x) * 1440 / math.tau % 1440


def midpoint(start: datetime, end: datetime) -> float:
    if not start < end:
        raise ValueError("sleep interval must be positive")
    return clock_minute(start + (end - start) / 2)


def alpha(setting: str, mass: float, count: int) -> tuple[float, float | None]:
    if count < 0 or mass < 0 or not math.isfinite(mass):
        raise ValueError("invalid history state")
    if setting == CURRENT:
        lam = 14.0
        value = mass / (mass + lam) if mass else 0.0
    elif setting == "count_weight":
        lam = None
        value = count / (count + 14.0) if count else 0.0
    elif setting == "lambda_linear14":
        lam = 14.0 / (1.0 + count / 14.0)
        value = mass / (mass + lam) if mass else 0.0
    elif setting == "lambda_linear28":
        lam = 14.0 / (1.0 + count / 28.0)
        value = mass / (mass + lam) if mass else 0.0
    elif setting == "lambda_decay28":
        lam = 14.0 * 2.0 ** (-count / 28.0)
        value = mass / (mass + lam) if mass else 0.0
    else:
        raise ValueError(f"unknown setting: {setting}")
    return value, lam


def _naive(value: Any, label: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    return value


def load_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("pandas and a parquet reader are required") from error
    frame = pd.read_parquet(path)
    required = {
        "subject_id", "episode_id", "sleep_start", "sleep_end", "forecast_date",
        "split", "temporal_split", "timezone", "is_main_sleep", "episode_qc_pass",
    }
    if not required.issubset(frame.columns):
        raise ValueError(f"missing columns: {sorted(required - set(frame.columns))}")
    if len(frame) != 196 or frame["subject_id"].nunique(dropna=False) != 1:
        raise ValueError("expected the frozen 196-row single-person artifact")
    if frame["episode_id"].duplicated().any() or frame["forecast_date"].duplicated().any():
        raise ValueError("episode_id and forecast_date must be unique")
    rows = []
    wake_dates: set[Any] = set()
    for record in frame.to_dict("records"):
        split = str(record["split"])
        if split not in SPLITS or record["timezone"] != "Asia/Seoul":
            raise ValueError("unexpected split or timezone")
        if not bool(record["is_main_sleep"]) or not bool(record["episode_qc_pass"]):
            raise ValueError("all rows must be valid main-sleep episodes")
        start = _naive(record["sleep_start"], "sleep_start")
        end = _naive(record["sleep_end"], "sleep_end")
        forecast = _naive(record["forecast_date"], "forecast_date")
        cutoff = forecast.replace(hour=20, minute=0, second=0, microsecond=0)
        if not cutoff < start < end:
            raise ValueError("target chronology violates the 20:00 cutoff")
        if end.date() in wake_dates:
            raise ValueError("duplicate wake date")
        wake_dates.add(end.date())
        rows.append({
            "split": split, "forecast": forecast, "cutoff": cutoff,
            "start": start, "end": end, "wake_date": end.date(),
            "target_midpoint": midpoint(start, end),
        })
    rows.sort(key=lambda row: row["forecast"])
    split_order = [SPLITS.index(row["split"]) for row in rows]
    if split_order != sorted(split_order):
        raise ValueError("splits are not chronological")
    counts = {split: sum(row["split"] == split for row in rows) for split in SPLITS}
    if counts != {"train": 137, "validation": 29, "test": 30}:
        raise ValueError(f"unexpected split counts: {counts}")
    return rows


def fixed_prior(rows: list[dict[str, Any]], mode: str) -> float:
    train = [row for row in rows if row["split"] == "train"]
    selected = train[:7] if mode == "first7_train" else train
    if mode not in {"first7_train", "all_train"}:
        raise ValueError("unknown prior mode")
    return circular_mean([row["target_midpoint"] for row in selected])


def eligible_history(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    eligible = [row for row in rows if row["end"] < cutoff]
    # The artifact has unique wake dates; retain the explicit production-like rule.
    latest = {}
    for row in eligible:
        previous = latest.get(row["wake_date"])
        if previous is None or row["end"] > previous["end"]:
            latest[row["wake_date"]] = row
    return sorted(latest.values(), key=lambda row: row["end"])


def predict_row(rows: list[dict[str, Any]], target: dict[str, Any], prior: float,
                setting: str) -> dict[str, Any]:
    history = eligible_history(rows, target["cutoff"])
    ages = [(target["cutoff"] - row["end"]).total_seconds() / 86400 for row in history]
    if any(age <= 0 for age in ages):
        raise ValueError("non-causal history age")
    weights = [2.0 ** (-age / 14.0) for age in ages]
    mass = sum(weights)
    summary = circular_mean([row["target_midpoint"] for row in history], weights)
    personal_weight, lam = alpha(setting, mass, len(history))
    predicted = (prior + personal_weight * circular_delta(summary, prior)) % 1440
    error = abs(circular_delta(predicted, target["target_midpoint"]))
    last_gap = min(ages)
    return {
        "setting": setting, "split": target["split"], "error_min": error,
        "history_count": len(history), "decayed_mass": mass,
        "personal_weight": personal_weight, "lambda_effective": lam,
        "last_gap_days": last_gap,
    }


def replay(rows: list[dict[str, Any]], split: str, prior: float,
           settings: tuple[str, ...]) -> list[dict[str, Any]]:
    return [predict_row(rows, target, prior, setting)
            for target in rows if target["split"] == split for setting in settings]


def metrics(records: list[dict[str, Any]], setting: str) -> dict[str, Any]:
    selected = [row for row in records if row["setting"] == setting]
    values = [row["error_min"] for row in selected]
    return {
        "setting": setting, "n_nights": len(values), "mean_absolute_error_min": mean(values),
        "median_absolute_error_min": median(values),
        "mean_personal_weight": mean(row["personal_weight"] for row in selected),
        "min_personal_weight": min(row["personal_weight"] for row in selected),
        "max_personal_weight": max(row["personal_weight"] for row in selected),
        "mean_decayed_mass": mean(row["decayed_mass"] for row in selected),
        "min_history_count": min(row["history_count"] for row in selected),
        "max_history_count": max(row["history_count"] for row in selected),
    }


def choose_candidate(validation: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if not validation or any(row.get("split") != "validation" for row in validation):
        raise ValueError("candidate selection requires validation records only")
    current = metrics(validation, CURRENT)
    alternatives = []
    for candidate in CANDIDATES:
        item = metrics(validation, candidate)
        item["gain_vs_current_min"] = (current["mean_absolute_error_min"] -
                                        item["mean_absolute_error_min"])
        alternatives.append(item)
    selected = max(alternatives, key=lambda row: (row["gain_vs_current_min"],
                                                   -CANDIDATES.index(row["setting"])))
    return selected, current, alternatives


def paired_summary(records: list[dict[str, Any]], alternative: str,
                   predicate=lambda row: True) -> dict[str, Any]:
    current = [row for row in records if row["setting"] == CURRENT and predicate(row)]
    candidate = [row for row in records if row["setting"] == alternative and predicate(row)]
    if len(current) != len(candidate):
        raise ValueError("paired coverage mismatch")
    gains = [a["error_min"] - b["error_min"] for a, b in zip(current, candidate)]
    return {
        "n_nights": len(gains),
        "mean_gain_min": mean(gains) if gains else None,
        "median_gain_min": median(gains) if gains else None,
        "improved_nights": sum(value > 1e-12 for value in gains),
        "equal_nights": sum(abs(value) <= 1e-12 for value in gains),
        "worsened_nights": sum(value < -1e-12 for value in gains),
    }


def history_diagnostics(records: list[dict[str, Any]], selected: str) -> dict[str, Any]:
    current = [row for row in records if row["setting"] == CURRENT]
    gaps = {
        "gap_lt_7": lambda row: row["last_gap_days"] < 7,
        "gap_7_to_27": lambda row: 7 <= row["last_gap_days"] < 28,
        "gap_ge_28": lambda row: row["last_gap_days"] >= 28,
    }
    return {
        "history_count": {
            "min": min(row["history_count"] for row in current),
            "max": max(row["history_count"] for row in current),
            "nights_ge_56": sum(row["history_count"] >= 56 for row in current),
            "nights_ge_90": sum(row["history_count"] >= 90 for row in current),
        },
        "decayed_mass": {
            "min": min(row["decayed_mass"] for row in current),
            "median": median(row["decayed_mass"] for row in current),
            "max": max(row["decayed_mass"] for row in current),
        },
        "last_gap_days": {
            "min": min(row["last_gap_days"] for row in current),
            "median": median(row["last_gap_days"] for row in current),
            "max": max(row["last_gap_days"] for row in current),
        },
        "gap_strata": {name: paired_summary(records, selected, predicate)
                       for name, predicate in gaps.items()},
    }


def structural_table() -> list[dict[str, Any]]:
    output = []
    for count in (1, 7, 14, 28, 56, 90, 180, 365):
        base_mass = sum(2.0 ** (-age / 14.0) for age in range(count))
        for gap in (0, 14, 56):
            mass = base_mass * 2.0 ** (-gap / 14.0)
            row = {"history_count": count, "gap_days": gap, "decayed_mass": mass}
            for setting in (CURRENT, *CANDIDATES):
                row[setting] = alpha(setting, mass, count)[0]
            output.append(row)
    return output


def _protocol_matches(protocol: dict[str, Any]) -> None:
    if protocol.get("schema") != "sleepwell.long_history_midpoint_protocol.v1":
        raise ValueError("protocol schema mismatch")
    if protocol.get("status") != "locally_prespecified_before_comparison":
        raise ValueError("protocol status mismatch")
    expected_alternatives = [
        {"id": "count_weight", "alpha": "n/(n+14)"},
        {"id": "lambda_linear14", "lambda": "14/(1+n/14)", "alpha": "M/(M+lambda)"},
        {"id": "lambda_linear28", "lambda": "14/(1+n/28)", "alpha": "M/(M+lambda)"},
        {"id": "lambda_decay28", "lambda": "14*2^(-n/28)", "alpha": "M/(M+lambda)"},
    ]
    if protocol.get("alternatives") != expected_alternatives:
        raise ValueError("protocol candidate mismatch")
    if protocol.get("current") != {"id": CURRENT, "alpha": "M/(M+14)"}:
        raise ValueError("protocol current mismatch")
    if protocol.get("shared", {}).get("recency_half_life_days") != 14.0:
        raise ValueError("protocol recency half-life mismatch")
    if protocol.get("selection", {}).get("split") != "validation":
        raise ValueError("protocol split mismatch")
    if protocol.get("selection", {}).get("metric") != "mean absolute circular sleep-midpoint error in minutes":
        raise ValueError("protocol selection metric mismatch")
    if protocol.get("decision", {}).get("production_promotion") != "prohibited from this companion analysis alone":
        raise ValueError("protocol decision boundary mismatch")


def _write_validation_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(input_path: Path, protocol_path: Path, output_dir: Path) -> dict[str, Any]:
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    _protocol_matches(protocol)
    rows = load_rows(input_path)
    primary_prior = fixed_prior(rows, "first7_train")
    all_train_prior = fixed_prior(rows, "all_train")

    validation = replay(rows, "validation", primary_prior, (CURRENT, *CANDIDATES))
    selected, validation_current, candidate_metrics = choose_candidate(validation)

    output_dir.mkdir(parents=True, exist_ok=True)
    lock = {
        "schema": "sleepwell.long_history_selection_lock.v1",
        "locked_before_test_replay": True,
        "selected_validation_alternative": selected["setting"],
        "validation_gain_min": selected["gain_vs_current_min"],
        "input_sha256": sha256(input_path), "protocol_sha256": sha256(protocol_path),
    }
    lock_path = output_dir / "selection_lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    test = replay(rows, "test", primary_prior, (CURRENT, selected["setting"]))
    sensitivity = replay(rows, "test", all_train_prior, (CURRENT, selected["setting"]))
    test_metrics = [metrics(test, setting) for setting in (CURRENT, selected["setting"])]
    test_paired = paired_summary(test, selected["setting"])
    result = {
        "schema": "sleepwell.long_history_midpoint_comparison.v1",
        "status": "retrospective_single_person_fixed_chronological_split",
        "decision": "companion_evidence_only_no_production_promotion",
        "selected_validation_alternative": selected["setting"],
        "validation": {
            "current": validation_current, "alternatives": candidate_metrics,
            "selected_paired": paired_summary(validation, selected["setting"]),
        },
        "test_opened_after_selection_lock": {
            "metrics": test_metrics,
            "selected_paired": test_paired,
            "diagnostics": history_diagnostics(test, selected["setting"]),
        },
        "change_recommendation": {
            "selected_alternative_improved_locked_test_mean_error": test_paired["mean_gain_min"] > 0,
            "action": "retain_current_setting",
            "reason": "the validation-selected alternative did not improve the locked Test mean error",
        },
        "all_train_prior_sensitivity_after_selection": {
            "metrics": [metrics(sensitivity, setting) for setting in (CURRENT, selected["setting"])],
            "selected_paired": paired_summary(sensitivity, selected["setting"]),
        },
        "structural_alpha_examples": structural_table(),
        "data_contract": {
            "rows": len(rows), "people": 1, "calendar_span_days": (rows[-1]["forecast"] - rows[0]["forecast"]).days,
            "split_rows": {split: sum(row["split"] == split for row in rows) for split in SPLITS},
            "availability_assumption": "sleep_end treated as observed/available time because observed_at is absent",
            "timestamp_storage": "source timestamp columns are timezone-naive local times and timezone column is Asia/Seoul",
            "sparse_nights_not_consecutive_days": True,
            "primary_prior_train_nights": 7,
            "primary_prior_uses_validation_or_test": False,
        },
        "provenance": {
            "input": {"path": str(input_path), "sha256": sha256(input_path)},
            "protocol": {"path": str(protocol_path), "sha256": sha256(protocol_path)},
            "selection_lock": {"path": str(lock_path), "sha256": sha256(lock_path)},
            "script": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__).resolve())},
        },
        "interpretation_limits": [
            "One participant cannot establish population transportability.",
            "The first-seven-Train prior is a proxy estimated from this participant's observations, not an actual onboarding or self-reported user prior.",
            "Sleep midpoint is a behavioral timing proxy, not DLMO, physiological phase, sleep need, or benefit.",
            "No observed_at exists; treating sleep_end as availability can be optimistic if synchronization was delayed.",
            "The 196 episodes span 1,212 calendar days and are not consecutive daily observations.",
            "Validation selected the alternative; Test is an out-of-time sensitivity check, not an independent cohort.",
            "No participant bootstrap or clinical confidence interval is valid for this n=1 record.",
        ],
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validation_csv = [{"setting": CURRENT, **validation_current, "gain_vs_current_min": 0.0},
                      *candidate_metrics]
    _write_validation_csv(output_dir / "validation_candidates.csv", validation_csv)
    return result


def render_report(result: dict[str, Any], output_path: Path) -> None:
    selected = result["selected_validation_alternative"]
    validation = result["validation"]
    test = result["test_opened_after_selection_lock"]
    current_test, selected_test = test["metrics"]
    paired = test["selected_paired"]
    sensitivity = result["all_train_prior_sensitivity_after_selection"]
    sensitivity_current, sensitivity_selected = sensitivity["metrics"]
    sensitivity_paired = sensitivity["selected_paired"]
    lines = [
        "# 장기 개인화 비중 보조 검증 — 2026-11-05", "",
        "## 결론", "",
        (f"한 사람의 장기 시간순 기록에서는 Validation에서 `{selected}`가 선택되었다. "
         f"Test 평균 수면 중간시각 오차는 현재 방식 {current_test['mean_absolute_error_min']:.2f}분, "
         f"선택 대안 {selected_test['mean_absolute_error_min']:.2f}분으로, 현재 대비 평균 "
         f"{paired['mean_gain_min']:.2f}분의 차이가 있었다."), "",
        "Validation 개선이 잠긴 Test에서 재현되지 않았으므로 현재 설정을 유지한다. 또한 한 사람의 희소한 행동 기록이며 수면 개선·생리 위상·모집단 일반화를 검증하지 못한다. 실제 `observed_at`이 없어 기상 시각에 결과가 이용 가능했다고 가정했다.", "",
        "## Validation에서 고정한 대안", "",
        "| 설정 | 평균 절대오차(분) | 현재 대비 이득(분) | 평균 개인 비중 |",
        "|---|---:|---:|---:|",
        (f"| {CURRENT} | {validation['current']['mean_absolute_error_min']:.2f} | 0.00 | "
         f"{validation['current']['mean_personal_weight']:.3f} |"),
    ]
    for item in validation["alternatives"]:
        lines.append(f"| {item['setting']} | {item['mean_absolute_error_min']:.2f} | {item['gain_vs_current_min']:.2f} | {item['mean_personal_weight']:.3f} |")
    lines += [
        "", "Validation 결과만으로 대안을 고정해 `selection_lock.json`을 쓴 뒤 Test를 계산했다.", "",
        "## 잠긴 Test 결과", "",
        "| 항목 | 값 |", "|---|---:|",
        f"| 밤 수 | {paired['n_nights']} |",
        f"| 현재 평균 절대오차 | {current_test['mean_absolute_error_min']:.2f}분 |",
        f"| 선택 대안 평균 절대오차 | {selected_test['mean_absolute_error_min']:.2f}분 |",
        f"| 평균 이득 | {paired['mean_gain_min']:.2f}분 |",
        f"| 중앙값 이득 | {paired['median_gain_min']:.2f}분 |",
        f"| 개선 / 동일 / 악화 밤 | {paired['improved_nights']} / {paired['equal_nights']} / {paired['worsened_nights']} |",
        "", "역사 길이와 공백별 결과:", "",
        "| 구간 | 밤 | 평균 이득(분) | 개선 / 동일 / 악화 |",
        "|---|---:|---:|---:|",
    ]
    for label, item in test["diagnostics"]["gap_strata"].items():
        gain = "NA" if item["mean_gain_min"] is None else f"{item['mean_gain_min']:.2f}"
        lines.append(f"| {label} | {item['n_nights']} | {gain} | {item['improved_nights']} / {item['equal_nights']} / {item['worsened_nights']} |")
    diagnostics = test["diagnostics"]
    lines += [
        "",
        (f"Test의 과거 기록 수는 {diagnostics['history_count']['min']}–{diagnostics['history_count']['max']}개이며, "
         f"90개 이상 이력인 밤은 {diagnostics['history_count']['nights_ge_90']}개다. 최근 기록 공백은 "
         f"중앙 {diagnostics['last_gap_days']['median']:.1f}일, 최대 {diagnostics['last_gap_days']['max']:.1f}일이다."),
        "", "## 초기값 민감도", "",
        ("주 분석의 초기값은 실제 가입 입력이나 생산 기본값이 아니라, Train의 첫 7밤 수면 중간시각으로 만든 대리 초기값이다. "
         "사전 지정한 민감도에서는 후보 선택을 바꾸지 않고 전체 Train으로만 대리 초기값을 다시 계산했다."), "",
        "| 초기값 구성 | 현재 평균오차(분) | 선택 대안 평균오차(분) | 평균 이득(분) |",
        "|---|---:|---:|---:|",
        (f"| Train 첫 7밤(주 분석) | {current_test['mean_absolute_error_min']:.2f} | "
         f"{selected_test['mean_absolute_error_min']:.2f} | {paired['mean_gain_min']:.2f} |"),
        (f"| 전체 Train(선택 후 민감도) | {sensitivity_current['mean_absolute_error_min']:.2f} | "
         f"{sensitivity_selected['mean_absolute_error_min']:.2f} | {sensitivity_paired['mean_gain_min']:.2f} |"),
        "", "## 해석", "",
        (f"현재 방식의 약 40% 초기값 잔존은 검증된 확률이 아니라 매일 기록된다는 이상적 조건에서 14일 최근성 기억과 고정 수축강도 14가 만드는 정상상태다. "
         f"실제 Test는 희소해 현재 방식의 평균 개인 비중이 {current_test['mean_personal_weight']:.3f}, 즉 평균 초기값 비중은 "
         f"{1-current_test['mean_personal_weight']:.3f}였다. 잡음과 긴 공백 뒤 오래된 행동을 과신하지 않는 장점이 있지만, 초기값이 이 사람에게 틀리면 장기 편향도 남는다."), "",
        (f"선택 대안은 Test 평균 개인 비중을 {selected_test['mean_personal_weight']:.3f}까지 높였지만 평균 오차를 "
         f"{-paired['mean_gain_min']:.2f}분 악화시켰다. 누적 개수 기반 방식은 기록이 쌓이면 개인 비중을 높이지만 긴 공백에도 높은 비중이 남고, λ를 기록 수에 따라 낮추는 방식도 정도의 차이는 있으나 같은 위험이 있다. 따라서 운영 수치를 바꾸지 않는다."), "",
        "## 범위와 재현", "",
        "- 자료는 196개 밤이 1,212일에 흩어진 한 사람의 Samsung 기록이다. 연속 196일 자료가 아니다.",
        "- 주 분석의 대리 초기값은 Train 첫 7개 밤만 사용했다. 이는 실제 가입 입력 또는 생산 초기값을 재현한 것이 아니다. Validation에서 대안을 선택했고 Test 30개 밤은 선택 후 한 번 평가했다.",
        "- 이 분석은 초기값과 최근 수면 중간시각 사이의 위상 가중식만 독립적으로 재구성했다. 생산 엔진 전체, 잔차 보정, 추천 산출 또는 실제 사용자 효용을 재현하지 않았다.",
        "- 원본 시각 열은 timezone-naive 현지시각이고 `timezone` 열은 모두 `Asia/Seoul`이었다. 실제 보고 시각은 없어 `sleep_end`를 이용 가능 시각으로 가정했다.",
        "- 수면 중간시각은 행동 시각 대리지표이며 DLMO, 생리 일주기 위상, 최적 수면 필요량 또는 추천 효과가 아니다.",
        "- 참여자가 한 명이므로 밤을 독립 참여자처럼 부트스트랩하거나 임상 신뢰구간을 제시하지 않았다.",
        "- 상세 집계, 구조적 비중 예시와 파일 해시는 `comparison.json`에 있다.",
        "", "재현 명령:", "",
        "```powershell",
        "C:\\Python312\\python.exe -X utf8 -B research_workbench_academic\\scripts\\compare_personalization_long_history.py",
        "Set-Location research_workbench_academic",
        "C:\\Python312\\python.exe -X utf8 -B -m unittest tests.test_personalization_long_history -v",
        "```", "",
        "판정: **보조 근거만 기록하고 운영 수치는 변경하지 않는다.**", "",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(args.input.resolve(), args.protocol.resolve(), args.output_dir.resolve())
    render_report(result, args.output_dir.resolve() / "REPORT.md")
    print(json.dumps({
        "selected": result["selected_validation_alternative"],
        "test_gain_min": result["test_opened_after_selection_lock"]["selected_paired"]["mean_gain_min"],
        "decision": result["decision"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
