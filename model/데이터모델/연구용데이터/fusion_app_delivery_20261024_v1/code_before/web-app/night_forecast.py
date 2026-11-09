"""Portable research next-night forecasts; never silently substitute legacy models."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path

from night_core import CONTRACT, DATA_FEATURES, PHYSICAL_FEATURES, features, local_time, constrain


class NightModelError(ValueError):
    pass


def numbers(value, size):
    if not isinstance(value, list) or len(value) != size:
        raise NightModelError("모델 배열의 길이가 맞지 않습니다.")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise NightModelError("모델에 유효하지 않은 숫자가 있습니다.")
    return [float(v) for v in value]


class NightModel:
    def __init__(self, path):
        try:
            payload = Path(path).read_bytes()
            if len(payload) > 1024 * 1024:
                raise NightModelError("모델 파일이 너무 큽니다.")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise NightModelError("모델 JSON 중복 키")
                    result[key] = value
                return result
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
            if not isinstance(raw, dict):
                raise NightModelError("모델 형식이 잘못되었습니다.")
            for key, expected in {
                "schema_version": 1, "artifact_type": "sleepwell_next_night_ridge",
                "feature_contract": CONTRACT, "status": "research_evaluated", "decision_time": "20:00",
            }.items():
                if raw.get(key) != expected:
                    raise NightModelError(f"지원하지 않는 모델 계약: {key}")
            if raw.get("cohort") not in ("cmi", "mesa", "samsung"):
                raise NightModelError("모델 출처를 확인할 수 없습니다.")
            if not isinstance(raw.get("version"), str) or not raw["version"]:
                raise NightModelError("모델 버전이 없습니다.")
            if raw.get("requires_personal_archive_link") is not (raw["cohort"] == "samsung"):
                raise NightModelError("개인 모델의 연결 조건이 잘못되었습니다.")
            expected_scope = "linked_personal_history" if raw["cohort"] == "samsung" else "research_only"
            if raw.get("supports_general_population") is not False or raw.get("runtime_eligibility") != expected_scope:
                raise NightModelError("모델 적용 범위가 검증 계약과 다릅니다.")
            prior = raw["prior"]
            # Shared feature validation also checks finite and coherent prior offsets.
            features([], local_time("2000-01-01T20:00:00+09:00"), prior)
            if set(raw["models"]) != {"data", "hybrid", "hybrid_frozen"}:
                raise NightModelError("비교 모델 세 종류가 필요합니다.")
            for arm, model in raw["models"].items():
                names = list(DATA_FEATURES if arm == "data" else DATA_FEATURES + PHYSICAL_FEATURES)
                if model.get("feature_order") != names:
                    raise NightModelError("모델 특징 순서가 다릅니다.")
                n = len(names)
                numbers(model["mean"], n)
                scales = numbers(model["scale"], n)
                if any(v <= 0 for v in scales):
                    raise NightModelError("모델 scale은 양수여야 합니다.")
                numbers(model["intercepts"], 2)
                if not isinstance(model["coefficients"], list) or len(model["coefficients"]) != 2:
                    raise NightModelError("두 예측값의 계수가 필요합니다.")
                for row in model["coefficients"]:
                    numbers(row, n)
            self.raw = raw
            self.sha256 = hashlib.sha256(payload).hexdigest()
        except (OSError, KeyError, TypeError, UnicodeError, ValueError) as error:
            raise NightModelError(f"다음날 예측 모델을 읽을 수 없습니다: {error}") from error

    def predict(self, profile, records, cutoff, research_context=False):
        if self.raw["runtime_eligibility"] == "research_only" and not research_context:
            return {"available": False,
                    "reason": "집단 비교 모델은 준비됐지만 현재 사용자의 연령·기기 적용 범위는 검증되지 않았습니다.",
                    "research_metrics": self.raw.get("test_metrics", {}), "source": self.raw["cohort"]}
        if self.raw["requires_personal_archive_link"] and not profile.get("link_history"):
            return {"available": False, "reason": "본인 과거 자료 연결이 필요한 모델입니다."}
        normal, state = features(records, cutoff, self.raw["prior"], enrolled_at=profile.get("created_at"))
        frozen, _ = features(records, cutoff, self.raw["prior"], personalize=False,
                             enrolled_at=profile.get("created_at"))
        offsets = {"physical": (normal["physical_bed"], normal["physical_wake"])}
        for arm, model in self.raw["models"].items():
            f = frozen if arm == "hybrid_frozen" else normal
            x = [(f[name] - model["mean"][i]) / model["scale"][i]
                 for i, name in enumerate(model["feature_order"])]
            output = [bias + sum(a*b for a, b in zip(coefficients, x))
                      for bias, coefficients in zip(model["intercepts"], model["coefficients"])]
            if not all(math.isfinite(v) for v in output):
                return {"available": False, "reason": "유효한 예측을 계산할 수 없습니다."}
            offsets[arm] = constrain(*output)
        predictions = {}
        for arm, (bed, wake) in offsets.items():
            predictions[arm] = {"bed": (cutoff + timedelta(minutes=bed)).isoformat(),
                                "wake": (cutoff + timedelta(minutes=wake)).isoformat(),
                                "interval_min": wake-bed}
        winner = self.raw.get("validation_winner", "hybrid").split("/")[0]
        if winner not in predictions:
            winner = "hybrid"
        return {
            "available": True, "cutoff": cutoff.isoformat(), "night_id": cutoff.date().isoformat(),
            **predictions[winner], "preferred_model": winner, "predictions": predictions,
            "role": "다음 주수면의 시작·종료 시각 참고 예측",
            "warning": "연구 자료에서 비교한 예측입니다. 권장 취침 시각이나 수면 개선 효과를 뜻하지 않습니다.",
            "model_version": self.raw["version"], "artifact_sha256": self.sha256,
            "feature_contract": CONTRACT, "source": self.raw["cohort"],
            "population_scope": "일반 인구·현재 기기에 대한 검증은 별도로 필요합니다.",
            "personalization": state, "feature_snapshot": normal,
            "observed_history_records": state["valid_days"],
            "test_metrics": self.raw.get("test_metrics", {}),
        }


class NightForecasts:
    def __init__(self, directory):
        self.models, self.errors = {}, {}
        for cohort in ("cmi", "samsung"):
            path = Path(directory) / f"{cohort}_night_model.json"
            if path.exists():
                try:
                    model = NightModel(path)
                    if model.raw["cohort"] != cohort:
                        raise NightModelError("파일과 모델 출처가 다릅니다.")
                    self.models[cohort] = model
                except NightModelError as error:
                    self.errors[cohort] = str(error)

    def predict(self, profile, records, now):
        now = local_time(now)
        cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now < cutoff:
            return {"available": False, "reason": "다음날 예측은 오늘 20:00 기준으로 제공됩니다.",
                    "next_decision_at": cutoff.isoformat()}
        key = "samsung" if profile.get("link_history") and ("samsung" in self.models or "samsung" in self.errors) else "cmi"
        if key in self.errors:
            return {"available": False, "reason": self.errors[key]}
        if key not in self.models:
            return {"available": False, "reason": "검증된 다음날 예측 파일이 아직 없습니다."}
        return self.models[key].predict(profile, records, cutoff)
