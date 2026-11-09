"""Result-oriented Tk views mixed into the desktop App.

The mixin deliberately imports no App symbols, keeping native widget construction
separate from the training, persistence, and queue layers.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from .data import data_profile, fingerprint, load_data
from .plotting import comparison_table, export_figure, export_tables, make_figure
from .recommendations import suggest
from .canvas import ResearchCanvas


LOWER_BETTER = {"mae", "mse", "rmse"}
VIEW_CATALOG = json.loads((Path(__file__).with_name("locales") / "views.json").read_text(encoding="utf-8"))


class ResultViews:
    """Overview, comparison, export and project settings views for ``App``."""

    def _tree(self, parent, columns, headings, height=8, selectmode="extended"):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True, pady=5)
        widget = ttk.Treeview(holder, columns=columns, show="headings", height=height, selectmode=selectmode)
        for column, heading in zip(columns, headings):
            widget.heading(column, text=heading)
            widget.column(column, width=150, minwidth=75, stretch=True)
        vertical = ttk.Scrollbar(holder, orient="vertical", command=widget.yview)
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=widget.xview)
        widget.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        widget.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        return widget

    def _show_figure(self, parent, key, figure):
        old = self.plot_canvases.pop(key, None)
        if old is not None:
            old.get_tk_widget().destroy()
        old_figure = self.plot_figures.pop(key, None)
        if old_figure is not None:
            old_figure.clear()
        canvas = ResearchCanvas(figure, master=parent)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True, pady=8)
        self.plot_canvases[key] = canvas
        self.plot_figures[key] = figure
        return figure

    def display_name(self, column):
        """Resolve a UI/plot alias without mutating the source column name."""
        names = self.project.state.get("display_names", {}).get(column, {}) if self.project else {}
        return names.get(self.lang) or column

    def plotting_dataframe(self, frame=None):
        """Return a renamed copy for plots; model and persisted data stay original."""
        source = (frame if frame is not None else self.project.df).copy()
        proposed = {column: self.display_name(column) for column in source.columns}
        aliases = list(proposed.values())
        if len(set(aliases)) != len(aliases):
            return source
        return source.rename(columns=proposed)

    def _plot_options(self, preview=True):
        hidden = self.var("hide_title").get() in {"1", "true", "True"}
        text = lambda key: self.var(key).get().strip() or None
        task = self.var("task").get()
        task_config = self.project.state.get("tasks", {}).get(task, {}) if self.project else {}
        return {
            "title": "" if hidden else text("title"),
            "xlabel": text("xlabel"), "ylabel": text("ylabel"), "annotation": text("annotation"),
            "font_size": float(self.var("font_size").get()), "width": float(self.var("width").get()),
            "height": float(self.var("height").get()), "dpi": 100 if preview else int(float(self.var("dpi").get())),
            "legend": self.var("legend").get(), "color": self.var("color").get(),
            "corr_method": self.var("corr_method").get(), "scope": "train", "task": task,
            "target_unit": task_config.get("target_unit") or self.var("target_unit").get(),
        }

    def _result_figure(self, preview=True):
        kind = self.var("result_kind").get()
        if kind in {"correlation", "missing", "histogram", "box", "target", "scatter"}:
            frame = self.training_df()
            if len(frame) > 5000:
                frame = frame.sample(5000, random_state=42)
            task_config = self.project.state["tasks"][self.var("task").get()]
            if kind == "correlation":
                frame = frame[task_config["features"]].select_dtypes(include="number").iloc[:, :40]
            frame = self.plotting_dataframe(frame)
            original_feature = self.var("feature").get()
            feature = self.display_name(original_feature) if original_feature else None
            target = self.display_name(task_config["target"])
            return make_figure(kind, df=frame, feature=feature, target=target, lang=self.lang, options=self._plot_options(preview))
        results = self.chosen_results()
        if not results: return None
        if kind in {"comparison", "delta"}: results = self._include_pinned_baseline(results)
        return make_figure(
            kind, results=results, metric=self.var("metric").get(),
            lang=self.lang, options=self._plot_options(preview), baseline_id=self.baseline_id(),
        )

    def _include_pinned_baseline(self, results):
        results = list(results)
        if results and not any(r["id"] == self.baseline_id() for r in results):
            baseline = next((r for r in self.task_results(self.var("phase").get()) if r["id"] == self.baseline_id()), None)
            if baseline: results.insert(0, baseline)
        return results

    def _plot_results(self, parent, key="results"):
        figure = self._result_figure(True)
        if figure is None:
            if hasattr(self, "result_plot_note"):
                self.result_plot_note.configure(text=self.t("no_results"))
            return None
        return self._show_figure(parent, key, figure)

    def build_overview(self, parent):
        self.task_selector(parent)
        self.overview_summary = ttk.Label(parent, style="Heading.TLabel", wraplength=1050, justify="left")
        self.overview_summary.pack(anchor="w", fill="x", pady=6)
        self.overview_quality = ttk.Label(parent, wraplength=1050, justify="left")
        self.overview_quality.pack(anchor="w", fill="x", pady=4)
        self.overview_target = ttk.Label(parent, wraplength=1050, justify="left")
        self.overview_target.pack(anchor="w", fill="x", pady=4)
        self.overview_runs = ttk.Label(parent, wraplength=1050, justify="left")
        self.overview_runs.pack(anchor="w", fill="x", pady=4)
        self.overview_baseline = ttk.Label(parent, wraplength=1050, justify="left")
        self.overview_baseline.pack(anchor="w", fill="x", pady=4)
        self.label(parent, "best", "Heading.TLabel")
        self.overview_best = ttk.Label(parent, wraplength=1050, justify="left")
        self.overview_best.pack(anchor="w", fill="x", pady=4)
        self.overview_figure = ttk.Frame(parent)
        self.overview_figure.pack(fill="both", expand=True)

        self.label(parent, "recommendations", "Heading.TLabel")
        self.label(parent, "recommend_help", "Muted.TLabel")
        self.recommendation_tree = self._tree(
            parent, ("reason", "change", "metric", "burden"),
            (self.t("reason"), self.t("change"), self.t("metric"), self.t("burden")), height=6,
        )
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "run_selected", self._run_selected_recommendation)
        self.button(actions, "auto_run", self._auto_run_recommendations)
        self.refresh_overview()

    def refresh_overview(self):
        if not self.project or not hasattr(self, "overview_summary"):
            return
        frame = self.training_df()
        profile = data_profile(frame)
        task = self.var("task").get(); config = self.project.state["tasks"][task]
        target = config["target"]
        target_values = frame[target].dropna()
        target_text = f"{target}: {target_values.nunique()} {self.t('unique')}"
        self.overview_summary.configure(
            text=f"{self.t('train_scope')} · {profile['rows']:,} {self.t('rows')} · {profile['columns']} {self.t('columns')} · {target_text}"
        )
        constants = sum(item["constant"] for item in profile["features"])
        self.overview_quality.configure(text=self.t(
            "quality_stats", rows=profile["rows"], cols=profile["columns"],
            duplicates=profile["duplicates"], constant=constants,
        ))
        flagged = [self._vt("quality_item", column=w.get("column", ""), reason=self._vt(w["code"])) for w in profile["warnings"][:8]]
        if flagged:
            self.overview_quality.configure(text=self.overview_quality.cget("text") + "\n" + " · ".join(flagged))
        if self.project.state.get("source_changed"):
            self.overview_quality.configure(text=self.overview_quality.cget("text") + "\n" + self.t("source_changed"))
        if task == "classification":
            counts = target_values.value_counts().to_dict()
            self.overview_target.configure(text=self._vt("class_distribution", values=" · ".join(f"{key}: {value}" for key, value in counts.items())))
        else:
            numeric = pd.to_numeric(target_values, errors="coerce").dropna()
            self.overview_target.configure(text=self._vt("regression_distribution", n=len(numeric), mean=numeric.mean(), sd=numeric.std(), low=numeric.min(), high=numeric.max()))
        statuses = [r.get("status") for r in self.all_results()]
        self.overview_runs.configure(text=self._vt(
            "runs", queued=statuses.count("queued"), running=statuses.count("running") + statuses.count("cancelling"),
            completed=statuses.count("completed"), failed=statuses.count("failed"),
            stopped=statuses.count("cancelled") + statuses.count("interrupted"),
        ))
        results = self.task_results("validation")
        metric = config.get("primary_metric", self.var("metric").get())
        valid = [r for r in results if r.get("summary", {}).get(metric, {}).get("mean") is not None]
        if self.baseline_id():
            comparisons = comparison_table(valid, self.baseline_id(), lang="en")
            comparable_ids = set(comparisons[(comparisons["metric"] == metric) & comparisons["comparable"]]["experiment_id"]) if not comparisons.empty else set()
            valid = [result for result in valid if result["id"] in comparable_ids]
        else:
            valid = []
        reverse = metric not in LOWER_BETTER
        best = sorted(valid, key=lambda r: r["summary"][metric]["mean"], reverse=reverse)[0] if valid else None
        if best:
            summary = best["summary"][metric]
            self.overview_best.configure(text=f"{best['name']} · {self._vt('metric_' + metric)} {summary['mean']:.4f} ± {summary.get('std', 0):.4f} · {self.t('fold_sd')}")
        else:
            self.overview_best.configure(text=self.t("no_results"))
        baseline = next((r for r in results if r.get("id") == self.baseline_id()), None)
        baseline_summary = baseline.get("summary", {}).get(metric, {}) if baseline else {}
        if baseline and baseline_summary.get("mean") is not None:
            self.overview_baseline.configure(text=self._vt(
                "baseline_result", name=baseline["name"], metric=self._vt("metric_" + metric), mean=baseline_summary["mean"], sd=baseline_summary.get("std") or 0,
            ))
        else:
            self.overview_baseline.configure(text=self.t("no_baseline"))
        if results:
            figure = make_figure("comparison", results=results, metric=metric, lang=self.lang, baseline_id=self.baseline_id())
            self._show_figure(self.overview_figure, "overview", figure)

        selected = set(self.recommendation_tree.selection()) if hasattr(self, "recommendation_tree") else set()
        self.recommendation_tree.delete(*self.recommendation_tree.get_children())
        self.recommendations_cache = suggest(frame, config)
        for index, item in enumerate(self.recommendations_cache):
            reason = self.t(item["code"], **item.get("values", {}))
            change = json.dumps(item["changes"], ensure_ascii=False)
            self.recommendation_tree.insert("", "end", iid=str(index), values=(reason, change, item["metric"], self.t("low")))
        for iid in selected:
            if self.recommendation_tree.exists(iid): self.recommendation_tree.selection_add(iid)
        if not self.recommendations_cache:
            self.recommendation_tree.insert("", "end", iid="none", values=(self.t("no_suggestions"), "", "", ""))

    def _recommendation_configs(self, selected_only):
        if self.var("task").get() in self.project.state.get("final_locks", {}):
            raise ValueError(self.t("final_locked"))
        indices = self.recommendation_tree.selection() if selected_only else [str(i) for i in range(len(self.recommendations_cache))]
        base = copy.deepcopy(self.project.state["tasks"][self.var("task").get()])
        base["max_seconds"] = float(self.var("max_seconds").get())
        configs = []
        for iid in indices:
            if not str(iid).isdigit(): continue
            item = self.recommendations_cache[int(iid)]
            config = copy.deepcopy(base)
            config.update(copy.deepcopy(item["changes"]))
            config["name"] = f"{base.get('name', self.t('baseline_name'))} · {self.t(item['code'], **item.get('values', {}))}"
            configs.append(config)
        return configs

    def _run_selected_recommendation(self):
        configs = self._recommendation_configs(True)
        if not configs: raise ValueError(self.t("no_selection"))
        self.run(configs)

    def _auto_run_recommendations(self):
        configs = self._recommendation_configs(False)
        cap = int(self.var("max_experiments").get())
        if cap <= 0: raise ValueError(self.t("budget_positive"))
        configs = configs[:cap]
        if not configs: raise ValueError(self.t("no_suggestions"))
        self.run(configs)

    def build_compare(self, parent):
        self.task_selector(parent)
        controls = ttk.Frame(parent); controls.pack(fill="x", pady=4)
        search_entry = self.field(controls, "filter", variable=self.var("result_filter"))
        search_entry.bind("<KeyRelease>", lambda event: self.safe(self.refresh_results))
        self.field(controls, "phase", choices=["validation", "final_test"], callback=self.refresh_results, variable=self.var("phase"))
        metrics = ["roc_auc", "average_precision", "accuracy", "balanced_accuracy", "recall", "specificity", "precision", "f1"] if self.var("task").get() == "classification" else ["rmse", "mae", "mse", "r2"]
        self.field(controls, "metric", choices=metrics, callback=self.refresh_results, translated=False, variable=self.var("metric"))
        self.result_tree = self._tree(
            parent, ("name", "model", "phase", "summary", "delta", "comparable", "duration", "id"),
            (self.t("name"), self.t("model"), self.t("phase"), self.t("summary"), self.t("difference"), self.t("row_overlap"), self.t("duration"), "ID"), height=10,
        )
        self.result_tree.bind("<<TreeviewSelect>>", lambda event: self.safe(self._selection_changed))
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "set_baseline", self._pin_baseline)
        self.button(actions, "clone", self._clone_result)
        self.button(actions, "evaluate_final", self._evaluate_final)
        self.button(actions, "details", self._show_details)
        plot_controls = ttk.Frame(parent); plot_controls.pack(fill="x", pady=5)
        kinds = ["comparison", "delta", "roc", "pr", "confusion", "learning"] if self.var("task").get() == "classification" else ["comparison", "delta", "predicted", "residuals", "learning"]
        self.combo(plot_controls, self.var("result_kind"), kinds, self._refresh_result_plot).pack(side="left")
        self.result_plot_note = ttk.Label(parent, style="Muted.TLabel"); self.result_plot_note.pack(anchor="w")
        self.result_plot_frame = ttk.Frame(parent); self.result_plot_frame.pack(fill="both", expand=True)
        self.label(parent, "not_causal", "Muted.TLabel")
        self.refresh_results()

    def refresh_results(self):
        if not self.project or not hasattr(self, "result_tree"):
            return
        selected = set(self.result_tree.selection()) | set(self.selected_results)
        self.result_tree.delete(*self.result_tree.get_children())
        phase = self.var("phase").get(); metric = self.var("metric").get()
        needle = self.var("result_filter").get().strip().lower()
        results = [r for r in self.task_results(phase) if needle in str(r.get("name", "")).lower()]
        results.sort(
            key=lambda r: r.get("summary", {}).get(metric, {}).get("mean") if r.get("summary", {}).get(metric, {}).get("mean") is not None else (-np.inf if metric not in LOWER_BETTER else np.inf),
            reverse=metric not in LOWER_BETTER,
        )
        baseline = next((r for r in self.task_results(phase) if r.get("id") == self.baseline_id()), None)
        comparison_inputs = results + ([baseline] if baseline and baseline not in results else [])
        table = comparison_table(comparison_inputs, self.baseline_id(), lang="en")
        metric_rows = {row["experiment_id"]: row for _, row in table[table["metric"] == metric].iterrows()} if not table.empty else {}
        for result in results:
            summary = result.get("summary", {}).get(metric, {}); mean = summary.get("mean"); sd = summary.get("std")
            if mean is None:
                reasons = sorted({fold.get("reasons", {}).get(metric) for fold in result.get("folds", []) if fold.get("reasons", {}).get(metric)})
                display = self.t("unavailable") + (": " + ", ".join(self._vt(reason) for reason in reasons) if reasons else "")
            else:
                display = f"{mean:.4f} ± {sd or 0:.4f}"
            row = metric_rows.get(result["id"]); delta = row.get("delta_raw") if row is not None else None
            comparable = bool(row.get("comparable")) if row is not None else False
            reason = row.get("comparison_reason") if row is not None else "baseline_not_found"
            if delta is not None and pd.notna(delta):
                pp = row.get("delta_percentage_points")
                delta_text = f"{float(pp):+.2f} pp" if pp is not None and pd.notna(pp) else f"{float(delta):+.4f}"
                improvement = row.get("improvement")
                if improvement is not None and pd.notna(improvement): delta_text += " · " + self._vt("improvement", value=float(improvement))
            else:
                delta_text = "—"
            self.result_tree.insert("", "end", iid=result["id"], values=(
                result.get("name"), result.get("config", {}).get("model"), self.t(result.get("phase", phase)), display,
                delta_text, self._vt("direct") if comparable else self._vt("not_direct", reason=self._vt(str(reason))),
                f"{float(result.get('duration', 0)):.1f}", result["id"],
            ))
            if result["id"] in selected: self.result_tree.selection_add(result["id"])
        self.selected_results = set(self.result_tree.selection())
        if results: self._refresh_result_plot()
        else: self.result_plot_note.configure(text=self.t("no_results"))

    def _selection_changed(self):
        self.selected_results = set(self.result_tree.selection())
        self._refresh_result_plot()

    def _refresh_result_plot(self):
        if hasattr(self, "result_plot_frame"):
            self.result_plot_note.configure(text=self.t("note_final") if self.var("phase").get() == "final_test" else self.t("fold_sd"))
            self._plot_results(self.result_plot_frame, "compare")

    def _selected_records(self, validation_only=False):
        ids = set(self.result_tree.selection()) if hasattr(self, "result_tree") else set(self.selected_results)
        records = [r for r in self.all_results() if r.get("id") in ids and r.get("status") == "completed"]
        if validation_only: records = [r for r in records if r.get("phase") == "validation"]
        return records

    def _pin_baseline(self):
        records = self._selected_records(True)
        if len(records) != 1: raise ValueError(self.t("need_complete"))
        self.project.state.setdefault("baselines", {})[self.var("task").get()] = records[0]["id"]
        self.project.save(); self.refresh_results(); self.refresh_overview()

    def _clone_result(self):
        if self.var("task").get() in self.project.state.get("final_locks", {}):
            raise ValueError(self.t("final_locked"))
        records = self._selected_records()
        if len(records) != 1: raise ValueError(self.t("need_complete"))
        config = copy.deepcopy(records[0]["config"])
        for key, value in config.items():
            if key not in self.vars or key in {"id", "data_hash", "features"}: continue
            if key == "params": value = json.dumps(value, ensure_ascii=False)
            if key == "bin_edges" and isinstance(value, list): value = ", ".join(str(item) for item in value)
            if key == "class_weight" and value is None: value = "none"
            self.var(key).set("" if value is None else str(value))
        self.var("name").set(f"{records[0]['name']} · {self.t('clone')}")
        self.selected_features = set(config.get("features", []))
        self.save_draft()
        self.current_page = "design"
        self.build()

    def _evaluate_final(self):
        task = self.var("task").get()
        if self.runner.busy: raise ValueError(self.t("busy_project"))
        existing_lock = self.project.state.get("final_locks", {}).get(task)
        if existing_lock:
            completed = any(r.get("task") == task and r.get("phase") == "final_test" and r.get("status") == "completed" for r in self.all_results())
            if completed: raise ValueError(self.t("final_locked"))
            if not messagebox.askyesno(self.t("final_test"), self.t("final_warning"), parent=self.root): return
            self.runner.submit(copy.deepcopy(existing_lock["config"]), self.project.state["splits"][task], final=True)
            self.project.save(); self.navigate("queue"); return
        records = self._selected_records(True)
        if len(records) != 1: raise ValueError(self.t("need_complete"))
        if not messagebox.askyesno(self.t("final_test"), self.t("final_warning"), parent=self.root): return
        chosen = records[0]
        frozen = copy.deepcopy(chosen["config"])
        lock = {"experiment_id": chosen["id"], "config": copy.deepcopy(frozen), "locked_at": datetime.now().astimezone().isoformat()}
        self.project.state.setdefault("final_locks", {})[task] = lock
        self.runner.submit(frozen, self.project.state["splits"][task], final=True)
        self.project.save(); self.navigate("queue")

    def _show_details(self):
        records = self._selected_records()
        if not records: raise ValueError(self.t("no_selection"))
        baseline = next((r for r in self.all_results() if r.get("id") == self.baseline_id()), None)
        payload = []
        for record in records:
            differences = {}
            if baseline:
                base_config = baseline.get("config", {})
                differences = {key: {"baseline": base_config.get(key), "selected": value} for key, value in record.get("config", {}).items() if base_config.get(key) != value}
            payload.append({"name": record.get("name"), "configuration_differences": differences, "record": record})
        dialog = tk.Toplevel(self.root); dialog.title(self.t("details")); dialog.geometry("900x680"); dialog.transient(self.root)
        text = tk.Text(dialog, wrap="none", font=("Consolas", 9)); text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2, default=str)); text.configure(state="disabled")
        self.button(dialog, "close", dialog.destroy)

    def build_export(self, parent):
        self.task_selector(parent)
        self.label(parent, "export_help", "Muted.TLabel")
        result_kinds = ["comparison", "delta", "roc", "pr", "confusion", "learning"] if self.var("task").get() == "classification" else ["comparison", "delta", "predicted", "residuals", "learning"]
        kinds = ["correlation", "missing", "histogram", "box", "target", "scatter", *result_kinds]
        self.field(parent, "plot", choices=kinds, callback=self._refresh_export_plot, variable=self.var("result_kind"))
        features = self.project.state["tasks"][self.var("task").get()].get("features", [])
        if features and self.var("feature").get() not in features: self.var("feature").set(features[0])
        self.field(parent, "feature", choices=features, callback=self._refresh_export_plot, translated=False, variable=self.var("feature"))
        metrics = ["roc_auc", "average_precision", "accuracy", "balanced_accuracy", "recall", "specificity", "precision", "f1"] if self.var("task").get() == "classification" else ["rmse", "mae", "mse", "r2"]
        self.field(parent, "metric", choices=metrics, callback=self._refresh_export_plot, translated=False, variable=self.var("metric"))
        for key in ("title", "xlabel", "ylabel", "annotation", "font_size", "width", "height", "dpi", "color"):
            self.field(parent, key, variable=self.var(key))
        self.field(parent, "legend", choices=["best", "upper right", "upper left", "lower right", "lower left", "none"], callback=self._refresh_export_plot, translated=False, variable=self.var("legend"))
        hide = ttk.Checkbutton(parent, text=self.t("hide_title"), variable=self.var("hide_title"), onvalue="1", offvalue="0", command=lambda: self.safe(self._refresh_export_plot))
        hide.pack(anchor="w", pady=4)
        actions = ttk.Frame(parent); actions.pack(fill="x")
        self.button(actions, "reset_auto", self._reset_plot_text)
        self.button(actions, "plot", self._refresh_export_plot)
        self.button(actions, "export_figure", self._export_current_figure)
        self.button(actions, "export_table", self._export_current_table)
        self.export_status = ttk.Label(parent, style="Muted.TLabel"); self.export_status.pack(anchor="w", fill="x")
        self.export_plot_frame = ttk.Frame(parent); self.export_plot_frame.pack(fill="both", expand=True)
        self._refresh_export_plot()

    def _reset_plot_text(self):
        for key in ("title", "xlabel", "ylabel", "annotation"): self.var(key).set("")
        self.var("hide_title").set("0"); self._refresh_export_plot()

    def _refresh_export_plot(self):
        if hasattr(self, "export_plot_frame"):
            self._plot_results(self.export_plot_frame, "export")

    def _export_current_figure(self):
        figure = self._result_figure(False)
        if figure is None: raise ValueError(self.t("no_results"))
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".png", filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")])
        if not path: return
        export_figure(figure, path, int(float(self.var("dpi").get())))
        figure.clear()
        self.export_status.configure(text=self.t("exported", path=path))

    def _export_current_table(self):
        kind = self.var("result_kind").get()
        if kind in {"correlation", "missing", "histogram", "box", "target", "scatter"}:
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
            if not path: return
            frame = self.training_df()
            if len(frame) > 5000: frame = frame.sample(5000, random_state=42)
            config = self.project.state["tasks"][self.var("task").get()]
            feature = self.var("feature").get(); target = config["target"]
            if kind == "correlation":
                table = frame[config["features"]].select_dtypes(include=np.number).iloc[:, :40].corr(method=self.var("corr_method").get())
            elif kind == "missing":
                table = pd.DataFrame({self.t("column"): frame.columns, self.t("missing"): frame.isna().sum().values, self.t("rows"): len(frame)})
            else:
                columns = [column for column in (feature, target) if column and column in frame]
                table = frame[list(dict.fromkeys(columns))]
            target_path = Path(path)
            if target_path.suffix.lower() == ".csv": table.to_csv(target_path, encoding="utf-8-sig", index=kind == "correlation")
            elif target_path.suffix.lower() in {".xlsx", ".xlsm"}: table.to_excel(target_path, index=kind == "correlation")
            else: raise ValueError("table_format_must_be_csv_or_xlsx")
            self.export_status.configure(text=self.t("exported", path=path)); return
        results = self._include_pinned_baseline(self.chosen_results())
        if not results: raise ValueError(self.t("no_results"))
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path: return
        export_tables(results, path, self.baseline_id(), self.lang)
        self.export_status.configure(text=self.t("exported", path=path))

    def build_settings(self, parent):
        if not self.project:
            self.label(parent, "language")
            self.label(parent, "limits_text", "Muted.TLabel")
            return
        self.label(parent, "source", "Heading.TLabel")
        source = self.project.state.get("source_path") or self.t("source_missing")
        self.settings_source = ttk.Label(parent, text=source, wraplength=1050); self.settings_source.pack(anchor="w", fill="x", pady=4)
        self.settings_version = ttk.Label(parent, text=self.project.state.get("data_hash", "")); self.settings_version.pack(anchor="w", fill="x", pady=4)
        source_actions = ttk.Frame(parent); source_actions.pack(fill="x")
        self.button(source_actions, "verify_source", self._verify_source)
        self.settings_source_status = ttk.Label(parent, style="Muted.TLabel"); self.settings_source_status.pack(anchor="w", fill="x", pady=4)
        self.label(parent, "display_names", "Heading.TLabel")
        self.var("display_names").set(json.dumps(self.project.state.get("display_names", {}), ensure_ascii=False))
        self.field(parent, "display_names", variable=self.var("display_names"))
        display_actions = ttk.Frame(parent); display_actions.pack(fill="x")
        self.button(display_actions, "save_settings", self._save_display_names)
        self.label(parent, "limitations", "Heading.TLabel")
        self.label(parent, "limits_text", "Muted.TLabel")
        self.label(parent, "config_saved", "Muted.TLabel")

    def _verify_source(self):
        source = self.project.state.get("source_path")
        if not source or not Path(source).is_file():
            self.settings_source_status.configure(text=self.t("source_missing")); return
        current = fingerprint(load_data(source))
        changed = current != self.project.state.get("data_hash")
        self.project.state["source_changed"] = changed
        self.project.save()
        self.settings_source_status.configure(text=self.t("source_changed" if changed else "source_unchanged"))

    def _save_display_names(self):
        mapping = json.loads(self.var("display_names").get() or "{}")
        if not isinstance(mapping, dict): raise ValueError(self.t("display_names"))
        clean = {}
        for column, names in mapping.items():
            if column not in self.project.df.columns or not isinstance(names, dict):
                raise ValueError(f"{self.t('column')}: {column}")
            clean[column] = {language: str(names.get(language, "")) for language in ("ko", "en")}
        self.project.state["display_names"] = clean
        self.project.save()
        self.settings_source_status.configure(text=self.t("saved"))

    def _vt(self, key, **values):
        text = VIEW_CATALOG.get(key, [key, key])[1 if self.lang == "en" else 0]
        return text.format(**values) if values else text

