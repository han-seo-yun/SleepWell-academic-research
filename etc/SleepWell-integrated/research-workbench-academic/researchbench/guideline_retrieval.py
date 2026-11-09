"""Local, provenance-bound retrieval for the official TRIPOD+AI/PROBAST+AI PDFs.

This module retrieves reporting guidance only.  Model metrics and checklist
judgments remain owned by the verified evaluation and guideline-audit snapshots.
No text or project data is sent to an external service.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

from .evidence_library import EvidenceLibrary
from .guideline_library import catalog, resource_status


OFFICIAL_RETRIEVAL_DOCUMENTS = (
    "tripod_checklist",
    "tripod_expanded",
    "probast_tool",
    "probast_explanation",
)

_QUERY_BY_DOCUMENT = {
    "tripod_checklist": "model performance measures confidence intervals calibration",
    "tripod_expanded": "external evaluation model performance limitations applicability",
    "probast_tool": "external evaluation participants predictors outcome analysis risk of bias",
    "probast_explanation": "evaluation data participants predictors outcome analysis applicability",
}


def _short_excerpt(text: str, maximum_words: int = 20) -> str:
    """Return a contiguous source substring capped at ``maximum_words``."""
    matches = list(re.finditer(r"\S+", text))
    if not matches:
        return ""
    end = matches[min(maximum_words, len(matches)) - 1].end()
    return text[:end].strip()


def _relevant_excerpt(text: str, query: str, maximum_words: int = 20) -> str:
    """Select the most query-relevant contiguous word window from a retrieved chunk."""
    words = list(re.finditer(r"(?u)\b[\w+-]+\b", text))
    query_terms = {
        match.group(0).lower()
        for match in re.finditer(r"(?u)\b[\w+-]+\b", query)
        if len(match.group(0)) >= 4
    }
    if not words or not query_terms:
        return _short_excerpt(text, maximum_words)
    best: tuple[tuple[int, int, int], int, int] | None = None
    for start in range(len(words)):
        end = min(len(words), start + maximum_words)
        values = [match.group(0).lower() for match in words[start:end]]
        exact = len(query_terms.intersection(values))
        partial = sum(
            1 for term in query_terms
            if any(term in value or value in term for value in values if len(value) >= 4)
        )
        window = text[words[start].start():words[end - 1].end()]
        # Prefer actual prose over table/header fragments when relevance ties.
        prose = int("." in window or "," in window or ";" in window)
        score = (exact, partial, prose)
        if best is None or score > best[0]:
            best = (score, words[start].start(), words[end - 1].end())
    if best is None or best[0][:2] == (0, 0):
        return _short_excerpt(text, maximum_words)
    return text[best[1]:best[2]].strip()


def _audit_summary(audit: dict[str, Any] | None) -> dict[str, Any] | None:
    """Summarize structured judgments without placing them in the text index."""
    if audit is None:
        return None
    tripod = audit.get("tripod", {}).get("items", [])
    probast = audit.get("probast", {}).get("questions", [])
    overall = audit.get("probast", {}).get("overall_judgments", [])
    return {
        "audit_id": audit.get("audit_id"),
        "artifact_sha256": audit.get("artifact_sha256"),
        "tripod_total": len(tripod),
        "tripod_status_counts": dict(sorted(Counter(str(row.get("status")) for row in tripod).items())),
        "probast_total": len(probast),
        "probast_answer_counts": dict(sorted(Counter(str(row.get("answer")) for row in probast).items())),
        "overall_judgments": [
            {"domain_id": row.get("domain_id"), "judgment": row.get("judgment")}
            for row in overall
        ],
        "indexed": False,
        "reason": "Checklist judgments are copied from the verified audit snapshot, not inferred by retrieval.",
    }


def prepare_guideline_retrieval(
    project_path: str | Path,
    *,
    audit: dict[str, Any] | None = None,
    embedding_model_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify, index, and search the four official reporting-assessment PDFs.

    The returned citations can be passed directly to ``ReportService.create`` or
    its evidence-filled gradual report path.  Each official source contributes at
    most one excerpt, capped at 20 words.
    """
    project = Path(project_path).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    documents = {str(row["id"]): row for row in catalog()["documents"]}
    sources: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    searches: list[dict[str, Any]] = []

    with EvidenceLibrary(project) as library:
        for document_id in OFFICIAL_RETRIEVAL_DOCUMENTS:
            document = documents[document_id]
            status = resource_status(document_id)
            if not status.get("valid") or status.get("sha256") != document.get("sha256"):
                raise ValueError(f"official_guideline_verification_failed:{document_id}")
            source = library.import_document(
                str(status["path"]),
                {
                    "title": document["title_en"],
                    "organization": "TRIPOD Statement" if document["framework"] == "tripod" else "PROBAST",
                    "published_date": str(document["year"]),
                    "version": str(document["version"]),
                    "language": "en",
                    "url": document["official_url"],
                    "domain": "prediction_model_guideline",
                    "tags": ["official_guideline", document["framework"], document_id],
                    "rights": document.get("license"),
                    "license": document.get("license"),
                },
            )
            if source.get("original_sha256") != document["sha256"]:
                raise ValueError(f"indexed_guideline_hash_mismatch:{document_id}")
            # Revalidate the stored immutable copy; import_document returns existing
            # metadata on repeat calls, so this also detects later file corruption.
            library.list_chunks(str(source["source_id"]))
            sources.append(
                {
                    "document_id": document_id,
                    "source_id": source["source_id"],
                    "title": source["title"],
                    "version": source["version"],
                    "sha256": source["original_sha256"],
                    "official_url": document["official_url"],
                    "extraction_status": source["extraction_status"],
                }
            )
            result = library.search(
                _QUERY_BY_DOCUMENT[document_id],
                lang="en",
                domain="prediction_model_guideline",
                source_ids=[str(source["source_id"])],
                top_k=1,
                embedding_model_path=embedding_model_path,
            )
            searches.append(
                {
                    "document_id": document_id,
                    "search_id": result["search_id"],
                    "query": result["query"],
                    "status": result["status"],
                    "retrieval": result["settings"]["retrieval"],
                    "hit_count": len(result["hits"]),
                }
            )
            if result["hits"]:
                hit = result["hits"][0]
                citations.append(
                    {
                        "document_id": document_id,
                        "chunk_id": hit["chunk_id"],
                        "search_id": result["search_id"],
                        "excerpt": _relevant_excerpt(
                            str(hit["text"]), _QUERY_BY_DOCUMENT[document_id]
                        ),
                        "locator": hit["locator"],
                        "source_id": hit["source_id"],
                    }
                )

        snapshot = library.snapshot()

    return {
        "status": "ready" if len(citations) == len(OFFICIAL_RETRIEVAL_DOCUMENTS) else "partial",
        "generation": "deterministic_template_no_llm",
        "network_transfer": False,
        "documents_requested": list(OFFICIAL_RETRIEVAL_DOCUMENTS),
        "sources": sources,
        "searches": searches,
        "citations": citations,
        "audit_summary": _audit_summary(audit),
        "retrieval_snapshot": snapshot,
        "feasibility": {
            "current": "Local retrieval-grounded deterministic report generation is supported.",
            "full_rag": False,
            "full_rag_reason": (
                "The current path retrieves evidence but does not invoke a generative language model; "
                "therefore it must not be described as full retrieval-augmented generation."
            ),
            "future_option": (
                "A separately enabled local or approved language model may draft prose from these citations, "
                "with claim-level source checks and human review."
            ),
            "validation_boundary": (
                "Retrieval cannot create independent external or prospective validation evidence and cannot "
                "change TRIPOD+AI or PROBAST+AI judgments."
            ),
        },
    }


def search_prepared_guidelines(
    project_path: str | Path,
    query: str,
    *,
    source_ids: list[str],
    top_k: int = 3,
) -> dict[str, Any]:
    """Run an auditable query constrained to prepared official guideline sources."""
    with EvidenceLibrary(project_path) as library:
        active = {str(row["source_id"]): row for row in library.list_sources()}
        for source_id in source_ids:
            source = active.get(str(source_id))
            if source is None or "official_guideline" not in source.get("tags", []):
                raise ValueError("source_is_not_prepared_official_guideline")
        return library.search(
            query,
            lang="en",
            domain="prediction_model_guideline",
            source_ids=[str(value) for value in source_ids],
            top_k=top_k,
        )


__all__ = [
    "OFFICIAL_RETRIEVAL_DOCUMENTS",
    "prepare_guideline_retrieval",
    "search_prepared_guidelines",
]
