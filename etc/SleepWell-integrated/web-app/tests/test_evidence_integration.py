"""Synthetic integration checks; these are not prospective or clinical evidence."""
import copy
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily_context import validate_daily_context
from engine import ZONE, parameters, validate_profile, validate_record
from server import Application, Store


class Research:
    summary = {"available": False}
    records = []
    metrics = None

    def behavioral_forecast(self, *args):
        return {"available": False}


def profile():
    return validate_profile({
        "name": "합성 검증", "age": 25, "sex": "unspecified", "occupation": "worker",
        "usual_bed": "23:00", "usual_wake": "07:00", "target_wake": "07:00",
        "weekend_wake": "07:00", "usual_sleep_h": 8, "desired_sleep_h": 8,
        "average_steps": 5000, "exercise_min": 20, "latency_min": 15,
        "link_history": False,
    })


class EvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.now = datetime(2026, 10, 27, 12, tzinfo=ZONE)
        self.store = Store(self.path)
        self.store.save_profile(profile(), [], self.now - timedelta(days=40))
        self.app = Application(self.store, Research(), clock=lambda: self.now)

    def test_old_derived_sleep_is_not_exposed_or_rewritten(self):
        record = validate_record({"bed": "2026-10-26T23:00", "wake": "2026-10-27T07:00"}, self.now)
        record["sleep_min"] = 480  # Simulate an old stored, incorrectly derived value.
        self.store.save_record(record, self.now)
        original = copy.deepcopy(self.store.records())
        shown = self.app.visible_records(self.store.profile())[0]
        self.assertEqual(shown["time_in_bed_min"], 480)
        self.assertIsNone(shown["sleep_min"])
        self.assertIsNone(shown["sleep_start"])
        self.assertEqual(shown["duration_status"], "missing_latency_and_awake")
        self.assertEqual(self.store.records(), original)

    def test_personalization_rejects_other_profile_and_pre_wake_reports(self):
        current = self.store.profile()
        valid = validate_record({"bed": "2026-10-26T23:00", "wake": "2026-10-27T07:00",
                                 "latency_min": 0, "awake_min": 0}, self.now)
        valid["enrollment_id"] = current["enrollment_id"]
        self.assertEqual(parameters(current, [valid], self.now)["valid_days"], 1)
        foreign = dict(valid, enrollment_id="different-profile")
        self.assertEqual(parameters(current, [foreign], self.now)["valid_days"], 0)
        impossible = dict(valid, observed_at="2026-10-27T06:00:00+09:00")
        self.assertEqual(parameters(current, [impossible], self.now)["valid_days"], 0)

    def test_exact_cutoff_revision_keeps_the_previous_available_record(self):
        cutoff = self.now.replace(hour=20)
        initial_time = cutoff - timedelta(hours=2)
        first = validate_record({"bed": "2026-10-26T23:00", "wake": "2026-10-27T07:00",
                                 "latency_min": 0, "awake_min": 0}, initial_time)
        self.store.save_record(first, initial_time)
        correction = validate_record({"bed": "2026-10-26T23:30", "wake": "2026-10-27T07:00",
                                      "latency_min": 0, "awake_min": 0}, cutoff)
        self.store.save_record(correction, cutoff)
        at_cutoff = self.store.record_versions(cutoff, strict=True)
        self.assertEqual(len(at_cutoff), 1)
        self.assertEqual(at_cutoff[0]["bed"], first["bed"])
        just_after = self.store.record_versions(cutoff + timedelta(microseconds=1), strict=True)
        self.assertEqual(len(just_after), 1)
        self.assertEqual(just_after[0]["bed"], correction["bed"])

    def test_exact_cutoff_profile_and_context_are_not_pre_cutoff(self):
        cutoff = self.now.replace(hour=20)
        old = self.store.profile()
        updated = dict(old, desired_sleep_h=9)
        self.store.save_profile(updated, [], cutoff)
        self.assertEqual(self.store.profile_at(cutoff)["desired_sleep_h"], 8)
        self.assertEqual(self.store.profile_at(cutoff + timedelta(microseconds=1))["desired_sleep_h"], 9)
        context = validate_daily_context({"night_id": cutoff.date().isoformat(), "preparation_min": 30,
                                          "schedules": [], "naps": [], "caffeine": []}, cutoff)
        self.store.save_context(context)
        self.assertIsNone(self.store.context_for(context["night_id"], cutoff, strict=True))
        self.assertIsNotNone(self.store.context_for(context["night_id"], cutoff + timedelta(microseconds=1), strict=True))

    def test_prior_policy_is_marked_without_mutating_saved_recommendation(self):
        self.store.save_prediction({"generated_at": (self.now - timedelta(hours=1)).isoformat(),
                                    "policy_version": "sleep-opportunity-v6-daily-personalization",
                                    "bed": "2026-10-27T23:00:00+09:00", "wake": "2026-10-28T07:00:00+09:00"})
        saved = copy.deepcopy(self.store.predictions())
        state = self.app.state()
        self.assertTrue(state["recommendation"]["stale_policy"])
        self.assertEqual(self.store.predictions(), saved)
        self.assertIsNone(state["recommendation_effects"]["causal_effect_estimate"])

    def test_monitoring_registration_and_insufficient_calibration_are_immutable(self):
        identity = "a" * 64
        registered = self.store.profile()
        lock, calibration = self.store.monitoring_artifacts(registered, identity, self.now)
        self.assertGreater(datetime.fromisoformat(lock["future_period_start"]), self.now)
        self.assertIsNone(calibration)
        restored = Store(self.path)
        again, _ = restored.monitoring_artifacts(registered, identity, self.now + timedelta(hours=1))
        self.assertEqual(lock, again)
        boundary = datetime.fromisoformat(lock["calibration_end"])
        same_lock, frozen = restored.monitoring_artifacts(registered, identity, boundary)
        self.assertEqual(same_lock, lock)
        self.assertIsNotNone(frozen)
        later_lock, later_frozen = restored.monitoring_artifacts(registered, identity, boundary + timedelta(days=1))
        self.assertEqual(later_lock, lock)
        self.assertEqual(later_frozen, frozen)
        other_lock, _ = restored.monitoring_artifacts(registered, "b" * 64, self.now)
        self.assertNotEqual(other_lock, lock)

    def test_expired_period_does_not_attempt_late_calibration(self):
        profile = self.store.profile()
        lock, _ = self.store.monitoring_artifacts(profile, "c" * 64, self.now)
        expired = datetime.fromisoformat(lock["heldout_end"])
        preserved, calibration = self.store.monitoring_artifacts(profile, "c" * 64, expired)
        self.assertEqual(preserved, lock)
        self.assertIsNone(calibration)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prospective_interval_calibrations").fetchone()[0], 0)

    def test_context_is_isolated_after_profile_change(self):
        old = self.store.profile()
        context = validate_daily_context({"night_id": self.now.date().isoformat(), "preparation_min": 30,
                                          "schedules": [], "naps": [], "caffeine": []}, self.now)
        self.store.save_context(context)
        self.store.save_profile(dict(old, name="다른 합성 참가자"), [], self.now + timedelta(minutes=1))
        self.assertIsNone(self.store.context_for(context["night_id"]))
        historical = self.store.context_for(context["night_id"], enrollment_id=old["enrollment_id"])
        self.assertEqual(historical["enrollment_id"], old["enrollment_id"])


if __name__ == "__main__":
    unittest.main()
