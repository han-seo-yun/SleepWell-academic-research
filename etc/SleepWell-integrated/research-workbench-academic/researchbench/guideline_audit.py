"""Immutable TRIPOD+AI reporting and PROBAST+AI risk-of-bias audits.

The two frameworks deliberately remain separate: TRIPOD+AI records reporting
completeness, while PROBAST+AI records signalling answers and human domain
judgments.  Nothing in this module produces a compliance score or certificate.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIT_SCHEMA = "researchbench.guideline_audit.v1"
EVALUATION_SCHEMA = "sleepwell.gradual_evaluation.v1"
TRIPOD_STATUSES = {
    "satisfied", "partially_satisfied", "not_reported", "unknown",
    "not_applicable", "pending",
}
PROBAST_ANSWERS = {"Y", "PY", "PN", "N", "NI", "NA"}
DOMAIN_JUDGMENTS = {"Low", "High", "Unclear"}
PAIRED_REDUCTION_HEADING = "Paired MAE reduction (legacy14 - gradual_v1; positive favors gradual_v1)"
NA_ALLOWED = {"development:4.4", "evaluation:4.4", "evaluation:4.5", "evaluation:4.6"}
_AUDIT_ID = re.compile(r"^audit_[0-9a-f]{32}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:participant_ids?|patient_ids?|subject_ids?|raw_records?|records|rows)(?:$|_)",
    re.IGNORECASE,
)


# Short paraphrases support navigation. They are not a substitute for the official tools.
TRIPOD_ITEMS = (
    ("1", "D/E", "제목"), ("2", "D/E", "초록"),
    ("3a", "D/E", "배경"), ("3b", "D/E", "사용 목적"),
    ("3c", "D/E", "건강 불평등"), ("4", "D/E", "연구 목적"),
    ("5a", "D/E", "자료 출처"), ("5b", "D/E", "자료 수집 시기"),
    ("6a", "D/E", "모집 환경"), ("6b", "D/E", "선정 기준"),
    ("6c", "D/E", "처치 정보"), ("7", "D/E", "전처리·품질"),
    ("8a", "D/E", "결과 정의"), ("8b", "D/E", "결과 판정자"),
    ("8c", "D/E", "결과 판정 눈가림"), ("9a", "D", "후보 변수"),
    ("9b", "D/E", "입력 정의"), ("9c", "D/E", "입력 판정자"),
    ("10", "D/E", "표본수 근거"), ("11", "D/E", "결측 처리"),
    ("12a", "D", "자료 배정"), ("12b", "D", "변수 변환"),
    ("12c", "D", "구축·튜닝"), ("12d", "D/E", "군집 이질성"),
    ("12e", "D/E", "평가 방법"), ("12f", "E", "모델 업데이트"),
    ("12g", "E", "예측 계산"), ("13", "D/E", "클래스 불균형"),
    ("14", "D/E", "공정성 방법"), ("15", "D", "출력 정의"),
    ("16", "D/E", "자료 차이"), ("17", "D/E", "윤리·동의"),
    ("18a", "D/E", "연구비"), ("18b", "D/E", "이해관계"),
    ("18c", "D/E", "프로토콜 접근"), ("18d", "D/E", "사전등록"),
    ("18e", "D/E", "데이터 접근"), ("18f", "D/E", "코드 접근"),
    ("19", "D/E", "이용자 참여"), ("20a", "D/E", "대상자 흐름"),
    ("20b", "D/E", "기초 특성"), ("20c", "E", "개발·평가 비교"),
    ("21", "D/E", "분석별 분모"), ("22", "D", "완전한 모델"),
    ("23a", "D/E", "성능·불확실성"), ("23b", "D/E", "성능 이질성"),
    ("24", "E", "업데이트 결과"), ("25", "D/E", "결과 해석"),
    ("26", "D/E", "한계"), ("27a", "D", "입력 품질 감시"),
    ("27b", "D", "사용자 개입"), ("27c", "D/E", "후속 검증"),
)

_TRIPOD_LABELS_EN = (
    "Title", "Abstract", "Background", "Intended use", "Health inequalities",
    "Objectives", "Data sources", "Data collection dates", "Recruitment setting",
    "Eligibility criteria", "Treatments", "Preprocessing and quality", "Outcome definition",
    "Outcome assessors", "Outcome blinding", "Candidate predictors", "Predictor definition",
    "Predictor assessors", "Sample-size rationale", "Missing data", "Data partitioning",
    "Predictor transformations", "Model building and tuning", "Cluster heterogeneity",
    "Performance methods", "Model updating", "Prediction calculation", "Class imbalance",
    "Fairness methods", "Output definition", "Dataset differences", "Ethics and consent",
    "Funding", "Conflicts of interest", "Protocol access", "Registration", "Data access",
    "Code access", "Public involvement", "Participant flow", "Baseline characteristics",
    "Development-evaluation comparison", "Analysis denominators", "Full model",
    "Performance and uncertainty", "Performance heterogeneity", "Updated-model results",
    "Interpretation", "Limitations", "Input-quality monitoring", "User intervention",
    "Further validation",
)
TRIPOD_LABEL_EN = {item[0]: label for item, label in zip(TRIPOD_ITEMS, _TRIPOD_LABELS_EN)}

_PROBAST_COMMON = (
    ("1.1", "자료원"), ("1.2", "연구설계"), ("1.3", "선정·대표성"),
    ("2.1", "입력 측정"), ("2.2", "입력 전처리"),
    ("2.3", "입력 눈가림"), ("2.4", "실사용 가용성"),
    ("3.1", "결과 적절성"), ("3.2", "결과 일관성"),
    ("3.3", "결과 눈가림"), ("3.4", "예측 시간 간격"),
)
PROBAST_ITEMS = (
    tuple(("development", item, label) for item, label in _PROBAST_COMMON)
    + (("development", "4.1", "표본 충분성"),
       ("development", "4.2", "변수 처리"),
       ("development", "4.3", "결측 처리"),
       ("development", "4.4", "불균형 재보정"),
       ("development", "4.5", "과적합 통제"))
    + tuple(("evaluation", item, label) for item, label in _PROBAST_COMMON)
    + (("evaluation", "4.1", "겉보기 성능 회피"),
       ("evaluation", "4.2", "평가 표본 충분성"),
       ("evaluation", "4.3", "평가 결측 처리"),
       ("evaluation", "4.4", "불균형 없는 평가"),
       ("evaluation", "4.5", "분할 정보 누출"),
       ("evaluation", "4.6", "재표본화 전 과정"),
       ("evaluation", "4.7", "성능 평가 적절성"))
)

_PROBAST_LABEL_EN = {
    "자료원":"Data source", "연구설계":"Study design", "선정·대표성":"Selection and representativeness",
    "입력 측정":"Predictor measurement", "입력 전처리":"Predictor preprocessing",
    "입력 눈가림":"Predictor blinding", "실사용 가용성":"Availability at intended use",
    "결과 적절성":"Outcome appropriateness", "결과 일관성":"Outcome consistency",
    "결과 눈가림":"Outcome blinding", "예측 시간 간격":"Prediction time interval",
    "표본 충분성":"Sample-size adequacy", "변수 처리":"Predictor handling",
    "결측 처리":"Missing-data handling", "불균형 재보정":"Imbalance adjustment",
    "과적합 통제":"Overfitting control", "겉보기 성능 회피":"Avoidance of apparent performance",
    "평가 표본 충분성":"Evaluation sample-size adequacy", "평가 결측 처리":"Evaluation missing data",
    "불균형 없는 평가":"Evaluation without imbalance distortion", "분할 정보 누출":"Partition information leakage",
    "재표본화 전 과정":"All resampling steps", "성능 평가 적절성":"Performance-evaluation appropriateness",
}


OFFICIAL_SOURCES = [
    {"id":"TRIPOD_AI", "title":"TRIPOD+AI statement", "url":"https://www.bmj.com/content/385/bmj-2023-078378"},
    {"id":"TRIPOD_AI_EXPANDED", "title":"TRIPOD+AI expanded checklist", "url":"https://www.tripod-statement.org/wp-content/uploads/2024/04/TRIPODAI-Supplement.pdf"},
    {"id":"PROBAST_AI", "title":"PROBAST+AI statement", "url":"https://www.bmj.com/content/388/bmj-2024-082505"},
    {"id":"PROBAST_AI_TOOL", "title":"PROBAST+AI assessment tool", "url":"https://www.probast.org/probast_ai/downloads/"},
    {"id":"SCIENTIFIC_AGENT_SKILLS", "title":"Kassis et al. Scientific Agent Skills (2026, v2)", "url":"https://doi.org/10.48550/arXiv.2609.00065"},
]

REMAINING_TASKS = {
    "existing_data_and_code": [
        "Report gradual_v1 performance, denominators, cluster uncertainty, residual diagnostics and failure counts.",
        "Report split-level flow, available missingness, model/code/data hashes and the reused-holdout limitation.",
        "Consolidate model purpose, target definition, preprocessing, executable calculation and limitations.",
    ],
    "new_participants_or_data": [
        "Evaluate a frozen model and analysis plan in new participants or a previously unseen future period.",
        "Capture actual device/diary availability timestamps, prediction failures and missing outcomes prospectively.",
        "Collect representative demographic and clinical attributes for subgroup performance and fairness analyses.",
        "Validate ACT/event and diary targets against an appropriate sleep reference when making physiologic claims.",
        "Use independent data to calibrate and assess any prediction intervals before presenting individual coverage.",
        "Run a separate controlled study before claiming that recommendations improve sleep or health outcomes.",
    ],
    "author_or_governance_information": [
        "Record ethics approval or exemption, consent and secondary-data access authority.",
        "Declare funding, sponsor roles, conflicts of interest, public involvement and registration status.",
        "Document original recruitment periods, settings, adjudication/blinding and participant flow from source records.",
    ],
}
REMAINING_TASKS_KO = {
    "existing_data_and_code": [
        "gradual_v1 성능, 분모, 참여자 군집 불확실성, 잔차 진단과 실패 수를 보고합니다.",
        "분할별 흐름, 확인 가능한 결측, 모델·코드·자료 해시와 재사용 holdout 한계를 보고합니다.",
        "모델 목적, 결과 정의, 전처리, 실행 가능한 계산 방법과 한계를 한 보고서로 통합합니다.",
    ],
    "new_participants_or_data": [
        "동결된 모델과 분석계획을 새 참여자 또는 이전에 보지 않은 미래 기간에서 평가합니다.",
        "실제 기기·일지 이용 가능 시각, 예측 실패와 결과 누락을 전향적으로 기록합니다.",
        "하위집단 성능과 공정성 분석에 필요한 대표성 있는 인구학·임상 특성을 수집합니다.",
        "생리학적 주장을 하려면 ACT/event 및 일지 결과를 적절한 수면 참조검사와 비교합니다.",
        "개인 예측구간을 제시하기 전 독립 자료로 구간을 보정하고 포함률을 평가합니다.",
        "추천이 수면이나 건강 결과를 개선한다고 주장하기 전 별도의 대조 연구를 수행합니다.",
    ],
    "author_or_governance_information": [
        "윤리 승인 또는 면제, 동의와 2차 자료 접근 권한을 기록합니다.",
        "연구비, 지원기관 역할, 이해관계, 이용자 참여와 사전등록 상태를 선언합니다.",
        "원 자료 기록에서 모집 기간·환경, 판정·눈가림과 대상자 흐름을 확인합니다.",
    ],
}

_SEED_PATH = Path(__file__).with_name("resources") / "sleepwell_guideline_seed.json"
_EVIDENCE_MANIFEST_PATH = Path(__file__).with_name("resources") / "sleepwell_evidence_digest_manifest.json"
_OFFICIAL_MANIFEST_PATH = Path(__file__).with_name("resources") / "guideline_source_manifest.json"
_STATUS_KO = {
    "충족":"satisfied", "부분 충족":"partially_satisfied", "미보고":"not_reported",
    "확인 불가":"unknown", "해당 없음":"not_applicable",
}


def _sleepwell_seed() -> tuple[dict[str, Any], str, str]:
    """Load the reviewed bundled snapshot or fail closed before seeding judgments."""
    raw = _SEED_PATH.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload.get("schema") != "sleepwell.guideline_seed.v1":
        raise ValueError("invalid_sleepwell_guideline_seed")
    if len(payload.get("tripod", [])) != 52 or len(payload.get("probast", [])) != 34:
        raise ValueError("incomplete_sleepwell_guideline_seed")
    return payload, _sha(raw), _sha(_canonical(payload))


def _bundled_json(path: Path, expected_schema: str | None = None) -> tuple[dict[str, Any], str, str]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if expected_schema and payload.get("schema") != expected_schema:
        raise ValueError(f"invalid_bundled_resource:{path.name}")
    return payload, _sha(raw), _sha(_canonical(payload))


def _official_sources() -> list[dict[str, Any]]:
    manifest, resource_sha, snapshot_sha = _bundled_json(_OFFICIAL_MANIFEST_PATH)
    by_fragment = {
        "TRIPOD_AI":"TRIPOD-AI-BMJ-2024.pdf",
        "TRIPOD_AI_EXPANDED":"TRIPODAI-Supplement-2024.pdf",
        "PROBAST_AI":"PROBAST-AI-BMJ-2025.pdf",
        "PROBAST_AI_TOOL":"mook082505.ww2.pdf",
    }
    result = copy.deepcopy(OFFICIAL_SOURCES)
    files = {row["file"]:row for row in manifest.get("files", [])}
    for source in result:
        file_name = by_fragment.get(source["id"])
        if file_name and file_name in files:
            source["source_file"] = file_name
            source["sha256"] = str(files[file_name]["sha256"]).lower()
    result.append({"id":"GUIDELINE_SOURCE_MANIFEST", "title":"Bundled official-source digest manifest",
                   "resource_sha256":resource_sha, "snapshot_sha256":snapshot_sha, "snapshot":manifest})
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _artifact_hash(audit: dict[str, Any]) -> str:
    value = copy.deepcopy(audit)
    value.pop("artifact_sha256", None)
    return _sha(_canonical(value))


def _aggregate_count_shape(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    return isinstance(value, dict) and bool(value) and all(_aggregate_count_shape(item) for item in value.values())


def _contains_sensitive_keys(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            aggregate_count = (bool(re.search(r"(?:^(?:rows?|records?)(?:_|$)|(?:^|_)(?:rows?|records?)$)", str(key).lower()))
                               and _aggregate_count_shape(item))
            if _SENSITIVE_KEY.search(str(key)) and not aggregate_count:
                found.append(child)
            found.extend(_contains_sensitive_keys(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value[:10000]):
            found.extend(_contains_sensitive_keys(item, f"{path}[{index}]"))
    return found


def _atomic_new_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("immutable_audit_exists")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GuidelineAuditService:
    """Create, revise, validate and export immutable guideline assessments."""

    def __init__(self, project_path: str | Path) -> None:
        self.project_path = Path(project_path).expanduser().resolve()
        if not self.project_path.is_dir():
            raise FileNotFoundError(self.project_path)
        self.root = self.project_path / "research" / "guideline_audits"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, audit_id: str) -> Path:
        if not _AUDIT_ID.fullmatch(str(audit_id)):
            raise ValueError("invalid_audit_id")
        return self.root / f"{audit_id}.json"

    def _load_evaluation(self, evaluation_path: str | Path | None) -> dict[str, Any] | None:
        if evaluation_path is None:
            return None
        source = Path(evaluation_path).expanduser().resolve()
        raw = source.read_bytes()
        if len(raw) > 20_000_000:
            raise ValueError("evaluation_too_large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("evaluation_must_be_object")
        if payload.get("schema") != EVALUATION_SCHEMA:
            raise ValueError("unsupported_evaluation_schema")
        if payload.get("policy") != "gradual_v1":
            raise ValueError("evaluation_policy_must_be_gradual_v1")
        if (payload.get("status") or payload.get("evaluation_status")) != "retrospective_reused_holdout":
            raise ValueError("evaluation_status_must_disclose_reused_holdout")
        sensitive = _contains_sensitive_keys(payload)
        if sensitive:
            raise ValueError("aggregate_evaluation_contains_row_identifiers:" + sensitive[0])
        return {
            "source_path": str(source),
            "source_sha256": _sha(raw),
            "snapshot_sha256": _sha(_canonical(payload)),
            "snapshot": payload,
        }

    def create(
        self,
        evaluation_path: str | Path | None = None,
        language: str = "ko",
        title: str = "TRIPOD+AI / PROBAST+AI assessment",
        *,
        template: str = "blank",
    ) -> dict[str, Any]:
        if language not in {"ko", "en"}:
            raise ValueError("unsupported_language")
        if template not in {"blank", "sleepwell_fixed"}:
            raise ValueError("unsupported_template")
        audit_id = f"audit_{uuid.uuid4().hex}"
        seeded = template == "sleepwell_fixed"
        seed, seed_resource_sha, seed_snapshot_sha = _sleepwell_seed() if seeded else (None, None, None)
        seed_tripod = {row["item_id"]: row for row in seed["tripod"]} if seed else {}
        seed_probast = {
            f"{'development' if row['phase_ko'] == '개발' else 'evaluation'}:{row['item_id']}": row
            for row in seed["probast"]
        } if seed else {}
        evaluation = self._load_evaluation(evaluation_path)
        tripod = []
        for item, scope, label in TRIPOD_ITEMS:
            seed_row = seed_tripod.get(item, {})
            evidence_ids = list(seed_row.get("evidence_ids", []))
            if evaluation and item in {"23a", "24"}:
                evidence_ids.append("GRADUAL_V1_EVALUATION")
            note = str(seed_row.get("note", ""))
            action = str(seed_row.get("remaining_action", ""))
            if seeded and item == "12f":
                if language == "ko":
                    note = ("평가한 FusionForecasts/PortableCohortModel 경로에서 gradual_v1은 개인화 특징의 "
                            "활성 조건과 가중치만 바꾼다. 잔차 보정 sidecar는 별도 경로이며 이번 gradual_v1 "
                            "성능평가에 포함되지 않는다.")
                    action = "정책과 실제 실행 추론 경로별로 성능 근거를 분리해 유지합니다."
                else:
                    note = ("In the evaluated FusionForecasts/PortableCohortModel route, gradual_v1 changes only "
                            "the personalization feature gate and weight. The residual-correction sidecar is a "
                            "separate route and is not part of this gradual_v1 performance evaluation.")
                    action = "Keep performance evidence separate for each policy and executable inference route."
            if evaluation and item in {"23a", "24"}:
                suffix = (" gradual_v1 집계 재평가와 불변 해시를 연결했다." if language == "ko" else
                          " gradual_v1 aggregate reassessment is attached with an immutable hash.")
                note = (note + suffix).strip()
                action = ("재사용 holdout 표시를 유지하고 새 참여자 또는 미노출 미래 자료에서 동결 분석을 확인합니다."
                          if language == "ko" else
                          "Retain reused-holdout labeling and confirm the frozen analysis in new participants or unseen future data.")
            tripod.append({
                "item_id": item,
                "scope": scope,
                "label": label if language == "ko" else TRIPOD_LABEL_EN[item],
                "label_ko": label,
                "label_en": TRIPOD_LABEL_EN[item],
                "status": _STATUS_KO[seed_row["status_ko"]] if seeded else "pending",
                "evidence_ids": evidence_ids,
                "note": note,
                "remaining_action": action,
                "assessed_by": "evidence_seed" if seeded else "",
                "assessed_at": _now() if seeded else None,
            })
        probast = []
        for phase, item, label in PROBAST_ITEMS:
            key = f"{phase}:{item}"
            seed_row = seed_probast.get(key, {})
            evidence_ids = list(seed_row.get("evidence_ids", []))
            if evaluation and key == "evaluation:4.7":
                evidence_ids.append("GRADUAL_V1_EVALUATION")
            rationale = str(seed_row.get("rationale", ""))
            if seeded and item == "2.3":
                rationale = (("평가 경로는 cutoff 이전 관측만 이력 특징에 사용한다. 잔차 sidecar 경계 검사는 "
                              "별도 경로에 해당하며 이번 gradual_v1 평가에는 포함되지 않는다.") if language == "ko" else
                             ("The evaluated route limits history-derived features to observations before the cutoff. "
                              "Residual-sidecar boundary tests concern a separate route that is not included in this "
                              "gradual_v1 evaluation."))
            probast.append({
                "phase": phase,
                "item_id": item,
                "domain": item.split(".", 1)[0],
                "label": label if language == "ko" else _PROBAST_LABEL_EN[label],
                "label_ko": label,
                "label_en": _PROBAST_LABEL_EN[label],
                "answer": seed_row.get("answer", "NI"),
                "evidence_ids": evidence_ids,
                "rationale": rationale,
                "remaining_action": str(seed_row.get("remaining_action", "")),
                "assessed_by": "evidence_seed" if seeded else "",
                "assessed_at": _now() if seeded else None,
            })
        domains = []
        for phase in ("development", "evaluation"):
            for domain in ("1", "2", "3", "4"):
                high = seeded and phase == "evaluation" and domain == "4"
                domains.append({"domain_id":f"{phase}:{domain}", "phase":phase, "domain":domain,
                                "judgment":"High" if high else "Unclear",
                                "rationale":"Adaptive reuse of previously exposed validation/test evidence limits evaluation independence." if high else "",
                                "assessed_by":"evidence_seed" if high else "", "assessed_at":_now() if high else None})
        domains.extend([
            {"domain_id":"applicability:participants", "phase":"applicability", "domain":"participants",
             "judgment":"High" if seeded else "Unclear",
             "rationale":"New Korean app users are not represented by the existing reused cohorts." if seeded else "",
             "assessed_by":"evidence_seed" if seeded else "", "assessed_at":_now() if seeded else None},
            {"domain_id":"applicability:predictors", "phase":"applicability", "domain":"predictors",
             "judgment":"High" if seeded else "Unclear",
             "rationale":"Live device and diary availability differs from offline replay assumptions." if seeded else "",
             "assessed_by":"evidence_seed" if seeded else "", "assessed_at":_now() if seeded else None},
            {"domain_id":"applicability:outcome", "phase":"applicability", "domain":"outcome",
             "judgment":"High" if seeded else "Unclear",
             "rationale":"Diary outcomes and physiologic sleep claims require separate measurement validation." if seeded else "",
             "assessed_by":"evidence_seed" if seeded else "", "assessed_at":_now() if seeded else None},
        ])
        overall = [
            {"domain_id":"overall:development_quality", "judgment":"Unclear", "rationale":"Development quality requires human domain judgments."},
            {"domain_id":"overall:evaluation_risk", "judgment":"High" if seeded else "Unclear",
             "rationale":"Provisional: repeated adaptive use of previously exposed test data." if seeded else ""},
            {"domain_id":"overall:applicability_same_endpoint", "judgment":"Unclear", "rationale":""},
            {"domain_id":"overall:applicability_new_korean_diary", "judgment":"High" if seeded else "Unclear",
             "rationale":"Target population, live availability and diary outcome require new evidence." if seeded else ""},
        ]
        sources = _official_sources()
        if seeded:
            evidence_manifest, evidence_resource_sha, evidence_snapshot_sha = _bundled_json(
                _EVIDENCE_MANIFEST_PATH, "sleepwell.evidence_digest_manifest.v1"
            )
            sources.append({
                "id":"SLEEPWELL_AUDIT_20261103",
                "title":"SleepWell evidence audit dated 2026-11-03",
                "sha256":seed["source_report_sha256"],
                "resource_sha256":seed_resource_sha,
                "snapshot_sha256":seed_snapshot_sha,
                "snapshot":seed,
                "note":"Seed provenance only; each judgment remains reviewable.",
            })
            sources.append({"id":"SLEEPWELL_EVIDENCE_MANIFEST", "title":"Bundled SleepWell evidence digest manifest",
                            "resource_sha256":evidence_resource_sha, "snapshot_sha256":evidence_snapshot_sha,
                            "snapshot":evidence_manifest})
            for row in evidence_manifest["evidence"]:
                sources.append({"id":row["id"], "title":row["description"],
                                "source_path":row["source_path"], "source_line":row["line"],
                                "sha256":row["sha256"], "contained_in":"SLEEPWELL_EVIDENCE_MANIFEST"})
        if evaluation:
            sources.append({
                "id":"GRADUAL_V1_EVALUATION",
                "title":"gradual_v1 aggregate retrospective reassessment",
                "sha256":evaluation["source_sha256"],
                "snapshot_sha256":evaluation["snapshot_sha256"],
                "note":"Reused holdout evidence; not external or prospective validation.",
            })
        audit: dict[str, Any] = {
            "schema": AUDIT_SCHEMA,
            "audit_id": audit_id,
            "version": 1,
            "parent_audit_id": None,
            "created_at": _now(),
            "language": language,
            "title": str(title).strip() or "TRIPOD+AI / PROBAST+AI assessment",
            "template": template,
            "assessment_state": "assessor_assisted_draft",
            "assessment_state_label": "평가자 검토가 필요한 근거 기반 초안" if language == "ko" else "Evidence-seeded draft requiring assessor review",
            "seeded": seeded,
            "provisional": True,
            "human_review_required": True,
            "certification_disclaimer": ("이 결과는 준수 점수나 인증이 아니며, 미충족·불명·미보고 항목을 포함한 평가자 보조 초안입니다."
                                         if language == "ko" else
                                         "This is an assessor-assisted draft with incomplete, unknown and unreported items; it is not a compliance score or certification."),
            "not_a_score_or_certification": True,
            "evaluation": evaluation,
            "sources": sources,
            "tripod": {"purpose":"reporting_completeness", "items":tripod},
            "probast": {"purpose":"quality_risk_and_applicability",
                        "evaluation_design":"reused_heldout" if (seeded or evaluation) else "unspecified",
                        "questions":probast,
                        "domains":domains, "overall_judgments":overall},
            "remaining_tasks": copy.deepcopy(REMAINING_TASKS_KO if language == "ko" else REMAINING_TASKS),
            "review_history": [],
        }
        self._derive_overall(audit)
        audit["artifact_sha256"] = _artifact_hash(audit)
        _atomic_new_json(self._path(audit_id), audit)
        return audit

    def get(self, audit_id: str, *, verify: bool = True) -> dict[str, Any]:
        audit = json.loads(self._path(audit_id).read_text(encoding="utf-8"))
        if verify:
            result = self.check(audit)
            if result["errors"]:
                raise ValueError("invalid_audit:" + ";".join(result["errors"]))
        return audit

    def list_audits(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.root.glob("audit_*.json")):
            try:
                audit = json.loads(path.read_text(encoding="utf-8"))
                result.append({key:audit.get(key) for key in
                               ("audit_id", "version", "parent_audit_id", "created_at", "title", "template")})
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(result, key=lambda row: str(row.get("created_at", "")), reverse=True)

    def list(self) -> list[dict[str, Any]]:
        return self.list_audits()

    @staticmethod
    def _derive_overall(audit: dict[str, Any], assessor: str = "derived_rule") -> None:
        domains = audit["probast"]["domains"]
        overall = {row["domain_id"]:row for row in audit["probast"]["overall_judgments"]}
        groups = {
            "overall:development_quality":[row for row in domains if row["phase"] == "development"],
            "overall:evaluation_risk":[row for row in domains if row["phase"] == "evaluation"],
            "overall:applicability_new_korean_diary":[row for row in domains if row["phase"] == "applicability"],
        }
        for overall_id, rows in groups.items():
            judgments = {row["judgment"] for row in rows}
            derived = "High" if "High" in judgments else ("Unclear" if "Unclear" in judgments else "Low")
            target = overall[overall_id]
            if target["judgment"] != derived:
                target["judgment"] = derived
                target["rationale"] = "Derived from domain judgments: any High → High; otherwise any Unclear → Unclear; all Low → Low."
                target["assessed_by"] = assessor
                target["assessed_at"] = _now()

    def revise(self, audit_id: str, edits: dict[str, Any], *, assessed_by: str) -> dict[str, Any]:
        assessor = str(assessed_by).strip()
        if not assessor:
            raise ValueError("assessed_by_required")
        source = self.get(audit_id)
        audit = copy.deepcopy(source)
        audit["audit_id"] = f"audit_{uuid.uuid4().hex}"
        audit["parent_audit_id"] = source["audit_id"]
        audit["version"] = int(source["version"]) + 1
        audit["created_at"] = _now()
        audit.pop("artifact_sha256", None)
        changed: list[str] = []
        for attachment in edits.get("add_sources") or []:
            source_row = self._snapshot_document(
                attachment["path"], attachment.get("title") or Path(attachment["path"]).name
            )
            if any(row.get("id") == source_row["id"] for row in audit["sources"]):
                source_row = next(row for row in audit["sources"] if row.get("id") == source_row["id"])
            else:
                audit["sources"].append(source_row)
            links = attachment.get("link_to") or {}
            for item_id in links.get("tripod", []):
                row = next((x for x in audit["tripod"]["items"] if x["item_id"] == item_id), None)
                if row is None: raise ValueError(f"unknown_tripod_item:{item_id}")
                if source_row["id"] not in row["evidence_ids"]: row["evidence_ids"].append(source_row["id"])
                row.update(assessed_by=assessor, assessed_at=_now())
            for key in links.get("probast", []):
                phase, _, item_id = key.partition(":")
                row = next((x for x in audit["probast"]["questions"]
                            if x["phase"] == phase and x["item_id"] == item_id), None)
                if row is None: raise ValueError(f"unknown_probast_item:{key}")
                if source_row["id"] not in row["evidence_ids"]: row["evidence_ids"].append(source_row["id"])
                row.update(assessed_by=assessor, assessed_at=_now())
            changed.append(f"source:{source_row['id']}")
        for item_id, update in (edits.get("tripod") or {}).items():
            row = next((x for x in audit["tripod"]["items"] if x["item_id"] == item_id), None)
            if row is None:
                raise ValueError(f"unknown_tripod_item:{item_id}")
            allowed = {"status", "note", "remaining_action", "evidence_ids"}
            if set(update) - allowed:
                raise ValueError(f"unsupported_tripod_edit:{item_id}")
            if "status" in update and update["status"] not in TRIPOD_STATUSES:
                raise ValueError(f"invalid_tripod_status:{item_id}")
            row.update(copy.deepcopy(update)); row.update(assessed_by=assessor, assessed_at=_now())
            changed.append(f"tripod:{item_id}")
        for key, update in (edits.get("probast") or {}).items():
            phase, _, item_id = key.partition(":")
            row = next((x for x in audit["probast"]["questions"]
                        if x["phase"] == phase and x["item_id"] == item_id), None)
            if row is None:
                raise ValueError(f"unknown_probast_item:{key}")
            allowed = {"answer", "rationale", "remaining_action", "evidence_ids"}
            if set(update) - allowed:
                raise ValueError(f"unsupported_probast_edit:{key}")
            answer = update.get("answer", row["answer"])
            if answer not in PROBAST_ANSWERS:
                raise ValueError(f"invalid_probast_answer:{key}")
            if answer == "NA" and key not in NA_ALLOWED:
                raise ValueError(f"na_not_allowed:{key}")
            if answer == "NA" and not str(update.get("rationale", row.get("rationale", ""))).strip():
                raise ValueError(f"na_rationale_required:{key}")
            row.update(copy.deepcopy(update)); row.update(assessed_by=assessor, assessed_at=_now())
            changed.append(f"probast:{key}")
        domain_rows = audit["probast"]["domains"] + audit["probast"]["overall_judgments"]
        for domain_id, update in (edits.get("domains") or {}).items():
            if domain_id in {"overall:development_quality", "overall:evaluation_risk",
                             "overall:applicability_new_korean_diary"}:
                raise ValueError(f"derived_overall_is_read_only:{domain_id}")
            row = next((x for x in domain_rows if x["domain_id"] == domain_id), None)
            if row is None:
                raise ValueError(f"unknown_domain:{domain_id}")
            if set(update) - {"judgment", "rationale"}:
                raise ValueError(f"unsupported_domain_edit:{domain_id}")
            if update.get("judgment", row["judgment"]) not in DOMAIN_JUDGMENTS:
                raise ValueError(f"invalid_domain_judgment:{domain_id}")
            row.update(copy.deepcopy(update)); row.update(assessed_by=assessor, assessed_at=_now())
            changed.append(f"domain:{domain_id}")
        if "evaluation_design" in edits:
            design = str(edits["evaluation_design"])
            if design not in {"unspecified", "simple_heldout", "reused_heldout", "resampling", "mixed"}:
                raise ValueError("invalid_evaluation_design")
            audit["probast"]["evaluation_design"] = design
            changed.append("evaluation_design")
        self._derive_overall(audit, assessor)
        if "title" in edits:
            audit["title"] = str(edits["title"]).strip() or audit["title"]
            changed.append("title")
        if "remaining_tasks" in edits:
            tasks = edits["remaining_tasks"]
            if (not isinstance(tasks, dict) or set(tasks) != set(audit["remaining_tasks"]) or
                    any(not isinstance(items, list) or any(not isinstance(item, str) or not item.strip()
                        for item in items) for items in tasks.values())):
                raise ValueError("invalid_remaining_tasks")
            audit["remaining_tasks"] = copy.deepcopy(tasks)
            changed.append("remaining_tasks")
        audit["review_history"].append({"at":_now(), "assessed_by":assessor,
                                        "changed":changed, "reason":str(edits.get("reason", ""))})
        audit["artifact_sha256"] = _artifact_hash(audit)
        checked = self.check(audit)
        if checked["errors"]:
            raise ValueError("invalid_revision:" + ";".join(checked["errors"]))
        _atomic_new_json(self._path(audit["audit_id"]), audit)
        return audit

    def _snapshot_document(self, source_path: str | Path, title: str) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        raw = source.read_bytes()
        if len(raw) > 50_000_000:
            raise ValueError("evidence_document_too_large")
        digest = _sha(raw)
        suffix = source.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,10}", source.suffix.lower()) else ".bin"
        evidence_root = self.root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        snapshot = evidence_root / f"{digest}{suffix}"
        if snapshot.exists():
            if _sha(snapshot.read_bytes()) != digest:
                raise ValueError("evidence_snapshot_hash_mismatch")
        else:
            fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=evidence_root)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                if snapshot.exists():
                    raise FileExistsError("evidence_snapshot_race")
                os.replace(temporary, snapshot)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        return {"id":f"EVIDENCE_{digest[:16]}", "title":str(title),
                "sha256":digest, "size_bytes":len(raw), "snapshot_path":str(snapshot)}

    def attach_document(
        self,
        audit_id: str,
        path: str | Path,
        title: str,
        *,
        assessed_by: str,
        item_links: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Snapshot a document and return a new audit revision linked to selected items."""
        return self.revise(
            audit_id,
            {"add_sources":[{"path":str(path), "title":title, "link_to":item_links or {}}],
             "reason":f"Attached evidence: {title}"},
            assessed_by=assessed_by,
        )

    def check(self, audit_or_id: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(audit_or_id, str):
            audit = json.loads(self._path(audit_or_id).read_text(encoding="utf-8"))
        else:
            audit = audit_or_id
        errors: list[str] = []
        warnings: list[str] = []
        if audit.get("schema") != AUDIT_SCHEMA:
            errors.append("unsupported_schema")
        if audit.get("artifact_sha256") != _artifact_hash(audit):
            errors.append("artifact_hash_mismatch")
        tripod = audit.get("tripod", {}).get("items", [])
        if len(tripod) != 52 or {x.get("item_id") for x in tripod} != {x[0] for x in TRIPOD_ITEMS}:
            errors.append("tripod_catalog_mismatch")
        for row in tripod:
            if row.get("status") not in TRIPOD_STATUSES:
                errors.append(f"invalid_tripod_status:{row.get('item_id')}")
        questions = audit.get("probast", {}).get("questions", [])
        expected = {f"{p}:{i}" for p, i, _ in PROBAST_ITEMS}
        actual = {f"{x.get('phase')}:{x.get('item_id')}" for x in questions}
        if len(questions) != 34 or actual != expected:
            errors.append("probast_catalog_mismatch")
        for row in questions:
            key = f"{row.get('phase')}:{row.get('item_id')}"
            answer = row.get("answer")
            if answer not in PROBAST_ANSWERS:
                errors.append(f"invalid_probast_answer:{key}")
            if answer == "NA" and key not in NA_ALLOWED:
                errors.append(f"na_not_allowed:{key}")
            if answer == "NA" and not str(row.get("rationale", "")).strip():
                errors.append(f"na_rationale_required:{key}")
        evaluation_design = audit.get("probast", {}).get("evaluation_design", "unspecified")
        if evaluation_design not in {"unspecified", "simple_heldout", "reused_heldout", "resampling", "mixed"}:
            errors.append("invalid_evaluation_design")
        answers = {f"{row.get('phase')}:{row.get('item_id')}":row.get("answer") for row in questions}
        if answers.get("evaluation:4.5") == "NA":
            warnings.append("evaluation_4.5_na_requires_design_specific_justification")
        if answers.get("evaluation:4.6") == "NA" and evaluation_design not in {"simple_heldout", "reused_heldout"}:
            warnings.append("evaluation_4.6_na_without_simple_heldout_design")
        domains = audit.get("probast", {}).get("domains", [])
        expected_domains = ({f"{phase}:{domain}" for phase in ("development", "evaluation")
                             for domain in ("1", "2", "3", "4")}
                            | {"applicability:participants", "applicability:predictors", "applicability:outcome"})
        if len(domains) != 11 or {row.get("domain_id") for row in domains} != expected_domains:
            errors.append("probast_domain_catalog_mismatch")
        overall_rows = audit.get("probast", {}).get("overall_judgments", [])
        expected_overall = {"overall:development_quality", "overall:evaluation_risk",
                            "overall:applicability_same_endpoint", "overall:applicability_new_korean_diary"}
        if len(overall_rows) != 4 or {row.get("domain_id") for row in overall_rows} != expected_overall:
            errors.append("probast_overall_catalog_mismatch")
        for row in domains + overall_rows:
            if row.get("judgment") not in DOMAIN_JUDGMENTS:
                errors.append(f"invalid_domain_judgment:{row.get('domain_id')}")
        for row in domains:
            if row.get("judgment") == "Low" and row.get("phase") in {"development", "evaluation"}:
                unresolved = [q for q in questions if q.get("phase") == row["phase"]
                              and q.get("domain") == row["domain"] and q.get("answer") in {"NI", "PN", "N"}]
                if unresolved:
                    warnings.append(f"low_judgment_with_unresolved_signals:{row['domain_id']}")
            if row.get("judgment") != "Unclear" and not str(row.get("rationale", "")).strip():
                warnings.append(f"domain_rationale_missing:{row.get('domain_id')}")
        overall_by_id = {row.get("domain_id"):row for row in overall_rows}
        for overall_id, phase in (("overall:development_quality", "development"),
                                  ("overall:evaluation_risk", "evaluation"),
                                  ("overall:applicability_new_korean_diary", "applicability")):
            judgments = {row.get("judgment") for row in domains if row.get("phase") == phase}
            expected = "High" if "High" in judgments else ("Unclear" if "Unclear" in judgments else "Low")
            if overall_by_id.get(overall_id, {}).get("judgment") != expected:
                errors.append(f"overall_domain_rule_mismatch:{overall_id}")
        source_ids = {str(row.get("id")) for row in audit.get("sources", [])}
        for source in audit.get("sources", []):
            if source.get("contained_in") and source.get("contained_in") not in source_ids:
                errors.append(f"source_parent_missing:{source.get('id')}")
            if source.get("snapshot") is not None:
                if _sha(_canonical(source["snapshot"])) != source.get("snapshot_sha256"):
                    errors.append(f"source_snapshot_hash_mismatch:{source.get('id')}")
            if source.get("snapshot_path"):
                snapshot_path = Path(source["snapshot_path"])
                if not snapshot_path.exists():
                    errors.append(f"source_snapshot_missing:{source.get('id')}")
                elif _sha(snapshot_path.read_bytes()) != source.get("sha256"):
                    errors.append(f"source_snapshot_hash_mismatch:{source.get('id')}")
            if source.get("source_path") and source.get("sha256"):
                source_path = Path(source["source_path"])
                if not source_path.exists():
                    warnings.append(f"primary_source_missing_digest_retained:{source.get('id')}")
                elif _sha(source_path.read_bytes()) != source.get("sha256"):
                    warnings.append(f"primary_source_changed_digest_retained:{source.get('id')}")
        for row in tripod:
            missing = set(row.get("evidence_ids", [])) - source_ids
            if missing: errors.append(f"unknown_evidence:{row.get('item_id')}:{','.join(sorted(missing))}")
        for row in questions:
            missing = set(row.get("evidence_ids", [])) - source_ids
            if missing: errors.append(f"unknown_evidence:{row.get('phase')}:{row.get('item_id')}")
        evaluation = audit.get("evaluation")
        if evaluation:
            snapshot = evaluation.get("snapshot")
            if _sha(_canonical(snapshot)) != evaluation.get("snapshot_sha256"):
                errors.append("evaluation_snapshot_hash_mismatch")
            if not isinstance(snapshot, dict) or snapshot.get("schema") != EVALUATION_SCHEMA:
                errors.append("evaluation_snapshot_schema_invalid")
            elif (snapshot.get("policy") != "gradual_v1" or
                  (snapshot.get("status") or snapshot.get("evaluation_status")) != "retrospective_reused_holdout"):
                errors.append("evaluation_scope_invalid")
            sensitive = _contains_sensitive_keys(snapshot)
            if sensitive: errors.append("evaluation_contains_row_identifiers")
            source_path = Path(str(evaluation.get("source_path", "")))
            if not source_path.exists():
                warnings.append("evaluation_source_missing_snapshot_retained")
            elif _sha(source_path.read_bytes()) != evaluation.get("source_sha256"):
                warnings.append("evaluation_source_changed_snapshot_retained")
        else:
            warnings.append("performance_evaluation_not_attached")
        if not audit.get("not_a_score_or_certification"):
            errors.append("score_or_certification_disclaimer_missing")
        if (audit.get("assessment_state") != "assessor_assisted_draft" or
                not audit.get("human_review_required") or not str(audit.get("certification_disclaimer", "")).strip()):
            errors.append("human_review_draft_disclosure_missing")
        counts = {
            "tripod": dict(Counter(row.get("status") for row in tripod)),
            "probast": dict(Counter(row.get("answer") for row in questions)),
            "domain_judgments": dict(Counter(row.get("judgment") for row in domains)),
        }
        return {"valid":not errors, "errors":errors, "warnings":warnings, "counts":counts}

    def export_pdf(self, audit_id: str, destination_file: str | Path) -> Path:
        """Export one printable assessment without changing the immutable audit."""
        audit = self.get(audit_id)
        target = Path(destination_file).expanduser().resolve()
        if target.suffix.lower() != ".pdf":
            raise ValueError("assessment_destination_must_be_pdf")
        if target.exists():
            raise FileExistsError("assessment_pdf_already_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".guideline-pdf-") as temporary:
            pdf = Path(temporary) / "assessment.pdf"
            self._write_pdf(pdf, audit)
            with target.open("xb") as handle:
                handle.write(pdf.read_bytes())
        return target

    def export(self, audit_id: str, output_dir: str | Path | None = None) -> dict[str, str]:
        audit = self.get(audit_id)
        base = Path(output_dir).expanduser().resolve() if output_dir else self.root / "exports"
        target = base / audit_id
        if target.exists() and any(target.iterdir()):
            raise FileExistsError("immutable_export_package_exists")
        target.mkdir(parents=True, exist_ok=True)
        paths = {
            "json": target / f"{audit_id}.json",
            "markdown": target / f"{audit_id}.md",
            "tripod_csv": target / f"{audit_id}_tripod.csv",
            "probast_csv": target / f"{audit_id}_probast.csv",
            "metrics_csv": target / f"{audit_id}_metrics.csv",
            "diagnostics_csv": target / f"{audit_id}_diagnostics.csv",
            "paired_differences_csv": target / f"{audit_id}_paired_policy_differences.csv",
            "docx": target / f"{audit_id}.docx",
            "pdf": target / f"{audit_id}.pdf",
        }
        paths["json"].write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)+"\n", encoding="utf-8")
        md = self._markdown(audit)
        paths["markdown"].write_text(md, encoding="utf-8")
        self._write_csv(paths["tripod_csv"], ["item_id","scope","label","status","evidence_ids","note","remaining_action"], audit["tripod"]["items"])
        self._write_csv(paths["probast_csv"], ["phase","item_id","domain","label","answer","evidence_ids","rationale","remaining_action"], audit["probast"]["questions"])
        snapshot = audit.get("evaluation", {}).get("snapshot", {})
        self._write_records_csv(paths["metrics_csv"], snapshot.get("metrics", []))
        self._write_records_csv(paths["diagnostics_csv"], snapshot.get("diagnostics", []))
        self._write_records_csv(paths["paired_differences_csv"], snapshot.get("paired_policy_differences", []))
        self._write_docx(paths["docx"], audit)
        self._write_pdf(paths["pdf"], audit)
        manifest = {name:{"path":str(path), "sha256":_sha(path.read_bytes())} for name, path in paths.items()}
        manifest_path = target / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)+"\n", encoding="utf-8")
        return {name:str(path) for name, path in paths.items()} | {"manifest":str(manifest_path)}

    @staticmethod
    def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for source in rows:
                row = copy.deepcopy(source)
                row["evidence_ids"] = ";".join(row.get("evidence_ids", []))
                writer.writerow(row)

    @staticmethod
    def _write_records_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        fields = sorted({str(key) for row in rows if isinstance(row, dict) for key in row})
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            if not fields:
                handle.write("status\r\nnot_available\r\n")
                return
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for source in rows:
                writer.writerow({key:GuidelineAuditService._display(source.get(key)) for key in fields})

    @staticmethod
    def _human_evaluation(snapshot: dict[str, Any]) -> dict[str, Any]:
        primary_metrics = [
            row for row in snapshot.get("metrics", [])
            if row.get("split") == "test" and row.get("weighting") == "participant"
            and row.get("metric") in {"onset_mae_min", "wake_mae_min", "joint_mae_min"}
            and row.get("evaluation_role", "reused_holdout") == "reused_holdout"
        ]
        paired = [
            row for row in snapshot.get("paired_policy_differences", [])
            if row.get("split") == "test" and row.get("weighting") == "participant"
            and str(row.get("metric", "")).split("_mae_", 1)[0] in {"onset", "wake", "joint"}
        ]
        diagnostics = [
            row for row in snapshot.get("diagnostics", [])
            if row.get("split") == "test" and row.get("weighting") == "participant"
            and row.get("policy") == "gradual_v1" and row.get("arm") == "data"
        ]
        intervals = [
            row for row in snapshot.get("exploratory_residual_intervals", [])
            if row.get("weighting") == "participant" and row.get("policy") == "gradual_v1"
            and row.get("arm") == "data"
        ]
        return {
            "primary_metrics": sorted(primary_metrics, key=lambda row:(str(row.get("cohort")), str(row.get("policy")), str(row.get("arm")), str(row.get("metric")))),
            "paired": sorted(paired, key=lambda row:(str(row.get("cohort")), str(row.get("arm")), str(row.get("metric")))),
            "diagnostics": diagnostics,
            "intervals": intervals,
            "flow": snapshot.get("cohort_flow", []),
            "failure_summary": snapshot.get("failure_summary", {}),
            "strata_counts": {
                "history":len(snapshot.get("history_strata", [])),
                "rarity":len(snapshot.get("rarity_strata", [])),
                "demographic":len(snapshot.get("demographic_strata", [])),
                "very_small_group_cautions":sum(
                    bool(row.get("very_small_group_caution"))
                    for key in ("history_strata", "rarity_strata", "demographic_strata")
                    for row in snapshot.get(key, [])
                ),
            },
        }

    def _append_markdown_table(self, lines: list[str], title: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
        lines += ["", f"### {title}", ""]
        if not rows:
            lines.append("- Not available")
            return
        lines += ["|" + "|".join(fields) + "|", "|" + "|".join("---" for _ in fields) + "|"]
        for row in rows:
            values = [self._human_display(row.get(field)).replace("|", "\\|").replace("\n", " ") for field in fields]
            lines.append("|" + "|".join(values) + "|")

    def _markdown(self, audit: dict[str, Any]) -> str:
        check = self.check(audit)
        lines = [f"# {audit['title']}", "", f"- Audit ID: `{audit['audit_id']}`",
                 f"- Created: {audit['created_at']}", f"- Template: `{audit['template']}`",
                 f"- Assessment state: `{audit['assessment_state']}`",
                 f"- Seeded: `{audit['seeded']}`; provisional: `{audit['provisional']}`; human review required: `{audit['human_review_required']}`",
                 f"- {audit['certification_disclaimer']}", "",
                 "## Evaluation scope", ""]
        if audit.get("evaluation"):
            snap = audit["evaluation"]["snapshot"]
            lines += [f"- Schema: `{snap.get('schema')}`", f"- Policy: `{snap.get('policy')}`",
                      f"- Status: `{snap.get('status') or snap.get('evaluation_status')}`",
                      f"- Snapshot SHA-256: `{audit['evaluation']['snapshot_sha256']}`"]
            for limitation in snap.get("limitations", []):
                lines.append(f"- Limitation: {limitation}")
            summary = self._human_evaluation(snap)
            self._append_markdown_table(lines, "Primary reused-test participant-weighted MAE",
                ["cohort","policy","arm","metric","value","ci95_low","ci95_high","n_nights","n_participants"],
                summary["primary_metrics"])
            self._append_markdown_table(lines, PAIRED_REDUCTION_HEADING,
                ["cohort","arm","metric","value","ci95_low","ci95_high","n_nights","n_participants"], summary["paired"])
            self._append_markdown_table(lines, "Residual and calibration diagnostics (gradual_v1 data arm)",
                ["cohort","endpoint","residual_bias_min","rmse_min","calibration_intercept_min","calibration_slope","pearson_correlation","n_nights","n_participants"],
                summary["diagnostics"])
            self._append_markdown_table(lines, "Exploratory residual intervals (not guaranteed individual intervals)",
                ["cohort","endpoint","mean_interval_width_min","empirical_coverage","n_validation_nights","n_test_nights","guarantee"], summary["intervals"])
            self._append_markdown_table(lines, "Cohort flow",
                ["cohort","split","n_rows","n_participants","age_missing_rows","sex_missing_rows"], summary["flow"])
            lines += ["", "### Failures and subgroup coverage", "",
                      f"- Failure summary: `{self._human_display(summary['failure_summary'])}`",
                      f"- Strata row counts: `{self._human_display(summary['strata_counts'])}`",
                      "- Full aggregate details are preserved in the JSON and dedicated CSV exports."]
        else:
            lines.append("- No gradual_v1 aggregate evaluation is attached; performance remains unknown here.")
        lines += ["", "## TRIPOD+AI reporting matrix", "",
                  "|Item|Scope|Topic|Status|Evidence|Note|Remaining action|",
                  "|---|---|---|---|---|---|---|"]
        for row in audit["tripod"]["items"]:
            values = [row["item_id"], row["scope"], row["label"], row["status"],
                      ", ".join(row["evidence_ids"]), row["note"], row["remaining_action"]]
            lines.append("|" + "|".join(str(x).replace("|", "\\|").replace("\n", " ") for x in values) + "|")
        lines += ["", "## PROBAST+AI signalling questions", "",
                  "|Phase|Question|Domain|Topic|Answer|Rationale|Remaining action|",
                  "|---|---|---|---|---|---|---|"]
        for row in audit["probast"]["questions"]:
            values = [row["phase"], row["item_id"], row["domain"], row["label"],
                      row["answer"], row["rationale"], row["remaining_action"]]
            lines.append("|" + "|".join(str(x).replace("|", "\\|").replace("\n", " ") for x in values) + "|")
        lines += ["", "## Human domain judgments", ""]
        for row in audit["probast"]["domains"] + audit["probast"]["overall_judgments"]:
            lines.append(f"- **{row['domain_id']} — {row['judgment']}**: {row.get('rationale','')}")
        lines += ["", "## Remaining work", ""]
        for group, tasks in audit["remaining_tasks"].items():
            lines.append(f"### {group}"); lines.append("")
            lines.extend(f"- {task}" for task in tasks); lines.append("")
        lines += ["## Sources", ""]
        for source in audit["sources"]:
            location = source.get("url") or source.get("snapshot_path") or source.get("path") or ""
            digest = source.get("sha256") or source.get("snapshot_sha256") or ""
            lines.append(f"- {source['id']}: {source['title']} — {location} — SHA-256 `{digest}`")
        lines += ["", "## Validation", "", f"- Errors: {len(check['errors'])}",
                  f"- Warnings: {len(check['warnings'])}"]
        return "\n".join(lines) + "\n"

    def _write_docx(self, path: Path, audit: dict[str, Any]) -> None:
        from docx import Document
        from docx.shared import Pt
        document = Document()
        document.styles["Normal"].font.name = "Malgun Gothic"
        document.styles["Normal"].font.size = Pt(9)
        document.add_heading(audit["title"], level=0)
        document.add_paragraph(audit["certification_disclaimer"])
        document.add_paragraph(
            f"State: {audit['assessment_state']}; seeded={audit['seeded']}; "
            f"provisional={audit['provisional']}; human review required={audit['human_review_required']}"
        )
        if audit.get("evaluation"):
            snapshot = audit["evaluation"]["snapshot"]
            summary = self._human_evaluation(snapshot)
            document.add_heading("Evaluation evidence", level=1)
            document.add_paragraph(
                f"{snapshot.get('schema')} · {snapshot.get('policy')} · {snapshot.get('status') or snapshot.get('evaluation_status')} · "
                f"snapshot {audit['evaluation']['snapshot_sha256']}"
            )
            sections = [
                ("Primary reused-test participant-weighted MAE",
                 ["cohort","policy","arm","metric","value","ci95_low","ci95_high","n_nights","n_participants"], summary["primary_metrics"]),
                (PAIRED_REDUCTION_HEADING,
                 ["cohort","arm","metric","value","ci95_low","ci95_high","n_nights","n_participants"], summary["paired"]),
                ("Residual and calibration diagnostics",
                 ["cohort","endpoint","residual_bias_min","rmse_min","calibration_slope","pearson_correlation","n_nights","n_participants"], summary["diagnostics"]),
                ("Cohort flow", ["cohort","split","n_rows","n_participants","age_missing_rows","sex_missing_rows"], summary["flow"]),
            ]
            for title, fields, rows in sections:
                document.add_heading(title, level=2)
                if not rows:
                    document.add_paragraph("Not available")
                    continue
                table = document.add_table(rows=1, cols=len(fields)); table.style = "Table Grid"
                for cell, value in zip(table.rows[0].cells, fields): cell.text = value
                for row in rows:
                    for cell, value in zip(table.add_row().cells, (self._human_display(row.get(field)) for field in fields)):
                        cell.text = value
            document.add_heading("Exploratory residual intervals", level=2)
            for row in summary["intervals"]:
                document.add_paragraph(self._human_display(row), style="List Bullet")
            document.add_heading("Failures, subgroups and limitations", level=2)
            document.add_paragraph("Failure summary: " + self._human_display(summary["failure_summary"]))
            document.add_paragraph("Strata counts: " + self._human_display(summary["strata_counts"]))
            for limitation in snapshot.get("limitations", []):
                document.add_paragraph(str(limitation), style="List Bullet")
        document.add_heading("TRIPOD+AI", level=1)
        table = document.add_table(rows=1, cols=5); table.style = "Table Grid"
        for cell, value in zip(table.rows[0].cells, ("Item","Topic","Status","Evidence","Note")): cell.text = value
        for row in audit["tripod"]["items"]:
            cells = table.add_row().cells
            for cell, value in zip(cells, (row["item_id"],row["label"],row["status"],", ".join(row["evidence_ids"]),row["note"])): cell.text = str(value)
        document.add_heading("PROBAST+AI", level=1)
        table = document.add_table(rows=1, cols=6); table.style = "Table Grid"
        for cell, value in zip(table.rows[0].cells, ("Phase","Question","Domain","Topic","Answer","Rationale")): cell.text = value
        for row in audit["probast"]["questions"]:
            cells = table.add_row().cells
            for cell, value in zip(cells, (row["phase"],row["item_id"],row["domain"],row["label"],row["answer"],row["rationale"])): cell.text = str(value)
        document.add_heading("Domain judgments", level=1)
        for row in audit["probast"]["domains"] + audit["probast"]["overall_judgments"]:
            document.add_paragraph(f"{row['domain_id']}: {row['judgment']} — {row.get('rationale','')}")
        document.add_heading("Remaining work", level=1)
        for group, tasks in audit["remaining_tasks"].items():
            document.add_heading(group, level=2)
            for task in tasks: document.add_paragraph(task, style="List Bullet")
        document.add_heading("Sources", level=1)
        for source in audit["sources"]:
            location = source.get("url") or source.get("snapshot_path") or source.get("path") or ""
            digest = source.get("sha256") or source.get("snapshot_sha256") or ""
            document.add_paragraph(f"{source['id']}: {source['title']} — {location} — SHA-256 {digest}")
        document.save(path)

    def _write_pdf(self, path: Path, audit: dict[str, Any]) -> None:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
        font = "Helvetica"
        for candidate in (Path("C:/Windows/Fonts/malgun.ttf"), Path("C:/Windows/Fonts/arial.ttf")):
            if candidate.exists():
                font = "GuidelineAuditFont"; pdfmetrics.registerFont(TTFont(font, str(candidate))); break
        styles = getSampleStyleSheet()
        body = ParagraphStyle("AuditBody", parent=styles["BodyText"], fontName=font, fontSize=8, leading=11, alignment=TA_LEFT)
        heading = ParagraphStyle("AuditHeading", parent=styles["Heading1"], fontName=font, fontSize=13, leading=16)
        story = [Paragraph(self._escape_pdf(audit["title"]), heading), Spacer(1, 4*mm),
                 Paragraph(self._escape_pdf(audit["certification_disclaimer"]), body),
                 Paragraph(self._escape_pdf(
                     f"State: {audit['assessment_state']}; seeded={audit['seeded']}; provisional={audit['provisional']}; "
                     f"human review required={audit['human_review_required']}"
                 ), body)]
        if audit.get("evaluation"):
            snapshot = audit["evaluation"]["snapshot"]
            summary = self._human_evaluation(snapshot)
            story.append(Paragraph("Evaluation evidence", heading))
            story.append(Paragraph(
                self._escape_pdf(f"{snapshot.get('schema')} · {snapshot.get('policy')} · {snapshot.get('status') or snapshot.get('evaluation_status')}"), body
            ))
            story.append(Paragraph("Primary reused-test participant-weighted MAE", heading))
            for row in summary["primary_metrics"]:
                story.append(Paragraph(self._escape_pdf(
                    f"{row.get('cohort')} · {row.get('policy')} · {row.get('arm')} · {row.get('metric')}: "
                    f"{self._human_number(row.get('value'))} (95% CI {self._human_number(row.get('ci95_low'))} to {self._human_number(row.get('ci95_high'))}; "
                    f"{row.get('n_participants')} participants, {row.get('n_nights')} nights)"
                ), body))
            story.append(Paragraph(self._escape_pdf(PAIRED_REDUCTION_HEADING), heading))
            for row in summary["paired"]:
                story.append(Paragraph(self._escape_pdf(
                    f"{row.get('cohort')} · {row.get('arm')} · {row.get('metric')}: {self._human_number(row.get('value'))} "
                    f"(95% CI {self._human_number(row.get('ci95_low'))} to {self._human_number(row.get('ci95_high'))})"
                ), body))
            story.append(Paragraph("Residual, interval, flow and subgroup summary", heading))
            for row in summary["diagnostics"]:
                story.append(Paragraph(self._escape_pdf(
                    f"Diagnostic {row.get('cohort')} {row.get('endpoint')}: bias={self._human_number(row.get('residual_bias_min'))}, "
                    f"RMSE={self._human_number(row.get('rmse_min'))}, slope={self._human_number(row.get('calibration_slope'))}, "
                    f"r={self._human_number(row.get('pearson_correlation'))}"
                ), body))
            for row in summary["intervals"]:
                story.append(Paragraph(self._escape_pdf(
                    f"Exploratory interval {row.get('cohort')} {row.get('endpoint')}: width={self._human_number(row.get('mean_interval_width_min'))}, "
                    f"coverage={self._human_number(row.get('empirical_coverage'))}; {row.get('guarantee')}"
                ), body))
            for row in summary["flow"]:
                story.append(Paragraph(self._escape_pdf(
                    f"Flow {row.get('cohort')} {row.get('split')}: {row.get('n_participants')} participants, {row.get('n_rows')} rows"
                ), body))
            story.append(Paragraph(self._escape_pdf("Failure summary: " + self._human_display(summary["failure_summary"])), body))
            story.append(Paragraph(self._escape_pdf("Strata counts: " + self._human_display(summary["strata_counts"])), body))
            for limitation in snapshot.get("limitations", []):
                story.append(Paragraph(self._escape_pdf("Limitation: " + str(limitation)), body))
        story.append(Paragraph("TRIPOD+AI", heading))
        for row in audit["tripod"]["items"]:
            story.append(Paragraph(self._escape_pdf(f"{row['item_id']} · {row['label']} · {row['status']} — {row['note']}"), body))
        story.extend([PageBreak(), Paragraph("PROBAST+AI", heading)])
        for row in audit["probast"]["questions"]:
            story.append(Paragraph(self._escape_pdf(f"{row['phase']} {row['item_id']} · {row['label']} · {row['answer']} — {row['rationale']}"), body))
        story.append(Paragraph("Domain judgments", heading))
        for row in audit["probast"]["domains"] + audit["probast"]["overall_judgments"]:
            story.append(Paragraph(self._escape_pdf(f"{row['domain_id']}: {row['judgment']} — {row.get('rationale','')}"), body))
        story.append(Paragraph("Remaining work", heading))
        for group, tasks in audit["remaining_tasks"].items():
            story.append(Paragraph(self._escape_pdf(group), heading))
            for task in tasks:
                story.append(Paragraph(self._escape_pdf("• " + task), body))
        story.append(Paragraph("Sources", heading))
        for source in audit["sources"]:
            location = source.get("url") or source.get("snapshot_path") or source.get("path") or ""
            digest = source.get("sha256") or source.get("snapshot_sha256") or ""
            story.append(Paragraph(self._escape_pdf(
                f"{source['id']}: {source['title']} — {location} — SHA-256 {digest}"
            ), body))
        SimpleDocTemplate(str(path), pagesize=A4, rightMargin=14*mm, leftMargin=14*mm,
                          topMargin=14*mm, bottomMargin=14*mm).build(story)

    @staticmethod
    def _display(value: Any) -> str:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return "" if value is None else str(value)

    @staticmethod
    def _human_number(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.3f}".rstrip("0").rstrip(".")
        return "" if value is None else str(value)

    @classmethod
    def _human_display(cls, value: Any) -> str:
        if isinstance(value, dict):
            return json.dumps({key:cls._human_value(item) for key, item in value.items()}, ensure_ascii=False, sort_keys=True)
        if isinstance(value, (list, tuple)):
            return json.dumps([cls._human_value(item) for item in value], ensure_ascii=False)
        return cls._human_number(value)

    @classmethod
    def _human_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key:cls._human_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._human_value(item) for item in value]
        if isinstance(value, float):
            return float(cls._human_number(value))
        return value

    @staticmethod
    def _escape_pdf(value: str) -> str:
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "AUDIT_SCHEMA", "EVALUATION_SCHEMA", "GuidelineAuditService", "NA_ALLOWED",
    "PROBAST_ANSWERS", "PROBAST_ITEMS", "TRIPOD_ITEMS", "TRIPOD_STATUSES",
]
