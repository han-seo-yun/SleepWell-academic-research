from __future__ import annotations

import csv
import copy
import hashlib
import html
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path, PureWindowsPath
from typing import Any

from . import __version__ as APP_VERSION
from .reporting import PROMPT_VERSION, REPORT_SCHEMA
from .result_adapter import adapt_run, model_color, verify_run


_PDF_LOCK = threading.RLock()
_FIGURE_LOCK = threading.RLock()


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "report"


def _claim_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {claim["claim_id"]: claim for claim in report.get("claims", [])}


def _report_sections(report: dict[str, Any]) -> list[dict[str, Any]]:
    sections = copy.deepcopy(report.get("sections", []))
    if report.get("ai_analysis"):
        from .report_agents import render_ai_sections

        # Original sections are an immutable pre-AI evidence snapshot. Qualify
        # their generation statement for the combined, subsequently annotated PDF.
        for section in sections:
            if section.get('section_id') == 'reproducibility':
                section['paragraphs'] = [
                    ('기본 성능표·비교 해석은 공식 지침 로컬 검색과 고정 서식으로 작성했다. '
                     '추가 AI 해석의 실제/모의 실행 여부는 앞의 AI 분석 실행 정보에 별도로 기록했다.')
                    if paragraph == '공식 지침 로컬 검색과 고정 서식으로 작성했으며 생성형 LLM·외부 AI 전송은 사용하지 않았다.'
                    else str(paragraph).replace('deterministic template; llm=false.',
                        'deterministic base template before AI analysis; see AI analysis provenance for the appended interpretation.')
                    for paragraph in section.get('paragraphs', [])]
        sections = render_ai_sections(report) + sections
    audit = report.get("guideline_evidence", {}).get("audit_snapshot")
    if not audit:
        return sections
    used = {key for framework in report.get("checklists", []) for item in framework["items"]
            for key in item.get("source_evidence_ids", [])}
    paragraphs = []
    for source in audit.get("sources", []):
        if source["id"] not in used:
            continue
        location = source.get("source_path") or source.get("snapshot_path") or source.get("source_file") or ""
        filename = PureWindowsPath(location).name if location else ""
        line = f":{source['source_line']}" if source.get("source_line") else ""
        digest = source.get("sha256") or source.get("snapshot_sha256") or ""
        paragraphs.append(f"{source['id']} — {source.get('title', '')}; {filename}{line}; SHA-256 {digest}")
    sections.append({"section_id": "source_index", "title": "점검 근거 문서 색인" if report["language"] == "ko"
                     else "Checklist source index", "paragraphs": paragraphs, "claim_ids": []})
    return sections


def _item_evidence_links(item: dict[str, Any]) -> str:
    return ", ".join(item.get("evidence_claim_ids") or item.get("source_evidence_ids") or []) or "—"


def _status(service: Any, lang: str, value: str) -> str:
    return service._t(lang, f"status_{value}")


def _item_status(service: Any, lang: str, item: dict[str, Any]) -> str:
    if item.get("source_answer"):
        return ("신호질문 응답 " if lang == "ko" else "Signaling-question response ") + item["source_answer"]
    if item.get("source_status") == "not_reported":
        return "미보고" if lang == "ko" else "Not reported"
    return _status(service, lang, item["status"])


def _check_state(service: Any, lang: str, value: str) -> str:
    return service.locales.get(lang, {}).get(f"state_{value}", value)


def _metric_rows(report: dict[str, Any]) -> list[list[str]]:
    rows = []
    for claim in report.get("claims", []):
        evidence = claim.get("evidence", {})
        if evidence.get("kind") not in {"experiment", "gradual_metric", "comparison_metric"}:
            continue
        if evidence.get("kind") == "gradual_metric":
            evidence = dict(evidence, model_id=".".join(str(evidence.get(key, ""))
                for key in ("cohort", "policy", "arm", "weighting")))
        value = evidence.get("value")
        if isinstance(value, (int, float)):
            value = format(float(value), ".6g")
        rows.append([str(evidence.get(key, "")) for key in ("model_id", "metric")] + [str(value), str(evidence.get("unit", "")), str(evidence.get("n_rows", "")), str(evidence.get("n_participants", ""))])
    return rows


def _redacted_report(report: dict[str, Any]) -> dict[str, Any]:
    """Remove evaluation row/participant identifiers from an export copy."""
    result = copy.deepcopy(report)
    for run_ref in result.get("run_refs", []):
        evaluation = run_ref.get("evaluation", {})
        scope_ids = evaluation.pop("scope_ids", None)
        if isinstance(scope_ids, list):
            evaluation["scope_id_count"] = len(scope_ids)
            evaluation["scope_ids_exported"] = False
    if result.get("template") == "model_comparison":
        evidence = result.get("comparison_evidence")
        if isinstance(evidence, dict):
            # Comparison reports are aggregate-only.  Defensively discard any
            # future/internal row payload before producing the shareable audit.
            for key in ("predictions", "participant_rows", "scope_ids", "participant_ids", "raw_rows"):
                evidence.pop(key, None)
            summary = evidence.get("summary")
            if isinstance(summary, dict):
                for key in ("predictions", "participant_rows", "scope_ids", "participant_ids", "raw_rows"):
                    summary.pop(key, None)
                summary["raw_prediction_rows_included"] = False
                summary["participant_identifiers_included"] = False
    if result.get("template") in {"gradual_guidelines", "model_comparison"}:
        # Keep the verifiable original in the project. The shared copy carries
        # content hashes and source basenames without local account/drive paths.
        def redact_paths(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: redact_paths(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact_paths(item) for item in value]
            if isinstance(value, str) and re.match(r"^[A-Za-z]:[\\/]", value):
                return "[local source]/" + PureWindowsPath(value).name
            if isinstance(value, str) and value.startswith("/"):
                return "[local source]/" + Path(value).name
            return value
        result = redact_paths(result)
        if result.get("template") == "gradual_guidelines":
            result["source_artifact_sha256"] = result.pop("artifact_sha256", None)
        result["export_view"] = "redacted_audit_view_not_reimportable"
        result["local_source_paths_included"] = False
        result["export_content_sha256"] = hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
    return result


def _generation_code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    names = ("reporting.py", "report_export.py", "result_adapter.py", "evidence_library.py",
             "gradual_reporting.py", "guideline_retrieval.py", "comparison_reporting.py",
             "comparison_metrics.py", "comparison_plotting.py", "report_agents.py", "ai_connections.py")
    return {name: _sha256(root / name) for name in names if (root / name).is_file()}


def _warning_lines(service: Any, report: dict[str, Any], mode: str, check_result: dict[str, Any]) -> list[str]:
    lang = report["language"]
    lines: list[str] = []
    if mode == "draft":
        lines.append(service._t(lang, "draft_warning"))
    if check_result.get("warnings") or check_result.get("errors"):
        lines.append(service._t(lang, "check_warning"))
    return lines


def render_markdown(
    service: Any,
    report: dict[str, Any],
    mode: str,
    check_result: dict[str, Any],
    figure_paths: list[Path] | None = None,
) -> str:
    claims = _claim_map(report)
    lines = [f"# {service._t(report['language'], 'report_title')}", ""]
    for warning in _warning_lines(service, report, mode, check_result):
        lines.extend([f"> **{warning}**", ""])
    lines.extend(
        [
            f"- {service._t(report['language'], 'report_id')}: `{report['report_id']}`",
            f"- {service._t(report['language'], 'version')}: `{report['version']}`",
            f"- {service._t(report['language'], 'template')}: `{report['template']}`",
            f"- {service._t(report['language'], 'language')}: `{report['language']}`",
            f"- {service._t(report['language'], 'generated')}: `{report['created_at']}`",
            f"- {service._t(report['language'], 'automatic_check')}: `{_check_state(service, report['language'], check_result['state'])}`",
            "",
        ]
    )
    for section in _report_sections(report):
        lines.extend([f"## {section['title']}", ""])
        for paragraph in section.get("paragraphs", []):
            lines.extend([str(paragraph), ""])
        for claim_id in section.get("claim_ids", []):
            claim = claims.get(claim_id)
            if not claim:
                continue
            lines.extend([f"- {claim['text']} `[{claim_id}]`", ""])
            evidence = claim.get("evidence", {})
            if evidence.get("kind") == "literature":
                anchor = evidence.get("anchor", {})
                lines.extend(
                    [
                        f"  - Source: `{evidence.get('source_id')}` v`{evidence.get('source_version')}`; "
                        f"{anchor.get('type')}: `{anchor.get('value')}`",
                        f"  - Excerpt: “{evidence.get('excerpt', '')}”",
                        "",
                    ]
                )
    if report.get("checklists"):
        lines.extend([f"## {service._t(report['language'], 'checklists')}", ""])
        for framework in report["checklists"]:
            lines.extend([f"### {framework['framework']}", ""])
            for item in framework.get("items", []):
                links = _item_evidence_links(item)
                lines.append(
                    f"- **{item['title']}** — {_item_status(service, report['language'], item)}; "
                    f"{service._t(report['language'], 'section')}: `{item.get('report_section', '')}`; "
                    f"{service._t(report['language'], 'evidence')}: `{links}`"
                )
                if item.get("note"):
                    lines.append(f"  - {item['note']}")
            lines.append("")
    rows = _metric_rows(report)
    if rows:
        headers = [service._t(report['language'], key) for key in ("model", "metric", "value", "unit", "n_rows", "n_participants")]
        lines.extend([f"## {service._t(report['language'], 'metric_table')}", "", "| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"])
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
        lines.append("")
    if figure_paths:
        lines.extend([f"## {service._t(report['language'], 'figures')}", ""])
        for path in figure_paths:
            lines.extend([f"![{service._t(report['language'], 'observed_vs_predicted')}](figures/{path.name})", ""])
    lines.extend([f"## {service._t(report['language'], 'audit')}", ""])
    if check_result.get("errors"):
        lines.append(f"### {service._t(report['language'], 'errors')}")
        for item in check_result["errors"]:
            lines.append(f"- `{item.get('code')}` — `{item.get('claim_id', '')}`")
        lines.append("")
    if check_result.get("warnings"):
        lines.append(f"### {service._t(report['language'], 'warnings')}")
        for item in check_result["warnings"]:
            lines.append(f"- `{item.get('code')}` — `{item.get('claim_id', '')}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_docx(path: Path, service: Any, report: dict[str, Any], mode: str, check_result: dict[str, Any], figures: list[Path]) -> None:
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("python-docx is required for DOCX export") from exc
    claims = _claim_map(report)
    document = Document()
    styles = document.styles
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2"):
        try:
            styles[style_name].font.name = "Malgun Gothic"
        except KeyError:
            pass
    document.add_heading(service._t(report["language"], "report_title"), 0)
    for warning in _warning_lines(service, report, mode, check_result):
        paragraph = document.add_paragraph()
        run = paragraph.add_run(warning)
        run.bold = True
        run.font.size = Pt(11)
    metadata = document.add_table(rows=0, cols=2)
    for label, value in (
        (service._t(report["language"], "report_id"), report["report_id"]),
        (service._t(report["language"], "version"), report["version"]),
        (service._t(report["language"], "template"), report["template"]),
        (service._t(report["language"], "language"), report["language"]),
        (service._t(report["language"], "automatic_check"), _check_state(service, report["language"], check_result["state"])),
    ):
        cells = metadata.add_row().cells
        cells[0].text = str(label)
        cells[1].text = str(value)
    for section in _report_sections(report):
        document.add_heading(section["title"], level=1)
        for paragraph in section.get("paragraphs", []):
            document.add_paragraph(str(paragraph))
        for claim_id in section.get("claim_ids", []):
            claim = claims.get(claim_id)
            if not claim:
                continue
            document.add_paragraph(f"{claim['text']} [{claim_id}]", style="List Bullet")
            evidence = claim.get("evidence", {})
            if evidence.get("kind") == "literature":
                anchor = evidence.get("anchor", {})
                document.add_paragraph(
                    f"{evidence.get('source_id')} v{evidence.get('source_version')}; "
                    f"{anchor.get('type')}: {anchor.get('value')}\n“{evidence.get('excerpt', '')}”"
                )
    if report.get("checklists"):
        document.add_heading(service._t(report["language"], "checklists"), level=1)
        for framework in report["checklists"]:
            document.add_heading(framework["framework"], level=2)
            for item in framework.get("items", []):
                links = _item_evidence_links(item)
                document.add_paragraph(
                    f"{item['title']} — {_item_status(service, report['language'], item)}; {item.get('report_section', '')}; {links}",
                    style="List Bullet",
                )
                if item.get("note"):
                    document.add_paragraph(item["note"])
    rows = _metric_rows(report)
    if rows:
        document.add_heading(service._t(report["language"], "metric_table"), level=1)
        table = document.add_table(rows=1, cols=6)
        for cell, key in zip(table.rows[0].cells, ("model", "metric", "value", "unit", "n_rows", "n_participants")):
            cell.text = service._t(report["language"], key)
        for row in rows:
            for cell, value in zip(table.add_row().cells, row):
                cell.text = value
    if figures:
        from docx.shared import Inches
        document.add_heading(service._t(report["language"], "figures"), level=1)
        for figure in figures:
            document.add_picture(str(figure), width=Inches(5.9))
    document.add_heading(service._t(report["language"], "audit"), level=1)
    document.add_paragraph(json.dumps(check_result, ensure_ascii=False, indent=2))
    document.save(path)


def _pdf_font(report: dict[str, Any]) -> tuple[str, str | None]:
    if report["language"] == "en":
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "NanumGothic.ttf",
            Path("C:/Windows/Fonts/NanumGothic.ttf"),
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf",
            Path("C:/Windows/Fonts/malgun.ttf"),
        ]
        for candidate in candidates:
            if candidate.exists():
                unique = hashlib.sha256(str(report.get("report_id", "report")).encode("utf-8")).hexdigest()[:12]
                return f"NanumGothic_{unique}", str(candidate)
        if json.dumps(report, ensure_ascii=False).isascii():
            return "Helvetica", None
        raise RuntimeError("A local Korean-capable font is required for preserved non-Latin text in English PDF export")
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf",
        Path("C:/Windows/Fonts/malgun.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            unique = hashlib.sha256(str(report.get("report_id", "report")).encode("utf-8")).hexdigest()[:12]
            return f"MalgunGothic_{unique}", str(candidate)
    raise RuntimeError("Malgun Gothic is required when report content contains Korean; the font is not bundled")


def _malgun_font(report: dict[str, Any]) -> tuple[str | None, str | None]:
    # Malgun is the Korean-report default.  Its very large cmap has shown
    # selective glyph loss in mixed Helvetica/Malgun dynamic subsets, so an
    # English report that preserves Korean user/source text uses the locally
    # installed static Nanum Gothic fallback instead.  No font is bundled.
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = (
        [font_dir / "NanumGothic.ttf", Path("C:/Windows/Fonts/NanumGothic.ttf")]
        if report.get("language") == "en"
        else [font_dir / "malgun.ttf", Path("C:/Windows/Fonts/malgun.ttf")]
    )
    for candidate in candidates:
        if candidate.exists():
            unique = hashlib.sha256((str(report.get("report_id", "report")) + ":fallback").encode("utf-8")).hexdigest()[:12]
            return f"KoreanFallback_{unique}", str(candidate)
    return None, None


def _pdf_markup(value: str, default_font: str, unicode_font: str | None) -> str:
    escaped = _escape(value)
    if default_font == "Helvetica" and any(ord(character) > 255 for character in value):
        if not unicode_font:
            raise RuntimeError("Malgun Gothic is required for non-Latin text in English PDF export")
        return f'<font name="{unicode_font}">{escaped}</font>'
    return escaped


def _write_pdf(path: Path, service: Any, report: dict[str, Any], mode: str, check_result: dict[str, Any], figures: list[Path]) -> None:
    # ReportLab's dynamic TrueType subset state is process-global and can retain
    # corrupt glyph mappings across otherwise sequential documents.  Render each
    # complete PDF in a fresh Python process; a thread lock alone is insufficient.
    render_report = copy.deepcopy(report)
    if service.library is not None:
        for claim in render_report.get("claims", []):
            evidence = claim.get("evidence") or {}
            if evidence.get("kind") != "literature" or not evidence.get("source_id"):
                continue
            try:
                source = service.library.get_source(str(evidence["source_id"]))
            except Exception:
                continue
            evidence["source_title"] = source.get("title") or Path(str(source.get("original_path") or "")).name
            evidence["source_url"] = source.get("url") or (
                f"https://doi.org/{source['doi']}" if source.get("doi") else None)
    job = {
        "path": str(path),
        "locales": service.locales,
        "report": render_report,
        "mode": mode,
        "check_result": check_result,
        "figures": [str(item) for item in figures],
    }
    fd, job_name = tempfile.mkstemp(prefix=".report-pdf-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(job, handle, ensure_ascii=False)
        environment = dict(os.environ)
        environment["RESEARCHBENCH_PDF_WORKER"] = "1"
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "researchbench.report_export", "--render-pdf", job_name],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not path.is_file():
            detail = (completed.stderr or completed.stdout or "pdf_worker_failed").strip()[-2000:]
            raise RuntimeError(f"pdf_worker_failed:{detail}")
    finally:
        Path(job_name).unlink(missing_ok=True)


def _write_pdf_locked(path: Path, service: Any, report: dict[str, Any], mode: str, check_result: dict[str, Any], figures: list[Path]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("reportlab is required for PDF report export") from exc
    font_name, font_path = _pdf_font(report)
    if font_path:
        pdfmetrics.registerFont(TTFont(font_name, font_path))
    unicode_font_name, unicode_font_path = (font_name, font_path)
    if font_name == "Helvetica":
        unicode_font_name, unicode_font_path = _malgun_font(report)
        if unicode_font_path and unicode_font_name != font_name:
            pdfmetrics.registerFont(TTFont(unicode_font_name, unicode_font_path))
    markup = lambda value: _pdf_markup(str(value), font_name, unicode_font_name)
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ReportBody", parent=styles["BodyText"], fontName=font_name, fontSize=9.5, leading=14,
        alignment=TA_LEFT, spaceAfter=5,
    )
    title = ParagraphStyle(
        "ReportTitle", parent=body, fontSize=18, leading=23, spaceAfter=12,
    )
    heading = ParagraphStyle(
        "ReportHeading", parent=body, fontSize=13, leading=17, spaceBefore=10, spaceAfter=6,
        keepWithNext=True,
    )
    warning_style = ParagraphStyle(
        "Warning", parent=body, textColor=colors.HexColor("#8A3B12"), backColor=colors.HexColor("#FFF3CD"),
        borderPadding=6, spaceAfter=8,
    )
    caption_style = ParagraphStyle(
        "FigureCaption", parent=body, fontSize=8.5, leading=12, textColor=colors.HexColor("#34495E"),
        spaceAfter=5, keepWithNext=True,
    )
    document = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=service._t(report["language"], "report_title"), author="ResearchBench",
    )

    def comparison_metric_table():
        summary = (report.get("comparison_evidence") or {}).get("summary") or {}
        comparison_rows = summary.get("rows") or []
        if not comparison_rows:
            return None
        selection = report["comparison_evidence"]["selection"]
        tolerance = float(selection["tolerance"])
        labels = (["모델", "MAE (분)", "RMSE (분)", "편향 (분)", f"±{tolerance:g}분 이내", "공통 밤", "참여자"]
                  if report["language"] == "ko" else
                  ["Model", "MAE (min)", "RMSE (min)", "Bias (min)", f"Within ±{tolerance:g} min", "Common nights", "Participants"])
        compact = [labels]
        for row in comparison_rows:
            compact.append([
                str(row["model_id"]), f"{float(row['mae']):.2f}", f"{float(row['rmse']):.2f}",
                f"{float(row['bias']):+.2f}", f"{float(row['within_tolerance']) * 100:.1f}%",
                str(row["n_nights"]), str(row["n_participants"]),
            ])
        cell_style = ParagraphStyle("ComparisonMetricCell", parent=body, fontSize=7.3, leading=9,
                                    spaceAfter=0, wordWrap="CJK")
        data = [[Paragraph(markup(cell), cell_style) for cell in row] for row in compact]
        table = Table(data, repeatRows=1,
                      colWidths=[47*mm, 20*mm, 21*mm, 20*mm, 25*mm, 20*mm, 20*mm])
        table.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), font_name), ("FONTSIZE", (0,0), (-1,-1), 7.3),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#D9EAF7")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F7FAFC")]),
            ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#777777")),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"), ("ALIGN", (1,1), (-1,-1), "RIGHT"),
            ("LEFTPADDING", (0,0), (-1,-1), 3), ("RIGHTPADDING", (0,0), (-1,-1), 3),
        ]))
        return table

    report_heading = ("SleepWell 모델 비교 보고서" if report["language"] == "ko" else "SleepWell model comparison report") if report.get("template") == "model_comparison" else service._t(report["language"], "report_title")
    story: list[Any] = [Paragraph(markup(report_heading), title)]
    for warning in _warning_lines(service, report, mode, check_result):
        story.append(Paragraph(markup(warning), warning_style))
    story.append(Paragraph(markup(f"{service._t(report['language'], 'report_id')}: {report['report_id']} | {service._t(report['language'], 'version')}: {report['version']}"), body))
    story.append(Paragraph(markup(f"{service._t(report['language'], 'automatic_check')}: {_check_state(service, report['language'], check_result['state'])}"), body))
    claims = _claim_map(report)
    for section in _report_sections(report):
        story.append(Paragraph(markup(section["title"]), heading))
        for paragraph in section.get("paragraphs", []):
            story.append(Paragraph(markup(str(paragraph)), body))
        for claim_id in section.get("claim_ids", []):
            claim = claims.get(claim_id)
            if claim:
                evidence = claim.get("evidence", {})
                if report.get("template") == "model_comparison" and evidence.get("kind") == "comparison_metric":
                    # The compact model-level table below carries the same
                    # checked numeric evidence without four repetitive lines/model.
                    continue
                suffix = "" if report.get("template") == "model_comparison" else f" [{claim_id}]"
                if not (report.get("template") == "model_comparison" and evidence.get("kind") == "literature"):
                    story.append(Paragraph(markup(f"- {claim['text']}{suffix}"), body))
                if evidence.get("kind") == "literature":
                    anchor = evidence.get("anchor") or {}
                    source_label = evidence.get("source_title") or evidence.get("title") or evidence.get("source_id") or "Source"
                    location = f"{anchor.get('type', 'location')}: {anchor.get('value', '')}"
                    page_match = re.search(r"pdf_page:(\d+)", str(anchor.get('value', '')))
                    if report.get("template") == "model_comparison" and page_match:
                        location = f"{page_match.group(1)}쪽" if report["language"] == "ko" else f"page {page_match.group(1)}"
                    url = evidence.get("source_url") or evidence.get("url")
                    reference = f"{source_label} — {location}"
                    story.append(Paragraph(markup(reference), body))
                    if url and str(url).startswith(("https://", "http://")):
                        story.append(Paragraph(f'<link href="{html.escape(str(url), quote=True)}" color="#126D92">{markup(url)}</link>', body))
                    story.append(Paragraph(markup(f"“{evidence.get('excerpt', '')}”"), body))
        if report.get("template") == "model_comparison" and section.get("section_id") == "results":
            table = comparison_metric_table()
            if table is not None:
                story.append(table)
    if report.get("checklists"):
        story.append(Paragraph(markup(service._t(report["language"], "checklists")), heading))
        for framework in report["checklists"]:
            framework_label = framework["framework"]
            if report.get("template") == "model_comparison" and report["language"] == "ko":
                framework_label = ("TRIPOD+AI · 비교 보고 관련 항목" if framework['framework_id'].startswith('tripod')
                                   else "PROBAST+AI · 평가자 검토가 필요한 영역")
            story.append(Paragraph(markup(framework_label), heading))
            for item in framework.get("items", []):
                if report.get("template") == "model_comparison":
                    linked_claims = [claims.get(claim_id) for claim_id in item.get("evidence_claim_ids") or []]
                    metric_count = sum(
                        claim is not None and claim.get("evidence", {}).get("kind") == "comparison_metric"
                        for claim in linked_claims)
                    source_count = sum(
                        claim is not None and claim.get("evidence", {}).get("kind") == "literature"
                        for claim in linked_claims)
                    if report["language"] == "ko":
                        parts = [f"동결 비교표·그래프 ({metric_count}개 수치 근거)"] if metric_count else []
                        if source_count:
                            parts.append(f"검색 문헌 {source_count}건")
                        links = ", ".join(parts) or "자동 근거 없음; 평가자 판단 대기"
                    else:
                        parts = ([f"frozen comparison table and figures ({metric_count} numeric evidence items)"]
                                 if metric_count else [])
                        if source_count:
                            parts.append(f"{source_count} retrieved source items")
                        links = ", ".join(parts) or "no automatic evidence; assessor judgment pending"
                else:
                    links = _item_evidence_links(item)
                section_prefix = "" if report.get("template") == "model_comparison" else f"{item.get('report_section', '')}; "
                story.append(
                    Paragraph(
                        markup(
                            f"- {item['title']} — {_item_status(service, report['language'], item)}; "
                            f"{section_prefix}{service._t(report['language'], 'evidence')}: {links}"
                        ),
                        body,
                    )
                )
                if item.get("note"):
                    story.append(Paragraph(markup(item["note"]), body))
    rows = _metric_rows(report)
    if report.get("template") != "model_comparison" and rows:
        story.append(Paragraph(markup(service._t(report['language'], 'metric_table')), heading))
        cell_style = ParagraphStyle("MetricCell", parent=body, fontSize=7.5, leading=10,
                                    spaceAfter=0, wordWrap="CJK")
        raw_data = [[service._t(report['language'], key) for key in ("model", "metric", "value", "unit", "n_rows", "n_participants")], *rows]
        data = [[Paragraph(markup(cell), cell_style) for cell in row] for row in raw_data]
        table = Table(data, repeatRows=1, colWidths=[52*mm, 44*mm, 23*mm, 15*mm, 20*mm, 20*mm])
        table.setStyle(TableStyle([("FONTNAME", (0,0), (-1,-1), font_name), ("FONTSIZE", (0,0), (-1,-1), 7.5), ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#D9EAF7")), ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#777777")), ("VALIGN", (0,0), (-1,-1), "TOP")]))
        story.append(table)
    if figures:
        captions_ko = {
            "mae": "선택 모델의 평균 절대 오차(MAE). 낮을수록 좋다.",
            "rmse": "큰 오차에 더 민감한 RMSE. 낮을수록 좋다.",
            "bias": "평균 편향(예측-실제). 0에 가까울수록 방향성 편향이 작다.",
            "tolerance": "지정된 허용오차 안에 든 공통 기록의 비율. 높을수록 좋다.",
            "predictions": "선택 모델별 실제값-예측값 산점도. 대각선은 완전 일치를 뜻한다.",
            "errors": "공통 야간의 예측 오차 분포. 0선에서의 치우침과 꼬리를 함께 본다.",
            "participants": "참여자별 MAE 분포. 사람 간 성능 이질성을 보여준다.",
        }
        captions_en = {
            "mae": "Mean absolute error (MAE) for the selected models; lower is better.",
            "rmse": "RMSE, which gives greater weight to large errors; lower is better.",
            "bias": "Mean bias (predicted minus observed); values nearer zero have less directional bias.",
            "tolerance": "Share of common records within the specified tolerance; higher is better.",
            "predictions": "Observed-versus-predicted panels for the selected models; the diagonal is perfect agreement.",
            "errors": "Night-level error distributions; displacement from zero and tails show error structure.",
            "participants": "Participant-level MAE distributions, showing heterogeneity between people.",
        }
        captions = captions_ko if report["language"] == "ko" else captions_en
        comparison = report.get("template") == "model_comparison"
        if not comparison:
            story.append(Paragraph(markup(service._t(report['language'], 'figures')), heading))
        for index, figure_path in enumerate(figures, start=1):
            if comparison:
                story.append(PageBreak())
                kind = next((name for name in _COMPARISON_FIGURE_KINDS if figure_path.stem.endswith("_" + name)), figure_path.stem)
                heading_text = (f"그림 {index}. {captions.get(kind, kind)}" if report["language"] == "ko"
                                else f"Figure {index}. {captions.get(kind, kind)}")
                image = Image(str(figure_path))
                scale = min(170*mm / image.imageWidth, 215*mm / image.imageHeight)
                image.drawWidth = image.imageWidth * scale
                image.drawHeight = image.imageHeight * scale
                story.append(KeepTogether([Paragraph(markup(heading_text), caption_style), image]))
            else:
                story.append(Image(str(figure_path), width=150*mm, height=126*mm, kind="proportional"))
        if comparison:
            story.append(PageBreak())
    story.extend([Spacer(1, 6), Paragraph(markup(service._t(report['language'], 'audit')), heading)])
    story.append(Paragraph(markup(
        f"{service._t(report['language'], 'automatic_check')}: "
        f"{_check_state(service, report['language'], check_result['state'])}; "
        f"{check_result.get('checked_at', '')}"
    ), body))
    for category in ("errors", "warnings"):
        grouped: dict[str, list[str]] = {}
        for item in check_result.get(category, []):
            identifiers = grouped.setdefault(str(item.get("code", "")), [])
            if item.get("claim_id"):
                identifiers.append(str(item["claim_id"]))
        story.append(Paragraph(markup(f"{service._t(report['language'], category)}: {len(check_result.get(category, []))}"), body))
        for code, identifiers in grouped.items():
            if report.get("template") == "model_comparison" and report["language"] == "ko":
                code = {
                    "comparison_is_point_estimate_not_superiority_test": "모델 차이는 점추정치 비교이며 통계적 우월성 검정 결과가 아닙니다.",
                    "probast_final_assessor_review_required": "PROBAST+AI의 최종 편향 위험·적용성 판단에는 평가자 검토가 필요합니다.",
                    "checklist_judgment_pending": "메타데이터와 연구자 검토가 필요한 지침 항목이 남아 있습니다.",
                    "semantic_support_pending_human_review": "검색된 문헌 구절이 해석을 뒷받침하는지 연구자가 확인해야 합니다.",
                }.get(code, code)
                detail = f" ({len(identifiers)}개 항목)" if identifiers else ""
                story.append(Paragraph(markup(code + detail), body))
                continue
            detail = f" — {', '.join(identifiers)}" if identifiers else ""
            story.append(Paragraph(markup(f"{code}{detail}"), body))
    def page_number(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawRightString(A4[0] - 18*mm, 8*mm, str(doc.page))
        canvas.restoreState()

    document.build(story, onFirstPage=page_number, onLaterPages=page_number)


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _write_claims_csv(path: Path, report: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "claim_id", "claim_type", "text", "run_id", "result_hash", "field_path", "model_id",
                "metric", "value", "unit", "n_rows", "n_participants", "source_id", "source_version",
                "chunk_id", "anchor", "search_id",
                "cohort", "policy", "arm", "weighting", "ci95_low", "ci95_high",
                "evaluation_snapshot_sha256", "metric_index",
            ],
        )
        writer.writeheader()
        for claim in report.get("claims", []):
            evidence = claim.get("evidence", {})
            writer.writerow(
                {
                    "claim_id": claim.get("claim_id"),
                    "claim_type": claim.get("claim_type"),
                    "text": claim.get("text"),
                    "run_id": evidence.get("run_id"),
                    "result_hash": evidence.get("result_hash"),
                    "field_path": json.dumps(evidence.get("field_path"), ensure_ascii=False) if evidence.get("field_path") else "",
                    "model_id": evidence.get("model_id"),
                    "metric": evidence.get("metric"),
                    "value": evidence.get("value"),
                    "unit": evidence.get("unit"),
                    "n_rows": evidence.get("n_rows"),
                    "n_participants": evidence.get("n_participants"),
                    "source_id": evidence.get("source_id"),
                    "source_version": evidence.get("source_version"),
                    "chunk_id": evidence.get("chunk_id"),
                    "anchor": json.dumps(evidence.get("anchor"), ensure_ascii=False) if evidence.get("anchor") else "",
                    "search_id": evidence.get("search_id"),
                    **{key: evidence.get(key) for key in (
                        "cohort", "policy", "arm", "weighting", "ci95_low", "ci95_high",
                        "evaluation_snapshot_sha256", "metric_index",
                    )},
                }
            )


def _bibtex_entry(service: Any, source_id: str) -> str:
    source = service.library.get_source(source_id)
    key = _slug(f"rb_{source_id}")
    fields: list[tuple[str, str]] = []
    if source.get("title"):
        fields.append(("title", source["title"]))
    authors = source.get("authors") or []
    if authors:
        fields.append(("author", " and ".join(str(item) for item in authors)))
    published = str(source.get("published_date") or "")
    year = re.search(r"\b(19|20)\d{2}\b", published)
    if year:
        fields.append(("year", year.group(0)))
    if source.get("doi"):
        fields.append(("doi", source["doi"]))
    if source.get("url"):
        fields.append(("url", source["url"]))
    fields.append(("note", f"ResearchBench source {source_id}, version {source.get('version') or ''}"))
    body = ",\n".join(f"  {name} = {{{str(value).replace('{', '').replace('}', '')}}}" for name, value in fields)
    return f"@misc{{{key},\n{body}\n}}\n"


def _write_bibtex(path: Path, service: Any, report: dict[str, Any]) -> None:
    source_ids = sorted(
        {
            claim.get("evidence", {}).get("source_id")
            for claim in report.get("claims", [])
            if claim.get("evidence", {}).get("kind") == "literature"
        }
        - {None}
    )
    entries: list[str] = []
    if source_ids and service.library is None:
        raise RuntimeError("evidence_library_required_for_bibliography")
    for source_id in source_ids:
        entries.append(_bibtex_entry(service, str(source_id)))
    path.write_text("\n".join(entries), encoding="utf-8")


_COMPARISON_FIGURE_KINDS = ("mae", "rmse", "bias", "tolerance", "predictions", "errors", "participants")


def _comparison_figures(service: Any, report: dict[str, Any], figure_dir: Path) -> list[Path]:
    """Recompute the frozen selection and render its seven deterministic plots."""
    from .comparison_reporting import comparison_data_for_report

    data = comparison_data_for_report(service, report)
    selected = (report.get("comparison_evidence") or {}).get("selection") or {}
    if data.get("model_ids") != selected.get("model_ids"):
        raise ValueError("comparison_figure_model_selection_mismatch")
    if data.get("target") != selected.get("target") or data.get("averaging") != selected.get("averaging"):
        raise ValueError("comparison_figure_setting_mismatch")
    if not math.isclose(float(data.get("tolerance")), float(selected.get("tolerance")), rel_tol=0, abs_tol=1e-12):
        raise ValueError("comparison_figure_tolerance_mismatch")

    figure_dir.mkdir(exist_ok=True)
    pngs: list[Path] = []
    with _FIGURE_LOCK:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from .comparison_plotting import make_comparison_figure

        for index, kind in enumerate(_COMPARISON_FIGURE_KINDS, start=1):
            figure = make_comparison_figure(data, kind=kind, lang=report["language"])
            FigureCanvasAgg(figure)
            stem = f"comparison_{index:02d}_{kind}"
            outputs = [figure_dir / f"{stem}.{suffix}" for suffix in ("png", "pdf", "svg")]
            try:
                for output in outputs:
                    figure.savefig(output, dpi=200 if output.suffix == ".png" else None,
                                   facecolor="white", bbox_inches="tight")
            finally:
                figure.clear()
            pngs.append(outputs[0])
    return pngs


def _prediction_figure(paths: list[Path], run_ref: dict[str, Any], project_path: Path, language: str, labels: dict[str, str]) -> bool:
    with _FIGURE_LOCK:
        return _prediction_figure_locked(paths, run_ref, project_path, language, labels)


def _prediction_figure_locked(paths: list[Path], run_ref: dict[str, Any], project_path: Path, language: str, labels: dict[str, str]) -> bool:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.font_manager import FontProperties
    except ImportError:  # pragma: no cover
        return False
    try:
        record = verify_run(
            {"project_path": str(project_path), "source_file": run_ref["source_file"], "result_hash": run_ref["result_hash"]}
        )
    except Exception:
        return False
    canonical = adapt_run(record, str(run_ref.get("engine") or "general"), str(project_path))
    figure = _build_prediction_figure(canonical, language, labels)
    if figure is None:
        return False
    for path in paths:
        figure.savefig(path, dpi=200 if path.suffix.lower() == ".png" else None)
    figure.clear()
    return True


def _build_prediction_figure(canonical: dict[str, Any], language: str, labels: dict[str, str]):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    import numpy as np

    predictions = canonical.get("predictions")
    if not isinstance(predictions, list) or not predictions:
        return None
    font = None
    malgun = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf"
    if malgun.exists():
        font = FontProperties(fname=str(malgun))
    task = canonical.get("evaluation", {}).get("task")
    figure = Figure(figsize=(6.2, 5.2), constrained_layout=True)
    FigureCanvasAgg(figure)
    axis = figure.subplots()

    common_ids = {
        tuple(str(part) for part in value) if isinstance(value, (list, tuple)) else (str(value),)
        for value in canonical.get("evaluation", {}).get("scope_ids", [])
    }

    if task == "classification":
        pairs = []
        for item in predictions:
            if not isinstance(item, dict):
                continue
            identity_value = item["row_id"] if "row_id" in item else item.get("night_id", item.get("date"))
            identity = (str(identity_value),) if identity_value is not None else ()
            if common_ids and identity not in common_ids:
                continue
            actual, predicted = item.get("y_true"), item.get("y_pred")
            if actual is None or predicted is None:
                continue
            if any(isinstance(value, float) and not math.isfinite(value) for value in (actual, predicted)):
                continue
            pairs.append((actual, predicted))
        if not pairs:
            return None
        classes = sorted({value for pair in pairs for value in pair}, key=lambda value: str(value))
        index = {value: position for position, value in enumerate(classes)}
        matrix = np.zeros((len(classes), len(classes)), dtype=int)
        for actual, predicted in pairs:
            matrix[index[actual], index[predicted]] += 1
        image = axis.imshow(matrix, cmap="Blues", interpolation="nearest")
        figure.colorbar(image, ax=axis, fraction=0.046, pad=0.04)
        axis.set_xticks(range(len(classes)), [str(value) for value in classes])
        axis.set_yticks(range(len(classes)), [str(value) for value in classes])
        axis.set_title(labels["confusion_matrix"], fontproperties=font)
        axis.set_xlabel(labels["predicted_class"], fontproperties=font)
        axis.set_ylabel(labels["actual_class"], fontproperties=font)
        threshold = matrix.max() / 2 if matrix.size else 0
        for row in range(len(classes)):
            for column in range(len(classes)):
                axis.text(column, row, str(matrix[row, column]), ha="center", va="center", color="white" if matrix[row, column] > threshold else "black")
        axis._researchbench_scope_n = len(pairs)  # type: ignore[attr-defined]
        axis._researchbench_confusion = matrix.tolist()  # type: ignore[attr-defined]
        return figure

    sleepwell = canonical.get("engine") == "sleepwell"
    groups: dict[str, list[tuple[float, float]]] = {}
    observation_ids: set[tuple[str, ...]] = set()
    for item in predictions:
        if not isinstance(item, dict):
            continue
        if sleepwell:
            identity = (str(item.get("participant_id")), str(item.get("cutoff")))
            if common_ids and identity not in common_ids:
                continue
            actual, predicted = item.get("actual_onset_offset_min"), item.get("onset_offset_min")
        else:
            identity_value = item["row_id"] if "row_id" in item else item.get("night_id", item.get("date"))
            identity = (str(identity_value),) if identity_value is not None else ()
            if common_ids and identity not in common_ids:
                continue
            actual = item["y_true"] if "y_true" in item else item.get("actual")
            predicted = item["y_pred"] if "y_pred" in item else item.get("predicted")
        try:
            pair = (float(actual), float(predicted))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in pair):
            continue
        model = str(item.get("model_id") or item.get("model") or item.get("model_key") or canonical.get("config", {}).get("model") or "model")
        groups.setdefault(model, []).append(pair)
        if identity:
            observation_ids.add(identity)
    if not groups:
        return None
    colors_used: dict[str, str] = {}
    for model, pairs in sorted(groups.items()):
        color = model_color(model)
        colors_used[model] = color
        axis.scatter([item[0] for item in pairs], [item[1] for item in pairs], alpha=0.65, color=color, label=f"{model} (n={len(pairs)})")
    all_pairs = [pair for pairs in groups.values() for pair in pairs]
    low = min(min(item) for item in all_pairs)
    high = max(max(item) for item in all_pairs)
    axis.plot([low, high], [low, high], linestyle="--", color="#333333", linewidth=1)
    unit = "min" if sleepwell else str(canonical.get("config", {}).get("target_unit") or "").strip()
    suffix = f" ({unit})" if unit else ""
    base_label = labels["sleep_onset_offset"] if sleepwell else labels["observed"]
    axis.set_title(labels["observed_vs_predicted"], fontproperties=font)
    axis.set_xlabel(base_label + suffix, fontproperties=font)
    axis.set_ylabel((labels["sleep_onset_offset"] if sleepwell else labels["predicted"]) + suffix, fontproperties=font)
    distinct = len(observation_ids) if observation_ids else len({actual for actual, _ in all_pairs})
    axis.text(0.02, 0.98, f"{labels['evaluated_n']}={len(all_pairs)}; {labels['distinct_n']}={distinct}", transform=axis.transAxes, va="top", fontproperties=font)
    if len(groups) > 1 or next(iter(groups)) != "model":
        axis.legend(loc="best", fontsize=7, prop=font)
    axis._researchbench_scope_n = distinct  # type: ignore[attr-defined]
    axis._researchbench_point_n = len(all_pairs)  # type: ignore[attr-defined]
    axis._researchbench_model_colors = colors_used  # type: ignore[attr-defined]
    return figure


def _runtime_versions() -> dict[str, str]:
    versions = {"python": platform.python_version(), "platform": platform.platform()}
    for package in ("matplotlib", "python-docx", "reportlab", "pypdf"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def export_report_package(
    service: Any,
    report: dict[str, Any],
    output_dir: Path,
    *,
    formats: set[str],
    mode: str,
    check_result: dict[str, Any],
) -> dict[str, Path]:
    allowed = {"md", "docx", "pdf", "json", "bib", "csv", "figures"}
    unknown = formats - allowed
    if unknown:
        raise ValueError(f"unsupported_export_formats:{','.join(sorted(unknown))}")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir = output_dir / _slug(f"{report['report_id']}_v{report['version']}_{report['language']}")
    if package_dir.exists():
        raise FileExistsError("immutable_export_package_exists")
    package_dir.mkdir()
    paths: dict[str, Path] = {}
    try:
        figure_dir = package_dir / "figures"
        embedded_figures: list[Path] = []
        if report.get("template") == "model_comparison":
            embedded_figures = _comparison_figures(service, report, figure_dir)
        else:
            for run_ref in report.get("run_refs", []):
                figure_dir.mkdir(exist_ok=True)
                stem = f"{_slug(run_ref['run_id'])}_predictions"
                outputs = [figure_dir / f"{stem}.{suffix}" for suffix in ("png", "pdf", "svg")]
                labels = {
                    key: service._t(report["language"], key)
                    for key in (
                        "observed_vs_predicted", "observed", "predicted", "evaluated_n", "distinct_n",
                        "sleep_onset_offset", "confusion_matrix", "actual_class", "predicted_class",
                    )
                }
                if _prediction_figure(outputs, run_ref, service.project_path, report["language"], labels):
                    embedded_figures.append(outputs[0])
        if embedded_figures:
            paths["figures"] = figure_dir
        elif figure_dir.exists():
            figure_dir.rmdir()

        markdown = render_markdown(service, report, mode, check_result, embedded_figures)
        if "md" in formats:
            path = package_dir / "report.md"
            path.write_text(markdown, encoding="utf-8")
            paths["markdown"] = path
        if "docx" in formats:
            path = package_dir / "report.docx"
            _write_docx(path, service, report, mode, check_result, embedded_figures)
            paths["docx"] = path
        if "pdf" in formats:
            path = package_dir / "report.pdf"
            _write_pdf(path, service, report, mode, check_result, embedded_figures)
            paths["pdf"] = path
        if "json" in formats:
            path = package_dir / "audit.json"
            audit = {
                "report": _redacted_report(report),
                "export_mode": mode,
                "export_check": check_result,
                "warning": _warning_lines(service, report, mode, check_result),
            }
            path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            paths["audit_json"] = path
        if "csv" in formats:
            path = package_dir / "tables.csv"
            _write_claims_csv(path, report)
            paths["tables_csv"] = path
        if "bib" in formats:
            path = package_dir / "references.bib"
            _write_bibtex(path, service, report)
            paths["bibtex"] = path
        cited = [
            claim["evidence"]
            for claim in report.get("claims", [])
            if claim.get("evidence", {}).get("kind") == "literature"
        ]
        knowledge_snapshot = service.library.snapshot() if service.library is not None else None
        retrieval_records = []
        if service.library is not None:
            for search_id in sorted({item["search_id"] for item in cited}):
                log = service.library.search_log(search_id)
                retrieval_records.append(
                    {
                        "search_id": search_id,
                        "query": log.get("query"),
                        "filters": log.get("filters"),
                        "settings": log.get("settings"),
                        "status": log.get("status"),
                        "hits": [
                            {
                                key: hit.get(key)
                                for key in (
                                    "rank", "chunk_id", "relevance_score", "lexical_score",
                                    "embedding_score", "selection_reason", "currently_valid",
                                )
                            }
                            for hit in log.get("hits", [])
                        ],
                    }
                )
        code_hashes = _generation_code_hashes()
        manifest = {
            "schema": "researchbench.report-package.v1",
            "app_version": APP_VERSION,
            "report_schema": REPORT_SCHEMA,
            "template_version": "1",
            "report_id": report["report_id"],
            "report_version": report["version"],
            "report_artifact_sha256": report["artifact_sha256"],
            "template": report["template"],
            "language": report["language"],
            "export_mode": mode,
            "automatic_check": check_result,
            "generation": copy.deepcopy(report.get("generation") or
                                        {"llm_used": False, "prompt_version": PROMPT_VERSION, "model": None}),
            "agent_trace_sha256": (report.get("ai_analysis") or {}).get("trace_sha256"),
            "generation_code": {
                "meaning": "code used to assemble and render this report; not the original experiment code identity",
                "files_sha256": code_hashes,
                "aggregate_sha256": hashlib.sha256(json.dumps(code_hashes, sort_keys=True).encode("utf-8")).hexdigest(),
            },
            "run_result_hashes": {item["run_id"]: item["result_hash"] for item in report.get("run_refs", [])},
            "source_ids": sorted({item["source_id"] for item in cited}),
            "chunk_ids": sorted({item["chunk_id"] for item in cited}),
            "search_ids": sorted({item["search_id"] for item in cited}),
            "knowledge_base_snapshot": knowledge_snapshot,
            "retrieval_records": retrieval_records,
            "runtime_versions": _runtime_versions(),
            "files": {},
            "privacy": {
                "raw_participant_records_included": False,
                "evaluation_scope_identifiers_included": False,
                "document_full_text_included": False,
                "api_credentials_included": False,
            },
        }
        if report.get("template") == "model_comparison":
            manifest["comparison_selection"] = copy.deepcopy(report["comparison_evidence"]["selection"])
            manifest["comparison_snapshot_sha256"] = report["comparison_evidence"].get("snapshot_sha256")
            manifest["comparison_figure_kinds"] = list(_COMPARISON_FIGURE_KINDS)
        for name, path in paths.items():
            if path.is_file():
                manifest["files"][name] = {"path": path.name, "sha256": _sha256(path)}
            elif path.is_dir():
                manifest["files"][name] = {
                    "path": path.name,
                    "items": {item.name: _sha256(item) for item in sorted(path.iterdir()) if item.is_file()},
                }
        manifest_path = package_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths["manifest"] = manifest_path
        paths["package"] = package_dir
        return paths
    except Exception:
        shutil.rmtree(package_dir, ignore_errors=True)
        raise


__all__ = ["export_report_package", "render_markdown"]


class _LocaleService:
    def __init__(self, locales: dict[str, Any]) -> None:
        self.locales = locales

    def _t(self, lang: str, key: str) -> str:
        return self.locales[lang][key]


def _pdf_worker_main(job_path: str) -> None:
    if os.environ.get("RESEARCHBENCH_PDF_WORKER") != "1":
        raise RuntimeError("pdf_worker_not_authorized")
    payload = json.loads(Path(job_path).read_text(encoding="utf-8"))
    with _PDF_LOCK:
        _write_pdf_locked(
            Path(payload["path"]),
            _LocaleService(payload["locales"]),
            payload["report"],
            payload["mode"],
            payload["check_result"],
            [Path(item) for item in payload.get("figures", [])],
        )


if __name__ == "__main__":  # pragma: no cover - exercised through parent-process export tests
    if len(sys.argv) == 3 and sys.argv[1] == "--render-pdf":
        _pdf_worker_main(sys.argv[2])
    else:
        raise SystemExit("report_export is an internal module")
