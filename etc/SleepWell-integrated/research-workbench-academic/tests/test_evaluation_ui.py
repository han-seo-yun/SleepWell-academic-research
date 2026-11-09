from __future__ import annotations

import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from researchbench.engine import evaluate_final, make_split_plan
from researchbench.evaluation_ui import EvaluationUIMixin
from researchbench.result_adapter import load_runs
from researchbench.storage import Project


class _Host(EvaluationUIMixin):
    def __init__(self, root, project_path: Path, views=None):
        self.root = root
        self.lang = "ko"
        self.vars = {}
        self.status = tk.StringVar(root)
        self.project = SimpleNamespace(path=project_path)
        self._views = views or []

    def var(self, key):
        if key not in self.vars:
            self.vars[key] = tk.StringVar(self.root)
        return self.vars[key]

    def safe(self, function):
        return function()

    def platform_runs(self):
        return self._views


class EvaluationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk unavailable: {error}")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "root"):
            cls.root.destroy()

    def setUp(self):
        for child in self.root.winfo_children():
            child.destroy()

    def _pump(self, host, timeout=8):
        deadline = time.monotonic() + timeout
        while getattr(host, "_evaluation_busy", False) and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(getattr(host, "_evaluation_busy", False), host.status.get())

    def _general_project(self, root: Path) -> tuple[Path, pd.DataFrame]:
        rng = np.random.default_rng(5)
        frame = pd.DataFrame({"x": rng.normal(size=72),
                              "participant_id": [f"p{i // 6}" for i in range(72)]})
        frame["target"] = (frame["x"] > 0).astype(int)
        path = root / "general"
        project = Project.create(path, frame, "ui-general")
        for index, regularization in enumerate((1.0, .5), start=1):
            config = {"id": f"final-{index}", "name": f"Final {index}",
                      "task": "classification", "target": "target", "features": ["x"],
                      "model": "linear", "split": "group", "group": "participant_id",
                      "params": {"C": regularization},
                      "folds": 3, "test_size": .2, "seed": 3, "imputer": "median",
                      "encoding": "onehot", "scaling": "standard", "positive_class": 1,
                      "threshold": .5, "average": "macro", "data_hash": project.state["data_hash"]}
            plan = make_split_plan(frame, config)
            project.state["splits"][plan["id"]] = plan
            project.save()
            project.save_experiment(evaluate_final(frame, config, plan))
        project.close()
        return path, frame

    def test_advanced_frame_freeze_external_and_paired_flow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path, frame = self._general_project(root)
            host = _Host(self.root, root / "academic.swa", load_runs(project_path))
            host.var("linked_general").set(str(project_path))
            parent = ttk.Frame(self.root)
            parent.pack(fill="both", expand=True)
            host.build_advanced_evaluation(parent)
            self.assertEqual(len(host._eval_general_records), 2)
            self.assertEqual(host.var("eval_input_designation").get(), "unknown")
            self.assertEqual(host.eval_input_designation_combo.get(), "확인되지 않음 (기본값)")
            host.eval_run_combo.current(0)
            host._select_eval_run()
            host._freeze_selected()
            self._pump(host)
            artifact = Path(host.var("eval_artifact_path").get())
            self.assertTrue((artifact / "manifest.json").is_file())
            external = root / "external.csv"
            frame.to_csv(external, index=False)
            host.var("eval_external_file").set(str(external))
            host.var("eval_study_label").set("ui external")
            host.var("eval_subgroup").set("participant_id")
            host.eval_input_designation_combo.current(1)
            host._select_input_designation()
            host._evaluate_external()
            self._pump(host)
            result = json.loads(host.var("evaluation_result_json").get())
            self.assertEqual(result["kind"], "frozen_external_evaluation")
            self.assertEqual(result["input_designation"], "declared_synthetic")
            self.assertGreater(len(host.external_metric_tree.get_children()), 0)

            left, right = host._views
            host.var("eval_paired_left_run").set(left["run_id"])
            host.var("eval_paired_right_run").set(right["run_id"])
            host.var("eval_paired_left_model").set("linear")
            host.var("eval_paired_right_model").set("linear")
            host.var("eval_paired_metric").set("accuracy")
            host.var("eval_resampling").set("participant_cluster")
            host.var("eval_bootstrap_repeats").set("100")
            host._run_paired()
            self._pump(host)
            paired = json.loads(host.var("evaluation_result_json").get())
            self.assertEqual(paired["method"], "paired_participant_cluster_bootstrap")
            self.assertGreater(paired["n_independent_units"], 1)

    def test_participant_frame_runs_declared_synthetic_flow_without_mixing_real_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _Host(self.root, root / "academic.swa")
            host.var("profile_json").set("{}")
            source = root / "synthetic.csv"
            source.write_text("participant_id,cutoff\np1,2026-01-01T20:00:00+09:00\n", encoding="utf-8")
            parent = ttk.Frame(self.root)
            parent.pack(fill="both", expand=True)
            host.build_participant_evaluation(parent)
            host.var("eval_synthetic_file").set(str(source))
            response = {"source_kind": "declared_synthetic_multi_participant_playback",
                        "participants_total": 1, "missing_or_unscored_participants": [],
                        "summary": [{"participant_keys": ["participant_hash"], "model_id": "fusion",
                                     "joint_mae_min": 5.0, "n_nights": 1,
                                     "n_participants": 1, "source_stratum": "diary"}]}
            with patch("researchbench.evaluation_ui.run_multi_participant_synthetic",
                       return_value=response) as mocked:
                host._run_synthetic_participants()
                self._pump(host)
            mocked.assert_called_once()
            self.assertEqual(len(host.participant_tree.get_children()), 1)
            self.assertIn("declared_synthetic", host.var("eval_participant_details").get())
            self.assertEqual(host.var("eval_real_file").get(), "")

    def test_language_switch_rebuild_preserves_drafts(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = _Host(self.root, Path(temporary))
            host.var("linked_general").set("")
            host.var("eval_study_label").set("사용자 연구명")
            host.var("eval_input_designation").set("declared_synthetic")
            parent = ttk.Frame(self.root); parent.pack()
            host.build_advanced_evaluation(parent)
            parent.destroy()
            host.lang = "en"
            parent = ttk.Frame(self.root); parent.pack()
            host.build_advanced_evaluation(parent)
            self.assertEqual(host.var("eval_study_label").get(), "사용자 연구명")
            self.assertEqual(host.var("eval_input_designation").get(), "declared_synthetic")
            self.assertEqual(host.eval_input_designation_combo.get(), "User-declared synthetic")
            self.assertEqual(host._et("advanced_title"), "Frozen model & external evaluation")


if __name__ == "__main__":
    unittest.main()
