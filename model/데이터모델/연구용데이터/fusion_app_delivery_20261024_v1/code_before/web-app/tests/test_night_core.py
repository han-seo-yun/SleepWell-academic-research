import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from night_core import features, normalize_history, local_time, advance, constrain


class NightCoreTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = local_time("2026-10-20T20:00:00+09:00")
        self.prior = {"bed_offset": 180., "wake_offset": 660.}

    def rows(self, n):
        rows = []
        for i in range(n, 0, -1):
            wake = self.cutoff.replace(hour=7) - timedelta(days=i-1)
            bed = wake - timedelta(hours=8)
            rows.append(dict(bed=bed.isoformat(), wake=wake.isoformat(),
                             observed_at=wake.isoformat(), source="diary", latency_min=0))
        return rows

    def test_future_and_late_entered_records_do_not_change_prediction(self):
        rows = self.rows(20)
        before = features(rows, self.cutoff, self.prior)
        late = dict(rows[0], observed_at=(self.cutoff + timedelta(seconds=1)).isoformat(), bed="2026-10-19T21:00:00+09:00")
        future = dict(rows[-1], wake=(self.cutoff + timedelta(hours=10)).isoformat())
        self.assertEqual(before, features(rows + [late, future], self.cutoff, self.prior))

    def test_unbounded_accumulation_and_idempotent_edits(self):
        rows = self.rows(60)
        f, state = features(rows, self.cutoff, self.prior)
        self.assertEqual(state["valid_days"], 60)
        self.assertAlmostEqual(state["personal_weight"], 60/74)
        self.assertEqual((f, state), features(rows + rows, self.cutoff, self.prior))
        changed = dict(rows[0], bed=(local_time(rows[0]["bed"]) - timedelta(hours=2)).isoformat(),
                       observed_at=(self.cutoff - timedelta(minutes=1)).isoformat())
        self.assertNotEqual(f["habit_bed"], features(rows + [changed], self.cutoff, self.prior)[0]["habit_bed"])

    def test_population_first_two_weeks(self):
        for count in (0, 1, 13):
            f, state = features(self.rows(count), self.cutoff, self.prior)
            self.assertEqual(state["mode"], "population")
            self.assertEqual(f["habit_bed"], 180)
        self.assertEqual(features(self.rows(20), self.cutoff, self.prior)[1]["mode"], "personalized")

    def test_no_archive_or_missing_availability(self):
        row = self.rows(1)[0]
        self.assertEqual(normalize_history([dict(row, source="archive"), dict(row, observed_at=None)], self.cutoff), [])

    def test_state_and_output_bounds(self):
        self.assertLess(advance(.6, True, 8), .6)
        self.assertGreater(advance(.2, False, 16), .2)
        f, _ = features(self.rows(20), self.cutoff, self.prior)
        self.assertGreaterEqual(f["physical_bed"], 0)
        self.assertGreater(f["physical_wake"], f["physical_bed"])
        self.assertEqual(constrain(-20, -2), (0, 30))
        with self.assertRaises(ValueError):
            features([], self.cutoff - timedelta(minutes=1), self.prior)


if __name__ == "__main__":
    unittest.main()
