"""Native academic workspace; frozen SleepWell models and original evaluations."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .academic_backend import experiment_catalog, model_status
from .academic_jobs import AcademicQueue
from .academic_plotting import export_academic_tables, make_academic_figure, model_label
from .academic_prospective import default_profile, list_forecasts
from .academic_store import AcademicProject, write_json
from .app import ScrollFrame, tree
from .canvas import ResearchCanvas
from .i18n import read_language, save_language
from .plotting import export_figure
from .platform_ui import PlatformMixin, LABELS as PLATFORM_LABELS
from .knowledge_ui import KnowledgeUIMixin
from .evaluation_ui import EvaluationUIMixin
from .optional_ai_ui import OptionalAIMixin
from .guideline_ui import GuidelineUIMixin
from .prospective_study_ui import ProspectiveStudyUIMixin
from .simple_workflow import SimpleWorkflowMixin


ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((Path(__file__).parent / "locales/academic_ui.json").read_text(encoding="utf-8"))
PAGES = ["data", "experiments", "reports"]
MODES = ["retrospective", "personalization", "prospective_demo", "report"]


class AcademicApp(SimpleWorkflowMixin, PlatformMixin, KnowledgeUIMixin, EvaluationUIMixin, OptionalAIMixin, GuidelineUIMixin, ProspectiveStudyUIMixin):
    def __init__(self, root, project=None, language=None, preferences=True):
        self.root = root
        self.preferences = preferences
        self.lang = language or read_language() or "ko"
        self.page = "data"
        self.vars = {}
        self.project = None
        self.runner = None
        self.selected_run = None
        self.figure = None
        self.plot_canvas = None
        self.closing = False
        self.last_saved = time.monotonic()
        self.root.title(self.t("title"))
        self.root.geometry("1340x900")
        self.root.minsize(1060, 700)
        self.root.option_add("*Font", ("Malgun Gothic", 11))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Malgun Gothic", 11), background="#f6f7f6", foreground="#20342e")
        style.configure("Treeview", rowheight=30, fieldbackground="white", background="white")
        style.configure("TButton", padding=(10, 7))
        style.configure("Primary.TButton", background="#216451", foreground="white", padding=(14, 9))
        style.map("Primary.TButton", background=[('active', '#184b3c'), ('disabled', '#d5ddd9')], foreground=[('disabled', '#54655c')])
        style.configure("Step.TButton", anchor="w", padding=(12, 14))
        style.map("Step.TButton", background=[('pressed', '#d9e9e1')])
        style.configure("AcademicTitle.TLabel", font=("Malgun Gothic", 18, "bold"))
        style.configure("AcademicNote.TLabel", foreground="#516579")
        defaults = {"name": "", "mode": "retrospective", "input_mode": "file", "input_synthetic": "1",
                    "cmi": "1", "mesa": "1", "n_nights": "50", "seed": "42", "start_date": "2026-09-01",
                    "jitter_minutes": "12", "phase_shift_minutes": "45", "shift_after_night": "25",
                    "max_seconds": "600", "max_experiments": "3", "report_type": "research_diagnostics",
                    "metric": "joint_mae_min", "kind": "comparison", "title": "", "annotation": "",
                    "personalization_field": "personal_weight", "corr_method": "pearson", "show_table": "1",
                    "font_size": "10", "width": "10", "height": "5.5", "dpi": "300",
                    "cutoff": datetime.now().date().isoformat(), "forecast_id": "", "sleep_start": "",
                    "wake": "", "observed_at": "", "outcome_source": "diary", "online_personalization": "1",
                    "profile_json": json.dumps(default_profile(), ensure_ascii=False, indent=2)}
        defaults.update(workspace="sleepwell", linked_general="", baseline_run="", comparison_selection="[]",
                        as_of=datetime.now(timezone(timedelta(hours=9))).isoformat(),
                        cohort="cmi", record_source="diary", report_json="{}")
        for key, value in defaults.items(): self.var(key).set(value)
        if project is None:
            default = ROOT / "projects" / "SleepWell_Academic.swa"
            bundled = ROOT / "deliverables" / "academic_demo.swa"
            if not default.exists() and (bundled / "project.json").exists():
                import shutil
                default.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(bundled, default)
            if (default / "project.json").exists(): project = AcademicProject.open(default)
            else:
                project = AcademicProject.create(default, "SleepWell 학술제")
                sample = ROOT / "samples" / "sleepwell_50days.csv"
                if sample.exists(): project.import_data(sample, synthetic=True)
        self.attach(project)
        self.root.bind("<Control-s>", lambda event: self.save())
        self.root.bind("<Control-o>", lambda event: self.safe(self.open_project))
        self.root.bind("<Control-Return>", lambda event: self.safe(self.start_simple_experiment))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick_id = self.root.after(100, self.tick)

    def t(self, key):
        if key == "pooled_retraining":
            return "CMI·MESA 통합 재학습" if self.lang != "en" else "Pooled CMI/MESA retraining"
        if key == "controlled_comparison":
            return "CMI·MESA 동일조건 비교" if self.lang != "en" else "Controlled CMI/MESA comparison"
        if key == "cross_cohort_evaluation":
            return "CMI·MESA 동결 모델 교차 평가" if self.lang != "en" else "Frozen CMI/MESA cross-cohort evaluation"
        if key == "participant_revalidation":
            return "CMI·MESA 고정 절차 참여자 교차검증" if self.lang != "en" else "Fixed-procedure CMI/MESA participant CV"
        if key == "personalization_comparison":
            return "CMI·MESA 개인화 융합 비교" if self.lang != "en" else "CMI/MESA personalization and fusion comparison"
        if key == "joint_ci95":
            return "joint 95% CI" if self.lang != "en" else "Joint 95% CI"
        if key == "paired_difference":
            return "짝지은 차이" if self.lang != "en" else "Paired difference"
        value = CATALOG.get(key) or PLATFORM_LABELS.get(key)
        return value[1 if self.lang == "en" else 0] if value else key

    def warning_text(self, value):
        value = str(value)
        if "gradual_v1" in value: return self.t("gradual_note")
        if value.startswith("Privacy:"): return self.t("privacy_note")
        if value.startswith("Research comparison only:"): return self.t("research_note")
        if value.startswith("Synthetic chronological playback;"): return self.t("playback_note")
        if value.startswith("Generated outcomes are independent"): return self.t("synthetic_badge")
        return value

    def var(self, key):
        if key not in self.vars: self.vars[key] = tk.StringVar(self.root)
        return self.vars[key]

    def safe(self, function):
        try: return function()
        except Exception as error:
            self.status.set(f"{self.t('error')}: {error}")
            messagebox.showerror(self.t("error"), str(error), parent=self.root)

    def text(self, parent, key, style="TLabel"):
        label = ttk.Label(parent, text=self.t(key), style=style, wraplength=1040, justify="left")
        label.pack(fill="x", anchor="w", pady=5)
        return label

    def button(self, parent, key, command):
        button = ttk.Button(parent, text=self.t(key), command=lambda: self.safe(command))
        button.pack(side="left", padx=(0, 7), pady=4)
        return button

    def combo(self, parent, key, values, callback=None, width=34):
        widget = ttk.Combobox(parent, values=[self.t(v) for v in values], state="readonly", width=width)
        value = self.var(key).get()
        widget.set(self.t(value))
        def selected(event):
            if widget.current() >= 0: self.var(key).set(values[widget.current()])
            if callback: self.safe(callback)
        widget.bind("<<ComboboxSelected>>", selected)
        return widget

    def field(self, parent, key, values=None, callback=None):
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self.t(key), width=29).pack(side="left")
        widget = self.combo(row, key, values, callback) if values else ttk.Entry(row, textvariable=self.var(key))
        widget.pack(side="left", fill="x", expand=True)
        return widget

    def capture(self):
        self.capture_knowledge_drafts()
        self.capture_guideline_drafts()
        self.capture_study_drafts()
        if hasattr(self, "profile_editor") and self.profile_editor.winfo_exists():
            self.var("profile_json").set(self.profile_editor.get("1.0", "end-1c"))

    def attach(self, project):
        if self.project is not None and self.project.path != project.path:
            self.stop_diary_server()
            for key in list(self.vars):
                if key.startswith(('knowledge_','eval_','evaluation_','optional_ai_','participant_','guideline_','study_')) or key in {'linked_general','baseline_run','comparison_selection'}:
                    self.vars[key].set('[]' if key=='comparison_selection' else '')
            for key in list(vars(self)):
                if key.startswith(('knowledge_','evaluation_','optional_ai_','guideline_','_guideline_')): delattr(self,key)
        if hasattr(self, "profile_editor") and self.profile_editor.winfo_exists():
            self.profile_editor.destroy()
        self.project = project
        ui = project.state.get("ui", {})
        for key, value in ui.get("vars", {}).items():
            self.var(key).set(value)
        self.var('workspace').set('sleepwell')
        self.page = ui.get("page", "data")
        if self.page not in PAGES: self.page = "data"
        self.selected_run = ui.get("selected_run")
        self.runner = AcademicQueue(project, self.on_record)
        self.build()

    def save(self):
        self.capture()
        if self.project:
            self.project.state.setdefault("ui", {}).update({"vars": {k: v.get() for k, v in self.vars.items()},
                                        "page": self.page, "selected_run": self.selected_run})
            self.project.save()
            self.last_saved = time.monotonic()

    def ensure_switch(self):
        if self.runner and self.runner.busy or getattr(self,'_knowledge_worker_active',False) or getattr(self,'_evaluation_busy',False) or getattr(self,'_optional_ai_worker',False) or getattr(self,'_guideline_busy',False):
            raise ValueError(self.t("busy"))
        self.save()

    def open_project(self):
        self.ensure_switch()
        path = filedialog.askdirectory(parent=self.root, title=self.t("open"))
        if path: self.attach(AcademicProject.open(path))

    def new_project(self):
        self.ensure_switch()
        path = filedialog.asksaveasfilename(parent=self.root, title=self.t("new"), defaultextension=".swa", filetypes=[("Academic project", "*.swa")])
        if path: self.attach(AcademicProject.create(path, Path(path).stem))

    def change_language(self, language):
        self.capture()
        self.lang = language
        if self.preferences: save_language(language)
        self.build()

    def build(self):
        self.capture()
        self.root.title(self.t("title"))
        if hasattr(self, "shell"): self.shell.destroy()
        self.plot_canvas = None
        self.figure = None
        self.shell = ttk.Frame(self.root); self.shell.pack(fill="both", expand=True)
        top = ttk.Frame(self.shell, padding=(14, 10)); top.pack(fill="x")
        ttk.Label(top, text=self.t("title"), style="AcademicTitle.TLabel").pack(side="left")
        language = ttk.Combobox(top, values=["한국어", "English"], width=10, state="readonly")
        language.set("한국어" if self.lang == "ko" else "English")
        language.bind("<<ComboboxSelected>>", lambda event: self.change_language("ko" if language.current() == 0 else "en"))
        language.pack(side="right")
        self.header = ttk.Label(self.shell, padding=(15, 0), wraplength=1360)
        self.header.pack(fill="x")
        controls = ttk.Frame(self.shell, padding=(14, 5)); controls.pack(fill="x")
        for key, callback in [("open", self.open_project), ("save", self.save)]:
            self.button(controls, key, callback)
        project_menu = ttk.Menubutton(controls, text=self.sw('프로젝트', 'Project'))
        menu = tk.Menu(project_menu, tearoff=False)
        menu.add_command(label=self.t('new'), command=lambda: self.safe(self.new_project))
        project_menu.configure(menu=menu); project_menu.pack(side='left', padx=6)
        body = ttk.Panedwindow(self.shell, orient="horizontal"); body.pack(fill="both", expand=True)
        nav = ttk.Frame(body, padding=12); body.add(nav, weight=0)
        self.workflow_buttons = {}
        for index, page in enumerate(PAGES):
            button = ttk.Button(nav, text=f"{index+1}  {self.workflow_label(page)}", style='Step.TButton', command=lambda p=page: self.navigate(p))
            button.pack(fill="x", pady=5)
            self.workflow_buttons[page] = button
        self.content = ttk.Frame(body, padding=(12, 8)); body.add(self.content, weight=1)
        self.status = tk.StringVar(self.root, value=self.t("saved"))
        ttk.Label(self.shell, textvariable=self.status, padding=(14, 7)).pack(fill="x", side="bottom")
        self.navigate(self.page)
        self.refresh_header()

    def navigate(self, page):
        self.capture()
        self.page = page
        self.plot_canvas = None
        self.figure = None
        for child in self.content.winfo_children(): child.destroy()
        self.scroll = ScrollFrame(self.content); self.scroll.pack(fill="both", expand=True)
        parent = self.scroll.body
        for key, button in self.workflow_buttons.items():
            button.state(['pressed'] if key == page else ['!pressed'])
        if page in PAGES:
            ttk.Label(parent, text=self.workflow_label(page), style='AcademicTitle.TLabel').pack(fill='x', pady=(4, 12))
            builders = {'data': self.build_quality, 'experiments': self.build_experiments, 'reports': self.build_simple_reports}
            builders[page](parent)
        else:
            # Legacy panels remain callable for archived tooling; no default menu links.
            self.text(parent, page, "AcademicTitle.TLabel")
            getattr(self, "build_" + page)(parent)

    def refresh_header(self):
        item = self.project.state.get("input") or {}
        self.header.configure(text=f"{self.project.state['name']}  ·  {item.get('source_name', self.sw('데이터 없음', 'No data'))}")

    def records(self):
        records = {r["id"]: r for r in self.project.runs()}
        if self.runner.active: records[self.runner.active["record"]["id"]] = self.runner.active["record"]
        return sorted(records.values(), key=lambda r: r["created"])

    def current_record(self):
        completed = [r for r in self.records() if r["status"] == "completed"]
        selected = next((r for r in completed if r["id"] == self.selected_run), None)
        return selected or (completed[-1] if completed else None)

    def build_overview(self, parent):
        self.platform_readiness(parent)
        if self.var('workspace').get()=='tabular':
            self.build_workspace_intro(parent)
            return
        if self.var('workspace').get()=='knowledge': return
        record = self.current_record()
        study_mode = (record or {}).get("result", {}).get("mode") in {
            "pooled_retraining", "controlled_comparison", "personalization_comparison",
        }
        if study_mode:
            comparison_mode = (record or {}).get("result", {}).get("mode") == "personalization_comparison"
            note = ((
                "DATA33·잔차 개인화·HYBRID39·수리 위상/임계값 개인화 융합을 같은 참여자 단위 교차검증으로 비교합니다. "
                "각 시점 이전에 관측된 기록만 개인화에 사용하며 독립 외부 검증은 아닙니다."
                if self.lang != "en" else
                "DATA33, residual personalization, HYBRID39, and personalized physics phase/threshold fusion are compared in the same participant-grouped CV. Personalization uses only observations available before each cutoff; this is not independent external validation."
            ) if comparison_mode else (
                ("통합 SQLite DB의 기존 Train split으로 새 모델을 학습했습니다. 동일조건 비교는 같은 split·특징·후보·가중 규칙에서 "
                 "8개 전략을 비교하며, 통합 재학습은 공통 모델을 별도로 적합합니다. Test는 재사용 자료이고 bootstrap 95% CI는 "
                 "고정된 fitted model 조건부이며 학습·선택 불확실성을 포함하지 않습니다. 원래 설치 모델은 교체하지 않습니다.")
                if self.lang != "en" else
                ("New models were fitted on the existing Train split in the unified SQLite database. The controlled comparison evaluates "
                 "eight strategies under the same splits, features, candidates, and weighting; pooled retraining fits separate common models. "
                 "Test is reused, bootstrap 95% CIs are conditional on fixed fitted models and exclude training/selection uncertainty, and the "
                 "original installed models are unchanged.")
            ))
            ttk.Label(parent, text=note, style="AcademicNote.TLabel", wraplength=1050,
                      justify="left").pack(fill="x", pady=5)
        else:
            self.text(parent, "scope_note", "AcademicNote.TLabel")
        item = self.project.state.get("input") or {}
        self.text(parent, "synthetic_badge" if item.get("synthetic") else "real_badge")
        records = self.records()
        summary = " · ".join(f"{self.t(key)} {sum(r['status']==key for r in records)}" for key in ["completed", "running", "queued", "failed", "interrupted"])
        ttk.Label(parent, text=summary, padding=(0, 8)).pack(fill="x")
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "sample", self.load_sample)
        self.button(actions, "suite", self.run_suite)
        if study_mode:
            note = ("CMI·MESA·Macro를 분리해 표시하며 joint MAE에만 해당 marginal 95% CI를 그립니다. 짝지은 차이는 결과 화면에서 별도로 확인합니다."
                    if self.lang != "en" else
                    "CMI, MESA, and Macro are faceted; only joint MAE uses its marginal 95% CI. Paired differences are available separately on the Results page.")
            ttk.Label(parent, text=note, style="AcademicNote.TLabel", wraplength=1050,
                      justify="left").pack(fill="x", pady=5)
        else:
            self.text(parent, "results_note", "AcademicNote.TLabel")
        if record:
            ttk.Label(parent, text=f"{record['name']} · {self.t(record['config']['mode'])}", padding=(0, 8)).pack(fill="x")
            self.plot_host = ttk.Frame(parent); self.plot_host.pack(fill="both", expand=True)
            self.draw_result(record["result"], kind="comparison")
        else: self.text(parent, "no_results")

    def import_data(self, path=None, synthetic=False):
        path = path or filedialog.askopenfilename(parent=self.root, filetypes=[("Data", "*.csv *.xlsx *.xlsm *.json")])
        if path:
            self.project.import_data(path, synthetic=synthetic)
            self.var("input_mode").set("file")
            self.var("input_synthetic").set("1" if synthetic else "0")
            self.refresh_header()
            self.navigate("data")

    def load_sample(self):
        self.import_data(ROOT / "samples/sleepwell_50days.csv", synthetic=True)

    def build_design(self, parent):
        if self.var('workspace').get()=='tabular':
            self.build_workspace_intro(parent)
            return
        self.text(parent, "data_help", "AcademicNote.TLabel")
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "import", self.import_data); self.button(actions, "sample", self.load_sample)
        self.field(parent, "name")
        self.field(parent, "mode", MODES, lambda:self.navigate('design'))
        self.field(parent, "input_mode", ["file", "synthetic"])
        ttk.Checkbutton(parent, text=self.t("input_synthetic"), variable=self.var("input_synthetic"), onvalue="1", offvalue="0").pack(anchor="w", pady=5)
        cohorts = ttk.Frame(parent); cohorts.pack(fill="x")
        ttk.Label(cohorts, text=self.t("cohorts"), width=28).pack(side="left")
        for cohort in ["cmi", "mesa"]:
            ttk.Checkbutton(cohorts, text=cohort.upper(), variable=self.var(cohort), onvalue="1", offvalue="0").pack(side="left", padx=10)
        if self.var('mode').get()=='report':
            self.field(parent, "report_type", [r["id"] for r in experiment_catalog()["report_types"]])
            self.field(parent, "as_of")
            self.field(parent, "cutoff")
            self.field(parent, "cohort", ["cmi", "samsung"])
            self.field(parent, "record_source", ["diary", "healthkit", "health_connect"])
            reports = ttk.Frame(parent); reports.pack(fill="x")
            self.button(reports, "import_report", self.import_report)
            self.button(reports, "clear_report", lambda: self.var("report_json").set("{}"))
            self.text(parent, "report_help", "AcademicNote.TLabel")
        options = ttk.Frame(parent); options.pack(fill="x", pady=5)
        for index, key in enumerate(["n_nights", "seed", "start_date", "jitter_minutes", "phase_shift_minutes", "shift_after_night", "max_seconds", "max_experiments"]):
            group = ttk.Frame(options); group.grid(row=index//3, column=index%3, sticky="ew", padx=(0, 12), pady=3)
            ttk.Label(group, text=self.t(key)).pack(anchor="w")
            ttk.Entry(group, textvariable=self.var(key), width=20).pack(fill="x")
        for column in range(3): options.columnconfigure(column, weight=1)
        actions = ttk.Frame(parent); actions.pack(fill="x", pady=10)
        self.button(actions, "run", self.run_selected); self.button(actions, "suite", self.run_suite)
        pooled = ttk.LabelFrame(
            parent,
            text="CMI·MESA 통합 재학습" if self.lang != "en" else "Pooled CMI/MESA retraining",
            padding=10,
        )
        pooled.pack(fill="x", pady=8)
        ttk.Label(
            pooled,
            text=(
                "통합 SQLite DB에서 새 모델을 학습하고 검증·시험 성능을 별도 결과 폴더에 저장합니다. "
                "위의 고정 모델 재평가와 다른 실행입니다."
                if self.lang != "en" else
                "Train new models from the unified SQLite database and save validation/test results "
                "to a separate output folder. This is distinct from frozen-model replay."
            ),
            style="AcademicNote.TLabel", wraplength=1000, justify="left",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            pooled,
            text="재학습 실행" if self.lang != "en" else "Run retraining",
            command=lambda: self.safe(self.run_pooled_retraining),
        ).pack(side="right", padx=(10, 0))
        controlled = ttk.LabelFrame(
            parent,
            text="CMI·MESA 동일조건 비교" if self.lang != "en" else "Controlled CMI/MESA comparison",
            padding=10,
        )
        controlled.pack(fill="x", pady=8)
        ttk.Label(
            controlled,
            text=(
                "같은 split·특징·후보·가중 규칙으로 개별 학습, 공통 학습, 출처 one-hot 인코딩 통합, "
                "공통 전처리·개별 회귀를 비교합니다. data는 공통 33개 지표, hybrid는 39개 지표이며 "
                "재사용 Test의 조건부 비교이고 원래 고정 모델은 교체하지 않습니다."
                if self.lang != "en" else
                "Compare separate fits, common fits, source one-hot encoded pooling, and shared-preprocessing "
                "separate regression under the same splits, features, candidates, and weighting. Data uses 33 "
                "common descriptors and hybrid uses 39. This is a conditional reused-Test comparison and does "
                "not replace the original frozen models."
            ),
            style="AcademicNote.TLabel", wraplength=1000, justify="left",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            controlled,
            text="동일조건 비교 실행" if self.lang != "en" else "Run controlled comparison",
            command=lambda: self.safe(self.run_controlled_comparison),
        ).pack(side="right", padx=(10, 0))
        revalidation = ttk.LabelFrame(
            parent,
            text=("CMI·MESA 참여자 교차검증" if self.lang != "en" else
                  "CMI/MESA participant-grouped cross-validation"),
            padding=10,
        )
        revalidation.pack(fill="x", pady=8)
        ttk.Label(
            revalidation,
            text=(
                "모델·전처리·평가기준을 실행 전에 고정하고, 같은 참여자의 모든 밤을 한 fold에 묶은 "
                "5-fold×2회 내부 교차검증을 실행합니다. 미사용 MESA holdout이 있으면 별도 표로 평가합니다. "
                "교차검증 자료는 과거 분석에 노출되어 독립 외부 검증은 아닙니다."
                if self.lang != "en" else
                "Lock models, preprocessing, and evaluation before fitting, then run 5-fold×2 repeated "
                "internal CV with every participant's nights kept in one fold. Historical exposure means "
                "this is not independent external validation."
            ),
            style="AcademicNote.TLabel", wraplength=1000, justify="left",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            revalidation,
            text="참여자 교차검증 실행" if self.lang != "en" else "Run participant CV",
            command=lambda: self.safe(self.run_participant_revalidation),
        ).pack(side="right", padx=(10, 0))
        ttk.Label(parent,text=f"{self.pt('budget_preview')}: {self.t(self.var('mode').get())} · {self.var('max_seconds').get()}s/run · max {self.var('max_experiments').get()} · CPU · concurrency=1",wraplength=1000).pack(fill='x')
        self.text(parent, "preview_note", "AcademicNote.TLabel")
        item = self.project.state.get("input")
        if item:
            import pandas as pd
            frame = pd.read_csv(self.project.snapshot_path(item["snapshot"]), nrows=100)
            preview = tree(parent, list(frame.columns), list(frame.columns), height=6)
            for row in frame.itertuples(index=False, name=None): preview.insert("", "end", values=list(row))

    def base_config(self, mode):
        self.capture()
        cohorts = [c for c in ["cmi", "mesa"] if self.var(c).get() == "1"]
        if not cohorts: raise ValueError("CMI / MESA")
        config = {"mode": mode, "name": self.var("name").get().strip() or self.t(mode),
                  "cohorts": cohorts, "max_seconds": float(self.var("max_seconds").get()),
                  "profile": json.loads(self.var("profile_json").get()),
                  "online_personalization": self.var("online_personalization").get() == "1"}
        synth = {"n_nights": int(self.var("n_nights").get()), "seed": int(self.var("seed").get()),
                 "start_date": self.var("start_date").get(), "bed_hour": 23.5, "duration_hours": 8.0,
                 "jitter_minutes": float(self.var("jitter_minutes").get()),
                 "phase_shift_minutes": float(self.var("phase_shift_minutes").get()),
                 "shift_after_night": int(self.var("shift_after_night").get()),
                 "missing_every": 0, "report_delay_hours": 1}
        if mode == "report":
            extra = json.loads(self.var("report_json").get())
            if not isinstance(extra, dict): raise ValueError(self.t("invalid_report"))
            allowed = {"records", "outcomes", "context", "snapshots", "profile", "as_of", "cutoff", "research_context", "personalization_policy", "cohort"}
            if set(extra) - allowed: raise ValueError(self.t("invalid_report"))
            config.update(report_type=self.var("report_type").get(), as_of=self.var("as_of").get(),
                          cohort=self.var("cohort").get(), record_source=self.var("record_source").get(),
                          cutoff=self.var("cutoff").get() if "T" in self.var("cutoff").get() else self.var("cutoff").get()+"T20:00:00+09:00")
            config.update(extra)
            if config["report_type"] == "phone_baseline" and not config.get("outcomes"):
                raise ValueError(self.t("phone_input_required"))
        if mode == "prospective_demo":
            config.update(synth)
            config["input_synthetic"] = True
            if self.var("input_mode").get() == "file":
                item = self.project.state.get("input")
                if not item: raise ValueError(self.t("input_missing"))
                if self.var("input_synthetic").get() != "1":
                    raise ValueError(self.t("demo_synthetic_required"))
                config.update(input_snapshot=item["snapshot"], data_hash=item["sha256"],
                              source_name=item["source_name"])
        elif mode in {"retrospective", "personalization"} and self.var("input_mode").get() == "synthetic":
            config.update(input_mode="synthetic", synthetic=synth, input_synthetic=True)
        elif mode in {"retrospective", "personalization", "prospective_commit", "report"}:
            item = self.project.state.get("input")
            if mode == "report" and ("records" in config or "outcomes" in config or config["report_type"] not in {"measurement_audit", "target_validity", "night_model"}):
                item = None
            if not item and mode != "report": raise ValueError(self.t("input_missing"))
            if item:
                if mode == "prospective_commit" and (item.get("synthetic") or self.var("input_synthetic").get() == "1"):
                    raise ValueError(self.t("actual_requires_real"))
                config.update(input_snapshot=item["snapshot"], data_hash=item["sha256"], source_name=item["source_name"],
                              input_synthetic=self.var("input_synthetic").get() == "1", input_mode="real")
        return config

    def import_report(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Original report request JSON", "*.json")])
        if path:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict): raise ValueError(self.t("invalid_report"))
            self.var("report_json").set(json.dumps(value, ensure_ascii=False))
            self.save()
            self.status.set(self.t("saved_file") + ": " + Path(path).name)

    def run_selected(self):
        self.runner.submit(self.base_config(self.var("mode").get()))
        self.save(); self.navigate("queue")

    def run_pooled_retraining(self):
        database = filedialog.askopenfilename(
            parent=self.root,
            title="CMI·MESA SQLite DB" if self.lang != "en" else "CMI/MESA SQLite database",
            filetypes=[("SQLite", "*.sqlite3 *.sqlite *.db"), ("All files", "*.*")],
        )
        if not database:
            return
        database_path = Path(database).resolve()
        if not database_path.is_file():
            raise FileNotFoundError(database_path)
        output = filedialog.asksaveasfilename(
            parent=self.root,
            title="새 결과 폴더" if self.lang != "en" else "New result folder",
            initialfile="cmi_mesa_retraining_20261104",
            confirmoverwrite=False,
        )
        if not output:
            return
        output_path = Path(output).resolve()
        if output_path.exists():
            raise FileExistsError(
                ("결과 경로가 이미 있습니다: " if self.lang != "en" else "Output path already exists: ")
                + str(output_path)
            )
        digest = hashlib.sha256()
        with database_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        self.runner.submit({
            "mode": "pooled_retraining",
            "name": self.t("pooled_retraining"),
            "database_path": str(database_path),
            "output_dir": str(output_path),
            "seed": 20261104,
            "bootstrap_replicates": 5000,
            "max_seconds": 7200.0,
            "data_hash": digest.hexdigest(),
            "source_name": database_path.name,
            "input_synthetic": False,
        })
        self.save()
        self.navigate("queue")

    def run_controlled_comparison(self):
        database = filedialog.askopenfilename(
            parent=self.root,
            title="CMI·MESA SQLite DB" if self.lang != "en" else "CMI/MESA SQLite database",
            filetypes=[("SQLite", "*.sqlite3 *.sqlite *.db"), ("All files", "*.*")],
        )
        if not database:
            return
        database_path = Path(database).resolve()
        if not database_path.is_file():
            raise FileNotFoundError(database_path)
        output = filedialog.asksaveasfilename(
            parent=self.root,
            title="새 동일조건 비교 결과 폴더" if self.lang != "en" else "New controlled-comparison folder",
            initialfile="cmi_mesa_controlled_comparison_20261104",
            confirmoverwrite=False,
        )
        if not output:
            return
        output_path = Path(output).resolve()
        if output_path.exists():
            raise FileExistsError(
                ("결과 경로가 이미 있습니다: " if self.lang != "en" else "Output path already exists: ")
                + str(output_path)
            )
        digest = hashlib.sha256()
        with database_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        self.runner.submit({
            "mode": "controlled_comparison",
            "name": self.t("controlled_comparison"),
            "database_path": str(database_path),
            "output_dir": str(output_path),
            "seed": 20261104,
            "bootstrap_replicates": 5000,
            "cv_repeats": 2,
            "cv_folds": 3,
            "max_seconds": 7200.0,
            "data_hash": digest.hexdigest(),
            "source_name": database_path.name,
            "input_synthetic": False,
        })
        self.save()
        self.navigate("queue")

    def run_participant_revalidation(self):
        database = filedialog.askopenfilename(
            parent=self.root,
            title="CMI·MESA SQLite DB" if self.lang != "en" else "CMI/MESA SQLite database",
            filetypes=[("SQLite", "*.sqlite3 *.sqlite *.db"), ("All files", "*.*")],
        )
        if not database:
            return
        audit = filedialog.askopenfilename(
            parent=self.root,
            title=("참여자 사용 현황 감사 JSON" if self.lang != "en" else
                   "Participant-use audit JSON"),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not audit:
            return
        untouched = filedialog.askopenfilename(
            parent=self.root,
            title=("선택 사항: 미사용 MESA holdout 파일" if self.lang != "en" else
                   "Optional: untouched MESA holdout file"),
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        database_path, audit_path = Path(database).resolve(), Path(audit).resolve()
        if not database_path.is_file():
            raise FileNotFoundError(database_path)
        if not audit_path.is_file():
            raise FileNotFoundError(audit_path)
        output = filedialog.asksaveasfilename(
            parent=self.root,
            title=("새 참여자 교차검증 결과 폴더" if self.lang != "en" else
                   "New participant-CV result folder"),
            initialfile="cmi_mesa_participant_revalidation_20261106",
            confirmoverwrite=False,
        )
        if not output:
            return
        output_path = Path(output).resolve()
        if output_path.exists():
            raise FileExistsError(
                ("결과 경로가 이미 있습니다: " if self.lang != "en" else
                 "Output path already exists: ") + str(output_path)
            )
        digest = hashlib.sha256()
        with database_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        audit_digest = hashlib.sha256(audit_path.read_bytes()).hexdigest()
        config = {
            "mode": "participant_revalidation",
            "name": self.t("participant_revalidation"),
            "database_path": str(database_path),
            "participant_audit_path": str(audit_path),
            "participant_audit_sha256": audit_digest,
            "output_dir": str(output_path),
            "seed": 20261106,
            "bootstrap_replicates": 5000,
            "cv_repeats": 2,
            "cv_folds": 5,
            "max_seconds": 14400.0,
            "data_hash": digest.hexdigest(),
            "source_name": database_path.name,
            "input_synthetic": False,
        }
        if untouched:
            holdout_path = Path(untouched).resolve()
            if not holdout_path.is_file():
                raise FileNotFoundError(holdout_path)
            config.update(
                untouched_holdout_path=str(holdout_path),
                untouched_holdout_sha256=hashlib.sha256(holdout_path.read_bytes()).hexdigest(),
            )
        from .participant_revalidation import prepare_revalidation
        prepare_revalidation(config)
        self.runner.submit(config)
        self.save()
        self.navigate("queue")

    def run_suite(self):
        if int(self.var("max_experiments").get()) < 3: raise ValueError(self.t("budget_error"))
        configs = [self.base_config(mode) for mode in ["retrospective", "personalization", "prospective_demo"]]
        for config in configs: self.runner.submit(config)
        self.save(); self.navigate("queue")

    def build_queue(self, parent):
        self.queue_tree = tree(parent, ["name", "mode", "status", "stage", "duration", "id"],
                               [self.t(k) for k in ["name", "mode", "status", "stage", "duration"]] + ["ID"], height=10)
        self.queue_tree.bind("<<TreeviewSelect>>", lambda event: self.queue_details())
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "cancel", self.cancel_selected); self.button(actions, "retry", self.retry_selected)
        self.queue_log = tk.Text(parent, height=10, wrap="word", font=("Malgun Gothic", 10))
        self.queue_log.pack(fill="both", expand=True)
        self.refresh_queue()

    def refresh_queue(self):
        if self.page != "queue": return
        selected = self.queue_tree.selection()
        self.queue_tree.delete(*self.queue_tree.get_children())
        for record in self.records():
            self.queue_tree.insert("", "end", iid=record["id"], values=(record["name"], self.t(record["config"]["mode"]),
                self.t(record["status"]), self.t(record.get("stage", "")), f"{record['duration']:.1f}", record["id"]))
        self.queue_tree.selection_set([i for i in selected if self.queue_tree.exists(i)])

    def queue_details(self):
        selected = set(self.queue_tree.selection())
        records = [r for r in self.records() if r["id"] in selected]
        self.queue_log.configure(state="normal"); self.queue_log.delete("1.0", "end")
        for record in records:
            payload = {k: record.get(k) for k in ["id", "config", "status", "progress", "error", "traceback"]}
            self.queue_log.insert("end", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        self.queue_log.configure(state="disabled")

    def cancel_selected(self):
        for run_id in self.queue_tree.selection(): self.runner.cancel(run_id)
        self.refresh_queue()

    def retry_selected(self):
        selected = set(self.queue_tree.selection())
        for record in self.records():
            if record["id"] in selected: self.runner.submit(copy.deepcopy(record["config"]))
        self.refresh_queue()

    def build_results(self, parent):
        tabs=ttk.Notebook(parent); tabs.pack(fill='both',expand=True)
        comparison=ttk.Frame(tabs,padding=6); tabs.add(comparison,text=self.pt('compare_runs'))
        if self.var('workspace').get()=='sleepwell':
            single=ttk.Frame(tabs,padding=6); tabs.add(single,text=self.pt('single_result'))
            self.build_single_results(single)
        self.build_comparison(comparison)
        if hasattr(self,'build_advanced_evaluation'):
            advanced=ttk.Frame(tabs,padding=6); tabs.add(advanced,text=self.pt('external_evaluation'))
            self.build_advanced_evaluation(advanced)
        saved_tab=self.var('results_tab').get()
        if saved_tab.isdigit() and int(saved_tab)<len(tabs.tabs()): tabs.select(int(saved_tab))
        tabs.bind('<<NotebookTabChanged>>',lambda e:self.var('results_tab').set(str(tabs.index('current'))))

    def build_single_results(self, parent):
        completed = [r for r in self.records() if r["status"] == "completed"]
        current = self.current_record()
        if (current or {}).get("result", {}).get("mode") in {
            "pooled_retraining", "controlled_comparison", "cross_cohort_evaluation",
            "participant_revalidation", "personalization_comparison",
        }:
            revalidation = (current or {}).get("result", {}).get("mode") in {
                "participant_revalidation", "personalization_comparison",
            }
            note = (("출처별 joint MAE와 참여자 군집 bootstrap 95% CI를 표시합니다. "
                     "고정 절차의 out-of-fold 내부 검증이며, 과거 절차 선택 불확실성과 독립 외부 검증을 포함하지 않습니다.")
                    if revalidation and self.lang != "en" else
                    ("Results show cohort-specific joint MAE with participant-cluster bootstrap 95% CIs. "
                     "This is fixed-procedure out-of-fold internal validation; it excludes prior procedure-selection "
                     "uncertainty and is not independent external validation.")
                    if revalidation else
                    ("출처별 joint MAE의 참여자 군집 bootstrap 95% CI와 짝지은 차이를 표시합니다. "
                     "재사용 Test의 고정 fitted model 조건부 구간으로 학습·선택 불확실성을 포함하지 않고 원래 모델을 교체하지 않습니다.")
                    if self.lang != "en" else
                    "Results are separated by cohort and show participant-cluster bootstrap 95% CIs for joint MAE plus paired differences. "
                    "These reused-Test intervals are conditional on fixed fitted models, exclude training/selection uncertainty, and do not replace original models.")
            ttk.Label(parent, text=note, style="AcademicNote.TLabel", wraplength=1050,
                      justify="left").pack(fill="x", pady=5)
        else:
            self.text(parent, "results_note", "AcademicNote.TLabel")
        self.result_choices = completed
        row = ttk.Frame(parent); row.pack(fill="x", pady=5)
        ttk.Label(row, text=self.t("run_select"), width=19).pack(side="left")
        self.run_combo = ttk.Combobox(row, values=[f"{r['name']} · {r['id'][:8]}" for r in completed], state="readonly", width=76)
        self.run_combo.pack(side="left", fill="x", expand=True)
        if current: self.run_combo.current(next(i for i,r in enumerate(completed) if r["id"] == current["id"]))
        self.run_combo.bind("<<ComboboxSelected>>", lambda event: self.safe(self.select_result))
        row = ttk.Frame(parent); row.pack(fill="x", pady=5)
        self.combo(row, "metric", ["joint_mae_min", "onset_mae_min", "wake_mae_min"], self.refresh_result, width=30).pack(side="left", padx=(0, 8))
        self.kind_combo = ttk.Combobox(row, values=[self.t(k) for k in ["comparison", "paired_difference", "predictions", "personalization_plot", "errors", "source_report", "correlation"]], state="readonly", width=28)
        kind_ids = ["comparison", "paired_difference", "predictions", "personalization", "errors", "source_report", "correlation"]
        self.kind_combo.current(kind_ids.index(self.var("kind").get()) if self.var("kind").get() in kind_ids else 0)
        def kind_selected(event):
            self.var("kind").set(kind_ids[self.kind_combo.current()]); self.safe(self.refresh_result)
        self.kind_combo.bind("<<ComboboxSelected>>", kind_selected); self.kind_combo.pack(side="left")
        self.button(row, "export_png", self.export_plot); self.button(row, "export_table", self.export_table)
        self.plot_options_row = ttk.Frame(parent); self.plot_options_row.pack(fill="x", pady=3)
        self.personalization_controls = ttk.Frame(self.plot_options_row)
        ttk.Label(self.personalization_controls, text=self.t("personalization_field")).pack(side="left", padx=(0, 8))
        self.combo(self.personalization_controls, "personalization_field", ["personal_weight", "sleep_threshold", "wake_threshold", "phase_min", "habit_bed_offset", "habit_wake_offset"], self.refresh_result, width=28).pack(side="left")
        self.correlation_controls = ttk.Frame(self.plot_options_row)
        ttk.Label(self.correlation_controls, text=self.t("corr_method")).pack(side="left", padx=(0, 8))
        self.combo(self.correlation_controls, "corr_method", ["pearson", "spearman"], self.refresh_result, width=28).pack(side="left")
        ttk.Checkbutton(self.plot_options_row, text=self.t("show_table"), variable=self.var("show_table"), onvalue="1", offvalue="0", command=self.toggle_result_table).pack(side="right")
        self.result_scope = ttk.Label(parent, wraplength=1080, justify="left"); self.result_scope.pack(fill="x", pady=5)
        self.result_tree = tree(parent, ["model", "onset", "wake", "joint", "joint_ci", "n", "people", "source"],
            [self.t(k) for k in ["model", "onset_mae_min", "wake_mae_min", "joint_mae_min", "joint_ci95", "n_nights", "n_participants", "source"]], height=9)
        self.result_tree.column("model", width=280, minwidth=210)
        for column in ["onset", "wake", "joint"]: self.result_tree.column(column, width=112)
        self.result_tree.column("joint_ci", width=155)
        self.result_tree.column("n", width=75); self.result_tree.column("people", width=80)
        self.plot_host = ttk.Frame(parent); self.plot_host.pack(fill="both", expand=True)
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "details", self.show_details); self.button(actions, "export_json", self.export_json)
        self.refresh_result()

    def toggle_result_table(self):
        holder = self.result_tree.master
        if self.var("show_table").get() == "1":
            holder.pack(fill="both", expand=True, pady=5, before=self.plot_host)
        else:
            holder.pack_forget()

    def select_result(self):
        index = self.run_combo.current()
        if index >= 0:
            record = self.result_choices[index]
            self.selected_run = record["id"]
            if record["config"]["mode"] == "report" and not record["result"].get("summary"):
                self.var("kind").set("source_report")
                self.kind_combo.current(5)
        self.refresh_result()

    def refresh_result(self):
        kind = self.var("kind").get()
        self.personalization_controls.pack_forget()
        self.correlation_controls.pack_forget()
        if kind == "personalization": self.personalization_controls.pack(side="left")
        if kind == "correlation": self.correlation_controls.pack(side="left")
        self.toggle_result_table()
        record = self.current_record()
        self.result_tree.delete(*self.result_tree.get_children())
        if not record:
            self.result_scope.configure(text=self.t("no_results")); return
        result = record["result"]
        synthetic = record["config"].get("input_synthetic") or result.get("mode") == "prospective_demo"
        text = f"{self.t(record['config']['mode'])}  ·  {self.t('synthetic_badge') if synthetic else self.t('real_badge')}"
        if record["config"]["mode"] == "report":
            text = self.t(record["config"].get("report_type","report")) + " · " + self.t("report_help")
        warnings = result.get("warnings") or []
        if warnings: text += "\n" + " · ".join(self.warning_text(w) for w in warnings[:3])
        if result.get("mode") in {
            "pooled_retraining", "controlled_comparison", "cross_cohort_evaluation",
            "participant_revalidation", "personalization_comparison",
        }:
            if result.get("mode") in {"participant_revalidation", "personalization_comparison"}:
                text += ("\n참여자 단위 out-of-fold 내부 검증 · 실행 전 절차 고정 · 독립 외부 검증 아님"
                         if self.lang != "en" else
                         "\nParticipant-grouped out-of-fold internal validation · procedure locked before fitting · not independent external validation")
            else:
                text += ("\n재사용 Test · CI는 고정된 fitted model 조건부이며 학습·선택 불확실성 제외 · 원래 고정 모델 미교체"
                         if self.lang != "en" else
                         "\nReused Test · CIs are conditional on fixed fitted models and exclude training/selection uncertainty · original frozen models unchanged")
        self.result_scope.configure(text=text)
        metric = self.var("metric").get()
        rows = sorted(result.get("summary") or [], key=lambda r: r.get(metric) if isinstance(r.get(metric),(int,float)) else float("inf"))
        if any(str(row.get("model_id", "")).startswith("online_") for row in rows):
            self.result_scope.configure(text=text + "\n" + self.t("online_metric_note"))
        number = lambda value: "—" if value is None else f"{value:.3f}" if isinstance(value, (int,float)) else str(value)
        for item in rows:
            joint_ci = (f"[{number(item.get('ci95_low'))}, {number(item.get('ci95_high'))}]"
                        if item.get("ci95_low") is not None and item.get("ci95_high") is not None else "—")
            self.result_tree.insert("", "end", values=(model_label(item.get("model_id", ""), self.lang),
                *[number(item.get(k)) for k in ["onset_mae_min", "wake_mae_min", "joint_mae_min"]],
                joint_ci, item.get("n_nights", "—"), item.get("n_participants", "—"), self.t(item.get("source_stratum", item.get("source", item.get("comparison_scope", ""))))))
        self.draw_result(result)

    def options(self):
        return {"title": self.var("title").get() or None, "annotation": self.var("annotation").get() or None,
                "font_size": float(self.var("font_size").get()), "width": float(self.var("width").get()),
                "height": float(self.var("height").get()), "dpi": 100,
                "personalization_field": self.var("personalization_field").get(),
                "corr_method": self.var("corr_method").get()}

    def draw_result(self, result, kind=None):
        figure = make_academic_figure(result, kind=kind or self.var("kind").get(), metric=self.var("metric").get(), lang=self.lang, options=self.options())
        if self.plot_canvas: self.plot_canvas.get_tk_widget().destroy()
        if self.figure: self.figure.clear()
        self.figure = figure
        self.plot_canvas = ResearchCanvas(figure, master=self.plot_host)
        self.plot_canvas.draw(); self.plot_canvas.get_tk_widget().pack(fill="both", expand=True, pady=8)

    def show_details(self):
        record = self.current_record()
        if not record: raise ValueError(self.t("no_selection"))
        window = tk.Toplevel(self.root); window.title(self.t("details")); window.geometry("1000x700")
        text = tk.Text(window, wrap="word", font=("Malgun Gothic", 10)); text.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(text, command=text.yview); scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", json.dumps(record, ensure_ascii=False, indent=2)); text.configure(state="disabled")

    def export_plot(self):
        if self.figure is None: raise ValueError(self.t("no_selection"))
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".png", filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
        if path: export_figure(self.figure, path, dpi=int(self.var("dpi").get())); self.status.set(self.t("saved_file") + ": " + path)

    def export_table(self):
        record = self.current_record()
        if not record: raise ValueError(self.t("no_selection"))
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if path: export_academic_tables(record["result"], path, lang=self.lang); self.status.set(self.t("saved_file") + ": " + path)

    def export_json(self):
        record = self.current_record()
        if not record: raise ValueError(self.t("no_selection"))
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path: write_json(path, record)

    def build_prospective(self, parent):
        tabs=ttk.Notebook(parent); tabs.pack(fill='both',expand=True)
        study=ttk.Frame(tabs,padding=6); tabs.add(study,text=self._st('웨어러블 없는 일지 연구', 'Diary study'))
        self.build_diary_study(study)
        multi=ttk.Frame(tabs,padding=6); tabs.add(multi,text=self.pt('participant_wrapper'))
        self.build_participant_evaluation(multi)
        original=ttk.Frame(tabs,padding=6); tabs.add(original,text=self.t('prospective'))
        self.build_original_prospective(original)

    def build_original_prospective(self, parent):
        self.text(parent, "prospective_help", "AcademicNote.TLabel")
        if (self.project.state.get("input") or {}).get("synthetic"):
            self.text(parent, "actual_requires_real", "AcademicNote.TLabel")
        self.field(parent, "cutoff")
        ttk.Checkbutton(parent, text=self.t("online_personalization"), variable=self.var("online_personalization"), onvalue="1", offvalue="0").pack(anchor="w", pady=5)
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "prospective_commit", lambda: self.prospective_run("prospective_commit"))
        self.button(actions, "prospective_summary", lambda: self.prospective_run("prospective_summary"))
        self.button(actions, "refresh", lambda: self.navigate("prospective"))
        self.forecast_tree = tree(parent, ["id", "cutoff", "created", "eligible", "outcome"],
            [self.t(k) for k in ["forecast_id", "cutoff", "generated_at", "eligible_capture", "has_outcome"]], height=5)
        for forecast in list_forecasts(self.project.path / "prospective.sqlite3"):
            self.forecast_tree.insert("", "end", iid=str(forecast["forecast_id"]), values=(forecast["forecast_id"], forecast["night_id"], forecast["generated_at"], str(forecast["eligible_capture"]), str(forecast["has_outcome"])))
        self.forecast_tree.bind("<<TreeviewSelect>>", lambda event: self.var("forecast_id").set(self.forecast_tree.selection()[0]) if self.forecast_tree.selection() else None)
        for key in ["forecast_id", "sleep_start", "wake", "observed_at"]: self.field(parent, key)
        self.field(parent, "outcome_source", ["diary", "healthkit", "health_connect"])
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "prospective_outcome", lambda: self.prospective_run("prospective_outcome"))
        self.text(parent, "profile", "AcademicNote.TLabel")
        self.profile_editor = tk.Text(parent, height=12, wrap="word", font=("Malgun Gothic", 10))
        self.profile_editor.pack(fill="both", expand=True)
        self.profile_editor.insert("1.0", self.var("profile_json").get())

    def prospective_run(self, mode):
        config = self.base_config(mode)
        if mode == "prospective_commit": config["cutoff"] = self.var("cutoff").get()
        if mode == "prospective_outcome":
            config.update(forecast_id=self.var("forecast_id").get(), outcome={
                "sleep_start": self.var("sleep_start").get(), "wake": self.var("wake").get(),
                "observed_at": self.var("observed_at").get(), "source": self.var("outcome_source").get()})
        self.runner.submit(config); self.save(); self.navigate("queue")

    def build_legacy_evidence(self, parent):
        self.text(parent, "evidence_note", "AcademicNote.TLabel")
        text = tk.Text(parent, height=35, wrap="word", font=("Malgun Gothic", 10)); text.pack(fill="both", expand=True)
        payload = {"catalog": experiment_catalog(), "model_status": model_status()}
        text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2)); text.configure(state="disabled")

    def build_settings(self, parent):
        tabs=ttk.Notebook(parent); tabs.pack(fill='both',expand=True)
        plot=ttk.Frame(tabs,padding=6); tabs.add(plot,text=self.t('settings'))
        ai=ttk.Frame(tabs,padding=6); tabs.add(ai,text=self._ait('title'))
        self.build_optional_ai(ai)
        self.build_plot_settings(plot)

    def build_plot_settings(self, parent):
        for key in ["title", "annotation", "font_size", "width", "height", "dpi"]:
            row = ttk.Frame(parent); row.pack(fill="x", pady=4)
            ttk.Label(row, text=self.t("title_label" if key == "title" else key), width=32).pack(side="left")
            ttk.Entry(row, textvariable=self.var(key)).pack(fill="x", expand=True, side="left")
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "reset_labels", self.reset_labels); self.button(actions, "save", self.save)
        self.text(parent, "scope_note", "AcademicNote.TLabel")
        ttk.Label(parent, text=str(self.project.path), wraplength=1000).pack(fill="x", pady=10)

    def reset_labels(self):
        self.var("title").set(""); self.var("annotation").set(""); self.save()

    def general_window(self):
        from .app import App
        window = tk.Toplevel(self.root)
        app=App(window, language=self.lang, preferences=False)
        linked=self.var('linked_general').get()
        if linked:
            from .storage import Project
            app.attach_project(Project.open(linked))
        window.title(self.t('title')+' · '+self.pt('tabular'))

    def on_record(self, record):
        self.simple_last_event = record
        if record["status"] == "completed": self.selected_run = record["id"]
        if self.page == "queue": self.refresh_queue()
        if self.page == 'experiments':
            if record['status'] == 'completed': self.navigate('experiments')
            else: self.refresh_simple_progress()

    def tick(self):
        if self.closing: return
        try:
            self.runner.poll()
            if self.page == 'experiments': self.refresh_simple_progress()
            active = self.runner.active
            if active:
                r = active["record"]
                self.status.set(f"{self.t(r['status'])} · {self.t(r.get('stage','running'))} · {r['duration']:.1f}s · CPU")
            elif not (getattr(self,'_knowledge_worker_active',False) or getattr(self,'_evaluation_busy',False) or getattr(self,'_optional_ai_worker',False) or getattr(self,'_guideline_busy',False)):
                self.status.set(f"{self.t('saved')} · {self.t('idle')} · CPU · Ctrl+S / Ctrl+O / Ctrl+Enter")
            if time.monotonic()-self.last_saved > 15: self.save()
        except Exception as error: self.status.set(f"{self.t('error')}: {error}")
        self.tick_id = self.root.after(100, self.tick)

    def close(self):
        if getattr(self,'_knowledge_worker_active',False) or getattr(self,'_evaluation_busy',False) or getattr(self,'_optional_ai_worker',False) or getattr(self,'_guideline_busy',False):
            if getattr(self,'_optional_ai_worker',False): self.cancel_optional_ai()
            if getattr(self,'_guideline_busy',False): self.cancel_guideline_work()
            self.status.set(self.t('busy'))
            if not getattr(self,'_close_pending',False):
                self._close_pending=True
                def wait_for_workers():
                    self._close_pending=False
                    self.close()
                self.root.after(200,wait_for_workers)
            return
        self.closing = True
        self.stop_diary_server()
        if getattr(self, "tick_id", None): self.root.after_cancel(self.tick_id)
        self.save()
        if self.runner: self.runner.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="SleepWell native academic workspace")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--language", choices=["ko", "en"])
    parser.add_argument("--no-preferences", action="store_true")
    parser.add_argument("--general", action="store_true")
    args = parser.parse_args()
    if args.general:
        import sys
        sys.argv.remove("--general")
        from .app import main as general_main
        return general_main()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError): pass
    root = tk.Tk()
    if not args.language and not args.no_preferences and read_language() not in {"ko", "en"}:
        dialog = tk.Toplevel(root); dialog.title("Language / 언어"); dialog.geometry("440x170")
        ttk.Label(dialog, text="표시 언어를 선택하세요 / Choose display language", padding=20).pack()
        def choose(language):
            args.language = language; save_language(language); dialog.destroy()
        ttk.Button(dialog, text="한국어", command=lambda: choose("ko")).pack(side="left", padx=40)
        ttk.Button(dialog, text="English", command=lambda: choose("en")).pack(side="right", padx=40)
        dialog.grab_set(); root.wait_window(dialog)
    project = AcademicProject.open(args.project) if args.project else None
    AcademicApp(root, project, args.language, preferences=not args.no_preferences)
    root.mainloop()
