"""Portable CMI/MESA robust-v2 research comparison forecasts.

The fitted sklearn pipelines are represented as strict JSON.  This module uses
only the Python standard library and never substitutes a legacy model when an
artifact is missing or invalid.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path

from night_core import CONTRACT, GRADUAL_POLICY, LEGACY_POLICY, constrain, local_time
from robust_features import DATA_V2, FEATURE_CONTRACT, HYBRID_V2, extended_features


SCHEMA_VERSION = 2
ARTIFACT_TYPE = "sleepwell_robust_next_night_portable"
MODEL_VERSION = "robust-next-night-research-v2"
RUNTIME_MODEL_VERSION = f"{MODEL_VERSION}+{GRADUAL_POLICY}"
PHYSICAL_PROVIDER = "two_process_fixed_v1"
COHORTS = ("cmi", "mesa")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class FusionModelError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FusionModelError("모델 JSON에 중복 키가 있습니다.")
        result[key] = value
    return result


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FusionModelError(f"{label}에 유효하지 않은 숫자가 있습니다.")
    return float(value)


def _number_list(value, size, label, positive=False):
    if not isinstance(value, list) or len(value) != size:
        raise FusionModelError(f"{label} 배열 길이가 맞지 않습니다.")
    result = [_finite(item, label) for item in value]
    if positive and any(item <= 0 for item in result):
        raise FusionModelError(f"{label} 값은 양수여야 합니다.")
    return result


def _integer_list(value, label, limit):
    if (not isinstance(value, list)
            or any(isinstance(item, bool) or not isinstance(item, int) for item in value)):
        raise FusionModelError(f"{label} 인덱스가 잘못되었습니다.")
    if value != sorted(set(value)) or any(item < 0 or item >= limit for item in value):
        raise FusionModelError(f"{label} 인덱스 범위가 잘못되었습니다.")
    return value


def _validate_tree(tree, feature_count):
    if not isinstance(tree, list) or not tree:
        raise FusionModelError("트리 노드가 없습니다.")
    for node in tree:
        if not isinstance(node, dict):
            raise FusionModelError("트리 노드 형식이 잘못되었습니다.")
        if set(node) == {"leaf"}:
            _finite(node["leaf"], "leaf")
            continue
        if set(node) != {"feature", "threshold", "missing_left", "left", "right"}:
            raise FusionModelError("트리 분기 형식이 잘못되었습니다.")
        feature = node["feature"]
        if isinstance(feature, bool) or not isinstance(feature, int) or not 0 <= feature < feature_count:
            raise FusionModelError("트리 특징 인덱스가 잘못되었습니다.")
        _finite(node["threshold"], "threshold")
        if not isinstance(node["missing_left"], bool):
            raise FusionModelError("결측 분기 형식이 잘못되었습니다.")
        for key in ("left", "right"):
            index = node[key]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(tree):
                raise FusionModelError("트리 자식 인덱스가 잘못되었습니다.")
    visiting, visited = set(), set()

    def visit(index):
        if index in visiting:
            raise FusionModelError("트리에 순환 참조가 있습니다.")
        if index in visited:
            return
        visiting.add(index)
        node = tree[index]
        if "leaf" not in node:
            visit(node["left"])
            visit(node["right"])
        visiting.remove(index)
        visited.add(index)

    visit(0)
    if len(visited) != len(tree):
        raise FusionModelError("트리에 도달할 수 없는 노드가 있습니다.")


def _validate_estimator(estimator, feature_count):
    if not isinstance(estimator, dict):
        raise FusionModelError("추정기 형식이 잘못되었습니다.")
    if estimator.get("kind") == "linear":
        if set(estimator) != {"kind", "intercept", "coefficients"}:
            raise FusionModelError("선형 추정기 필드가 잘못되었습니다.")
        _finite(estimator["intercept"], "intercept")
        _number_list(estimator["coefficients"], feature_count, "coefficients")
        return
    if estimator.get("kind") == "hist_gradient_boosting":
        if set(estimator) != {"kind", "baseline", "trees"}:
            raise FusionModelError("HGB 추정기 필드가 잘못되었습니다.")
        _finite(estimator["baseline"], "baseline")
        trees = estimator["trees"]
        if not isinstance(trees, list) or not trees or len(trees) > 1000:
            raise FusionModelError("HGB 트리 수가 잘못되었습니다.")
        for tree in trees:
            _validate_tree(tree, feature_count)
        return
    raise FusionModelError("지원하지 않는 추정기입니다.")


def _validate_pipeline(model, feature_order):
    if not isinstance(model, dict) or set(model) != {"feature_order", "preprocessing", "estimators"}:
        raise FusionModelError("파이프라인 형식이 잘못되었습니다.")
    if model["feature_order"] != list(feature_order):
        raise FusionModelError("모델 특징 순서가 맞지 않습니다.")
    preprocessing = model["preprocessing"]
    expected_fields = {
        "imputer_median", "missing_indicator_features", "clip_low", "clip_high",
        "variance_keep", "scale_mean", "scale",
    }
    if not isinstance(preprocessing, dict) or set(preprocessing) != expected_fields:
        raise FusionModelError("전처리 계약이 잘못되었습니다.")
    raw_count = len(feature_order)
    _number_list(preprocessing["imputer_median"], raw_count, "imputer_median")
    indicators = _integer_list(
        preprocessing["missing_indicator_features"], "missing_indicator_features", raw_count
    )
    expanded_count = raw_count + len(indicators)
    low = _number_list(preprocessing["clip_low"], expanded_count, "clip_low")
    high = _number_list(preprocessing["clip_high"], expanded_count, "clip_high")
    if any(left > right for left, right in zip(low, high)):
        raise FusionModelError("클리핑 범위가 잘못되었습니다.")
    keep = _integer_list(preprocessing["variance_keep"], "variance_keep", expanded_count)
    _number_list(preprocessing["scale_mean"], len(keep), "scale_mean")
    _number_list(preprocessing["scale"], len(keep), "scale", positive=True)
    estimators = model["estimators"]
    if not isinstance(estimators, list) or len(estimators) != 2:
        raise FusionModelError("입면·기상 추정기 두 개가 필요합니다.")
    for estimator in estimators:
        _validate_estimator(estimator, len(keep))


def _tree_predict(tree, row):
    index = 0
    while "leaf" not in tree[index]:
        node = tree[index]
        value = row[node["feature"]]
        go_left = node["missing_left"] if math.isnan(value) else value <= node["threshold"]
        index = node["left"] if go_left else node["right"]
    return tree[index]["leaf"]


def _pipeline_predict(model, feature_values):
    preprocessing = model["preprocessing"]
    source = [float(feature_values[name]) for name in model["feature_order"]]
    if any(not math.isfinite(value) for value in source):
        raise FusionModelError("계산된 특징에 유효하지 않은 숫자가 있습니다.")
    missing = [math.isnan(value) for value in source]
    row = [
        preprocessing["imputer_median"][index] if missing[index] else value
        for index, value in enumerate(source)
    ]
    row.extend(1.0 if missing[index] else 0.0
               for index in preprocessing["missing_indicator_features"])
    row = [
        min(preprocessing["clip_high"][index],
            max(preprocessing["clip_low"][index], value))
        for index, value in enumerate(row)
    ]
    row = [row[index] for index in preprocessing["variance_keep"]]
    row = [
        (value - preprocessing["scale_mean"][index]) / preprocessing["scale"][index]
        for index, value in enumerate(row)
    ]
    output = []
    for estimator in model["estimators"]:
        if estimator["kind"] == "linear":
            predicted = estimator["intercept"] + sum(
                coefficient * value
                for coefficient, value in zip(estimator["coefficients"], row)
            )
        else:
            predicted = estimator["baseline"] + sum(
                _tree_predict(tree, row) for tree in estimator["trees"]
            )
        if not math.isfinite(predicted):
            raise FusionModelError("유효한 예측값을 계산할 수 없습니다.")
        output.append(predicted)
    return constrain(*output)


class PortableCohortModel:
    def __init__(self, path, expected_cohort):
        path = Path(path)
        try:
            payload = path.read_bytes()
            if len(payload) > MAX_ARTIFACT_BYTES:
                raise FusionModelError("모델 파일이 너무 큽니다.")
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(raw, dict):
                raise FusionModelError("모델 최상위 형식이 잘못되었습니다.")
            required = {
                "schema_version", "artifact_type", "feature_contract", "model_version", "status",
                "cohort", "decision_time", "target", "physical_provider", "prior", "models",
                "source_model_sha256", "research_metrics", "provenance", "limitations",
            }
            if set(raw) != required:
                raise FusionModelError("모델 최상위 필드가 잘못되었습니다.")
            expected_values = {
                "schema_version": SCHEMA_VERSION,
                "artifact_type": ARTIFACT_TYPE,
                "feature_contract": FEATURE_CONTRACT,
                "model_version": MODEL_VERSION,
                "status": "research_comparison_only",
                "cohort": expected_cohort,
                "decision_time": "20:00 Asia/Seoul",
                "physical_provider": PHYSICAL_PROVIDER,
            }
            for key, expected in expected_values.items():
                if raw.get(key) != expected:
                    raise FusionModelError(f"지원하지 않는 모델 계약: {key}")
            if set(raw["models"]) != {"data", "hybrid"}:
                raise FusionModelError("데이터·융합 모델이 모두 필요합니다.")
            _validate_pipeline(raw["models"]["data"], DATA_V2)
            _validate_pipeline(raw["models"]["hybrid"], HYBRID_V2)
            prior = raw["prior"]
            if not isinstance(prior, dict):
                raise FusionModelError("집단 사전값이 잘못되었습니다.")
            for key in ("bed_offset", "wake_offset"):
                _finite(prior.get(key), key)
            # Exercise the shared validator and physical model contract.
            extended_features([], local_time("2000-01-01T20:00:00+09:00"), prior,
                              "2000-01-01T20:00:00+09:00")
            if (not isinstance(raw["research_metrics"], dict)
                    or not isinstance(raw["provenance"], dict)
                    or not isinstance(raw["limitations"], list)):
                raise FusionModelError("연구 메타데이터 형식이 잘못되었습니다.")
            hashes = raw["source_model_sha256"]
            if (not isinstance(hashes, dict) or set(hashes) != {"data", "hybrid"}
                    or any(not isinstance(value, str) or len(value) != 64 for value in hashes.values())):
                raise FusionModelError("원본 모델 해시가 잘못되었습니다.")
            self.raw = raw
            self.sha256 = hashlib.sha256(payload).hexdigest()
            self.path = path
        except FusionModelError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise FusionModelError(f"연구 융합 모델을 읽을 수 없습니다: {error}") from error

    def predict(self, profile, records, cutoff, personalization_policy=LEGACY_POLICY):
        created_at = profile.get("created_at")
        if not created_at:
            raise FusionModelError("가입 시각이 없어 인과적 개인 기록 범위를 정할 수 없습니다.")
        enrollment = local_time(created_at)
        if enrollment > cutoff:
            raise FusionModelError("가입 시각이 예측 기준 이후입니다.")
        values, state = extended_features(
            records,
            cutoff,
            self.raw["prior"],
            created_at,
            personalization_policy=personalization_policy,
        )
        return {
            "features": values,
            "personalization": state,
            "physical": constrain(values["physical_bed"], values["physical_wake"]),
            "data": _pipeline_predict(self.raw["models"]["data"], values),
            "hybrid": _pipeline_predict(self.raw["models"]["hybrid"], values),
        }


class FusionForecasts:
    """Load both reference cohorts and expose research comparison predictions."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.models = {}
        self.errors = {}
        for cohort in COHORTS:
            path = self.directory / f"robust_{cohort}_v2.json"
            if not path.is_file():
                self.errors[cohort] = "모델 파일이 없습니다."
                continue
            try:
                self.models[cohort] = PortableCohortModel(path, cohort)
            except FusionModelError as error:
                self.errors[cohort] = str(error)

    def status(self):
        cohorts = {}
        for cohort in COHORTS:
            if cohort in self.models:
                model = self.models[cohort]
                cohorts[cohort] = {
                    "loaded": True,
                    "cohort": cohort,
                    "sha256": model.sha256,
                    "model_version": model.raw["model_version"],
                    "physical_provider": model.raw["physical_provider"],
                }
            else:
                cohorts[cohort] = {"loaded": False, "cohort": cohort, "error": self.errors[cohort]}
        return {
            "available": len(self.models) == len(COHORTS) and not self.errors,
            "model_version": MODEL_VERSION,
            "runtime_model_version": RUNTIME_MODEL_VERSION,
            "runtime_personalization_policy": GRADUAL_POLICY,
            "feature_contract": CONTRACT,
            "model_feature_contract": FEATURE_CONTRACT,
            "cohorts": cohorts,
            "research_metrics": {cohort: model.raw["research_metrics"]
                                 for cohort, model in self.models.items()},
            "research_metrics_scope": "frozen_legacy14_artifact_metrics",
        }

    def predict(self, profile, records, now):
        now = local_time(now)
        cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now < cutoff:
            return {
                "available": False,
                "reason": "다음날 연구 예측은 오늘 20:00 기준으로 계산됩니다.",
                "next_decision_at": cutoff.isoformat(),
                "status": self.status(),
            }
        return self.predict_at(profile, records, cutoff)

    def predict_at(self, profile, records, cutoff):
        cutoff = local_time(cutoff)
        if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
            return {"available": False, "reason": "예측 기준은 현지 시각 20:00이어야 합니다."}
        status = self.status()
        if not status["available"]:
            return {
                "available": False,
                "reason": "CMI·MESA 연구 모델 두 개를 모두 확인할 수 없습니다.",
                "status": status,
            }
        predictions = {}
        snapshots = {}
        personalization = {}
        artifacts = {}
        try:
            for cohort in COHORTS:
                model = self.models[cohort]
                result = model.predict(
                    profile,
                    records,
                    cutoff,
                    personalization_policy=GRADUAL_POLICY,
                )
                snapshots[cohort] = result["features"]
                personalization[cohort] = result["personalization"]
                artifacts[cohort] = {
                    "cohort": cohort,
                    "sha256": model.sha256,
                    "model_feature_contract": model.raw["feature_contract"],
                    "source_model_sha256": model.raw["source_model_sha256"],
                    "personalization_policy": GRADUAL_POLICY,
                }
                for arm in ("physical", "data", "hybrid"):
                    bed_offset, wake_offset = result[arm]
                    predictions[f"{arm}_{cohort}"] = {
                        "bed": (cutoff + timedelta(minutes=bed_offset)).isoformat(),
                        "wake": (cutoff + timedelta(minutes=wake_offset)).isoformat(),
                        "interval_min": wake_offset - bed_offset,
                        "onset_offset_min": bed_offset,
                        "wake_offset_min": wake_offset,
                    }
        except (FusionModelError, KeyError, TypeError, ValueError, OverflowError) as error:
            return {"available": False, "reason": f"연구 예측을 계산할 수 없습니다: {error}"}
        return {
            "available": True,
            "cutoff": cutoff.isoformat(),
            "night_id": cutoff.date().isoformat(),
            "predictions": predictions,
            "model_version": RUNTIME_MODEL_VERSION,
            "personalization_policy": GRADUAL_POLICY,
            "feature_contract": CONTRACT,
            "model_feature_contract": FEATURE_CONTRACT,
            "physical_provider": PHYSICAL_PROVIDER,
            "artifacts": artifacts,
            "feature_snapshot": snapshots,
            "personalization": personalization,
            "research_metrics": {},
            "research_metrics_scope": "not_validated_for_gradual_v1_runtime",
            "role": "다음 주수면 입면·최종 기상 시각의 연구용 코호트별 비교",
            "warning": (
                "CMI와 MESA는 별도 참고 예측이며 결합 앙상블이 아닙니다. "
                "수면 질 개선이나 일반 인구 성능을 보장하지 않습니다."
            ),
        }
