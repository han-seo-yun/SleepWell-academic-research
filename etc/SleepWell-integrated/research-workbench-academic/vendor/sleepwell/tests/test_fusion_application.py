"""Real packaged-model integration with a temporary participant database."""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from engine import validate_record
from fusion_forecast import FusionForecasts
from night_core import local_time
from prospective import BASE_ARMS, FUSION_ARMS
from server import Application, Store
from test_prospective_store import Research, profile


class FusionApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = local_time("2026-10-24T20:05:00+09:00")
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.store.save_profile(profile(), [], self.now - timedelta(days=30))
        self.app = Application(self.store, Research(), clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_models_freeze_nine_arms_and_survive_later_correction(self):
        original = self.app.ensure_due()
        self.assertEqual(set(original["predictions"]), BASE_ARMS | FUSION_ARMS)
        self.assertTrue(original["eligible_capture"])
        self.assertEqual(set(original["fusion_model"]["artifacts"]), {"cmi", "mesa"})
        self.now = local_time("2026-10-25T09:00:00+09:00")
        row = validate_record({"bed": "2026-10-24T23:30", "wake": "2026-10-25T07:00", "latency_min": 10}, self.now)
        self.store.save_record(row, self.now)
        summary = self.app.prospective_state(self.now)["summary"]
        self.assertEqual(summary["evaluated_nights"], 1)
        self.assertEqual(set(summary["versions"][0]["arms"]), BASE_ARMS | FUSION_ARMS)
        self.assertEqual(summary["outcome_source_policy"]["primary_evaluated_nights"], 1)
        self.now += timedelta(hours=1)
        row = validate_record(dict(row, bed="2026-10-25T00:30"), self.now)
        self.store.save_record(row, self.now)
        restored = Application(Store(self.store.path), Research(), clock=lambda: self.now)
        self.assertEqual(restored.store.prospective_forecasts()[0], original)
        self.assertEqual(restored.prospective_state(self.now)["summary"]["versions"], summary["versions"])
        self.assertEqual(restored.prospective_state(self.now)["summary"]["corrections_count"], 1)

    def test_wearable_first_does_not_preempt_later_diary_primary(self):
        self.app.ensure_due()
        self.now = local_time("2026-10-25T08:00:00+09:00")
        row = validate_record({"bed": "2026-10-24T23:30", "wake": "2026-10-25T07:00", "latency_min": 10}, self.now)
        row["source"] = "health_connect"
        row["sleep_start"] = "2026-10-24T23:40:00+09:00"
        self.store.save_record(row, self.now)
        self.now += timedelta(hours=1)
        diary = validate_record({"bed": "2026-10-25T00:30", "wake": "2026-10-25T07:00", "latency_min": 10}, self.now)
        diary["source"] = "diary"
        self.store.save_record(diary, self.now)
        state = self.app.prospective_state(self.now)
        self.assertEqual(state["primary_diary_summary"]["evaluated_nights"], 1)
        self.assertEqual(state["auxiliary_wearable_summary"]["evaluated_nights"], 1)
        primary = state["primary_diary_summary"]["versions"][0]["arms"]["data_cmi"]["onset_mae_min"]
        auxiliary = state["auxiliary_wearable_summary"]["versions"][0]["arms"]["data_cmi"]["onset_mae_min"]
        self.assertNotEqual(primary, auxiliary)

    def test_generated_recommendation_and_status_use_real_cohort_models(self):
        result = self.app.generate()
        self.assertTrue(result["fusion_forecast"]["available"])
        self.assertEqual(set(result["fusion_forecast"]["predictions"]), FUSION_ARMS)
        state = self.app.state()
        self.assertTrue(state["fusion_status"]["available"])
        self.assertEqual(set(state["fusion_status"]["research_metrics"]), {"cmi", "mesa"})
        json.dumps(state, allow_nan=False)

    def test_missing_models_report_capture_failure_without_silent_fallback(self):
        self.app.fusion_models = FusionForecasts(Path(self.temp.name) / "missing")
        result = self.app.ensure_due()
        self.assertIsNone(result)
        self.assertEqual(self.store.prospective_forecasts(), [])
        self.assertIsNotNone(self.app.prospective_state(self.now)["capture_error"])
        self.assertFalse(self.app.generate()["fusion_forecast"]["available"])

    def test_no_early_capture_or_retroactive_qualification(self):
        self.now = self.now.replace(hour=19)
        self.assertIsNone(self.app.ensure_due())
        self.assertFalse(self.app.generate()["fusion_forecast"]["available"])
        self.now = self.now.replace(hour=22)
        result = self.app.ensure_due()
        self.assertFalse(result["eligible_capture"])
        self.assertEqual(set(result["predictions"]), BASE_ARMS | FUSION_ARMS)


if __name__ == "__main__":
    unittest.main()
