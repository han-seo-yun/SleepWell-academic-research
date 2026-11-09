"""Native Tkinter surface for approval-gated optional AI drafts."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable

from .evidence_library import EvidenceLibrary
from .optional_ai import OptionalAI
from .reporting import ReportService


_TEXT = json.loads((Path(__file__).with_name("locales") / "optional_ai_ui.json").read_text(encoding="utf-8"))
_PURPOSES = ("result_summary", "experiment_plan", "settings_explanation", "follow_up_experiments")


class OptionalAIMixin:
    """Host contract: root, project.path, lang, var(), status, and platform_runs()."""

    def _ait(self, key: str, **values: Any) -> str:
        pair = _TEXT.get(key)
        text = pair[1 if getattr(self, "lang", "ko") == "en" else 0] if pair else key
        return text.format(**values) if values else text

    def _ai_status(self, key: str, detail: str = "") -> None:
        message = self._ait(key)
        if detail:
            message = f"{message}: {detail}"
        if getattr(self, "status", None) is not None:
            self.status.set(message)
        self.var("optional_ai_status").set(message)

    def _ai_async(self, work: Callable[[], Any], complete: Callable[[Any], None]) -> None:
        if getattr(self, "_optional_ai_worker", False) or getattr(self, "_ai_busy", False):
            return
        self._optional_ai_worker = True
        self._ai_busy = True
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()

        def run() -> None:
            try:
                messages.put(("ok", work()))
            except Exception as error:
                messages.put(("error", error))

        def poll() -> None:
            try:
                kind, value = messages.get_nowait()
            except queue.Empty:
                self.root.after(80, poll)
                return
            self._optional_ai_worker = False
            self._ai_busy = False
            if kind == "ok":
                try:
                    complete(value)
                except Exception as error:
                    self._ai_status("operation_failed", str(error))
            else:
                self._ai_status("operation_failed", str(value))
            button = getattr(self, "optional_ai_cancel_button", None)
            try:
                if button is not None and button.winfo_exists():
                    button.configure(state="disabled")
            except tk.TclError:
                pass

        threading.Thread(target=run, daemon=True, name="researchbench-optional-ai").start()
        self.root.after(80, poll)

    def _ai_entry(self, parent: tk.Misc, label: str, key: str, *, width: int = 28) -> ttk.Entry:
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=2)
        ttk.Label(frame, text=self._ait(label), width=24).pack(side="left")
        entry = ttk.Entry(frame, textvariable=self.var(key), width=width)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _ai_defaults(self) -> None:
        defaults = {
            "optional_ai_provider": "openai-compatible",
            "optional_ai_endpoint": "https://api.openai.com/v1/chat/completions",
            "optional_ai_model": "",
            "optional_ai_purpose": "result_summary",
            "optional_ai_max_calls": "3", "optional_ai_budget": "1.00",
            "optional_ai_timeout": "30", "optional_ai_retries": "1",
            "optional_ai_max_tokens": "800", "optional_ai_temperature": "0",
            "optional_ai_input_price": "", "optional_ai_output_price": "",
            "optional_ai_query": "", "optional_ai_search_mode": "lexical",
            "optional_ai_report": "", "optional_ai_run": "",
        }
        for key, value in defaults.items():
            if not self.var(key).get():
                self.var(key).set(value)

    def build_optional_ai(self, parent: tk.Misc) -> None:
        self._ai_defaults()
        ttk.Label(parent, text=self._ait("title"), style="AcademicTitle.TLabel").pack(anchor="w")
        ttk.Label(parent, text=self._ait("scope_notice"), style="AcademicNote.TLabel",
                  wraplength=1100).pack(fill="x", pady=(2, 8))
        panes = ttk.Panedwindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)
        setup = ttk.LabelFrame(panes, text=self._ait("setup"), padding=7)
        evidence = ttk.LabelFrame(panes, text=self._ait("authorized_evidence"), padding=7)
        output = ttk.LabelFrame(panes, text=self._ait("preview_and_draft"), padding=7)
        panes.add(setup, weight=1); panes.add(evidence, weight=2); panes.add(output, weight=2)

        self._ai_entry(setup, "provider", "optional_ai_provider")
        self._ai_entry(setup, "endpoint", "optional_ai_endpoint")
        self._ai_entry(setup, "model", "optional_ai_model")
        ttk.Label(setup, text=self._ait("purpose")).pack(anchor="w", pady=(6, 0))
        purpose = ttk.Combobox(setup, state="readonly", values=[self._ait(item) for item in _PURPOSES])
        purpose.current(_PURPOSES.index(self.var("optional_ai_purpose").get()))
        purpose.bind("<<ComboboxSelected>>",
                     lambda _event: self.var("optional_ai_purpose").set(_PURPOSES[purpose.current()]))
        purpose.pack(fill="x")
        ttk.Label(setup, text=self._ait("run")).pack(anchor="w", pady=(6, 0))
        self.optional_ai_runs = list(self.platform_runs())
        self.optional_ai_run_map = {view["run_id"]: view for view in self.optional_ai_runs}
        run_values = [f"{view['name']} · {view['run_id']}" for view in self.optional_ai_runs]
        self.optional_ai_run_combo = ttk.Combobox(setup, state="readonly", values=run_values)
        if run_values:
            self.optional_ai_run_combo.current(0)
            self.var("optional_ai_run").set(self.optional_ai_runs[0]["run_id"])
        self.optional_ai_run_combo.bind("<<ComboboxSelected>>", lambda _event: self.var(
            "optional_ai_run").set(self.optional_ai_runs[self.optional_ai_run_combo.current()]["run_id"]))
        self.optional_ai_run_combo.pack(fill="x")
        ttk.Label(setup, text=self._ait("linked_report")).pack(anchor="w", pady=(6, 0))
        reports = ReportService(self.project.path).list_reports()
        self.optional_ai_report_ids = [""] + [item["report_id"] for item in reports]
        report_values = [self._ait("no_report")] + [
            f"{item['report_id']} · {item['status']}" for item in reports]
        self.optional_ai_report_combo = ttk.Combobox(setup, state="readonly", values=report_values)
        self.optional_ai_report_combo.current(0)
        self.optional_ai_report_combo.bind("<<ComboboxSelected>>", lambda _event: self.var(
            "optional_ai_report").set(self.optional_ai_report_ids[self.optional_ai_report_combo.current()]))
        self.optional_ai_report_combo.pack(fill="x")

        budget = ttk.LabelFrame(setup, text=self._ait("budget"), padding=5)
        budget.pack(fill="x", pady=7)
        for label, key in (("max_calls", "optional_ai_max_calls"), ("budget_usd", "optional_ai_budget"),
                           ("timeout", "optional_ai_timeout"), ("retries", "optional_ai_retries"),
                           ("max_tokens", "optional_ai_max_tokens"),
                           ("input_price", "optional_ai_input_price"),
                           ("output_price", "optional_ai_output_price")):
            self._ai_entry(budget, label, key, width=12)
        ttk.Label(budget, text=self._ait("cost_disclosure"), style="AcademicNote.TLabel",
                  wraplength=320).pack(fill="x")
        ttk.Label(setup, text=self._ait("api_key")).pack(anchor="w", pady=(6, 0))
        self.optional_ai_key_entry = ttk.Entry(setup, show="●")
        self.optional_ai_key_entry.pack(fill="x")
        ttk.Button(setup, text=self._ait("save_key"), command=self._save_optional_ai_key).pack(anchor="w", pady=4)

        search = ttk.Frame(evidence); search.pack(fill="x")
        ttk.Entry(search, textvariable=self.var("optional_ai_query")).pack(side="left", fill="x", expand=True)
        self.optional_ai_mode = ttk.Combobox(search, state="readonly",
                                             values=(self._ait("lexical_default"), self._ait("hybrid_experimental")),
                                             width=22)
        self.optional_ai_mode.current(0); self.optional_ai_mode.pack(side="left", padx=4)
        self.optional_ai_mode.bind("<<ComboboxSelected>>", lambda _event: self.var(
            "optional_ai_search_mode").set("lexical" if self.optional_ai_mode.current() == 0 else "hybrid"))
        ttk.Button(search, text=self._ait("search"), command=self._search_optional_ai_evidence).pack(side="left")
        self.optional_ai_search_tree = ttk.Treeview(
            evidence, columns=("source_title", "locator", "source", "chunk"), show="headings",
            selectmode="extended", height=9)
        for column, width in (("source_title", 180), ("locator", 110), ("source", 180), ("chunk", 190)):
            self.optional_ai_search_tree.heading(column, text=self._ait(column))
            self.optional_ai_search_tree.column(column, width=width, stretch=True)
        self.optional_ai_search_tree.pack(fill="both", expand=True)
        ttk.Button(evidence, text=self._ait("authorize_selected"),
                   command=self._authorize_optional_ai_hits).pack(anchor="w", pady=4)
        ttk.Label(evidence, text=self._ait("authorized_exact_chunks")).pack(anchor="w")
        self.optional_ai_authorized_tree = ttk.Treeview(
            evidence, columns=("source_title", "locator", "chunk"), show="headings", height=6)
        for column, width in (("source_title", 210), ("locator", 120), ("chunk", 230)):
            self.optional_ai_authorized_tree.heading(column, text=self._ait(column))
            self.optional_ai_authorized_tree.column(column, width=width)
        self.optional_ai_authorized_tree.pack(fill="both", expand=True)
        self.optional_ai_search_hits = {}
        self.optional_ai_authorized = {}

        ttk.Button(output, text=self._ait("create_preview"),
                   command=self._create_optional_ai_preview).pack(anchor="w")
        self.optional_ai_preview_text = tk.Text(output, height=17, wrap="word", font=("Consolas", 9))
        self.optional_ai_preview_text.pack(fill="both", expand=True, pady=4)
        self.optional_ai_preview_text.configure(state="disabled")
        self.optional_ai_confirm = tk.BooleanVar(self.root, False)
        self.optional_ai_confirm_button = ttk.Checkbutton(
            output, text=self._ait("confirm_exact_payload"), variable=self.optional_ai_confirm,
            command=self._optional_ai_confirmation_changed, state="disabled")
        self.optional_ai_confirm_button.pack(anchor="w")
        self.optional_ai_send_button = ttk.Button(
            output, text=self._ait("send_external"), command=self._send_optional_ai, state="disabled")
        self.optional_ai_send_button.pack(anchor="w", pady=4)
        self.optional_ai_cancel_button = ttk.Button(
            output, text=self._ait("cancel_external"), command=self.cancel_optional_ai, state="disabled")
        self.optional_ai_cancel_button.pack(anchor="w", pady=2)
        self.optional_ai_output_text = tk.Text(output, height=10, wrap="word", font=("Malgun Gothic", 10))
        self.optional_ai_output_text.pack(fill="both", expand=True)
        ttk.Label(output, textvariable=self.var("optional_ai_status"),
                  style="AcademicNote.TLabel", wraplength=450).pack(fill="x")

    def _optional_ai_service_config(self) -> dict[str, Any]:
        """Read Tk variables on the Tk thread before starting worker code."""
        return {
            "project_path": Path(self.project.path).resolve(),
            "max_calls": int(self.var("optional_ai_max_calls").get()),
            "budget_usd": float(self.var("optional_ai_budget").get()),
            "timeout_seconds": float(self.var("optional_ai_timeout").get()),
            "max_retries": int(self.var("optional_ai_retries").get()),
        }

    def _optional_ai_service(self, config: dict[str, Any] | None = None) -> OptionalAI:
        values = config if config is not None else self._optional_ai_service_config()
        return OptionalAI(
            values["project_path"], max_calls=values["max_calls"], budget_usd=values["budget_usd"],
            timeout_seconds=values["timeout_seconds"], max_retries=values["max_retries"],
        )

    def _save_optional_ai_key(self) -> None:
        value = self.optional_ai_key_entry.get()
        provider = self.var("optional_ai_provider").get().strip()
        endpoint = self.var("optional_ai_endpoint").get().strip()
        account = OptionalAI.credential_account(provider, endpoint)
        OptionalAI.store_api_key(provider, value, account)
        self.optional_ai_key_entry.delete(0, "end")
        self._ai_status("key_saved")

    def _search_optional_ai_evidence(self) -> None:
        query = self.var("optional_ai_query").get().strip()
        mode = self.var("optional_ai_search_mode").get() or "lexical"
        project_path = Path(self.project.path).resolve()
        model_path = (Path(__file__).parents[1] / ".models" /
                      "paraphrase-multilingual-MiniLM-L12-v2") if mode == "hybrid" else None

        def work() -> dict[str, Any]:
            with EvidenceLibrary(project_path) as library:
                return library.search(query, top_k=10, embedding_model_path=model_path)

        self._ai_status("search_running")
        self._ai_async(work, self._show_optional_ai_hits)

    def _show_optional_ai_hits(self, result: dict[str, Any]) -> None:
        self.optional_ai_search_hits = {hit["chunk_id"]: hit for hit in result["hits"]}
        tree = getattr(self, "optional_ai_search_tree", None)
        if tree is None or not tree.winfo_exists():
            self._ai_status("search_complete", f"n={len(result['hits'])}")
            return
        tree.delete(*tree.get_children())
        for hit in result["hits"]:
            tree.insert("", "end", iid=hit["chunk_id"], values=(
                hit["title"], hit["locator"], hit["source_id"], hit["chunk_id"]))
        self._ai_status("search_complete", f"n={len(result['hits'])}")

    def _authorize_optional_ai_hits(self) -> None:
        for chunk_id in self.optional_ai_search_tree.selection():
            hit = self.optional_ai_search_hits[chunk_id]
            self.optional_ai_authorized[chunk_id] = hit
            if not self.optional_ai_authorized_tree.exists(chunk_id):
                self.optional_ai_authorized_tree.insert("", "end", iid=chunk_id, values=(
                    hit["title"], hit["locator"], chunk_id))
        self._ai_status("chunks_authorized", str(len(self.optional_ai_authorized)))

    def _optional_ai_summary(self) -> dict[str, Any]:
        run_id = self.var("optional_ai_run").get()
        view = self.optional_ai_run_map.get(run_id)
        if not view:
            raise ValueError(self._ait("select_run"))
        evaluation = view.get("evaluation", {})
        safe_evaluation = {key: evaluation.get(key) for key in (
            "task", "target", "evaluation_type", "synthetic", "calculation", "population",
            "positive_class", "threshold", "average", "averaging")}
        metrics = {f"{item['model_id']}:{item['metric']}": {
            "value": item.get("value"), "unit": item.get("unit"), "n_rows": item.get("n_rows"),
            "n_participants": item.get("n_participants"), "reason": item.get("reason")}
                   for item in view.get("metrics", [])}
        return {"run_id": run_id, "result_adapter_id": view.get("result_hash"),
                "task_type": evaluation.get("task"), "data_version": view.get("data_hash"),
                "target": evaluation.get("target"), "metrics": metrics, "evaluation": safe_evaluation,
                "synthetic": evaluation.get("synthetic"), "limitations": list(view.get("warnings") or [])}

    def _create_optional_ai_preview(self) -> None:
        summary = self._optional_ai_summary()
        with self._optional_ai_service() as service:
            preview = service.create_preview(
                experiment_summary=summary,
                authorized_chunk_ids=list(self.optional_ai_authorized),
                provider=self.var("optional_ai_provider").get().strip(),
                model=self.var("optional_ai_model").get().strip(),
                endpoint=self.var("optional_ai_endpoint").get().strip(),
                purpose=self.var("optional_ai_purpose").get(),
                settings={"temperature": float(self.var("optional_ai_temperature").get()),
                          "max_tokens": int(self.var("optional_ai_max_tokens").get()),
                          "input_cost_per_million": float(self.var("optional_ai_input_price").get()),
                          "output_cost_per_million": float(self.var("optional_ai_output_price").get())},
            )
        self.optional_ai_current_preview = preview
        rendered = {"payload_sha256": preview["payload_sha256"],
                    "transmission_preview": preview["transmission_preview"],
                    "serialized_request": preview["serialized_request"]}
        self.optional_ai_preview_text.configure(state="normal")
        self.optional_ai_preview_text.delete("1.0", "end")
        self.optional_ai_preview_text.insert("1.0", json.dumps(rendered, ensure_ascii=False, indent=2))
        self.optional_ai_preview_text.configure(state="disabled")
        self.optional_ai_confirm.set(False)
        self.optional_ai_confirm_button.configure(state="normal")
        self.optional_ai_send_button.configure(state="disabled")
        self._ai_status("preview_ready", preview["payload_sha256"])

    def _optional_ai_confirmation_changed(self) -> None:
        state = "normal" if self.optional_ai_confirm.get() and hasattr(
            self, "optional_ai_current_preview") else "disabled"
        self.optional_ai_send_button.configure(state=state)

    def _send_optional_ai(self) -> None:
        if not self.optional_ai_confirm.get():
            raise PermissionError(self._ait("confirmation_required"))
        preview = dict(self.optional_ai_current_preview)
        provider = self.var("optional_ai_provider").get().strip()
        endpoint = self.var("optional_ai_endpoint").get().strip()
        model = self.var("optional_ai_model").get().strip()
        if (provider, endpoint.rstrip("/"), model) != (
                preview["provider"], preview["endpoint"], preview["model"]):
            raise PermissionError(self._ait("settings_changed_after_preview"))
        account = OptionalAI.credential_account(provider, endpoint)
        report_id = self.var("optional_ai_report").get() or None
        service_config = self._optional_ai_service_config()
        cancel_event = threading.Event()
        self.optional_ai_cancel_event = cancel_event
        self.optional_ai_send_button.configure(state="disabled")
        self.optional_ai_cancel_button.configure(state="normal")

        def work() -> dict[str, Any]:
            with self._optional_ai_service(service_config) as service:
                token = service.approve_preview(
                    preview["preview_id"], user_confirmed=True,
                    payload_sha256=preview["payload_sha256"])
                result = service.send_approved(
                    preview["preview_id"], token, account=account, cancel_event=cancel_event)
                return service.save_draft(result["request_id"], report_id=report_id)

        self._ai_status("external_send_running")
        self._ai_async(work, self._show_optional_ai_draft)

    def _show_optional_ai_draft(self, draft: dict[str, Any]) -> None:
        output = getattr(self, "optional_ai_output_text", None)
        if output is None or not output.winfo_exists():
            self._ai_status("draft_saved", draft["path"])
            return
        output.delete("1.0", "end")
        output.insert("1.0", draft["content"])
        self.optional_ai_confirm.set(False)
        self.optional_ai_confirm_button.configure(state="disabled")
        self._ai_status("draft_saved", draft["path"])

    def cancel_optional_ai(self) -> None:
        event = getattr(self, "optional_ai_cancel_event", None)
        if event is not None:
            event.set()
            self._ai_status("cancel_requested")
