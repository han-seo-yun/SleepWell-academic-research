"""Export bounded public aggregate tables from frozen validity artifacts.

The export intentionally excludes participant-level rows, local paths, hashes, and
model objects.  It is a reporting transform only: it does not fit, select, or
promote a model.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


COHORTS = ("cmi", "mesa")
MODELS = ("physical", "data", "hybrid")


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid JSON object: {path.name}")
    return value


def _number(value, label: str) -> float | int:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Invalid numeric field: {label}")
    return value


def _count(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid count: {label}")
    return value


def _ci(value, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"Invalid interval: {label}")
    return float(_number(value[0], label)), float(_number(value[1], label))


def _panel(identifier: str, title: str, columns: list[str], rows: list[list], *notes: str) -> dict:
    return {"id": identifier, "title": title, "columns": columns, "rows": rows, "notes": list(notes)}


def _validate_inputs(full: dict, labels: dict, psg: dict, profiles: dict) -> None:
    if (full.get("schema_version") != 2
            or full.get("artifact_type") != "sleepwell_full_target_validity"
            or full.get("test_status") != "retrospective_reused_test"
            or full.get("no_service_promotion") is not True):
        raise ValueError("Full-validity research contract mismatch")
    if set(full.get("cohorts", {})) != set(COHORTS):
        raise ValueError("Full-validity cohorts mismatch")
    if (labels.get("schema_version") != 1
            or labels.get("artifact_type") != "sleepwell_label_sensitivity"
            or labels.get("study_status") != "retrospective_reused_test"
            or labels.get("no_service_promotion") is not True
            or set(labels.get("cohorts", {})) != set(COHORTS)):
        raise ValueError("Label-sensitivity research contract mismatch")
    if (psg.get("schema_version") != 1
            or psg.get("purpose") != "retrospective wrist-sensor classification against independently scored PSG"
            or psg.get("protocol_frozen_before_test") is not True
            or psg.get("selected_before_test") != "300s:activity_light_hgb"):
        raise ValueError("PSG experiment contract mismatch")
    if (profiles.get("schema_version") != 1
            or profiles.get("artifact_type") != "sleepwell_endpoint_period_profiles"
            or profiles.get("test_status") != "retrospective_reused_test"
            or profiles.get("no_service_promotion") is not True
            or not isinstance(profiles.get("rows"), list) or len(profiles["rows"]) != 18):
        raise ValueError("Endpoint-period profile contract mismatch")


def _baselines(full: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        item = full["cohorts"][cohort]
        people, nights = _count(item["n_people"], "people"), _count(item["n_nights"], "nights")
        for score in item["scores"]:
            low, high = _ci(score["ci95_min"], "baseline ci")
            rows.append([cohort.upper(), score["name"], people, nights,
                         _number(score["onset_mae_min"], "onset mae"),
                         _number(score["wake_mae_min"], "wake mae"),
                         _number(score["joint_mae_min"], "joint mae"),
                         score["skill_vs_recent14"], score["gain_vs_recent14_min"], low, high])
    return _panel("baselines", "동일 Test에서의 수치 기준선과 연구 모델",
                  ["코호트", "모델", "사람", "밤", "입면 MAE(분)", "기상 MAE(분)",
                   "공동 MAE(분)", "mean14 대비 skill", "mean14 대비 이득(분)", "이득 CI 하한", "이득 CI 상한"],
                  rows,
                  "사람별 오차를 먼저 평균한 participant-macro 결과이다.",
                  "Test를 재사용한 탐색 비교이며 새 외부검증이나 서비스 모델 승격이 아니다.",
                  "물리·데이터·융합 모델은 이미 동결된 예측을 재표현했으며 이 내보내기에서 새 모델을 학습하지 않았다.")


def _profiles(profiles: dict) -> dict:
    rows = []
    seen = set()
    for value in profiles["rows"]:
        key = (value.get("cohort"), value.get("target"), value.get("model"))
        if (key[0] not in COHORTS or key[1] not in ("onset", "wake", "joint")
                or key[2] not in MODELS or key in seen):
            raise ValueError("Invalid endpoint-period profile row")
        seen.add(key)
        rows.append([key[0].upper(), key[1], key[2], _count(value["n_windows"], "windows"),
                     _count(value["eligible_windows"], "eligible windows"),
                     _count(value["n_people"], "profile people"), value["median_participant_window_skill"],
                     value["q25_skill"], value["q75_skill"], value["positive_participant_window_fraction"],
                     value["equal_participant_mean_skill"]])
    expected = {(cohort, target, model) for cohort in COHORTS
                for target in ("onset", "wake", "joint") for model in MODELS}
    if seen != expected:
        raise ValueError("Incomplete endpoint-period profiles")
    return _panel("profiles", "개인-기간 14일 창의 기준선 상대 성능",
                  ["코호트", "목표", "모델", "전체 창", "적격 창", "사람", "창 skill 중앙값", "Q25", "Q75",
                   "양의 창 비율", "사람동일가중 평균 skill"],
                  rows,
                  "분석 단위는 참가자가 아니라 participant-window이다. 여러 창을 가진 사람은 이 요약에 더 큰 가중치를 갖는다.",
                  "창마다 최소 4밤과 mean14 MAE 1분 이상을 요구했으며 짧은 계열의 탐색적 기술통계이다.",
                  "개인 수준의 효과 추론에는 사람별 선집계 또는 계층적 재표집이 추가로 필요하다.")


def _within(full: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        for model, targets in full["cohorts"][cohort]["within_person"].items():
            for target, value in targets.items():
                rows.append([cohort.upper(), model, target,
                             _count(value["eligible_people"], "within eligible"),
                             _count(value["excluded_short_or_constant_people"], "within excluded"),
                             value["median_centered_r2"]])
    return _panel("within", "사람 내 변동 설명 진단",
                  ["코호트", "모델", "목표", "적격 사람", "짧음·상수로 제외", "centered R² 중앙값"], rows,
                  "Test 결과를 사람별로 중심화한 평가 진단이며 예측 입력이나 모델 선택에는 사용하지 않았다.",
                  "음의 centered R²는 개인 평균만 사용하는 것보다 야간 변동을 덜 설명했음을 뜻하며 생리적 예측가능성과 동일하지 않다.")


def _stability(full: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        for value in full["cohorts"][cohort]["split_half"]:
            low, high = _ci(value["ci95_min"], "split-half ci")
            rows.append([cohort.upper(), value["model"], _count(value["n_people"], "split people"),
                         value["early_mae_min"], value["late_mae_min"], value["early_minus_late_mae_min"],
                         low, high, value["skill_spearman"]])
    return _panel("stability", "동일 사람의 전반부·후반부 안정성",
                  ["코호트", "모델", "사람", "전반 MAE(분)", "후반 MAE(분)", "전반-후반(분)",
                   "CI 하한", "CI 상한", "skill Spearman"], rows,
                  "8밤 이상인 같은 사람을 시간순으로 둘로 나눈 기술 분석이다.",
                  "시간, 적응, 표본구성 및 측정 변화가 혼재하므로 후반 오차 감소를 학습 효과로 해석할 수 없다.")


def _variability(full: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        for value in full["cohorts"][cohort]["variability_audit"]:
            rows.append([cohort.upper(), value["model"], value["variability_vs_mae_spearman"],
                         value["variability_vs_naive_global_scale_score_spearman"],
                         value["variability_vs_baseline_skill_spearman"]])
    return _panel("variability", "개인 변동성과 성능지표의 관계",
                  ["코호트", "모델", "변동성-MAE Spearman", "변동성-전역척도점수 Spearman", "변동성-기준선 skill Spearman"], rows,
                  "상관은 기술적 연관이며 원인, 모델 편향의 기전 또는 임상적 차이를 입증하지 않는다.",
                  "전역척도점수는 고정 척도에서 MAE를 단조 변환하므로 MAE 상관과 독립된 증거가 아니다.")


def _feasibility(full: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        value = full["cohorts"][cohort]["feasibility"]
        rows.append([cohort.upper(), _count(value["n_people"], "feasibility people"),
                     _count(value["profile_min4_people"], "profile people"),
                     _count(value["split_half_min8_people"], "split people"),
                     _count(value["longitudinal_min28days_min16nights_gap7_people"], "longitudinal people"),
                     "통과" if value["longitudinal_gate_min30_people_passed"] is True else "미통과"])
    return _panel("feasibility", "개인·장기 분석 가능성 사전 점검",
                  ["코호트", "전체 사람", "4밤 이상", "8밤 이상", "28일·16밤·최대공백7일", "30명 계획 기준"], rows,
                  "오차를 보기 전에 계산한 연구 보고 가능성 점검이다.",
                  "30명 기준은 계획 규칙이지 효과크기 기반 검정력 계산이 아니며 일상적 개인화 갱신을 막는 기준도 아니다.",
                  "두 코호트 모두 장기 기준을 통과하지 않아 장기 습관개선이나 개인화 학습 효과를 입증할 수 없다.")


def _repeat(full: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        for stratum in full["cohorts"][cohort]["repeat_sensitivity"]:
            for score in stratum["scores"]:
                rows.append([cohort.upper(), stratum["stratum"], _count(stratum["n_people"], "repeat people"),
                             _count(stratum["n_nights"], "repeat nights"), score["name"],
                             score["joint_mae_min"], score["skill_vs_recent14"], score["gain_vs_recent14_min"]])
    return _panel("repeat", "반복쌍·기록공백 민감도",
                  ["코호트", "층", "사람", "밤", "모델", "공동 MAE(분)", "mean14 대비 skill", "이득(분)"], rows,
                  "연속 두 밤의 입면·기상 오프셋이 모두 0.5분 이내인 경우만 반복쌍 민감도에서 제외했다.",
                  "동일값은 중복의 증거가 아니며 기록공백 층도 착용중단이나 수면부재를 뜻하지 않는다.")


def _labels(labels: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        item = labels["cohorts"][cohort]
        ambiguity = item["candidate_ambiguity"]
        for variant, value in item["target_variants"].items():
            for model, score in value["models"].items():
                low, high = _ci(score["ci95_min"], "label ci")
                rows.append([cohort.upper(), variant, _count(value["matched_nights"], "matched nights"),
                             _count(value["changed_nights"], "changed nights"), model,
                             score["joint_mae_min"], score["alternative_minus_original_mae_min"], low, high,
                             score["participants_lower_error_under_alternative_fraction"]])
        if ambiguity["multiple_candidate_nights"] > item["n_nights"]:
            raise ValueError("Candidate ambiguity exceeds cohort nights")
    return _panel("labels", "주수면 목표 정의 민감도",
                  ["코호트", "목표 정의", "매칭 밤", "변경 밤", "모델", "공동 MAE(분)",
                   "대안-원본 MAE(분)", "CI 하한", "CI 상한", "대안에서 낮은오차 사람비율"], rows,
                  "대안 목표는 민감도 분석이며 임상적 참값 후보가 아니다.",
                  "목표 정의를 바꾸면 모델을 개선하지 않아도 측정 오차가 달라질 수 있다.",
                  "MESA에서 longest·earliest·latest의 차이는 낮잠/주수면 모호성을 보여주며 원자료를 덮어쓰지 않았다.")


def _quality(labels: dict) -> dict:
    rows = []
    for cohort in COHORTS:
        value = labels["cohorts"][cohort]["fixed_quality_rule"]
        for model, all_score in value["all_models"].items():
            subset = value["subset_models"][model]
            rows.append([cohort.upper(), model, _count(labels["cohorts"][cohort]["n_nights"], "all nights"),
                         _count(value["selected_nights"], "quality nights"),
                         _count(value["selected_people"], "quality people"),
                         _count(value["excluded_nights"], "excluded nights"),
                         all_score["joint_mae_min"], subset["joint_mae_min"],
                         subset["joint_mae_min"] - all_score["joint_mae_min"]])
    return _panel("quality", "전체 밤과 고정 측정품질 부분집합",
                  ["코호트", "모델", "전체 밤", "품질 밤", "품질 사람", "제외 밤", "전체 공동 MAE", "부분집합 공동 MAE", "부분집합-전체"], rows,
                  "품질 규칙은 결과오차를 보기 전에 고정했다.",
                  "전체-부분집합 차이는 측정품질과 사람·밤 표본구성의 차이를 함께 포함하며 품질처리의 인과 효과가 아니다.",
                  "주 분석은 전체 밤을 유지한다.")


def _constructs(labels: dict) -> dict:
    construct = labels["cohorts"]["mesa"].get("actigraphy_construct")
    if not isinstance(construct, dict):
        raise ValueError("Missing MESA actigraphy constructs")
    definitions = construct["definitions"]
    rows = []
    for name, value in construct.items():
        if name == "definitions":
            continue
        rows.append([name, value["median"], value["q25"], value["q75"], definitions[name]])
    return _panel("constructs", "MESA ACT 측정구성의 분포와 정의",
                  ["구성", "중앙값", "Q25", "Q75", "정의"], rows,
                  "ACT wake와 REST-S에서 계산한 약한 actigraphy 구성으로 PSG 수면단계나 임상적 수면효율이 아니다.",
                  "first-to-last sleep span은 중간 각성을 포함하며 TST와 동일하지 않다.")


def _psg(psg: dict) -> dict:
    selected = psg["test_all_frozen_candidates_descriptive"]["300s:activity_light_hgb"]["projected_shared_native_30s_grid"]
    frozen = psg["frozen_ACT_model_on_same_PSG_test"]
    paired = psg["PSG_selected_minus_frozen_ACT_paired_subject_bootstrap"]
    low, high = _ci(paired["ci95"], "psg paired ci")
    if (_count(selected["subjects"], "psg selected people") != 151
            or _count(frozen["subjects"], "psg act people") != 151):
        raise ValueError("Unexpected PSG test population")
    rows = [
        ["PSG 선택모델 300초 문맥→30초 격자", selected["subjects"], selected["rows"],
         selected["subject_macro_balanced_accuracy"], None, None, None],
        ["동결 ACT 모델 동일 PSG Test", frozen["subjects"], frozen["rows"],
         frozen["subject_macro_balanced_accuracy"], None, None, None],
        ["사람쌍 PSG선택-ACT 차이", paired["subjects"], None, None,
         paired["mean_difference"], low, high],
    ]
    return _panel("psg", "독립 PSG 주석에 대한 후향적 수면·각성 분류",
                  ["비교", "Test 사람", "30초 행", "사람-macro balanced accuracy", "BA 차이", "CI 하한", "CI 상한"], rows,
                  "151명 Test에서 PSG 주석에 정렬한 후향적 30초 수면·각성 분류이다.",
                  "선택모델 BA 0.79142867, 동일 PSG Test의 동결 ACT 모델 BA 0.71102793, 사람쌍 차이 CI 0.0713674–0.0891482이다.",
                  "다음날 입면·기상 예측, 권장시간 또는 권고 효과와 별개의 과제이며 외부검증이 아니다.")


def _applicability() -> dict:
    rows = [
        ["방법 1", "적용", "동결 Test 예측을 인구·최근·요일 기준선과 비교", "Test 재사용 탐색분석"],
        ["방법 2", "적용", "개인-기간 창, 사람 내 변동, 전후반 안정성", "창 가중 결과이며 개인 효과 아님"],
        ["방법 3", "적용", "변동성·기록공백·반복쌍·장기 가능성 점검", "연관·가능성 기술, 인과 아님"],
        ["방법 4", "적용", "주수면 선택, 품질 부분집합, ACT 구성, PSG 분류", "측정대상 민감도와 별도 PSG 과제"],
        ["실제 최초입력 지연", "미측정", "보관 백업에서 처음 관측된 시각만 계산 가능", "최초 등록시각 또는 전체기간 true first가 아님"],
        ["착용·기기·OS", "미측정", "현재 수면기록 계약에 검증 필드 없음", "결측수면에서 비착용을 추론하지 않음"],
        ["주간·다른 예측기간", "미검증", "현재 결과는 다음 주수면 입면·기상", "별도 목표·purged window·재학습 필요"],
        ["권장일정 효과", "미검증", "예측정확도와 권고 노출·순응·효과는 다른 질문", "전향적 비교 실험 필요"],
    ]
    return _panel("applicability", "수동감지 타당성 방법의 적용범위",
                  ["항목", "상태", "현재 근거", "경계"], rows,
                  "방법 1–4를 현재 저장 자료에 가능한 범위로 모두 적용했다.",
                  "이 공개표 생성은 기존 산출물의 집계 변환이며 새 모델 학습·선택·서비스 승격을 하지 않는다.",
                  "예측, 추천, 효과는 서로 다른 연구목표이며 한 목표의 성능으로 다른 목표를 입증할 수 없다.")


def build(full: dict, labels: dict, psg: dict, profiles: dict) -> dict:
    _validate_inputs(full, labels, psg, profiles)
    panels = [_baselines(full), _profiles(profiles), _within(full), _stability(full),
              _variability(full), _feasibility(full), _repeat(full), _labels(labels),
              _quality(labels), _constructs(labels), _psg(psg), _applicability()]
    return {"schema_version": 1, "artifact_type": "sleepwell_full_validity_tables",
            "version": "2026-10-25-public-v2", "test_status": "retrospective_reused_test",
            "no_service_promotion": True, "panels": panels}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--psg", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = build(_load(args.full), _load(args.labels), _load(args.psg), _load(args.profiles))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"panels": len(artifact["panels"]),
                      "rows": {panel["id"]: len(panel["rows"]) for panel in artifact["panels"]}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
