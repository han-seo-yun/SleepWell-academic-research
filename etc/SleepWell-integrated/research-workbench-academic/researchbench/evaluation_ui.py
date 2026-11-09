"""Tk mixin for frozen external and participant-specific evaluation screens."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from tkinter import filedialog, ttk
import tkinter as tk
from typing import Any, Mapping

import numpy as np
import matplotlib as mpl
from matplotlib.figure import Figure

from .advanced_evaluation import evaluate_external, freeze_general, paired_compare
from .canvas import ResearchCanvas
from .data import load_data
from .participant_prospective import ParticipantProspective, run_multi_participant_synthetic
from .result_adapter import verify_run


_TEXT = json.loads((Path(__file__).parent / "locales" / "evaluation_ui.json").read_text(encoding="utf-8"))
mpl.rcParams["font.family"] = "sans-serif"
mpl.rcParams["font.sans-serif"] = ["Malgun Gothic", "DejaVu Sans"]
mpl.rcParams["axes.unicode_minus"] = False


class EvaluationUIMixin:
    """Host contract: root, project, lang, var(), status, and platform_runs()."""

    def _et(self, key: str) -> str:
        value = _TEXT.get(key)
        return value[1 if self.lang == "en" else 0] if value else key

    def _eval_row(self, parent: tk.Misc, key: str, variable: str, *, width: int = 62) -> ttk.Entry:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et(key), width=29).pack(side="left")
        entry = ttk.Entry(row, textvariable=self.var(variable), width=width)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _eval_button(self, parent: tk.Misc, key: str, command: Any) -> ttk.Button:
        button = ttk.Button(parent, text=self._et(key), command=lambda: self.safe(command))
        button.pack(side="left", padx=(0, 7), pady=3)
        return button

    def _eval_json(self, value: Any) -> None:
        self.var("evaluation_result_json").set(json.dumps(value, ensure_ascii=False, indent=2))
        if hasattr(self, "evaluation_json") and self.evaluation_json.winfo_exists():
            self.evaluation_json.configure(state="normal")
            self.evaluation_json.delete("1.0", "end")
            text = self.var("evaluation_result_json").get()
            self.evaluation_json.insert("1.0", text if len(text) <= 120_000 else text[:120_000] + "\n…")
            self.evaluation_json.configure(state="disabled")

    def _eval_async(self, label: str, operation: Any, completed: Any) -> None:
        if getattr(self, "_evaluation_busy", False):
            raise ValueError(self._et("running"))
        self._evaluation_busy = True
        self.status.set(f"{self._et('running')} · {label}")
        mailbox: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._evaluation_mailbox = mailbox
        self._evaluation_callback = completed

        def worker() -> None:
            try:
                result = operation()
            except Exception as error:
                mailbox.put(("error", error))
            else:
                mailbox.put(("result", result))

        threading.Thread(target=worker, name="researchbench-evaluation", daemon=True).start()
        self.root.after(25, self._eval_poll_async)

    def _eval_poll_async(self) -> None:
        if not getattr(self, "_evaluation_busy", False):
            return
        try:
            kind, value = self._evaluation_mailbox.get_nowait()
        except queue.Empty:
            self.root.after(25, self._eval_poll_async)
            return
        if kind == "error":
            self._eval_async_failed(value)
        else:
            self._eval_async_completed(self._evaluation_callback, value)

    def _eval_async_failed(self, error: Exception) -> None:
        self._evaluation_busy = False
        self.status.set(f"{self._et('required')}: {error}")
        self._eval_json({"status": "failed", "error": str(error)})

    def _eval_async_completed(self, callback: Any, result: Any) -> None:
        self._evaluation_busy = False
        try:
            callback(result)
        except Exception as error:
            self._eval_async_failed(error)
        else:
            self.status.set(self._et("completed"))

    def _choose_eval_directory(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title=self._et("general_project"))
        if path:
            self.var("eval_general_project").set(path)
            self._refresh_eval_general_runs()

    def _choose_eval_file(self, variable: str, *, synthetic: bool = False) -> None:
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Data", "*.csv *.xlsx *.xlsm")])
        if path:
            self.var(variable).set(path)
            if variable == "eval_real_file":
                self._refresh_participant_ids()

    def _refresh_eval_general_runs(self) -> None:
        path = self.var("eval_general_project").get().strip()
        self._eval_general_records = []
        if path:
            directory = Path(path).expanduser().resolve() / "experiments"
            self._eval_general_records = [
                record for record in
                (json.loads(item.read_text(encoding="utf-8")) for item in sorted(directory.glob("*.json")))
                if record.get("status") == "completed"
            ] if directory.is_dir() else []
        labels = [f"{record.get('name', record['id'])} · {record.get('phase', '?')} · {record['id']}"
                  for record in self._eval_general_records]
        if hasattr(self, "eval_run_combo"):
            self.eval_run_combo.configure(values=labels)
            selected = self.var("eval_freeze_run").get()
            index = next((i for i, item in enumerate(self._eval_general_records)
                          if item["id"] == selected), -1)
            if index >= 0:
                self.eval_run_combo.current(index)
            else:
                self.eval_run_combo.set("")

    def _select_eval_run(self, _event: Any = None) -> None:
        index = self.eval_run_combo.current()
        self.var("eval_freeze_run").set(
            self._eval_general_records[index]["id"] if index >= 0 else ""
        )

    def _select_input_designation(self, _event: Any = None) -> None:
        index = self.eval_input_designation_combo.current()
        values = ("unknown", "declared_synthetic", "declared_observed")
        if 0 <= index < len(values):
            self.var("eval_input_designation").set(values[index])

    def _freeze_selected(self) -> None:
        project = self.var("eval_general_project").get().strip()
        run_id = self.var("eval_freeze_run").get().strip()
        if not project or not run_id:
            raise ValueError(self._et("required"))
        allow = self.var("eval_allow_validation").get() == "1"
        self._eval_async(self._et("freeze"),
                         lambda: freeze_general(project, run_id, allow_validation_config=allow),
                         self._freeze_completed)

    def _freeze_completed(self, manifest: Mapping[str, Any]) -> None:
        artifact = Path(manifest["project_path"]) / "research" / "frozen" / manifest["artifact_id"]
        self.var("eval_artifact_path").set(str(artifact))
        self._eval_json(manifest)

    def _view_manifest(self) -> None:
        path = Path(self.var("eval_artifact_path").get()) / "manifest.json"
        if not path.is_file():
            raise ValueError(self._et("required"))
        self._eval_json(json.loads(path.read_text(encoding="utf-8")))

    def _evaluate_external(self) -> None:
        artifact = self.var("eval_artifact_path").get().strip()
        source = self.var("eval_external_file").get().strip()
        study = self.var("eval_study_label").get().strip()
        if not artifact or not source or not study:
            raise ValueError(self._et("required"))
        subgroup = self.var("eval_subgroup").get().strip() or None
        influenced = self.var("eval_selection_after").get() == "1"
        designation = self.var("eval_input_designation").get().strip() or "unknown"
        self._eval_async(self._et("evaluate"),
                         lambda: evaluate_external(artifact, source, study,
                                                   subgroup=subgroup,
                                                   selection_after_external=influenced,
                                                   input_designation=designation),
                         self._external_completed)

    def _external_completed(self, result: Mapping[str, Any]) -> None:
        self._eval_json(result)
        if hasattr(self, "external_metric_tree") and self.external_metric_tree.winfo_exists():
            self.external_metric_tree.delete(*self.external_metric_tree.get_children())
            for metric, value in result.get("metrics", {}).items():
                self.external_metric_tree.insert("", "end", values=(metric,
                    "—" if value is None else f"{value:.6g}", result.get("reasons", {}).get(metric, "")))
        detail = {key: result.get(key) for key in
                  ("n_source_rows", "n_evaluated", "calibration", "residual_summary", "subgroups",
                   "input_designation", "selection_note", "evaluation_metadata", "methods")}
        self.var("eval_external_details").set(json.dumps(detail, ensure_ascii=False, indent=2))
        if hasattr(self, "external_detail") and self.external_detail.winfo_exists():
            self.external_detail.configure(state="normal")
            self.external_detail.delete("1.0", "end")
            self.external_detail.insert("1.0", self.var("eval_external_details").get())
            self.external_detail.configure(state="disabled")
        self._draw_external(result)

    def _draw_external(self, result: Mapping[str, Any]) -> None:
        if not hasattr(self, "external_plot_host") or not self.external_plot_host.winfo_exists():
            return
        for child in self.external_plot_host.winfo_children():
            child.destroy()
        figure = Figure(figsize=(8, 3.8), dpi=100)
        axis = figure.add_subplot(111)
        calibration = result.get("calibration") or {}
        if calibration.get("available"):
            bins = [row for row in calibration["bins"] if row["n"]]
            axis.plot([row["mean_probability"] for row in bins],
                      [row["observed_rate"] for row in bins], marker="o", color="#0072B2")
            axis.plot([0, 1], [0, 1], linestyle="--", color="#666666")
            axis.set(xlabel=self._et("predicted_probability"), ylabel=self._et("observed_rate"))
        elif result.get("residual_summary"):
            residual = [float(row["y_true"]) - float(row["y_pred"])
                        for row in result.get("predictions", [])]
            axis.hist(residual, bins=min(20, max(5, int(np.sqrt(len(residual))))), color="#0072B2")
            axis.axvline(0, linestyle="--", color="#666666")
            axis.set(xlabel=self._et("residual_axis"), ylabel=self._et("count"))
        else:
            metrics = [(key, value) for key, value in result.get("metrics", {}).items()
                       if isinstance(value, (int, float))]
            axis.bar([key for key, _ in metrics], [value for _, value in metrics], color="#0072B2")
            axis.tick_params(axis="x", rotation=35)
        axis.set_title(self._et("external_results"))
        figure.tight_layout()
        self.eval_figure = figure
        self.eval_plot_canvas = ResearchCanvas(figure, master=self.external_plot_host)
        self.eval_plot_canvas.draw()
        self.eval_plot_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _platform_views(self) -> list[dict[str, Any]]:
        values = self.platform_runs()
        return list(values.values()) if isinstance(values, dict) else list(values)

    def _refresh_paired_runs(self) -> None:
        self._eval_views = self._platform_views()
        labels = [f"{view.get('name', view['run_id'])} · {view['engine']} · {view['run_id']}"
                  for view in self._eval_views]
        for side in ("left", "right"):
            combo = getattr(self, f"paired_{side}_combo", None)
            if combo:
                combo.configure(values=labels)
                selected = self.var(f"eval_paired_{side}_run").get()
                index = next((i for i, view in enumerate(self._eval_views)
                              if view["run_id"] == selected), -1)
                if index >= 0:
                    combo.current(index)
                else:
                    combo.set("")

    def _paired_run_selected(self, side: str) -> None:
        combo = getattr(self, f"paired_{side}_combo")
        index = combo.current()
        if index < 0:
            return
        view = self._eval_views[index]
        self.var(f"eval_paired_{side}_run").set(view["run_id"])
        models = sorted({str(item["model_id"]) for item in view.get("metrics", [])})
        model_combo = getattr(self, f"paired_{side}_model_combo")
        model_combo.configure(values=models)
        self.var(f"eval_paired_{side}_model").set(models[0] if len(models) == 1 else "")
        model_combo.set(self.var(f"eval_paired_{side}_model").get())

    def _view(self, run_id: str) -> dict[str, Any]:
        matches = [view for view in self._eval_views if view["run_id"] == run_id]
        if len(matches) != 1:
            raise ValueError("run selection is missing or ambiguous")
        verify_run(matches[0])
        return matches[0]

    def _paired_prediction_payload(self, view: Mapping[str, Any], model_id: str,
                                   metric: str, participant_field: str) -> tuple[dict[str, Any], dict[Any, Any]]:
        if not model_id:
            raise ValueError("an explicit model ID is required for each run")
        rows = list(view.get("predictions") or [])
        if view["engine"] == "general":
            configured = str(view.get("config", {}).get("model", ""))
            if model_id != configured:
                raise ValueError("selected model does not belong to the general run")
            if metric not in {"accuracy", "mae", "mse", "rmse"}:
                raise ValueError("general paired comparison requires accuracy, MAE, MSE, or RMSE")
            mapping = {row["row_id"]: row.get(participant_field) for row in rows
                       if participant_field in row}
            if not mapping and participant_field:
                data_path = Path(str(view.get("project_path", ""))) / "data.json"
                if data_path.is_file():
                    import pandas as pd
                    frame = pd.read_json(data_path, orient="table", precise_float=True)
                    if participant_field in frame.columns:
                        mapping = {row["row_id"]: frame.iloc[int(row["row_id"])][participant_field]
                                   for row in rows}
            return {"predictions": rows}, mapping
        selected = [row for row in rows if str(row.get("model_id")) == model_id]
        if metric not in {"onset_mae_min", "wake_mae_min"}:
            raise ValueError("SleepWell paired comparison requires onset_mae_min or wake_mae_min")
        prefix = "onset" if metric.startswith("onset") else "wake"
        truth_key = f"actual_{prefix}_offset_min"
        predicted_key = f"{prefix}_offset_min"
        converted, participants = [], {}
        for row in selected:
            if not all(isinstance(row.get(key), (int, float)) for key in (truth_key, predicted_key)):
                continue
            identifier = f"{row.get(participant_field, row.get('participant_id'))}|{row.get('cutoff')}"
            participant = row.get(participant_field, row.get("participant_id"))
            converted.append({"row_id": identifier, "y_true": row[truth_key],
                              "y_pred": row[predicted_key], "participant_id": participant})
            participants[identifier] = participant
        if not converted:
            raise ValueError("selected SleepWell model has no matching raw predictions")
        return {"predictions": converted}, participants

    def _run_paired(self) -> None:
        self._refresh_paired_runs()
        left = self._view(self.var("eval_paired_left_run").get())
        right = self._view(self.var("eval_paired_right_run").get())
        metric = self.var("eval_paired_metric").get()
        participant_field = self.var("eval_participant_field").get().strip() or "participant_id"
        left_payload, left_participants = self._paired_prediction_payload(
            left, self.var("eval_paired_left_model").get(), metric, participant_field)
        right_payload, right_participants = self._paired_prediction_payload(
            right, self.var("eval_paired_right_model").get(), metric, participant_field)
        resampling = self.var("eval_resampling").get()
        if resampling == "participant_cluster":
            if not left_participants or left_participants != right_participants or any(
                value in (None, "") for value in left_participants.values()
            ):
                raise ValueError("participant cluster comparison requires matching participant IDs")
            participants, ordered, block = left_participants, False, None
        elif resampling == "ordered_blocks":
            participants, ordered = None, True
            block = int(self.var("eval_block_length").get())
        else:
            raise ValueError("resampling unit must be explicit")
        engine_metric = "mae" if metric in {"onset_mae_min", "wake_mae_min"} else metric
        left_model = self.var("eval_paired_left_model").get()
        right_model = self.var("eval_paired_right_model").get()
        unit_id = self.var("eval_unit_id").get().strip() or "row_id"
        repeats = int(self.var("eval_bootstrap_repeats").get())
        seed = int(self.var("eval_bootstrap_seed").get())

        def operation() -> dict[str, Any]:
            result = paired_compare(
                left_payload, right_payload, metric=engine_metric,
                unit_id=unit_id,
                participant_ids=participants, ordered=ordered,
                bootstrap_repeats=repeats, seed=seed, block_length=block,
            )
            result.update(display_metric=metric, left_model_id=left_model, right_model_id=right_model)
            return result

        self._eval_async(self._et("compare"), operation, self._paired_completed)

    def _paired_completed(self, result: Mapping[str, Any]) -> None:
        self._eval_json(result)
        text = (f"{self._et('method')}: {result['method']}\n"
                f"{self._et('denominator')}: rows={result['n_paired_rows']}, "
                f"independent={result['n_independent_units']}\n"
                f"{self._et('ci')}: {result['delta_ci_95_percentile']}\n{result['method_notes']}")
        self.var("eval_paired_details").set(text)
        if hasattr(self, "paired_detail") and self.paired_detail.winfo_exists():
            self.paired_detail.configure(text=text)

    def build_advanced_evaluation(self, parent: tk.Misc) -> None:
        defaults = {"eval_general_project": self.var("linked_general").get(),
                    "eval_allow_validation": "0", "eval_selection_after": "0",
                    "eval_input_designation": "unknown",
                    "eval_study_label": "", "eval_subgroup": "", "eval_external_file": "",
                    "eval_artifact_path": "", "eval_paired_metric": "mae", "eval_unit_id": "row_id",
                    "eval_resampling": "participant_cluster", "eval_participant_field": "participant_id",
                    "eval_block_length": "5", "eval_bootstrap_repeats": "1000", "eval_bootstrap_seed": "42"}
        for key, value in defaults.items():
            if not self.var(key).get():
                self.var(key).set(value)
        ttk.Label(parent, text=self._et("advanced_title"), style="AcademicTitle.TLabel").pack(fill="x")
        ttk.Label(parent, text=self._et("advanced_help"), wraplength=1100, justify="left").pack(fill="x", pady=5)
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("general_project"), width=29).pack(side="left")
        ttk.Entry(row, textvariable=self.var("eval_general_project")).pack(side="left", fill="x", expand=True)
        self._eval_button(row, "browse_project", self._choose_eval_directory)
        self._eval_button(row, "refresh_runs", self._refresh_eval_general_runs)
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("chosen_run"), width=29).pack(side="left")
        self.eval_run_combo = ttk.Combobox(row, state="readonly", width=78)
        self.eval_run_combo.pack(side="left", fill="x", expand=True)
        self.eval_run_combo.bind("<<ComboboxSelected>>", self._select_eval_run)
        ttk.Checkbutton(parent, text=self._et("allow_validation"), variable=self.var("eval_allow_validation"),
                        onvalue="1", offvalue="0").pack(anchor="w", pady=3)
        ttk.Label(parent, text=self._et("validation_warning"), wraplength=1100).pack(fill="x")
        row = ttk.Frame(parent); row.pack(fill="x")
        self._eval_button(row, "freeze", self._freeze_selected)
        self._eval_button(row, "open_manifest", self._view_manifest)
        self._eval_row(parent, "artifact", "eval_artifact_path")
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("external_file"), width=29).pack(side="left")
        ttk.Entry(row, textvariable=self.var("eval_external_file")).pack(side="left", fill="x", expand=True)
        self._eval_button(row, "choose_file", lambda: self._choose_eval_file("eval_external_file"))
        self._eval_row(parent, "study_label", "eval_study_label")
        self._eval_row(parent, "subgroup", "eval_subgroup")
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("input_designation"), width=29).pack(side="left")
        designation_values = [self._et(key) for key in
                              ("designation_unknown", "designation_synthetic", "designation_observed")]
        self.eval_input_designation_combo = ttk.Combobox(
            row, state="readonly", width=42, values=designation_values,
        )
        self.eval_input_designation_combo.pack(side="left")
        designation_codes = ("unknown", "declared_synthetic", "declared_observed")
        selected_designation = self.var("eval_input_designation").get()
        selected_index = (designation_codes.index(selected_designation)
                          if selected_designation in designation_codes else 0)
        self.eval_input_designation_combo.current(selected_index)
        self.var("eval_input_designation").set(designation_codes[selected_index])
        self.eval_input_designation_combo.bind("<<ComboboxSelected>>", self._select_input_designation)
        ttk.Label(row, text=self._et("input_designation_help"), wraplength=600).pack(
            side="left", padx=(8, 0), fill="x", expand=True)
        ttk.Checkbutton(parent, text=self._et("selection_after"), variable=self.var("eval_selection_after"),
                        onvalue="1", offvalue="0").pack(anchor="w", pady=3)
        ttk.Label(parent, text=self._et("external_warning"), wraplength=1100).pack(fill="x")
        row = ttk.Frame(parent); row.pack(fill="x")
        self._eval_button(row, "evaluate", self._evaluate_external)
        self.external_metric_tree = ttk.Treeview(parent, columns=("metric", "value", "reason"),
                                                 show="headings", height=7)
        for column, key, width in (("metric", "metric", 190), ("value", "value", 130),
                                   ("reason", "reason", 420)):
            self.external_metric_tree.heading(column, text=self._et(key))
            self.external_metric_tree.column(column, width=width)
        self.external_metric_tree.pack(fill="x", pady=5)
        self.external_detail = tk.Text(parent, height=8, wrap="word")
        self.external_detail.pack(fill="x")
        self.external_detail.configure(state="disabled")
        self.external_plot_host = ttk.Frame(parent)
        self.external_plot_host.pack(fill="both", expand=True, pady=5)
        ttk.Separator(parent).pack(fill="x", pady=9)
        ttk.Label(parent, text=self._et("paired_title"), style="AcademicTitle.TLabel").pack(fill="x")
        ttk.Label(parent, text=self._et("paired_help"), wraplength=1100, justify="left").pack(fill="x")
        for side, label in (("left", "left_run"), ("right", "right_run")):
            row = ttk.Frame(parent); row.pack(fill="x", pady=3)
            ttk.Label(row, text=self._et(label), width=29).pack(side="left")
            combo = ttk.Combobox(row, state="readonly", width=60)
            combo.pack(side="left", fill="x", expand=True)
            combo.bind("<<ComboboxSelected>>", lambda _event, s=side: self._paired_run_selected(s))
            setattr(self, f"paired_{side}_combo", combo)
            model_combo = ttk.Combobox(row, textvariable=self.var(f"eval_paired_{side}_model"),
                                       state="readonly", width=24)
            model_combo.pack(side="left", padx=(6, 0))
            setattr(self, f"paired_{side}_model_combo", model_combo)
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("paired_metric"), width=29).pack(side="left")
        ttk.Combobox(row, textvariable=self.var("eval_paired_metric"), state="readonly",
                     values=("accuracy", "mae", "mse", "rmse", "onset_mae_min", "wake_mae_min"),
                     width=24).pack(side="left")
        ttk.Label(row, text=self._et("resampling"), width=23).pack(side="left", padx=(12, 0))
        ttk.Combobox(row, textvariable=self.var("eval_resampling"), state="readonly",
                     values=("participant_cluster", "ordered_blocks"), width=25).pack(side="left")
        for key, variable in (("unit_id", "eval_unit_id"), ("participant_field", "eval_participant_field"),
                              ("block_length", "eval_block_length"),
                              ("bootstrap_repeats", "eval_bootstrap_repeats"),
                              ("seed", "eval_bootstrap_seed")):
            self._eval_row(parent, key, variable)
        row = ttk.Frame(parent); row.pack(fill="x")
        self._eval_button(row, "compare", self._run_paired)
        self.paired_detail = ttk.Label(parent, textvariable=self.var("eval_paired_details"),
                                       wraplength=1100, justify="left")
        self.paired_detail.pack(fill="x", pady=5)
        ttk.Label(parent, text=self._et("result_json")).pack(anchor="w")
        self.evaluation_json = tk.Text(parent, height=12, wrap="none")
        self.evaluation_json.pack(fill="both", expand=True)
        self.evaluation_json.insert("1.0", self.var("evaluation_result_json").get())
        self.evaluation_json.configure(state="disabled")
        self._refresh_eval_general_runs()
        self._refresh_paired_runs()

    def _participant_service(self) -> ParticipantProspective:
        return ParticipantProspective(Path(self.project.path) / "research" / "prospective_participants")

    def _refresh_participant_ids(self) -> None:
        path = self.var("eval_real_file").get().strip()
        values: list[str] = []
        if path:
            frame = load_data(path)
            if "participant_id" in frame.columns:
                values = sorted(str(value) for value in frame["participant_id"].dropna().unique())
        if hasattr(self, "participant_id_combo"):
            self.participant_id_combo.configure(values=values)
        if len(values) == 1:
            self.var("eval_participant_id").set(values[0])

    def _profile(self) -> dict[str, Any]:
        text = self.var("profile_json").get().strip()
        return json.loads(text) if text else {}

    def _run_synthetic_participants(self) -> None:
        source = self.var("eval_synthetic_file").get().strip()
        if not source:
            raise ValueError(self._et("required"))
        config = {"seed": int(self.var("eval_synthetic_seed").get()), "profile": self._profile()}
        self._eval_async(self._et("run_synthetic"),
                         lambda: run_multi_participant_synthetic(source, config),
                         self._participant_completed)

    def _commit_participant(self) -> None:
        participant = self.var("eval_participant_id").get().strip()
        if not participant:
            raise ValueError(self._et("required"))
        config: dict[str, Any] = {"cutoff": self.var("eval_participant_cutoff").get(),
                                  "profile": self._profile()}
        source = self.var("eval_real_file").get().strip()
        if source:
            config["csv_file"] = source
        def operation() -> dict[str, Any]:
            service = self._participant_service()
            return {"operation": service.commit(participant, config),
                    "summary": service.summary(participant)}
        self._eval_async(self._et("commit"), operation, self._participant_operation_completed)

    def _record_participant_outcome(self) -> None:
        participant = self.var("eval_participant_id").get().strip()
        forecast_id = self.var("eval_forecast_id").get().strip()
        if not participant or not forecast_id:
            raise ValueError(self._et("required"))
        outcome = {"sleep_start": self.var("eval_sleep_start").get(),
                   "wake": self.var("eval_wake").get(),
                   "observed_at": self.var("eval_observed_at").get(),
                   "source": self.var("eval_outcome_source").get()}
        def operation() -> dict[str, Any]:
            service = self._participant_service()
            return {"operation": service.record_outcome(participant, forecast_id, outcome),
                    "summary": service.summary(participant)}
        self._eval_async(self._et("record_outcome"), operation, self._participant_operation_completed)

    def _participant_operation_completed(self, result: Mapping[str, Any]) -> None:
        self._eval_json(result)
        operation = result.get("operation", {}).get("result", {})
        raw = operation.get("raw", {})
        forecast = raw.get("forecast") or {}
        assessment = raw.get("assessment") or {}
        event = forecast or assessment
        self.var("eval_participant_event").set(json.dumps({
            "participant_key": result.get("operation", {}).get("participant_key"),
            "forecast_id": forecast.get("id") or assessment.get("forecast_id"),
            "generated_at": forecast.get("generated_at"),
            "observed_at": assessment.get("observed_at"),
            "eligible": raw.get("eligible"),
        }, ensure_ascii=False))
        self._participant_completed(result["summary"], update_json=False)

    def _participant_completed(self, result: Mapping[str, Any], *, update_json: bool = True) -> None:
        if update_json:
            self._eval_json(result)
        if not hasattr(self, "participant_tree") or not self.participant_tree.winfo_exists():
            return
        self.participant_tree.delete(*self.participant_tree.get_children())
        for row in result.get("summary", []):
            self.participant_tree.insert("", "end", values=(
                ", ".join(row.get("participant_keys", [])) or "—", row.get("model_id", "—"),
                "—" if row.get("joint_mae_min") is None else f"{row['joint_mae_min']:.3f}",
                row.get("n_nights", 0), row.get("n_participants", 0), row.get("source_stratum", "—")))
        missing = result.get("missing_or_unscored_participants", [])
        self.var("eval_participant_details").set(
            f"{self._et('n_participants')}: {result.get('participants_total', 0)} · "
            f"{self._et('missing_dropout')}: {len(missing)} ({', '.join(missing)}) · "
            f"{self._et('source')}: {result.get('source_kind', '')} · "
            f"event: {self.var('eval_participant_event').get()}"
        )

    def build_participant_evaluation(self, parent: tk.Misc) -> None:
        defaults = {"eval_synthetic_file": "", "eval_synthetic_seed": "42", "eval_real_file": "",
                    "eval_participant_id": "", "eval_participant_cutoff": "",
                    "eval_forecast_id": "", "eval_sleep_start": "", "eval_wake": "",
                    "eval_observed_at": "", "eval_outcome_source": "diary"}
        for key, value in defaults.items():
            if not self.var(key).get():
                self.var(key).set(value)
        ttk.Label(parent, text=self._et("participant_title"), style="AcademicTitle.TLabel").pack(fill="x")
        ttk.Label(parent, text=self._et("participant_help"), wraplength=1100, justify="left").pack(fill="x", pady=5)
        ttk.Label(parent, text=self._et("synthetic_section"), style="AcademicTitle.TLabel").pack(fill="x", pady=(8, 2))
        ttk.Label(parent, text=self._et("synthetic_badge")).pack(anchor="w")
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("synthetic_file"), width=29).pack(side="left")
        ttk.Entry(row, textvariable=self.var("eval_synthetic_file")).pack(side="left", fill="x", expand=True)
        self._eval_button(row, "choose_file", lambda: self._choose_eval_file("eval_synthetic_file", synthetic=True))
        self._eval_row(parent, "seed", "eval_synthetic_seed")
        row = ttk.Frame(parent); row.pack(fill="x")
        self._eval_button(row, "run_synthetic", self._run_synthetic_participants)
        ttk.Separator(parent).pack(fill="x", pady=9)
        ttk.Label(parent, text=self._et("real_section"), style="AcademicTitle.TLabel").pack(fill="x")
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("real_file"), width=29).pack(side="left")
        ttk.Entry(row, textvariable=self.var("eval_real_file")).pack(side="left", fill="x", expand=True)
        self._eval_button(row, "choose_file", lambda: self._choose_eval_file("eval_real_file"))
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=self._et("participant_id"), width=29).pack(side="left")
        self.participant_id_combo = ttk.Combobox(row, textvariable=self.var("eval_participant_id"), width=55)
        self.participant_id_combo.pack(side="left", fill="x", expand=True)
        self._eval_row(parent, "cutoff", "eval_participant_cutoff")
        row = ttk.Frame(parent); row.pack(fill="x")
        self._eval_button(row, "commit", self._commit_participant)
        for key, variable in (("forecast_id", "eval_forecast_id"), ("sleep_start", "eval_sleep_start"),
                              ("wake", "eval_wake"), ("observed_at", "eval_observed_at"),
                              ("outcome_source", "eval_outcome_source")):
            self._eval_row(parent, key, variable)
        row = ttk.Frame(parent); row.pack(fill="x")
        self._eval_button(row, "record_outcome", self._record_participant_outcome)
        self.participant_tree = ttk.Treeview(parent, columns=("participant", "model", "joint", "n", "people", "source"),
                                             show="headings", height=8)
        for column, key, width in (("participant", "participant", 220), ("model", "model", 180),
                                   ("joint", "value", 100), ("n", "n_nights", 90),
                                   ("people", "n_participants", 100), ("source", "source", 190)):
            self.participant_tree.heading(column, text=self._et(key))
            self.participant_tree.column(column, width=width)
        self.participant_tree.pack(fill="x", pady=6)
        ttk.Label(parent, textvariable=self.var("eval_participant_details"),
                  wraplength=1100, justify="left").pack(fill="x")
        ttk.Label(parent, text=self._et("result_json")).pack(anchor="w", pady=(6, 0))
        self.evaluation_json = tk.Text(parent, height=12, wrap="none")
        self.evaluation_json.pack(fill="both", expand=True)
        self.evaluation_json.insert("1.0", self.var("evaluation_result_json").get())
        self.evaluation_json.configure(state="disabled")
        self._refresh_participant_ids()
