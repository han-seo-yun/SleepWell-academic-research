import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import ZONE
from mobile_store import ingest, summary
from server import Application, Store
from engine import validate_profile


class App:
    def __init__(self, path):
        self.store = Store(path)
        self.lock = threading.RLock()


class Research:
    summary = {"available": False}
    records = []
    metrics = None

    def behavioral_forecast(self, *args):
        return {"available": False}


def profile():
    return validate_profile({
        "name": "mobile-test", "age": 30, "sex": "unspecified", "occupation": "worker",
        "usual_bed": "23:00", "usual_wake": "07:00", "target_wake": "07:00",
        "weekend_wake": "08:00", "usual_sleep_h": 7.5, "desired_sleep_h": 8,
        "average_steps": 5000, "exercise_min": 20, "latency_min": 15,
        "link_history": False})


def session(identity="night-1", start="2026-10-19T23:00:00+09:00",
            end="2026-10-20T07:00:00+09:00", modified="2026-10-20T07:10:00+09:00",
            stages=None):
    return {"id": identity, "modified_at": modified, "start": start, "end": end,
            "stages": stages if stages is not None else [
                {"start": start, "end": "2026-10-20T03:00:00+09:00", "stage": "light"},
                {"start": "2026-10-20T03:00:00+09:00", "end": "2026-10-20T03:20:00+09:00", "stage": "awake"},
                {"start": "2026-10-20T03:20:00+09:00", "end": end, "stage": "rem"}]}


class MobileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sleep.sqlite3"
        self.app = App(self.path)
        self.now = datetime(2026, 10, 20, 8, tzinfo=ZONE)

    def tearDown(self):
        self.temp.cleanup()

    def sync(self, sessions=None, deleted=None, now=None):
        payload = {"schema_version": 1, "sessions": sessions or []}
        if deleted is not None:
            payload["deleted_ids"] = deleted
        return ingest(self.app, "phone-1", payload, now or self.now)

    def test_import_derives_only_objective_fields_and_is_idempotent(self):
        first = self.sync([session()])
        record = self.app.store.records()[0]
        self.assertEqual(first["records_upserted"], 1)
        self.assertEqual(record["source"], "health_connect")
        self.assertEqual(record["sleep_start"], "2026-10-19T23:00:00+09:00")
        self.assertEqual(record["sleep_min"], 460)
        self.assertEqual(record["awake_min"], 20)
        self.assertIsNone(record["latency_min"])
        self.assertIsNone(record["steps"])
        observed, identity = record["observed_at"], record["record_id"]
        second = self.sync([session()], now=self.now + timedelta(hours=1))
        after = self.app.store.records()[0]
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual((after["observed_at"], after["record_id"]), (observed, identity))

    def test_stale_resend_does_not_refresh_session_or_record_observation(self):
        self.sync([session()])
        record_observed = self.app.store.records()[0]["observed_at"]
        with self.app.store.connect() as db:
            session_observed = db.execute(
                "SELECT observed_at FROM mobile_sleep_sessions WHERE device_id=? AND session_id=?",
                ("phone-1", "night-1")).fetchone()[0]
        stale = session(modified="2026-10-20T07:00:00+09:00")
        result = self.sync([stale], now=self.now + timedelta(hours=1))
        self.assertEqual(result["stale"], 1)
        self.assertEqual(self.app.store.records()[0]["observed_at"], record_observed)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT observed_at FROM mobile_sleep_sessions WHERE device_id=? AND session_id=?",
                ("phone-1", "night-1")).fetchone()[0], session_observed)

    def test_invalid_batch_rolls_back_before_any_mutation(self):
        bad = session("bad")
        bad["stages"][1]["start"] = "2026-10-20T02:00:00+09:00"
        with self.assertRaises(ValueError):
            self.sync([session(), bad])
        self.assertEqual(self.app.store.records(), [])
        with closing(sqlite3.connect(self.path)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='mobile_sleep_sessions'").fetchone()
        self.assertIsNone(exists)

    def test_boolean_schema_version_is_not_accepted(self):
        with self.assertRaises(ValueError):
            ingest(self.app, "phone-1", {"schema_version": True, "sessions": []}, self.now)

    def test_manual_record_is_never_overwritten_and_conflict_is_pending(self):
        manual = {"date": "2026-10-20", "bed": "2026-10-19T22:00:00+09:00",
                  "wake": "2026-10-20T06:00:00+09:00", "sleep_start": "2026-10-19T22:15:00+09:00",
                  "sleep_min": 450, "latency_min": 15, "awake_min": 15, "source": "diary",
                  "observed_at": self.now.isoformat()}
        self.app.store.save_record(manual, self.now)
        original = self.app.store.records()[0]
        result = self.sync([session()])
        self.assertEqual(self.app.store.records(), [original])
        self.assertEqual(result["conflicts_pending"], 1)

    def test_idempotent_resend_detects_manual_record_added_after_import(self):
        self.sync([session()])
        manual = {"date": "2026-10-20", "bed": "2026-10-19T22:00:00+09:00",
                  "wake": "2026-10-20T06:00:00+09:00", "sleep_start": "2026-10-19T22:15:00+09:00",
                  "sleep_min": 450, "latency_min": 15, "awake_min": 15, "source": "diary",
                  "observed_at": (self.now + timedelta(minutes=10)).isoformat()}
        self.app.store.save_record(manual, self.now + timedelta(minutes=10))
        saved = self.app.store.records()[0]
        result = self.sync([session()], now=self.now + timedelta(hours=1))
        self.assertEqual(self.app.store.records(), [saved])
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["conflicts_pending"], 1)

    def test_longest_main_sleep_wins_and_short_nap_is_not_imported(self):
        nap = session("nap", "2026-10-20T13:00:00+09:00", "2026-10-20T14:00:00+09:00",
                      "2026-10-20T14:05:00+09:00", [{"start": "2026-10-20T13:00:00+09:00",
                      "end": "2026-10-20T14:00:00+09:00", "stage": "sleeping"}])
        self.sync([nap], now=datetime(2026, 10, 20, 15, tzinfo=ZONE))
        self.assertEqual(self.app.store.records(), [])
        self.sync([session()])
        self.assertEqual(self.app.store.records()[0]["provenance"]["session_id"], "night-1")

    def test_held_summary_reports_reasons_without_exposing_sessions(self):
        no_sleep = session("awake", stages=[
            {"start": "2026-10-19T23:00:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "awake"}])
        short = session("short", "2026-10-20T12:00:00+09:00", "2026-10-20T13:00:00+09:00",
                        "2026-10-20T13:05:00+09:00", [{"start": "2026-10-20T12:00:00+09:00",
                        "end": "2026-10-20T13:00:00+09:00", "stage": "sleeping"}])
        unknown = session("unknown", stages=[
            {"start": "2026-10-19T23:00:00+09:00", "end": "2026-10-20T02:00:00+09:00", "stage": "sleeping"},
            {"start": "2026-10-20T02:00:00+09:00", "end": "2026-10-20T02:30:00+09:00", "stage": "unknown"},
            {"start": "2026-10-20T02:30:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "sleeping"}])
        gap = session("gap", stages=[
            {"start": "2026-10-19T23:00:00+09:00", "end": "2026-10-20T02:00:00+09:00", "stage": "sleeping"},
            {"start": "2026-10-20T02:30:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "sleeping"}])
        result = self.sync([no_sleep, short, unknown, gap], now=datetime(2026, 10, 20, 15, tzinfo=ZONE))
        expected = {"no_asleep_stage": 1, "short_sleep": 1, "unknown_stage": 1, "stage_gap": 1}
        self.assertEqual(result["records_held"], 4)
        self.assertEqual(result["held_reasons"], expected)
        aggregate = summary(self.app.store)
        self.assertEqual(aggregate["held_reasons"], expected)
        self.assertNotIn("sessions", aggregate)

    def test_bed_uses_first_asleep_stage_because_session_start_is_not_time_in_bed(self):
        row = session(start="2026-10-19T22:30:00+09:00", stages=[
            {"start": "2026-10-19T23:00:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "sleeping"}])
        self.sync([row])
        record = self.app.store.records()[0]
        self.assertEqual(record["bed"], "2026-10-19T23:00:00+09:00")
        self.assertEqual(record["sleep_start"], record["bed"])
        self.assertIsNone(record["latency_min"])

    def test_timezone_normalization_and_wake_date_revision_preserve_identity_and_history(self):
        utc = session(start="2026-10-19T14:00:00+00:00", end="2026-10-19T22:00:00+00:00",
                      modified="2026-10-19T22:10:00+00:00", stages=[
                          {"start": "2026-10-19T14:00:00+00:00", "end": "2026-10-19T22:00:00+00:00",
                           "stage": "sleeping"}])
        self.sync([utc])
        original = self.app.store.records()[0]
        moved = session(start="2026-10-20T15:00:00+00:00", end="2026-10-20T22:00:00+00:00",
                        modified="2026-10-20T22:10:00+00:00", stages=[
                            {"start": "2026-10-20T15:00:00+00:00", "end": "2026-10-20T22:00:00+00:00",
                             "stage": "sleeping"}])
        later = datetime(2026, 10, 21, 8, tzinfo=ZONE)
        self.sync([moved], now=later)
        current = self.app.store.records()[0]
        self.assertEqual(current["date"], "2026-10-21")
        self.assertEqual(current["record_id"], original["record_id"])
        self.assertEqual(self.app.store.record_versions(self.now)[0]["date"], "2026-10-20")

    def test_unknown_or_gapped_stages_are_quarantined(self):
        unknown = session(stages=[
            {"start": "2026-10-19T23:00:00+09:00", "end": "2026-10-20T02:00:00+09:00", "stage": "light"},
            {"start": "2026-10-20T02:00:00+09:00", "end": "2026-10-20T02:30:00+09:00", "stage": "unknown"},
            {"start": "2026-10-20T02:30:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "rem"}])
        self.sync([unknown])
        self.assertEqual(self.app.store.records(), [])

    def test_future_timestamp_rejected_and_tombstone_removes_only_imported_record(self):
        self.sync([session()])
        record = self.app.store.records()[0]
        with self.assertRaises(ValueError):
            self.sync([session("future", modified="2026-10-20T09:00:00+09:00")])
        self.assertEqual(self.app.store.records(), [record])
        result = self.sync(deleted=["night-1"], now=self.now + timedelta(minutes=30))
        self.assertEqual(result["records_deleted"], 1)
        self.assertEqual(self.app.store.records(), [])
        self.assertEqual(self.app.store.record_versions(self.now)[0]["record_id"], record["record_id"])

    def test_revision_observed_after_cutoff_does_not_rewrite_causal_history(self):
        self.sync([session()])
        cutoff = self.now + timedelta(minutes=30)
        corrected = session(modified="2026-10-20T09:00:00+09:00", stages=[
            {"start": "2026-10-19T23:30:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "sleeping"}])
        correction_time = datetime(2026, 10, 20, 10, tzinfo=ZONE)
        self.sync([corrected], now=correction_time)
        historic = self.app.store.record_versions(cutoff)
        self.assertEqual(len(historic), 1)
        self.assertEqual(historic[0]["sleep_start"], "2026-10-19T23:00:00+09:00")
        self.assertEqual(self.app.store.records()[0]["sleep_start"], "2026-10-19T23:30:00+09:00")

    def test_deleted_assessment_is_retracted_and_same_record_identity_restores_metrics(self):
        enrolled = datetime(2026, 10, 1, 9, tzinfo=ZONE)
        self.app.store.save_profile(profile(), [], enrolled)
        capture_time = datetime(2026, 10, 19, 20, 1, tzinfo=ZONE)
        application = Application(self.app.store, Research(), clock=lambda: capture_time)
        forecast = application.ensure_due(capture_time)
        self.assertTrue(forecast["eligible_capture"])
        self.app = application
        self.sync([session()])
        record_id = self.app.store.records()[0]["record_id"]
        self.assertEqual(application.prospective_state(self.now)["summary"]["evaluated_nights"], 1)

        deleted_at = self.now + timedelta(minutes=30)
        self.sync(deleted=["night-1"], now=deleted_at)
        retracted = application.prospective_state(deleted_at)
        self.assertEqual(retracted["summary"]["evaluated_nights"], 0)
        self.assertEqual(retracted["summary"]["pending_nights"], 1)
        self.assertEqual(retracted["retracted_health_connect_assessments"], 1)
        self.assertEqual(len(self.app.store.assessments()), 1)

        restored = session(modified="2026-10-20T09:00:00+09:00", stages=[
            {"start": "2026-10-19T23:15:00+09:00", "end": "2026-10-20T07:00:00+09:00", "stage": "sleeping"}])
        restored_at = datetime(2026, 10, 20, 10, tzinfo=ZONE)
        self.sync([restored], now=restored_at)
        self.assertEqual(self.app.store.records()[0]["record_id"], record_id)
        active = application.prospective_state(restored_at)
        self.assertEqual(active["summary"]["evaluated_nights"], 1)
        self.assertEqual(active["corrected_summary"]["evaluated_nights"], 1)
        self.assertEqual(active["retracted_health_connect_assessments"], 0)
        self.assertEqual(len(self.app.store.assessments()), 2)

    def test_superseded_main_sleep_retracts_old_identity_but_keeps_new_assessment(self):
        self.app.store.save_profile(profile(), [], datetime(2026, 10, 1, 9, tzinfo=ZONE))
        capture_time = datetime(2026, 10, 19, 20, 1, tzinfo=ZONE)
        application = Application(self.app.store, Research(), clock=lambda: capture_time)
        application.ensure_due(capture_time)
        self.app = application
        self.sync([session()])
        old_id = self.app.store.records()[0]["record_id"]
        longer = session("night-long", "2026-10-19T22:00:00+09:00",
                         "2026-10-20T07:00:00+09:00", "2026-10-20T08:30:00+09:00", [
                             {"start": "2026-10-19T22:00:00+09:00",
                              "end": "2026-10-20T07:00:00+09:00", "stage": "sleeping"}])
        changed_at = datetime(2026, 10, 20, 9, tzinfo=ZONE)
        self.sync([longer], now=changed_at)
        self.assertNotEqual(self.app.store.records()[0]["record_id"], old_id)
        state = application.prospective_state(changed_at)
        self.assertEqual(state["summary"]["evaluated_nights"], 1)
        self.assertEqual(state["retracted_health_connect_assessments"], 1)
        self.assertEqual(len(self.app.store.assessments()), 2)

    def test_manual_correction_same_identity_keeps_health_result_as_primary(self):
        self.app.store.save_profile(profile(), [], datetime(2026, 10, 1, 9, tzinfo=ZONE))
        capture_time = datetime(2026, 10, 19, 20, 1, tzinfo=ZONE)
        application = Application(self.app.store, Research(), clock=lambda: capture_time)
        application.ensure_due(capture_time)
        self.app = application
        self.sync([session()])
        original = self.app.store.records()[0]
        before = application.prospective_state(self.now)
        primary_before = before["summary"]["versions"][0]["arms"]["recent_adaptive"]["joint_mae_min"]

        corrected_at = datetime(2026, 10, 20, 9, tzinfo=ZONE)
        correction = dict(original, source="diary", observed_at=corrected_at.isoformat(),
                          bed="2026-10-19T23:45:00+09:00",
                          sleep_start="2026-10-19T23:45:00+09:00", sleep_min=415)
        self.app.store.save_record(correction, corrected_at)
        saved = self.app.store.records()[0]
        self.assertEqual(saved["record_id"], original["record_id"])
        self.assertEqual(saved["source"], "diary")

        state = application.prospective_state(corrected_at)
        primary_after = state["summary"]["versions"][0]["arms"]["recent_adaptive"]["joint_mae_min"]
        latest = state["corrected_summary"]["versions"][0]["arms"]["recent_adaptive"]["joint_mae_min"]
        self.assertEqual(primary_after, primary_before)
        self.assertNotEqual(latest, primary_before)
        self.assertEqual(state["summary"]["corrections_count"], 1)
        self.assertEqual(state["retracted_health_connect_assessments"], 0)


if __name__ == "__main__":
    unittest.main()
