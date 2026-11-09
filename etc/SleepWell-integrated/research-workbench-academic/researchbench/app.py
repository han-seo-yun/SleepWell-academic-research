"""Native desktop application. No server, browser, or external API is involved."""
from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from .data import data_profile, fingerprint, load_data
from .engine import make_split_plan, resolve_config
from .i18n import read_language, save_language, tr
from .plotting import make_figure, comparison_table, export_figure, export_tables
from .queueing import ExperimentQueue
from .recommendations import suggest, target_review
from .storage import Project
from .views import ResultViews
from .canvas import ResearchCanvas

CLASS_METRICS = ["roc_auc", "average_precision", "accuracy", "balanced_accuracy", "recall", "specificity", "precision", "f1"]
REG_METRICS = ["rmse", "mae", "mse", "r2"]
PAGES = ["overview", "explore", "design", "queue", "compare", "export", "settings"]


class ScrollFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f5f7fa")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.body = ttk.Frame(self.canvas, padding=16)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=max(1100, e.width)))
        self.canvas.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self.wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def wheel(self, event):
        if not isinstance(event.widget, (ttk.Treeview, tk.Text, ttk.Combobox)):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")


def tree(parent, columns, headings, height=9, selectmode="extended"):
    holder = ttk.Frame(parent)
    widget = ttk.Treeview(holder, columns=columns, show="headings", height=height, selectmode=selectmode)
    for column, heading in zip(columns, headings):
        widget.heading(column, text=heading)
        widget.column(column, width=160, minwidth=75, stretch=True)
    sy = ttk.Scrollbar(holder, orient="vertical", command=widget.yview)
    sx = ttk.Scrollbar(holder, orient="horizontal", command=widget.xview)
    widget.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    widget.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    sx.grid(row=1, column=0, sticky="ew")
    holder.columnconfigure(0, weight=1)
    holder.rowconfigure(0, weight=1)
    holder.pack(fill="both", expand=True, pady=5)
    return widget


class App(ResultViews):
    def __init__(self, root, language=None, preferences=True):
        self.root = root
        self.use_preferences = preferences
        self.lang = language or read_language()
        self.project = None
        self.runner = None
        self.current_page = "overview"
        self.vars = {}
        self.selected_features = set()
        self.selected_results = set()
        self.plot_canvases = {}
        self.plot_figures = {}
        self.page_index = 0
        self.io = queue.Queue()
        self.io_busy = False
        self.last_refresh = 0.0
        self.closing = False
        self.last_auto_save = time.monotonic()
        self.root.title("Research Workbench")
        self.root.geometry("1440x940")
        self.root.minsize(850, 600)
        self.root.option_add("*Font", ("Malgun Gothic", 10))
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure(".", font=("Malgun Gothic", 10), background="#f5f7fa", foreground="#182d43")
        self.style.configure("TButton", padding=(12, 7))
        self.style.configure("Title.TLabel", font=("Malgun Gothic", 20, "bold"), foreground="#123955")
        self.style.configure("Heading.TLabel", font=("Malgun Gothic", 13, "bold"))
        self.style.configure("Muted.TLabel", foreground="#536778")
        self.style.configure("Treeview", rowheight=28, fieldbackground="white", background="white")
        self.style.map("TButton", foreground=[("focus", "#004b80")], background=[("focus", "#dcecf6")])
        if self.lang not in {"ko", "en"}:
            self.lang = "ko"
            self.choose_first_language()
        self.defaults()
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-n>", lambda e: self.import_project())
        self.root.bind("<Control-o>", lambda e: self.open_project())
        self.root.bind("<Control-s>", lambda e: self.save())
        self.root.bind("<Control-Return>", lambda e: self.run())
        self.root.bind("<F1>", lambda e: self.navigate("settings"))
        self.root.after(100, self.tick)

    def t(self, key, **kwargs):
        return tr(key, self.lang, **kwargs)

    def var(self, key, default=""):
        if key not in self.vars:
            self.vars[key] = tk.StringVar(self.root, value=str(default))
        return self.vars[key]

    def defaults(self):
        values = dict(task="classification", target="", name=self.t("baseline_name"), model="linear",
                      imputer="median", encoding="onehot", scaling="standard", positive_class="",
                      threshold="0.5", average="macro", class_weight="none", target_unit="",
                      target_transform="none", params="{}", primary_metric="roc_auc", max_seconds="300",
                      max_experiments="5", search="", type_filter="all", result_filter="", phase="validation",
                      metric="roc_auc", eda_kind="correlation", result_kind="comparison", feature="",
                      corr_method="pearson", title="", xlabel="", ylabel="", annotation="", hide_title="0",
                      font_size="11", width="8", height="5.4", dpi="300", legend="best", color="#0072B2",
                      bin_edges="", bin_rationale="", display_names="{}")
        for key, value in values.items():
            self.var(key, value)

    def choose_first_language(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Language / 언어")
        dialog.geometry("440x190")
        dialog.transient(self.root)
        ttk.Label(dialog, text=self.t("first_language"), padding=20).pack()
        def choose(lang):
            self.lang = lang
            if self.use_preferences:
                save_language(lang)
            dialog.destroy()
        ttk.Button(dialog, text="한국어", command=lambda: choose("ko")).pack(side="left", padx=35, pady=20)
        ttk.Button(dialog, text="English", command=lambda: choose("en")).pack(side="right", padx=35, pady=20)
        dialog.grab_set()
        self.root.wait_window(dialog)

    def button(self, parent, key, command, **kwargs):
        b = ttk.Button(parent, text=self.t(key), command=lambda: self.safe(command), **kwargs)
        b.pack(side="left", padx=(0, 7), pady=3)
        return b

    def label(self, parent, key, style="TLabel", **kwargs):
        w = ttk.Label(parent, text=self.t(key, **kwargs), style=style, wraplength=1000, justify="left")
        w.pack(anchor="w", fill="x", pady=5)
        return w

    def combo(self, parent, variable, choices, callback=None, width=26, translated=True):
        labels = [self.t(v) if translated else str(v) for v in choices]
        widget = ttk.Combobox(parent, values=labels, state="readonly", width=width)
        value = variable.get()
        widget.set(labels[choices.index(value)] if value in choices else value)
        def changed(event):
            idx = widget.current()
            if idx >= 0:
                variable.set(choices[idx])
            if callback:
                self.safe(callback)
        widget.bind("<<ComboboxSelected>>", changed)
        return widget

    def field(self, parent, key, choices=None, callback=None, translated=True, variable=None, help_key=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=self.t(key), width=30, anchor="w").pack(side="left", padx=(0, 8))
        var = variable or self.var(key)
        if choices is None:
            w = ttk.Entry(row, textvariable=var)
        else:
            w = self.combo(row, var, choices, callback, translated=translated)
        w.pack(side="left", fill="x", expand=True)
        if help_key:
            self.label(parent, help_key, "Muted.TLabel")
        return w

    def build(self):
        if hasattr(self, "shell"):
            self.capture_selection()
            self.shell.destroy()
        self.plot_canvases = {}
        self.plot_figures = {}
        self.shell = ttk.Frame(self.root)
        self.shell.pack(fill="both", expand=True)
        top = ttk.Frame(self.shell, padding=(18, 10))
        top.pack(fill="x")
        ttk.Label(top, text=self.t("app"), style="Title.TLabel").pack(side="left", padx=(0, 20))
        self.header = ttk.Label(top, text="", wraplength=650)
        self.header.pack(side="left", fill="x", expand=True)
        self.header.bind("<Configure>", lambda event: self.header.configure(wraplength=max(180, event.width)))
        top_controls = ttk.Frame(self.shell, padding=(18, 0, 18, 8))
        top_controls.pack(fill="x")
        for key, command in [("new", self.import_project), ("open", self.open_project), ("save", self.save)]:
            self.button(top_controls, key, command)
        language = ttk.Combobox(top_controls, values=["한국어", "English"], width=10, state="readonly")
        language.set("한국어" if self.lang == "ko" else "English")
        language.bind("<<ComboboxSelected>>", lambda e: self.change_language("ko" if language.current() == 0 else "en"))
        language.pack(side="right", padx=8)
        body = ttk.Panedwindow(self.shell, orient="horizontal")
        body.pack(fill="both", expand=True)
        nav = ttk.Frame(body, padding=(12, 20), width=220)
        body.add(nav, weight=0)
        self.label(nav, "tagline", "Muted.TLabel")
        for index, page in enumerate(PAGES):
            ttk.Button(nav, text=f"{index + 1:02d}  {self.t(page)}", command=lambda p=page: self.navigate(p)).pack(fill="x", pady=5)
        self.content = ttk.Frame(body)
        body.add(self.content, weight=1)
        self.pages = {}
        for page in PAGES:
            frame = ScrollFrame(self.content)
            self.pages[page] = frame
            self.label(frame.body, page, "Title.TLabel")
            if self.project or page == "settings":
                getattr(self, "build_" + page)(frame.body)
            else:
                self.label(frame.body, "empty")
                self.button(frame.body, "new", self.import_project)
        self.footer = ttk.Label(self.shell, text="", padding=(18, 7), style="Muted.TLabel")
        self.footer.pack(fill="x", side="bottom")
        self.navigate(self.current_page)
        self.refresh()

    def navigate(self, page):
        self.current_page = page
        for name, frame in self.pages.items():
            if name == page:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        if self.project:
            if page == "overview": self.refresh_overview()
            if page == "compare": self.refresh_results()

    def change_language(self, language):
        self.save_draft()
        self.lang = language
        if self.use_preferences: save_language(language)
        self.build()

    def safe(self, function):
        try:
            return function()
        except Exception as exc:
            self.show_error(exc)

    def show_error(self, exc):
        messagebox.showerror(self.t("error"), self.t("error_summary") + "\n\n" + str(exc), parent=self.root)

    def attach_project(self, project):
        if self.project: self.project.close()
        self.project = project
        self.runner = ExperimentQueue(project, self.on_record)
        self.selected_results = set()
        tasks = list(project.state.get("tasks", {}))
        self.var("task").set(project.state.get("ui", {}).get("task", tasks[0] if tasks else "classification"))
        self.load_task()
        self.page_index = 0
        self.current_page = "overview"
        self.build()

    def ensure_switch(self):
        if self.runner and self.runner.busy:
            raise ValueError(self.t("busy_project"))
        self.save()

    def import_project(self):
        self.ensure_switch()
        path = filedialog.askopenfilename(parent=self.root, title=self.t("data_file"), filetypes=[("CSV / Excel", "*.csv *.xlsx *.xlsm")])
        if not path: return
        self.io_busy = True
        def read():
            try: self.io.put(("loaded", (load_data(path), path)))
            except Exception as exc: self.io.put(("error", exc))
        threading.Thread(target=read, daemon=True).start()

    def open_project(self):
        self.ensure_switch()
        path = filedialog.askdirectory(parent=self.root, title=self.t("open"))
        if path: self.attach_project(Project.open(path))

    def save_draft(self):
        if not self.project: return
        draft = {key: value.get() for key, value in self.vars.items()}
        draft["selected_features"] = sorted(self.selected_features)
        self.project.state.setdefault("ui", {}).setdefault("drafts", {})[self.var("task").get()] = draft
        self.project.state["ui"]["task"] = self.var("task").get()
        self.project.state["ui"]["page"] = self.current_page
        self.project.state["graph_settings"] = {k: self.var(k).get() for k in ["title", "xlabel", "ylabel", "annotation", "hide_title", "font_size", "width", "height", "dpi", "legend", "color"]}

    def save(self):
        if self.project:
            self.save_draft()
            self.project.save()
            self.last_auto_save = time.monotonic()

    def load_task(self):
        task = self.var("task").get()
        config = self.project.state["tasks"][task]
        for key, value in config.items():
            if key in self.vars:
                if key == "params": value = json.dumps(value)
                if key == "bin_edges": value = ", ".join(str(v) for v in value) if value else ""
                if value is None: value = "none" if key == "class_weight" else ""
                self.var(key).set(value)
        self.var("metric").set(config.get("primary_metric", "roc_auc" if task == "classification" else "rmse"))
        self.selected_features = set(config["features"])
        draft = self.project.state.get("ui", {}).get("drafts", {}).get(task, {})
        for key, value in draft.items():
            if key in self.vars: self.var(key).set(value)
        if "selected_features" in draft: self.selected_features = set(draft["selected_features"])
        for key, value in self.project.state.get("graph_settings", {}).items():
            if key in self.vars: self.var(key).set(value)

    def switch_task(self):
        # Previous draft was saved by ordinary edits/autosave; explicit save occurs before user switching below.
        self.load_task()
        self.selected_results = set()
        self.build()

    def task_selector(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=self.t("task") + "  ").pack(side="left")
        old_task = self.var("task").get()
        def selected():
            new_task = self.var("task").get()
            self.var("task").set(old_task)
            self.save_draft()
            self.var("task").set(new_task)
            self.switch_task()
        self.combo(row, self.var("task"), list(self.project.state["tasks"]), selected).pack(side="left")
        self.label(parent, "dual_notice", "Muted.TLabel")

    def training_df(self):
        plan = self.plan()
        return self.project.df.iloc[plan["train_rows"]]

    def plan(self):
        return self.project.state["splits"][self.var("task").get()]

    def all_results(self):
        return self.project.experiments() if self.project else []

    def task_results(self, phase=None):
        return [r for r in self.all_results() if r.get("status") == "completed" and r["task"] == self.var("task").get() and (phase is None or r.get("phase") == phase)]

    def capture_selection(self):
        if hasattr(self, "result_tree") and self.result_tree.winfo_exists():
            self.selected_results = set(self.result_tree.selection())

    def chosen_results(self):
        self.capture_selection()
        results = self.task_results(self.var("phase").get())
        needle = self.var("result_filter").get().strip().lower()
        results = [r for r in results if needle in r.get("name", "").lower()]
        return [r for r in results if not self.selected_results or r["id"] in self.selected_results]

    def baseline_id(self):
        return self.project.state.get("baselines", {}).get(self.var("task").get())

    def config(self):
        config = copy.deepcopy(self.project.state["tasks"][self.var("task").get()])
        for key in ["name", "model", "imputer", "encoding", "scaling", "average", "target_unit", "target_transform", "primary_metric", "bin_rationale"]:
            config[key] = self.var(key).get()
        config["features"] = [c for c in self.project.df.columns if c in self.selected_features]
        if not config["features"]: raise ValueError(self.t("need_feature"))
        config["params"] = json.loads(self.var("params").get())
        if not isinstance(config["params"], dict): raise ValueError(self.t("params"))
        config["class_weight"] = None if self.var("class_weight").get() == "none" else self.var("class_weight").get()
        config["threshold"] = float(self.var("threshold").get())
        config["max_seconds"] = float(self.var("max_seconds").get())
        if config["max_seconds"] <= 0 or int(self.var("max_experiments").get()) <= 0: raise ValueError(self.t("budget_positive"))
        if not 0 <= config["threshold"] <= 1: raise ValueError(self.t("threshold"))
        config["data_hash"] = self.project.state["data_hash"]
        config["device"] = "cpu"
        cuts = self.var("bin_edges").get().strip()
        config["bin_edges"] = [float(v.strip()) for v in cuts.split(",")] if cuts else None
        if config["bin_edges"] and not config["bin_rationale"].strip(): raise ValueError(self.t("need_rationale"))
        if config["task"] == "classification":
            labels = self.project.df[config["target"]].dropna().unique().tolist()
            chosen = self.var("positive_class").get()
            matching = [v for v in labels if str(v) == chosen]
            config["positive_class"] = matching[0] if matching else chosen or None
        return config

    def run(self, configs=None):
        if not self.project: return
        task = self.var("task").get()
        if task in self.project.state.get("final_locks", {}): raise ValueError(self.t("final_locked"))
        configs = configs or [self.config()]
        cap = int(self.var("max_experiments").get())
        for config in configs[:cap]:
            self.runner.submit(config, self.project.state["splits"][config["task"]])
        self.save()
        self.navigate("queue")
        self.refresh_queue()

    def batch(self, add=False):
        base = self.config()
        allowed = [c for c, role in self.project.state["roles"].items() if role == "feature"]
        candidates = [f for f in allowed if f not in base["features"]] if add else base["features"]
        configs = []
        for feature in candidates:
            config = copy.deepcopy(base)
            config["features"] = base["features"] + [feature] if add else [f for f in base["features"] if f != feature]
            if config["features"]:
                config["name"] = base["name"] + (" + " if add else " − ") + feature
                configs.append(config)
        if configs: self.run(configs)

    def on_record(self, record):
        if record["status"] == "completed" and record.get("phase") == "validation":
            if not self.project.state["baselines"].get(record["task"]):
                self.project.state["baselines"][record["task"]] = record["id"]
                self.project.save()
        self.last_refresh = 0

    def tick(self):
        if self.closing: return
        try:
            if self.runner: self.runner.poll()
            try:
                kind, payload = self.io.get_nowait()
                self.io_busy = False
                if kind == "loaded": self.wizard(*payload)
                else: self.show_error(payload)
            except queue.Empty: pass
            if time.monotonic() - self.last_refresh > .7:
                self.refresh()
                self.last_refresh = time.monotonic()
            if self.project and time.monotonic() - self.last_auto_save > 15:
                self.save()
        except Exception as exc:
            if hasattr(self, "footer"): self.footer.configure(text=self.t("error") + ": " + str(exc))
        self.root.after(100, self.tick)

    def refresh(self):
        if self.project:
            self.header.configure(text=f"{self.project.state['name']}  |  {self.t('version')} {self.project.state['data_hash'][:10]}  |  {self.t(self.var('task').get())}")
            if self.current_page == "queue": self.refresh_queue()
            if self.current_page == "design": self.update_run_summary()
        else: self.header.configure(text=self.t("no_project"))
        status = "loading" if self.io_busy else "running" if self.runner and self.runner.busy else "idle"
        self.footer.configure(text=f"{self.t('saved') if self.project else self.t('ready')}  ·  {self.t(status)}  ·  CPU  |  {self.t('shortcuts')}")

    def close(self):
        if self.closing: return
        self.closing = True
        self.save()
        if self.runner: self.runner.close()
        if self.project: self.project.close()
        self.root.destroy()

    def wizard(self, df, source):
        win = tk.Toplevel(self.root)
        win.title(self.t("new"))
        win.geometry("1020x850")
        win.minsize(740, 520)
        win.transient(self.root)
        area = ScrollFrame(win)
        area.pack(fill="both", expand=True)
        body = area.body
        columns = list(df.columns)
        variables = {key: tk.StringVar(win, value=str(value)) for key, value in dict(
            project_name=Path(source).stem, target=columns[-1], task="classification",
            regression_target=columns[-1], positive_class="", target_unit="", split="random",
            folds=3, test_size=.2, seed=42, role="feature", bin_edges="", bin_rationale="").items()}
        roles = {c: "feature" for c in columns}
        self.label(body, "new", "Title.TLabel")
        self.field(body, "project_name", variable=variables["project_name"])
        ttk.Label(body, text=f"{Path(source).name}  ·  {len(df):,} × {len(columns)}", style="Muted.TLabel").pack(anchor="w")
        self.label(body, "target_first", "Heading.TLabel")
        self.field(body, "target", choices=columns, variable=variables["target"], translated=False)
        self.label(body, "task_second", "Heading.TLabel")
        self.field(body, "task", choices=["classification", "regression", "both"], variable=variables["task"])
        self.field(body, "regression_target", choices=columns, variable=variables["regression_target"], translated=False)
        self.label(body, "numeric_hint", "Muted.TLabel")
        self.field(body, "positive_class", variable=variables["positive_class"])
        self.field(body, "target_unit", variable=variables["target_unit"])
        self.field(body, "bins", variable=variables["bin_edges"])
        self.field(body, "bin_rationale", variable=variables["bin_rationale"])
        self.label(body, "roles_third", "Heading.TLabel")
        self.label(body, "roles_help", "Muted.TLabel")
        role_tree = tree(body, ["column", "dtype", "role", "missing"], [self.t(k) for k in ["column", "dtype", "role", "missing"]], 7)
        def targets():
            return {variables["target"].get(), variables["regression_target"].get()} if variables["task"].get() == "both" else {variables["target"].get()}
        def refresh_roles(*args):
            selection = role_tree.selection()
            role_tree.delete(*role_tree.get_children())
            for i, column in enumerate(columns):
                role_tree.insert("", "end", iid=str(i), values=(column, str(df[column].dtype), self.t("target" if column in targets() else roles[column]), f"{df[column].isna().mean():.1%}"))
            role_tree.selection_set([i for i in selection if role_tree.exists(i)])
        refresh_roles()
        for key in ["target", "task", "regression_target"]: variables[key].trace_add("write", refresh_roles)
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.combo(row, variables["role"], ["feature", "id", "group", "time", "exclude"]).pack(side="left")
        def apply_role():
            for index in role_tree.selection():
                column = columns[int(index)]
                if column not in targets(): roles[column] = variables["role"].get()
            refresh_roles()
        self.button(row, "apply_role", apply_role)
        self.label(body, "evaluation_fourth", "Heading.TLabel")
        self.field(body, "split", choices=["random", "group", "time"], variable=variables["split"])
        for key in ["folds", "test_size", "seed"]: self.field(body, key, variable=variables[key])
        self.label(body, "split_help", "Muted.TLabel")
        error_label = ttk.Label(body, text="", foreground="#a32222", wraplength=900)
        error_label.pack(fill="x", pady=8)
        buttons = ttk.Frame(win, padding=10)
        buttons.pack(fill="x", side="bottom")
        def finish(run):
            try:
                task_choice = variables["task"].get()
                tasks = ["classification", "regression"] if task_choice == "both" else [task_choice]
                assigned = {c: "target" if c in targets() else roles[c] for c in columns}
                features = [c for c in columns if assigned[c] == "feature"]
                if not features: raise ValueError(self.t("need_feature"))
                group = next((c for c, r in assigned.items() if r == "group"), None)
                time_column = next((c for c, r in assigned.items() if r == "time"), None)
                strategy = variables["split"].get()
                if strategy == "group" and not group: raise ValueError(self.t("need_group"))
                if strategy == "time" and not time_column: raise ValueError(self.t("need_time"))
                configs = {}
                for task in tasks:
                    target = variables["regression_target"].get() if task_choice == "both" and task == "regression" else variables["target"].get()
                    if task == "regression" and not pd.api.types.is_numeric_dtype(df[target]): raise ValueError(self.t("numeric_regression"))
                    cuts = [float(x.strip()) for x in variables["bin_edges"].get().split(",")] if task == "classification" and variables["bin_edges"].get().strip() else None
                    if cuts and not variables["bin_rationale"].get().strip(): raise ValueError(self.t("need_rationale"))
                    labels = df[target].dropna().unique().tolist()
                    positive = next((v for v in labels if str(v) == variables["positive_class"].get()), None)
                    if variables["positive_class"].get() and not cuts and positive is None and task == "classification": raise ValueError(self.t("need_positive"))
                    config = dict(task=task, target=target, features=features, split=strategy, group=group,
                                  time=time_column, folds=int(variables["folds"].get()), test_size=float(variables["test_size"].get()),
                                  seed=int(variables["seed"].get()), name=self.t("baseline_name"), model="linear", params={},
                                  imputer="median", encoding="onehot", scaling="standard", positive_class=positive,
                                  threshold=.5, average="macro", class_weight=None, target_unit=variables["target_unit"].get(),
                                  target_transform="none", bin_edges=cuts, bin_rationale=variables["bin_rationale"].get(),
                                  device="cpu", primary_metric="roc_auc" if task == "classification" else "rmse", max_seconds=300)
                    if len(tasks) == 2: config["shared_targets"] = list(targets())
                    configs[task] = config
                common_plan = make_split_plan(df, configs[tasks[0]])
                configs = {task: resolve_config(df, config, common_plan) for task, config in configs.items()}
                folder = filedialog.askdirectory(parent=win, title=self.t("project_folder"))
                if not folder: return
                name = variables["project_name"].get().strip() or "Research"
                safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)[:80]
                path = Path(folder) / (safe_name + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".rwb")
                project = Project.create(path, df, name, source)
                for config in configs.values(): config["data_hash"] = project.state["data_hash"]
                project.state.update(roles=assigned, tasks=configs, splits={task: common_plan for task in tasks})
                project.save()
                win.destroy()
                self.attach_project(project)
                if run:
                    for config in configs.values(): self.runner.submit(config, common_plan)
                    self.navigate("queue")
            except Exception as exc:
                if win.winfo_exists():
                    error_label.configure(text=self.t("invalid") + ": " + str(exc))
                    area.canvas.yview_moveto(1)
                else:
                    self.show_error(exc)
        self.button(buttons, "create_run", lambda: finish(True))
        self.button(buttons, "create", lambda: finish(False))
        self.button(buttons, "close", win.destroy)

    def build_explore(self, parent):
        self.label(parent, "train_scope", "Heading.TLabel")
        self.label(parent, "sample_notice", "Muted.TLabel")
        tabs = ttk.Notebook(parent)
        tabs.pack(fill="both", expand=True, pady=8)
        preview = ttk.Frame(tabs, padding=8)
        quality = ttk.Frame(tabs, padding=8)
        plotting = ttk.Frame(tabs, padding=8)
        tabs.add(preview, text=self.t("preview"))
        tabs.add(quality, text=self.t("quality"))
        tabs.add(plotting, text=self.t("plot"))
        self.preview_tree = tree(preview, list(self.project.df.columns), list(self.project.df.columns), 15)
        row = ttk.Frame(preview)
        row.pack(fill="x")
        self.button(row, "previous", lambda: self.change_page(-1))
        self.button(row, "next", lambda: self.change_page(1))
        self.page_label = ttk.Label(row)
        self.page_label.pack(side="left", padx=10)
        self.refresh_preview()
        profile = data_profile(self.training_df())
        self.quality_tree = tree(quality, ["column", "dtype", "missing", "unique", "role"], [self.t(k) for k in ["column", "dtype", "missing", "unique", "role"]], 12)
        for f in profile["features"]:
            self.quality_tree.insert("", "end", values=(f["name"], f["dtype"], f"{f['missing']:,} ({f['missing_pct']:.1f}%)", f["unique"], self.t(self.project.state["roles"].get(f["name"], "feature"))))
        self.label(quality, "data_target_warning", "Muted.TLabel")
        config = self.project.state["tasks"][self.var("task").get()]
        for warning in target_review(self.training_df(), config["target"], config["features"], config["task"]):
            self.label(quality, warning["code"], "Muted.TLabel", **{k: v for k, v in warning.items() if k != "code"})
        summary = tk.Text(quality, height=10, wrap="word", font=("Malgun Gothic", 10))
        summary.pack(fill="both", expand=True)
        stats = self.training_df().describe(include="all").to_string()
        summary.insert("1.0", stats)
        summary.configure(state="disabled")
        row = ttk.Frame(plotting)
        row.pack(fill="x")
        self.combo(row, self.var("eda_kind"), ["correlation", "missing", "histogram", "box", "target", "scatter"]).pack(side="left", padx=4)
        self.combo(row, self.var("corr_method"), ["pearson", "spearman"], translated=False, width=12).pack(side="left", padx=4)
        if not self.var("feature").get(): self.var("feature").set(self.project.state["tasks"][self.var("task").get()]["features"][0])
        self.combo(row, self.var("feature"), list(self.project.df.columns), translated=False, width=32).pack(side="left", fill="x", expand=True)
        self.button(row, "plot", self.draw_eda)
        self.eda_host = ttk.Frame(plotting)
        self.eda_host.pack(fill="both", expand=True)
        self.draw_eda()

    def change_page(self, delta):
        total = max(1, math.ceil(len(self.training_df()) / 100))
        self.page_index = max(0, min(total - 1, self.page_index + delta))
        self.refresh_preview()

    def refresh_preview(self):
        frame = self.training_df()
        self.preview_tree.delete(*self.preview_tree.get_children())
        for row in frame.iloc[self.page_index * 100:(self.page_index + 1) * 100].itertuples(index=False, name=None):
            self.preview_tree.insert("", "end", values=["—" if pd.isna(v) else str(v) for v in row])
        self.page_label.configure(text=self.t("page", page=self.page_index + 1, total=max(1, math.ceil(len(frame) / 100)), rows=len(frame)))

    def embed(self, key, host, figure):
        if key in self.plot_canvases:
            self.plot_canvases[key].get_tk_widget().destroy()
        canvas = ResearchCanvas(figure, master=host)
        canvas.draw()
        canvas.get_tk_widget().configure(height=440)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.plot_canvases[key] = canvas
        self.plot_figures[key] = figure

    def draw_eda(self):
        frame = self.training_df()
        if len(frame) > 5000: frame = frame.sample(5000, random_state=42)
        kind = self.var("eda_kind").get()
        config = self.project.state["tasks"][self.var("task").get()]
        if kind == "correlation": frame = frame[config["features"]].select_dtypes(include="number").iloc[:, :40]
        frame = self.plotting_dataframe(frame)
        fig = make_figure(kind, df=frame, lang=self.lang, target=self.display_name(config["target"]), feature=self.display_name(self.var("feature").get()),
                          options={"corr_method": self.var("corr_method").get(), "scope": self.t("train_scope"), "task": config["task"], "target_unit": config.get("target_unit"), "width": 8, "height": 4.8})
        self.embed("eda", self.eda_host, fig)

    def build_design(self, parent):
        self.task_selector(parent)
        self.label(parent, "fixed_split", "Muted.TLabel")
        pane = ttk.Panedwindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        form = ttk.Frame(pane, padding=(0, 0, 12, 0))
        feature_side = ttk.Frame(pane, padding=8)
        pane.add(form, weight=3)
        pane.add(feature_side, weight=2)
        tabs = ttk.Notebook(form)
        tabs.pack(fill="x")
        basic = ttk.Frame(tabs, padding=10)
        advanced = ttk.Frame(tabs, padding=10)
        tabs.add(basic, text=self.t("basic"))
        tabs.add(advanced, text=self.t("advanced"))
        self.field(basic, "name")
        self.field(basic, "model", ["dummy", "linear", "forest", "mlp"])
        self.field(basic, "imputer", ["median", "mean", "constant"])
        self.field(basic, "encoding", ["onehot", "ordinal"])
        self.field(basic, "scaling", ["standard", "robust", "none"])
        self.field(basic, "primary_metric", CLASS_METRICS if self.var("task").get() == "classification" else REG_METRICS, translated=False)
        if self.var("task").get() == "classification":
            self.field(basic, "positive_class")
            self.field(basic, "threshold")
            self.field(basic, "average", ["macro", "weighted"])
            self.field(basic, "class_weight", ["none", "balanced"])
            self.field(advanced, "bins", variable=self.var("bin_edges"))
            self.field(advanced, "bin_rationale")
            self.label(advanced, "bin_help", "Muted.TLabel")
        else:
            self.field(basic, "target_unit")
            self.field(basic, "target_transform", ["none", "log1p", "standard"])
        self.field(advanced, "params")
        self.label(advanced, "params_help", "Muted.TLabel")
        self.field(advanced, "max_seconds")
        self.field(advanced, "max_experiments")
        self.label(advanced, "concurrency", "Muted.TLabel")
        self.label(advanced, "cpu_notice", "Muted.TLabel")
        self.label(basic, "metric_help", "Muted.TLabel")
        self.label(feature_side, "features", "Heading.TLabel")
        search = self.field(feature_side, "search")
        search.bind("<KeyRelease>", lambda e: self.refresh_features())
        self.field(feature_side, "dtype", ["all", "numeric", "categorical"], self.refresh_features, variable=self.var("type_filter"))
        actions = ttk.Frame(feature_side)
        actions.pack(fill="x")
        self.button(actions, "select_all", lambda: self.select_visible(True))
        self.button(actions, "deselect_all", lambda: self.select_visible(False))
        self.feature_tree = tree(feature_side, ["selected", "column", "dtype"], ["✓", self.t("column"), self.t("dtype")], 13)
        self.feature_tree.column("selected", width=40, stretch=False)
        self.feature_tree.bind("<Double-1>", lambda e: self.toggle_feature())
        self.feature_tree.bind("<space>", lambda e: self.toggle_feature())
        self.feature_tree.bind("<Return>", lambda e: self.toggle_feature())
        self.feature_count = ttk.Label(feature_side)
        self.feature_count.pack(fill="x", pady=4)
        self.feature_detail = ttk.Label(feature_side, wraplength=450)
        self.feature_detail.pack(fill="x")
        self.feature_tree.bind("<<TreeviewSelect>>", lambda e: self.feature_detail.configure(text="\n".join(self.feature_names[int(i)] for i in self.feature_tree.selection())))
        self.refresh_features()
        self.label(parent, "preview_settings", "Heading.TLabel")
        self.design_summary = ttk.Label(parent, wraplength=1050)
        self.design_summary.pack(fill="x", pady=5)
        self.design_error = ttk.Label(parent, foreground="#aa2222", wraplength=1050)
        self.design_error.pack(fill="x")
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=8)
        self.button(actions, "run", self.design_run)
        self.button(actions, "ablation", lambda: self.design_run("remove"))
        self.button(actions, "addition", lambda: self.design_run("add"))
        self.label(parent, "batch_help", "Muted.TLabel")
        self.label(parent, "config_saved", "Muted.TLabel")
        self.update_run_summary()

    def refresh_features(self):
        self.feature_names = [c for c, role in self.project.state["roles"].items() if role == "feature"]
        search = self.var("search").get().lower()
        kind = self.var("type_filter").get()
        self.feature_tree.delete(*self.feature_tree.get_children())
        for i, column in enumerate(self.feature_names):
            numeric = pd.api.types.is_numeric_dtype(self.project.df[column])
            if search not in column.lower() or (kind == "numeric" and not numeric) or (kind == "categorical" and numeric): continue
            self.feature_tree.insert("", "end", iid=str(i), values=("✓" if column in self.selected_features else "□", column, str(self.project.df[column].dtype)))
        self.feature_count.configure(text=self.t("feature_count", selected=len(self.selected_features), excluded=len(self.feature_names) - len(self.selected_features)))

    def toggle_feature(self):
        ids = self.feature_tree.selection()
        for index in ids:
            column = self.feature_names[int(index)]
            if column in self.selected_features: self.selected_features.remove(column)
            else: self.selected_features.add(column)
        self.refresh_features()
        self.feature_tree.selection_set(ids)
        self.update_run_summary()

    def select_visible(self, state):
        names = {self.feature_names[int(i)] for i in self.feature_tree.get_children()}
        if state: self.selected_features |= names
        else: self.selected_features -= names
        self.refresh_features()
        self.update_run_summary()

    def update_run_summary(self, count=1):
        if not hasattr(self, "design_summary") or not self.design_summary.winfo_exists(): return
        config = self.project.state["tasks"][self.var("task").get()]
        self.design_summary.configure(text=self.t("run_summary", task=self.t(self.var("task").get()), target=config["target"], model=self.t(self.var("model").get()), n=len(self.selected_features), folds=config["folds"], test=config["test_size"], count=count))

    def design_run(self, mode=None):
        try:
            self.config()
            self.design_error.configure(text="")
            if mode: self.batch(add=mode == "add")
            else: self.run()
        except Exception as exc:
            self.design_error.configure(text=self.t("invalid") + ": " + str(exc))

    def build_queue(self, parent):
        self.label(parent, "concurrency", "Muted.TLabel")
        self.queue_tree = tree(parent, ["name", "id", "task", "model", "status", "stage", "duration", "progress", "metric"], [self.t(k) for k in ["name", "id", "task", "model", "status", "stage", "duration", "progress", "metric"]], 14)
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        self.button(actions, "cancel", self.cancel_selected)
        self.button(actions, "retry", self.retry_selected)
        self.button(actions, "logs", self.queue_details)
        self.queue_log = tk.Text(parent, height=11, wrap="word", font=("Malgun Gothic", 10))
        self.queue_log.pack(fill="both", expand=True, pady=8)
        self.live_plot_host = ttk.Frame(parent)
        self.live_plot_host.pack(fill="both", expand=True)
        self.last_history_key = None
        self.queue_tree.bind("<<TreeviewSelect>>", lambda e: self.queue_details())
        self.refresh_queue()

    def refresh_queue(self):
        if not hasattr(self, "queue_tree") or not self.queue_tree.winfo_exists(): return
        selection = self.queue_tree.selection()
        records = {r["id"]: r for r in self.all_results()}
        if self.runner: records.update(self.runner.records)
        for record in records.values():
            progress = record.get("progress", {})
            metric = record.get("summary", {}).get(record["config"].get("primary_metric", "roc_auc"), {}).get("mean")
            values = (record["name"], record["id"][:12], self.t(record["task"]), self.t(record["config"]["model"]), self.t(record["status"]), self.t(record.get("stage", record["status"])), f"{record.get('duration',0):.1f}", f"{progress.get('fold','—')}/{progress.get('total_folds','—')} · {progress.get('epoch','—')}/{progress.get('total_epochs','—')}", "—" if metric is None else f"{metric:.4f}")
            if self.queue_tree.exists(record["id"]): self.queue_tree.item(record["id"], values=values)
            else: self.queue_tree.insert("", "end", iid=record["id"], values=values)
        for iid in self.queue_tree.get_children():
            if iid not in records: self.queue_tree.delete(iid)
        self.queue_tree.selection_set([i for i in selection if self.queue_tree.exists(i)])
        if self.runner and self.runner.active:
            active = self.runner.active["record"]
            history = active.get("history", [])
            history_key = (active["id"], len(history))
            if history and history_key != self.last_history_key:
                self.last_history_key = history_key
                figure = make_figure("learning", results=[active], lang=self.lang, options={"width": 8, "height": 4, "dpi": 90})
                self.embed("live", self.live_plot_host, figure)

    def cancel_selected(self):
        for iid in self.queue_tree.selection(): self.runner.cancel(iid)
        self.refresh_queue()

    def retry_selected(self):
        ids = set(self.queue_tree.selection())
        for record in self.all_results():
            if record["id"] in ids:
                if record["task"] in self.project.state.get("final_locks", {}): raise ValueError(self.t("final_locked"))
                config = copy.deepcopy(record["config"])
                self.runner.submit(config, self.project.state["splits"][record["task"]])

    def queue_details(self):
        ids = set(self.queue_tree.selection())
        records = {r["id"]: r for r in self.all_results()}
        records.update(self.runner.records)
        selected = [r for i, r in records.items() if i in ids]
        self.queue_log.configure(state="normal")
        self.queue_log.delete("1.0", "end")
        for record in selected:
            self.queue_log.insert("end", f"{record['name']} · {self.t(record['status'])}\n")
            if record.get("error"): self.queue_log.insert("end", self.t("error_summary") + "\n" + record.get("traceback", record["error"]) + "\n")
            self.queue_log.insert("end", json.dumps({k: record.get(k) for k in ["id", "config", "progress", "summary", "row_scope", "versions", "duration"]}, ensure_ascii=False, indent=2) + "\n\n")
        self.queue_log.configure(state="disabled")


def main():
    import argparse
    import ctypes
    parser = argparse.ArgumentParser(description="Native tabular research workbench")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--language", choices=["ko", "en"])
    parser.add_argument("--no-preferences", action="store_true", help="Use session-only language preferences")
    args = parser.parse_args()
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError): pass
    root = tk.Tk()
    app = App(root, language=args.language, preferences=not args.no_preferences)
    if args.project: app.safe(lambda: app.attach_project(Project.open(args.project)))
    root.after(300, root.deiconify)
    root.mainloop()
