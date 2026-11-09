import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

import mesa_experiments as mesa


HEADER = [
    "mesaid", "line", "linetime", "offwrist", "activity", "marker",
    "whitelight", "redlight", "greenlight", "bluelight", "wake", "interval",
    "dayofweek", "daybymidnight", "daybynoon", "date_shifted", "actigraphy_month_start",
]


def write_rows(path: Path, participant: str, *, bad_row: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for index in range(1, 41):
            writer.writerow([
                participant,
                index,
                f"00:{(index - 1) // 2:02d}:{30 if index % 2 == 0 else 0:02d}",
                "1" if index == bad_row else "0",
                float(index),
                "",
                1.0,
                2.0,
                3.0,
                4.0,
                "0" if index <= 20 else "1",
                "REST-S" if index <= 20 else "ACTIVE",
                1,
                1,
                1,
                "2000-01-01",
                1,
            ])


class MesaExperimentsTest(unittest.TestCase):
    @staticmethod
    def _reference_aggregate(arrays, epoch_seconds):
        rows_per_epoch = epoch_seconds // 30
        lines = arrays["line"]
        bins = (lines - 1) // rows_per_epoch
        changes = np.flatnonzero(np.diff(bins)) + 1
        starts = np.r_[0, changes]
        ends = np.r_[changes, len(lines)]
        output_lines = []
        output_x = []
        output_y = []
        for start, end in zip(starts, ends):
            bin_id = int(bins[start])
            expected_start = bin_id * rows_per_epoch + 1
            expected_lines = np.arange(expected_start, expected_start + rows_per_epoch, dtype=np.int32)
            if end - start != rows_per_epoch or not np.array_equal(lines[start:end], expected_lines):
                continue
            output_lines.append(expected_start)
            output_x.append(arrays["x"][start:end].mean(axis=0, dtype=np.float64).astype(np.float32))
            output_y.append(int(arrays["y_sleep"][start:end].mean() >= 0.5))
        return {
            "line_start": np.asarray(output_lines, dtype=np.int32),
            "x": np.asarray(output_x, dtype=np.float32).reshape((-1, len(mesa.FEATURE_NAMES))),
            "y": np.asarray(output_y, dtype=np.uint8),
        }

    def test_subject_hash_split_is_stable_and_disjoint(self):
        participants = [str(index) for index in range(1, 500)]
        first = {participant: mesa.subject_split(participant) for participant in participants}
        second = {participant: mesa.subject_split(participant.zfill(4)) for participant in participants}
        self.assertEqual(first, second)
        groups = {name: {p for p, split in first.items() if split == name} for name in ("train", "validation", "test")}
        self.assertFalse(groups["train"] & groups["validation"])
        self.assertFalse(groups["train"] & groups["test"])
        self.assertFalse(groups["validation"] & groups["test"])
        self.assertTrue(0.62 < len(groups["train"]) / len(participants) < 0.78)

    def test_mask_excludes_entire_incomplete_ten_minute_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "mesa-sleep-0001.csv"
            write_rows(source, "1", bad_row=3)
            arrays, qc, _ = mesa.extract_common_rows(source, "1")
        self.assertEqual(qc["source_rows"], 40)
        self.assertEqual(qc["offwrist_or_invalid_rows"], 1)
        self.assertEqual(qc["eligible_ten_minute_blocks"], 1)
        self.assertEqual(arrays["line"].tolist(), list(range(21, 41)))

    def test_label_polarity_is_sleep_equals_one_minus_wake(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "mesa-sleep-0001.csv"
            write_rows(source, "0001")
            arrays, _, _ = mesa.extract_common_rows(source, "1")
        self.assertEqual(arrays["y_sleep"][:20].tolist(), [1] * 20)
        self.assertEqual(arrays["y_sleep"][20:].tolist(), [0] * 20)
        self.assertEqual(arrays["t_rel_sec"][[0, -1]].tolist(), [0, 1170])

    def test_epoch_aggregation_requires_complete_bins(self):
        arrays = {
            "line": np.arange(1, 11, dtype=np.int32),
            "x": np.ones((10, 5), dtype=np.float32),
            "y_sleep": np.asarray([1] * 6 + [0] * 4, dtype=np.uint8),
        }
        sixty = mesa.aggregate_epoch(arrays, 60)
        self.assertEqual(len(sixty["y"]), 5)
        self.assertEqual(sixty["line_start"].tolist(), [1, 3, 5, 7, 9])
        incomplete = {name: values[:-1] for name, values in arrays.items()}
        five_minute = mesa.aggregate_epoch(incomplete, 300)
        self.assertEqual(len(five_minute["y"]), 0)

    def test_vectorized_aggregation_and_confusion_match_reference_exactly(self):
        rng = np.random.default_rng(20261010)
        all_lines = np.arange(1, 401, dtype=np.int32)
        for _ in range(20):
            keep = rng.random(len(all_lines)) > 0.12
            lines = all_lines[keep]
            arrays = {
                "line": lines,
                "x": rng.uniform(0, 100, size=(len(lines), 5)).astype(np.float32),
                "y_sleep": rng.integers(0, 2, size=len(lines), dtype=np.uint8),
            }
            for seconds in mesa.EPOCH_SECONDS:
                expected = self._reference_aggregate(arrays, seconds)
                actual = mesa.aggregate_epoch(arrays, seconds)
                for key in ("line_start", "x", "y"):
                    np.testing.assert_array_equal(actual[key], expected[key])

            truth = rng.integers(0, 2, size=500, dtype=np.uint8)
            prediction = rng.integers(0, 2, size=500, dtype=np.uint8)
            accumulator = mesa.MetricAccumulator()
            accumulator.add(truth, prediction)
            expected_confusion = np.zeros((2, 2), dtype=np.int64)
            for actual, predicted in zip(truth.astype(int), prediction.astype(int)):
                expected_confusion[actual, predicted] += 1
            np.testing.assert_array_equal(accumulator.confusion, expected_confusion)

    def test_forbidden_label_columns_are_not_model_features(self):
        self.assertNotIn("wake", mesa.FEATURE_NAMES)
        self.assertNotIn("interval", mesa.FEATURE_NAMES)
        for candidate in mesa.CANDIDATES:
            self.assertNotIn("wake", candidate["features"])
            self.assertNotIn("interval", candidate["features"])

    def test_rejects_duplicate_line_or_bad_cadence_and_sensor_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "mesa-sleep-0001.csv"
            write_rows(source, "1")
            with source.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            rows[5][1] = rows[4][1]
            with source.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(rows)
            with self.assertRaisesRegex(ValueError, "strictly increasing"):
                mesa.extract_common_rows(source, "1")
        self.assertIsNone(mesa._finite_sensor("-1"))
        self.assertIsNone(mesa._finite_sensor("1e100"))

    def test_paired_bootstrap_is_deterministic_and_paired(self):
        result = mesa._paired_bootstrap([0.8, 0.7, 0.9], [0.5, 0.6, 0.4], draws=200)
        self.assertEqual(result["subjects"], 3)
        self.assertAlmostEqual(result["mean_difference"], 0.3)
        self.assertEqual(result, mesa._paired_bootstrap([0.8, 0.7, 0.9], [0.5, 0.6, 0.4], draws=200))
        with self.assertRaisesRegex(ValueError, "lengths"):
            mesa._paired_bootstrap([0.5], [0.5, 0.6])

    def test_small_end_to_end_run_persists_frozen_protocol_and_predictions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "raw"
            output = Path(temporary) / "result"
            for participant in range(1, 81):
                write_rows(root / f"mesa-sleep-{participant:04d}.csv", str(participant))

            report = mesa.run_experiment(root, output)

            self.assertTrue((output / "protocol.json").is_file())
            self.assertTrue((output / "selected.json").is_file())
            self.assertTrue((output / "selected_model.joblib").is_file())
            self.assertTrue((output / "evaluation" / "test" / "subject_metrics.jsonl").is_file())
            self.assertEqual(len(report["test_all_frozen_candidates_descriptive"]), 16)
            self.assertTrue(report["protocol_frozen_before_test"])

            resumed_output = Path(temporary) / "resumed"
            stage = Path(temporary) / ".resumed.staging-123"
            output.rename(stage)
            for name in ("protocol.json", "selected.json", "selected_model.joblib", "experiment_report.json"):
                (stage / name).unlink()
            shutil.rmtree(stage / "evaluation")
            resumed_report = mesa.resume_experiment(stage, resumed_output, "a" * 64)
            self.assertTrue((resumed_output / "resume_validation.json").is_file())
            self.assertEqual(resumed_report["selected_before_test"], report["selected_before_test"])
            protocol = json.loads((resumed_output / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(protocol["resume"]["subject_archives"], 80)
            self.assertEqual(protocol["resume"]["previous_source_code_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
