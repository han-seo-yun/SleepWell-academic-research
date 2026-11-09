"""Verified official-reference library for the SleepWell workbench.

The catalog is application metadata.  A document is made available only after
its bytes match the catalog SHA-256 and a PDF parser can open it.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCHEMA = "sleepwell.guideline_library.v1"
MAX_PDF_BYTES = 25 * 1024 * 1024
RESOURCE_ROOT = Path(__file__).resolve().parent / "resources"
CATALOG_PATH = RESOURCE_ROOT / "guideline_library.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("official_document_url_must_be_https")


def _document_index(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in data["documents"]}


def _validate_catalog(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("unsupported_guideline_library_schema")
    frameworks = data.get("frameworks")
    documents = data.get("documents")
    if not isinstance(frameworks, list) or not isinstance(documents, list):
        raise ValueError("invalid_guideline_library_catalog")

    framework_fields = {
        "id", "name", "year", "role_ko", "role_en", "application_ko",
        "application_en", "scope_ko", "scope_en", "official_url",
    }
    document_fields = {
        "id", "framework", "title_ko", "title_en", "year", "url",
        "official_url", "filename", "sha256", "bundled_path", "version",
        "verified_on", "notes_ko", "notes_en",
    }
    framework_ids: set[str] = set()
    for item in frameworks:
        if not isinstance(item, dict) or not framework_fields.issubset(item):
            raise ValueError("invalid_guideline_framework_entry")
        item_id = str(item["id"])
        if not _SAFE_ID_RE.fullmatch(item_id) or item_id in framework_ids:
            raise ValueError("invalid_or_duplicate_guideline_framework_id")
        _require_https(str(item["official_url"]))
        framework_ids.add(item_id)

    document_ids: set[str] = set()
    filenames: set[str] = set()
    root = RESOURCE_ROOT.resolve()
    for item in documents:
        if not isinstance(item, dict) or not document_fields.issubset(item):
            raise ValueError("invalid_guideline_document_entry")
        item_id = str(item["id"])
        filename = str(item["filename"])
        digest = str(item["sha256"]).lower()
        if not _SAFE_ID_RE.fullmatch(item_id) or item_id in document_ids:
            raise ValueError("invalid_or_duplicate_guideline_document_id")
        if str(item["framework"]) not in framework_ids:
            raise ValueError("guideline_document_framework_not_found")
        if Path(filename).name != filename or not filename.lower().endswith(".pdf") or filename in filenames:
            raise ValueError("invalid_or_duplicate_guideline_filename")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid_guideline_document_sha256")
        relative = Path(str(item["bundled_path"]))
        resolved = (root / relative).resolve()
        if relative.is_absolute() or resolved == root or root not in resolved.parents or resolved.suffix.lower() != ".pdf":
            raise ValueError("invalid_guideline_bundled_path")
        _require_https(str(item["url"]))
        _require_https(str(item["official_url"]))
        document_ids.add(item_id)
        filenames.add(filename)
        item["sha256"] = digest
    return data


def catalog() -> dict[str, Any]:
    """Return the validated official-reference catalog."""
    try:
        raw = CATALOG_PATH.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"guideline_library_catalog_not_found:{CATALOG_PATH}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_guideline_library_catalog_json") from exc
    # Detach callers from the parsed object and normalize the SHA values.
    return _validate_catalog(data)


def _document(doc_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = catalog()
    try:
        return data, _document_index(data)[str(doc_id)]
    except KeyError as exc:
        raise KeyError(f"guideline_document_not_found:{doc_id}") from exc


def _validate_pdf(data: bytes, expected_sha256: str) -> str:
    if not data.startswith(b"%PDF-"):
        raise ValueError("guideline_document_is_not_pdf")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("guideline_document_exceeds_size_limit")
    actual = _digest(data)
    if actual != expected_sha256:
        raise ValueError("guideline_document_sha256_mismatch")
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted or len(reader.pages) < 1:
            raise ValueError("guideline_document_unreadable_pdf")
        # Force page-tree parsing now, rather than accepting a lazy reader.
        for page in reader.pages:
            _ = page.mediabox
    except ImportError as exc:
        raise RuntimeError("pypdf_is_required_for_guideline_verification") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("guideline_document_unreadable_pdf") from exc
    return actual


def _bundled_bytes(document: dict[str, Any]) -> tuple[bytes, Path]:
    root = RESOURCE_ROOT.resolve()
    path = (root / str(document["bundled_path"])).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(f"verified_bundled_guideline_not_found:{document['id']}")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError("guideline_document_exceeds_size_limit")
    data = path.read_bytes()
    _validate_pdf(data, str(document["sha256"]))
    return data, path


def _download_bytes(document: dict[str, Any]) -> bytes:
    # ``acquisition_url`` may be an archive/API provenance endpoint.  Only the
    # catalog's explicit document URL is treated as the direct PDF endpoint.
    url = str(document["url"])
    _require_https(url)
    request = Request(url, headers={"User-Agent": "SleepWell-Academic-Workbench/1.0"})
    with urlopen(request, timeout=20) as response:  # nosec: URL is catalog-validated HTTPS
        _require_https(str(response.geturl()))
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_PDF_BYTES:
                    raise ValueError("guideline_document_exceeds_size_limit")
            except ValueError as exc:
                if str(exc) == "guideline_document_exceeds_size_limit":
                    raise
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(1024 * 1024, MAX_PDF_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise ValueError("guideline_document_exceeds_size_limit")
    data = b"".join(chunks)
    _validate_pdf(data, str(document["sha256"]))
    return data


def _cache_paths(document: dict[str, Any], cache_root: str | Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser().resolve()
    folder = root / "official_guidelines" / str(document["id"])
    digest = str(document["sha256"])
    return folder / f"{digest}.pdf", folder / f"{digest}.json"


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f"immutable_guideline_cache_conflict:{path}")


def _store_cache(document: dict[str, Any], data: bytes, source: str, cache_root: str | Path) -> Path:
    _validate_pdf(data, str(document["sha256"]))
    pdf_path, metadata_path = _cache_paths(document, cache_root)
    _write_exclusive(pdf_path, data)
    metadata = {
        "schema": SCHEMA,
        "document_id": document["id"],
        "filename": document["filename"],
        "sha256": document["sha256"],
        "source": source,
        "source_semantics": "initial_cache_creation",
        "verified_on": document["verified_on"],
        "cached_at": _utc_now(),
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid_guideline_cache_metadata:{metadata_path}") from exc
        if existing.get("document_id") != document["id"] or existing.get("sha256") != document["sha256"]:
            raise ValueError(f"immutable_guideline_cache_metadata_conflict:{metadata_path}")
    else:
        _write_exclusive(metadata_path, metadata_bytes)
    return pdf_path


def _receipt(
    document: dict[str, Any], cache_root: str | Path, retrieval_source: str,
    warning: str | None,
) -> Path:
    pdf_path, _ = _cache_paths(document, cache_root)
    receipt_path = pdf_path.parent / "receipts" / f"{uuid.uuid4().hex}.json"
    receipt = {
        "schema": SCHEMA,
        "document_id": document["id"],
        "sha256": document["sha256"],
        "url": document["url"],
        "acquisition_url": document.get("acquisition_url"),
        "retrieval_source": retrieval_source,
        "timestamp": _utc_now(),
        "warning": warning,
    }
    _write_exclusive(
        receipt_path,
        (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return receipt_path


def _obtain_result(
    document: dict[str, Any], path: Path, source: str, warning: str | None,
    cache_root: str | Path,
) -> dict[str, Any]:
    receipt_path = _receipt(document, cache_root, source, warning)
    return {
        "document_id": document["id"], "path": str(path),
        "sha256": document["sha256"], "source": source,
        "retrieval_source": source, "verified_on": document["verified_on"],
        "warning": warning, "receipt_path": str(receipt_path),
    }


def _existing_cache(document: dict[str, Any], cache_root: str | Path) -> Path | None:
    pdf_path, _ = _cache_paths(document, cache_root)
    if not pdf_path.exists():
        return None
    data = pdf_path.read_bytes()
    _validate_pdf(data, str(document["sha256"]))
    return pdf_path


def resource_status(doc_id: str) -> dict[str, Any]:
    """Report whether the catalog's bundled original is present and verified."""
    _, document = _document(doc_id)
    result: dict[str, Any] = {
        "document_id": document["id"],
        "filename": document["filename"],
        "expected_sha256": document["sha256"],
        "verified_on": document["verified_on"],
        "available": False,
        "bundled": False,
        "valid": False,
        "path": None,
        "warning": None,
    }
    try:
        data, path = _bundled_bytes(document)
        result.update({
            "available": True,
            "bundled": True,
            "valid": True,
            "path": str(path),
            "sha256": _digest(data),
        })
    except Exception as exc:
        result["warning"] = f"Bundled original is unavailable or failed verification: {exc}"
    return result


def obtain(doc_id: str, cache_root: str | Path, prefer_network: bool = False) -> dict[str, Any]:
    """Obtain verified original bytes, preferring the bundle unless requested otherwise."""
    _, document = _document(doc_id)
    warning: str | None = None

    if prefer_network:
        try:
            data = _download_bytes(document)
            path = _store_cache(document, data, "network", cache_root)
            return _obtain_result(document, path, "network", None, cache_root)
        except Exception as exc:
            warning = f"Official download could not be verified ({exc}); a verified local copy was used."

    try:
        data, _ = _bundled_bytes(document)
        path = _store_cache(document, data, "bundled", cache_root)
        return _obtain_result(document, path, "bundled", warning, cache_root)
    except Exception as bundle_error:
        cached = _existing_cache(document, cache_root)
        if cached is not None:
            fallback_warning = warning or f"Bundled original is unavailable ({bundle_error}); a previously verified cache was used."
            return _obtain_result(document, cached, "cache", fallback_warning, cache_root)
        if warning:
            raise RuntimeError(f"{warning} No verified bundled or cached copy is available.") from bundle_error
        raise


def copy_original(doc_id: str, destination_file: str | Path, cache_root: str | Path) -> dict[str, Any]:
    """Copy verified original bytes without conversion and without overwriting."""
    obtained = obtain(doc_id, cache_root, prefer_network=False)
    source = Path(obtained["path"])
    data = source.read_bytes()
    _validate_pdf(data, str(obtained["sha256"]))
    destination = Path(destination_file).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"destination_exists:{destination}")
    _write_exclusive(destination, data)
    return {**obtained, "path": str(destination), "copied_from": str(source)}


def save_all(destination_dir: str | Path, cache_root: str | Path) -> list[dict[str, Any]]:
    """Copy every catalog document and write an integrity manifest."""
    data = catalog()
    destination = Path(destination_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"destination_directory_not_empty:{destination}")
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, Any]] = []
    for document in data["documents"]:
        result = copy_original(document["id"], destination / document["filename"], cache_root)
        copied.append({
            "document_id": document["id"],
            "framework": document["framework"],
            "filename": document["filename"],
            "year": document["year"],
            "url": document["url"],
            "official_url": document["official_url"],
            "acquisition_url": document.get("acquisition_url"),
            "version": document["version"],
            "license": document.get("license"),
            "sha256": result["sha256"],
            "source": result["source"],
            "retrieval_source": result["retrieval_source"],
            "verified_on": result["verified_on"],
            "path": result["path"],
            "warning": result["warning"],
        })
    manifest = {
        "schema": SCHEMA,
        "created_at": _utc_now(),
        "documents": [{key: value for key, value in item.items() if key != "path"} for item in copied],
    }
    manifest_path = destination / "manifest.json"
    _write_exclusive(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return copied


def _pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    name = "SleepWellGuidelineLibraryFont"
    for candidate in (Path("C:/Windows/Fonts/malgun.ttf"), Path("C:/Windows/Fonts/arial.ttf")):
        if candidate.is_file():
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(candidate)))
            return name
    return "Helvetica"


def export_guide_pdf(destination_file: str | Path, language: str = "ko") -> Path:
    """Export a SleepWell application guide; this is not an official original."""
    if language not in {"ko", "en"}:
        raise ValueError("language_must_be_ko_or_en")
    destination = Path(destination_file).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"destination_exists:{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = catalog()
    try:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise RuntimeError("reportlab_is_required_for_guideline_guide_export") from exc

    font = _pdf_font()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "GuideBody", parent=styles["BodyText"], fontName=font, fontSize=9,
        leading=13, alignment=TA_LEFT, wordWrap="CJK",
    )
    heading = ParagraphStyle(
        "GuideHeading", parent=styles["Heading1"], fontName=font, fontSize=14,
        leading=18, spaceBefore=8, keepWithNext=True,
    )
    subheading = ParagraphStyle(
        "GuideSubheading", parent=styles["Heading2"], fontName=font, fontSize=11,
        leading=15, spaceBefore=6, keepWithNext=True,
    )

    def para(value: Any, style: Any = body) -> Any:
        return Paragraph(html.escape(str(value)).replace("\n", "<br/>"), style)

    ko = language == "ko"
    story: list[Any] = [
        para("SleepWell 공식 지침 적용 안내서" if ko else "SleepWell Official-Guideline Application Guide", heading),
        para(
            "이 문서는 SleepWell 적용 안내서이며 공식 지침 원문이 아닙니다. 또한 규정 준수 인증이나 임상적 인증을 의미하지 않습니다."
            if ko else
            "This is a SleepWell application guide, not an official guideline original. It does not constitute compliance or clinical certification."
        ),
        Spacer(1, 3 * mm),
    ]
    verified_dates = [str(item.get("verified_on", "")) for item in data["documents"] if item.get("verified_on")]
    checked_on = str(data.get("verified_on") or (max(verified_dates) if verified_dates else date.today().isoformat()))
    story.append(para(("출처 확인일: " if ko else "Source checking date: ") + checked_on))
    story.append(para("Framework roles" if not ko else "지침별 역할", heading))
    framework_names = {str(item["id"]): str(item["name"]) for item in data["frameworks"]}
    for framework in data["frameworks"]:
        story.append(para(f"{framework['name']} ({framework['year']})", subheading))
        story.append(para(framework["role_ko" if ko else "role_en"]))
        story.append(para(framework["application_ko" if ko else "application_en"]))
        story.append(para(framework["scope_ko" if ko else "scope_en"]))
        story.append(para(("공식 URL: " if ko else "Official URL: ") + str(framework["official_url"])))
        if framework.get("article_url"):
            story.append(para(("공식 논문 URL: " if ko else "Official article URL: ") + str(framework["article_url"])))

    story.append(para("외부 데이터만 사용할 수 있는 경우" if ko else "External-data-only route", heading))
    catalog_note = data.get("external_data_note_ko" if ko else "external_data_note_en")
    if catalog_note:
        story.append(para(catalog_note))
    story.append(para(
        "독립된 외부 기존 자료로 하는 평가는 후향적 외부검증으로 보고합니다. 미래 결과가 발생하기 전에 예측값을 실제로 고정·기록하지 않았다면 전향적 검증이라고 주장할 수 없습니다."
        if ko else
        "Evaluation on independent, pre-existing external data is retrospective external validation. Prospective validation cannot be claimed unless predictions were actually fixed and captured before the future outcomes occurred."
    ))
    story.append(PageBreak())
    story.append(para("원문 자료 목록" if ko else "Official-original document list", heading))
    if not data["documents"]:
        story.append(para("등록된 원문 없음" if ko else "No original documents are currently registered."))
    for document in data["documents"]:
        title = document["title_ko" if ko else "title_en"]
        framework_name = framework_names.get(str(document["framework"]), str(document["framework"]))
        block: list[Any] = [
            para(f"[{framework_name}] {title} — {document['version']} ({document['year']})", subheading),
            para(("문서 PDF URL: " if ko else "Document PDF URL: ") + str(document["url"])),
        ]
        acquisition_url = str(document.get("acquisition_url") or "")
        if acquisition_url and acquisition_url != str(document["url"]):
            block.append(para(("확보 출처: " if ko else "Acquisition source: ") + acquisition_url))
        if document.get("official_url") and str(document["official_url"]) not in {str(document["url"]), acquisition_url}:
            block.append(para(("공식 안내 페이지: " if ko else "Official information page: ") + str(document["official_url"])))
        if document.get("license"):
            block.append(para(("라이선스·권리 안내: " if ko else "License/rights note: ") + str(document["license"])))
        block.append(para(f"SHA-256: {document['sha256']} · verified_on: {document['verified_on']}"))
        notes = document["notes_ko" if ko else "notes_en"]
        if notes:
            block.append(para(notes))
        story.append(KeepTogether(block))

    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")

    def add_page_number(canvas: Any, _document: Any) -> None:
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.drawCentredString(A4[0] / 2, 7 * mm, str(canvas.getPageNumber()))
        canvas.restoreState()

    try:
        SimpleDocTemplate(
            str(temp), pagesize=A4, rightMargin=14 * mm, leftMargin=14 * mm,
            topMargin=14 * mm, bottomMargin=14 * mm,
            title="SleepWell Guideline Application Guide",
            author="SleepWell Academic Workbench",
        ).build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
        pdf_bytes = temp.read_bytes()
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("generated_guide_is_not_pdf")
        _write_exclusive(destination, pdf_bytes)
    finally:
        if temp.exists():
            temp.unlink()
    return destination
