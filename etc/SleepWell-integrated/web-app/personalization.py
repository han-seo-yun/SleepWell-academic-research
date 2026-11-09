"""Population warm-start selection for cumulative personalization.

The artifact contains observed group sleep duration. It is an initialization
prior, not an estimate of an individual's physiological sleep need.
"""
from __future__ import annotations

import json
import math
from pathlib import Path


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "sleepwell_population_prior"
DEFAULT_PATH = Path(__file__).resolve().parent / "models" / "population_prior.json"
AGE_GROUPS = ((18, 29, "18-29"), (30, 44, "30-44"), (45, 59, "45-59"), (60, 80, "60-80"))


def unavailable(reason: str, source: str | None = None) -> dict:
    return {"available": False, "artifact": None, "reason": reason, "source": source}


def load_population_prior(path: str | Path | None = None) -> dict:
    """Load and minimally validate a versioned population-prior artifact."""
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return unavailable("집단 사전값 파일이 없어 가입 시 입력값을 사용합니다.", str(target))
    try:
        artifact = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            raise ValueError("최상위 형식 오류")
        if artifact.get("schema_version") != SCHEMA_VERSION or artifact.get("artifact_type") != ARTIFACT_TYPE:
            raise ValueError("지원하지 않는 스키마 또는 자료 유형")
        supported = artifact["supported_age"]
        if not (isinstance(supported, list) and len(supported) == 2 and
                all(isinstance(x, int) and not isinstance(x, bool) for x in supported) and
                supported == [18, 80]):
            raise ValueError("supported_age 형식 오류")
        if not isinstance(artifact.get("version"), str) or not artifact["version"]:
            raise ValueError("version 누락")
        if not isinstance(artifact.get("source"), str) or not artifact["source"]:
            raise ValueError("source 누락")
        groups = artifact.get("groups")
        if not isinstance(groups, list):
            raise ValueError("groups 형식 오류")
        seen = set()
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("group 항목 형식 오류")
            if group.get("age_group") not in {x[2] for x in AGE_GROUPS}:
                raise ValueError("age_group 오류")
            if group.get("sex") not in {"male", "female", "any"}:
                raise ValueError("sex 오류")
            if group.get("activity_band") not in {"low", "medium", "high", "any"}:
                raise ValueError("activity_band 오류")
            key = (group["age_group"], group["sex"], group["activity_band"])
            if key in seen:
                raise ValueError("중복 집단 키")
            seen.add(key)
            n_people, duration = group.get("n_people"), group.get("mean_sleep_min")
            if (not isinstance(n_people, int) or isinstance(n_people, bool) or not 0 <= n_people <= 1_000_000_000 or
                    not isinstance(duration, (int, float)) or isinstance(duration, bool) or
                    not math.isfinite(duration) or not 30 <= duration <= 960):
                raise ValueError("집단 통계 오류")
        global_prior = artifact.get("global")
        if not isinstance(global_prior, dict):
            raise ValueError("global 누락")
        n_people, duration = global_prior.get("n_people"), global_prior.get("mean_sleep_min")
        if (not isinstance(n_people, int) or isinstance(n_people, bool) or not 0 <= n_people <= 1_000_000_000 or
                not isinstance(duration, (int, float)) or isinstance(duration, bool) or
                not math.isfinite(duration) or not 30 <= duration <= 960):
            raise ValueError("전체 통계 오류")
        unsupported = artifact.get("unsupported_fields")
        if not (isinstance(unsupported, list) and all(isinstance(x, str) for x in unsupported) and
                len(unsupported) == len(set(unsupported)) and {"occupation", "usual_bed"}.issubset(unsupported)):
            raise ValueError("unsupported_fields 형식 오류")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return unavailable(f"집단 사전값을 안전하게 읽지 못해 가입 시 입력값을 사용합니다: {error}", str(target))
    return {"available": True, "artifact": artifact, "reason": "사용 가능", "source": str(target)}


def age_group(age: int) -> str | None:
    return next((label for low, high, label in AGE_GROUPS if low <= age <= high), None)


def activity_band(hours_per_day: float | None) -> str:
    if hours_per_day is None:
        return "any"
    minutes = hours_per_day * 60
    return "low" if minutes < 30 else "medium" if minutes < 90 else "high"


def select_population_prior(profile: dict, loaded: dict, minimum_people: int = 30) -> dict:
    """Choose the narrowest sufficiently supported group, then broaden safely."""
    if not isinstance(minimum_people, int) or isinstance(minimum_people, bool) or minimum_people < 30:
        raise ValueError("minimum_people는 30 이상의 정수여야 합니다")
    if not loaded.get("available"):
        return {"available": False, "reason": loaded.get("reason"), "source": loaded.get("source")}
    artifact = loaded["artifact"]
    age = int(profile["age"])
    low, high = artifact["supported_age"]
    group_age = age_group(age)
    if group_age is None or not low <= age <= high:
        return {"available": False, "reason": f"연령 {age}세는 집단 사전값 지원 범위 {low}~{high}세 밖이어서 가입 시 입력값을 사용합니다.",
                "source": artifact["source"], "version": artifact["version"]}
    sex = profile.get("sex", "unspecified")
    selected_sex = sex if sex in {"male", "female"} else "any"
    raw_hours = profile.get("activity_hours_per_day")
    hours = None if raw_hours in (None, "") else float(raw_hours)
    band = activity_band(hours)
    keys = list(dict.fromkeys([(group_age, selected_sex, band), (group_age, selected_sex, "any"),
                              (group_age, "any", "any")]))
    groups = artifact["groups"]
    for age_key, sex_key, activity_key in keys:
        match = next((g for g in groups if g["age_group"] == age_key and g["sex"] == sex_key and
                      g["activity_band"] == activity_key and g["n_people"] >= minimum_people), None)
        if match:
            return {"available": True, "mean_sleep_min": float(match["mean_sleep_min"]), "n_people": match["n_people"],
                    "level": f"{age_key}/{sex_key}/{activity_key}", "version": artifact["version"],
                    "source": artifact["source"], "reason": "인구집단에서 관측된 평균 수면시간"}
    global_prior = artifact["global"]
    if global_prior["n_people"] >= minimum_people:
        return {"available": True, "mean_sleep_min": float(global_prior["mean_sleep_min"]),
                "n_people": global_prior["n_people"], "level": "global", "version": artifact["version"],
                "source": artifact["source"], "reason": "세부 집단 표본이 부족해 전체 관측 평균 사용"}
    return {"available": False, "reason": f"30명 이상인 지원 집단이 없어 가입 시 입력값을 사용합니다.",
            "source": artifact["source"], "version": artifact["version"]}
