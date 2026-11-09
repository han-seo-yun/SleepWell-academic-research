import copy
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Application, Store, Handler, capture_loop
from engine import ZONE, validate_profile, validate_record
from daily_context import validate_daily_context


class Research:
    summary = {"available": False}
    records = []
    metrics = None

    def behavioral_forecast(self, *args):
        return {"available": False}


def profile():
    return validate_profile({
        "name": "검증 사용자", "age": 25, "sex": "unspecified", "occupation": "worker",
        "usual_bed": "23:30", "usual_wake": "07:00", "target_wake": "07:00",
        "weekend_wake": "08:00", "usual_sleep_h": 7, "desired_sleep_h": 8,
        "average_steps": 6000, "exercise_min": 20, "latency_min": 20, "link_history": False})


class ProspectiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.clock = datetime(2026, 10, 20, 19, tzinfo=ZONE)
        self.store = Store(self.path)
        self.store.save_profile(profile(), [], self.clock - timedelta(days=20))
        self.app = Application(self.store, Research(), clock=lambda: self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def context(self, now, prep=30):
        return validate_daily_context({
            "night_id": "2026-10-20", "preparation_min": prep,
            "schedules": [{"kind": "class", "start": "2026-10-21T08:00",
                           "end": "2026-10-21T09:00"}],
            "naps": [{"start": "2026-10-20T13:00", "end": "2026-10-20T13:30"}],
            "caffeine": [{"at": "2026-10-20T10:00", "mg": 100}]}, now)

    def capture(self):
        self.clock = self.clock.replace(hour=20, minute=1)
        return self.app.ensure_due()

    def test_recommendation_exposure_is_first_actual_view_not_generation(self):
        identity = self.store.save_prediction({"generated_at": self.clock.isoformat()})
        self.assertEqual(self.store.recommendation_exposures(), [])
        viewed = self.clock + timedelta(minutes=10)
        self.store.mark_recommendation_viewed(identity, viewed)
        self.store.mark_recommendation_viewed(identity, viewed + timedelta(hours=1))
        self.assertEqual(self.store.recommendation_exposures(), [
            {"recommendation_id": identity, "viewed_at": viewed.isoformat()}])
        restored = Store(self.path)
        self.assertEqual(restored.recommendation_exposures(), self.store.recommendation_exposures())

    def test_recommendation_exposure_rejects_unknown_future_and_invalid_ids(self):
        identity = self.store.save_prediction({
            "generated_at": (self.clock + timedelta(hours=1)).isoformat()})
        for bad_id in (identity, 999999, True, "1", 0):
            with self.assertRaises(ValueError):
                self.store.mark_recommendation_viewed(bad_id, self.clock)
        self.assertEqual(self.store.recommendation_exposures(), [])

    def test_feedback_matches_viewed_plan_before_newer_unviewed_plan(self):
        plan = {"generated_at": self.clock.isoformat(),
                "bed": "2026-10-20T23:00:00+09:00", "wake": "2026-10-21T07:00:00+09:00"}
        viewed_id = self.store.save_prediction(plan)
        self.store.mark_recommendation_viewed(viewed_id, self.clock + timedelta(minutes=5))
        self.store.save_prediction(dict(plan, generated_at=(self.clock + timedelta(hours=1)).isoformat()))
        self.store.save_record(self.outcome(), self.clock)
        feedback = self.app.state()["feedback"][0]
        self.assertEqual(feedback["recommendation_id"], viewed_id)
        self.assertTrue(feedback["exposure_before_sleep"])

    def outcome(self, bed_hour=23):
        self.clock = datetime(2026, 10, 21, 8, tzinfo=ZONE)
        return validate_record({"bed": f"2026-10-20T{bed_hour}:00",
                                "wake": "2026-10-21T07:00", "latency_min": 10},
                               self.clock)

    def test_cutoff_profile_context_and_immutable_capture_survive_restart(self):
        self.store.save_context(self.context(self.clock))
        self.assertIsNone(self.app.ensure_due())
        original = self.capture()
        self.assertTrue(original["eligible_capture"])
        self.assertEqual(original["context_snapshot"]["preparation_min"], 30)
        self.clock = self.clock.replace(hour=21)
        self.store.save_context(self.context(self.clock, prep=90))
        changed = profile()
        changed["usual_bed"] = "01:30"
        self.store.save_profile(changed, [], self.clock)
        self.assertEqual(self.app.ensure_due(), original)
        restored = Application(Store(self.path), Research(), clock=lambda: self.clock)
        self.assertEqual(restored.ensure_due(), original)
        self.assertEqual(len(self.store.prospective_forecasts()), 1)
        self.assertEqual(self.store.context_for("2026-10-20")["preparation_min"], 90)
        json.dumps(restored.state(), allow_nan=False)

    def test_identity_change_starts_new_enrollment_and_cannot_evaluate_old_forecast(self):
        original = self.capture()
        old_profile = self.store.profile()
        preferences = dict(old_profile, usual_bed="00:00")
        self.store.save_profile(preferences, [], self.clock)
        self.assertEqual(self.store.profile()["enrollment_id"], old_profile["enrollment_id"])
        self.clock += timedelta(minutes=1)
        self.store.save_profile(dict(preferences, name="다른 참여자"), [], self.clock)
        current = self.store.profile()
        self.assertNotEqual(current["enrollment_id"], old_profile["enrollment_id"])
        self.assertEqual(current["created_at"], self.clock.isoformat())
        self.store.save_record(self.outcome(), self.clock)
        self.assertEqual(self.store.assessments()[0]["reason"], "different_enrollment")
        self.assertEqual(self.app.prospective_state(self.clock)["primary_diary_summary"]["evaluated_nights"], 0)
        self.assertEqual(self.store.prospective_forecasts()[0], original)

    def test_ineligible_first_diary_remains_primary_after_valid_correction(self):
        self.capture()
        record = self.outcome()
        record['observed_at'] = '2026-10-21T06:00:00+09:00'
        self.store.save_record(record, self.clock)
        self.assertFalse(self.store.assessments()[0]['eligible'])
        corrected = self.outcome()
        self.store.save_record(corrected, self.clock)
        state = self.app.prospective_state(self.clock)
        self.assertEqual(state['primary_diary_summary']['evaluated_nights'], 0)
        self.assertEqual(state['corrected_summary']['evaluated_nights'], 1)

    def test_late_first_open_excluded_and_no_missed_night_backfill(self):
        self.clock = self.clock.replace(hour=21)
        forecast = self.app.ensure_due()
        self.assertFalse(forecast["eligible_capture"])
        self.store.save_record(self.outcome(), self.clock)
        state = self.app.prospective_state(self.clock)["summary"]
        self.assertEqual(state["evaluated_nights"], 0)
        self.clock = datetime(2026, 10, 24, 19, tzinfo=ZONE)
        self.assertIsNone(self.app.ensure_due())
        self.assertEqual(len(self.store.prospective_forecasts()), 1)
        self.clock = self.clock.replace(hour=20)
        self.app.ensure_due()
        self.assertEqual([r["night_id"] for r in self.store.prospective_forecasts()],
                         ["2026-10-20", "2026-10-24"])

    def test_new_profile_after_cutoff_cannot_be_backdated(self):
        path = Path(self.temp.name) / "late.sqlite3"
        store = Store(path)
        self.clock = self.clock.replace(hour=20, minute=5)
        store.save_profile(profile(), [], self.clock)
        app = Application(store, Research(), clock=lambda: self.clock)
        self.assertIsNone(app.ensure_due())
        self.clock += timedelta(days=1)
        self.assertTrue(app.ensure_due()["eligible_capture"])

    def test_first_outcome_and_correction_have_separate_metrics(self):
        forecast = self.capture()
        record = self.outcome()
        self.store.save_record(record, self.clock)
        primary = copy.deepcopy(self.app.prospective_state(self.clock))
        self.assertEqual(primary["summary"]["evaluated_nights"], 1)
        first_error = primary["summary"]["versions"][0]["arms"]["recent_adaptive"]["joint_mae_min"]
        self.clock += timedelta(hours=1)
        corrected = validate_record(dict(record, bed="2026-10-21T01:00"), self.clock)
        self.store.save_record(corrected, self.clock)
        updated = self.app.prospective_state(self.clock)
        self.assertEqual(updated["summary"]["evaluated_nights"], 1)
        self.assertEqual(updated["summary"]["versions"][0]["arms"]["recent_adaptive"]["joint_mae_min"], first_error)
        self.assertNotEqual(updated["corrected_summary"]["versions"][0]["arms"]["recent_adaptive"]["joint_mae_min"], first_error)
        self.assertEqual(updated["summary"]["corrections_count"], 1)
        self.assertEqual(self.store.prospective_forecasts()[0], forecast)
        self.assertEqual(len(self.store.assessments()), 2)

    def test_sleep_already_started_at_generation_is_excluded_not_pending(self):
        self.capture()
        self.clock = datetime(2026, 10, 21, 8, tzinfo=ZONE)
        record = validate_record({"bed": "2026-10-20T20:00", "wake": "2026-10-21T07:00",
                                  "latency_min": 0}, self.clock)
        self.store.save_record(record, self.clock)
        summary = self.app.prospective_state(self.clock)["summary"]
        self.assertEqual(summary["evaluated_nights"], 0)
        self.assertEqual(summary["pending_nights"], 0)
        self.assertEqual(summary["excluded_nights"], 1)

    def test_concurrent_capture_is_single_insert(self):
        self.clock = self.clock.replace(hour=20, minute=1)
        errors = []
        def run():
            try:
                self.app.ensure_due()
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=run) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.prospective_forecasts()), 1)

    def test_wake_date_edit_preserves_episode_and_causal_history(self):
        first_forecast = self.capture()
        self.store.save_record(self.outcome(), self.clock)
        original = self.store.records()[0]
        self.clock = datetime(2026, 10, 21, 20, tzinfo=ZONE)
        second_forecast = self.app.ensure_due()
        before_edit = self.clock
        self.clock = datetime(2026, 10, 22, 8, tzinfo=ZONE)
        correction = validate_record({
            "replaces_date": original["date"], "bed": "2026-10-21T23:00",
            "wake": "2026-10-22T07:00", "latency_min": 10,
            "record_id": "client-cannot-change-identity"}, self.clock)
        self.store.save_record(correction, self.clock)
        current = self.store.records()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["record_id"], original["record_id"])
        self.assertEqual(current[0]["date"], "2026-10-22")
        assessments = self.store.assessments()
        self.assertEqual([r["forecast_id"] for r in assessments], [first_forecast["id"]] * 2)
        self.assertFalse(assessments[-1]["eligible"])
        self.assertEqual(assessments[-1]["reason"], "different_target_night")
        self.assertEqual(self.store.record_versions(before_edit)[0]["date"], original["date"])
        self.assertEqual(len(self.store.record_versions(self.clock)), 1)
        self.assertEqual(self.store.record_versions(self.clock)[0]["date"], "2026-10-22")
        self.assertEqual(self.store.prospective_forecasts(), [first_forecast, second_forecast])
        summary = self.app.prospective_state(self.clock)
        self.assertEqual(summary["summary"]["evaluated_nights"], 1)
        self.assertEqual(summary["corrected_summary"]["evaluated_nights"], 0)
        self.assertEqual(summary["corrected_summary"]["pending_nights"], 1)

    def test_explicit_edit_rejects_missing_source_and_date_collision(self):
        self.store.save_record(self.outcome(), self.clock)
        self.clock += timedelta(days=1)
        second = validate_record({"bed": "2026-10-21T23:00", "wake": "2026-10-22T07:00"}, self.clock)
        self.store.save_record(second, self.clock)
        with self.assertRaises(ValueError):
            self.store.save_record(dict(second, replaces_date="2026-10-21"), self.clock)
        with self.assertRaises(ValueError):
            self.store.save_record(dict(second, replaces_date="2026-10-01"), self.clock)
        self.assertEqual(len(self.store.records()), 2)

    def test_additive_migration_keeps_legacy_records_and_predictions(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE records(date TEXT PRIMARY KEY,data TEXT NOT NULL)")
            db.execute("CREATE TABLE predictions(id INTEGER PRIMARY KEY AUTOINCREMENT,generated_at TEXT NOT NULL,data TEXT NOT NULL)")
            db.execute("INSERT INTO records VALUES(?,?)", ("2020-01-01", '{"source":"archive","date":"2020-01-01"}'))
            db.execute("INSERT INTO predictions(generated_at,data) VALUES(?,?)", ("2020-01-01", '{"bed":"2020-01-01"}'))
        migrated = Store(path)
        self.assertEqual(len(migrated.records()), 1)
        self.assertEqual(len(migrated.predictions()), 1)
        self.assertEqual(migrated.prospective_forecasts(), [])

    def test_timer_captures_without_browser_request(self):
        self.clock = self.clock.replace(hour=20)
        class StopAfterTick:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True
        capture_loop(self.app, StopAfterTick())
        self.assertEqual(len(self.store.prospective_forecasts()), 1)

    def test_http_context_versions_export_and_server_time(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.application = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        def call(path, data=None):
            headers = {"Content-Type": "application/json", "X-SleepWell-Token": self.app.token}
            request = Request(root + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
            with urlopen(request) as response:
                return json.load(response)
        try:
            payload = self.context(self.clock)
            payload["observed_at"] = "2000-01-01T00:00+09:00"
            saved = call("/api/context", payload)
            self.assertEqual(saved["daily_context"]["observed_at"], self.clock.isoformat())
            self.assertEqual(call("/api/context?night_id=2026-10-20")["context"]["schedules"][0]["kind"], "class")
            self.clock = self.clock.replace(hour=20, minute=1)
            call("/api/state")
            record = self.outcome()
            result = call("/api/record", record)
            self.assertEqual(result["prospective"]["summary"]["evaluated_nights"], 1)
            exported = call("/api/export")
            self.assertEqual(len(exported["prospective_forecasts"]), 1)
            self.assertEqual(len(exported["prospective_assessments"]), 1)
            self.assertEqual(len(exported["daily_context_versions"]), 1)
            with self.assertRaises(HTTPError) as caught:
                call("/api/context", {"night_id": "not-a-date"})
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(len(self.store.contexts()), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
