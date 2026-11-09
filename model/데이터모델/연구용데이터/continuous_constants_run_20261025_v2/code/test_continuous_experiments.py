"""Synthetic pre-freeze regression checks; never open project outcome data."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import continuous_experiments as c


PROTOCOL = json.loads((Path(__file__).parent / "protocol.json").read_text("utf8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf8")


class ContinuousGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sleepwell_continuous_guards_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def frozen_fixture(self):
        run = self.root / "run"
        write_json(run / "protocol.json", PROTOCOL)
        (run / "source.py").write_text("frozen", encoding="utf8")
        write_json(run / "source_manifest.json", {
            "protocol_sha256": c.m.sha(run / "protocol.json"),
            "sources": {"source.py": c.m.sha(run / "source.py")},
        })
        for cohort in c.COHORTS:
            out = run / cohort
            write_json(out / "train_selection.json", {
                "selected_daily_policy": "daily_mean_h14_l14",
            })
            external = self.root / (cohort + "_v1.json")
            write_json(external, {"v1": cohort})
            parity = {
                "maximum_absolute_difference_min": {"data": 0., "hybrid": 0.},
                "external_dependencies": {str(external): c.m.sha(external)},
            }
            write_json(out / "legacy_validation_reproduction.json", parity)
            model = out / "models" / "legacy14__data.joblib"
            model.parent.mkdir()
            model.write_bytes(b"frozen synthetic model")
            write_json(out / "calibration_lock.json", {
                "protocol_sha256": c.m.sha(run / "protocol.json"),
                "selection_sha256": c.m.sha(out / "train_selection.json"),
                "legacy_parity_sha256": c.m.sha(out / "legacy_validation_reproduction.json"),
                "external_dependencies": parity["external_dependencies"],
                "model_hashes": {model.name: c.m.sha(model)},
            })
        return run

    def test_frozen_fixture_accepts_unchanged_sources_and_both_locks(self):
        c.verify_locks(self.frozen_fixture())

    def test_any_frozen_dependency_change_blocks_before_test_read_or_marker(self):
        mutations = ("protocol", "source", "selection", "parity", "external", "model", "missing_lock")
        for kind in mutations:
            with self.subTest(kind=kind):
                self.root = Path(self.tmp.name) / kind
                run = self.frozen_fixture()
                paths = {
                    "protocol": run / "protocol.json", "source": run / "source.py",
                    "selection": run / "mesa/train_selection.json",
                    "parity": run / "mesa/legacy_validation_reproduction.json",
                    "external": self.root / "mesa_v1.json",
                    "model": run / "mesa/models/legacy14__data.joblib",
                    "missing_lock": run / "mesa/calibration_lock.json",
                }
                if kind == "missing_lock":
                    paths[kind].unlink()
                else:
                    paths[kind].write_bytes(paths[kind].read_bytes() + b"\nchanged")
                with patch.object(c.m, "load_frame") as load:
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        c.evaluate(self.root, run, "cmi", PROTOCOL)
                    load.assert_not_called()
                self.assertFalse((run / "cmi/test_started.json").exists())

    def test_calibration_refuses_existing_lock_or_other_cohort_test_start(self):
        run = self.frozen_fixture()
        with self.assertRaisesRegex(ValueError, "Cannot recalibrate"):
            c.calibrate(run, "cmi", PROTOCOL)
        (run / "cmi/calibration_lock.json").unlink()
        write_json(run / "mesa/test_started.json", {})
        with self.assertRaisesRegex(ValueError, "Cannot recalibrate"):
            c.calibrate(run, "cmi", PROTOCOL)

    def test_calibration_refuses_parity_or_external_change_before_validation_read(self):
        for kind in ("parity", "external"):
            with self.subTest(kind=kind):
                self.root = Path(self.tmp.name) / kind
                run = self.frozen_fixture()
                (run / "cmi/calibration_lock.json").unlink()
                if kind == "parity":
                    p = run / "cmi/legacy_validation_reproduction.json"
                    obj = json.loads(p.read_text("utf8"))
                    obj["maximum_absolute_difference_min"]["data"] = .001
                    write_json(p, obj)
                else:
                    write_json(self.root / "cmi_v1.json", {"changed": True})
                with patch.object(c.pd, "read_parquet") as read:
                    with self.assertRaisesRegex(ValueError, "Legacy"):
                        c.calibrate(run, "cmi", PROTOCOL)
                    read.assert_not_called()
                self.assertFalse((run / "cmi/calibration_lock.json").exists())

    def test_replay_orders_chronologically_isolates_subjects_and_splits(self):
        rows = []
        for subject, split, days in (("a", "train", (3, 1, 2)),
                                    ("b", "train", (1, 2)), ("a", "validation", (1, 2))):
            for day in days:
                rows.append({
                    "row_id": f"{subject}_{split}_{day}", "subject_id": subject, "split": split,
                    "cutoff": f"2026-01-{day:02d}T20:00:00+09:00",
                    "bed": f"2026-01-{day:02d}T23:00:00+09:00",
                    "wake": f"2026-01-{day+1:02d}T07:00:00+09:00",
                    "observed_at": f"2026-01-{day+1:02d}T08:00:00+09:00",
                })
        frame = pd.DataFrame(rows)
        calls = []
        def feature(records, cutoff, prior, enrolled, policy):
            calls.append((copy.deepcopy(records), cutoff, enrolled))
            return {"history_count": len(records)}, {"history_count": len(records)}
        with patch.object(c, "extended_features", side_effect=feature):
            x, states = c.replay(frame, {"bed_offset": 180., "wake_offset": 660.}, "legacy14")
        self.assertEqual(x.row_id.tolist(), frame.row_id.tolist())
        self.assertEqual(states.row_id.tolist(), frame.row_id.tolist())
        expected = [2, 0, 1, 0, 1, 0, 1]
        self.assertEqual(x.history_count.tolist(), expected)
        for history, cutoff, enrolled in calls:
            for record in history:
                self.assertLess(pd.Timestamp(record["bed"]), cutoff)
                self.assertLess(pd.Timestamp(record["observed_at"]), cutoff)
            self.assertLessEqual(pd.Timestamp(enrolled), cutoff)

    def training_fixture(self, suffix="", mismatch=None):
        project = self.root / ("project" + suffix)
        run = self.root / ("training" + suffix)
        run.mkdir()
        old = project / "데이터모델/연구용데이터/matched_physics_run_20261025_v1"
        rep = PROTOCOL["learners"]["cmi"]["representation"]
        pair = "hgb__" + rep
        selection = {"primary_pair": pair, "pairs": [
            {"pair_id": pair, "family": "hgb", "candidate": "hgb7", "representation": rep}]}
        write_json(old / "cmi/train_selection.json", selection)
        write_json(old / "protocol.json", {"candidates": {"hgb7": PROTOCOL["candidate"]}})
        rows = []
        for subject, split in [(f"t{i}", "train") for i in range(6)] + [("v0", "validation"), ("v1", "validation"), ("x", "test")]:
            for day in (1, 2):
                rows.append({"row_id": f"{subject}_{day}", "subject_id": subject, "split": split,
                             c.m.TARGETS[0]: 100., c.m.TARGETS[1]: 100.})
        frame = pd.DataFrame(rows)
        val = frame.loc[frame.split.eq("validation")].reset_index(drop=True)
        old_val = val.copy()
        if mismatch == "rows":
            old_val.loc[0, "row_id"] = "wrong"
        if mismatch == "subjects":
            old_val.loc[0, "subject_id"] = "wrong"
        if mismatch == "targets":
            old_val.loc[0, c.m.TARGETS[0]] = 101.
        old_val.to_parquet(old / "cmi/validation_rows_private.parquet", index=False)
        old_preds = np.full((len(val), 2), 101. if mismatch == "predictions" else 100.)
        np.savez(old / "cmi/validation_predictions_private.npz",
                 **{pair + "__" + arm: old_preds for arm in c.ARMS})
        source = project / "synthetic_episodes.txt"
        source.write_text("synthetic only", encoding="utf8")
        return project, run, frame, source, old

    def train_mocked(self, project, run, frame, source):
        priors = []
        replays = []
        fits = []
        def prior(f):
            people = sorted(f.subject_id.unique())
            self.assertTrue(f.split.eq("train").all())
            priors.append(people)
            return {"bed_offset": 100., "wake_offset": 100., "fit_people": people}
        def replay(f, p, policy):
            people = set(f.subject_id)
            if f.split.eq("train").all():
                self.assertEqual(people, set(p["fit_people"]))
            else:
                self.assertFalse(people & set(p["fit_people"]))
            replays.append((f.split.iloc[0], people, set(p["fit_people"])))
            x = pd.DataFrame({"physical_bed": np.full(len(f), 100.),
                              "physical_wake": np.full(len(f), 100.)})
            states = pd.DataFrame({"personal_weight": np.zeros(len(f)), "valid_days": np.zeros(len(f), dtype=int),
                                   "physical_fallback": np.zeros(len(f), dtype=int)})
            return x, states
        def fit(candidate, f, x, ref, seed):
            self.assertTrue(f.split.eq("train").all())
            fits.append(set(f.subject_id))
            return {"synthetic_model": True}, {}
        with ExitStack() as stack:
            replacements = [(c.m, "load_frame", lambda *_: (frame, source)),
                (c.m, "prior_from_train", prior), (c, "replay", replay),
                (c.m, "encode", lambda f, x, *a: x), (c.m, "fit_one", fit),
                (c.m, "reference", lambda x, *a: np.zeros((len(x), 2))),
                (c.m, "predict", lambda models, x, ref: np.full((len(x), 2), 100.)),
                (c.m, "project", lambda x: x), (c.m, "log", lambda *_: None)]
            for obj, name, replacement in replacements:
                stack.enter_context(patch.object(obj, name, side_effect=replacement))
            c.train(project, run, "cmi", PROTOCOL)
        return priors, replays, fits

    def test_train_fold_priors_and_fits_use_only_disjoint_train_people(self):
        project, run, frame, source, _ = self.training_fixture()
        priors, replays, fits = self.train_mocked(project, run, frame, source)
        self.assertEqual(len(priors), 4)  # three fold-local priors and final TRAIN prior
        self.assertEqual([len(p) for p in priors], [4, 4, 4, 6])
        self.assertEqual(len(fits), 40)  # 3 folds and final fit, 5 policies, 2 arms
        self.assertTrue(all("x" not in people for people in fits))
        self.assertTrue(all("x" not in people for _, people, _ in replays))
        done = json.loads((run / "cmi/training_done.json").read_text("utf8"))
        self.assertFalse(done["test_scored"])
        parity = json.loads((run / "cmi/legacy_validation_reproduction.json").read_text("utf8"))
        self.assertEqual(parity["maximum_absolute_difference_min"], {"data": 0., "hybrid": 0.})
        self.assertEqual(len(parity["external_dependencies"]), 4)

    def test_legacy_mismatch_prevents_training_done_and_parity_record(self):
        for mismatch in ("rows", "subjects", "targets", "predictions"):
            with self.subTest(mismatch=mismatch):
                project, run, frame, source, _ = self.training_fixture(mismatch, mismatch)
                with self.assertRaisesRegex(ValueError, "Legacy validation"):
                    self.train_mocked(project, run, frame, source)
                self.assertFalse((run / "cmi/training_done.json").exists())
                self.assertFalse((run / "cmi/legacy_validation_reproduction.json").exists())

    def test_fixed_learner_and_parameters_reject_drift_before_data_loading(self):
        for mismatch in ("learner", "candidate"):
            with self.subTest(mismatch=mismatch):
                project, run, _, _, _ = self.training_fixture(mismatch)
                protocol = copy.deepcopy(PROTOCOL)
                if mismatch == "learner":
                    protocol["learners"]["cmi"]["representation"] = "raw_absolute"
                else:
                    protocol["candidate"]["leaves"] = 9
                with patch.object(c.m, "load_frame") as load:
                    with self.assertRaisesRegex(ValueError, "Fixed"):
                        c.train(project, run, "cmi", protocol)
                    load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
