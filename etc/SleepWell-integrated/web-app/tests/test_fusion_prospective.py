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


def fusion(artifact="a" * 64, cutoff=None):
    offsets = {
        "physical_cmi": (250, 680), "data_cmi": (240, 690), "hybrid_cmi": (245, 685),
        "physical_mesa": (260, 700), "data_mesa": (250, 695), "hybrid_mesa": (255, 690),
    }
    cutoff = local_time(cutoff or "2026-10-20T20:00:00+09:00")
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


class MonitoringProtocolTests(unittest.TestCase):
    def setUp(self):
        from prospective import create_monitoring_lock
        self.enrollment = "synthetic-enrollment-A"
        self.profile = dict(profile(), enrollment_id=self.enrollment)
        self.start = local_time("2026-10-20T20:00:00+09:00")
        seed = self.build_at(self.start)
        self.artifact_identity = seed["fusion_model"]["artifact_identity_sha256"]
        self.lock = create_monitoring_lock(
            "2026-10-20T18:00:00+09:00", self.enrollment,
            self.artifact_identity)

    def build_at(self, cutoff, lock=None, calibration=None, identity="a" * 64):
        cutoff = local_time(cutoff)
        return build_forecast(
            self.profile, [], {}, cutoff, cutoff + timedelta(minutes=5),
            {"available": False}, fusion(identity, cutoff),
            monitoring_lock=lock, interval_calibration=calibration)

    def assessment(self, forecast, residual=5, source="diary", assessment_id=None):
        cutoff = local_time(forecast["cutoff"])
        point = forecast["predictions"]["recent_adaptive"]
        onset = cutoff + timedelta(minutes=point["onset_offset_min"] + residual)
        wake = cutoff + timedelta(minutes=point["wake_offset_min"] + residual)
        result = evaluate_forecast(forecast, {
            "sleep_start": onset.isoformat(),
            "wake": wake.isoformat(),
            "source": source,
            "observed_at": (wake + timedelta(minutes=30)).isoformat(),
            "enrollment_id": self.enrollment,
        })
        if assessment_id is not None:
            result["assessment_id"] = assessment_id
        return result

    def calibration_rows(self, count=20):
        forecasts, assessments = [], []
        for index in range(count):
            cutoff = local_time(self.lock["future_period_start"]) + timedelta(days=index)
            forecast = self.build_at(cutoff, self.lock)
            forecast["id"] = index + 1
            forecasts.append(forecast)
            assessments.append(self.assessment(
                forecast, residual=(index % 7) + 1, assessment_id=index + 1))
        return forecasts, assessments

    def test_lock_chronology_hash_tampering_and_no_retroactive_registration(self):
        from prospective import create_monitoring_lock
        exact = create_monitoring_lock(
            "2026-10-20T20:00:00+09:00", self.enrollment,
            self.artifact_identity)
        self.assertEqual(exact["future_period_start"],
                         "2026-10-21T20:00:00+09:00")
        self.assertEqual(exact["calibration_end"],
                         "2026-11-20T20:00:00+09:00")
        self.assertEqual(exact["heldout_end"],
                         "2026-12-20T20:00:00+09:00")
        unregistered = self.build_at(self.start)
        self.assertEqual(unregistered["prediction_intervals"]["status"],
                         "not_registered")
        self.assertEqual(unregistered["prediction_intervals"]["arms"], {})
        locked = self.build_at(self.start, self.lock)
        with self.subTest(contract="persisted_v4_identity"):
            from prospective import _canonical_hash
            self.assertEqual(locked["monitoring_phase"], "calibration")
            locked["id"] = 40
            self.assertTrue(evaluate_forecast(locked, outcome())["eligible"])
            identity_tampered = copy.deepcopy(locked)
            identity_tampered["monitoring_lock_sha256"] = "0" * 64
            identity_tampered["input_hash"] = _canonical_hash({
                key: value for key, value in identity_tampered.items()
                if key not in ("id", "forecast_id", "input_hash")
            })
            self.assertEqual(
                evaluate_forecast(identity_tampered, outcome())["reason"],
                "invalid_forecast_snapshot")
        tampered = copy.deepcopy(self.lock)
        tampered["min_calibration_n"] = 19
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.build_at(self.start, tampered)
        with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
            self.build_at(self.start, self.lock, identity="b" * 64)
        after_cutoff_lock = create_monitoring_lock(
            "2026-10-20T20:01:00+09:00", self.enrollment,
            self.artifact_identity)
        preperiod = self.build_at(self.start, after_cutoff_lock)
        self.assertEqual(preperiod["monitoring_phase"], "pre_future_period")
        self.assertEqual(preperiod["prediction_intervals"]["status"],
                         "awaiting_future_data")

    def test_calibration_uses_first_timely_diary_only_and_periods_are_disjoint(self):
        from prospective import fit_interval_calibration
        forecasts, assessments = self.calibration_rows(20)
        first = assessments[0]
        late_first = copy.deepcopy(first)
        late_first["assessment_id"] = 0
        late_first["outcome_snapshot"]["observed_at"] = self.lock["calibration_end"]
        correction = copy.deepcopy(first)
        correction["assessment_id"] = 1000
        wearable = copy.deepcopy(assessments[1])
        wearable["assessment_id"] = -1
        wearable["outcome_snapshot"]["source"] = "health_connect"
        heldout = self.build_at(self.lock["calibration_end"], self.lock)
        heldout["id"] = 100
        heldout_assessment = self.assessment(heldout, assessment_id=2000)
        artifact = fit_interval_calibration(
            self.lock, [*forecasts, heldout],
            [wearable, late_first, *assessments[1:], correction,
             heldout_assessment],
            self.lock["calibration_end"])
        self.assertEqual(artifact["candidate_calibration_forecast_n"], 20)
        self.assertEqual(artifact["calibration_n"], 19)
        self.assertEqual(artifact["status"], "insufficient_calibration")
        self.assertIsNone(artifact["joint_radius_min"])
        self.assertEqual(
            artifact["exclusions"]["label_not_available_before_calibration_end"], 1)
        heldout_cutoff = local_time(self.lock["calibration_end"])
        late_issued = build_forecast(
            self.profile, [], {}, heldout_cutoff,
            heldout_cutoff + timedelta(minutes=16),
            {"available": False}, fusion("a" * 64, heldout_cutoff),
            monitoring_lock=self.lock)
        late_issued["id"] = 101
        self.assertFalse(late_issued["eligible_capture"])
        with self.assertRaisesRegex(
                ValueError, "before the first held-out forecast"):
            fit_interval_calibration(
                self.lock, [*forecasts, late_issued], assessments,
                heldout_cutoff + timedelta(minutes=17))

    def test_insufficient_calibration_is_terminal_and_emits_no_interval(self):
        from prospective import fit_interval_calibration
        forecasts, assessments = self.calibration_rows(19)
        calibration = fit_interval_calibration(
            self.lock, forecasts, assessments, self.lock["calibration_end"])
        self.assertTrue(calibration["terminal"])
        self.assertFalse(calibration["validated"])
        self.assertIsNone(calibration["joint_radius_min"])
        heldout = self.build_at(
            self.lock["calibration_end"], self.lock, calibration)
        self.assertEqual(heldout["prediction_intervals"]["status"],
                         "insufficient_calibration")
        self.assertEqual(heldout["prediction_intervals"]["arms"], {})

    def test_calibration_is_not_validation_and_heldout_coverage_uses_stored_bounds(self):
        from prospective import fit_interval_calibration, summarize_interval_monitoring
        forecasts, assessments = self.calibration_rows(20)
        calibration = fit_interval_calibration(
            self.lock, forecasts, assessments, self.lock["calibration_end"])
        self.assertEqual(calibration["status"], "calibrated_heldout_pending")
        self.assertFalse(calibration["validated"])
        heldout = self.build_at(
            self.lock["calibration_end"], self.lock, calibration)
        heldout["id"] = 500
        intervals = heldout["prediction_intervals"]
        self.assertEqual(intervals["status"], "calibrated_heldout_pending")
        self.assertEqual(set(intervals["arms"]), {"recent_adaptive"})
        pending = summarize_interval_monitoring([heldout], [])
        pending_group = pending["groups"][0]
        self.assertEqual(pending_group["status"], "pending_future_observations")
        self.assertIsNone(pending_group["source_strata"]["user_reported_primary"]
                          ["joint"]["empirical_coverage"])
        observed = self.assessment(heldout, residual=1, assessment_id=1)
        auxiliary = self.assessment(
            heldout, residual=1, source="health_connect", assessment_id=2)
        summary = summarize_interval_monitoring(
            [heldout], [observed, auxiliary])
        group = summary["groups"][0]
        self.assertEqual(group["status"], "heldout_coverage_observed")
        self.assertEqual(group["source_strata"]["user_reported_primary"]
                         ["joint"]["eligible_n"], 1)
        self.assertEqual(group["source_strata"]["automatic_wearable_auxiliary"]
                         ["joint"]["eligible_n"], 1)
        self.assertFalse(group["validated"])
        self.assertEqual(summary["recommendation_effect"]["status"],
                         "not_estimated")

    def test_mixed_calibration_identity_is_refused_not_pooled(self):
        from prospective import fit_interval_calibration, summarize_interval_monitoring
        forecasts, assessments = self.calibration_rows(20)
        first = fit_interval_calibration(
            self.lock, forecasts, assessments, self.lock["calibration_end"])
        second_frozen = (local_time(self.lock["calibration_end"])
                         + timedelta(seconds=1)).isoformat()
        second = fit_interval_calibration(
            self.lock, forecasts, assessments, second_frozen)
        first_heldout = self.build_at(
            self.lock["calibration_end"], self.lock, first)
        first_heldout["id"] = 601
        second_heldout = self.build_at(
            self.lock["calibration_end"], self.lock, second)
        second_heldout["id"] = 602
        summary = summarize_interval_monitoring(
            [first_heldout, second_heldout], [])
        self.assertEqual(summary["groups"][0]["status"],
                         "mixed_identity_refused")
        self.assertEqual(summary["groups"][0]["issued_interval_n"], 0)


if __name__ == "__main__":
    unittest.main()
