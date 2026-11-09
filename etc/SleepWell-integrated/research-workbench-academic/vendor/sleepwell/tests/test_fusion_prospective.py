import copy
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from night_core import CONTRACT, local_time
from prospective import (BASE_ARMS, FUSION_ARMS, build_forecast, evaluate_forecast,
                         summarize_evaluations)


def profile():
    return {"name": "P", "age": 30, "sex": "unspecified", "occupation": "worker",
            "usual_bed": "23:30", "usual_wake": "07:00", "target_wake": "07:00",
            "weekend_wake": "08:00", "usual_sleep_h": 7.5, "desired_sleep_h": 8,
            "average_steps": 5000, "exercise_min": 30, "latency_min": 20,
            "activity_hours_per_day": 1, "link_history": False,
            "created_at": "2026-09-01T10:00:00+09:00"}


def fusion(artifact="a" * 64):
    offsets = {
        "physical_cmi": (250, 680), "data_cmi": (240, 690), "hybrid_cmi": (245, 685),
        "physical_mesa": (260, 700), "data_mesa": (250, 695), "hybrid_mesa": (255, 690),
    }
    cutoff = local_time("2026-10-20T20:00:00+09:00")
    predictions = {}
    for arm, (bed, wake) in offsets.items():
        predictions[arm] = {
            "bed": (cutoff + timedelta(minutes=bed)).isoformat(),
            "wake": (cutoff + timedelta(minutes=wake)).isoformat(),
            "onset_offset_min": bed, "wake_offset_min": wake, "interval_min": wake - bed,
        }
    return {"available": True, "predictions": predictions, "model_version": "fusion-2026-10",
            "feature_contract": CONTRACT, "artifacts": {"bundle_sha256": artifact},
            "feature_snapshot": {"history_count": 0},
            "research_metrics": {"cmi_joint_mae_min": 49.23, "mesa_joint_mae_min": 68.24}}


def outcome(source="diary"):
    return {"sleep_start": "2026-10-20T23:50:00+09:00",
            "wake": "2026-10-21T07:10:00+09:00", "source": source,
            "observed_at": "2026-10-21T08:00:00+09:00"}


class FusionProspectiveTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = local_time("2026-10-20T20:00:00+09:00")
        self.generated = local_time("2026-10-20T20:05:00+09:00")

    def build(self, fusion_result=None):
        return build_forecast(profile(), [], {}, self.cutoff, self.generated,
                              {"available": False}, fusion_result)

    def test_base_fallback_remains_three_arms_and_malformed_fusion_is_bounded(self):
        base = self.build()
        self.assertEqual(set(base["predictions"]), BASE_ARMS)
        self.assertTrue(evaluate_forecast(base, outcome())["eligible"])
        corrupt = fusion()
        corrupt["predictions"]["data_cmi"]["bed"] = "2026-10-22T10:00:00+09:00"
        fallback = self.build(corrupt)
        self.assertEqual(set(fallback["predictions"]), BASE_ARMS)
        self.assertEqual(fallback["feature_snapshot"]["fusion"]["reason"],
                         "rejected_invalid_fusion_result")
        self.assertNotIn("fusion_model", fallback)

    def test_nine_arm_snapshot_is_immutable_and_requires_registered_identity(self):
        supplied = fusion()
        forecast = self.build(supplied)
        self.assertEqual(set(forecast["predictions"]), BASE_ARMS | FUSION_ARMS)
        original_hash = forecast["input_hash"]
        original_bed = forecast["predictions"]["data_cmi"]["bed"]
        supplied["predictions"]["data_cmi"]["bed"] = "2099-01-01T00:00:00+09:00"
        supplied["artifacts"]["bundle_sha256"] = "changed"
        self.assertEqual(forecast["predictions"]["data_cmi"]["bed"], original_bed)
        self.assertEqual(forecast["input_hash"], original_hash)
        self.assertTrue(evaluate_forecast(forecast, outcome())["eligible"])
        tampered = copy.deepcopy(forecast)
        tampered["fusion_model"]["artifacts"]["bundle_sha256"] = "b" * 64
        self.assertEqual(evaluate_forecast(tampered, outcome())["reason"],
                         "nonprospective_or_incompatible_forecast")

    def test_summary_does_not_pool_artifacts_and_stratifies_wearable(self):
        diary_forecast = self.build(fusion("a" * 64)); diary_forecast["id"] = 1
        wearable_forecast = self.build(fusion("b" * 64)); wearable_forecast["id"] = 2
        diary_result = evaluate_forecast(diary_forecast, outcome("diary"))
        corrected_result = evaluate_forecast(diary_forecast, outcome("health_connect"))
        wearable_result = evaluate_forecast(wearable_forecast, outcome("health_connect"))
        summary = summarize_evaluations(
            [diary_forecast, wearable_forecast],
            [diary_result, corrected_result, wearable_result])
        self.assertEqual(len(summary["versions"]), 2)
        self.assertEqual(summary["duplicate_evaluations_ignored"], 1)
        self.assertEqual(summary["outcome_source_policy"]["primary_evaluated_nights"], 1)
        self.assertEqual(summary["outcome_source_policy"]["auxiliary_evaluated_nights"], 1)
        strata = [row["source_strata"] for row in summary["versions"]]
        self.assertTrue(any("user_reported_primary" in item for item in strata))
        self.assertTrue(any("automatic_wearable_auxiliary" in item for item in strata))


if __name__ == "__main__":
    unittest.main()
