from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guideline_audit import GuidelineAuditService

TEMPLATE = "gradual_guidelines"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def evaluation_path() -> Path:
    return Path(__file__).resolve().parents[1] / "deliverables" / "gradual_v1_20261103" / "evaluation.json"


def load_current_evaluation() -> tuple[dict[str, Any], str, str]:
    raw = evaluation_path().read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if (payload.get("schema") != "sleepwell.gradual_evaluation.v1" or payload.get("policy") != "gradual_v1"
            or (payload.get("status") or payload.get("evaluation_status")) != "retrospective_reused_holdout"):
        raise ValueError("current_gradual_evaluation_invalid")
    return payload, _sha(raw), _sha(_canonical(payload))


def resolve_audit(project_path: Path, audit_id: str | None = None) -> dict[str, Any]:
    service = GuidelineAuditService(project_path)
    _, source_sha, snapshot_sha = load_current_evaluation()
    if audit_id:
        audit = service.get(audit_id)
        attached = audit.get("evaluation") or {}
        if audit.get("template") != "sleepwell_fixed" or audit.get("seeded") is not True:
            raise ValueError("audit_is_not_sleepwell_evidence_seeded")
        if attached.get("source_sha256") != source_sha or attached.get("snapshot_sha256") != snapshot_sha:
            raise ValueError("audit_does_not_match_current_gradual_evaluation")
        return audit
    for row in service.list_audits():
        try:
            candidate = service.get(str(row["audit_id"]))
        except (KeyError, OSError, ValueError):
            continue
        attached = candidate.get("evaluation") or {}
        if (candidate.get("template") == "sleepwell_fixed" and candidate.get("seeded") is True
                and attached.get("source_sha256") == source_sha
                and attached.get("snapshot_sha256") == snapshot_sha):
            return candidate
    return service.create(evaluation_path(), language="ko", template="sleepwell_fixed",
                          title="SleepWell gradual_v1 TRIPOD+AI / PROBAST+AI assessment")


def _tripod_status(value: str) -> str:
    return {"satisfied": "satisfied", "partially_satisfied": "partially_satisfied",
            "not_satisfied": "not_satisfied", "not_reported": "not_satisfied",
            "unknown": "judgment_pending", "pending": "judgment_pending",
            "not_applicable": "not_applicable"}.get(value, "judgment_pending")


def _probast_status(value: str) -> str:
    return {"Y": "satisfied", "PY": "partially_satisfied", "N": "not_satisfied",
            "PN": "not_satisfied", "NI": "judgment_pending", "NA": "not_applicable"}.get(value, "judgment_pending")


_GOVERNANCE_TRIPOD = {"17", "18a", "18b", "18d", "18e", "19"}
_EXTERNAL_TRIPOD = {"3c", "14", "20b", "23b", "27a", "27c"}
_EXTERNAL_DOCUMENT_TRIPOD = {"5a", "5b", "6a", "8a", "8b", "8c", "9c"}
_LOCAL_TRIPOD = {"6b", "10", "16", "20a", "20c", "27b"}
_EXTERNAL_PROBAST = {"1.3", "2.4", "3.1"}
_EXTERNAL_DOCUMENT_PROBAST = {"1.1", "1.2", "2.1", "3.2", "3.3"}
_LOCAL_PROBAST = {"4.2", "4.3", "4.7"}


def _gap_category(text: str, framework: str = "", item_id: str = "") -> str:
    text = text.lower()
    if framework == "TRIPOD+AI" and item_id in _GOVERNANCE_TRIPOD:
        return "governance_human_review"
    if framework == "TRIPOD+AI" and item_id in _EXTERNAL_TRIPOD:
        return "external_existing_data"
    if framework == "TRIPOD+AI" and item_id in _EXTERNAL_DOCUMENT_TRIPOD:
        return "external_source_documentation"
    if framework == "TRIPOD+AI" and item_id in _LOCAL_TRIPOD:
        return "local_documentation_or_code"
    if framework == "PROBAST+AI" and item_id in _EXTERNAL_PROBAST:
        return "external_existing_data"
    if framework == "PROBAST+AI" and item_id in _EXTERNAL_DOCUMENT_PROBAST:
        return "external_source_documentation"
    if framework == "PROBAST+AI" and item_id in _LOCAL_PROBAST:
        return "local_documentation_or_code"
    if any(x in text for x in ("윤리", "동의", "권한", "연구비", "이해관계", "사전등록", "저자", "governance", "ethic")):
        return "governance_human_review"
    if any(x in text for x in ("새 참여", "신규", "외부", "독립", "미노출", "미래", "목표 집단", "대표성",
                               "원 자료", "기기", "센서", "참조검사", "표본", "모집", "인구학", "임상 특성", "실제 자료")):
        return "external_existing_data"
    return "local_documentation_or_code"


def _resolution_route(category: str, action: str, lang: str) -> tuple[str, str]:
    if category == "external_source_documentation":
        if lang == "ko":
            return ("원 코호트의 공식 설명서, 데이터 사전, 판정·주석 절차 및 접근 조건 문서를 확보해 출처 위치와 함께 연결",
                    "문서 보완은 자료 생성·독립 외부 검증·전향 검증을 대신하지 않음")
        return ("Obtain original-cohort documentation, data dictionaries, adjudication/annotation procedures, and access terms with source locators",
                "Source documentation does not create data or establish independent external/prospective validation")
    if category != "external_existing_data":
        return action, "기존 문서·코드 또는 책임 연구자 검토로 보완" if lang == "ko" else "Address through existing documents, code, or accountable human review"
    if lang == "ko":
        route = "외부 기관이 독립적으로 수집을 완료한 미노출 자료, 공개 코호트 또는 데이터 사용 협약 자료를 확보해 동결 분석계획으로 평가"
        limit = "완료된 후향 자료는 독립 외부 검증에는 쓸 수 있지만 전향적 수행 과정, 실시간 자료 가용성 또는 중재 효과의 근거를 대신할 수 없음"
    else:
        route = "Obtain independently collected, unseen completed data from an external institution, public cohort, or data-use agreement and apply the frozen analysis plan"
        limit = "Completed retrospective data may support independent external validation but cannot establish prospective conduct, live availability, or intervention benefit"
    return route, limit


def _metric_claim(index: int, row: dict[str, Any], snapshot_sha: str, lang: str) -> dict[str, Any]:
    cohort, value = str(row["cohort"]).upper(), float(row["value"])
    low, high = float(row["ci95_low"]), float(row["ci95_high"])
    if lang == "ko":
        text = (f"{cohort} 재사용 holdout의 gradual_v1 {row['arm']} arm {row['weighting']} 가중 {row['metric']}는 "
                f"{value:.2f}분 (95% CI {low:.2f}–{high:.2f}; {row['n_participants']}명, {row['n_nights']}밤)이다.")
    else:
        text = (f"In the {cohort} reused holdout, gradual_v1 {row['arm']}-arm {row['weighting']}-weighted {row['metric']} was "
                f"{value:.2f} min (95% CI {low:.2f}–{high:.2f}; {row['n_participants']} participants, {row['n_nights']} nights).")
    return {"claim_id": f"gradual_metric_{index}", "claim_type": "gradual_metric_fact", "text": text,
            "rendered_value": format(value, ".12g"), "review_state": "not_required_for_numeric_identity",
            "evidence": {"kind": "gradual_metric", "evaluation_snapshot_sha256": snapshot_sha,
                         "metric_index": index, "cohort": row["cohort"], "split": row["split"],
                         "evaluation_role": row["evaluation_role"], "policy": row["policy"], "arm": row["arm"],
                         "weighting": row["weighting"], "metric": row["metric"], "value": row["value"],
                         "ci95_low": row["ci95_low"], "ci95_high": row["ci95_high"], "unit": "min",
                         "n_rows": row["n_nights"], "n_participants": row["n_participants"]}}


def _sections(audit: dict[str, Any], lang: str, result_ids: list[str], reference_ids: list[str],
              gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snap, attached = audit["evaluation"]["snapshot"], audit["evaluation"]
    flow = {(r["cohort"], r["split"]): r for r in snap["cohort_flow"]}
    cmi, mesa = flow[("cmi", "test")], flow[("mesa", "test")]
    totals = {cohort: {
        "n_rows": sum(r["n_rows"] for r in snap["cohort_flow"] if r["cohort"] == cohort),
        "n_participants": sum(r["n_participants"] for r in snap["cohort_flow"] if r["cohort"] == cohort),
    } for cohort in ("cmi", "mesa")}
    diagnostic_rows = [r for r in snap.get("diagnostics", []) if r.get("split") == "test"
                       and r.get("policy") == "gradual_v1" and r.get("arm") == "data"
                       and r.get("weighting") == "participant" and r.get("endpoint") in {"onset", "wake"}]
    diagnostic_text = "; ".join(
        f"{r['cohort'].upper()} {r['endpoint']}: bias={r['residual_bias_min']:.2f} min, "
        f"slope={r['calibration_slope']:.3f}, r={r['pearson_correlation']:.3f}" for r in diagnostic_rows)
    strata_text = (f"history={len(snap.get('history_strata', []))}, rarity={len(snap.get('rarity_strata', []))}, "
                   f"demographic={len(snap.get('demographic_strata', []))}; "
                   f"failures={snap.get('failure_summary', {}).get('count', 'unknown')}")
    tc: dict[str, int] = {}
    pc: dict[str, int] = {}
    for row in audit["tripod"]["items"]: tc[row["status"]] = tc.get(row["status"], 0) + 1
    for row in audit["probast"]["questions"]: pc[row["answer"]] = pc.get(row["answer"], 0) + 1
    if lang == "ko":
        status_labels = {"satisfied": "충족", "partially_satisfied": "부분 충족", "unknown": "불명",
                         "not_reported": "미보고", "not_applicable": "해당 없음", "not_satisfied": "미충족"}
        tripod_summary = ", ".join(f"{status_labels.get(k, k)} {v}개" for k, v in sorted(tc.items()))
        probast_summary = ", ".join(f"신호질문 응답 {k} {v}개" for k, v in sorted(pc.items()))
    else:
        tripod_summary = ", ".join(f"{k} {v}" for k, v in sorted(tc.items()))
        probast_summary = ", ".join(f"signaling answer {k} {v}" for k, v in sorted(pc.items()))
    if lang == "ko":
        rows = [
            ("purpose", "목적과 적용 범위", ["SleepWell robust-v2의 gradual_v1 개인화 정책이 다음 수면 시작·기상 시각을 예측하는 연구용 모델임을 보고한다. 임상 의사결정, 치료 효과 또는 수면 개선 주장을 위한 보고서가 아니다."]),
            ("data", "자료", [f"CMI 전체 {totals['cmi']['n_participants']}명·{totals['cmi']['n_rows']:,}밤 중 재사용 test {cmi['n_participants']}명·{cmi['n_rows']}밤, MESA 전체 {totals['mesa']['n_participants']:,}명·{totals['mesa']['n_rows']:,}밤 중 재사용 test {mesa['n_participants']}명·{mesa['n_rows']:,}밤을 평가했다.", "CMI event-derived timing과 MESA ACT-derived weak timing은 PSG 수면 단계가 아니다."]),
            ("methods", "모델과 평가 방법", [str(snap["modeling"]["parameters"]), str(snap["modeling"]["history_rule"]), f"동결 모델을 재적합하거나 선택하지 않고 기존 참여자 분리 split을 재사용했다. 참여자 군집 bootstrap {snap['bootstrap_replicates']:,}회로 95% CI를 계산했다."]),
            ("results", "gradual_v1 성능", []),
            ("diagnostics", "보정·잔차·하위집단·실패 진단", [diagnostic_text, strata_text]),
            ("uncertainty", "불확실성", ["95% CI는 동결 모델 파라미터에 조건부이며 학습·모델 선택 불확실성을 포함하지 않는다. 경험적 예측구간은 재사용 자료 기반으로 개인별 95% 보장을 뜻하지 않는다."]),
            ("guideline_summary", "TRIPOD+AI·PROBAST+AI 점검 요약", [f"TRIPOD+AI 52개 세부항목: {tripod_summary}", f"PROBAST+AI 34개 신호질문: {probast_summary}"] + [f"{r['domain_id']}: {r['judgment']} — {r.get('rationale') or '최종 평가자 판단 필요'}" for r in audit['probast']['overall_judgments']] + [audit["certification_disclaimer"]]),
            ("external_data", "외부 자료가 필요한 보완", ["사용자가 직접 새 참여자나 웨어러블 자료를 수집하지 않는 조건을 반영했다. 독립 검증은 연구 이용이 허용된 기존 외부 코호트, 공개 데이터, 데이터 사용 협약 또는 외부 기관의 미노출 자료를 확보해 동결 분석계획으로 수행해야 한다.", "현재 CMI/MESA test는 개발 과정에서 반복 노출된 재사용 holdout이므로 새 외부 또는 전향 검증으로 표시하지 않는다."]),
            ("rag_scope", "RAG 적용 범위", ["로컬 검색 근거는 공식 지침의 관련 구절을 찾아 출처·페이지와 연결하는 해석 보조에만 사용한다. LLM을 사용하지 않으며 새 성능 근거나 독립 자료를 만들어내지 않는다."]),
            ("limitations", "한계", list(snap["limitations"]) + ["PROBAST+AI 판정은 평가자 보조 초안이며 인간 평가자의 최종 검토가 필요하다."]),
            ("reproducibility", "재현성과 근거", [f"evaluation snapshot={attached['snapshot_sha256']}", f"audit={audit['audit_id']}:{audit['artifact_sha256']}", "결정적 템플릿 생성; LLM 사용 안 함; 원시 참여자 행 미포함."]),
            ("references", "검색 근거", ["연결된 검색 근거는 정확한 문서 제목·페이지/구획·검색 기록과 함께 보존된다."] if reference_ids else ["공식 지침 원문은 지침 라이브러리에서 관리하며, 이 보고서의 판정은 고정된 감사 스냅샷에서 가져왔다."])]
    else:
        rows = [
            ("purpose", "Purpose and scope", ["Research report for SleepWell robust-v2 gradual_v1. It does not support clinical decisions, treatment effects, or sleep-benefit claims."]),
            ("data", "Data", [f"CMI total {totals['cmi']['n_participants']} participants/{totals['cmi']['n_rows']} nights, reused test {cmi['n_participants']}/{cmi['n_rows']}; MESA total {totals['mesa']['n_participants']} participants/{totals['mesa']['n_rows']} nights, reused test {mesa['n_participants']}/{mesa['n_rows']}.", "The timing targets are not PSG sleep stages."]),
            ("methods", "Model and evaluation methods", [str(snap["modeling"]["parameters"]), str(snap["modeling"]["history_rule"]), f"Frozen model; existing participant-disjoint splits; {snap['bootstrap_replicates']} participant-cluster bootstrap replicates."]),
            ("results", "gradual_v1 performance", []),
            ("diagnostics", "Calibration, residual, subgroup and failure diagnostics", [diagnostic_text, strata_text]),
            ("uncertainty", "Uncertainty", ["Intervals are conditional on frozen parameters and exclude training/model-selection uncertainty."]),
            ("guideline_summary", "TRIPOD+AI and PROBAST+AI summary", [f"52 TRIPOD+AI subitems: {tripod_summary}", f"34 PROBAST+AI signaling questions: {probast_summary}"] + [f"{r['domain_id']}: {r['judgment']} — {r.get('rationale') or 'Final assessor judgment required'}" for r in audit['probast']['overall_judgments']] + [audit["certification_disclaimer"]]),
            ("external_data", "Remediation requiring external data", ["Independent validation must use research-authorized existing external cohorts, public datasets, data-use agreements, or unseen partner-held data under a frozen plan.", "Current reused CMI/MESA holdouts are not fresh external or prospective validation."]),
            ("rag_scope", "RAG scope", ["Local retrieval may assist interpretation by locating official guideline passages with exact source/page locators. It uses no LLM and creates neither performance evidence nor independent data."]),
            ("limitations", "Limitations", list(snap["limitations"]) + ["Final PROBAST+AI judgments require human assessor review."]),
            ("reproducibility", "Reproducibility and evidence", [f"evaluation snapshot={attached['snapshot_sha256']}", f"audit={audit['audit_id']}:{audit['artifact_sha256']}", "Deterministic template; no LLM; no raw participant rows."]),
            ("references", "Retrieved evidence", ["Retrieval evidence retains exact document title, page/section locator, and search record."] if reference_ids else ["Judgments come from the frozen audit snapshot."])]
    insertion = len(rows) - 3
    category_titles = ({"external_existing_data": "항목별 외부 기존자료 보완", "external_source_documentation": "항목별 외부 원자료 문서 보완", "local_documentation_or_code": "항목별 문서·코드 보완",
                        "governance_human_review": "항목별 거버넌스·인간 검토"} if lang == "ko" else
                       {"external_existing_data": "Item-level external existing-data remediation", "external_source_documentation": "Item-level external source-document remediation", "local_documentation_or_code": "Item-level document/code remediation",
                        "governance_human_review": "Item-level governance/human review"})
    for category, title in reversed(list(category_titles.items())):
        entries = [f"{g['framework']} {g['item_id']}: {g['source_remaining_action']} → {g['planned_route']}" for g in gaps if g["category"] == category]
        rows.insert(insertion, (f"remediation_{category}", title, entries or ["해당 항목 없음" if lang == "ko" else "No items"]))
    return [{"section_id": sid, "title": title, "paragraphs": paragraphs,
             "claim_ids": result_ids if sid == "results" else reference_ids if sid == "references" else []}
            for sid, title, paragraphs in rows]


def build_content(audit: dict[str, Any], lang: str, literature: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    attached, snap = audit["evaluation"], audit["evaluation"]["snapshot"]
    metrics = [_metric_claim(i, row, attached["snapshot_sha256"], lang)
               for i, row in enumerate(snap["metrics"])
               if row.get("split") == "test" and row.get("evaluation_role") == "reused_holdout"
               and row.get("policy") == "gradual_v1"
               and ((row.get("arm") == "data" and row.get("weighting") in {"night", "participant"}
                     and row.get("metric") in {"onset_mae_min", "wake_mae_min", "joint_mae_min", "interval_mae_min"})
                    or (row.get("weighting") == "participant" and row.get("metric") == "joint_mae_min"))
               and row.get("ci95_low") is not None and row.get("ci95_high") is not None]
    literature = copy.deepcopy(literature or [])
    gaps, tripod, probast = [], [], []
    for row in audit["tripod"]["items"]:
        action = str(row.get("remaining_action") or "")
        category = _gap_category(action + " " + str(row.get("note") or ""), "TRIPOD+AI", row["item_id"])
        title = row.get("label_ko") if lang == "ko" else row.get("label_en")
        note = f"원 판정={row['status']}; 근거={', '.join(row.get('evidence_ids', [])) or '없음'}; {row.get('note', '')}"
        route, evidence_limit = _resolution_route(category, action, lang)
        is_gap = row["status"] not in {"satisfied", "not_applicable"}
        if action and is_gap:
            note += f"; 원 보완사항={action}; 실행 경로={route}; 근거 한계={evidence_limit}; 구분={category}"
            gaps.append({"framework": "TRIPOD+AI", "item_id": row["item_id"], "category": category,
                         "source_remaining_action": action, "planned_route": route, "evidence_limit": evidence_limit,
                         "classification_review_required": True})
        tripod.append({"item_id": f"tripod_{row['item_id']}", "source_item_id": row["item_id"],
                       "title": f"{row['item_id']} {title}", "status": _tripod_status(row["status"]),
                       "source_status": row["status"], "report_section": "guideline_summary",
                       "evidence_claim_ids": [], "source_evidence_ids": list(row.get("evidence_ids", [])),
                       "note": note, "assessed_by": row.get("assessed_by") or "evidence_seed",
                       "source_remaining_action": action, "remaining_action": route if action and is_gap else "",
                       "maintenance_action": action if action and not is_gap else "",
                       "evidence_limit": evidence_limit, "gap_category": category,
                       "classification_review_required": bool(action and is_gap)})
    for row in audit["probast"]["questions"]:
        action = str(row.get("remaining_action") or "")
        category = _gap_category(action + " " + str(row.get("rationale") or ""), "PROBAST+AI", row["item_id"])
        title = row.get("label_ko") if lang == "ko" else row.get("label_en")
        note = f"원 응답={row['answer']} (신호질문 응답이며 준수 인증이 아님); 근거={', '.join(row.get('evidence_ids', [])) or '없음'}; {row.get('rationale', '')}"
        route, evidence_limit = _resolution_route(category, action, lang)
        is_gap = row["answer"] not in {"Y", "NA"}
        if action and is_gap:
            note += f"; 원 보완사항={action}; 실행 경로={route}; 근거 한계={evidence_limit}; 구분={category}"
            gaps.append({"framework": "PROBAST+AI", "item_id": f"{row['phase']}:{row['item_id']}",
                         "category": category, "source_remaining_action": action,
                         "planned_route": route, "evidence_limit": evidence_limit,
                         "classification_review_required": True})
        probast.append({"item_id": f"probast_{row['phase']}_{row['item_id']}", "source_item_id": row["item_id"],
                        "phase": row["phase"], "title": f"{row['phase']} {row['item_id']} {title}",
                        "status": _probast_status(row["answer"]), "source_answer": row["answer"],
                        "report_section": "guideline_summary", "evidence_claim_ids": [],
                        "source_evidence_ids": list(row.get("evidence_ids", [])), "note": note,
                        "assessed_by": row.get("assessed_by") or "evidence_seed",
                        "source_remaining_action": action, "remaining_action": route if action and is_gap else "",
                        "maintenance_action": action if action and not is_gap else "",
                        "evidence_limit": evidence_limit, "gap_category": category,
                        "classification_review_required": bool(action and is_gap)})
    claims = metrics + literature
    return {"claims": claims, "sections": _sections(audit, lang, [r["claim_id"] for r in metrics],
                                                       [r["claim_id"] for r in literature], gaps),
            "checklists": [{"framework_id": "tripod_ai_2024_full", "framework": "TRIPOD+AI 2024 — 52 subitems",
                            "reference": "TRIPOD+AI official checklist", "items": tripod},
                           {"framework_id": "probast_ai_2025_full", "framework": "PROBAST+AI 2025 — 34 signaling questions",
                            "reference": "PROBAST+AI official tool", "items": probast}],
            "remediation_gaps": gaps}


def build_report(service: Any, lang: str, audit_id: str | None, literature: list[dict[str, Any]]) -> dict[str, Any]:
    audit = resolve_audit(service.project_path, audit_id)
    _, source_sha, snapshot_sha = load_current_evaluation()
    attached = audit["evaluation"]
    if attached["source_sha256"] != source_sha or attached["snapshot_sha256"] != snapshot_sha:
        raise ValueError("resolved_audit_evaluation_mismatch")
    return {"schema": "researchbench.report.v1", "report_id": service._new_report_id(), "version": 1,
            "parent_report_id": None, "template": TEMPLATE, "language": lang,
            "question": "gradual_v1 TRIPOD+AI / PROBAST+AI evidence report",
            "context": {"domain": "sleep_prediction", "medical_prediction": True, "research_use_only": True},
            "created_at": datetime.now(timezone.utc).isoformat(), "status": "draft",
            "generation": {"mode": "deterministic_evidence_template", "llm_used": False, "prompt_version": "0"},
            "run_refs": [], **build_content(audit, lang, literature),
            "guideline_evidence": {"audit_id": audit["audit_id"], "audit_artifact_sha256": audit["artifact_sha256"],
                                   "audit_snapshot_sha256": _sha(_canonical(audit)), "audit_snapshot": audit,
                                   "evaluation_source_path": str(evaluation_path()),
                                   "evaluation_source_sha256": source_sha, "evaluation_snapshot_sha256": snapshot_sha},
            "automatic_check": {"state": "not_run", "errors": [], "warnings": [], "checked_at": None},
            "human_reviews": []}


def validate_report(report: dict[str, Any], project_path: Path) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    evidence, audit = report.get("guideline_evidence") or {}, (report.get("guideline_evidence") or {}).get("audit_snapshot")
    if not isinstance(audit, dict):
        return [{"code": "gradual_audit_snapshot_missing", "claim_id": None}]
    if _sha(_canonical(audit)) != evidence.get("audit_snapshot_sha256"):
        errors.append({"code": "gradual_audit_snapshot_hash_mismatch", "claim_id": None})
    if audit.get("artifact_sha256") != evidence.get("audit_artifact_sha256"):
        errors.append({"code": "gradual_audit_artifact_mismatch", "claim_id": None})
    attached = audit.get("evaluation") or {}
    if attached.get("source_sha256") != evidence.get("evaluation_source_sha256"):
        errors.append({"code": "gradual_embedded_evaluation_source_mismatch", "claim_id": None})
    if attached.get("snapshot_sha256") != evidence.get("evaluation_snapshot_sha256"):
        errors.append({"code": "gradual_embedded_evaluation_snapshot_mismatch", "claim_id": None})
    try:
        audit_check = GuidelineAuditService(project_path).check(audit)
        if audit_check.get("errors"):
            errors.append({"code": "gradual_audit_invalid", "claim_id": None, "detail": audit_check["errors"]})
    except Exception as exc:
        errors.append({"code": "gradual_audit_invalid", "claim_id": None, "detail": str(exc)})
    try:
        _, source_sha, snapshot_sha = load_current_evaluation()
        if source_sha != evidence.get("evaluation_source_sha256") or snapshot_sha != evidence.get("evaluation_snapshot_sha256"):
            errors.append({"code": "current_gradual_evaluation_changed", "claim_id": None})
    except Exception as exc:
        errors.append({"code": "current_gradual_evaluation_unavailable", "claim_id": None, "detail": str(exc)})
    literature = [r for r in report.get("claims", []) if r.get("evidence", {}).get("kind") == "literature"]
    try:
        expected = build_content(audit, str(report.get("language")), literature)
        for key in ("claims", "sections", "checklists", "remediation_gaps"):
            if report.get(key) != expected[key]:
                errors.append({"code": f"gradual_{key}_mismatch", "claim_id": None})
    except Exception as exc:
        errors.append({"code": "gradual_content_rebuild_failed", "claim_id": None, "detail": str(exc)})
    return errors
