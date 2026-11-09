from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from researchbench import academic_prospective as adapter
from researchbench.academic_backend import get_provenance


class AcademicProspectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = str(Path(self.temp.name) / "prospective.sqlite3")
        self.now = datetime.fromisoformat("2026-10-20T20:05:00+09:00")

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, **updates):
        config = {"mode": "prospective_commit", "journal_path": self.journal,
                  "cutoff": "2026-10-20"}
        config.update(updates)
        with mock.patch.object(adapter, "_now", return_value=self.now):
            return adapter.run_prospective(config)

    def outcome(self, forecast_id, **updates):
        value = {
            "sleep_start": "2026-10-20T23:50:00+09:00",
            "wake": "2026-10-21T07:10:00+09:00",
            "observed_at": "2026-10-21T08:00:00+09:00",
            "source": "diary",
            "enrollment_id": "academic-demo-enrollment",
        }
        value.update(updates)
        return adapter.run_prospective({
            "mode": "prospective_outcome", "journal_path": self.journal,
            "forecast_id": forecast_id, "outcome": value,
        })

    def test_synthetic_playback_runs_all_nine_arms_and_four_additive_online_arms(self):
        progress = []
        result = adapter.run_prospective(
            {"mode": "prospective_demo", "n_nights": 20, "seed": 9},
            progress.append,
        )
        models = {row["model_id"] for row in result["summary"]}
        self.assertEqual(models, {
            "profile_baseline", "recent_adaptive", "context_candidate",
            "physical_cmi", "data_cmi", "hybrid_cmi",
            "physical_mesa", "data_mesa", "hybrid_mesa",
            "online_data_cmi", "online_hybrid_cmi",
            "online_data_mesa", "online_hybrid_mesa",
        })
        self.assertEqual(result["raw"]["aggregate"]["evaluated_nights"], 20)
        self.assertIn("not genuine future validation", result["warnings"][0])
        self.assertEqual(progress[-1]["stage"], "complete")
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_source_hashes_are_exact_verified_vendor_sources(self):
        result = adapter.run_prospective({"mode": "prospective_demo", "n_nights": 1})
        canonical = get_provenance()["sources"]
        for name in ("prospective", "online_trial", "night_core", "fusion_forecast"):
            self.assertEqual(result["provenance"]["source_sha256"][name],
                             canonical[f"{name}.py"]["sha256"])

    def test_declared_synthetic_csv_is_replayed_in_timestamp_order(self):
        source = (Path(__file__).resolve().parents[2] / "work" / "demo-personal-50d" /
                  "SYNTHETIC_50일_개인화시연" / "SYNTHETIC_개인수면_50일.csv")
        result = adapter.run_prospective({
            "mode": "prospective_demo", "csv_file": str(source),
            "input_synthetic": True,
        })
        self.assertEqual(len(result["raw"]["forecasts"]), 50)
        self.assertEqual(result["raw"]["aggregate"]["evaluated_nights"], 50)
        self.assertEqual(result["raw"]["synthetic_config"]["source"],
                         "declared_synthetic_csv")
        self.assertEqual(result["raw"]["forecasts"][0]["night_id"], "2026-09-01")
        with self.assertRaisesRegex(ValueError, "input_synthetic"):
            adapter.run_prospective({"mode": "prospective_demo", "csv_file": str(source)})

    def test_commit_is_immutable_idempotent_and_uses_system_generated_at(self):
        first = self.commit()
        forecast = first["raw"]["forecast"]
        self.assertEqual(forecast["generated_at"], self.now.isoformat())
        self.assertFalse(first["raw"]["idempotent_existing"])
        second = self.commit(records=[{
            "bed": "2026-10-19T23:00:00+09:00",
            "wake": "2026-10-20T07:00:00+09:00",
            "observed_at": "2026-10-20T08:00:00+09:00", "source": "diary",
        }])
        self.assertTrue(second["raw"]["idempotent_existing"])
        self.assertEqual(second["raw"]["forecast"], forecast)
        listed = adapter.list_forecasts(self.journal)
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["has_outcome"])

    def test_future_record_is_excluded_from_committed_feature_snapshot(self):
        future = {
            "bed": "2026-10-20T23:00:00+09:00",
            "wake": "2026-10-21T07:00:00+09:00",
            "observed_at": "2026-10-21T08:00:00+09:00", "source": "diary",
        }
        result = self.commit(records=[future])
        self.assertEqual(result["raw"]["forecast"]["feature_snapshot"]["history_count"], 0)

    def test_outcome_is_separate_append_only_and_duplicate_is_idempotent(self):
        forecast_id = self.commit()["raw"]["forecast"]["id"]
        first = self.outcome(forecast_id)
        self.assertTrue(first["raw"]["eligible"])
        self.assertFalse(first["raw"]["idempotent_existing"])
        duplicate = self.outcome(forecast_id)
        self.assertTrue(duplicate["raw"]["idempotent_existing"])
        correction = self.outcome(
            forecast_id, sleep_start="2026-10-21T00:00:00+09:00",
            observed_at="2026-10-21T08:30:00+09:00",
        )
        self.assertNotEqual(correction["raw"]["assessment"]["outcome_hash"],
                            first["raw"]["assessment"]["outcome_hash"])
        summary = adapter.run_prospective(
            {"mode": "prospective_summary", "journal_path": self.journal}
        )
        self.assertEqual(summary["raw"]["aggregate"]["evaluated_nights"], 1)
        self.assertEqual(summary["raw"]["aggregate"]["duplicate_evaluations_ignored"], 1)
        self.assertTrue(adapter.list_forecasts(self.journal)[0]["has_outcome"])

    def test_delay_and_capture_window_are_enforced_by_original_contract(self):
        forecast_id = self.commit()["raw"]["forecast"]["id"]
        invalid = self.outcome(forecast_id, observed_at="2026-10-20T20:04:00+09:00")
        self.assertFalse(invalid["raw"]["eligible"])
        self.assertEqual(invalid["raw"]["assessment"]["reason"], "outcome_not_observed_after_forecast")
        late = datetime.fromisoformat("2026-10-21T20:15:01+09:00")
        other_journal = str(Path(self.temp.name) / "late.sqlite3")
        with mock.patch.object(adapter, "_now", return_value=late):
            result = adapter.run_prospective({
                "mode": "prospective_commit", "journal_path": other_journal,
                "cutoff": "2026-10-21",
            })
        self.assertFalse(result["raw"]["forecast"]["eligible_capture"])
        self.assertEqual(result["raw"]["forecast"]["capture_reason"], "late_generation")

    def test_cancelled_demo_stops_at_safe_night_boundary(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 2

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            adapter.run_prospective(
                {"mode": "prospective_demo", "n_nights": 10}, cancelled=cancelled
            )


if __name__ == "__main__":
    unittest.main()
