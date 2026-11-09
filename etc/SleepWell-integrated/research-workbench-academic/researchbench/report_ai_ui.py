"""Optional AI connection and report controls for the three-page workbench."""
from __future__ import annotations

import copy
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from . import ai_connections as connections
from .evidence_library import EvidenceLibrary
from .reporting import ReportService


def generate_ai_report(project_path: str | Path, view: dict[str, Any], selection: dict[str, Any],
                       purpose: str, language: str, settings: dict[str, Any], *,
                       cancel_event=None, on_progress=None, client=None) -> dict[str, Any]:
    """Freeze and verify the chosen experiment before any model request."""
    from .guideline_retrieval import prepare_guideline_retrieval
    from .report_agents import AgentRunCancelled, ReportAgentHarness

    def checkpoint(stage):
        if cancel_event is not None and cancel_event.is_set():
            raise AgentRunCancelled("agent_run_cancelled")
        if on_progress:
            on_progress({"stage": stage, "state": "running"})

    checkpoint("retrieval")
    retrieval = prepare_guideline_retrieval(project_path)
    with EvidenceLibrary(project_path) as library:
        service = ReportService(project_path, library)
        draft = service.create_comparison(view, selection, lang=language, question=purpose,
                                          citations=list(retrieval.get("citations") or []))
        checked = service.apply_check(draft["report_id"])
        if checked["automatic_check"]["errors"]:
            raise ValueError("blocking_report_check_errors")
        checkpoint("evidence_check")
        harness_settings = {
            "max_calls": settings["max_calls"], "max_output_tokens": settings["max_output_tokens"],
            "per_call_timeout_seconds": settings["timeout_seconds"],
            "total_timeout_seconds": settings["total_timeout_seconds"],
            "max_cost_usd": settings.get("budget_usd"),
        }
        provider = client if client is not None else connections.ProviderClient(settings, cancel_event=cancel_event)
        analysis = ReportAgentHarness(project_path, provider, settings=harness_settings,
                                      cancel_event=cancel_event, on_progress=on_progress).run(checked, authorized=True)
        checkpoint("verification")
        report = service.attach_ai_analysis(checked["report_id"], analysis)
    return {"report": report, "retrieval": retrieval}


class ReportAIUIMixin:
    def _ai_text(self, ko: str, en: str) -> str:
        return en if self.lang == "en" else ko

    def _build_report_ai_controls(self, parent: tk.Misc) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(6, 0))
        self.report_ai_settings_button = ttk.Button(
            row, text=self._ai_text("AI 연결 설정", "AI connection"),
            command=lambda: self.safe(self._report_ai_settings))
        self.report_ai_settings_button.pack(side="left")
        ttk.Label(row, textvariable=self.var("knowledge_ai_connection"), wraplength=750).pack(side="left", padx=8)
        ttk.Label(parent, text=self._ai_text(
            "AI 작성 시 선택 모델의 집계 지표·보고 목적·지침 발췌·체크리스트·한계·실행 식별자와 에이전트 분석을 선택한 서비스에 전송합니다. 원시 수면 기록은 제외합니다.",
            "AI generation sends aggregate report context, purpose, guideline excerpts, checklists, limitations, provenance identifiers and agent analyses to the selected service. Raw sleep records are excluded."),
            wraplength=950, justify="left").pack(fill="x", pady=5)
        row = ttk.Frame(parent)
        row.pack(fill="x")
        self.report_ai_generate_button = ttk.Button(row, text=self._ai_text("AI 근거 분석·보고서 작성", "Analyze evidence and write report"),
            style="Primary.TButton", command=lambda: self.safe(self._report_ai_generate))
        self.report_ai_generate_button.pack(side="left", padx=(0, 7))
        self.report_ai_cancel_button = ttk.Button(row, text=self._ai_text("중단", "Cancel"), state="disabled",
                                                  command=self._report_ai_cancel)
        self.report_ai_cancel_button.pack(side="left", padx=(0, 7))
        self.report_ai_export_button = ttk.Button(row, text=self._ai_text("선택 보고서 PDF 저장", "Save selected report PDF"),
            command=lambda: self.safe(self._report_ai_export))
        self.report_ai_export_button.pack(side="left")
        ttk.Label(parent, textvariable=self.var("knowledge_ai_progress"), wraplength=950).pack(fill="x", pady=4)
        self._report_ai_refresh()

    def _report_ai_refresh(self) -> None:
        try:
            settings = connections.load_settings(self.project.path)
            meta = next(item for item in connections.provider_catalog() if item["id"] == settings["provider"])
            # Refresh status only reads the OS credential store; no HTTP requests.
            status = connections.credential_status(settings)
            ready = status.get("connected") is True or (not meta["subscription"] and status.get("available"))
            label = self._ai_text("연결 정보 저장됨", "Credentials saved") if ready else self._ai_text("연결 필요", "Connection needed")
            self.var("knowledge_ai_connection").set(f"{meta['label']} · {settings.get('model') or self._ai_text('모델 선택 필요', 'Choose model')} · {label}")
        except Exception:
            self.var("knowledge_ai_connection").set(self._ai_text("AI 연결 설정을 확인하세요.", "Check AI connection settings."))
        button = getattr(self, "report_ai_generate_button", None)
        if button is not None:
            try:
                view = getattr(self, "simple_report_sources", {}).get(self.var("knowledge_simple_report_source").get())
                selection = self._simple_report_comparison_selection(view) if view else None
                enabled = bool(selection and selection.get("model_ids")) and not getattr(self, "_knowledge_worker_active", False)
                if button.winfo_exists():
                    button.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _report_ai_settings(self) -> None:
        if getattr(self, '_knowledge_worker_active', False):
            self.var('knowledge_ai_progress').set(self._ai_text('진행 중인 작업이 끝난 뒤 연결 설정을 변경할 수 있습니다.',
                                                               'Connection settings can be changed after the active operation finishes.'))
            return
        existing = getattr(self, "report_ai_settings_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        settings = connections.load_settings(self.project.path)
        catalog = [item for item in connections.provider_catalog() if item['id'] not in {'codex_subscription', 'claude_subscription'}]
        migration = {'codex_subscription': 'chatgpt_subscription', 'claude_subscription': 'anthropic'}
        if settings['provider'] in migration:
            settings['provider'] = migration[settings['provider']]
            settings['endpoint'] = next(item['default_endpoint'] for item in catalog if item['id'] == settings['provider'])
            settings['model'] = ''
        window = tk.Toplevel(self.root)
        self.report_ai_settings_window = window
        window.title(self._ai_text("SleepWell · AI 연결", "SleepWell · AI connection"))
        window.geometry("790x650")
        window.minsize(650, 560)
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        variables = {key: tk.StringVar(window, value="" if value is None else str(value)) for key, value in settings.items()}
        key_var = tk.StringVar(window)
        status_var = tk.StringVar(window)
        self.report_ai_settings_vars = variables
        self.report_ai_key_var = key_var
        self.report_ai_settings_status = status_var
        ttk.Label(panel, text=self._ai_text("API 키 또는 ChatGPT 공식 로그인", "API key or official ChatGPT sign-in"),
                  font=("Malgun Gothic", 13, "bold")).pack(anchor="w", pady=(0, 8))
        combo = ttk.Combobox(panel, state="readonly", values=[item["label"] for item in catalog])
        combo.current(next(i for i, item in enumerate(catalog) if item["id"] == settings["provider"]))
        combo.pack(fill="x", pady=4)
        self.report_ai_provider_combo = combo

        def field(label, key, *, secret=False):
            row = ttk.Frame(panel)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, width=21).pack(side="left")
            entry = ttk.Entry(row, textvariable=key_var if secret else variables[key], show="•" if secret else "")
            entry.pack(side="left", fill="x", expand=True)
            return entry

        endpoint = field(self._ai_text("API 주소", "API endpoint"), "endpoint")
        key_entry = field("API key", "", secret=True)
        ttk.Label(panel, text=self._ai_text("키와 로그인 토큰은 Windows 자격 증명 저장소에 보관됩니다.",
                                          "Keys and login tokens stay in the Windows credential store."), wraplength=730).pack(anchor="w")
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=8)
        ttk.Label(row, text=self._ai_text("사용할 모델", "Model"), width=21).pack(side="left")
        models = ttk.Combobox(row, textvariable=variables["model"])
        models.pack(side="left", fill="x", expand=True)
        self.report_ai_model_combo = models
        note = ttk.Label(panel, wraplength=730, justify="left")
        note.pack(fill="x", pady=6)
        limit_entries = {}
        other_account = tk.BooleanVar(window, value=False)
        switch_account = ttk.Checkbutton(panel, text=self._ai_text('다른 ChatGPT 계정으로 새 연결', 'Connect a different ChatGPT account'), variable=other_account)
        switch_account.pack(anchor='w', pady=2)

        def snapshot():
            value = dict(settings)
            for key in ("provider", "endpoint", "model", "max_calls", "max_output_tokens", "timeout_seconds", "total_timeout_seconds"):
                value[key] = variables[key].get()
            for key in ("budget_usd", "input_price_per_million", "output_price_per_million"):
                value[key] = variables[key].get() or None
            return value

        def save():
            if getattr(self, '_knowledge_worker_active', False):
                raise ValueError(self._ai_text('진행 중인 작업을 먼저 중단하세요.', 'Stop the active operation first.'))
            value = connections.save_settings(self.project.path, snapshot())
            if key_var.get().strip():
                connections.store_key(value, key_var.get().strip())
                key_var.set("")
            self._report_ai_refresh()
            status_var.set(self._ai_text("설정을 저장했습니다. 모델 목록 또는 연결 확인을 누르세요.", "Saved. Load models or check the connection."))
            return value

        def guarded(action):
            try:
                action()
            except Exception as exc:
                status_var.set(self._ai_text("설정 확인: ", "Check settings: ") + str(exc))

        def provider_changed(_event=None):
            meta = catalog[combo.current()]
            variables["provider"].set(meta["id"])
            variables["endpoint"].set(meta["default_endpoint"])
            variables["model"].set("")
            models.configure(values=[])
            key_var.set("")
            show_provider(meta)

        def show_provider(meta):
            sub = meta["subscription"]
            key_entry.configure(state="disabled" if sub else "normal")
            endpoint.configure(state="normal" if meta["id"] == "openai_compatible" else "readonly")
            login_button.configure(state="normal" if meta.get("login_supported") else "disabled")
            switch_account.configure(state='normal' if meta.get('login_supported') else 'disabled')
            if meta.get("login_supported"):
                text = self._ai_text("ChatGPT 공식 로그인과 권한 동의로 연결합니다. 구독 경로는 출력 토큰·USD 예산 제한을 지원하지 않습니다. 호출 수·시간·응답 크기 제한을 적용합니다.",
                                    "Connect eligible ChatGPT usage through official browser consent. Output-token and USD caps are unavailable; call, time and response-size limits apply.")
            elif sub:
                text = self._ai_text("이 제공자의 구독 직접 연결은 지원하지 않습니다. Anthropic API를 선택해 API 키를 입력하세요.",
                                    "Direct subscription connection is not supported here. Choose Anthropic API and enter an API key.")
            else:
                text = self._ai_text("API 사용료는 구독과 별도일 수 있습니다. 모델 목록을 불러와 선택하거나 사용 가능한 모델 ID를 입력하세요.",
                                    "API billing may be separate from subscriptions. Load models or enter an available model ID.")
            note.configure(text=text)
            for key in ('max_output_tokens', 'budget_usd', 'input_price_per_million', 'output_price_per_million'):
                if key in limit_entries:
                    limit_entries[key].configure(state='disabled' if sub else 'normal')

        def network_action(kind):
            if getattr(self, "_knowledge_worker_active", False):
                status_var.set(self._ai_text("현재 작업이 끝난 뒤 다시 시도하세요.", "Wait for the current operation."))
                return
            value = save()
            if kind == "login":
                from . import chatgpt_auth
                new_account = bool(other_account.get())
                work = lambda event, progress: chatgpt_auth.login(cancel_event=event, on_progress=progress, new_account=new_account)
            elif kind == "models":
                work = lambda event, progress: connections.list_models(value, cancel_event=event)
            else:
                work = lambda event, progress: connections.test_connection(value, cancel_event=event)

            def done(result):
                self._report_ai_refresh()
                if not window.winfo_exists():
                    return
                if kind == "models":
                    models.configure(values=result)
                    if result and not variables["model"].get():
                        variables["model"].set(result[0])
                    status_var.set(self._ai_text("모델을 선택한 뒤 설정을 저장하세요.", "Choose a model and save settings."))
                else:
                    if kind == 'login' and not result.get('plan_scope_granted'):
                        status_var.set(self._ai_text('로그인은 완료했지만 구독 사용 권한이 없습니다. API 키 연결을 사용할 수 있습니다.',
                                                    'Signed in, but plan usage was not granted. You can use an API key.'))
                    else:
                        status_var.set(self._ai_text("연결 확인 완료. 모델 목록을 불러오세요. 보고서는 아직 생성하지 않았습니다.",
                                                    "Connection checked. Load models. No report has been generated."))
            self._report_ai_background(work, done, error_sink=status_var)
            status_var.set(self._ai_text("연결 중… 브라우저가 열리면 로그인을 완료하세요.", "Connecting… complete sign-in if the browser opens."))

        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=5)
        login_button = ttk.Button(buttons, text=self._ai_text("ChatGPT로 로그인", "Sign in with ChatGPT"),
                                  command=lambda: guarded(lambda: network_action("login")))
        login_button.pack(side="left", padx=(0, 6))
        self.report_ai_login_button = login_button
        ttk.Button(buttons, text=self._ai_text("모델 목록", "Load models"), command=lambda: guarded(lambda: network_action("models"))).pack(side="left", padx=6)
        ttk.Button(buttons, text=self._ai_text("연결 확인", "Check connection"), command=lambda: guarded(lambda: network_action("test"))).pack(side="left", padx=6)
        ttk.Button(buttons, text=self._ai_text("중단", "Cancel"), command=self._report_ai_cancel).pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", provider_changed)
        show_provider(catalog[combo.current()])

        advanced = ttk.Frame(panel)
        def toggle_limits():
            if advanced.winfo_manager():
                advanced.pack_forget()
            else:
                advanced.pack(fill="x", pady=4, before=footer)
        ttk.Button(panel, text=self._ai_text("실행 한도·예산 설정", "Execution limits and budget"), command=toggle_limits).pack(anchor="w", pady=5)
        for key, label in (("max_calls", "최대 호출 / Calls"), ("max_output_tokens", "출력 토큰 / Output tokens"),
                           ("timeout_seconds", "호출 제한(초) / Call timeout"), ("total_timeout_seconds", "전체 제한(초) / Total timeout"),
                           ("budget_usd", "예산 USD (선택)"), ("input_price_per_million", "입력 백만 토큰 USD"),
                           ("output_price_per_million", "출력 백만 토큰 USD")):
            r = ttk.Frame(advanced)
            r.pack(fill="x", pady=1)
            ttk.Label(r, text=label, width=30).pack(side="left")
            entry = ttk.Entry(r, textvariable=variables[key], width=18)
            entry.pack(side="left")
            limit_entries[key] = entry
        show_provider(catalog[combo.current()])
        footer = ttk.Frame(panel)
        footer.pack(fill="x", pady=8)
        self.report_ai_save_settings_button = ttk.Button(footer, text=self._ai_text("설정 저장", "Save settings"), command=lambda: guarded(save))
        self.report_ai_save_settings_button.pack(side="left", padx=(0, 8))

        def forget():
            if getattr(self, '_knowledge_worker_active', False):
                status_var.set(self._ai_text('진행 중인 작업을 중단한 후 연결 정보를 삭제하세요.', 'Stop the active operation before removing credentials.'))
                return
            # Invalid unsaved limit fields must never prevent credential deletion.
            value = connections.load_settings(self.project.path)
            meta = catalog[combo.current()]
            value['provider'] = meta['id']
            value['endpoint'] = variables['endpoint'].get()
            if meta.get("login_supported"):
                from . import chatgpt_auth
                def logged_out(result):
                    self._report_ai_refresh()
                    status_var.set(self._ai_text('로컬 로그인 정보를 삭제했습니다.', 'Local sign-in cleared.') +
                                   (self._ai_text(' 서버 세션 해제는 확인하지 못했습니다.', ' Remote revocation was not confirmed.')
                                    if result.get('revocation_attempted') and result.get('revoked') is False else ''))
                self._report_ai_background(lambda event, progress: chatgpt_auth.logout(), logged_out, error_sink=status_var)
                return
            elif not meta["subscription"]:
                connections.delete_key(value)
            key_var.set("")
            status_var.set(self._ai_text("저장된 연결 정보를 삭제했습니다.", "Stored credentials removed."))
            self._report_ai_refresh()
        ttk.Button(footer, text=self._ai_text("키 삭제 / 로그아웃", "Delete key / Sign out"), command=lambda: guarded(forget)).pack(side="left", padx=5)
        ttk.Button(footer, text=self._ai_text("닫기", "Close"), command=window.destroy).pack(side="right")
        ttk.Label(panel, textvariable=status_var, wraplength=730, justify="left").pack(fill="x", pady=5)
        # Secret entry values are not included in project draft persistence.
        window.bind("<Destroy>", lambda e: key_var.set("") if e.widget is window else None)

    def _report_ai_background(self, work, complete, *, error_sink=None) -> None:
        if getattr(self, "_knowledge_worker_active", False):
            return
        self._knowledge_worker_active = True
        event = threading.Event()
        self._report_ai_cancel_event = event
        messages = queue.Queue()
        def run():
            try:
                messages.put(("ok", work(event, lambda info: messages.put(("progress", info)))))
            except Exception as exc:
                messages.put(("error", exc))
        threading.Thread(target=run, daemon=True, name="sleepwell-report-agents").start()
        self._report_ai_refresh()
        cancel = getattr(self, "report_ai_cancel_button", None)
        if cancel is not None and cancel.winfo_exists():
            cancel.configure(state="normal")
        stages = {"retrieval": "공식 근거 검색", "evidence_check": "실험 근거 확인", "performance_analyst": "성능 분석 에이전트",
                  "guideline_reviewer": "TRIPOD·PROBAST 검토 에이전트", "report_writer": "보고서 작성 에이전트", "verification": "수치·출처 검사"}
        def poll():
            try:
                while True:
                    kind, value = messages.get_nowait()
                    if kind == "progress":
                        info = value if isinstance(value, dict) else {"stage": str(value)}
                        stage = str(info.get("stage", ""))
                        self.var("knowledge_ai_progress").set(self._ai_text(stages.get(stage, stage), stage) + " · " + str(info.get("state", "")))
                        continue
                    self._knowledge_worker_active = False
                    self._report_ai_cancel_event = None
                    self._report_ai_refresh()
                    current = getattr(self, "report_ai_cancel_button", None)
                    if current is not None and current.winfo_exists():
                        current.configure(state="disabled")
                    if kind == "error":
                        from .report_agents import AgentRunCancelled
                        cancelled = isinstance(value, AgentRunCancelled) or str(value) in {
                            'ai_request_cancelled', 'chatgpt_request_cancelled', 'chatgpt_login_cancelled'}
                        text = self._ai_text("작업을 중단했습니다.", "Operation cancelled.") if cancelled else self._ai_text("작업 실패: ", "Operation failed: ") + str(value)
                        self.var("knowledge_ai_progress").set(text)
                        if error_sink is not None:
                            error_sink.set(text)
                    else:
                        try:
                            complete(value)
                        except Exception as exc:
                            self.var("knowledge_ai_progress").set(str(exc))
                    return
            except queue.Empty:
                self.root.after(80, poll)
        self.root.after(40, poll)

    def _report_ai_cancel(self) -> None:
        event = getattr(self, "_report_ai_cancel_event", None)
        if event is not None:
            event.set()
            self.var("knowledge_ai_progress").set(self._ai_text("중단 요청됨 · 이미 전송한 요청은 과금될 수 있습니다.", "Cancellation requested; already sent requests may be billed."))

    def _report_ai_generate(self) -> None:
        if getattr(self, "_knowledge_worker_active", False):
            return
        view = getattr(self, "simple_report_sources", {}).get(self.var("knowledge_simple_report_source").get())
        selection = self._simple_report_comparison_selection(view) if view else None
        if not selection or not selection.get("model_ids"):
            raise ValueError(self._ai_text("원시 예측을 포함한 완료된 실험과 비교 모델을 선택하세요.", "Select a completed experiment with predictions and comparison models."))
        settings = connections.load_settings(self.project.path)
        status = connections.credential_status(settings)
        meta = next(item for item in connections.provider_catalog() if item["id"] == settings["provider"])
        ready = status.get("connected") is True if meta["subscription"] else status.get("available")
        if not ready or not settings.get("model"):
            self._report_ai_settings()
            self.var("knowledge_ai_progress").set(self._ai_text("AI 연결과 모델을 설정한 뒤 작성 버튼을 누르세요.", "Set up a connection and model, then generate the report."))
            return
        project_path, language = Path(self.project.path), str(self.lang)
        frozen_view, frozen_selection = copy.deepcopy(view), copy.deepcopy(selection)
        purpose = self.var("knowledge_simple_report_purpose").get().strip()
        self._report_ai_background(lambda event, progress: generate_ai_report(
            project_path, frozen_view, frozen_selection, purpose, language, settings,
            cancel_event=event, on_progress=progress), self._report_ai_complete)

    def _report_ai_complete(self, result: dict[str, Any]) -> None:
        report = result["report"]
        self.var("knowledge_selected_report").set(report["report_id"])
        self.var("knowledge_ai_progress").set(self._ai_text(
            "보고서와 실행 기록을 저장했습니다. 아래 미리보기를 확인한 후 PDF로 저장하세요.",
            "Report and execution record saved. Review the preview, then save the PDF."))
        self._simple_report_refresh_history(report["report_id"])

    def _report_ai_export(self) -> None:
        if getattr(self, "_knowledge_worker_active", False):
            return
        report_id = self.var("knowledge_selected_report").get()
        if not report_id:
            raise ValueError(self._ai_text("최근 보고서에서 출력할 보고서를 선택하세요.", "Choose a report from recent reports."))
        destination = filedialog.asksaveasfilename(parent=self.root, title=self._ai_text("보고서 PDF 저장", "Save report PDF"),
            initialfile="SleepWell_RAG_report.pdf", defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not destination:
            return
        project_path = Path(self.project.path)
        def export():
            with EvidenceLibrary(project_path) as library:
                service = ReportService(project_path, library)
                report = service.get(report_id)
                path = service.export_pdf(report_id, destination, mode="checked")
                return {"report": report, "path": path}
        self._knowledge_async(export, self._simple_report_complete)
