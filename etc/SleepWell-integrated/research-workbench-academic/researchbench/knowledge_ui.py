"""Tkinter evidence-library and report-authoring workspace.

The mixin is intentionally limited to UI composition and calls the project-scoped
EvidenceLibrary and ReportService for every persisted operation. Long-running file
extraction, retrieval, and export operations use a worker thread whose result is
marshalled back to Tk's main thread.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable

from .academic_backend import experiment_catalog, model_status
from .evidence_library import EvidenceLibrary
from .reporting import ReportService
from .report_ai_ui import ReportAIUIMixin


_LOCALE_PATH = Path(__file__).with_name("locales") / "knowledge_ui.json"
_KNOWLEDGE_LOCALE = json.loads(_LOCALE_PATH.read_text(encoding="utf-8"))
_TEMPLATES = ("gradual_guidelines", "research", "industrial", "model_card", "comparison")
_CHECKLIST_STATUSES = (
    "satisfied", "partially_satisfied", "not_satisfied", "not_applicable", "judgment_pending"
)


class KnowledgeUIMixin(ReportAIUIMixin):
    """Native UI surface mixed into AcademicApp by the host module."""

    def _kt(self, key: str, **values: Any) -> str:
        pair = _KNOWLEDGE_LOCALE.get(key)
        text = pair[1 if getattr(self, "lang", "ko") == "en" else 0] if pair else key
        return text.format(**values) if values else text

    def _kvar(self, key: str, default: str = "") -> tk.StringVar:
        variable = self.var(key)
        if not variable.get() and default:
            variable.set(default)
        return variable

    def _knowledge_state(self) -> dict[str, Any]:
        ui = self.project.state.setdefault("ui", {})
        return ui.setdefault("knowledge", {})

    def _restore_knowledge_state(self) -> None:
        state = self._knowledge_state()
        project_token = str(Path(self.project.path).resolve())
        previous_token = getattr(self, "_knowledge_project_token", None)
        if previous_token is not None and previous_token != project_token:
            for key, variable in list(getattr(self, "vars", {}).items()):
                if str(key).startswith("knowledge_"):
                    variable.set("")
            for attribute in (
                "knowledge_citations", "knowledge_search_result", "knowledge_search_hits",
                "knowledge_search_id", "knowledge_current_report", "knowledge_claims",
            ):
                if hasattr(self, attribute):
                    delattr(self, attribute)
        self._knowledge_project_token = project_token
        if not hasattr(self, "knowledge_citations"):
            self.knowledge_citations = list(state.get("citations") or [])
        for key, value in (state.get("drafts") or {}).items():
            if not self.var(key).get():
                self.var(key).set(str(value))

    def capture_knowledge_drafts(self) -> None:
        """Capture transient editors before navigation, language rebuild, or save."""
        self._restore_knowledge_state()
        editor_map = {
            "knowledge_report_editor": "knowledge_report_editor_draft",
            "knowledge_review_note_text": "knowledge_review_note",
        }
        for attribute, variable_key in editor_map.items():
            widget = getattr(self, attribute, None)
            try:
                if widget is not None and widget.winfo_exists():
                    self.var(variable_key).set(widget.get("1.0", "end-1c"))
            except tk.TclError:
                pass
        draft_keys = (
            "knowledge_evidence_title", "knowledge_evidence_authors", "knowledge_evidence_version",
            "knowledge_evidence_domain", "knowledge_evidence_rights", "knowledge_evidence_doi",
            "knowledge_evidence_url", "knowledge_evidence_language", "knowledge_query",
            "knowledge_filter_language", "knowledge_filter_domain", "knowledge_filter_version",
            "knowledge_embedding_path", "knowledge_top_k", "knowledge_report_template",
            "knowledge_report_question", "knowledge_research_domain", "knowledge_audience",
            "knowledge_intended_use", "knowledge_acceptance_criteria", "knowledge_acceptance_predeclared",
            "knowledge_medical_prediction", "knowledge_reviewer", "knowledge_review_note",
            "knowledge_target_language", "knowledge_export_mode", "knowledge_selected_report",
            "knowledge_guideline_retrieval",
            "knowledge_simple_report_source", "knowledge_simple_report_purpose",
            "knowledge_selected_source", "knowledge_selected_node", "knowledge_report_editor_draft",
        )
        state = self._knowledge_state()
        state["editor_context"] = {
            "report_id": self.var("knowledge_selected_report").get(),
            "node": self.var("knowledge_selected_node").get(),
        }
        state["drafts"] = {key: self.var(key).get() for key in draft_keys}
        state["citations"] = list(getattr(self, "knowledge_citations", []))
        state["last_search"] = getattr(self, "knowledge_search_id", state.get("last_search"))

    def _set_knowledge_status(self, key: str, detail: str = "") -> None:
        message = self._kt(key)
        if detail:
            message = f"{message}: {detail}"
        status = getattr(self, "status", None)
        if status is not None:
            status.set(message)
        self.var("knowledge_status").set(message)

    def _knowledge_async(
        self,
        work: Callable[[], Any],
        complete: Callable[[Any], None] | None = None,
    ) -> None:
        if getattr(self, "_knowledge_worker_active", False):
            return
        self._knowledge_worker_active = True
        self._set_knowledge_status("operation_running")
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()

        def run() -> None:
            try:
                messages.put(("ok", work()))
            except Exception as error:  # UI reports the bounded error without blocking dialogs
                messages.put(("error", error))

        threading.Thread(target=run, daemon=True, name="researchbench-knowledge").start()

        def poll() -> None:
            try:
                kind, value = messages.get_nowait()
            except queue.Empty:
                try:
                    self.root.after(50, poll)
                except tk.TclError:
                    self._knowledge_worker_active = False
                return
            self._knowledge_worker_active = False
            if kind == "error":
                self._set_knowledge_status("operation_failed", str(value))
                return
            if complete is not None:
                try:
                    complete(value)
                except Exception as error:
                    self._set_knowledge_status("operation_failed", str(error))

        self.root.after(20, poll)

    def _kfield(self, parent: tk.Misc, key: str, variable_key: str, default: str = "") -> ttk.Entry:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=self._kt(key), width=27).pack(side="left")
        entry = ttk.Entry(row, textvariable=self._kvar(variable_key, default))
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _kbutton(self, parent: tk.Misc, key: str, command: Callable[[], None]) -> ttk.Button:
        button = ttk.Button(parent, text=self._kt(key), command=lambda: self.safe(command))
        button.pack(side="left", padx=(0, 7), pady=4)
        return button

    def _ktree(
        self,
        parent: tk.Misc,
        columns: list[tuple[str, str, int]],
        *,
        height: int = 8,
        selectmode: str = "browse",
    ) -> ttk.Treeview:
        host = ttk.Frame(parent)
        host.pack(fill="both", expand=True, pady=4)
        names = [item[0] for item in columns]
        widget = ttk.Treeview(host, columns=names, show="headings", height=height, selectmode=selectmode)
        vertical = ttk.Scrollbar(host, orient="vertical", command=widget.yview)
        horizontal = ttk.Scrollbar(host, orient="horizontal", command=widget.xview)
        widget.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        widget.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)
        for name, label, width in columns:
            widget.heading(name, text=self._kt(label))
            widget.column(name, width=width, minwidth=70, stretch=True)
        return widget

    # Evidence workspace -------------------------------------------------
    def build_evidence(self, frame: tk.Misc) -> None:
        self._restore_knowledge_state()
        ttk.Label(frame, text=self._kt("evidence_help"), wraplength=1100, justify="left").pack(fill="x", pady=(0, 8))
        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)
        library_tab = ttk.Frame(notebook, padding=8)
        search_tab = ttk.Frame(notebook, padding=8)
        provenance_tab = ttk.Frame(notebook, padding=8)
        notebook.add(library_tab, text=self._kt("document_library"))
        notebook.add(search_tab, text=self._kt("evidence_search"))
        notebook.add(provenance_tab, text=self._kt("model_provenance"))
        self.knowledge_evidence_notebook = notebook
        self._build_library_tab(library_tab)
        self._build_search_tab(search_tab)
        self._build_provenance_tab(provenance_tab)
        self._refresh_sources()

    def _build_library_tab(self, parent: tk.Misc) -> None:
        form = ttk.LabelFrame(parent, text=self._kt("document_library"), padding=8)
        form.pack(fill="x")
        self._kfield(form, "title", "knowledge_evidence_title")
        self._kfield(form, "authors", "knowledge_evidence_authors")
        self._kfield(form, "version", "knowledge_evidence_version", "1")
        self._kfield(form, "domain", "knowledge_evidence_domain")
        self._kfield(form, "rights", "knowledge_evidence_rights")
        self._kfield(form, "doi", "knowledge_evidence_doi")
        self._kfield(form, "url", "knowledge_evidence_url")
        language_row = ttk.Frame(form)
        language_row.pack(fill="x", pady=2)
        ttk.Label(language_row, text=self._kt("language"), width=27).pack(side="left")
        language = ttk.Combobox(language_row, state="readonly", width=16)
        language["values"] = (self._kt("ko"), self._kt("en"))
        code = self._kvar("knowledge_evidence_language", getattr(self, "lang", "ko")).get()
        language.current(0 if code == "ko" else 1)
        language.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.var("knowledge_evidence_language").set("ko" if language.current() == 0 else "en"),
        )
        language.pack(side="left")
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=4)
        self._kbutton(actions, "import_document", self._choose_evidence_document)
        self._kbutton(actions, "register_metadata", self._register_metadata_async)
        self._kbutton(actions, "open_original", self._open_selected_source)
        self._kbutton(actions, "deactivate", lambda: self._set_selected_source_active(False))
        self._kbutton(actions, "reactivate", lambda: self._set_selected_source_active(True))
        self._kbutton(actions, "refresh", self._refresh_sources)
        self.knowledge_source_tree = self._ktree(
            parent,
            [
                ("title", "title", 210), ("version", "version", 80), ("type", "type", 90),
                ("extraction", "extraction", 130), ("status", "status", 90), ("source_id", "source_id", 270),
            ],
            height=9,
        )
        self.knowledge_source_tree.bind("<<TreeviewSelect>>", self._source_selected)
        self.knowledge_source_detail = tk.Text(parent, height=7, wrap="word", font=("Malgun Gothic", 9))
        self.knowledge_source_detail.pack(fill="both", expand=True, pady=(4, 0))

    def _source_metadata(self) -> dict[str, Any]:
        authors = [item.strip() for item in self.var("knowledge_evidence_authors").get().split(";") if item.strip()]
        return {
            "title": self.var("knowledge_evidence_title").get().strip(),
            "authors": authors,
            "version": self.var("knowledge_evidence_version").get().strip() or "1",
            "domain": self.var("knowledge_evidence_domain").get().strip() or None,
            "rights": self.var("knowledge_evidence_rights").get().strip() or None,
            "language": self.var("knowledge_evidence_language").get() or getattr(self, "lang", "ko"),
            "doi": self.var("knowledge_evidence_doi").get().strip() or None,
            "url": self.var("knowledge_evidence_url").get().strip() or None,
        }

    def _evidence_import_sync(self, path: str | Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with EvidenceLibrary(self.project.path) as library:
            return library.import_document(path, metadata or self._source_metadata())

    def _choose_evidence_document(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title=self._kt("import_document"),
            filetypes=[(self._kt("supported_documents"), "*.pdf *.docx *.md *.markdown *.txt")],
        )
        if not path:
            return
        metadata = self._source_metadata()
        self._knowledge_async(
            lambda: self._evidence_import_sync(path, metadata),
            lambda _source: (self._refresh_sources(), self._set_knowledge_status("document_registered")),
        )

    def _register_metadata_sync(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        values = metadata or self._source_metadata()
        title = str(values.get("title") or "").strip()
        doi = str(values.get("doi") or "").strip() or None
        url = str(values.get("url") or "").strip() or None
        if not title or not (doi or url):
            raise ValueError(self._kt("title_doi_required"))
        with EvidenceLibrary(self.project.path) as library:
            return library.register_metadata(title=title, doi=doi, url=url, metadata=values)

    def _register_metadata_async(self) -> None:
        metadata = self._source_metadata()
        self._knowledge_async(
            lambda: self._register_metadata_sync(metadata),
            lambda _source: (self._refresh_sources(), self._set_knowledge_status("metadata_registered")),
        )

    def _list_sources_sync(self) -> list[dict[str, Any]]:
        with EvidenceLibrary(self.project.path) as library:
            return library.list_sources(include_inactive=True)

    def _refresh_sources(self) -> None:
        tree = getattr(self, "knowledge_source_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        selected_id = self.var("knowledge_selected_source").get()
        sources = self._list_sources_sync()
        self.knowledge_sources = {source["source_id"]: source for source in sources}
        tree.delete(*tree.get_children())
        for source in sources:
            source_id = source["source_id"]
            tree.insert(
                "", "end", iid=source_id,
                values=(
                    source.get("title"), source.get("version"), source.get("document_type"),
                    source.get("extraction_status"), self._kt("active" if source.get("active") else "inactive"), source_id,
                ),
            )
        if selected_id and tree.exists(selected_id):
            tree.selection_set(selected_id)
            tree.see(selected_id)
            self._source_selected()
        if not sources:
            self._set_knowledge_status("no_sources")

    def _source_selected(self, _event: Any = None) -> None:
        selection = self.knowledge_source_tree.selection()
        if not selection:
            return
        source_id = selection[0]
        self.var("knowledge_selected_source").set(source_id)
        source = getattr(self, "knowledge_sources", {}).get(source_id)
        if source:
            self.knowledge_source_detail.configure(state="normal")
            self.knowledge_source_detail.delete("1.0", "end")
            detail = "\n".join(
                [
                    f"{self._kt('title')}: {source.get('title') or '—'}",
                    f"{self._kt('authors')}: {'; '.join(source.get('authors') or []) or '—'}",
                    f"{self._kt('version')}: {source.get('version') or '—'}",
                    f"{self._kt('type')}: {source.get('document_type') or '—'}",
                    f"{self._kt('extraction')}: {source.get('extraction_status') or '—'}",
                    f"{self._kt('status')}: {self._kt('active' if source.get('active') else 'inactive')}",
                    f"{self._kt('doi')}: {source.get('doi') or '—'}",
                    f"{self._kt('url')}: {source.get('url') or '—'}",
                    f"{self._kt('source_id')}: {source.get('source_id')}",
                    f"SHA-256: {source.get('original_sha256') or '—'}",
                ]
            )
            self.knowledge_source_detail.insert("1.0", detail)
            self.knowledge_source_detail.configure(state="disabled")

    def _set_selected_source_active(self, active: bool) -> None:
        source_id = self.var("knowledge_selected_source").get()
        if not source_id:
            raise ValueError(self._kt("no_selection"))
        with EvidenceLibrary(self.project.path) as library:
            library.set_active(source_id, active)
        self._refresh_sources()
        self._set_knowledge_status("source_updated")

    def _open_selected_source(self) -> None:
        source_id = self.var("knowledge_selected_source").get()
        if not source_id:
            raise ValueError(self._kt("no_selection"))
        with EvidenceLibrary(self.project.path) as library:
            source = library.get_source(source_id, include_inactive=True)
        relative = source.get("stored_path") if source else None
        if not relative:
            raise ValueError(self._kt("open_failed"))
        path = (Path(self.project.path) / relative).resolve()
        evidence_root = (Path(self.project.path) / "research" / "evidence_sources").resolve()
        if not path.is_relative_to(evidence_root) or not path.exists():
            raise ValueError(self._kt("open_failed"))
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:  # pragma: no cover - Windows is the supported desktop target
            raise OSError(self._kt("open_failed"))

    def _build_search_tab(self, parent: tk.Misc) -> None:
        self._kfield(parent, "query", "knowledge_query")
        self._kfield(parent, "filter_language", "knowledge_filter_language")
        self._kfield(parent, "filter_domain", "knowledge_filter_domain")
        self._kfield(parent, "filter_version", "knowledge_filter_version")
        self._kfield(parent, "embedding_path", "knowledge_embedding_path")
        self._kfield(parent, "top_k", "knowledge_top_k", "5")
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self._kbutton(actions, "search", self._search_evidence_async)
        self._kbutton(actions, "attach_citation", self._attach_selected_citation)
        self._kbutton(actions, "remove_citation", self._remove_selected_citation)
        self.knowledge_attachment_label = ttk.Label(actions)
        self.knowledge_attachment_label.pack(side="left", padx=8)
        self.knowledge_search_message = ttk.Label(parent, wraplength=1000, justify="left")
        self.knowledge_search_message.pack(fill="x", pady=4)
        self.knowledge_search_tree = self._ktree(
            parent,
            [
                ("title", "title", 180), ("locator", "locator", 160), ("relevance", "relevance", 90),
                ("excerpt", "excerpt", 550),
            ],
            height=10,
        )
        self.knowledge_search_tree.bind("<<TreeviewSelect>>", self._search_hit_selected)
        self.knowledge_search_detail = tk.Text(parent, height=7, wrap="word", font=("Malgun Gothic", 9))
        self.knowledge_search_detail.pack(fill="both", expand=True)
        self._refresh_attachment_label()

    def _evidence_search_sync(self, query: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        values = dict(filters or {})
        with EvidenceLibrary(self.project.path) as library:
            return library.search(
                query,
                lang=values.get("lang") or None,
                domain=values.get("domain") or None,
                version=values.get("version") or None,
                top_k=int(values.get("top_k") or 5),
                embedding_model_path=values.get("embedding_model_path") or None,
            )

    def _search_evidence_async(self) -> None:
        query = self.var("knowledge_query").get().strip()
        if not query:
            raise ValueError("empty_search_query")
        filters = {
            "lang": self.var("knowledge_filter_language").get().strip(),
            "domain": self.var("knowledge_filter_domain").get().strip(),
            "version": self.var("knowledge_filter_version").get().strip(),
            "top_k": self.var("knowledge_top_k").get().strip() or "5",
            "embedding_model_path": self.var("knowledge_embedding_path").get().strip(),
        }
        self._knowledge_async(
            lambda: self._evidence_search_sync(query, filters),
            lambda result: (self._populate_search_results(result), self._set_knowledge_status("search_complete")),
        )

    def _populate_search_results(self, result: dict[str, Any]) -> None:
        self.knowledge_search_result = result
        self.knowledge_search_id = result.get("search_id")
        self.knowledge_search_hits = {item["chunk_id"]: item for item in result.get("hits", [])}
        tree = self.knowledge_search_tree
        tree.delete(*tree.get_children())
        for hit in result.get("hits", []):
            tree.insert(
                "", "end", iid=hit["chunk_id"],
                values=(hit.get("title"), hit.get("locator"), f"{float(hit.get('relevance_score', 0)):.3f}", hit.get("text")),
            )
        self.knowledge_search_message.configure(
            text=self._kt("insufficient_evidence") if result.get("status") != "ok" else self._kt("semantic_pending")
        )
        self.capture_knowledge_drafts()

    def _search_hit_selected(self, _event: Any = None) -> None:
        selection = self.knowledge_search_tree.selection()
        if not selection:
            return
        hit = getattr(self, "knowledge_search_hits", {}).get(selection[0])
        if not hit:
            return
        self.knowledge_search_detail.configure(state="normal")
        self.knowledge_search_detail.delete("1.0", "end")
        self.knowledge_search_detail.insert("1.0", json.dumps(hit, ensure_ascii=False, indent=2))
        self.knowledge_search_detail.configure(state="disabled")

    def _attach_selected_citation(self) -> None:
        selection = self.knowledge_search_tree.selection()
        if not selection:
            raise ValueError(self._kt("no_selection"))
        hit = dict(self.knowledge_search_hits[selection[0]])
        hit["search_id"] = self.knowledge_search_id
        key = (hit["search_id"], hit["chunk_id"])
        existing = {(item.get("search_id"), item.get("chunk_id")) for item in self.knowledge_citations}
        if key not in existing:
            self.knowledge_citations.append(hit)
        self.capture_knowledge_drafts()
        self._refresh_attachment_label()
        self._set_knowledge_status("citation_attached")

    def _remove_selected_citation(self) -> None:
        selection = self.knowledge_search_tree.selection()
        if not selection:
            raise ValueError(self._kt("no_selection"))
        chunk_id = selection[0]
        self.knowledge_citations = [item for item in self.knowledge_citations if item.get("chunk_id") != chunk_id]
        self.capture_knowledge_drafts()
        self._refresh_attachment_label()

    def _refresh_attachment_label(self) -> None:
        label = getattr(self, "knowledge_attachment_label", None)
        if label is not None and label.winfo_exists():
            label.configure(text=self._kt("attached_count", count=len(getattr(self, "knowledge_citations", []))))

    def _build_provenance_tab(self, parent: tk.Misc) -> None:
        catalog = experiment_catalog()
        status = model_status()
        catalog_frame = ttk.LabelFrame(parent, text=self._kt("model_provenance"), padding=6)
        catalog_frame.pack(fill="x")
        self.knowledge_provenance_tree = self._ktree(
            catalog_frame,
            [
                ("family", "family", 230), ("models", "models", 90), ("mode", "mode", 190),
                ("status", "status", 140), ("note", "note", 470),
            ],
            height=10,
        )
        for index, row in enumerate(catalog.get("coverage_matrix", [])):
            self.knowledge_provenance_tree.insert(
                "", "end", iid=f"coverage_{index}",
                values=(row.get("family"), row.get("models"), row.get("mode"), self._kt(str(row.get("status"))), row.get("note")),
            )
        run_frame = ttk.LabelFrame(parent, text=self._kt("completed_run_provenance"), padding=6)
        run_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.knowledge_provenance_runs = self._ktree(
            run_frame,
            [("name", "run_name", 260), ("engine", "engine", 100), ("evaluation", "evaluation_type", 180), ("run_id", "run_id", 310)],
            height=6,
        )
        self.knowledge_provenance_runs.bind("<<TreeviewSelect>>", self._provenance_run_selected)
        self.knowledge_provenance_views = {str(view.get("run_id")): view for view in self.platform_runs()}
        for run_id, view in self.knowledge_provenance_views.items():
            self.knowledge_provenance_runs.insert(
                "", "end", iid=run_id,
                values=(view.get("name") or run_id, view.get("engine"), view.get("evaluation", {}).get("evaluation_type"), run_id),
            )
        self.knowledge_provenance_detail = self._ktree(
            run_frame, [("field", "field", 230), ("value", "details", 770)], height=8,
        )
        actions = ttk.Frame(run_frame)
        actions.pack(fill="x", pady=(4, 0))
        self._kbutton(actions, "open_original", self._open_selected_run_source)
        self._kbutton(actions, "show_advanced_json", self._show_provenance_json)
        self.knowledge_provenance_payload = {"catalog": catalog, "model_status": status}

    def _provenance_run_selected(self, _event: Any = None) -> None:
        selection = self.knowledge_provenance_runs.selection()
        if not selection:
            return
        view = self.knowledge_provenance_views.get(selection[0], {})
        self.knowledge_selected_provenance_run = view
        detail = self.knowledge_provenance_detail
        detail.delete(*detail.get_children())
        evaluation = view.get("evaluation", {})
        provenance = view.get("provenance", {})
        versions = provenance.get("versions") or provenance.get("model_version") or "—"
        rows = [
            ("run_id", view.get("run_id")), ("data_hash", view.get("data_hash")),
            ("model_version", json.dumps(versions, ensure_ascii=False) if isinstance(versions, (dict, list)) else versions),
            ("calculation", evaluation.get("calculation")), ("evaluation_type", evaluation.get("evaluation_type")),
            ("synthetic", evaluation.get("synthetic")), ("source_path", view.get("source_file")),
            ("result_hash", view.get("result_hash")), ("hash_verification", self._kt("verified")),
        ]
        for index, (key, value) in enumerate(rows):
            detail.insert("", "end", iid=f"detail_{index}", values=(self._kt(key), "—" if value is None else value))

    def _open_selected_run_source(self) -> None:
        view = getattr(self, "knowledge_selected_provenance_run", None) or {}
        path = Path(str(view.get("source_file") or "")).resolve()
        roots = [Path(self.project.path).resolve()]
        linked = self.var("linked_general").get().strip()
        if linked:
            roots.append(Path(linked).resolve())
        if not path.is_file() or path.suffix.lower() != ".json" or not any(path.is_relative_to(root) for root in roots):
            raise ValueError(self._kt("run_source_open_failed"))
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:  # pragma: no cover
            raise OSError(self._kt("run_source_open_failed"))

    def _show_provenance_json(self) -> None:
        window = tk.Toplevel(self.root)
        window.title(self._kt("advanced_json"))
        text_widget = tk.Text(window, wrap="none", font=("Consolas", 9))
        text_widget.pack(fill="both", expand=True)
        payload = dict(getattr(self, "knowledge_provenance_payload", {}))
        selected = getattr(self, "knowledge_selected_provenance_run", None)
        if selected:
            payload["selected_run"] = selected
        text_widget.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        text_widget.configure(state="disabled")

    # Compact report workflow -------------------------------------------
    def _simple_report_text(self, ko: str, en: str) -> str:
        return en if getattr(self, "lang", "ko") == "en" else ko

    def _simple_report_status(self, status: str) -> str:
        labels = {
            "automatic_check_passed": ("자동 검사 통과", "Automatic check passed"),
            "automatic_check_failed": ("자동 검사 실패", "Automatic check failed"),
            "draft": ("초안", "Draft"),
            "not_run": ("검사 전", "Not checked"),
            "user_review_recorded": ("사용자 검토 기록", "User review recorded"),
        }
        ko, en = labels.get(str(status), (str(status), str(status)))
        return self._simple_report_text(ko, en)

    def _simple_report_views(self) -> list[dict[str, Any]]:
        """Return completed runs that contain reportable metric evidence."""
        views = [
            view for view in self._knowledge_views()
            if any(metric.get("value") is not None and metric.get("field_path") for metric in view.get("metrics", []))
        ]

        def created_at(view: dict[str, Any]) -> str:
            source = Path(str(view.get("source_file") or ""))
            record = json.loads(source.read_text(encoding="utf-8"))
            return str(record.get("created") or record.get("created_at") or "")

        return sorted(views, key=created_at)

    def _simple_report_comparison_selection(self, view: dict[str, Any]) -> dict[str, Any] | None:
        """Capture settings for this exact run; never reuse another run's selection."""
        from .result_adapter import verify_run
        saved = self.project.state.get('comparison_selections', {}).get(view['run_id'])
        if saved is not None:
            if saved.get('run_id') != view['run_id'] or saved.get('result_hash') != view['result_hash']:
                raise ValueError('comparison_selection_run_mismatch')
            return copy.deepcopy(saved)
        record = verify_run(view)
        predictions = (record.get('result', {}).get('raw') or {}).get('predictions') or []
        if not predictions:
            return None
        return {'run_id': view['run_id'], 'result_hash': view['result_hash'],
                'model_ids': list(dict.fromkeys(str(row['model_id']) for row in predictions if row.get('model_id'))),
                'target': 'onset', 'averaging': 'participant_equal', 'tolerance': 30}

    def _simple_report_refresh_comparison(self) -> None:
        from .academic_plotting import model_label
        source = self.var('knowledge_simple_report_source').get()
        view = self.simple_report_sources.get(source)
        if not view:
            text = self._simple_report_text('gradual_v1 기준 평가 보고서입니다. 화면의 모델 비교 선택과 별개입니다.',
                                           'The gradual_v1 reference report is separate from the model comparison selection.') if source else ''
            enabled = bool(source)
        else:
            selection = self._simple_report_comparison_selection(view)
            enabled = selection is None or bool(selection['model_ids'])
            if selection is None:
                text = self._simple_report_text('원시 예측이 없는 과거 결과입니다. 저장된 지표 보고서를 만듭니다.',
                                               'This legacy result has no raw predictions; a stored-metrics report will be created.')
            elif not enabled:
                text = self._simple_report_text('선택된 모델이 없습니다. 모델 실험 화면에서 비교 모델을 선택하세요.',
                                               'No models selected. Select comparison models in the experiment screen.')
            else:
                models = ', '.join(model_label(model, self.lang) for model in selection['model_ids'])
                target = {'onset': ('취침', 'onset'), 'wake': ('기상', 'wake'), 'interval': ('취침-기상 구간', 'sleep interval')}[selection['target']]
                weight = ('참여자 동일 가중', 'equal participant weight') if selection['averaging'] == 'participant_equal' else ('밤 동일 가중', 'equal night weight')
                text = self._simple_report_text('PDF에 포함할 비교: ', 'Comparison included in PDF: ') + models
                text += f"\n{self._simple_report_text(*target)} · {self._simple_report_text(*weight)} · ±{selection['tolerance']:g} min"
                text += self._simple_report_text('\n지표·그래프 7종·모델 간 차이·TRIPOD+AI/PROBAST+AI 관련 근거와 보완사항을 자동으로 포함합니다.',
                                                '\nIncludes metrics, seven figures, model differences, relevant TRIPOD+AI/PROBAST+AI evidence and gaps.')
        self.simple_report_comparison_label.configure(text=text)
        self.simple_report_create_button.configure(state='normal' if enabled else 'disabled')
        if hasattr(self, 'report_ai_generate_button'):
            self._report_ai_refresh()

    def build_simple_reports(self, frame: tk.Misc) -> None:
        """Build the default one-screen result-to-PDF workflow."""
        self._restore_knowledge_state()
        intro_label = ttk.Label(
            frame,
            text=self._simple_report_text(
                "선택한 모델의 지표·비교 그래프·해석을 공식 TRIPOD+AI·PROBAST+AI 근거와 연결해 PDF로 만듭니다.",
                "Create a PDF linking the selected models' metrics, comparison plots and interpretation to official TRIPOD+AI and PROBAST+AI evidence.",
            ),
            wraplength=1050, justify="left", style="AcademicNote.TLabel",
        )
        intro_label.pack(fill="x", pady=(0, 8))
        method_label = ttk.Label(
            frame,
            text=self._simple_report_text(
                "키 없이 근거 기반 PDF를 만들거나, AI를 연결해 성능 분석·지침 검토·보고서 작성 에이전트를 실행할 수 있습니다.",
                "Create an evidence-based PDF without a key, or connect AI for performance analysis, guideline review and report writing.",
            ),
            wraplength=1050, justify="left",
        )
        method_label.pack(fill="x", pady=(0, 10))

        wrapped_labels = [intro_label, method_label]

        def resize_wrapped_labels(event: Any) -> None:
            width = max(240, int(event.width) - 25)
            for label in wrapped_labels:
                label.configure(wraplength=width)

        frame.bind("<Configure>", resize_wrapped_labels, add="+")

        setup = ttk.LabelFrame(frame, text=self._simple_report_text("보고서 만들기", "Create report"), padding=10)
        setup.pack(fill="x", pady=(0, 10))
        views = self._simple_report_views()
        self.simple_report_sources = {str(view["run_id"]): view for view in views}
        source_ids = [""] + list(self.simple_report_sources) + ["gradual_v1_reference"]
        source_labels = [self._simple_report_text("완료된 결과를 선택하세요", "Choose a completed result")] + [
            self._simple_report_text(
                f"실험 결과 · {view.get('name') or view['run_id']}",
                f"Experiment result · {view.get('name') or view['run_id']}",
            ) for view in views
        ] + [self._simple_report_text("기준 모델 · gradual_v1 평가", "Reference model · gradual_v1 evaluation")]
        selected_run = str(getattr(self, "selected_run", "") or "")
        if selected_run in self.simple_report_sources:
            preferred = selected_run
        elif selected_run:
            preferred = ""
        else:
            preferred = str(views[-1]["run_id"]) if views else "gradual_v1_reference"
        self.var("knowledge_simple_report_source").set(preferred)
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._simple_report_text("결과 선택", "Result source"), width=20).pack(side="left")
        self.simple_report_source_combo = ttk.Combobox(row, values=source_labels, state="readonly", width=64)
        self.simple_report_source_combo.current(source_ids.index(preferred))
        self.simple_report_source_combo.pack(side="left", fill="x", expand=True)

        def source_selected(_event=None):
            index = self.simple_report_source_combo.current()
            if index >= 0:
                self.var("knowledge_simple_report_source").set(source_ids[index])
                self._simple_report_refresh_comparison()

        self.simple_report_source_combo.bind("<<ComboboxSelected>>", source_selected)
        self.simple_report_comparison_label = ttk.Label(setup, wraplength=1050, justify='left', style='AcademicNote.TLabel')
        self.simple_report_comparison_label.pack(fill='x', pady=6)
        wrapped_labels.append(self.simple_report_comparison_label)
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._simple_report_text("보고 목적 (선택)", "Purpose (optional)"), width=20).pack(side="left")
        ttk.Entry(row, textvariable=self._kvar("knowledge_simple_report_purpose")).pack(side="left", fill="x", expand=True)
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=(8, 0))
        self.simple_report_create_button = ttk.Button(
            row, text=self._simple_report_text("키 없이 기본 PDF 저장", "Save basic PDF without AI"),
            command=lambda: self.safe(self._simple_report_create_async),
        )
        self.simple_report_create_button.configure(state="normal" if preferred else "disabled")
        self.simple_report_create_button.pack(side="left", padx=(0, 7))
        self._simple_report_refresh_comparison()
        self.simple_report_open_button = ttk.Button(
            row, text=self._simple_report_text("마지막 PDF 열기", "Open last PDF"),
            command=lambda: self.safe(self._simple_report_open_pdf),
        )
        self.simple_report_open_button.pack(side="left")
        self._simple_report_refresh_open_button()
        self._build_report_ai_controls(setup)
        output_label = ttk.Label(
            setup, textvariable=self._kvar("knowledge_report_output", ""), wraplength=1050, justify="left",
        )
        output_label.pack(fill="x", pady=(6, 0))
        wrapped_labels.append(output_label)
        ttk.Label(
            setup, textvariable=self._kvar("knowledge_status", ""), justify="left",
        ).pack(fill="x", pady=(3, 0))

        history = ttk.LabelFrame(frame, text=self._simple_report_text("최근 보고서", "Recent reports"), padding=8)
        history.pack(fill="both", expand=True)
        self.simple_report_history = ttk.Treeview(
            history, columns=("source", "status", "created"), show="headings", height=5,
        )
        for column, label, width in (
            ("source", self._simple_report_text("근거", "Source"), 360),
            ("status", self._simple_report_text("검사 상태", "Check status"), 210),
            ("created", self._simple_report_text("생성 시각", "Created"), 220),
        ):
            self.simple_report_history.heading(column, text=label)
            self.simple_report_history.column(column, width=width, stretch=True)
        self.simple_report_history.pack(fill="x")
        self.simple_report_history.bind("<<TreeviewSelect>>", self._simple_report_history_selected)
        self.simple_report_preview = tk.Text(
            history, height=10, wrap="word", font=("Malgun Gothic", 11), state="disabled",
        )
        self.simple_report_preview.pack(fill="both", expand=True, pady=(6, 0))
        self._simple_report_refresh_history()

    def _simple_report_generate_sync(
        self, view: dict[str, Any] | None, purpose: str, destination: str | Path, language: str,
        comparison_selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .guideline_retrieval import prepare_guideline_retrieval

        retrieval = prepare_guideline_retrieval(self.project.path)
        citations = list(retrieval.get("citations") or [])
        with EvidenceLibrary(self.project.path) as library:
            service = ReportService(self.project.path, library)
            if view is None:
                draft = service.create_gradual(lang=language, citations=citations)
            else:
                if not view.get("metrics"):
                    raise ValueError("selected_run_has_no_reportable_metrics")
                selection = comparison_selection if comparison_selection is not None else self._simple_report_comparison_selection(view)
                if selection is not None:
                    draft = service.create_comparison(view, selection, lang=language, question=purpose, citations=citations)
                else:
                    draft = service.create(
                        [view], template="research", lang=language, question=purpose,
                        context={"purpose": purpose, "medical_prediction": True,
                                 "retrieval": "local_official_guidelines", "llm_used": False},
                        citations=citations,
                    )
            checked = service.apply_check(draft["report_id"])
            if checked.get("automatic_check", {}).get("errors"):
                raise ValueError("blocking_report_check_errors")
            path = service.export_pdf(checked["report_id"], destination, mode="checked")
        return {"report": checked, "path": path, "retrieval": retrieval}

    def _simple_report_create_async(self) -> None:
        source_id = self.var("knowledge_simple_report_source").get()
        if not source_id:
            raise ValueError(self._simple_report_text(
                "보고서에 사용할 완료된 실험 결과 또는 gradual_v1 기준 평가를 선택하세요.",
                "Choose a completed experiment result or the gradual_v1 reference evaluation.",
            ))
        purpose = self.var("knowledge_simple_report_purpose").get().strip()
        language = str(self.lang)
        view = None if source_id == "gradual_v1_reference" else self.simple_report_sources.get(source_id)
        if source_id != "gradual_v1_reference" and view is None:
            raise ValueError(self._kt("select_run"))
        selection = self._simple_report_comparison_selection(view) if view else None
        if selection is not None and not selection['model_ids']:
            raise ValueError(self._simple_report_text('비교할 모델을 먼저 선택하세요.', 'Select models to compare first.'))
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title=self._simple_report_text("근거 기반 PDF 저장", "Save evidence-based PDF"),
            initialfile="SleepWell_gradual_v1_report.pdf" if view is None else "SleepWell_experiment_report.pdf",
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
        )
        if not destination:
            return
        self._knowledge_async(
            lambda: self._simple_report_generate_sync(view, purpose, destination, language, comparison_selection=selection),
            self._simple_report_complete,
        )

    def _simple_report_complete(self, result: dict[str, Any]) -> None:
        report = result["report"]
        path = Path(result["path"])
        self.var("knowledge_selected_report").set(report["report_id"])
        self.var("knowledge_pdf_report_id").set(report["report_id"])
        self.var("knowledge_pdf_path").set(str(path))
        self.var("knowledge_report_output").set(self._kt("report_pdf_saved", path=str(path)))
        self._set_knowledge_status("report_pdf_ready")
        self._simple_report_refresh_open_button()
        self._simple_report_refresh_history(report["report_id"])

    def _simple_report_refresh_open_button(self) -> None:
        button = getattr(self, "simple_report_open_button", None)
        if button is None:
            return
        try:
            if not button.winfo_exists():
                return
        except tk.TclError:
            return
        button.configure(state="normal" if Path(self.var("knowledge_pdf_path").get()).is_file() else "disabled")

    def _simple_report_open_pdf(self) -> None:
        path = Path(self.var("knowledge_pdf_path").get())
        if not path.is_file():
            raise ValueError(self._kt("report_pdf_missing"))
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:  # pragma: no cover
            import webbrowser
            webbrowser.open(path.resolve().as_uri())

    def _simple_report_refresh_history(self, select_id: str | None = None) -> None:
        widget = getattr(self, "simple_report_history", None)
        if widget is None:
            return
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        widget.delete(*widget.get_children())
        for report in self._list_reports_sync()[:10]:
            source = "gradual_v1" if report["template"] == "gradual_guidelines" else self._simple_report_text("실험 결과", "Experiment result")
            widget.insert("", "end", iid=report["report_id"], values=(
                source, self._simple_report_status(report["status"]), report["created_at"],
            ))
        report_id = select_id or self.var("knowledge_selected_report").get()
        if report_id and widget.exists(report_id):
            widget.selection_set(report_id)
            widget.see(report_id)
            self._simple_report_history_selected()

    def _simple_report_history_selected(self, _event: Any = None) -> None:
        widget = getattr(self, "simple_report_history", None)
        preview = getattr(self, "simple_report_preview", None)
        if widget is None or preview is None or not widget.selection():
            return
        report = self._get_report_sync(widget.selection()[0])
        self.var("knowledge_selected_report").set(report["report_id"])
        metrics = [claim for claim in report.get("claims", []) if claim.get("evidence", {}).get("kind") in {"experiment", "gradual_metric", "comparison_metric"}]
        literature = [claim for claim in report.get("claims", []) if claim.get("evidence", {}).get("kind") == "literature"]
        automatic = report.get("automatic_check", {})
        source = "gradual_v1" if report["template"] == "gradual_guidelines" else ", ".join(
            str(item.get("name") or item.get("run_id")) for item in report.get("run_refs", [])
        )
        lines = [
            f"{self._simple_report_text('보고서', 'Report')}: {report['report_id']}",
            f"{self._simple_report_text('자료', 'Source')}: {source}",
            f"{self._simple_report_text('검증된 성능 항목', 'Verified performance claims')}: {len(metrics)}",
            f"{self._simple_report_text('검색 근거', 'Retrieved evidence')}: {len(literature)}",
            f"{self._simple_report_text('자동 검사', 'Automatic check')}: "
            f"{self._simple_report_status(automatic.get('state', 'not_run'))}",
            f"{self._simple_report_text('오류', 'Errors')}: {len(automatic.get('errors', []))} · "
            f"{self._simple_report_text('경고', 'Warnings')}: {len(automatic.get('warnings', []))}", "",
        ]
        from .report_export import _report_sections

        for section in _report_sections(report):
            lines.append(str(section.get("title") or ""))
            lines.extend(str(item) for item in section.get("paragraphs", []))
            lines.append("")
        analysis = report.get("ai_analysis")
        if analysis:
            lines.append(self._simple_report_text("에이전트 실행 기록", "Agent execution record"))
            lines.append(f"{analysis.get('provider')} / {analysis.get('model')} · {analysis.get('run_id')}")
            lines.extend(f"{stage.get('role')}: {stage.get('status')}" for stage in analysis.get("stages", []))
            lines.append(f"SHA-256: {analysis.get('trace_sha256')}")
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", "\n".join(lines))
        preview.configure(state="disabled")

    # Report workspace ---------------------------------------------------
    def build_reports(self, frame: tk.Misc) -> None:
        self._restore_knowledge_state()
        ttk.Label(frame, text=self._kt("reports_help"), wraplength=1100, justify="left").pack(fill="x", pady=(0, 8))
        self._build_report_output_actions(frame)
        setup = ttk.LabelFrame(frame, text=self._kt("create_report"), padding=8)
        setup.pack(fill="x")
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=self._kt("template"), width=24).pack(side="left")
        self.knowledge_template_combo = ttk.Combobox(
            row, state="readonly", values=[self._kt(item) for item in _TEMPLATES], width=34
        )
        template = self._kvar("knowledge_report_template", "gradual_guidelines").get()
        self.knowledge_template_combo.current(_TEMPLATES.index(template) if template in _TEMPLATES else 0)
        self.knowledge_template_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.var("knowledge_report_template").set(_TEMPLATES[self.knowledge_template_combo.current()]),
        )
        self.knowledge_template_combo.pack(side="left")
        ttk.Label(setup, text=self._kt("gradual_report_help"), wraplength=1050,
                  justify="left").pack(fill="x", pady=4)
        ttk.Checkbutton(
            setup, text=self._kt("guideline_retrieval"),
            variable=self._kvar("knowledge_guideline_retrieval", "1"), onvalue="1", offvalue="0",
        ).pack(anchor="w")
        self._kfield(setup, "question", "knowledge_report_question")
        self._kfield(setup, "research_domain", "knowledge_research_domain")
        self._kfield(setup, "audience", "knowledge_audience")
        self._kfield(setup, "intended_use", "knowledge_intended_use")
        self._kfield(setup, "acceptance_criteria", "knowledge_acceptance_criteria")
        flags = ttk.Frame(setup)
        flags.pack(fill="x", pady=3)
        ttk.Checkbutton(
            flags, text=self._kt("acceptance_predeclared"),
            variable=self._kvar("knowledge_acceptance_predeclared", "0"), onvalue="1", offvalue="0",
        ).pack(side="left", padx=(0, 15))
        ttk.Checkbutton(
            flags, text=self._kt("medical_prediction"),
            variable=self._kvar("knowledge_medical_prediction", "0"), onvalue="1", offvalue="0",
        ).pack(side="left")
        ttk.Label(setup, text=self._kt("runs")).pack(fill="x", pady=(5, 0))
        self.knowledge_run_tree = self._ktree(
            setup,
            [("name", "title", 260), ("engine", "type", 110), ("evaluation", "mode", 220), ("run_id", "report_id", 300)],
            height=5,
            selectmode="extended",
        )
        self._populate_report_runs()
        actions = ttk.Frame(setup)
        actions.pack(fill="x")
        self.knowledge_create_pdf_button = self._kbutton(
            actions, "create_report_pdf", lambda: self._create_report_async(export_pdf=True)
        )
        self._kbutton(actions, "create_draft_only", self._create_report_async)
        ttk.Label(setup, text=self._kt("create_report_help"), wraplength=1050).pack(fill="x", pady=3)
        self.knowledge_report_versions = self._ktree(
            frame,
            [
                ("version", "version", 70), ("template", "template", 150), ("language", "language", 80),
                ("status", "status", 180), ("created", "created", 210), ("report_id", "report_id", 310),
            ],
            height=6,
        )
        self.knowledge_report_versions.bind("<<TreeviewSelect>>", self._report_selected)
        self._build_report_editor(frame)
        self._refresh_report_versions()

    def _build_report_output_actions(self, parent: tk.Misc) -> None:
        """Keep output and guideline entry points above the long document editor."""
        output = ttk.LabelFrame(parent, text=self._kt("report_output"), padding=8)
        output.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(output)
        row.pack(fill="x")
        self.knowledge_gradual_pdf_button = self._kbutton(
            row, "create_gradual_pdf", self._create_gradual_report_async
        )
        row = ttk.Frame(output)
        row.pack(fill="x")
        self.knowledge_save_pdf_button = self._kbutton(row, "save_report_pdf", self._export_report_pdf_async)
        self.knowledge_open_pdf_button = self._kbutton(row, "open_report_pdf", self._open_report_pdf)
        self.knowledge_save_pdf_button.configure(state="disabled")
        self.knowledge_open_pdf_button.configure(state="disabled")
        self._kbutton(row, "export_report", self._export_report_async)
        row = ttk.Frame(output)
        row.pack(fill="x")
        ttk.Label(row, text=self._kt("export_mode")).pack(side="left", padx=(0, 5))
        self.knowledge_export_combo = ttk.Combobox(
            row, state="readonly", values=(self._kt("draft"), self._kt("checked")), width=20
        )
        export_mode = self._kvar("knowledge_export_mode", "draft").get()
        self.knowledge_export_combo.current(0 if export_mode == "draft" else 1)
        self.knowledge_export_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.var("knowledge_export_mode").set(
                "draft" if self.knowledge_export_combo.current() == 0 else "checked"
            ),
        )
        self.knowledge_export_combo.pack(side="left")
        if callable(getattr(self, "build_guidelines", None)):
            self._kbutton(row, "guideline_reports", lambda: self._open_report_guidelines(1))
            self._kbutton(row, "official_guideline_pdfs", lambda: self._open_report_guidelines(3))
        ttk.Label(output, textvariable=self._kvar("knowledge_report_output", self._kt("report_output_help")),
                  wraplength=1050, justify="left").pack(fill="x", pady=4)

    def _open_report_guidelines(self, tab_index: int) -> None:
        self.navigate("guidelines")
        self.guideline_notebook.select(tab_index)

    def _knowledge_views(self) -> list[dict[str, Any]]:
        provider = getattr(self, "platform_runs", None)
        if provider is None:
            raise AttributeError(self._kt("platform_runs_required"))
        return list(provider())

    def _populate_report_runs(self) -> None:
        views = self._knowledge_views()
        self.knowledge_run_views = {str(view["run_id"]): view for view in views}
        self.knowledge_run_tree.delete(*self.knowledge_run_tree.get_children())
        for view in views:
            evaluation = view.get("evaluation", {})
            self.knowledge_run_tree.insert(
                "", "end", iid=str(view["run_id"]),
                values=(
                    view.get("name"), view.get("engine"), evaluation.get("evaluation_type"), view.get("run_id"),
                ),
            )

    def _create_report_sync(
        self,
        views: list[dict[str, Any]],
        template: str,
        question: str,
        citations: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if template == "gradual_guidelines":
            attached = list(citations)
            if (context or {}).get("guideline_retrieval", True):
                from .guideline_retrieval import prepare_guideline_retrieval
                retrieval = prepare_guideline_retrieval(self.project.path)
                existing = {(item.get("search_id"), item.get("chunk_id")) for item in attached}
                attached.extend(item for item in retrieval["citations"]
                                if (item.get("search_id"), item.get("chunk_id")) not in existing)
            with EvidenceLibrary(self.project.path) as library:
                return ReportService(self.project.path, library).create_gradual(lang=self.lang, citations=attached)
        with EvidenceLibrary(self.project.path) as library:
            return ReportService(self.project.path, library).create(
                views, template=template, lang=self.lang, question=question,
                context=context or {"purpose": question}, citations=citations,
            )

    def _create_gradual_report_async(self) -> None:
        self.var("knowledge_report_template").set("gradual_guidelines")
        self.knowledge_template_combo.current(_TEMPLATES.index("gradual_guidelines"))
        self._create_report_async(export_pdf=True)

    def _create_report_async(self, export_pdf: bool = False) -> None:
        template = self.var("knowledge_report_template").get() or "gradual_guidelines"
        selected = list(self.knowledge_run_tree.selection())
        if not selected and template != "gradual_guidelines":
            raise ValueError(self._kt("select_run"))
        views = [] if template == "gradual_guidelines" else [self.knowledge_run_views[run_id] for run_id in selected]
        question = self.var("knowledge_report_question").get().strip()
        citations = list(self.knowledge_citations)
        context = {
            "purpose": question,
            "domain": self.var("knowledge_research_domain").get().strip() or "general_ml",
            "audience": self.var("knowledge_audience").get().strip(),
            "intended_use": self.var("knowledge_intended_use").get().strip(),
            "acceptance_criteria": self.var("knowledge_acceptance_criteria").get().strip() or None,
            "acceptance_predeclared": self.var("knowledge_acceptance_predeclared").get() == "1",
            "medical_prediction": self.var("knowledge_medical_prediction").get() == "1",
            "guideline_retrieval": self.var("knowledge_guideline_retrieval").get() != "0",
        }
        destination = None
        if export_pdf:
            destination = filedialog.asksaveasfilename(
                parent=self.root, title=self._kt("create_report_pdf"),
                initialfile="SleepWell_gradual_v1_TRIPOD_PROBAST.pdf" if template == "gradual_guidelines" else "SleepWell_report.pdf",
                defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            )
            if not destination:
                return
        mode = self.var("knowledge_export_mode").get() or "draft"

        def complete(report):
            self._after_report_operation(report, "report_created")
            if destination:
                self._start_report_pdf_export(report["report_id"], destination, mode)

        self._knowledge_async(
            lambda: self._create_report_sync(views, template, question, citations, context),
            complete,
        )

    def _list_reports_sync(self) -> list[dict[str, Any]]:
        return ReportService(self.project.path).list_reports()

    def _get_report_sync(self, report_id: str) -> dict[str, Any]:
        with EvidenceLibrary(self.project.path) as library:
            return ReportService(self.project.path, library).get(report_id)

    def _refresh_report_versions(self, select_id: str | None = None) -> None:
        tree = getattr(self, "knowledge_report_versions", None)
        if tree is None or not tree.winfo_exists():
            return
        reports = self._list_reports_sync()
        tree.delete(*tree.get_children())
        for report in reports:
            tree.insert(
                "", "end", iid=report["report_id"],
                values=(
                    report["version"], self._kt(report["template"]), report["language"], report["status"],
                    report["created_at"], report["report_id"],
                ),
            )
        report_id = select_id or self.var("knowledge_selected_report").get()
        if report_id and tree.exists(report_id):
            tree.selection_set(report_id)
            tree.see(report_id)
            self._report_selected()

    def _build_report_editor(self, parent: tk.Misc) -> None:
        paned = ttk.Panedwindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=6)
        left = ttk.LabelFrame(paned, text=self._kt("sections_claims"), padding=5)
        center = ttk.LabelFrame(paned, text=self._kt("draft_editor"), padding=5)
        right = ttk.LabelFrame(paned, text=self._kt("evidence_check"), padding=5)
        paned.add(left, weight=1)
        paned.add(center, weight=2)
        paned.add(right, weight=2)
        self.knowledge_claim_tree = ttk.Treeview(left, show="tree", height=16)
        self.knowledge_claim_tree.pack(fill="both", expand=True)
        self.knowledge_claim_tree.bind("<<TreeviewSelect>>", self._report_node_selected)
        self.knowledge_report_editor = tk.Text(center, height=18, wrap="word", font=("Malgun Gothic", 10))
        self.knowledge_report_editor.pack(fill="both", expand=True)
        checklist_row = ttk.Frame(center)
        checklist_row.pack(fill="x", pady=(4, 0))
        ttk.Label(checklist_row, text=self._kt("checklist_status")).pack(side="left")
        self.knowledge_checklist_combo = ttk.Combobox(
            checklist_row, state="disabled", values=[self._kt(item) for item in _CHECKLIST_STATUSES], width=22
        )
        self.knowledge_checklist_combo.pack(side="left", padx=5)
        self.knowledge_report_detail = tk.Text(right, height=18, wrap="word", font=("Consolas", 9))
        self.knowledge_report_detail.pack(fill="both", expand=True)
        controls = ttk.Frame(parent)
        controls.pack(fill="x")
        self._kbutton(controls, "save_new_version", self._save_report_edit_async)
        self._kbutton(controls, "automatic_check", self._check_report_async)
        self._kfield(parent, "reviewer", "knowledge_reviewer")
        ttk.Label(parent, text=self._kt("review_note")).pack(fill="x")
        self.knowledge_review_note_text = tk.Text(parent, height=3, wrap="word", font=("Malgun Gothic", 10))
        self.knowledge_review_note_text.pack(fill="x")
        self.knowledge_review_note_text.insert("1.0", self.var("knowledge_review_note").get())
        review_actions = ttk.Frame(parent)
        review_actions.pack(fill="x")
        self._kbutton(review_actions, "record_review", self._record_review_async)
        ttk.Label(review_actions, text=self._kt("target_language")).pack(side="left", padx=(12, 4))
        self.knowledge_target_language = ttk.Combobox(
            review_actions, state="readonly", values=(self._kt("ko"), self._kt("en")), width=12
        )
        target = self._kvar("knowledge_target_language", "en" if self.lang == "ko" else "ko").get()
        self.knowledge_target_language.current(0 if target == "ko" else 1)
        self.knowledge_target_language.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.var("knowledge_target_language").set(
                "ko" if self.knowledge_target_language.current() == 0 else "en"
            ),
        )
        self.knowledge_target_language.pack(side="left")
        self._kbutton(review_actions, "translate", self._translate_report_async)
        errors = ttk.LabelFrame(parent, text=self._kt("audit_errors"), padding=5)
        errors.pack(fill="both", expand=True, pady=(4, 0))
        self.knowledge_report_errors = tk.Text(errors, height=6, wrap="word", font=("Consolas", 9))
        self.knowledge_report_errors.pack(fill="both", expand=True)

    def _report_selected(self, _event: Any = None) -> None:
        selection = self.knowledge_report_versions.selection()
        if not selection:
            return
        report_id = selection[0]
        self.var("knowledge_selected_report").set(report_id)
        self.knowledge_save_pdf_button.configure(state="normal")
        self._refresh_report_pdf_action(report_id)
        report = self._get_report_sync(report_id)
        self.knowledge_current_report = report
        claims = {claim["claim_id"]: claim for claim in report.get("claims", [])}
        self.knowledge_claims = claims
        tree = self.knowledge_claim_tree
        tree.delete(*tree.get_children())
        for section in report.get("sections", []):
            section_iid = f"section:{section['section_id']}"
            tree.insert("", "end", iid=section_iid, text=section["title"], open=True)
            for claim_id in section.get("claim_ids", []):
                claim = claims.get(claim_id)
                if claim:
                    tree.insert(section_iid, "end", iid=f"claim:{claim_id}", text=claim["text"][:100])
        if report.get("checklists"):
            checklist_root = "checklists"
            tree.insert("", "end", iid=checklist_root, text=self._kt("checklists"), open=True)
            for framework in report["checklists"]:
                framework_iid = f"framework:{framework['framework_id']}"
                tree.insert(checklist_root, "end", iid=framework_iid, text=framework["framework"], open=True)
                for item in framework.get("items", []):
                    tree.insert(
                        framework_iid, "end", iid=f"checklist:{item['item_id']}",
                        text=f"{item.get('source_answer') or self._kt(item.get('source_status', item['status']))} · {item['title']}",
                    )
        automatic = report.get("automatic_check", {})
        self.knowledge_report_errors.configure(state="normal")
        self.knowledge_report_errors.delete("1.0", "end")
        error_lines = [f"{self._kt('status')}: {automatic.get('state', 'not_run')}"]
        error_lines.append(f"{self._kt('errors')}: {len(automatic.get('errors', []))}")
        error_lines.extend(
            f"  • {item.get('code')} · {item.get('claim_id') or '—'} · {item.get('detail', '')}"
            for item in automatic.get("errors", [])
        )
        error_lines.append(f"{self._kt('warnings')}: {len(automatic.get('warnings', []))}")
        error_lines.extend(
            f"  • {item.get('code')} · {item.get('claim_id') or '—'}"
            for item in automatic.get("warnings", [])
        )
        self.knowledge_report_errors.insert("1.0", "\n".join(error_lines))
        self.knowledge_report_errors.configure(state="disabled")
        saved_node = self.var("knowledge_selected_node").get()
        if not saved_node or not tree.exists(saved_node):
            sections = report.get("sections", [])
            saved_node = f"section:{sections[0]['section_id']}" if sections else ""
        if saved_node and tree.exists(saved_node):
            tree.selection_set(saved_node)
            tree.see(saved_node)
            self._report_node_selected()

    def _report_node_selected(self, _event: Any = None) -> None:
        selection = self.knowledge_claim_tree.selection()
        if not selection:
            return
        node = selection[0]
        self.var("knowledge_selected_node").set(node)
        report = self.knowledge_current_report
        text = ""
        detail_lines: list[str] = []
        checklist_status: str | None = None
        if node.startswith("claim:"):
            claim = self.knowledge_claims[node.split(":", 1)[1]]
            text = claim.get("text", "")
            evidence = claim.get("evidence", {})
            detail_lines = [
                f"{self._kt('claim_id')}: {claim.get('claim_id')}",
                f"{self._kt('kind')}: {evidence.get('kind') or '—'}",
                f"{self._kt('review_state')}: {claim.get('review_state') or '—'}",
            ]
            if evidence.get("kind") == "experiment":
                detail_lines.extend(
                    [
                        f"{self._kt('run_id')}: {evidence.get('run_id')}",
                        f"{self._kt('metric')}: {evidence.get('model_id')} · {evidence.get('metric')}",
                        f"{self._kt('value')}: {evidence.get('value')} {evidence.get('unit') or ''}",
                        f"{self._kt('sample_count')}: {evidence.get('n_rows') if evidence.get('n_rows') is not None else '—'}",
                        f"{self._kt('participant_count')}: {evidence.get('n_participants') if evidence.get('n_participants') is not None else '—'}",
                        f"{self._kt('field_path')}: {' / '.join(str(value) for value in evidence.get('field_path', []))}",
                        f"SHA-256: {evidence.get('result_hash')}",
                    ]
                )
            elif evidence.get("kind") == "gradual_metric":
                detail_lines.extend([
                    f"{self._kt('source')}: gradual_v1 evaluation.json",
                    f"{self._kt('metric')}: {evidence.get('cohort')} / {evidence.get('arm')} / {evidence.get('weighting')} / {evidence.get('metric')}",
                    f"{self._kt('value')}: {evidence.get('value')} min",
                    f"95% CI: {evidence.get('ci95_low')} – {evidence.get('ci95_high')}",
                    f"{self._kt('sample_count')}: {evidence.get('n_rows')}",
                    f"{self._kt('participant_count')}: {evidence.get('n_participants')}",
                    f"{self._kt('field_path')}: metrics / {evidence.get('metric_index')}",
                    f"SHA-256: {evidence.get('evaluation_snapshot_sha256')}",
                ])
            elif evidence.get("kind") == "literature":
                anchor = evidence.get("anchor", {})
                detail_lines.extend(
                    [
                        f"{self._kt('source')}: {evidence.get('source_id')}",
                        f"{self._kt('source_version')}: {evidence.get('source_version')}",
                        f"{self._kt('locator')}: {anchor.get('type')} · {anchor.get('value')}",
                        f"{self._kt('search_id')}: {evidence.get('search_id')}",
                        f"{self._kt('excerpt')}:\n“{evidence.get('excerpt', '')}”",
                        self._kt("semantic_pending"),
                    ]
                )
        elif node.startswith("section:"):
            section_id = node.split(":", 1)[1]
            section = next(item for item in report["sections"] if item["section_id"] == section_id)
            text = "\n\n".join(section.get("paragraphs", []))
            detail_lines = [
                f"{self._kt('sections_claims')}: {section['title']}",
                f"{self._kt('claim_id')}: {', '.join(section.get('claim_ids', [])) or '—'}",
            ]
        elif node.startswith("checklist:"):
            item_id = node.split(":", 1)[1]
            item = next(
                item
                for framework in report.get("checklists", [])
                for item in framework.get("items", [])
                if item["item_id"] == item_id
            )
            text = item.get("note", "")
            checklist_status = item["status"]
            detail_lines = [
                item["title"],
                f"{self._kt('checklist_status')}: {item.get('source_answer') or self._kt(item.get('source_status', item['status']))}",
                f"{self._kt('sections_claims')}: {item.get('report_section') or '—'}",
                f"{self._kt('claim_id')}: {', '.join(item.get('evidence_claim_ids', [])) or '—'}",
                f"{self._kt('review_state')}: {item.get('assessed_by') or '—'}",
            ]
            source_ids = set(item.get("source_evidence_ids", []))
            for source in report.get("guideline_evidence", {}).get("audit_snapshot", {}).get("sources", []):
                if source.get("id") in source_ids:
                    detail_lines.append(f"{source['id']}: {source.get('title', '')}\n"
                                        f"SHA-256: {source.get('sha256') or source.get('snapshot_sha256') or '—'}")
        state = self._knowledge_state()
        editor_context = state.get("editor_context") or {}
        if (
            editor_context.get("report_id") == report.get("report_id")
            and editor_context.get("node") == node
            and self.var("knowledge_report_editor_draft").get()
        ):
            text = self.var("knowledge_report_editor_draft").get()
        self.knowledge_report_editor.delete("1.0", "end")
        self.knowledge_report_editor.insert("1.0", text)
        if checklist_status is None:
            self.knowledge_checklist_combo.configure(state="disabled")
            self.knowledge_checklist_combo.set("")
        else:
            self.knowledge_checklist_combo.configure(state="readonly")
            self.knowledge_checklist_combo.current(_CHECKLIST_STATUSES.index(checklist_status))
        self.knowledge_report_detail.configure(state="normal")
        self.knowledge_report_detail.delete("1.0", "end")
        self.knowledge_report_detail.insert("1.0", "\n".join(detail_lines))
        self.knowledge_report_detail.configure(state="disabled")

    def _selected_report_id(self) -> str:
        report_id = self.var("knowledge_selected_report").get()
        if not report_id:
            raise ValueError(self._kt("select_report"))
        return report_id

    def _save_report_edit_sync(self, report_id: str, node: str, text: str) -> dict[str, Any]:
        if node.startswith("claim:"):
            edits = {"claims": {node.split(":", 1)[1]: text}}
        elif node.startswith("section:"):
            edits = {"sections": {node.split(":", 1)[1]: text}}
        elif node.startswith("checklist:"):
            status_index = self.knowledge_checklist_combo.current()
            if status_index < 0:
                raise ValueError(self._kt("select_section_claim"))
            edits = {
                "checklists": {
                    node.split(":", 1)[1]: {"status": _CHECKLIST_STATUSES[status_index], "note": text}
                }
            }
        else:
            raise ValueError(self._kt("select_section_claim"))
        return ReportService(self.project.path).edit(report_id, edits)

    def _save_report_edit_async(self) -> None:
        report_id = self._selected_report_id()
        node = self.var("knowledge_selected_node").get()
        if not node:
            raise ValueError(self._kt("select_section_claim"))
        text = self.knowledge_report_editor.get("1.0", "end-1c")
        self._knowledge_async(
            lambda: self._save_report_edit_sync(report_id, node, text),
            lambda report: self._after_report_operation(report, "report_edited"),
        )

    def _check_report_sync(self, report_id: str) -> dict[str, Any]:
        with EvidenceLibrary(self.project.path) as library:
            return ReportService(self.project.path, library).apply_check(report_id)

    def _check_report_async(self) -> None:
        report_id = self._selected_report_id()
        self._knowledge_async(
            lambda: self._check_report_sync(report_id),
            lambda report: self._after_report_operation(report, "report_checked"),
        )

    def _record_review_sync(self, report_id: str, reviewer: str, note: str) -> dict[str, Any]:
        return ReportService(self.project.path).review(report_id, reviewer, note)

    def _record_review_async(self) -> None:
        report_id = self._selected_report_id()
        reviewer = self.var("knowledge_reviewer").get().strip()
        note = self.knowledge_review_note_text.get("1.0", "end-1c").strip()
        if not reviewer or not note:
            raise ValueError(self._kt("review_required"))
        self._knowledge_async(
            lambda: self._record_review_sync(report_id, reviewer, note),
            lambda report: self._after_report_operation(report, "review_recorded"),
        )

    def _translate_report_sync(self, report_id: str, lang: str) -> dict[str, Any]:
        with EvidenceLibrary(self.project.path) as library:
            return ReportService(self.project.path, library).new_language(report_id, lang)

    def _translate_report_async(self) -> None:
        report_id = self._selected_report_id()
        language = self.var("knowledge_target_language").get()
        self._knowledge_async(
            lambda: self._translate_report_sync(report_id, language),
            lambda report: self._after_report_operation(report, "language_created"),
        )

    def _export_report_sync(self, report_id: str, destination: str | Path, mode: str) -> dict[str, Path]:
        with EvidenceLibrary(self.project.path) as library:
            return ReportService(self.project.path, library).export(report_id, destination, mode=mode)

    def _export_report_pdf_sync(self, report_id: str, destination: str | Path, mode: str) -> Path:
        with EvidenceLibrary(self.project.path) as library:
            return ReportService(self.project.path, library).export_pdf(report_id, destination, mode=mode)

    def _export_report_pdf_async(self) -> None:
        report_id = self._selected_report_id()
        destination = filedialog.asksaveasfilename(
            parent=self.root, title=self._kt("save_report_pdf"), initialfile=f"{report_id}.pdf",
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
        )
        if destination:
            self._start_report_pdf_export(report_id, destination, self.var("knowledge_export_mode").get() or "draft")

    def _start_report_pdf_export(self, report_id: str, destination: str, mode: str) -> None:
        self._knowledge_async(
            lambda: self._export_report_pdf_sync(report_id, destination, mode),
            lambda path: self._after_report_pdf_export(report_id, path),
        )

    def _after_report_pdf_export(self, report_id: str, path: str | Path) -> None:
        self.var("knowledge_pdf_report_id").set(report_id)
        self.var("knowledge_pdf_path").set(str(path))
        self.var("knowledge_report_output").set(self._kt("report_pdf_saved", path=str(path)))
        self._refresh_report_pdf_action(self.var("knowledge_selected_report").get())
        self._set_knowledge_status("report_pdf_ready")

    def _refresh_report_pdf_action(self, report_id: str) -> None:
        button = getattr(self, "knowledge_open_pdf_button", None)
        if button is None or not button.winfo_exists():
            return
        path = self.var("knowledge_pdf_path").get()
        matches = report_id == self.var("knowledge_pdf_report_id").get()
        button.configure(state="normal" if matches and path and Path(path).is_file() else "disabled")

    def _open_report_pdf(self) -> None:
        report_id = self._selected_report_id()
        path = Path(self.var("knowledge_pdf_path").get())
        if report_id != self.var("knowledge_pdf_report_id").get() or not path.is_file():
            raise ValueError(self._kt("report_pdf_missing"))
        if hasattr(os, "startfile"):
            os.startfile(str(path))
        else:
            import webbrowser
            webbrowser.open(path.resolve().as_uri())

    def _export_report_async(self) -> None:
        report_id = self._selected_report_id()
        destination = filedialog.askdirectory(parent=self.root, title=self._kt("export_report"))
        if not destination:
            return
        mode = self.var("knowledge_export_mode").get() or "draft"
        self._knowledge_async(
            lambda: self._export_report_sync(report_id, destination, mode),
            lambda paths: self._after_report_pdf_export(report_id, paths["pdf"]),
        )

    def _after_report_operation(self, report: dict[str, Any], status_key: str) -> None:
        self.var("knowledge_selected_report").set(report["report_id"])
        self._refresh_report_versions(report["report_id"])
        self._set_knowledge_status(status_key)
        self.capture_knowledge_drafts()


__all__ = ["KnowledgeUIMixin"]
