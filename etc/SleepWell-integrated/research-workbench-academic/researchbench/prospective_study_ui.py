"""Project-local study preparation, diary collection and aggregate reporting."""
from __future__ import annotations

import json
import threading
import tkinter as tk
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from .prospective_study import ProspectiveStudy

PROTOCOL_PATH = Path(__file__).with_name("resources") / "prospective_protocol.json"


class ProspectiveStudyUIMixin:
    def _st(self, ko, en):
        return en if self.lang == "en" else ko

    def _study_service(self):
        path = self.var("study_root").get().strip()
        if not path:
            raise ValueError(self._st("연구를 만들거나 연구 폴더를 선택하세요.", "Create or select a study first."))
        return ProspectiveStudy(path)

    def capture_study_drafts(self):
        for name in ("protocol",):
            box = getattr(self, f"study_{name}_editor", None)
            if box is not None and box.winfo_exists():
                self.var(f"study_{name}_draft").set(box.get("1.0", "end-1c"))

    def _study_display(self, value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        # Enrollment tokens are displayed only in the transient receipt box.
        box = getattr(self, "study_output", None)
        if box is not None and box.winfo_exists():
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", rendered)
            box.configure(state="disabled")

    def _study_button(self, parent, ko, en, callback):
        button = ttk.Button(parent, text=self._st(ko, en), command=lambda: self.safe(callback))
        button.pack(side="left", padx=(0, 5), pady=3)
        return button

    def build_diary_study(self, parent):
        ttk.Label(parent, text=self._st(
            "웨어러블 없는 전향 검증 · 아침 자기보고 일지 · 정확도 평가 동안 예측 비공개",
            "Prospective diary validation · No wearable required · Predictions withheld"),
            style="AcademicTitle.TLabel").pack(anchor="w", pady=4)
        ttk.Label(parent, text=self._st(
            "본 연구: 500명·28밤(7밤 초기 + 21밤 평가), 280명 축소안은 모집 전 동결. 예비조사: 별도 30명·14밤. "
            "실제 연구는 승인/면제 근거·동의·책임자·모집일을 기록한 뒤 시작합니다. 생성된 초안은 승인이나 전향 검증 결과가 아닙니다.",
            "Main plan: 500 people × 28 nights; a 280-person alternative must be fixed before recruitment. "
            "Feasibility: separate 30 people × 14 nights. A draft is neither ethics approval nor prospective evidence."),
            wraplength=1050).pack(fill="x", pady=5)
        row = ttk.Frame(parent); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.var("study_root")).pack(side="left", fill="x", expand=True)
        self._study_button(row, "연구 폴더 선택", "Select study", self._select_diary_study)
        self._study_button(row, "현황", "Status", self._refresh_diary_study)
        self._study_button(row, "집계 보고서 저장", "Export aggregate report", self._export_diary_study)
        notebook = ttk.Notebook(parent); notebook.pack(fill="both", expand=True, pady=6)
        setup, enrollment, collection = (ttk.Frame(notebook, padding=6) for _ in range(3))
        for tab, ko, en in ((setup, "프로토콜 준비", "Prepare protocol"),
                            (enrollment, "동의 후 참여자 등록", "Enroll after consent"),
                            (collection, "일지 수집 서버", "Diary server")):
            notebook.add(tab, text=self._st(ko, en))
        if not self.var("study_protocol_draft").get():
            self.var("study_protocol_draft").set(PROTOCOL_PATH.read_text(encoding="utf-8"))
        self.study_protocol_editor = tk.Text(setup, height=13, wrap="word")
        self.study_protocol_editor.pack(fill="both", expand=True)
        self.study_protocol_editor.insert("1.0", self.var("study_protocol_draft").get())
        row = ttk.Frame(setup); row.pack(fill="x")
        self._study_button(row, "새 본 연구 준비", "Create main study", lambda: self._create_diary_study(False))
        self._study_button(row, "새 예비조사 준비", "Create feasibility study", lambda: self._create_diary_study(True))
        ttk.Label(setup, text=self._st("프로토콜은 생성 후 고정됩니다. 수정 시 새 연구 폴더를 만들며 기존 기록은 유지합니다.",
                                       "Protocols freeze at creation. Revisions require a new study folder."), wraplength=1000).pack(anchor="w")
        if not self.var("study_enrollment_draft").get():
            self.var("study_enrollment_draft").set(json.dumps({
                "consent_confirmed": False, "sex": "unknown", "shift_work": "unknown",
                "recruitment_source": "unknown", "sleep_medication": "unknown", "sleep_diagnosis": "unknown",
                "device_access": "unknown", "profile": {
                    "age": "", "usual_bed": "", "usual_wake": "", "target_wake": "", "weekend_wake": "",
                    "usual_sleep_h": "", "desired_sleep_h": "", "latency_min": "",
                    "average_steps": "", "exercise_min": ""}}, ensure_ascii=False, indent=2))
        ttk.Label(enrollment, text=self._st("실제 동의와 설문 응답을 입력하세요. 토큰은 등록 직후 한 번만 표시되며 프로젝트 화면 설정에 저장하지 않습니다.",
            "Enter actual consent and questionnaire responses. The token appears once and is not saved in UI preferences."), wraplength=1000).pack(anchor="w")
        self.study_enrollment_editor = tk.Text(enrollment, height=12, wrap="word")
        self.study_enrollment_editor.pack(fill="both", expand=True)
        self.study_enrollment_editor.insert("1.0", self.var("study_enrollment_draft").get())
        row = ttk.Frame(enrollment); row.pack(fill="x")
        self._study_button(row, "참여자 등록 · 토큰 발급", "Enroll and issue token", self._enroll_diary_participant)
        ttk.Label(collection, text=self._st(
            "기본 주소는 이 PC에서만 접속됩니다. 휴대폰 수집은 지속 가동하는 HTTPS 서버와 신뢰할 수 있는 인증서가 필요합니다. "
            "서버를 켜 두면 20:00–20:15에 자동으로 예측을 고정합니다. 꺼진 시간의 예측은 재생성하지 않습니다.",
            "Loopback is this PC only. Phone access needs a continuously running HTTPS host and trusted certificate. "
            "The running server captures at 20:00–20:15 KST; missed forecasts are never backfilled."), wraplength=1000).pack(fill="x", pady=4)
        for key, ko, en, default in (("host", "수신 주소", "Listen address", "127.0.0.1"),
                                     ("port", "포트", "Port", "8767"),
                                     ("certfile", "TLS 인증서 경로", "TLS certificate path", ""),
                                     ("keyfile", "TLS 개인키 경로", "TLS private-key path", "")):
            if not self.var("study_" + key).get(): self.var("study_" + key).set(default)
            row = ttk.Frame(collection); row.pack(fill="x", pady=3)
            ttk.Label(row, text=self._st(ko, en), width=22).pack(side="left")
            ttk.Entry(row, textvariable=self.var("study_" + key)).pack(side="left", fill="x", expand=True)
        row = ttk.Frame(collection); row.pack(fill="x")
        self._study_button(row, "수집 서버 시작", "Start server", self._start_diary_server)
        self._study_button(row, "서버 중지", "Stop server", self.stop_diary_server)
        self._study_button(row, "이 PC에서 입력폼 보기", "Preview on this PC", self._preview_diary_form)
        self.study_output = tk.Text(parent, height=8, wrap="word", state="disabled")
        self.study_output.pack(fill="both", expand=True, pady=3)
        if self.var("study_root").get(): self.safe(self._refresh_diary_study)

    def _create_diary_study(self, feasibility=False):
        self.capture_study_drafts()
        protocol = json.loads(self.var("study_protocol_draft").get())
        if feasibility:
            protocol.update(phase="feasibility", duration_days=14, adaptation_days=7, primary_days=7,
                            minimum_primary_nights_for_completer=5, enrollment_target=30, completeness_target=24,
                            primary_endpoint="Feasibility only: completion, burden, capture availability; no validation claim.")
        else:
            protocol.update(phase="main_validation", duration_days=28, adaptation_days=7, primary_days=21,
                            minimum_primary_nights_for_completer=14)
        self.stop_diary_server()
        root = self.project.path / "research" / "diary_studies" / (protocol["phase"] + "_" + uuid.uuid4().hex[:12])
        result = ProspectiveStudy(root).create(protocol)
        self.var("study_root").set(str(root))
        self._study_display(result)
        self.save()

    def _select_diary_study(self):
        selected = filedialog.askdirectory(parent=self.root)
        if selected:
            service = ProspectiveStudy(selected)
            result = service.status()
            self.stop_diary_server()
            self.var("study_root").set(str(service.root))
            self._study_display(result)

    def _enroll_diary_participant(self):
        metadata = json.loads(self.study_enrollment_editor.get("1.0", "end-1c"))
        receipt = self._study_service().enroll(metadata)
        self._study_display(receipt)
        # The response may include sensitive baseline responses; do not persist the editor.
        self.study_enrollment_editor.delete("1.0", "end")
        self.study_enrollment_editor.insert("1.0", "{}")
        self.var("study_enrollment_draft").set("{}")

    def _refresh_diary_study(self):
        result = self._study_service().status()
        server = getattr(self, "_study_http_server", None)
        result["server_running_in_this_window"] = server is not None
        if server is not None:
            result["scheduler_status"] = server.scheduler_status
            result["scheduler_error_type"] = server.scheduler_error_type
        self._study_display(result)

    def _export_diary_study(self):
        destination = filedialog.askdirectory(parent=self.root)
        if destination:
            target = Path(destination) / ("study_report_" + uuid.uuid4().hex[:12])
            self._study_display(str(self._study_service().export(target)))

    def _start_diary_server(self):
        from .diary_server import make_server
        if getattr(self, "_study_http_server", None) is not None:
            raise ValueError(self._st("수집 서버가 이미 실행 중입니다.", "The server is already running."))
        server = make_server(self._study_service(), self.var("study_host").get(),
                             int(self.var("study_port").get()), self.var("study_certfile").get() or None,
                             self.var("study_keyfile").get() or None)
        self._study_http_server = server
        self._study_http_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._study_http_thread.start()
        self._refresh_diary_study()

    def stop_diary_server(self):
        server = getattr(self, "_study_http_server", None)
        if server is not None:
            server.shutdown()
            server.server_close()
            self._study_http_thread.join(timeout=2)
            self._study_http_server = None

    def _preview_diary_form(self):
        server = getattr(self, "_study_http_server", None)
        if server is None:
            raise ValueError(self._st("수집 서버를 먼저 시작하세요.", "Start the server first."))
        scheme = "https" if self.var("study_certfile").get() else "http"
        webbrowser.open(f"{scheme}://127.0.0.1:{server.server_port}/")
