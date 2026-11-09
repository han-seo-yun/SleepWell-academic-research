from __future__ import annotations

import copy
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from researchbench.app import App, PAGES
from researchbench.data import fingerprint, load_data
from researchbench.engine import make_split_plan, run_experiment
from researchbench.storage import Project


ROOT = Path(__file__).resolve().parents[1]


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _task_config(df, task: str) -> dict:
    classification = task == "classification"
    return {
        "id": f"gui-{task}-baseline",
        "name": "GUI baseline",
        "task": task,
        "target": "clinical_response" if classification else "outcome_score",
        "features": ["age", "biomarker", "treatment", "site"],
        "model": "linear",
        "params": {"max_iter": 300},
        "imputer": "median",
        "encoding": "onehot",
        "scaling": "standard",
        "positive_class": "responder" if classification else None,
        "threshold": 0.5,
        "average": "macro",
        "class_weight": None,
        "target_unit": None if classification else "score",
        "target_transform": "none",
        "device": "cpu",
        "primary_metric": "roc_auc" if classification else "rmse",
        "data_hash": fingerprint(df),
        "seed": 31,
        "max_seconds": 30,
        "split": "random",
        "test_size": 0.2,
        "folds": 3,
    }


class TkApplicationSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.callback_errors: list[tuple[type[BaseException], BaseException]] = []
        self.root.report_callback_exception = (
            lambda exc_type, exc, traceback: self.callback_errors.append((exc_type, exc))
        )
        self.error_patch = patch("researchbench.app.messagebox.showerror")
        self.view_error_patch = patch("researchbench.views.messagebox.showerror")
        self.error_patch.start()
        self.view_error_patch.start()
        self.app = App(self.root, language="ko", preferences=False)
        self._attach_dual_project()

    def tearDown(self) -> None:
        try:
            if self.root.winfo_exists():
                for callback_id in self.root.tk.call("after", "info"):
                    self.root.after_cancel(callback_id)
                self.app.close()
        finally:
            self.view_error_patch.stop()
            self.error_patch.stop()
            self.temp.cleanup()

    def _attach_dual_project(self) -> None:
        df = load_data(ROOT / "samples" / "dual_tasks.csv")
        project = Project.create(Path(self.temp.name) / "gui.rwb", df, "GUI dual project")
        roles = {column: "excluded" for column in df.columns}
        roles.update({feature: "feature" for feature in ["age", "biomarker", "treatment", "site"]})
        roles.update(
            {
                "participant_id": "id",
                "group_id": "group",
                "visit_date": "time",
                "clinical_response": "target",
                "outcome_score": "target",
            }
        )
        tasks = {}
        plans = {}
        baselines = {}
        for task in ("classification", "regression"):
            config = _task_config(df, task)
            plan = make_split_plan(df, config)
            result = run_experiment(df, config, plan)
            project.save_experiment(result)
            tasks[task] = config
            plans[task] = plan
            baselines[task] = result["id"]
        project.state.update(
            roles=roles,
            tasks=tasks,
            splits=plans,
            baselines=baselines,
            final_locks={},
            ui={"language": "ko", "task": "classification", "drafts": {}},
        )
        project.save()
        self.app.attach_project(project)
        self.root.update_idletasks()
        self.root.update()

    def _assert_no_callback_errors(self) -> None:
        if self.callback_errors:
            exc_type, exc = self.callback_errors[0]
            self.fail(f"Tk callback failed: {exc_type.__name__}: {exc}")

    def _wizard_field(self, window, label_text: str):
        for container in _descendants(window):
            children = container.winfo_children()
            if len(children) < 2:
                continue
            if children[0].winfo_class() in {"TLabel", "Label"} and children[0].cget("text") == label_text:
                return children[1]
        raise AssertionError(f"wizard field not found: {label_text}")

    def _run_wizard_case(self, filename: str, target: str, task: str) -> None:
        source = ROOT / "samples" / filename
        frame = load_data(source)
        output = Path(self.temp.name) / f"wizard-{task}"
        output.mkdir()
        self.app.wizard(frame, str(source))
        self.root.update_idletasks()
        window = next(child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel))

        target_widget = self._wizard_field(window, self.app.t("target"))
        target_widget.set(target)
        target_widget.event_generate("<<ComboboxSelected>>")
        task_widget = self._wizard_field(window, self.app.t("task"))
        task_widget.current(["classification", "regression", "both"].index(task))
        task_widget.event_generate("<<ComboboxSelected>>")
        self.root.update()

        create_button = next(
            widget
            for widget in _descendants(window)
            if widget.winfo_class() == "TButton" and widget.cget("text") == self.app.t("create_run")
        )
        with patch("researchbench.app.filedialog.askdirectory", return_value=str(output)) as dialog:
            create_button.invoke()
        self.root.update()
        dialog.assert_called_once()
        self.assertFalse(window.winfo_exists())
        self.assertIn(task, self.app.project.state["tasks"])
        self.assertIn(task, self.app.project.state["splits"])
        self.assertEqual(self.app.project.state["tasks"][task]["target"], target)
        self.assertTrue(self.app.runner.busy)

        state = {"timed_out": False}

        def until_complete() -> None:
            if not self.app.runner.busy:
                self.root.quit()
                return
            self.root.after(25, until_complete)

        def timeout() -> None:
            state["timed_out"] = True
            self.root.quit()

        self.root.after(0, until_complete)
        self.root.after(20_000, timeout)
        self.root.mainloop()
        self.assertFalse(state["timed_out"])
        completed = [r for r in self.app.runner.records.values() if r["status"] == "completed"]
        self.assertEqual(len(completed), 1, self.app.runner.records)
        self.assertEqual(completed[0]["task"], task)
        if task == "classification":
            self.assertIsNotNone(completed[0]["config"]["positive_class"])
        self._assert_no_callback_errors()

    def test_all_pages_languages_scaling_and_draft_persistence(self) -> None:
        for geometry in ("900x650", "1440x940"):
            self.root.geometry(geometry)
            for scaling in (1.0, 1.5, 2.0):
                self.root.tk.call("tk", "scaling", scaling)
                for page in PAGES:
                    self.app.navigate(page)
                    self.root.update_idletasks()
                    self.root.update()
                    self.assertEqual(self.app.current_page, page)
                    self.assertTrue(self.app.pages[page].winfo_exists())

        self.app.var("task").set("classification")
        self.app.var("name").set("작성 중 분류 실험")
        self.app.selected_features = {"age", "treatment"}
        self.app.save_draft()
        self.app.var("task").set("regression")
        self.app.switch_task()
        self.app.var("name").set("draft regression")
        self.app.selected_features = {"age", "biomarker"}
        self.app.save_draft()
        self.app.var("task").set("classification")
        self.app.switch_task()
        self.assertEqual(self.app.var("name").get(), "작성 중 분류 실험")
        self.assertEqual(self.app.selected_features, {"age", "treatment"})

        config_before_language_change = self.app.config()
        self.app.change_language("en")
        self.assertEqual(self.app.lang, "en")
        self.assertEqual(self.app.var("name").get(), "작성 중 분류 실험")
        self.assertEqual(self.app.selected_features, {"age", "treatment"})
        self.assertEqual(self.app.config(), config_before_language_change)
        self.app.change_language("ko")
        self.assertEqual(self.app.lang, "ko")
        self.assertEqual(self.app.config(), config_before_language_change)
        self._assert_no_callback_errors()

    def test_deiconified_window_has_usable_visible_controls_at_tk_scalings(self) -> None:
        defects = []
        self.root.deiconify()
        for geometry in ("900x650", "1440x940"):
            for scaling in (1.0, 1.5, 2.0):
                self.root.tk.call("tk", "scaling", scaling)
                self.root.geometry(geometry)
                self.app.build()
                for page in PAGES:
                    self.app.navigate(page)
                    self.root.update_idletasks()
                    self.root.update()
                    if self.root.winfo_width() < 850 or self.root.winfo_height() < 600:
                        defects.append((geometry, scaling, page, "root_below_minimum"))
                    frame = self.app.pages[page]
                    bbox = frame.canvas.bbox("all")
                    if bbox is None or bbox[2] <= 0 or bbox[3] <= 0:
                        defects.append((geometry, scaling, page, "empty_scroll_region"))
                    for widget in _descendants(frame):
                        if not widget.winfo_ismapped():
                            continue
                        width = widget.winfo_width()
                        if widget.winfo_class() == "TButton" and width < 35:
                            defects.append((geometry, scaling, page, "button_too_narrow", widget.cget("text"), width))
                        if widget.winfo_class() == "TLabel" and widget.cget("text") and width < 12:
                            defects.append((geometry, scaling, page, "label_too_narrow", widget.cget("text"), width))
        self.root.withdraw()
        self.assertFalse(defects, defects[:10])
        self._assert_no_callback_errors()

    def test_compare_clone_final_lock_and_dialog_exports(self) -> None:
        self.app.navigate("compare")
        self.app.refresh_results()
        result_id = self.app.baseline_id()
        self.assertTrue(self.app.result_tree.exists(result_id))
        self.app.result_tree.selection_set(result_id)
        self.app.selected_results = {result_id}

        self.app._clone_result()
        self.assertEqual(self.app.current_page, "design")
        self.assertIn(self.app.t("clone"), self.app.var("name").get())
        self.assertEqual(self.app.selected_features, {"age", "biomarker", "treatment", "site"})

        self.app.navigate("compare")
        self.app.refresh_results()
        self.app.result_tree.selection_set(result_id)
        self.app.selected_results = {result_id}
        with patch("researchbench.views.messagebox.askyesno", return_value=True):
            self.app._evaluate_final()
        self.assertIn("classification", self.app.project.state["final_locks"])
        self.assertEqual(
            self.app.project.state["final_locks"]["classification"]["experiment_id"], result_id
        )
        self.assertTrue(self.app.runner.pending)

        # Cancel the queued final job so export testing remains deterministic.
        final_job_id = self.app.runner.pending[0][0]["id"]
        self.app.runner.cancel(final_job_id)
        self.app.navigate("export")
        self.app.var("phase").set("validation")
        self.app.selected_results = {result_id}
        self.app._refresh_export_plot()
        image_path = Path(self.temp.name) / "gui_export.png"
        table_path = Path(self.temp.name) / "gui_export.xlsx"
        with patch(
            "researchbench.views.filedialog.asksaveasfilename",
            side_effect=[str(image_path), str(table_path)],
        ):
            self.app._export_current_figure()
            self.app._export_current_table()
        self.assertGreater(image_path.stat().st_size, 500)
        self.assertGreater(table_path.stat().st_size, 100)
        self._assert_no_callback_errors()

    def test_delta_plot_pins_baseline_and_result_filter_limits_selection(self) -> None:
        baseline_id = self.app.baseline_id()
        variant_config = copy.deepcopy(self.app.project.state["tasks"]["classification"])
        variant_config.update(
            id="gui-classification-drop-biomarker",
            name="Biomarker removed variant",
            features=["age", "treatment", "site"],
        )
        variant = run_experiment(
            self.app.project.df,
            variant_config,
            self.app.project.state["splits"]["classification"],
        )
        self.app.project.save_experiment(variant)

        self.app.navigate("compare")
        self.app.refresh_results()
        self.app.result_tree.selection_set(variant["id"])
        self.app.selected_results = {variant["id"]}
        self.app.var("result_kind").set("delta")
        figure = self.app._result_figure()
        self.assertIsNotNone(figure)
        labels = {
            tick.get_text()
            for axis in figure.axes
            for tick in (*axis.get_xticklabels(), *axis.get_yticklabels())
            if tick.get_text()
        }
        self.assertTrue(any("GUI baseline" in label for label in labels), labels)
        self.assertTrue(any("Biomarker removed variant" in label for label in labels), labels)
        self.assertEqual(set(self.app.result_tree.selection()), {variant["id"]})

        self.app.var("result_filter").set("removed variant")
        self.app.refresh_results()
        self.assertEqual([result["id"] for result in self.app.chosen_results()], [variant["id"]])
        self.assertEqual(set(self.app.result_tree.get_children()), {variant["id"]})

        self.app.result_tree.selection_remove(*self.app.result_tree.selection())
        self.app.selected_results.clear()
        self.app.var("result_filter").set("GUI baseline")
        self.app.refresh_results()
        self.assertEqual([result["id"] for result in self.app.chosen_results()], [baseline_id])
        self.assertEqual(set(self.app.result_tree.get_children()), {baseline_id})
        self._assert_no_callback_errors()

    def test_mainloop_heartbeat_during_mlp_and_cancel(self) -> None:
        self.root.deiconify()
        self.root.geometry("1100x760")
        self.app.var("task").set("regression")
        self.app.switch_task()
        self.app.var("model").set("mlp")
        self.app.var("name").set("mainloop cancellation")
        self.app.var("params").set(json.dumps({"epochs": 500, "hidden_layer_sizes": [128, 64], "batch_size": 16}))
        self.app.selected_features = {"age", "biomarker", "treatment", "site"}
        self.app.run()
        self.root.update_idletasks()
        self.root.update()

        state = {"heartbeats": 0, "cancelled_id": None, "timed_out": False, "saw_epoch": False, "saw_live_plot": False, "live_size": None}

        def heartbeat() -> None:
            state["heartbeats"] += 1
            if self.app.runner.active:
                active_record = self.app.runner.active["record"]
                progress = active_record.get("progress", {})
                if active_record.get("history") or int(progress.get("epoch", 0) or 0) >= 1:
                    state["saw_epoch"] = True
                if state["saw_epoch"] and "live" in self.app.plot_figures:
                    canvas = self.app.plot_canvases["live"].get_tk_widget()
                    self.root.update_idletasks()
                    state["live_size"] = (canvas.winfo_width(), canvas.winfo_height())
                    state["saw_live_plot"] = min(state["live_size"]) >= 50
                if state["saw_live_plot"] and state["cancelled_id"] is None:
                    experiment_id = active_record["id"]
                    state["cancelled_id"] = experiment_id
                    self.app.runner.cancel(experiment_id)
            if state["cancelled_id"] and not self.app.runner.busy:
                self.root.quit()
                return
            self.root.after(25, heartbeat)

        def timeout() -> None:
            state["timed_out"] = True
            self.root.quit()

        self.root.after(0, heartbeat)
        self.root.after(20_000, timeout)
        self.root.mainloop()
        self.assertFalse(state["timed_out"])
        self.assertGreater(state["heartbeats"], 2)
        self.assertTrue(state["saw_epoch"])
        self.assertTrue(state["saw_live_plot"], state["live_size"])
        self.assertIsNotNone(state["cancelled_id"])
        record = self.app.runner.records[state["cancelled_id"]]
        self.assertEqual(record["status"], "cancelled")
        self.assertTrue(record.get("history"))
        self.assertIsNone(self.app.runner.active)
        self._assert_no_callback_errors()

    def test_classification_wizard_creates_and_runs_project(self) -> None:
        self._run_wizard_case("classification_mixed.csv", "response", "classification")

    def test_regression_wizard_creates_and_runs_project(self) -> None:
        self._run_wizard_case("regression_mixed.xlsx", "outcome_score", "regression")


if __name__ == "__main__":
    unittest.main()
