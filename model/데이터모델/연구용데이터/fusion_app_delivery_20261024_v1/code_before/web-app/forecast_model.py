"""Strict, portable behavior-forecast artifact for the local web app.

The model predicts clock offsets from the 20:00 Asia/Seoul decision time.  It
uses only completed sleep records available before that cutoff.  This module is
deliberately independent of pandas/scikit-learn so a reviewed artifact can be
deployed without the training environment.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from engine import ZONE, clock_min, parse_date


ARTIFACT_TYPE = "sleepwell_behavior_clock_linear"
FEATURE_CONTRACT = "web_completed_history_pre20_v1"
FEATURE_ORDER = (
    "prior_bedtime_offset_median_7_min",
    "prior_wake_offset_median_7_min",
    "prior_duration_median_7_min",
    "previous_sleep_duration_min",
    "history_count_7",
    "weekend",
)
FEATURE_UNITS = {
    "prior_bedtime_offset_median_7_min": "minutes_after_20:00",
    "prior_wake_offset_median_7_min": "minutes_after_20:00",
    "prior_duration_median_7_min": "minutes",
    "previous_sleep_duration_min": "minutes",
    "history_count_7": "count",
    "weekend": "binary_0_1",
}
TARGET_NAMES = ("bedtime_offset_min", "wake_offset_min")
HISTORY_LOOKBACK_DAYS = 28
MAX_ARTIFACT_BYTES = 1024 * 1024

PORTABLE_MODEL_CONTRACT = {
    "schema_version": 1,
    "artifact_type": ARTIFACT_TYPE,
    "status": "trained",
    "decision_time": "20:00",
    "timezone": "Asia/Seoul",
    "feature_contract": FEATURE_CONTRACT,
    "feature_order": list(FEATURE_ORDER),
    "feature_units": FEATURE_UNITS,
    "history_lookback_days": HISTORY_LOOKBACK_DAYS,
    "required_sections": ["preprocessing", "targets", "population", "release_gate"],
    "release_gate_status": "evaluation_passed",
}


class ForecastModelError(ValueError):
    pass


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForecastModelError(f"{label}: 유한한 숫자가 필요합니다")
    value = float(value)
    if not math.isfinite(value):
        raise ForecastModelError(f"{label}: 유한한 숫자가 필요합니다")
    return value


def _number_list(value, label: str, size: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ForecastModelError(f"{label}: 길이 {size} 배열이 필요합니다")
    return tuple(_finite_number(item, f"{label}[{i}]") for i, item in enumerate(value))


def _clock_offset(value: datetime, cutoff: datetime) -> float:
    """Return a future-oriented clock offset from cutoff in [0, 1440)."""
    return (clock_min(value) - clock_min(cutoff)) % 1440


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def completed_history_features(records: list[dict], cutoff: datetime) -> dict[str, float] | None:
    """Build exact contract features from up to seven records known by cutoff."""
    by_wake_date = {}
    earliest_wake = cutoff - timedelta(days=HISTORY_LOOKBACK_DAYS)
    for record in records:
        if record.get("source") not in ("diary", "healthkit"):
            continue
        try:
            bed, wake = parse_date(record["bed"]), parse_date(record["wake"])
            observed = parse_date(record["observed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        # Retrospectively entered records were not observable at the decision time.
        if observed < cutoff and earliest_wake <= wake < cutoff and bed < wake:
            duration = (wake - bed).total_seconds() / 60
            bed_offset = _clock_offset(bed, cutoff)
            wake_offset = bed_offset + duration
            if 30 <= duration <= 16 * 60 and 0 <= bed_offset <= 16 * 60 and wake_offset <= 24 * 60:
                candidate = (wake, bed_offset, wake_offset, duration, observed)
                previous = by_wake_date.get(wake.date())
                if previous is None or observed > previous[4]:
                    by_wake_date[wake.date()] = candidate
    completed = list(by_wake_date.values())
    completed.sort(key=lambda item: item[0])
    recent = completed[-7:]
    if not recent:
        return None
    bed_offsets = [item[1] for item in recent]
    wake_offsets = [item[2] for item in recent]
    durations = [item[3] for item in recent]
    return {
        "prior_bedtime_offset_median_7_min": _median(bed_offsets),
        "prior_wake_offset_median_7_min": _median(wake_offsets),
        "prior_duration_median_7_min": _median(durations),
        "previous_sleep_duration_min": durations[-1],
        "history_count_7": float(len(recent)),
        "weekend": float(cutoff.weekday() >= 5),
    }


@dataclass(frozen=True)
class Population:
    source: str
    age_min: int
    age_max: int
    occupations: tuple[str, ...]

    def eligibility(self, profile: dict) -> tuple[bool, str]:
        try:
            age = int(profile["age"])
            occupation = str(profile["occupation"])
        except (KeyError, TypeError, ValueError):
            return False, "프로필의 나이·생활 유형을 확인할 수 없습니다."
        if not self.age_min <= age <= self.age_max:
            return False, f"학습 집단 연령 범위({self.age_min}~{self.age_max}세) 밖입니다."
        if occupation not in self.occupations:
            return False, "학습 집단에 현재 생활 유형이 포함되지 않았습니다."
        return True, "학습 집단 적용 범위와 일치합니다."


@dataclass(frozen=True)
class PortableForecastModel:
    model_version: str
    imputation_median: tuple[float, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    targets: dict[str, tuple[float, tuple[float, ...]]]
    population: Population
    minimum_history_records: int

    @classmethod
    def load(cls, path: Path) -> "PortableForecastModel":
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise ForecastModelError(f"모델 JSON을 읽을 수 없습니다: {error}") from error
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ForecastModelError(f"모델 JSON은 {MAX_ARTIFACT_BYTES}바이트 이하여야 합니다")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ForecastModelError("모델 JSON은 올바른 UTF-8이어야 합니다") from error

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ForecastModelError(f"모델 JSON에 중복 키가 있습니다: {key}")
                result[key] = value
            return result

        def reject_constant(value):
            raise ForecastModelError(f"모델 JSON에 비표준 숫자가 있습니다: {value}")

        try:
            raw = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
        except json.JSONDecodeError as error:
            raise ForecastModelError(f"모델 JSON 문법이 잘못되었습니다: {error}") from error
        if not isinstance(raw, dict):
            raise ForecastModelError("모델 JSON 최상위는 object여야 합니다")
        exact = {
            "schema_version": 1,
            "artifact_type": ARTIFACT_TYPE,
            "status": "trained",
            "decision_time": "20:00",
            "timezone": "Asia/Seoul",
            "feature_contract": FEATURE_CONTRACT,
            "history_lookback_days": HISTORY_LOOKBACK_DAYS,
        }
        for key, expected in exact.items():
            if raw.get(key) != expected:
                raise ForecastModelError(f"{key}: {expected!r}만 지원합니다")
        version = raw.get("model_version")
        if not isinstance(version, str) or not version.strip():
            raise ForecastModelError("model_version이 필요합니다")
        if raw.get("feature_order") != list(FEATURE_ORDER) or raw.get("feature_units") != FEATURE_UNITS:
            raise ForecastModelError("feature 순서·단위가 web_completed_history_pre20_v1 계약과 다릅니다")

        gate = raw.get("release_gate")
        if not isinstance(gate, dict) or gate.get("status") != "evaluation_passed":
            raise ForecastModelError("release_gate.status=evaluation_passed인 연구 평가 완료 모델만 사용합니다")
        if not isinstance(gate.get("validation_protocol"), str) or not gate["validation_protocol"].strip():
            raise ForecastModelError("release_gate.validation_protocol이 필요합니다")

        population = raw.get("population")
        if not isinstance(population, dict):
            raise ForecastModelError("population 적용 범위가 필요합니다")
        source = population.get("source")
        occupations = population.get("occupations")
        if not isinstance(source, str) or not source.strip() or not isinstance(occupations, list) or not occupations:
            raise ForecastModelError("population.source와 occupations가 필요합니다")
        allowed_occupations = {"student", "worker", "other"}
        if any(not isinstance(item, str) or item not in allowed_occupations for item in occupations):
            raise ForecastModelError("population.occupations 값이 잘못되었습니다")
        age_min = population.get("age_min")
        age_max = population.get("age_max")
        if isinstance(age_min, bool) or isinstance(age_max, bool) or not isinstance(age_min, int) or not isinstance(age_max, int) or not 13 <= age_min <= age_max <= 100:
            raise ForecastModelError("population age_min/age_max 범위가 잘못되었습니다")

        minimum = raw.get("minimum_history_records")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 7:
            raise ForecastModelError("minimum_history_records는 1~7 정수여야 합니다")
        prep = raw.get("preprocessing")
        if not isinstance(prep, dict):
            raise ForecastModelError("preprocessing이 필요합니다")
        n = len(FEATURE_ORDER)
        medians = _number_list(prep.get("imputation_median"), "imputation_median", n)
        means = _number_list(prep.get("mean"), "mean", n)
        scales = _number_list(prep.get("scale"), "scale", n)
        if any(value <= 0 for value in scales):
            raise ForecastModelError("scale은 0보다 커야 합니다")

        raw_targets = raw.get("targets")
        if not isinstance(raw_targets, dict) or set(raw_targets) != set(TARGET_NAMES):
            raise ForecastModelError("bedtime_offset_min과 wake_offset_min target이 정확히 필요합니다")
        targets = {}
        for name in TARGET_NAMES:
            target = raw_targets[name]
            if not isinstance(target, dict):
                raise ForecastModelError(f"targets.{name}이 필요합니다")
            targets[name] = (
                _finite_number(target.get("intercept"), f"{name}.intercept"),
                _number_list(target.get("coefficients"), f"{name}.coefficients", n),
            )
        return cls(version.strip(), medians, means, scales, targets,
                   Population(source.strip(), age_min, age_max, tuple(occupations)), minimum)

    def predict(self, profile: dict, records: list[dict], cutoff: datetime) -> dict:
        eligible, reason = self.population.eligibility(profile)
        if not eligible:
            return {"available": False, "reason": reason, "model_version": self.model_version}
        features = completed_history_features(records, cutoff)
        if features is None or features["history_count_7"] < self.minimum_history_records:
            return {"available": False,
                    "reason": f"20:00 이전에 완료된 수면 기록 {self.minimum_history_records}회가 필요합니다.",
                    "model_version": self.model_version}
        raw = [features[name] for name in FEATURE_ORDER]
        # Every feature is observable when history exists. Imputation values remain
        # part of the artifact contract for reproducibility, not silent live-data fill.
        if any(not math.isfinite(value) for value in raw):
            return {"available": False, "reason": "필수 과거 수면 특징이 유효하지 않습니다.",
                    "model_version": self.model_version}
        z = [(value - self.mean[i]) / self.scale[i] for i, value in enumerate(raw)]
        offsets = {}
        for name, (intercept, coefficients) in self.targets.items():
            offsets[name] = intercept + sum(a * b for a, b in zip(coefficients, z))
        bed, wake = offsets["bedtime_offset_min"], offsets["wake_offset_min"]
        if not 0 <= bed <= 16 * 60 or not bed < wake <= 24 * 60:
            return {"available": False, "reason": "모델 출력이 지원하는 야간 시간 범위를 벗어났습니다.",
                    "model_version": self.model_version}
        return {
            "available": True,
            "cutoff": cutoff.isoformat(),
            "bed": (cutoff + timedelta(minutes=bed)).isoformat(),
            "wake": (cutoff + timedelta(minutes=wake)).isoformat(),
            "role": "과거 행동패턴 참고 · 권장 시각 계산에는 미사용",
            "warning": "공학적 연구 평가 문턱을 통과한 행동 예측 모델이며 수면 개선 효과나 임상 승인을 뜻하지 않습니다.",
            "model_version": self.model_version,
            "feature_contract": FEATURE_CONTRACT,
            "observed_history_records": int(features["history_count_7"]),
        }
