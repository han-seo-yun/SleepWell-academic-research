from __future__ import annotations

import csv
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from researchbench import gradual_evaluation as gradual


ZONE = timezone(timedelta(hours=9))


class FakeModel:
    def __init__(self, path="fake.json", expected_cohort="cmi"):
        self.path = Path(path)
        self.sha256 = "a" * 64

    def predict(self, profile, records, cutoff, personalization_policy="legacy14"):
        assert all(datetime.fromisoformat(row["wake"]) < cutoff for row in records)
        assert all(datetime.fromisoformat(row["observed_at"]) < cutoff for row in records)
        count = len(records)
        shift = 1.0 if personalization_policy == "gradual_v1" and count else 0.0
        bed, wake = 100.0 + count + shift, 600.0 + count + shift
        return {
            "data": (bed, wake), "hybrid": (bed + 2, wake + 2),
            "physical": (bed + 4, wake + 4),
            "features": {"median14_bed": 106.0 + count, "median14_wake": 606.0 + count},
            "personalization": {"valid_days": count,
                                "personal_weight": count / (count + 14) if count else 0.0},
        }


def row(participant, split, day, row_id, *, cohort="cmi"):
    cutoff = datetime(2026, 1, day, 20, tzinfo=ZONE)
    bed = cutoff + timedelta(minutes=120)
    wake = cutoff + timedelta(minutes=620)
    return {
        "cohort": cohort, "participant_id": participant, "split": split,
        "row_id": str(row_id), "cutoff": cutoff, "bed": bed, "wake": wake,
        "observed_at": wake, "bed_offset": 120.0, "wake_offset": 620.0,
        "age": 70.0, "sex": "female", "label_source": "test label",
        "rare_by_train_mad": False, "reference": {},
    }


class GradualEvaluationTests(unittest.TestCase):
    def test_history_is_causal_and_current_label_is_not_visible(self):
        rows = [row("a", "test", 1, 1), row("a", "test", 2, 2)]
        first, failures = gradual._predict_rows(rows, {"cmi": FakeModel()}, "gradual_v1", None, None)
        changed = [dict(item) for item in rows]
        changed[1] = dict(changed[1], bed_offset=500.0, wake_offset=1000.0)
        second, _ = gradual._predict_rows(changed, {"cmi": FakeModel()}, "gradual_v1", None, None)
        self.assertFalse(failures)
        first_prediction = next(item for item in first if item["row_id"] == "1" and item["arm"] == "data")
        changed_prediction = next(item for item in second if item["row_id"] == "1" and item["arm"] == "data")
        self.assertEqual(first_prediction["predicted_bed_offset"], changed_prediction["predicted_bed_offset"])
        later = next(item for item in first if item["row_id"] == "2" and item["arm"] == "data")
        self.assertEqual(later["history_count"], 1)

    def test_participant_disjoint_split_violation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            self._write_input(path, [
                self._csv_row("a", "train", 1, 1),
                self._csv_row("a", "test", 2, 2),
            ])
            with self.assertRaisesRegex(ValueError, "Participant-disjoint"):
                gradual._read_input(path)

    def test_cluster_bootstrap_is_deterministic(self):
        clusters = {"a": [1.0, 2.0], "b": [3.0], "c": [4.0, 5.0]}
        first = gradual.participant_cluster_interval(clusters, "participant", 200, 17)
        second = gradual.participant_cluster_interval(clusters, "participant", 200, 17)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], first[1])

    def test_explicit_gradual_policy_differs_after_first_history(self):
        rows = [row("a", "test", 1, 1), row("a", "test", 2, 2)]
        legacy, _ = gradual._predict_rows(rows, {"cmi": FakeModel()}, "legacy14", None, None)
        changed, _ = gradual._predict_rows(rows, {"cmi": FakeModel()}, "gradual_v1", None, None)
        legacy_later = next(r for r in legacy if r["row_id"] == "2" and r["arm"] == "data")
        changed_later = next(r for r in changed if r["row_id"] == "2" and r["arm"] == "data")
        self.assertNotEqual(legacy_later["predicted_bed_offset"], changed_later["predicted_bed_offset"])

    def test_invalid_rows_are_reported_without_identifiers_in_aggregate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            valid = self._csv_row("a", "test", 1, 1)
            invalid = dict(valid, participant_id="", row_id="2")
            self._write_input(path, [valid, invalid])
            rows, failures, _ = gradual._read_input(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(failures), 1)
            self.assertNotIn("participant_id", failures[0])

    def test_inconsistent_timestamp_and_offset_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            valid = self._csv_row("a", "test", 1, 1)
            invalid = self._csv_row("b", "test", 2, 2)
            invalid["bed_offset"] = 121
            self._write_input(path, [valid, invalid])
            rows, failures, _ = gradual._read_input(path)
            self.assertEqual(len(rows), 1)
            self.assertRegex(failures[0]["reason"], "do not match")

    def test_malformed_row_cannot_conceal_split_overlap(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            valid = self._csv_row("a", "train", 1, 1)
            malformed = self._csv_row("a", "test", 2, 2)
            malformed["bed_offset"] = "not-a-number"
            self._write_input(path, [valid, malformed])
            with self.assertRaisesRegex(ValueError, "raw input"):
                gradual._read_input(path)

    def test_end_to_end_writes_aggregate_and_private_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            input_path = base / "input.csv"
            source_rows = []
            row_id = 0
            for split, participant, start in (("train", "tr", 1), ("validation", "va", 5), ("test", "te", 9)):
                for day in (start, start + 1):
                    source_rows.append(self._csv_row(participant, split, day, row_id))
                    row_id += 1
            self._write_input(input_path, source_rows)
            fake_module = types.SimpleNamespace(
                __file__=str(base / "fusion_forecast.py"), PortableCohortModel=FakeModel,
            )
            output = base / "result"
            with mock.patch.object(gradual, "vendor_module", return_value=fake_module):
                result = gradual.evaluate(input_path, output, bootstrap_replicates=20, seed=3)
            self.assertEqual(result["evaluation"]["schema"], gradual.SCHEMA)
            self.assertTrue(result["evaluation"]["legacy_replay_parity"]["verified"])
            self.assertTrue((output / "evaluation.json").is_file())
            self.assertTrue((output / "private_predictions.csv").is_file())
            aggregate_text = (output / "evaluation.json").read_text(encoding="utf-8")
            self.assertNotIn('"participant_id"', aggregate_text)
            self.assertTrue(result["evaluation"]["exploratory_residual_intervals"])
            metrics = result["evaluation"]["metrics"]
            legacy = next(r for r in metrics if r["split"] == "test" and r["policy"] == "legacy14"
                          and r["arm"] == "recent_median" and r["weighting"] == "participant"
                          and r["metric"] == "joint_mae_min")
            changed = next(r for r in metrics if r["split"] == "test" and r["policy"] == "gradual_v1"
                           and r["arm"] == "recent_median" and r["weighting"] == "participant"
                           and r["metric"] == "joint_mae_min")
            self.assertEqual((legacy["ci95_low"], legacy["ci95_high"]),
                             (changed["ci95_low"], changed["ci95_high"]))

    def test_evaluate_fails_closed_when_any_input_row_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            input_path = base / "input.csv"
            valid = self._csv_row("a", "test", 1, 1)
            invalid = self._csv_row("b", "test", 2, 2)
            invalid["bed_offset"] = "bad"
            self._write_input(input_path, [valid, invalid])
            with mock.patch.object(gradual, "vendor_module") as vendor:
                with self.assertRaisesRegex(ValueError, "failed closed"):
                    gradual.evaluate(input_path, base / "result", bootstrap_replicates=10)
                vendor.assert_not_called()

    def test_stale_metadata_sidecar_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            input_path = base / "input.csv"
            self._write_input(input_path, [self._csv_row("a", "test", 1, 1)])
            input_path.with_suffix(".metadata.json").write_text(json.dumps({
                "schema": "sleepwell.gradual_input_metadata.v1",
                "normalized_input": {"sha256": "0" * 64, "rows": 1},
                "cohorts": {}, "sources": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CSV hash mismatch"):
                gradual._load_source_metadata(input_path, gradual._read_input(input_path)[0])

    def test_inconsistent_demographics_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            first = self._csv_row("a", "test", 1, 1)
            second = self._csv_row("a", "test", 2, 2)
            second["age"] = 71
            self._write_input(path, [first, second])
            with self.assertRaisesRegex(ValueError, "Inconsistent within-participant"):
                gradual._read_input(path)

    @staticmethod
    def _csv_row(participant, split, day, row_id):
        cutoff = datetime(2026, 1, day, 20, tzinfo=ZONE)
        history_count = 0 if split != "test" else day - 9
        bed = 100.0 + history_count
        wake = 600.0 + history_count
        values = {
            "cohort": "cmi", "participant_id": participant, "split": split,
            "row_id": row_id, "cutoff": cutoff.isoformat(),
            "bed": (cutoff + timedelta(minutes=120)).isoformat(),
            "wake": (cutoff + timedelta(minutes=620)).isoformat(),
            "observed_at": (cutoff + timedelta(minutes=620)).isoformat(),
            "bed_offset": 120, "wake_offset": 620, "age": 70, "sex": "female",
            "label_source": "test", "rare_by_train_mad": "false" if split == "test" else "",
        }
        for arm, extra in (("data", 0), ("hybrid", 2), ("physical", 4)):
            values[f"legacy_{arm}_bed"] = bed + extra if split == "test" else ""
            values[f"legacy_{arm}_wake"] = wake + extra if split == "test" else ""
        return values

    @staticmethod
    def _write_input(path, rows):
        fields = list(rows[0])
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()

