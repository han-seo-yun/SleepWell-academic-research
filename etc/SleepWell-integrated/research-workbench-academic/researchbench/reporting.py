from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .result_adapter import adapt_run, digest, resolve_field, verify_run


REPORT_SCHEMA = "researchbench.report.v1"
PROMPT_VERSION = "0"
SUPPORTED_LANGUAGES = {"ko", "en"}
SUPPORTED_TEMPLATES = {"research", "industrial", "model_card", "comparison", "gradual_guidelines"}
CHECKLIST_STATUSES = {
    "satisfied", "partially_satisfied", "not_satisfied", "not_applicable", "judgment_pending"
}
_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|token|raw[_-]?(?:patient|participant|data)|full[_-]?text|"
    r"(?:^|_)(?:rows|records|participant_ids|patient_ids)(?:$|_))",
    re.IGNORECASE,
)
_SELECTED_SCOPE_FIELDS = (
    "source_rows", "rows", "participants", "cohort", "split",
    "max_nights_per_participant", "selection_rule", "purpose", "source_sha256",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _artifact_hash(report: dict[str, Any]) -> str:
    payload = copy.deepcopy(report)
    payload.pop("artifact_sha256", None)
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _stable_id(prefix: str, *values: Any) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:16]}"


def _display_number(value: Any) -> str:
    if value is None:
        return "NA"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "NA"
    return format(number, ".12g")


def _safe_context(value: Any, *, depth: int = 0) -> Any:
    """Keep report context compact and exclude credential/raw-record shaped fields."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE_KEY.search(name):
                continue
            result[name] = _safe_context(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_context(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _selected_scope(config: Any) -> dict[str, Any]:
    """Expose only aggregate selection metadata, never row or participant identifiers."""
    if not isinstance(config, dict) or not isinstance(config.get("selection"), dict):
        return {}
    selection = config["selection"]
    scope = {
        key: selection[key]
        for key in _SELECTED_SCOPE_FIELDS
        if key in selection and isinstance(selection[key], (str, int, float, bool))
    }
    counts = selection.get("actual_cohort_participants")
    if (isinstance(counts, dict) and 0 < len(counts) <= 20
            and all(isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9_. -]{1,64}", label)
                    and type(count) is int and count > 0 for label, count in counts.items())
            and sum(counts.values()) == selection.get("participants")):
        scope["actual_cohort_participants"] = dict(sorted(counts.items()))
    return scope


class ReportService:
    """Versioned deterministic reports backed by experiment and document evidence.

    Report JSON is immutable. Edits, translations, automatic checks, and human-review
    records create descendants instead of replacing an earlier artifact.
    """

    def __init__(self, project_path: str | Path, library: Any | None = None) -> None:
        self.project_path = Path(project_path).expanduser().resolve()
        if not self.project_path.exists() or not self.project_path.is_dir():
            raise FileNotFoundError(self.project_path)
        self.root = self.project_path / "research" / "reports"
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_evidence_root = self.project_path / "research" / "run_evidence"
        self.run_evidence_root.mkdir(parents=True, exist_ok=True)
        self.library = library
        locale_path = Path(__file__).with_name("locales") / "reports.json"
        self.locales = json.loads(locale_path.read_text(encoding="utf-8"))

    def _t(self, lang: str, key: str) -> str:
        return self.locales[lang][key]

    def _new_report_id(self) -> str:
        return f"report_{uuid.uuid4().hex}"

    def _report_path(self, report_id: str) -> Path:
        if not _REPORT_ID_RE.fullmatch(report_id):
            raise ValueError("invalid_report_id")
        return self.root / f"{report_id}.json"

    def save(self, report: dict[str, Any]) -> Path:
        payload = copy.deepcopy(report)
        report_id = str(payload.get("report_id", ""))
        path = self._report_path(report_id)
        payload["artifact_sha256"] = _artifact_hash(payload)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing != encoded:
                raise FileExistsError("immutable_report_version")
            return path
        fd, temporary = tempfile.mkstemp(prefix=f".{report_id}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        report.clear()
        report.update(payload)
        return path

    def get(self, report_id: str) -> dict[str, Any]:
        path = self._report_path(report_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("artifact_sha256") != _artifact_hash(payload):
            raise ValueError("report_artifact_hash_mismatch")
        return payload

    def list_reports(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("report_*.json")):
            try:
                report = self.get(path.stem)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            results.append(
                {
                    "report_id": report["report_id"],
                    "version": report["version"],
                    "parent_report_id": report.get("parent_report_id"),
                    "template": report["template"],
                    "language": report["language"],
                    "status": report["status"],
                    "created_at": report["created_at"],
                    "artifact_sha256": report["artifact_sha256"],
                }
            )
        return sorted(results, key=lambda item: (item["created_at"], item["report_id"]), reverse=True)

    def create(
        self,
        views: list[dict[str, Any]],
        template: str = "research",
        lang: str = "ko",
        question: str = "",
        context: dict[str, Any] | None = None,
        citations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if template == "gradual_guidelines":
            return self.create_gradual(lang=lang, citations=citations)
        if template not in SUPPORTED_TEMPLATES:
            raise ValueError("unsupported_report_template")
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError("unsupported_report_language")
        if not views:
            raise ValueError("at_least_one_run_required")
        safe_context = _safe_context(context or {})
        run_refs: list[dict[str, Any]] = []
        numeric_claims: list[dict[str, Any]] = []
        for view in views:
            original = verify_run(view)
            engine = str(view.get("engine", ""))
            if engine not in {"general", "sleepwell"}:
                raise ValueError("unsupported_run_engine")
            canonical_view = adapt_run(original, engine, str(view.get("project_path") or ""))
            if canonical_view["result_hash"] != view.get("result_hash") or canonical_view["run_id"] != view.get("run_id"):
                raise ValueError("normalized_run_identity_mismatch")
            frozen_path = self._freeze_run(original, canonical_view["result_hash"])
            config = canonical_view.get("config", {})
            run_ref = {
                "run_id": str(canonical_view.get("run_id", "")),
                "engine": engine,
                "name": str(canonical_view.get("name", canonical_view.get("run_id", ""))),
                "result_hash": str(canonical_view.get("result_hash", "")),
                "data_hash": str(canonical_view.get("data_hash", "")),
                "source_file": str(frozen_path),
                "frozen_relative_path": frozen_path.relative_to(self.project_path).as_posix(),
                "evidence_mode": "project_scoped_frozen_copy",
                "origin_project_path": str(Path(str(view.get("project_path", ""))).resolve()),
                "origin_source_file": str(Path(str(view.get("source_file", ""))).resolve()),
                "origin_verified_at_create": True,
                "experiment_mode": str(config.get("mode") or "unknown") if isinstance(config, dict) else "unknown",
                "selected_scope": _selected_scope(config),
                "evaluation": _safe_context(canonical_view.get("evaluation", {})),
                "provenance": _safe_context(canonical_view.get("provenance", {})),
            }
            run_refs.append(run_ref)
            for metric in canonical_view.get("metrics", []):
                value = metric.get("value")
                if value is None:
                    continue
                try:
                    if not math.isfinite(float(value)):
                        continue
                except (TypeError, ValueError):
                    continue
                field_path = list(metric.get("field_path") or [])
                if not field_path:
                    continue
                displayed = _display_number(value)
                unit = str(metric.get("unit") or "")
                model = str(metric.get("model_id") or "model")
                metric_name = str(metric.get("metric") or metric.get("id") or "metric")
                n_rows = metric.get("n_rows")
                n_participants = metric.get("n_participants")
                claim_id = _stable_id(
                    "claim",
                    run_ref["run_id"],
                    run_ref["result_hash"],
                    field_path,
                    metric_name,
                    model,
                )
                text = self._t(lang, "metric_sentence").format(
                    model=model,
                    metric=metric_name,
                    value=displayed,
                    unit=f" {unit}" if unit else "",
                    n_rows=n_rows if n_rows is not None else self._t(lang, "unknown"),
                    n_participants=n_participants if n_participants is not None else self._t(lang, "unknown"),
                )
                numeric_claims.append(
                    {
                        "claim_id": claim_id,
                        "claim_type": "experiment_fact",
                        "text": text,
                        "rendered_value": displayed,
                        "evidence": {
                            "kind": "experiment",
                            "run_id": run_ref["run_id"],
                            "result_hash": run_ref["result_hash"],
                            "field_path": field_path,
                            "model_id": metric.get("model_id"),
                            "metric_id": metric.get("id"),
                            "metric": metric_name,
                            "value": value,
                            "unit": unit,
                            "n_rows": n_rows,
                            "n_participants": n_participants,
                            "variation": _safe_context(metric.get("variation")),
                        },
                        "review_state": "not_required_for_numeric_identity",
                    }
                )
        literature_claims = self._literature_claims(citations or [], lang)
        claims = numeric_claims + literature_claims
        report_id = self._new_report_id()
        sections = self._sections(template, lang, question, safe_context, claims, run_refs)
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "report_id": report_id,
            "version": 1,
            "parent_report_id": None,
            "template": template,
            "language": lang,
            "question": str(question),
            "context": safe_context,
            "created_at": _now(),
            "status": "draft",
            "generation": {
                "mode": "deterministic_template",
                "llm_used": False,
                "prompt_version": PROMPT_VERSION,
            },
            "run_refs": run_refs,
            "claims": claims,
            "sections": sections,
            "checklists": self._checklists(template, lang, safe_context, sections, claims),
            "automatic_check": {"state": "not_run", "errors": [], "warnings": [], "checked_at": None},
            "human_reviews": [],
        }
        self.save(report)
        return report

    def create_gradual(
        self,
        *,
        lang: str = "ko",
        audit_id: str | None = None,
        citations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a report from the frozen gradual_v1 evaluation and guideline audit."""
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError("unsupported_report_language")
        from .gradual_reporting import build_report

        literature_claims = self._literature_claims(citations or [], lang)
        report = build_report(self, lang, audit_id, literature_claims)
        self.save(report)
        return report

    def create_comparison(
        self,
        view: dict[str, Any],
        selection: dict[str, Any],
        *,
        lang: str = "ko",
        question: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a deterministic selected-model comparison bound to one verified run."""
        from .comparison_reporting import build_report

        literature_claims = self._literature_claims(citations or [], lang)
        report = build_report(self, view, selection, lang=lang, question=question,
                              literature_claims=literature_claims)
        self.save(report)
        return report

    def _freeze_run(self, record: dict[str, Any], result_hash: str) -> Path:
        if digest(record) != result_hash:
            raise ValueError("run_freeze_hash_mismatch")
        path = self.run_evidence_root / f"{result_hash}.json"
        encoded = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if digest(existing) != result_hash:
                raise ValueError("frozen_run_evidence_hash_mismatch")
            return path
        fd, temporary = tempfile.mkstemp(prefix=f".{result_hash}.", suffix=".tmp", dir=self.run_evidence_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                raise FileExistsError("frozen_run_evidence_collision")
            os.rename(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path

    def _literature_claims(self, citations: Iterable[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
        if not citations:
            return []
        if self.library is None:
            raise RuntimeError("evidence_library_required_for_citations")
        claims: list[dict[str, Any]] = []
        for item in citations:
            chunk_id = str(item.get("chunk_id", ""))
            search_id = str(item.get("search_id", ""))
            chunk = self.library.get_chunk(chunk_id)
            log = self.library.search_log(search_id)
            if not any(hit.get("chunk_id") == chunk_id for hit in log.get("hits", [])):
                raise ValueError("citation_not_in_search_record")
            requested = str(item.get("excerpt") or item.get("text") or "").strip()
            excerpt = requested if requested and requested in chunk["text"] else chunk["text"][:320].strip()
            if len(excerpt) > 500:
                excerpt = excerpt[:500].rstrip()
            title = str(chunk.get("title") or chunk["source_id"])
            locator = str(chunk.get("locator") or chunk.get("section") or "anchor")
            claim_id = _stable_id("claim", "literature", chunk_id, search_id, excerpt)
            claims.append(
                {
                    "claim_id": claim_id,
                    "claim_type": "literature_retrieval",
                    "text": self._t(lang, "literature_sentence").format(title=title, locator=locator),
                    "evidence": {
                        "kind": "literature",
                        "source_id": chunk["source_id"],
                        "source_version": str(chunk.get("version") or ""),
                        "source_sha256": str(chunk.get("original_sha256") or ""),
                        "chunk_id": chunk_id,
                        "chunk_sha256": str(chunk.get("text_sha256") or ""),
                        "anchor": {
                            "type": str(chunk.get("locator_type") or "anchor"),
                            "value": locator,
                            "section": str(chunk.get("section") or ""),
                        },
                        "excerpt": excerpt,
                        "search_id": search_id,
                    },
                    "review_state": "pending_human_review",
                    "semantic_support": "not_automatically_determined",
                }
            )
        return claims

    def _numeric_claim_text(self, evidence: dict[str, Any], lang: str) -> str:
        return self._t(lang, "metric_sentence").format(
            model=evidence.get("model_id") or "model",
            metric=evidence.get("metric") or evidence.get("metric_id") or "metric",
            value=_display_number(evidence.get("value")),
            unit=f" {evidence.get('unit')}" if evidence.get("unit") else "",
            n_rows=evidence.get("n_rows") if evidence.get("n_rows") is not None else self._t(lang, "unknown"),
            n_participants=(
                evidence.get("n_participants")
                if evidence.get("n_participants") is not None
                else self._t(lang, "unknown")
            ),
        )

    def _sections(
        self,
        template: str,
        lang: str,
        question: str,
        context: dict[str, Any],
        claims: list[dict[str, Any]],
        run_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        layouts = {
            "research": [
                "question", "data", "methods", "evaluation_design", "results", "uncertainty",
                "limitations", "reproducibility", "references",
            ],
            "industrial": [
                "purpose", "data", "evaluation_design", "acceptance", "results", "uncertainty",
                "limitations", "next_steps", "reproducibility", "references",
            ],
            "model_card": [
                "model_details", "intended_use", "unsupported_use", "data", "evaluation_design",
                "results", "limitations", "reproducibility", "references",
            ],
            "comparison": [
                "purpose", "comparison", "evaluation_design", "results", "uncertainty",
                "limitations", "next_steps", "reproducibility", "references",
            ],
        }
        result_claim_ids = [claim["claim_id"] for claim in claims if claim["claim_type"] == "experiment_fact"]
        reference_claim_ids = [claim["claim_id"] for claim in claims if claim["claim_type"] == "literature_retrieval"]
        evaluation_types = {
            str(
                ref.get("evaluation", {}).get("evaluation_type", ref.get("evaluation", {}).get("type", ""))
            ).lower()
            for ref in run_refs
            if isinstance(ref.get("evaluation"), dict)
        }
        no_external = not any(value in {"external", "external_validation"} for value in evaluation_types)
        no_prospective = not any("prospective" in value or "전향" in value for value in evaluation_types)
        has_synthetic = any(ref.get("evaluation", {}).get("synthetic") is True for ref in run_refs)

        def generated_purpose() -> str:
            supplied = context.get("purpose") or context.get("intended_use")
            if supplied:
                return str(supplied)
            if lang == "ko":
                return "선택한 실행의 성능과 근거를 정리하는 탐색적 모델 실험 보고서이며, 독립 검증이 아닙니다."
            return "An exploratory model experiment report summarizing the selected run's performance and evidence; this is not independent validation."

        def origin_label(ref: dict[str, Any]) -> str:
            synthetic = ref.get("evaluation", {}).get("synthetic")
            if lang == "ko":
                return "합성 자료" if synthetic is True else ("실제 관측 자료(실행 설정에서 선언됨)" if synthetic is False else "자료 출처 미확인")
            return "synthetic data" if synthetic is True else ("observed data (declared by the run configuration)" if synthetic is False else "data origin not declared")

        def scope_text(ref: dict[str, Any]) -> str:
            scope = ref.get("selected_scope") or {}
            if not scope:
                run_id = str(ref.get("run_id") or "unknown")
                return (
                    f"run={run_id}: 선택 범위 메타데이터가 실행 레코드에 없습니다."
                    if lang == "ko" else
                    f"run={run_id}: selected-scope metadata is absent from the run record."
                )
            fields = "; ".join(f"{key}={scope[key]}" for key in _SELECTED_SCOPE_FIELDS if key in scope)
            if scope.get("actual_cohort_participants"):
                counts = ", ".join(f"{label}:{count}" for label, count in scope["actual_cohort_participants"].items())
                fields += f"; actual_cohort_participants={counts}"
            prefix = "검증된 선택 범위" if lang == "ko" else "Verified selected scope"
            return f"{prefix}: {fields}"

        def generated_data() -> list[str]:
            intro = (
                "각 자료 범위와 출처 표시는 검증된 실행 설정과 동결된 실행 증거에서 가져왔습니다."
                if lang == "ko" else
                "Each data scope and origin label is taken from the verified run configuration and frozen run evidence."
            )
            lines = [str(context.get("data_description") or intro)]
            for ref in run_refs:
                lines.append(
                    f"run={ref['run_id']}; origin={origin_label(ref)}; data_hash={ref['data_hash']}; {scope_text(ref)}"
                )
            return lines

        def generated_methods() -> list[str]:
            supplied = context.get("methods")
            if supplied:
                return [str(supplied)]
            lines = []
            for ref in run_refs:
                model_ids = sorted({
                    str(claim.get("evidence", {}).get("model_id") or "model")
                    for claim in claims
                    if claim.get("evidence", {}).get("kind") == "experiment"
                    and claim.get("evidence", {}).get("run_id") == ref.get("run_id")
                })
                if lang == "ko":
                    lines.append(
                        f"run={ref['run_id']}의 동결된 결과에서 지표를 직접 읽었습니다. "
                        f"experiment_mode={ref.get('experiment_mode') or 'unknown'}; engine={ref.get('engine')}; "
                        f"models={','.join(model_ids) or 'unknown'}. 로컬 근거 검색과 결정적 서식을 사용했으며 LLM은 사용하지 않았습니다."
                    )
                else:
                    lines.append(
                        f"Metrics were read directly from the frozen result for run={ref['run_id']}. "
                        f"experiment_mode={ref.get('experiment_mode') or 'unknown'}; engine={ref.get('engine')}; "
                        f"models={','.join(model_ids) or 'unknown'}. Local evidence retrieval and a deterministic template were used; no LLM was used."
                    )
            return lines

        def generated_uncertainty() -> list[str]:
            supplied = context.get("uncertainty_method")
            if supplied:
                return [str(supplied)]
            variations = [
                claim.get("evidence", {}).get("variation")
                for claim in claims
                if claim.get("evidence", {}).get("kind") == "experiment"
                and isinstance(claim.get("evidence", {}).get("variation"), dict)
            ]
            kinds = sorted({str(value.get("kind") or "unspecified") for value in variations})
            if kinds:
                label = ", ".join(kinds)
                return [
                    (f"실행 결과에 기록된 불확실성 요약만 보고합니다: {label}. 독립 검증 불확실성을 뜻하지 않습니다."
                     if lang == "ko" else
                     f"Only uncertainty summaries recorded in the run are reported: {label}. They do not represent independent-validation uncertainty.")
                ]
            return [
                ("검증된 실행 결과에 불확실성 구간이 없어 점추정치만 보고합니다. 신뢰구간 또는 재표본화 평가는 별도로 필요합니다."
                 if lang == "ko" else
                 "The verified run contains no uncertainty interval, so only point estimates are reported. Confidence-interval or resampling analysis remains necessary.")
            ]

        def generated_next_steps() -> str:
            supplied = context.get("next_steps")
            if supplied:
                return str(supplied)
            if lang == "ko":
                return "현재 보고서에는 독립 외부·전향 검증 증거가 없습니다. 확보 가능한 외부 자료로 별도 검증하고 불확실성을 평가해야 합니다."
            return "This report contains no independent external or prospective validation evidence. Separate validation on available external data and uncertainty assessment remain necessary."

        sections: list[dict[str, Any]] = []
        for section_id in layouts[template]:
            paragraphs: list[str] = []
            claim_ids: list[str] = []
            if section_id == "question":
                paragraphs = [question or generated_purpose()]
            elif section_id in {"purpose", "intended_use"}:
                paragraphs = [generated_purpose()]
            elif section_id == "unsupported_use":
                paragraphs = [str(context.get("unsupported_use") or self._t(lang, "not_provided"))]
            elif section_id == "data":
                paragraphs = generated_data()
            elif section_id == "methods":
                paragraphs = generated_methods()
            elif section_id in {"evaluation_design", "model_details", "comparison"}:
                paragraphs = []
                for ref in run_refs:
                    evaluation = ref.get("evaluation", {})
                    scope_ids = evaluation.get("scope_ids") or []
                    evaluation_type = evaluation.get("evaluation_type")
                    evaluation_label = self.locales[lang].get(
                        f"evaluation_{evaluation_type}", evaluation_type or self._t(lang, "unknown")
                    )
                    calculation = evaluation.get("calculation")
                    calculation_label = self.locales[lang].get(
                        f"calculation_{calculation}", calculation or self._t(lang, "unknown")
                    )
                    synthetic = evaluation.get("synthetic")
                    synthetic_label = self._t(lang, "yes" if synthetic is True else "no") if synthetic is not None else self._t(lang, "unknown")
                    fields = [
                        f"run={ref['run_id']}",
                        f"task={evaluation.get('task') or 'unknown'}",
                        f"target={evaluation.get('target') or 'unknown'}",
                        f"evaluation_type={evaluation_label}",
                        f"synthetic={synthetic_label}",
                        f"calculation={calculation_label}",
                        f"population={evaluation.get('population') or 'unknown'}",
                        f"split_id={evaluation.get('split_id') or 'unknown'}",
                        f"scope_hash={evaluation.get('scope_hash') or 'unknown'}",
                    ]
                    if scope_ids:
                        fields.append(f"evaluation_scope_n={len(scope_ids)}")
                    elif evaluation.get("scope_basis") == "verified_same_run_aggregate_test_scope":
                        fields.extend([
                            "evaluation_scope_identifiers=not_exported",
                            f"aggregate_n_nights={evaluation.get('aggregate_n_nights') or 'unknown'}",
                            f"aggregate_n_participants={evaluation.get('aggregate_n_participants') or 'unknown'}",
                        ])
                    paragraphs.append("; ".join(fields))
            elif section_id == "acceptance":
                criterion = context.get("acceptance_criteria")
                paragraphs = [
                    str(criterion)
                    if criterion and context.get("acceptance_predeclared") is True
                    else self._t(lang, "not_provided")
                ]
            elif section_id == "results":
                claim_ids = result_claim_ids
                supplied = context.get("results_summary")
                if isinstance(supplied, list):
                    paragraphs = [str(item) for item in supplied]
                elif supplied:
                    paragraphs = [str(supplied)]
            elif section_id == "uncertainty":
                paragraphs = generated_uncertainty()
            elif section_id == "limitations":
                if has_synthetic:
                    paragraphs.append(self._t(lang, "synthetic_limitation"))
                if no_external:
                    paragraphs.append(self._t(lang, "no_external_validation"))
                if no_prospective:
                    paragraphs.append(self._t(lang, "no_prospective_validation"))
                paragraphs.append(self._t(lang, "interpretation_pending"))
                supplied = context.get("limitations")
                if supplied:
                    paragraphs.append(str(supplied))
            elif section_id == "next_steps":
                paragraphs = [generated_next_steps()]
            elif section_id == "reproducibility":
                paragraphs = [
                    ", ".join(f"{ref['run_id']}:{ref['result_hash']}" for ref in run_refs),
                    f"schema={REPORT_SCHEMA}; prompt={PROMPT_VERSION}; llm=false",
                ]
            elif section_id == "references":
                claim_ids = reference_claim_ids
            sections.append(
                {
                    "section_id": section_id,
                    "title": self._t(lang, section_id),
                    "paragraphs": paragraphs,
                    "claim_ids": claim_ids,
                }
            )
        return sections

    def _checklists(
        self,
        template: str,
        lang: str,
        context: dict[str, Any],
        sections: list[dict[str, Any]],
        claims: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        section_ids = {section["section_id"] for section in sections}
        numeric_ids = [claim["claim_id"] for claim in claims if claim.get("claim_type") == "experiment_fact"]
        literature_ids = [claim["claim_id"] for claim in claims if claim.get("claim_type") == "literature_retrieval"]
        nist = [
            ("nist_govern", "purpose", []),
            ("nist_map", "data", literature_ids),
            ("nist_measure", "results", numeric_ids),
            ("nist_manage", "limitations", []),
        ]
        cards = [
            ("card_details", "model_details", []),
            ("card_intended_use", "intended_use", []),
            ("card_factors", "data", []),
            ("card_metrics", "results", numeric_ids),
            ("card_data", "data", []),
            ("card_caveats", "limitations", literature_ids),
        ]
        requested = context.get("checklists") or {}
        if isinstance(requested, list):
            requested = {str(item.get("item_id")): item for item in requested if isinstance(item, dict)}
        if not isinstance(requested, dict):
            requested = {}

        def framework(identifier: str, name: str, reference: str, definitions: list[tuple[str, str, list[str]]]):
            items = []
            for item_id, suggested_section, suggested_claims in definitions:
                supplied = requested.get(item_id) if isinstance(requested.get(item_id), dict) else {}
                status = str(supplied.get("status") or "judgment_pending")
                if status not in CHECKLIST_STATUSES:
                    raise ValueError(f"unsupported_checklist_status:{status}")
                linked = supplied.get("evidence_claim_ids", suggested_claims)
                linked = [str(item) for item in linked] if isinstance(linked, list) else []
                report_section = str(supplied.get("report_section") or suggested_section)
                if report_section not in section_ids:
                    report_section = next(iter(section_ids), "")
                items.append(
                    {
                        "item_id": item_id,
                        "title": self._t(lang, item_id),
                        "status": status,
                        "report_section": report_section,
                        "evidence_claim_ids": linked,
                        "note": str(supplied.get("note") or ""),
                        "assessed_by": "user" if supplied else "not_assessed",
                    }
                )
            return {"framework_id": identifier, "framework": name, "reference": reference, "items": items}

        result = [
            framework(
                "nist_ai_rmf_1_0", "NIST AI RMF 1.0", "NIST.AI.100-1",
                nist,
            )
        ]
        if template == "model_card":
            result.append(
                framework(
                    "model_cards", "Model Cards for Model Reporting", "arXiv:1810.03993",
                    cards,
                )
            )
        if context.get("medical_prediction") is True:
            result.append(
                framework(
                    "tripod_ai_applicability",
                    "TRIPOD+AI applicability review",
                    "BMJ 2024;385:e078378 (official items not embedded)",
                    [("tripod_applicability", "limitations", literature_ids)],
                )
            )
        return result

    def _view_for_ref(self, run_ref: dict[str, Any]) -> dict[str, Any]:
        relative = Path(str(run_ref.get("frozen_relative_path") or ""))
        frozen = (self.project_path / relative).resolve()
        if not frozen.is_relative_to(self.run_evidence_root.resolve()):
            raise ValueError("frozen_run_evidence_outside_project")
        return {
            "project_path": str(self.project_path),
            "source_file": str(frozen),
            "result_hash": run_ref["result_hash"],
        }

    def check(self, report: dict[str, Any]) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if report.get("artifact_sha256") and report.get("artifact_sha256") != _artifact_hash(report):
            errors.append({"code": "report_artifact_hash_mismatch", "claim_id": None})
        if report.get("context", {}).get("acceptance_criteria") and not report.get("context", {}).get(
            "acceptance_predeclared"
        ):
            warnings.append({"code": "acceptance_criteria_not_marked_predeclared", "claim_id": None})
        if report.get("template") == "gradual_guidelines":
            from .gradual_reporting import validate_report

            errors.extend(validate_report(report, self.project_path))
            warnings.append({"code": "assessor_review_required_not_certification", "claim_id": None})
        if report.get("template") == "model_comparison":
            from .comparison_reporting import validate_report

            errors.extend(validate_report(self, report))
            warnings.append({"code": "comparison_is_point_estimate_not_superiority_test", "claim_id": None})
            warnings.append({"code": "probast_final_assessor_review_required", "claim_id": None})
        if "ai_analysis" in report:
            from .report_agents import validate_ai_analysis

            errors.extend(validate_ai_analysis(self, report))
            warnings.append({"code": "ai_interpretation_requires_human_review", "claim_id": None})
            if report.get("ai_analysis", {}).get("test_only"):
                warnings.append({"code": "mock_ai_output_not_live_generation", "claim_id": None})
        elif (report.get("generation") or {}).get("mode") == "llm_rag_agents":
            errors.append({"code": "ai_analysis_missing", "claim_id": None})
        claim_ids = {str(item.get("claim_id")) for item in report.get("claims", [])}
        for framework in report.get("checklists", []):
            for item in framework.get("items", []):
                if item.get("status") not in CHECKLIST_STATUSES:
                    errors.append({"code": "invalid_checklist_status", "claim_id": item.get("item_id")})
                missing = [value for value in item.get("evidence_claim_ids", []) if value not in claim_ids]
                if missing:
                    errors.append(
                        {"code": "checklist_evidence_link_invalid", "claim_id": item.get("item_id"), "detail": missing}
                    )
                if item.get("status") == "judgment_pending":
                    warnings.append({"code": "checklist_judgment_pending", "claim_id": item.get("item_id")})
        run_refs = {str(item.get("run_id")): item for item in report.get("run_refs", [])}
        checked_run_metadata: set[str] = set()
        for section in report.get("sections", []):
            if section.get("edited_by_user"):
                # Free prose is deliberately not promoted to a verified fact.  A human
                # review record documents review activity but cannot bind arbitrary
                # numbers in prose to a structured run field or source locator.
                errors.append(
                    {
                        "code": "edited_section_requires_structured_revalidation",
                        "claim_id": None,
                        "section_id": section.get("section_id"),
                    }
                )
        for claim in report.get("claims", []):
            evidence = claim.get("evidence", {})
            claim_id = claim.get("claim_id")
            if evidence.get("kind") == "experiment":
                run_ref = run_refs.get(str(evidence.get("run_id")))
                if run_ref is None:
                    errors.append({"code": "run_reference_missing", "claim_id": claim_id})
                    continue
                if evidence.get("result_hash") != run_ref.get("result_hash"):
                    errors.append({"code": "claim_run_hash_mismatch", "claim_id": claim_id})
                    continue
                try:
                    original = verify_run(self._view_for_ref(run_ref))
                    actual = resolve_field(original, list(evidence.get("field_path") or []))
                except Exception as exc:  # evidence errors must become audit findings
                    errors.append({"code": "experiment_evidence_invalid", "claim_id": claim_id, "detail": str(exc)})
                    continue
                origin_source = Path(str(run_ref.get("origin_source_file") or ""))
                origin_project = Path(str(run_ref.get("origin_project_path") or ""))
                if origin_source.is_file() and origin_project.is_dir():
                    try:
                        verify_run(
                            {
                                "project_path": str(origin_project),
                                "source_file": str(origin_source),
                                "result_hash": run_ref["result_hash"],
                            }
                        )
                    except Exception as exc:
                        errors.append({"code": "origin_run_changed", "claim_id": claim_id, "detail": str(exc)})
                else:
                    warnings.append({"code": "origin_run_unavailable_using_frozen_copy", "claim_id": claim_id})
                canonical = adapt_run(original, str(run_ref.get("engine")), str(self.project_path))
                run_id = str(run_ref.get("run_id"))
                if run_id not in checked_run_metadata:
                    checked_run_metadata.add(run_id)
                    expected_scope = _selected_scope(canonical.get("config", {}))
                    if run_ref.get("selected_scope") != expected_scope:
                        errors.append({
                            "code": "selected_scope_mismatch", "claim_id": claim_id,
                            "expected": expected_scope, "reported": run_ref.get("selected_scope"),
                        })
                    config = canonical.get("config", {})
                    expected_mode = str(config.get("mode") or "unknown") if isinstance(config, dict) else "unknown"
                    if run_ref.get("experiment_mode") != expected_mode:
                        errors.append({
                            "code": "experiment_mode_mismatch", "claim_id": claim_id,
                            "expected": expected_mode, "reported": run_ref.get("experiment_mode"),
                        })
                canonical_metric = next(
                    (
                        item
                        for item in canonical.get("metrics", [])
                        if list(item.get("field_path") or []) == list(evidence.get("field_path") or [])
                    ),
                    None,
                )
                if canonical_metric is None:
                    errors.append({"code": "canonical_metric_not_found", "claim_id": claim_id})
                    continue
                for key in ("model_id", "metric", "value", "unit", "n_rows", "n_participants"):
                    if evidence.get(key) != canonical_metric.get(key):
                        errors.append(
                            {
                                "code": f"canonical_metric_{key}_mismatch",
                                "claim_id": claim_id,
                                "expected": canonical_metric.get(key),
                                "reported": evidence.get(key),
                            }
                        )
                expected = evidence.get("value")
                if actual != expected:
                    try:
                        same = math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-12)
                    except (TypeError, ValueError):
                        same = False
                    if not same:
                        errors.append(
                            {"code": "numeric_value_mismatch", "claim_id": claim_id, "expected": actual, "reported": expected}
                        )
                rendered = _display_number(actual)
                if rendered not in str(claim.get("text", "")) or claim.get("rendered_value") != rendered:
                    errors.append({"code": "numeric_text_mismatch", "claim_id": claim_id})
                canonical_text = self._numeric_claim_text({**evidence, "value": actual}, report.get("language", "ko"))
                number_pattern = r"(?<![A-Za-z_])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
                if re.findall(number_pattern, str(claim.get("text", ""))) != re.findall(number_pattern, canonical_text):
                    errors.append({"code": "unexpected_numeric_text", "claim_id": claim_id})
            elif evidence.get("kind") == "literature":
                if self.library is None:
                    errors.append({"code": "evidence_library_unavailable", "claim_id": claim_id})
                    continue
                try:
                    chunk = self.library.get_chunk(str(evidence.get("chunk_id", "")))
                    source = self.library.get_source(str(evidence.get("source_id", "")))
                    log = self.library.search_log(str(evidence.get("search_id", "")))
                except Exception as exc:
                    errors.append({"code": "literature_evidence_invalid", "claim_id": claim_id, "detail": str(exc)})
                    continue
                checks = [
                    (chunk.get("source_id") == evidence.get("source_id"), "citation_source_mismatch"),
                    (str(chunk.get("version") or "") == str(evidence.get("source_version") or ""), "citation_version_mismatch"),
                    (str(chunk.get("text_sha256") or "") == str(evidence.get("chunk_sha256") or ""), "citation_chunk_hash_mismatch"),
                    (str(source.get("original_sha256") or "") == str(evidence.get("source_sha256") or ""), "citation_source_hash_mismatch"),
                    (str(evidence.get("excerpt") or "") in str(chunk.get("text") or ""), "citation_excerpt_not_found"),
                    (
                        any(hit.get("chunk_id") == evidence.get("chunk_id") for hit in log.get("hits", [])),
                        "citation_not_in_search_record",
                    ),
                ]
                for valid, code in checks:
                    if not valid:
                        errors.append({"code": code, "claim_id": claim_id})
                warnings.append({"code": "semantic_support_pending_human_review", "claim_id": claim_id})
            elif evidence.get("kind") == "gradual_metric" and report.get("template") == "gradual_guidelines":
                # Full field, text and source-snapshot identity are regenerated and
                # compared by gradual_reporting.validate_report above.
                continue
            elif evidence.get("kind") == "comparison_metric" and report.get("template") == "model_comparison":
                # The full comparison snapshot, selection, numeric claims and prose are
                # regenerated from the frozen run by comparison_reporting.validate_report.
                continue
            else:
                errors.append({"code": "unknown_claim_evidence_kind", "claim_id": claim_id})
        state = "automatic_check_passed" if not errors else "automatic_check_failed"
        return {"state": state, "errors": errors, "warnings": warnings, "checked_at": _now()}

    def _derive(self, source: dict[str, Any]) -> dict[str, Any]:
        derived = copy.deepcopy(source)
        derived.pop("artifact_sha256", None)
        derived["report_id"] = self._new_report_id()
        derived["version"] = int(source.get("version", 1)) + 1
        derived["parent_report_id"] = source["report_id"]
        derived["created_at"] = _now()
        return derived

    def revise(self, report_id: str, edits: dict[str, Any]) -> dict[str, Any]:
        source = self.get(report_id)
        revised = self._derive(source)
        claim_edits = edits.get("claims", {}) if isinstance(edits, dict) else {}
        section_edits = edits.get("sections", {}) if isinstance(edits, dict) else {}
        checklist_edits = edits.get("checklists", {}) if isinstance(edits, dict) else {}
        for claim in revised.get("claims", []):
            if claim["claim_id"] in claim_edits:
                claim["text"] = str(claim_edits[claim["claim_id"]])
                claim["edited_by_user"] = True
                claim["review_state"] = "pending_recheck"
        for section in revised.get("sections", []):
            if section["section_id"] in section_edits:
                value = section_edits[section["section_id"]]
                section["paragraphs"] = [str(item) for item in value] if isinstance(value, list) else [str(value)]
                section["edited_by_user"] = True
        for framework in revised.get("checklists", []):
            for item in framework.get("items", []):
                update = checklist_edits.get(item["item_id"])
                if not isinstance(update, dict):
                    continue
                status = str(update.get("status", item["status"]))
                if status not in CHECKLIST_STATUSES:
                    raise ValueError(f"unsupported_checklist_status:{status}")
                item["status"] = status
                if "note" in update:
                    item["note"] = str(update["note"])
                if "report_section" in update:
                    item["report_section"] = str(update["report_section"])
                if "evidence_claim_ids" in update:
                    item["evidence_claim_ids"] = [str(value) for value in update["evidence_claim_ids"]]
                item["assessed_by"] = "user"
        revised["status"] = "draft"
        revised["automatic_check"] = {"state": "not_run", "errors": [], "warnings": [], "checked_at": None}
        revised["human_reviews"] = []
        self.save(revised)
        return revised

    def edit(self, report_id: str, edits: dict[str, Any]) -> dict[str, Any]:
        return self.revise(report_id, edits)

    def apply_check(self, report_id: str) -> dict[str, Any]:
        source = self.get(report_id)
        checked = self._derive(source)
        checked["automatic_check"] = self.check(checked)
        checked["status"] = checked["automatic_check"]["state"]
        self.save(checked)
        return checked

    def attach_ai_analysis(self, report_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        """Create a checked descendant without changing any experiment evidence."""
        source = self.get(report_id)
        if self.check(source)["errors"]:
            raise ValueError("ai_base_report_check_failed")
        if source.get("ai_analysis"):
            raise ValueError("ai_analysis_already_attached")
        derived = self._derive(source)
        derived["ai_analysis"] = copy.deepcopy(analysis)
        derived.setdefault('context', {}).update(
            llm_used=analysis.get('actual_llm') is True,
            core_sections_scope='immutable_deterministic_base_before_ai_analysis')
        derived["generation"] = {
            "mode": "llm_rag_agents", "llm_used": analysis.get("actual_llm") is True,
            "provider": analysis.get("provider"), "model": analysis.get("model"),
            "test_only": analysis.get("test_only") is True, "prompt_version": "report-agents-v1",
        }
        derived["automatic_check"] = self.check(derived)
        if derived["automatic_check"]["errors"]:
            codes = sorted({item["code"] for item in derived["automatic_check"]["errors"]})
            raise ValueError("ai_report_check_failed:" + ",".join(codes))
        derived["status"] = derived["automatic_check"]["state"]
        derived["human_reviews"] = []
        self.save(derived)
        return derived

    def record_review(self, report_id: str, reviewer: str, note: str) -> dict[str, Any]:
        if not reviewer.strip():
            raise ValueError("reviewer_required")
        source = self.get(report_id)
        reviewed = self._derive(source)
        reviewed.setdefault("human_reviews", []).append(
            {"reviewer": reviewer.strip(), "note": note.strip(), "recorded_at": _now(), "certification": False}
        )
        reviewed["status"] = "user_review_recorded"
        self.save(reviewed)
        return reviewed

    def review(self, report_id: str, reviewer: str, note: str) -> dict[str, Any]:
        return self.record_review(report_id, reviewer, note)

    def translate(self, report_id: str, lang: str) -> dict[str, Any]:
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError("unsupported_report_language")
        source = self.get(report_id)
        if source.get("ai_analysis"):
            raise ValueError("ai_report_translation_requires_regeneration")
        translated = self._derive(source)
        translated["language"] = lang
        if translated.get("template") == "gradual_guidelines":
            from .gradual_reporting import build_content

            for claim in translated.get("claims", []):
                evidence = claim.get("evidence", {})
                if evidence.get("kind") == "literature":
                    title = evidence.get("source_id", "source")
                    locator = evidence.get("anchor", {}).get("value", "anchor")
                    if self.library is not None:
                        try:
                            title = self.library.get_source(evidence["source_id"])["title"]
                        except Exception:
                            pass
                    claim["text"] = self._t(lang, "literature_sentence").format(title=title, locator=locator)
            audit = translated["guideline_evidence"]["audit_snapshot"]
            literature = [item for item in translated.get("claims", []) if item.get("evidence", {}).get("kind") == "literature"]
            translated.update(build_content(audit, lang, literature))
            translated["status"] = "draft"
            translated["automatic_check"] = {"state": "not_run", "errors": [], "warnings": [], "checked_at": None}
            translated["human_reviews"] = []
            translated["translation_of"] = source["report_id"]
            self.save(translated)
            return translated
        if translated.get("template") == "model_comparison":
            from .comparison_reporting import rebuild_content

            for claim in translated.get("claims", []):
                evidence = claim.get("evidence", {})
                if evidence.get("kind") == "literature":
                    title = evidence.get("source_id", "source")
                    locator = evidence.get("anchor", {}).get("value", "anchor")
                    if self.library is not None:
                        try:
                            title = self.library.get_source(evidence["source_id"])["title"]
                        except Exception:
                            pass
                    claim["text"] = self._t(lang, "literature_sentence").format(title=title, locator=locator)
            translated.update(rebuild_content(self, translated, lang))
            translated["status"] = "draft"
            translated["automatic_check"] = {"state": "not_run", "errors": [], "warnings": [], "checked_at": None}
            translated["human_reviews"] = []
            translated["translation_of"] = source["report_id"]
            self.save(translated)
            return translated
        run_refs = translated.get("run_refs", [])
        for claim in translated.get("claims", []):
            evidence = claim.get("evidence", {})
            if claim.get("edited_by_user"):
                claim["source_language_text"] = claim.get("text", "")
                claim["review_state"] = "pending_recheck"
            elif evidence.get("kind") == "experiment":
                claim["text"] = self._numeric_claim_text(evidence, lang)
            elif evidence.get("kind") == "literature":
                title = evidence.get("source_id", "source")
                locator = evidence.get("anchor", {}).get("value", "anchor")
                if self.library is not None:
                    try:
                        title = self.library.get_source(evidence["source_id"])["title"]
                    except Exception:
                        pass
                claim["text"] = self._t(lang, "literature_sentence").format(title=title, locator=locator)
        generated_sections = self._sections(
            translated["template"], lang, translated.get("question", ""), translated.get("context", {}),
            translated.get("claims", []), run_refs,
        )
        original_sections = {item.get("section_id"): item for item in translated.get("sections", [])}
        for section in generated_sections:
            original = original_sections.get(section.get("section_id"), {})
            if original.get("edited_by_user"):
                section["paragraphs"] = copy.deepcopy(original.get("paragraphs", []))
                section["edited_by_user"] = True
                section["source_language"] = source.get("language")
        translated["sections"] = generated_sections
        for framework in translated.get("checklists", []):
            for item in framework.get("items", []):
                item["title"] = self._t(lang, item["item_id"])
        translated["status"] = "draft"
        translated["automatic_check"] = {"state": "not_run", "errors": [], "warnings": [], "checked_at": None}
        translated["human_reviews"] = []
        translated["translation_of"] = source["report_id"]
        self.save(translated)
        return translated

    def new_language(self, report_id: str, lang: str) -> dict[str, Any]:
        return self.translate(report_id, lang)

    def export(
        self,
        report_id: str,
        output_dir: str | Path,
        *,
        formats: Iterable[str] = ("md", "docx", "pdf", "json", "bib", "csv", "figures"),
        mode: str = "draft",
    ) -> dict[str, Path]:
        from .report_export import export_report_package

        report = self.get(report_id)
        result = self.check(report)
        if mode not in {"draft", "checked"}:
            raise ValueError("export_mode_must_be_draft_or_checked")
        if mode == "checked" and result["errors"]:
            raise ValueError("blocking_report_check_errors")
        return export_report_package(
            self,
            report,
            Path(output_dir),
            formats=set(formats),
            mode=mode,
            check_result=result,
        )

    def export_pdf(
        self,
        report_id: str,
        destination_file: str | Path,
        *,
        mode: str = "draft",
    ) -> Path:
        """Export one printable PDF while retaining the regular report checks."""
        destination = Path(destination_file).expanduser().resolve()
        if destination.suffix.lower() != ".pdf":
            raise ValueError("report_destination_must_be_pdf")
        if destination.exists():
            raise FileExistsError("report_pdf_already_exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="researchbench-report-pdf-") as temporary:
            paths = self.export(report_id, temporary, formats=("pdf",), mode=mode)
            source = paths.get("pdf")
            if source is None or not source.is_file():
                raise RuntimeError("report_pdf_export_missing")
            with destination.open("xb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
        return destination


__all__ = ["PROMPT_VERSION", "REPORT_SCHEMA", "ReportService"]
