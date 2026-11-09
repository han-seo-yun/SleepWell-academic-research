import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from night_core import local_time
from prospective import (FEATURE_CONTRACT, LEGACY_FEATURE_CONTRACT, LEGACY_MODEL_VERSION,
                         MODEL_VERSION, V2_FEATURE_CONTRACT, V2_MODEL_VERSION,
                         build_forecast, evaluate_forecast, summarize_evaluations)


def profile():
    return {"name": "P", "age": 30, "sex": "unspecified", "occupation": "worker",
            "usual_bed": "23:30", "usual_wake": "07:00", "target_wake": "07:00",
            "weekend_wake": "08:00", "usual_sleep_h": 7.5, "desired_sleep_h": 8,
            "average_steps": 5000, "exercise_min": 30, "latency_min": 20,
            "activity_hours_per_day": 1, "link_history": False,
            "created_at": "2026-09-01T10:00:00+09:00"}


def record(day, observed_hour=10, bed_hour=23, latency=10):
    wake_day = day + 1
    return {"bed": f"2026-09-{day:02d}T{bed_hour:02d}:00:00+09:00",
            "wake": f"2026-09-{wake_day:02d}T07:00:00+09:00", "latency_min": latency,
            "observed_at": f"2026-09-{wake_day:02d}T{observed_hour:02d}:00:00+09:00",
            "source": "diary"}


def context(**updates):
    value = {"schema_version": 1, "night_id": "2026-10-20",
             "observed_at": "2026-10-20T19:59:00+09:00",
             "schedules": [], "preparation_min": 0, "naps": [], "caffeine": []}
    value.update(updates)
    return value


class ProspectiveTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = local_time("2026-10-20T20:00:00+09:00")
        self.generated = local_time("2026-10-20T20:15:00+09:00")

    def build(self, **kwargs):
        args = dict(profile=profile(), record_versions=[], context_snapshot={},
                    cutoff=self.cutoff, generated_at=self.generated, population_prior={"available": False})
        args.update(kwargs)
        return build_forecast(**args)

    def test_primary_window_boundary_and_no_backdating(self):
        self.assertTrue(self.build()["eligible_capture"])
        late = self.build(generated_at=local_time("2026-10-20T20:15:01+09:00"))
        self.assertFalse(late["eligible_capture"])
        self.assertEqual(late["capture_reason"], "late_generation")
        with self.assertRaises(ValueError):
            self.build(generated_at=local_time("2026-10-20T19:59:59+09:00"))
        with self.assertRaises(ValueError):
            self.build(context_snapshot=context(observed_at="2026-10-20T20:10:00+09:00"))
        with self.assertRaises(ValueError):
            self.build(context_snapshot=context(caffeine=[
                {"at": "2026-10-20T20:05:00+09:00", "mg": 80}]))
        with self.assertRaises(ValueError):
            self.build(profile=dict(profile(), created_at="2026-10-20T20:01:00+09:00"))

    def test_snapshots_and_future_records_do_not_change_forecast(self):
        history = [record(day) for day in range(1, 16)]
        snapshot = context(caffeine=[{"at": "2026-10-20T14:00:00+09:00", "mg": 80}])
        base = self.build(record_versions=history, context_snapshot=snapshot)
        future = dict(record(20), observed_at="2026-10-20T21:00:00+09:00")
        again = self.build(record_versions=history + [future], context_snapshot=snapshot)
        self.assertEqual(base["predictions"], again["predictions"])
        self.assertEqual(base["input_hash"], again["input_hash"])
        source_profile = profile()
        frozen = build_forecast(source_profile, [], {}, self.cutoff, self.generated, {"available": False})
        source_profile["usual_bed"] = "01:00"
        self.assertEqual(frozen["profile_snapshot"]["usual_bed"], "23:30")

    def test_profile_onset_and_context_schedule_contract(self):
        forecast = self.build(context_snapshot=context(
            schedules=[{"kind": "work", "start": "2026-10-21T06:30:00+09:00", "end": None}],
            preparation_min=30,
            naps=[{"start": "2026-10-20T15:00:00+09:00", "end": "2026-10-20T15:20:00+09:00"}],
            caffeine=[{"at": "2026-10-20T13:00:00+09:00", "mg": 100}]))
        baseline = forecast["predictions"]["profile_baseline"]
        self.assertEqual(baseline["bed"], "2026-10-20T23:50:00+09:00")
        self.assertLessEqual(forecast["predictions"]["context_candidate"]["wake"],
                             "2026-10-21T06:00:00+09:00")
        self.assertEqual(forecast["feature_snapshot"]["nap_and_caffeine_policy"],
                         "captured_only_no_fitted_coefficient")
        self.assertEqual(forecast["selected_model"], "recent_adaptive")
        overnight = self.build(context_snapshot=context(schedules=[{
            "kind": "work", "start": "2026-10-20T22:00:00+09:00",
            "end": "2026-10-21T02:00:00+09:00"}]))
        flags = overnight["feature_snapshot"]["context"]
        self.assertFalse(flags["schedule_applied"])
        self.assertEqual(len(flags["commitment_overlap"]), 1)
        self.assertTrue(flags["heuristic_unvalidated"])

    def test_evaluation_contract_and_same_target(self):
        forecast = self.build()
        forecast["id"] = 41
        outcome = {"bed": "2026-10-20T23:40:00+09:00", "latency_min": 10,
                   "wake": "2026-10-21T07:10:00+09:00", "source": "diary",
                   "observed_at": "2026-10-21T08:00:00+09:00"}
        result = evaluate_forecast(forecast, outcome)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["forecast_id"], 41)
        self.assertEqual(set(result["errors"]), set(forecast["predictions"]))
        self.assertEqual(result["errors"]["profile_baseline"]["onset_min"], 0)
        before = dict(outcome, observed_at="2026-10-20T20:00:00+09:00")
        self.assertEqual(evaluate_forecast(forecast, before)["reason"],
                         "outcome_not_observed_after_forecast")
        premature = dict(outcome, observed_at="2026-10-21T06:00:00+09:00")
        self.assertEqual(evaluate_forecast(forecast, premature)["reason"],
                         "outcome_observed_before_wake")
        wrong = dict(outcome, bed="2026-10-22T00:00:00+09:00", wake="2026-10-22T08:00:00+09:00",
                     observed_at="2026-10-22T09:00:00+09:00")
        self.assertEqual(evaluate_forecast(forecast, wrong)["reason"], "different_target_night")

    def test_late_and_invalid_outcomes_are_not_evaluated(self):
        late = self.build(generated_at=local_time("2026-10-20T20:16:00+09:00"))
        outcome = {"sleep_start": "2026-10-20T23:30:00+09:00",
                   "wake": "2026-10-21T07:00:00+09:00", "source": "diary",
                   "observed_at": "2026-10-21T08:00:00+09:00"}
        self.assertEqual(evaluate_forecast(late, outcome)["reason"], "late_generation")
        archive = dict(outcome, source="archive")
        self.assertEqual(evaluate_forecast(self.build(), archive)["reason"],
                         "unsupported_outcome_source")
        midnight = dict(self.build(), generated_at="2026-10-21T00:01:00+09:00",
                        eligible_capture=True)
        self.assertEqual(evaluate_forecast(midnight, outcome)["reason"], "late_generation")
        old = dict(self.build(), feature_contract="sleepwell_next_night_pre20_v1")
        self.assertEqual(evaluate_forecast(old, outcome)["reason"],
                         "nonprospective_or_incompatible_forecast")
        incomplete = copy.deepcopy(self.build())
        incomplete["predictions"].pop("context_candidate")
        self.assertEqual(evaluate_forecast(incomplete, outcome)["reason"],
                         "incomplete_prediction_arms")
        with self.assertRaises(ValueError):
            evaluate_forecast(self.build(), dict(outcome, latency_min=math.nan))

    def test_old_v1_v2_and_new_v3_are_scored_and_summarized_separately(self):
        forecast = self.build()
        forecast["id"] = 1
        outcome = {"sleep_start": "2026-10-20T23:40:00+09:00",
                   "wake": "2026-10-21T07:10:00+09:00", "source": "diary",
                   "observed_at": "2026-10-21T08:00:00+09:00"}
        first = evaluate_forecast(forecast, outcome)
        self.assertEqual((forecast["model_version"], forecast["feature_contract"]),
                         (MODEL_VERSION, FEATURE_CONTRACT))
        forecast2 = copy.deepcopy(forecast)
        forecast2.update(id=2, model_version=V2_MODEL_VERSION,
                         feature_contract=V2_FEATURE_CONTRACT)
        second = evaluate_forecast(forecast2, outcome)
        self.assertTrue(second["eligible"])
        forecast1 = copy.deepcopy(forecast)
        forecast1.update(id=3, model_version=LEGACY_MODEL_VERSION,
                         feature_contract=LEGACY_FEATURE_CONTRACT)
        third = evaluate_forecast(forecast1, outcome)
        self.assertTrue(third["eligible"])
        summary = summarize_evaluations(
            [forecast, forecast2, forecast1], [first, second, third])
        self.assertEqual({row["model_version"] for row in summary["versions"]},
                         {LEGACY_MODEL_VERSION, V2_MODEL_VERSION, MODEL_VERSION})
        v1 = next(row for row in summary["versions"]
                  if row["model_version"] == LEGACY_MODEL_VERSION)
        self.assertEqual(v1["arms"]["profile_baseline"]["n"], 1)
        self.assertIn("paired_selected_vs_profile_mae_improvement_min",
                      v1)
        self.assertEqual(summary["calibration_progress"]["eligible_nights"], 3)
        self.assertFalse(summary["calibration_progress"]["calibrated_intervals_available"])

    def test_summary_joins_deduplicates_and_separates_pending_from_excluded(self):
        outcome = {"sleep_start": "2026-10-20T23:40:00+09:00",
                   "wake": "2026-10-21T07:10:00+09:00", "source": "diary",
                   "observed_at": "2026-10-21T08:00:00+09:00"}
        evaluated = self.build(); evaluated["id"] = 1
        pending = self.build(); pending["id"] = 2
        late = self.build(generated_at=local_time("2026-10-20T20:16:00+09:00")); late["id"] = 3
        rejected = self.build(); rejected["id"] = 4
        good = evaluate_forecast(evaluated, outcome)
        duplicate = copy.deepcopy(good)
        bad = evaluate_forecast(rejected, dict(outcome, source="unknown"))
        orphan = dict(good, forecast_id=999)
        summary = summarize_evaluations(
            [evaluated, pending, late, rejected], [good, duplicate, bad, orphan])
        self.assertEqual(summary["total_forecasts"], 4)
        self.assertEqual(summary["eligible_captures"], 3)
        self.assertEqual(summary["evaluated_nights"], 1)
        self.assertEqual(summary["pending_nights"], 1)
        self.assertEqual(summary["excluded_nights"], 2)
        self.assertEqual(summary["duplicate_evaluations_ignored"], 1)
        self.assertEqual(summary["orphan_evaluations"], 1)


if __name__ == "__main__":
    unittest.main()
