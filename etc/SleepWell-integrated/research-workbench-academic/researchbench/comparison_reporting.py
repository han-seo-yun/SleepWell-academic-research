"""Deterministic, run-bound reporting for selected SleepWell model comparisons."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .comparison_metrics import comparison_data
from .result_adapter import adapt_run, digest, verify_run


TEMPLATE = "model_comparison"
SELECTION_KEYS = {"run_id", "result_hash", "model_ids", "target", "averaging", "tolerance"}
METRICS = ("mae", "rmse", "bias", "within_tolerance")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _stable_id(*values: Any) -> str:
    return "comparison_" + _sha(values)[:20]


def _number(value: Any) -> str:
    return format(float(value), ".12g")


def _validate_selection(selection: Any) -> dict[str, Any]:
    if not isinstance(selection, dict) or set(selection) != SELECTION_KEYS:
        raise ValueError("comparison_selection_schema_invalid")
    run_id, result_hash = selection.get("run_id"), selection.get("result_hash")
    models = selection.get("model_ids")
    if not isinstance(run_id, str) or not run_id or not isinstance(result_hash, str) or len(result_hash) != 64:
        raise ValueError("comparison_selection_identity_invalid")
    if (not isinstance(models, list) or not models or len(models) > 50
            or any(not isinstance(value, str) or not value or len(value) > 128 for value in models)
            or len(set(models)) != len(models)):
        raise ValueError("comparison_model_selection_invalid")
    if selection.get("target") not in {"onset", "wake", "interval"}:
        raise ValueError("comparison_target_invalid")
    if selection.get("averaging") not in {"participant_equal", "night_equal"}:
        raise ValueError("comparison_averaging_invalid")
    tolerance = selection.get("tolerance")
    if (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float))
            or not math.isfinite(float(tolerance)) or not 0 <= float(tolerance) <= 1440):
        raise ValueError("comparison_tolerance_invalid")
    return {
        "run_id": run_id, "result_hash": result_hash, "model_ids": list(models),
        "target": selection["target"], "averaging": selection["averaging"],
        "tolerance": float(tolerance),
    }


def _aggregate(data: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    rows = [{key: row[key] for key in ("model_id", "mae", "rmse", "bias", "within_tolerance",
                                       "n_nights", "n_participants")} for row in data["rows"]]
    summary = {
        "rows": rows,
        "n_nights": data["n_nights"],
        "n_participants": data["n_participants"],
        "excluded_counts": copy.deepcopy(data["excluded_counts"]),
        "comparison_scope": "common_participant_cutoff_selected_models",
        "raw_prediction_rows_included": False,
        "participant_identifiers_included": False,
    }
    evidence = {"selection": copy.deepcopy(selection), "summary": summary}
    evidence["snapshot_sha256"] = _sha(evidence)
    return evidence


def _leaders(rows: list[dict[str, Any]], metric: str, *, higher: bool = False) -> tuple[list[str], float, float | None]:
    ordered = sorted(rows, key=lambda row: ((-1 if higher else 1) * float(row[metric]), row["model_id"]))
    best = float(ordered[0][metric])
    tied = [row["model_id"] for row in ordered if math.isclose(float(row[metric]), best, rel_tol=0, abs_tol=1e-9)]
    runner = next((float(row[metric]) for row in ordered if row["model_id"] not in tied), None)
    return tied, best, None if runner is None else abs(runner - best)


def _ranking(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric, higher in (("mae", False), ("rmse", False), ("within_tolerance", True)):
        leaders, value, difference = _leaders(rows, metric, higher=higher)
        result[metric] = {"leaders": leaders, "value": value, "runner_up_difference": difference,
                          "direction": "higher" if higher else "lower"}
    result["leaders_agree"] = (
        set(result["mae"]["leaders"]) == set(result["rmse"]["leaders"])
        == set(result["within_tolerance"]["leaders"])
    )
    return result


def _metric_text(evidence: dict[str, Any], lang: str) -> str:
    value = _number(evidence["value"])
    model, metric = evidence["model_id"], evidence["metric"]
    unit = "%" if metric == "within_tolerance_percent" else "분"
    if lang == "ko":
        return (f"선택 비교에서 {model}의 {metric}는 {value}{unit}이다 "
                f"(공통 {evidence['n_rows']}밤, {evidence['n_participants']}명).")
    unit = "%" if metric == "within_tolerance_percent" else " min"
    return (f"In the selected comparison, {model} {metric} was {value}{unit} "
            f"({evidence['n_rows']} common nights, {evidence['n_participants']} participants).")


def _metric_claims(evidence: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    selection, summary = evidence["selection"], evidence["summary"]
    snapshot_hash = evidence["snapshot_sha256"]
    claims = []
    for row in summary["rows"]:
        for metric in METRICS:
            report_metric = "within_tolerance_percent" if metric == "within_tolerance" else metric
            value = float(row[metric]) * 100 if metric == "within_tolerance" else float(row[metric])
            claim_evidence = {
                "kind": "comparison_metric", "run_id": selection["run_id"],
                "result_hash": selection["result_hash"], "source_hash": selection["result_hash"],
                "comparison_snapshot_sha256": snapshot_hash,
                "model_id": row["model_id"], "metric": report_metric, "value": value,
                "unit": "%" if metric == "within_tolerance" else "min",
                "n_rows": row["n_nights"], "n_participants": row["n_participants"],
                "target": selection["target"], "averaging": selection["averaging"],
                "tolerance": selection["tolerance"],
            }
            claims.append({
                "claim_id": _stable_id(selection["result_hash"], row["model_id"], report_metric,
                                       selection["target"], selection["averaging"], selection["tolerance"]),
                "claim_type": "comparison_metric_fact", "text": _metric_text(claim_evidence, lang),
                "rendered_value": _number(value), "evidence": claim_evidence,
                "review_state": "not_required_for_numeric_identity",
            })
    return claims


def _winner_paragraphs(evidence: dict[str, Any], lang: str) -> list[str]:
    summary, selection = evidence["summary"], evidence["selection"]
    ranking = _ranking(summary["rows"])
    tolerance = selection["tolerance"]

    if len(summary["rows"]) == 1:
        row = summary["rows"][0]
        if lang == "ko":
            return [
                f"선택한 모델은 {row['model_id']} 1개이며 MAE {row['mae']:.2f}분, RMSE {row['rmse']:.2f}분, "
                f"±{tolerance:g}분 허용오차 내 비율 {row['within_tolerance']*100:.1f}%의 점추정치를 보였다.",
                "단일 모델 성능 요약이므로 모델 간 순위·우열·차이 해석은 생략한다.",
            ]
        return [
            f"The single selected model, {row['model_id']}, had point estimates of MAE {row['mae']:.2f} min, "
            f"RMSE {row['rmse']:.2f} min, and {row['within_tolerance']*100:.1f}% within ±{tolerance:g} min.",
            "Because this is a single-model performance summary, between-model ranking, superiority, and differences are not interpreted.",
        ]

    def names(metric: str) -> str:
        return ", ".join(ranking[metric]["leaders"])

    mae, rmse, rate = ranking["mae"], ranking["rmse"], ranking["within_tolerance"]
    difference = mae["runner_up_difference"]
    difference_text = (None if difference is None else
                       f"{difference:.2f}" if difference >= .005 else f"{difference:.3g}")
    mae_diff = "없음" if difference_text is None else f"{difference_text}분"
    mae_tied = len(mae["leaders"]) > 1
    if lang == "ko":
        lines = [
            (f"주 지표 MAE 점추정치가 같은 공동 선두는 {names('mae')} ({mae['value']:.2f}분)이며 고유 선두는 없다. " if mae_tied else
             f"주 지표 MAE 점추정치가 가장 낮은 모델은 {names('mae')} ({mae['value']:.2f}분)이다. ") +
            f"다음 순위와의 차이는 {mae_diff}이며, 이 선택 자료·대상·집계에서만 우선 모델로 해석한다.",
            f"RMSE가 가장 낮은 모델은 {names('rmse')} ({rmse['value']:.2f}분), "
            f"±{tolerance:g}분 허용오차 내 비율이 가장 높은 모델은 {names('within_tolerance')} ({rate['value']*100:.1f}%)이다.",
        ]
        lines.append("세 기준의 선두가 일치한다." if ranking["leaders_agree"] else
                     "MAE·RMSE·허용오차 기준의 선두가 일치하지 않아 사용 목적에 따른 절충이 필요하다.")
        return lines
    mae_diff_en = "not available" if difference_text is None else f"{difference_text} min"
    lines = [
        ((f"The tied lowest MAE point estimate was observed for {names('mae')} ({mae['value']:.2f} min), so there is no unique leader. " if mae_tied else
          f"The lowest MAE point estimate was observed for {names('mae')} ({mae['value']:.2f} min). ") +
         f"The difference from the next rank was {mae_diff_en}; this supports preference only within the selected data, target, and aggregation."),
        f"The lowest RMSE was observed for {names('rmse')} ({rmse['value']:.2f} min), while the highest rate within "
        f"±{tolerance:g} min was observed for {names('within_tolerance')} ({rate['value']*100:.1f}%).",
    ]
    lines.append("The leaders agree across all three criteria." if ranking["leaders_agree"] else
                 "The MAE, RMSE, and tolerance-rate leaders differ, so the intended use determines the trade-off.")
    return lines


def _sections(evidence: dict[str, Any], run_ref: dict[str, Any], claims: list[dict[str, Any]],
              question: str, lang: str) -> list[dict[str, Any]]:
    selection, summary = evidence["selection"], evidence["summary"]
    metric_ids = [claim["claim_id"] for claim in claims if claim["claim_type"] == "comparison_metric_fact"]
    literature_ids = [claim["claim_id"] for claim in claims if claim["claim_type"] == "literature_retrieval"]
    scope = run_ref.get("selected_scope") or {}
    origin = run_ref.get("evaluation", {}).get("synthetic")
    observed = "합성" if origin is True else ("실제 관측으로 선언된" if origin is False else "출처 미확인")
    target_ko = {"onset": "취침 시각", "wake": "기상 시각", "interval": "취침–기상 구간"}[selection['target']]
    averaging_ko = {"participant_equal": "참여자 동일 가중", "night_equal": "밤 동일 가중"}[selection['averaging']]
    exclusions = summary['excluded_counts']
    excluded_ko = "모델별 원 기록 / 유효하지 않은 쌍 / 공통 집합 밖 제외: " + "; ".join(
        f"{model} {exclusions[model]['total_rows']} / {exclusions[model]['invalid_pairs']} / {exclusions[model]['outside_common_nights']}건"
        for model in selection['model_ids']) + "."
    excluded_en = "Per model: original rows / invalid pairs / valid rows outside the common set: " + "; ".join(
        f"{model} {exclusions[model]['total_rows']} / {exclusions[model]['invalid_pairs']} / {exclusions[model]['outside_common_nights']}"
        for model in selection['model_ids']) + "."
    if lang == "ko":
        rows = [
            ("purpose", "목적", [question or "선택한 SleepWell 회귀 모델을 동일한 공통 기록에서 비교하고 지침 관련 한계를 보고한다."]),
            ("comparison_scope", "비교 설정과 자료 범위", [
                f"모델: {', '.join(selection['model_ids'])}. 예측 대상: {target_ko}. "
                f"집계: {averaging_ko}. 허용오차: ±{selection['tolerance']:g}분.",
                f"{observed} 기존 자료의 공통 {summary['n_nights']}밤·{summary['n_participants']}명을 비교했다. "
                f"split={scope.get('split', 'unknown')}; 원시 참여자 행과 식별자는 보고서에 포함하지 않았다.",
                excluded_ko,
            ]),
            ("methods", "지표와 비교 방법", [
                "선택한 모든 모델에서 참여자·예측시점이 같은 유효 기록의 교집합만 사용했다. MAE는 주 순위 지표이고 RMSE, 평균 편향, 허용오차 내 비율을 함께 계산했다.",
                "참여자 동일 가중은 참여자별 지표를 먼저 계산한 뒤 사람 간 평균한다. 이때 RMSE도 참여자별 RMSE의 평균이다. 밤 동일 가중은 공통 밤마다 같은 가중치를 부여한다.",
            ]),
            ("results", "모델별 성능 지표", []),
            ("interpretation", "조건부 비교 해석", _winner_paragraphs(evidence, lang) + [
                "수치 판단 근거는 위의 동일 관측 비교표와 뒤의 그래프이다. 원 실행과 비교 스냅샷의 식별 정보는 재현성 항목에 기록했다. "
                "보고·해석 범위는 TRIPOD+AI 12e, 23a, 25, 26을 참고하며 문헌 제목·페이지는 검색 근거 항목에 제시한다.",
            ]),
            ("uncertainty", "불확실성", [
                "현재 선택 비교에는 참여자 군집 신뢰구간이나 사전 정의된 동등성 범위가 없어 점추정치 차이의 통계적 유의성 또는 실질적 우월성을 확정할 수 없다.",
            ]),
            ("tripod_probast", "TRIPOD+AI·PROBAST+AI 적용", [
                "이 보고서는 선택한 기존 모델을 기록된 공통 평가 자료에서 탐색 비교하는 보고 보조물이다. 외부·전향 검증이 아니며 TRIPOD+AI 또는 PROBAST+AI 준수를 확립하지 않는다.",
                "PROBAST+AI의 참여자·예측변수·결과·분석 영역 편향위험 및 적용성은 자동 판정하지 않으며 평가자 검토 대기 상태다.",
            ]),
            ("limitations", "한계와 보완", [
                f"참여자 {summary['n_participants']}명의 기존 자료에 대한 탐색 비교이며 독립 외부 또는 전향 검증이 아니다.",
                "현재 자료로 모델 선택·반복 비교의 민감도, 결측·제외 영향, 하위집단 성능을 추가할 수 있다. 참여자 군집 신뢰구간은 아직 산출되지 않았다.",
                "모델 개발·선택에 쓰인 자료와 이 비교 자료의 분리 여부가 입증되지 않아 훈련/시험 자료 재사용 가능성을 배제할 수 없다.",
                "자료원·예측변수·결과 정의 메타데이터와 임상 허용오차는 원 연구 책임자의 확인이 필요하다.",
                "허용오차 기준은 탐색 설정이며 임상적으로 검증된 정확도 기준이 아니다. 모델 출력은 취침·기상 시각이며 치료 효과나 수면 개선을 뜻하지 않는다.",
                "독립적 일반화 평가는 연구 이용이 허용된 미노출 외부 자료를 동결 분석계획으로 평가해야 한다. 전향 수행성은 완료된 후향 자료로 대체할 수 없다.",
            ]),
            ("reproducibility", "재현성과 근거", [
                f"원 실행 ID: {selection['run_id']}",
                f"원 결과 SHA-256: {selection['result_hash']}",
                f"비교 스냅샷 SHA-256: {evidence['snapshot_sha256']}",
                "공식 지침 로컬 검색과 고정 서식으로 작성했으며 생성형 LLM·외부 AI 전송은 사용하지 않았다.",
            ]),
            ("references", "검색 근거", ["공식 지침 검색 구절은 출처·페이지/구획·검색 기록에 연결되며 성능 수치를 생성하거나 변경하지 않는다."]),
        ]
    else:
        origin_en = "synthetic" if origin is True else ("declared observed" if origin is False else "origin-undeclared")
        rows = [
            ("purpose", "Purpose", [question or "Compare selected SleepWell timing regressors on identical records and report guideline-relevant limitations."]),
            ("comparison_scope", "Comparison settings and data scope", [
                f"run={selection['run_id']}; models={','.join(selection['model_ids'])}; target={selection['target']}; "
                f"averaging={selection['averaging']}; tolerance=±{selection['tolerance']:g} min.",
                f"The comparison used {summary['n_nights']} common nights from {summary['n_participants']} participants in existing {origin_en} data; "
                f"split={scope.get('split', 'unknown')}. No raw participant rows or identifiers are included.",
                excluded_en,
            ]),
            ("methods", "Metrics and comparison method", [
                "Only valid participant/prediction-time records shared by every selected model were used. MAE was the primary rank metric; RMSE, mean bias, and within-tolerance rate were also calculated.",
                "Participant-equal aggregation computes each participant's metric first and weights people equally; night-equal aggregation weights each common night equally.",
            ]),
            ("results", "Model performance metrics", []),
            ("interpretation", "Conditional comparison interpretation", _winner_paragraphs(evidence, lang) + [
                f"The numeric basis is the selected-model MAE table recomputed from immutable source result {selection['result_hash']} and comparison snapshot {evidence['snapshot_sha256']}. "
                "The reporting and interpretation scope is guided by TRIPOD+AI items 12e, 23a, 25, and 26; retrieved guidance remains linked as separate literature claims.",
            ]),
            ("uncertainty", "Uncertainty", [
                "No participant-cluster interval or prespecified equivalence margin is available, so point-estimate differences do not establish statistical significance or material superiority.",
            ]),
            ("tripod_probast", "TRIPOD+AI and PROBAST+AI scope", [
                "This is a reporting aid for an exploratory comparison of selected existing models on the recorded common evaluation set. It is not external or prospective validation and does not establish TRIPOD+AI or PROBAST+AI compliance.",
                "PROBAST+AI participant, predictor, outcome, analysis, and applicability judgments remain pending human assessor review.",
            ]),
            ("limitations", "Limitations and remediation", [
                f"This is an exploratory comparison in existing data from {summary['n_participants']} participants, not independent external or prospective validation.",
                "The existing data can support sensitivity analyses for model selection, missing/excluded records, and subgroups. Participant-cluster intervals have not been produced.",
                "Separation of model-development/selection data from these comparison data is unproven, so reuse of training/test data cannot be excluded.",
                "Data-source, predictor, outcome-definition metadata and the clinical tolerance require responsible human confirmation.",
                "The tolerance is exploratory rather than a clinically validated accuracy threshold. Timing predictions do not establish treatment effect or sleep improvement.",
                "Generalizability requires research-authorized unseen external data under a frozen plan; completed retrospective data cannot establish prospective feasibility.",
            ]),
            ("reproducibility", "Reproducibility and evidence", [
                f"result={selection['result_hash']}; comparison={evidence['snapshot_sha256']}; deterministic template; llm=false.",
            ]),
            ("references", "Retrieved evidence", ["Official guideline passages retain source/page or section and search provenance; retrieval does not create or alter performance evidence."]),
        ]
    return [{"section_id": key, "title": title, "paragraphs": paragraphs,
             "claim_ids": metric_ids if key == "results" else literature_ids if key == "references" else []}
            for key, title, paragraphs in rows]


def _checklists(evidence: dict[str, Any], claims: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    metric_ids = [claim["claim_id"] for claim in claims if claim["claim_type"] == "comparison_metric_fact"]
    literature_ids = [claim["claim_id"] for claim in claims if claim["claim_type"] == "literature_retrieval"]
    ko = lang == "ko"
    tripod_items = [
        ("tripod_ai_12e", "12e 성능 지표·그래프·다중 모델 비교 근거" if ko else "12e Performance measures, plots, and multi-model rationale", "partially_satisfied", "methods", metric_ids,
         "현재 자료로 자동 생성 가능; 지표 선택 근거는 사람 검토 필요" if ko else "Automatically available from existing data; metric rationale needs human review"),
        ("tripod_ai_20a_21", "20a/21 분석 대상 흐름·수 보고" if ko else "20a/21 Participant flow and numbers analysed", "partially_satisfied", "comparison_scope", metric_ids,
         "공통 밤·참여자·모델별 제외 수만 제공하며 전체 모집 흐름은 메타데이터 필요" if ko else "Common nights, participants, and per-model exclusions are available; full recruitment flow needs metadata"),
        ("tripod_ai_23a", "23a 성능 추정치·불확실성·그래프" if ko else "23a Performance estimates, uncertainty, and plots", "partially_satisfied", "results", metric_ids,
         "점추정치와 그래프를 보고하며 참여자 군집 불확실성 구간은 없음" if ko else "Point estimates and plots are reported; participant-cluster intervals are absent"),
        ("tripod_ai_25_26", "25/26 조건부 해석·한계" if ko else "25/26 Conditional interpretation and limitations", "partially_satisfied", "interpretation", metric_ids + literature_ids,
         "기존 기록의 적절한 하위집단이 있으면 공정성을 탐색할 수 있고, 미기록 집단과 외부 일반화의 실증에는 추가 자료가 필요" if ko else "Fairness can be explored in suitable recorded subgroups; unrecorded groups and empirical external generalization need additional data"),
        ("tripod_ai_other_pending", "그 밖의 개발·검증 보고 항목" if ko else "Other development and validation reporting items", "judgment_pending", "limitations", literature_ids,
         "5–11, 12a/c/d, 16, 20b/c, 22, 27c는 메타데이터와 사람 검토가 필요" if ko else "Items 5–11, 12a/c/d, 16, 20b/c, 22, and 27c need metadata and human review"),
    ]
    probast_items = [
        ("probast_ai_participants", "참여자 영역 편향위험" if ko else "Participants-domain risk of bias", "judgment_pending", "limitations", literature_ids,
         "모집·포함·제외·대표성은 원 코호트 문서와 평가자 검토 필요" if ko else "Recruitment, inclusion, exclusion, and representativeness need source documentation and assessor review"),
        ("probast_ai_predictors", "예측변수 영역 편향위험" if ko else "Predictors-domain risk of bias", "judgment_pending", "limitations", literature_ids,
         "측정 시점·눈가림·가용성을 원 자료 사전과 코드로 검토" if ko else "Review measurement timing, blinding, and availability using source dictionaries and code"),
        ("probast_ai_outcome", "결과 영역 편향위험" if ko else "Outcome-domain risk of bias", "judgment_pending", "limitations", literature_ids,
         "수면 시각 정답 정의·측정·예측변수 독립성을 평가자가 검토" if ko else "An assessor must review timing-outcome definition, measurement, and predictor independence"),
        ("probast_ai_analysis", "분석 영역 편향위험(D4)" if ko else "Analysis-domain risk of bias (D4)", "judgment_pending", "uncertainty", metric_ids + literature_ids,
         "공통 평가 집합과 지표는 확인됨; 4.1–4.7의 표본수·결측·누출·재표집·보정·성능 적절성은 사람 검토 필요" if ko else "Common evaluation set and metrics are evidenced; D4.1–4.7 sample size, missingness, leakage, resampling, correction, and performance appropriateness need review"),
        ("probast_ai_applicability", "적용성" if ko else "Applicability", "judgment_pending", "limitations", literature_ids,
         "목표 집단·사용 환경과 원 코호트/예측변수/결과 문서로 적용성을 판단하며 외부 코호트는 일반화 실증에 필요" if ko else "Applicability needs a defined target setting/population and source cohort/predictor/outcome documentation; an external cohort is needed to test generalization empirically"),
    ]

    def framework(identifier: str, name: str, reference: str, definitions: list[tuple[Any, ...]]):
        remediation_types = {
            "tripod_ai_12e": ["existing_data", "human_review"],
            "tripod_ai_20a_21": ["metadata", "human_review"],
            "tripod_ai_23a": ["existing_data"],
            "tripod_ai_25_26": ["existing_data", "external_data_for_unrecorded_groups_and_generalization"],
            "tripod_ai_other_pending": ["metadata", "human_review"],
            "probast_ai_participants": ["metadata", "human_review"],
            "probast_ai_predictors": ["metadata", "human_review"],
            "probast_ai_outcome": ["metadata", "human_review"],
            "probast_ai_analysis": ["existing_data", "human_review"],
            "probast_ai_applicability": ["metadata", "human_review", "external_data_for_generalization_test"],
        }
        return {"framework_id": identifier, "framework": name, "reference": reference,
                "scope": "comparison_report_subset_not_full_assessment", "items": [
                    {"item_id": item_id, "title": title, "status": status, "report_section": section,
                     "evidence_claim_ids": evidence, "note": note, "assessed_by": "deterministic_scope_check_not_final_assessor"}
                    | {"remediation_type": remediation_types[item_id]}
                    for item_id, title, status, section, evidence, note in definitions]}

    return [
        framework("tripod_ai_comparison_reporting", "TRIPOD+AI comparison reporting subset",
                  "TRIPOD+AI 2024; scoped reporting aid, not full compliance", tripod_items),
        framework("probast_ai_comparison_review", "PROBAST+AI comparison review subset",
                  "PROBAST+AI 2025; final assessor judgments pending", probast_items),
    ]


def _run_ref(service: Any, view: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from .reporting import _safe_context, _selected_scope
    original = verify_run(view)
    if str(view.get("engine")) != "sleepwell":
        raise ValueError("comparison_requires_sleepwell_run")
    canonical = adapt_run(original, "sleepwell", str(view.get("project_path") or ""))
    if canonical["result_hash"] != view.get("result_hash") or canonical["run_id"] != view.get("run_id"):
        raise ValueError("normalized_run_identity_mismatch")
    frozen = service._freeze_run(original, canonical["result_hash"])
    config = canonical.get("config") or {}
    ref = {
        "run_id": canonical["run_id"], "engine": "sleepwell", "name": canonical.get("name") or canonical["run_id"],
        "result_hash": canonical["result_hash"], "data_hash": canonical.get("data_hash") or "",
        "frozen_relative_path": frozen.relative_to(service.project_path).as_posix(),
        "evidence_mode": "project_scoped_frozen_copy",
        "experiment_mode": str(config.get("mode") or "unknown"), "selected_scope": _selected_scope(config),
        "evaluation": _safe_context(canonical.get("evaluation") or {}),
        "provenance": _safe_context(canonical.get("provenance") or {}),
    }
    recorded_at = original.get("created_at", original.get("created"))
    if recorded_at is not None:
        ref["run_created_at"] = _safe_context(recorded_at)
    versions = (canonical.get("provenance") or {}).get("versions")
    ref["model_versions"] = _safe_context(versions) if versions else None
    return original, ref


def build_report(service: Any, view: dict[str, Any], selection: dict[str, Any], *, lang: str,
                 question: str, literature_claims: list[dict[str, Any]]) -> dict[str, Any]:
    if lang not in {"ko", "en"}:
        raise ValueError("unsupported_report_language")
    selected = _validate_selection(selection)
    original, run_ref = _run_ref(service, view)
    if selected["run_id"] != run_ref["run_id"] or selected["result_hash"] != run_ref["result_hash"]:
        raise ValueError("comparison_selection_run_mismatch")
    run_ref["comparison_selection_sha256"] = _sha(selected)
    data = comparison_data(original.get("result") or {}, model_ids=selected["model_ids"],
                           target=selected["target"], averaging=selected["averaging"],
                           tolerance=selected["tolerance"])
    if not data["available"]:
        raise ValueError("comparison_metrics_unavailable:" + ";".join(data.get("warnings") or []))
    if data["model_ids"] != selected["model_ids"]:
        raise ValueError("comparison_model_order_mismatch")
    summary = _aggregate(data, selected)
    metric_claims = _metric_claims(summary, lang)
    claims = metric_claims + copy.deepcopy(literature_claims)
    report = {
        "schema": "researchbench.report.v1", "report_id": service._new_report_id(), "version": 1,
        "parent_report_id": None, "template": TEMPLATE, "language": lang, "question": str(question),
        "context": {"medical_prediction": True, "retrieval": "local_official_guidelines", "llm_used": False},
        "created_at": None, "status": "draft",
        "generation": {"mode": "deterministic_comparison_template", "llm_used": False, "prompt_version": "0"},
        "run_refs": [run_ref], "comparison_evidence": summary, "claims": claims,
        "sections": _sections(summary, run_ref, claims, str(question), lang),
        "checklists": _checklists(summary, claims, lang),
        "automatic_check": {"state": "not_run", "errors": [], "warnings": [], "checked_at": None},
        "human_reviews": [],
    }
    # created_at is supplied by reporting._now without making it a service API.
    from .reporting import _now
    report["created_at"] = _now()
    return report


def comparison_data_for_report(service: Any, report: dict[str, Any]) -> dict[str, Any]:
    evidence = report.get("comparison_evidence") or {}
    selected = _validate_selection(evidence.get("selection"))
    refs = report.get("run_refs") or []
    if len(refs) != 1 or refs[0].get("run_id") != selected["run_id"] or refs[0].get("result_hash") != selected["result_hash"]:
        raise ValueError("comparison_run_reference_mismatch")
    if refs[0].get("comparison_selection_sha256") != _sha(selected):
        raise ValueError("comparison_selection_binding_mismatch")
    allowed_ref_keys = {
        "run_id", "engine", "name", "result_hash", "data_hash",
        "frozen_relative_path", "evidence_mode", "experiment_mode", "selected_scope",
        "evaluation", "provenance", "comparison_selection_sha256", "run_created_at",
        "model_versions",
    }
    if not set(refs[0]).issubset(allowed_ref_keys):
        raise ValueError("comparison_run_reference_schema_invalid")
    frozen = (service.project_path / Path(str(refs[0].get("frozen_relative_path") or ""))).resolve()
    if frozen != (service.run_evidence_root / f"{selected['result_hash']}.json").resolve():
        raise ValueError("comparison_frozen_source_path_mismatch")
    summary = evidence.get("summary")
    if not isinstance(summary, dict) or evidence.get("snapshot_sha256") != _sha({"selection": selected, "summary": summary}):
        raise ValueError("comparison_snapshot_hash_mismatch")
    original = verify_run(service._view_for_ref(refs[0]))
    if digest(original) != selected["result_hash"]:
        raise ValueError("comparison_frozen_run_hash_mismatch")
    canonical = adapt_run(original, "sleepwell", str(service.project_path))
    from .reporting import _safe_context, _selected_scope
    expected_metadata = {
        "engine": "sleepwell", "name": canonical.get("name") or canonical["run_id"],
        "data_hash": canonical.get("data_hash") or "",
        "evidence_mode": "project_scoped_frozen_copy",
        "experiment_mode": str((canonical.get("config") or {}).get("mode") or "unknown"),
        "selected_scope": _selected_scope(canonical.get("config") or {}),
        "evaluation": _safe_context(canonical.get("evaluation") or {}),
        "provenance": _safe_context(canonical.get("provenance") or {}),
        "run_created_at": _safe_context(original.get("created_at", original.get("created"))),
        "model_versions": (
            _safe_context((canonical.get("provenance") or {}).get("versions"))
            if (canonical.get("provenance") or {}).get("versions") else None
        ),
    }
    for key, expected in expected_metadata.items():
        if refs[0].get(key) != expected:
            raise ValueError(f"comparison_run_metadata_mismatch:{key}")
    data = comparison_data(original.get("result") or {}, model_ids=selected["model_ids"], target=selected["target"],
                           averaging=selected["averaging"], tolerance=selected["tolerance"])
    if not data["available"]:
        raise ValueError("comparison_metrics_unavailable")
    return data


def rebuild_content(service: Any, report: dict[str, Any], lang: str) -> dict[str, Any]:
    data = comparison_data_for_report(service, report)
    selected = _validate_selection(report["comparison_evidence"]["selection"])
    summary = _aggregate(data, selected)
    literature = [copy.deepcopy(claim) for claim in report.get("claims", [])
                  if claim.get("evidence", {}).get("kind") == "literature"]
    metric_claims = _metric_claims(summary, lang)
    claims = metric_claims + literature
    return {"comparison_evidence": summary, "claims": claims,
            "sections": _sections(summary, report["run_refs"][0], claims, str(report.get("question") or ""), lang),
            "checklists": _checklists(summary, claims, lang)}


def validate_report(service: Any, report: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if report.get("template") != TEMPLATE:
        return [{"code": "comparison_template_invalid", "claim_id": None}]
    try:
        expected = rebuild_content(service, report, str(report.get("language") or "ko"))
    except Exception as exc:
        return [{"code": "comparison_evidence_invalid", "claim_id": None, "detail": str(exc)}]
    for key, code in (("comparison_evidence", "comparison_snapshot_mismatch"),
                      ("claims", "comparison_claims_mismatch"),
                      ("sections", "comparison_narrative_mismatch"),
                      ("checklists", "comparison_checklists_mismatch")):
        if report.get(key) != expected[key]:
            errors.append({"code": code, "claim_id": None})
    generation = report.get("generation") or {}
    if "ai_analysis" in report:
        # The unchanged numerical report is checked above. ReportService separately
        # checks the bound AI annex, execution record and generation metadata.
        if generation.get("mode") != "llm_rag_agents":
            errors.append({"code": "comparison_generation_metadata_mismatch", "claim_id": None})
    elif generation.get("llm_used") is not False or generation.get("mode") != "deterministic_comparison_template":
        errors.append({"code": "comparison_generation_metadata_mismatch", "claim_id": None})
    return errors


__all__ = ["TEMPLATE", "build_report", "comparison_data_for_report", "rebuild_content", "validate_report"]
