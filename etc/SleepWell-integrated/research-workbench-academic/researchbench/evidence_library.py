"""Project-scoped, local evidence library with auditable retrieval.

Experiment facts do not belong here.  This store contains literature/technical
documents only.  Retrieved text is treated as data and is never executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_RATIO = 200
MAX_CHUNKS = 2_000
CHUNK_CHARS = 1_400
LEXICAL_RELEVANCE_THRESHOLD = 0.08
EMBEDDING_RELEVANCE_THRESHOLD = 0.30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("authors", "tags"):
        result[key] = json.loads(result.get(key) or "[]")
    result["active"] = bool(result.get("active"))
    return result


def _archive_guard(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        return
    with zipfile.ZipFile(path) as archive:
        total = sum(item.file_size for item in archive.infolist())
        compressed = sum(max(1, item.compress_size) for item in archive.infolist())
        if total > MAX_ARCHIVE_BYTES or total / max(1, compressed) > MAX_ARCHIVE_RATIO:
            raise ValueError("document_archive_limits_exceeded")


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _structural_blocks(path: Path) -> tuple[list[dict[str, Any]], str, str | None]:
    """Return blocks, extraction status, and an error code (never invented text)."""
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md", ".markdown"}:
            text = path.read_text(encoding="utf-8-sig")
            blocks: list[dict[str, Any]] = []
            section = ""
            for number, paragraph in enumerate(re.split(r"\n\s*\n", text), 1):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                if suffix != ".txt" and paragraph.startswith("#"):
                    first = paragraph.splitlines()[0]
                    section = first.lstrip("# ").strip()
                blocks.append({"text": paragraph, "locator_type": "paragraph",
                               "locator": f"paragraph:{number}", "section": section})
            return blocks, "extracted" if blocks else "empty", None
        if suffix == ".docx":
            _archive_guard(path)
            try:
                from docx import Document
            except ImportError:
                return [], "dependency_missing", "python_docx_not_installed"
            document = Document(path)
            blocks = []
            section = ""
            for number, paragraph in enumerate(document.paragraphs, 1):
                text = paragraph.text.strip()
                if not text:
                    continue
                if str(paragraph.style.name).lower().startswith("heading"):
                    section = text
                blocks.append({"text": text, "locator_type": "paragraph",
                               "locator": f"paragraph:{number}", "section": section})
            for table_number, table in enumerate(document.tables, 1):
                for row_number, row in enumerate(table.rows, 1):
                    text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                    if text:
                        blocks.append({"text": text, "locator_type": "table_row",
                                       "locator": f"table:{table_number}:row:{row_number}", "section": section})
            return blocks, "extracted" if blocks else "empty", None
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return [], "dependency_missing", "pypdf_not_installed"
            reader = PdfReader(path)
            blocks = []
            for page_index, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append({"text": text, "locator_type": "pdf_page",
                                   "locator": f"pdf_page:{page_index}", "section": ""})
            if not blocks and reader.pages:
                return [], "ocr_needed", "pdf_has_pages_but_no_extractable_text"
            return blocks, "extracted" if blocks else "empty", None
        return [], "unsupported", "unsupported_document_type"
    except Exception as error:
        return [], "failed", f"{type(error).__name__}:{error}"


def _chunks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for block in blocks:
        text = re.sub(r"[ \t]+", " ", block["text"]).strip()
        if not text:
            continue
        pieces = [text[start:start + CHUNK_CHARS] for start in range(0, len(text), CHUNK_CHARS)]
        for index, piece in enumerate(pieces, 1):
            locator = block["locator"] + (f":part:{index}" if len(pieces) > 1 else "")
            chunks.append({**block, "text": piece, "locator": locator})
            if len(chunks) > MAX_CHUNKS:
                raise ValueError("document_chunk_limit_exceeded")
    return chunks


class EvidenceLibrary:
    """A document library whose IDs and reads are confined to one project."""

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path).expanduser().resolve()
        if not self.project_path.exists() or not self.project_path.is_dir():
            raise FileNotFoundError(f"project_not_found:{self.project_path}")
        self.root = self.project_path / "research"
        self.source_dir = self.root / "evidence_sources"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "evidence.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.project_id = ""
        self._schema()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "EvidenceLibrary":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(
          source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
          authors TEXT NOT NULL, organization TEXT, published_date TEXT, version TEXT NOT NULL,
          language TEXT, doi TEXT, url TEXT, local_name TEXT, stored_path TEXT,
          original_sha256 TEXT, stored_sha256 TEXT, document_type TEXT NOT NULL,
          domain TEXT, tags TEXT NOT NULL, rights TEXT, license TEXT,
          extraction_tool TEXT, extraction_status TEXT NOT NULL, extraction_error TEXT,
          indexed_at TEXT, accessed_at TEXT, active INTEGER NOT NULL,
          supersedes_source_id TEXT, tombstoned_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks(
          chunk_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
          locator_type TEXT NOT NULL, locator TEXT NOT NULL, section TEXT,
          FOREIGN KEY(source_id) REFERENCES sources(source_id)
        );
        CREATE TABLE IF NOT EXISTS searches(
          search_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, query TEXT NOT NULL,
          filters_json TEXT NOT NULL, settings_json TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS search_hits(
          search_id TEXT NOT NULL, rank INTEGER NOT NULL, chunk_id TEXT NOT NULL,
          relevance_score REAL NOT NULL, lexical_score REAL, embedding_score REAL,
          selection_reason TEXT NOT NULL, PRIMARY KEY(search_id, rank)
        );
        """)
        current = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if current and int(current[0]) != SCHEMA_VERSION:
            raise ValueError("unsupported_evidence_schema")
        self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        identity = self.db.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
        existing = [row[0] for row in self.db.execute("SELECT DISTINCT project_id FROM sources")]
        if identity:
            self.project_id = str(identity[0])
            if existing and existing != [self.project_id]:
                raise ValueError("evidence_project_identity_mismatch")
        elif existing:
            if len(existing) != 1:
                raise ValueError("evidence_project_identity_ambiguous")
            self.project_id = str(existing[0])
            self.db.execute("INSERT INTO metadata(key,value) VALUES('project_id',?)", (self.project_id,))
        else:
            self.project_id = uuid.uuid4().hex[:16]
            self.db.execute("INSERT INTO metadata(key,value) VALUES('project_id',?)", (self.project_id,))
        self.db.commit()

    def _source_id(self, digest: str, version: str) -> str:
        return f"src_{self.project_id}_{_sha_text(digest + '|' + version)[:20]}"

    def import_document(self, path: str | Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = dict(metadata or {})
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("document_file_size_limit_exceeded")
        _archive_guard(source)
        content = source.read_bytes()
        digest = _sha_bytes(content)
        version = str(metadata.get("version") or "1")
        source_id = self._source_id(digest, version)
        existing = self.get_source(source_id, include_inactive=True)
        if existing:
            return existing
        stored = self.source_dir / f"{digest}{source.suffix.lower()}"
        if stored.exists() and _sha_bytes(stored.read_bytes()) != digest:
            raise ValueError("stored_document_hash_mismatch")
        if not stored.exists():
            temporary = stored.with_suffix(stored.suffix + ".tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, stored)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        blocks, status, error = _structural_blocks(stored)
        chunks = _chunks(blocks)
        created = _now()
        title = str(metadata.get("title") or source.stem)
        authors = metadata.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        tags = metadata.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        supersedes = metadata.get("supersedes_source_id")
        if supersedes and not self.get_source(str(supersedes), include_inactive=True):
            raise ValueError("superseded_source_not_found")
        relative = stored.relative_to(self.project_path).as_posix()
        tool = "builtin-text/1"
        if source.suffix.lower() == ".pdf":
            try:
                import pypdf
                tool = f"pypdf/{pypdf.__version__}"
            except ImportError:
                tool = "pypdf/not-installed"
        elif source.suffix.lower() == ".docx":
            try:
                import docx
                tool = f"python-docx/{docx.__version__}"
            except ImportError:
                tool = "python-docx/not-installed"
        with self.db:
            self.db.execute("""INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                source_id, self.project_id, title, _json(authors), metadata.get("organization"),
                metadata.get("published_date"), version, metadata.get("language"), metadata.get("doi"),
                metadata.get("url"), source.name, relative, digest, digest, source.suffix.lower().lstrip("."),
                metadata.get("domain"), _json(tags), metadata.get("rights"), metadata.get("license"),
                tool, status, error, created if chunks else None, metadata.get("accessed_at") or created,
                1, supersedes, None, created,
            ))
            for ordinal, chunk in enumerate(chunks, 1):
                chunk_id = f"chk_{self.project_id}_{_sha_text(source_id + '|' + chunk['locator'] + '|' + chunk['text'])[:24]}"
                self.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", (
                    chunk_id, self.project_id, source_id, ordinal, chunk["text"], _sha_text(chunk["text"]),
                    chunk["locator_type"], chunk["locator"], chunk.get("section") or None,
                ))
            if supersedes:
                self.db.execute("UPDATE sources SET active=0 WHERE source_id=? AND project_id=?", (supersedes, self.project_id))
        return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]

    def register_metadata(self, *, title: str, doi: str | None = None, url: str | None = None,
                          metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Register a citation lead without claiming full-text extraction."""
        values = dict(metadata or {})
        version = str(values.get("version") or "1")
        identity = _sha_text((doi or "") + "|" + (url or "") + "|" + title)
        source_id = self._source_id(identity, version)
        existing = self.get_source(source_id, include_inactive=True)
        if existing:
            return existing
        created = _now()
        authors = values.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        tags = values.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        supersedes = values.get("supersedes_source_id")
        if supersedes and not self.get_source(str(supersedes), include_inactive=True):
            raise ValueError("superseded_source_not_found")
        with self.db:
            self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                source_id, self.project_id, title, _json(authors), values.get("organization"),
                values.get("published_date"), version, values.get("language"), doi, url, None, None, None, None,
                "metadata", values.get("domain"), _json(tags), values.get("rights"),
                values.get("license"), "metadata-only", "metadata_only", None, None,
                values.get("accessed_at") or created, 1, supersedes, None, created,
            ))
            if supersedes:
                self.db.execute("UPDATE sources SET active=0 WHERE source_id=? AND project_id=?", (supersedes, self.project_id))
        return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]

    def list_sources(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources WHERE project_id=?" + ("" if include_inactive else " AND active=1") + " ORDER BY created_at"
        return [_row(item) for item in self.db.execute(sql, (self.project_id,)).fetchall()]  # type: ignore[misc]

    def get_source(self, source_id: str, include_inactive: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM sources WHERE source_id=? AND project_id=?" + ("" if include_inactive else " AND active=1")
        return _row(self.db.execute(sql, (source_id, self.project_id)).fetchone())

    def set_active(self, source_id: str, active: bool) -> dict[str, Any]:
        source = self.get_source(source_id, include_inactive=True)
        if not source:
            raise KeyError("source_not_found")
        if active and source.get("extraction_status") in {"deleted", "purge_pending"}:
            raise ValueError("deleted_source_cannot_be_reactivated")
        with self.db:
            self.db.execute("UPDATE sources SET active=? WHERE source_id=? AND project_id=?",
                            (int(active), source_id, self.project_id))
        return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]

    def delete_source(self, source_id: str) -> dict[str, Any]:
        source = self.get_source(source_id, include_inactive=True)
        if not source:
            raise KeyError("source_not_found")
        stored_path = source.get("stored_path")
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE source_id=? AND project_id=?", (source_id, self.project_id))
            self.db.execute("""UPDATE sources SET active=0, extraction_status='purge_pending',
                              extraction_error=NULL, tombstoned_at=COALESCE(tombstoned_at,?)
                              WHERE source_id=? AND project_id=?""", (_now(), source_id, self.project_id))
        if not stored_path:
            with self.db:
                self.db.execute("""UPDATE sources SET stored_sha256=NULL,extraction_status='deleted',
                                  extraction_error=NULL WHERE source_id=? AND project_id=?""",
                                (source_id, self.project_id))
            return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]
        other_reference = self.db.execute(
            """SELECT 1 FROM sources WHERE project_id=? AND source_id<>? AND stored_path=?
                 AND active=1 AND extraction_status NOT IN ('deleted','purge_pending') LIMIT 1""",
            (self.project_id, source_id, stored_path),
        ).fetchone()
        if other_reference:
            with self.db:
                self.db.execute("""UPDATE sources SET stored_path=NULL,stored_sha256=NULL,
                                  extraction_status='deleted',
                                  extraction_error='retained_for_other_authorized_source'
                                  WHERE source_id=? AND project_id=?""", (source_id, self.project_id))
            return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]
        candidate = (self.project_path / stored_path).resolve()
        if not candidate.is_relative_to(self.source_dir.resolve()):
            with self.db:
                self.db.execute("""UPDATE sources SET extraction_error='purge_failed:invalid_stored_path'
                                  WHERE source_id=? AND project_id=?""", (source_id, self.project_id))
            return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            with self.db:
                self.db.execute("""UPDATE sources SET extraction_error=?
                                  WHERE source_id=? AND project_id=?""",
                                (f"purge_failed:{type(error).__name__}", source_id, self.project_id))
            return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]
        with self.db:
            self.db.execute("""UPDATE sources SET stored_path=NULL,stored_sha256=NULL,
                              extraction_status='deleted',extraction_error=NULL
                              WHERE source_id=? AND project_id=?""", (source_id, self.project_id))
        return self.get_source(source_id, include_inactive=True)  # type: ignore[return-value]

    def get_chunk(self, chunk_id: str) -> dict[str, Any]:
        row = self.db.execute("""SELECT c.*,s.title,s.version,s.original_sha256,s.stored_sha256,s.stored_path,s.active
                               FROM chunks c JOIN sources s ON s.source_id=c.source_id
                               WHERE c.chunk_id=? AND c.project_id=? AND s.project_id=?""",
                              (chunk_id, self.project_id, self.project_id)).fetchone()
        if not row:
            raise KeyError("chunk_not_found_for_project")
        result = dict(row)
        if not result["active"]:
            raise PermissionError("source_inactive")
        if _sha_text(result["text"]) != result["text_sha256"]:
            raise ValueError("chunk_hash_mismatch")
        if result.get("stored_path"):
            stored = (self.project_path / result["stored_path"]).resolve()
            if not stored.is_relative_to(self.source_dir.resolve()) or not stored.is_file():
                raise ValueError("stored_document_missing")
            if _sha_bytes(stored.read_bytes()) != result["stored_sha256"]:
                raise ValueError("stored_document_hash_mismatch")
        return result

    def list_chunks(self, source_id: str) -> list[dict[str, Any]]:
        source = self.get_source(source_id)
        if not source:
            raise PermissionError("source_not_active")
        return [self.get_chunk(row[0]) for row in self.db.execute(
            "SELECT chunk_id FROM chunks WHERE source_id=? AND project_id=? ORDER BY ordinal", (source_id, self.project_id))]

    def search(self, query: str, *, lang: str | None = None, domain: str | None = None,
               version: str | None = None,
               top_k: int = 5, source_ids: list[str] | None = None,
               embedding_model_path: str | Path | None = None) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("empty_search_query")
        if not 1 <= int(top_k) <= 100:
            raise ValueError("top_k_out_of_range")
        conditions = ["s.project_id=?", "c.project_id=?", "s.active=1", "s.extraction_status='extracted'"]
        params: list[Any] = [self.project_id, self.project_id]
        if lang:
            conditions.append("s.language=?"); params.append(lang)
        if domain:
            conditions.append("s.domain=?"); params.append(domain)
        if version:
            conditions.append("s.version=?"); params.append(version)
        if source_ids is not None:
            if not source_ids:
                conditions.append("0")
            else:
                conditions.append("s.source_id IN (%s)" % ",".join("?" for _ in source_ids)); params.extend(source_ids)
        raw_candidates = self.db.execute(f"""SELECT c.*,s.title,s.version,s.language,s.domain
                                             FROM chunks c JOIN sources s ON s.source_id=c.source_id
                                             WHERE {' AND '.join(conditions)} ORDER BY c.chunk_id""", params).fetchall()
        candidates = []
        exclusions = []
        for candidate in raw_candidates:
            try:
                self.get_chunk(candidate["chunk_id"])
            except (KeyError, PermissionError, ValueError) as error:
                exclusions.append({"chunk_id": candidate["chunk_id"], "source_id": candidate["source_id"],
                                   "reason": str(error)})
            else:
                candidates.append(candidate)
        settings: dict[str, Any] = {"retrieval": "lexical_tfidf_word_char", "score_meaning": "relevance_not_probability",
                                    "embedding": None, "reranker": None,
                                    "relevance_gate": {"lexical_minimum": LEXICAL_RELEVANCE_THRESHOLD,
                                                       "embedding_cosine_minimum": None,
                                                       "preregistered": True},
                                    "candidate_exclusions": exclusions}
        hits: list[dict[str, Any]] = []
        if candidates:
            texts = [row["text"] for row in candidates]
            word = TfidfVectorizer(lowercase=True, ngram_range=(1, 2), token_pattern=r"(?u)\b\w+\b")
            char = TfidfVectorizer(lowercase=True, analyzer="char_wb", ngram_range=(2, 5))
            try:
                word_score = (word.fit_transform(texts) @ word.transform([query]).T).toarray().ravel()
            except ValueError:
                word_score = np.zeros(len(texts))
            char_score = (char.fit_transform(texts) @ char.transform([query]).T).toarray().ravel()
            lexical = 0.65 * word_score + 0.35 * char_score
            embedding = None
            combined = lexical.copy()
            eligible = lexical >= LEXICAL_RELEVANCE_THRESHOLD
            if embedding_model_path is not None:
                model_path = Path(embedding_model_path).expanduser().resolve()
                if not model_path.is_dir():
                    raise FileNotFoundError("local_embedding_model_not_found")
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as error:
                    raise RuntimeError("sentence_transformers_not_installed") from error
                config_path = model_path / "config.json"
                config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
                identity_text = " ".join((str(model_path), str(config.get("_name_or_path", "")))).lower()
                uses_e5_prefix = "e5" in identity_text
                encoded_query = f"query: {query}" if uses_e5_prefix else query
                encoded_texts = [f"passage: {text}" for text in texts] if uses_e5_prefix else texts
                model = SentenceTransformer(str(model_path), local_files_only=True)
                vectors = model.encode([encoded_query, *encoded_texts], normalize_embeddings=True)
                embedding = np.asarray(vectors[1:]) @ np.asarray(vectors[0])
                # Reciprocal-rank fusion makes unlike score scales explicit.
                lexical_rank = np.argsort(np.argsort(-lexical)) + 1
                embedding_rank = np.argsort(np.argsort(-embedding)) + 1
                combined = 1 / (60 + lexical_rank) + 1 / (60 + embedding_rank)
                eligible = eligible | (embedding >= EMBEDDING_RELEVANCE_THRESHOLD)
                settings.update(retrieval="hybrid_rrf", embedding={
                    "path": str(model_path), "local_files_only": True,
                    "model_sha256": _directory_sha256(model_path),
                    "revision": config.get("_commit_hash") or config.get("revision") or "not_recorded_by_model",
                    "input_prefix": "e5_query_passage" if uses_e5_prefix else "none",
                })
                settings["relevance_gate"]["embedding_cosine_minimum"] = EMBEDDING_RELEVANCE_THRESHOLD
            order = [index for index in np.argsort(-combined, kind="stable") if eligible[index]][:top_k]
            for rank, index in enumerate(order, 1):
                item = dict(candidates[int(index)])
                hits.append({"rank": rank, "chunk_id": item["chunk_id"], "source_id": item["source_id"],
                             "title": item["title"], "version": item["version"], "section": item["section"],
                             "locator_type": item["locator_type"], "locator": item["locator"], "text": item["text"],
                             "relevance_score": float(combined[index]), "lexical_score": float(lexical[index]),
                             "embedding_score": None if embedding is None else float(embedding[index]),
                             "selection_reason": "highest_relevance_after_filters_and_preregistered_gate"})
        status = "ok" if hits else "no_evidence"
        search_id = f"search_{uuid.uuid4()}"
        filters = {"language": lang, "domain": domain, "version": version, "source_ids": source_ids}
        with self.db:
            self.db.execute("INSERT INTO searches VALUES(?,?,?,?,?,?,?)",
                            (search_id, self.project_id, query, _json(filters), _json(settings), status, _now()))
            for hit in hits:
                self.db.execute("INSERT INTO search_hits VALUES(?,?,?,?,?,?,?)", (
                    search_id, hit["rank"], hit["chunk_id"], hit["relevance_score"], hit["lexical_score"],
                    hit["embedding_score"], hit["selection_reason"],
                ))
        return {"search_id": search_id, "query": query, "filters": filters, "settings": settings,
                "status": status, "hits": hits}

    def search_log(self, search_id: str) -> dict[str, Any]:
        record = self.db.execute("SELECT * FROM searches WHERE search_id=? AND project_id=?",
                                 (search_id, self.project_id)).fetchone()
        if not record:
            raise KeyError("search_not_found_for_project")
        result = dict(record)
        result["filters"] = json.loads(result.pop("filters_json")); result["settings"] = json.loads(result.pop("settings_json"))
        result["hits"] = [dict(row) for row in self.db.execute(
            """SELECT h.*,
                      CASE WHEN c.chunk_id IS NOT NULL AND s.active=1
                                AND s.extraction_status='extracted' THEN 1 ELSE 0 END AS currently_valid
                 FROM search_hits h
                 LEFT JOIN chunks c ON c.chunk_id=h.chunk_id AND c.project_id=?
                 LEFT JOIN sources s ON s.source_id=c.source_id AND s.project_id=?
                WHERE h.search_id=? ORDER BY h.rank""",
            (self.project_id, self.project_id, search_id))]
        return result

    def snapshot(self) -> dict[str, Any]:
        sources = self.list_sources(include_inactive=True)
        payload = [{key: source.get(key) for key in ("source_id", "version", "original_sha256", "active", "extraction_status")}
                   for source in sources]
        return {"schema_version": SCHEMA_VERSION, "project_id": self.project_id, "created_at": _now(),
                "sources": payload, "snapshot_sha256": _sha_text(_json(payload))}


def run_evaluation(library: EvidenceLibrary, fixture_path: str | Path, *, split: str = "test", top_k: int = 3) -> dict[str, Any]:
    """Deterministic retrieval evaluation; semantic support still needs human review."""
    fixture_path = Path(fixture_path).resolve()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    source_map: dict[str, str] = {}
    for document in fixture["documents"]:
        path = (fixture_path.parent / document["file"]).resolve()
        source = library.import_document(path, document.get("metadata"))
        source_map[document["key"]] = source["source_id"]
    questions = [item for item in fixture["questions"] if item["split"] == split]
    reciprocal: list[float] = []
    recalls: list[float] = []
    no_evidence_correct = 0
    no_evidence_count = 0
    cases = []
    for item in questions:
        response = library.search(item["query"], top_k=top_k, lang=item.get("language"))
        returned = [(hit["source_id"], hit["locator"]) for hit in response["hits"]]
        expected_anchors = [
            (source_map[anchor["source"]], anchor["locator"])
            for anchor in item.get("expected_anchors", [])
        ]
        if not expected_anchors:
            expected_anchors = [(source_map[key], None) for key in item.get("expected_sources", [])]
        if expected_anchors:
            def relevant(hit: tuple[str, str], gold: tuple[str, str | None]) -> bool:
                return hit[0] == gold[0] and (gold[1] is None or hit[1] == gold[1])

            found = [gold for gold in expected_anchors if any(relevant(hit, gold) for hit in returned)]
            ranks = [rank for rank, hit in enumerate(returned, 1)
                     if any(relevant(hit, gold) for gold in expected_anchors)]
            recalls.append(len(found) / len(expected_anchors))
            reciprocal.append(1 / min(ranks) if ranks else 0.0)
        else:
            no_evidence_count += 1
            no_evidence_correct += int(response["status"] == "no_evidence")
        cases.append({"id": item["id"], "status": response["status"],
                      "returned_anchors": [{"source_id": source, "locator": locator}
                                           for source, locator in returned],
                      "expected_anchors": [{"source_id": source, "locator": locator}
                                           for source, locator in expected_anchors],
                      "automated_retrieval_only": True})
    denominator = len(questions)
    return {"fixture_version": fixture["version"], "split": split, "top_k": top_k, "denominator": denominator,
            "answerable_denominator": len(recalls), "no_evidence_denominator": no_evidence_count,
            "recall_at_k": sum(recalls) / len(recalls) if recalls else None,
            "mrr": sum(reciprocal) / len(reciprocal) if reciprocal else None,
            "no_evidence_accuracy": no_evidence_correct / no_evidence_count if no_evidence_count else None,
            "cases": cases,
            "semantic_support_review": "not_performed"}
