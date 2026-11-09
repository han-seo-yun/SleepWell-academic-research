import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from night_core import ZONE
from research_lab import evaluate_run
from sleep_observation import MIN_RHYTHM_DAYS, MIN_SRI_DAY_PAIRS, describe_observations


def lab_row(day, onset=180.0, wake=660.0, delay_hours=1.0, labeled=True, participant="P-001"):
    cutoff = datetime(2026, 1, 1, 20, 0, tzinfo=ZONE) + timedelta(days=day)
    if not labeled:
        return {"participant_id": participant, "cutoff": cutoff,
                "sleep_start": None, "sleep_end": None, "observed_at": None}
    sleep_end = cutoff + timedelta(minutes=wake)
    return {"participant_id": participant, "cutoff": cutoff,
            "sleep_start": cutoff + timedelta(minutes=onset), "sleep_end": sleep_end,
            "observed_at": sleep_end + timedelta(hours=delay_hours)}


def regularity(rows):
    return describe_observations(rows)["regularity"][0]


def completeness(rows):
    return describe_observations(rows)["completeness"][0]


class RegularityTests(unittest.TestCase):
    def test_identical_days_have_perfect_sri_and_stability(self):
        result = regularity([lab_row(day) for day in range(10)])
        self.assertEqual(result["sri"], 100.0)
        self.assertEqual(result["interdaily_stability"], 1.0)
        self.assertEqual(result["onset_sd_min"], 0.0)
        self.assertEqual(result["onset_mean_clock"], "23:00")
        self.assertEqual(result["wake_mean_clock"], "07:00")
        self.assertEqual(result["midpoint_mean_clock"], "03:00")
        self.assertEqual(result["duration_mean_min"], 480.0)
        self.assertEqual(result["unavailable"], [])

    def test_sri_matches_hand_computed_alternating_shift(self):
        # Alternating 60-minute shift of an 8 h episode: every day pair disagrees on
        # 2 x 60 = 120 of 1440 minutes -> SRI = -100 + 200 * 1320/1440.
        rows = [lab_row(day, onset=180 + (60 if day % 2 else 0), wake=660 + (60 if day % 2 else 0))
                for day in range(10)]
        self.assertAlmostEqual(regularity(rows)["sri"], round(-100 + 200 * 1320 / 1440, 1))

    def test_unlabeled_nights_break_pairs_instead_of_counting_as_wake(self):
        rows = [lab_row(day, labeled=day % 2 == 0) for day in range(2 * MIN_SRI_DAY_PAIRS + 2)]
        result = regularity(rows)
        self.assertEqual(result["sri_day_pairs"], 0)
        self.assertIsNone(result["sri"])
        self.assertTrue(any(reason.startswith("SRI") for reason in result["unavailable"]))

    def test_minimum_pair_threshold(self):
        enough = regularity([lab_row(day) for day in range(MIN_SRI_DAY_PAIRS + 1)])
        short = regularity([lab_row(day) for day in range(MIN_SRI_DAY_PAIRS)])
        self.assertEqual(enough["sri_day_pairs"], MIN_SRI_DAY_PAIRS)
        self.assertIsNotNone(enough["sri"])
        self.assertIsNone(short["sri"])
        too_few_days = regularity([lab_row(day) for day in range(MIN_RHYTHM_DAYS - 1)])
        self.assertIsNone(too_few_days["interdaily_stability"])
        self.assertIsNone(too_few_days["intradaily_variability"])

    def test_irregular_schedule_lowers_stability_and_raises_variability(self):
        steady = regularity([lab_row(day) for day in range(14)])
        shifted = regularity([lab_row(day, onset=60 + (day % 4) * 120, wake=540 + (day % 4) * 120)
                              for day in range(14)])
        self.assertLess(shifted["interdaily_stability"], steady["interdaily_stability"])
        self.assertLess(shifted["sri"], steady["sri"])
        self.assertGreater(shifted["midpoint_sd_min"], 60.0)

    def test_weekend_gap_uses_friday_and_saturday_cutoffs(self):
        # 2026-01-02 is a Friday; weekend nights sleep 90 minutes later.
        rows = []
        for day in range(21):
            cutoff_day = (datetime(2026, 1, 1) + timedelta(days=day)).weekday()
            late = 90 if cutoff_day in (4, 5) else 0
            rows.append(lab_row(day, onset=180 + late, wake=660 + late))
        result = regularity(rows)
        self.assertEqual(result["weekend_weekday_midpoint_gap_min"], 90.0)
        self.assertEqual(result["weekend_nights"], 6)


class CompletenessTests(unittest.TestCase):
    def test_statuses_and_timeliness_boundary(self):
        rows = [
            lab_row(0, delay_hours=1),
            lab_row(1, delay_hours=13),           # 07:00 + 13 h = 20:00 next cutoff -> late
            lab_row(2, labeled=False),
            lab_row(4, delay_hours=12.9),         # just before next cutoff -> timely
        ]
        result = completeness(rows)
        statuses = [cell["status"] for cell in result["grid"]]
        self.assertEqual(statuses, ["timely", "late", "prediction_only", "missing", "timely"])
        self.assertEqual(result["span_days"], 5)
        self.assertEqual(result["labeled_nights"], 3)
        self.assertEqual(result["missing_nights"], 1)
        self.assertEqual(result["labeled_coverage"], 0.6)
        self.assertAlmostEqual(result["timely_share_of_labeled"], round(2 / 3, 3))
        self.assertEqual(result["report_delay_median_min"], 774.0)
        self.assertEqual(result["grid"][3]["report_delay_min"], None)

    def test_participants_are_separate(self):
        rows = [lab_row(day, participant="A") for day in range(3)] + [lab_row(5, participant="B")]
        result = describe_observations(rows)
        self.assertEqual([item["participant_id"] for item in result["completeness"]], ["A", "B"])
        self.assertEqual(result["completeness"][1]["span_days"], 1)
        self.assertEqual(result["role"], "descriptive_observed_data")


class EngineIntegrationTests(unittest.TestCase):
    def test_observations_do_not_change_metrics_or_run_id(self):
        payload = {"schema_version": 1, "mode": "synthetic", "cohorts": ["cmi"],
                   "synthetic": {"n_nights": 20, "seed": 7, "shift_after_night": 10}}
        result = evaluate_run(payload)
        self.assertIn("observations", result)
        self.assertEqual(result["observations"]["regularity"][0]["labeled_nights"],
                         result["summary"]["labeled_nights"])
        again = evaluate_run(payload)
        self.assertEqual(result["run_id"], again["run_id"])
        self.assertEqual(result["metrics"], again["metrics"])
        json.dumps(result, allow_nan=False)


class CommandLineTests(unittest.TestCase):
    def test_cli_writes_self_describing_folder_and_refuses_reuse(self):
        from research_lab_cli import main

        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(json.dumps({"schema_version": 1, "mode": "synthetic", "cohorts": ["mesa"],
                                           "synthetic": {"n_nights": 14, "seed": 3, "shift_after_night": 7}}), encoding="utf-8")
            out = Path(directory) / "out"
            self.assertEqual(main(["--request", str(request), "--out", str(out), "--no-figures", "--quiet"]), 0)
            names = sorted(path.name for path in out.iterdir())
            self.assertEqual(names, ["metrics.csv", "observed_completeness.csv", "observed_regularity.csv",
                                     "result.json", "run.json"])
            run = json.loads((out / "run.json").read_text(encoding="utf-8"))
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(run["run_id"], result["run_id"])
            self.assertEqual(len(run["input_sha256"]), 64)
            self.assertEqual(set(run["files"]), set(names) - {"run.json"})
            self.assertEqual(main(["--request", str(request), "--out", str(out), "--quiet"]), 3)

    def test_cli_rejects_invalid_input(self):
        from research_lab_cli import main

        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.csv"
            bad.write_text("participant_id,cutoff\nA,2026-01-01T20:00:00+09:00\n", encoding="utf-8")
            self.assertEqual(main(["--csv", str(bad), "--out", str(Path(directory) / "o"), "--quiet"]), 2)


if __name__ == "__main__":
    unittest.main()
