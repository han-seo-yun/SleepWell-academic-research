from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import Store
from mobile_context_store import ingest, list_events, summary


class MobileContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        with self.store.connect() as db:
            db.execute("CREATE TABLE mobile_devices(id TEXT PRIMARY KEY,last_sync TEXT)")
            db.execute("INSERT INTO mobile_devices VALUES('device-1',NULL)")
        self.now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, events, scopes=()):
        return {"schema_version": 2, "participant_key": "a" * 32,
                "generated_at": self.now.isoformat(),
                "range_start": (self.now - timedelta(days=30)).isoformat(),
                "range_end": self.now.isoformat(), "complete_scopes": list(scopes),
                "events": events, "cleared_groups": []}

    def event(self, event_id="steps:1", kind="steps", value=100, modified=None, group=None):
        details = None if kind in {"steps", "exercise"} else {"night_id": self.now.date().isoformat()}
        if kind == "schedule":
            details |= {"schedule_kind": "work", "preparation_minutes": 30}
        return {"id": event_id, "group_id": group, "kind": kind,
                "start": (self.now - timedelta(hours=2)).isoformat(),
                "end": (self.now - timedelta(hours=1)).isoformat() if kind in {"steps", "exercise", "nap"} else None,
                "value": value, "modified_at": (modified or self.now - timedelta(minutes=10)).isoformat(),
                "observed_at": None if kind in {"steps", "exercise"} else (modified or self.now - timedelta(minutes=10)).isoformat(),
                "source": "samsung_health" if kind in {"steps", "exercise"} else "manual_android",
                "details": details}

    def test_retry_is_idempotent_and_preserves_current_version_receipt(self):
        payload = self.payload([self.event()], ["steps"])
        first = ingest(self.store, "device-1", payload, self.now)
        self.assertEqual(first["accepted"], 1)
        self.assertEqual(first["received_at"], self.now.isoformat())
        later = self.now + timedelta(minutes=1)
        self.assertEqual(ingest(self.store, "device-1", payload, later)["unchanged"], 1)
        row = list_events(self.store)[0]
        self.assertEqual(row["version_received_at"], self.now.isoformat())
        self.assertEqual(row["available_at"], self.now.isoformat())
        self.assertEqual(row["last_received_at"], later.isoformat())
        self.assertNotIn("observed_at", row)
        self.assertEqual(summary(self.store)["by_kind"]["steps"], 1)
        changed = self.event(value=125, modified=self.now + timedelta(seconds=30))
        ingest(self.store, "device-1", self.payload([changed], ["steps"]), later)
        row = list_events(self.store)[0]
        self.assertEqual(row["version_received_at"], later.isoformat())
        self.assertEqual(row["source_modified_at"], changed["modified_at"])

    def test_new_manual_version_supersedes_without_erasing_history(self):
        old_time = self.now - timedelta(minutes=20)
        new_time = self.now - timedelta(minutes=5)
        old = self.event("manual:day:caffeine:old", "caffeine", 80, old_time, "manual:day:caffeine")
        new = self.event("manual:day:caffeine:new", "caffeine", 120, new_time, "manual:day:caffeine")
        ingest(self.store, "device-1", self.payload([old]), self.now)
        result = ingest(self.store, "device-1", self.payload([old, new]), self.now + timedelta(seconds=1))
        self.assertEqual(result["superseded"], 1)
        self.assertEqual([(r["kind"], r["value"]) for r in list_events(self.store)], [("caffeine", 120.0)])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM mobile_context_events").fetchone()[0], 2)

    def test_deletion_only_for_explicitly_complete_sensor_scope(self):
        ingest(self.store, "device-1", self.payload([self.event()], ["steps"]), self.now)
        no_scope = self.payload([])
        ingest(self.store, "device-1", no_scope, self.now + timedelta(minutes=1))
        self.assertEqual(len(list_events(self.store)), 1)
        complete = self.payload([], ["steps"])
        self.assertEqual(ingest(self.store, "device-1", complete, self.now + timedelta(minutes=2))["deleted"], 1)
        self.assertEqual(list_events(self.store), [])

    def test_rejects_unknown_source_and_future_sensor(self):
        bad = self.event()
        bad["source"] = "phone"
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([bad]), self.now)
        future = self.event()
        future["start"] = (self.now + timedelta(hours=1)).isoformat()
        future["end"] = (self.now + timedelta(hours=2)).isoformat()
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([future]), self.now)
        unfinished = self.event()
        unfinished['end'] = (self.now + timedelta(minutes=1)).isoformat()
        with self.assertRaises(ValueError):
            ingest(self.store, 'device-1', self.payload([unfinished]), self.now)
        malformed = self.payload([])
        malformed["schema_version"] = True
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", malformed, self.now)

    def test_complete_window_does_not_delete_next_boundary_event(self):
        event = self.event()
        ingest(self.store, 'device-1', self.payload([event]), self.now)
        prior_window = self.payload([], ['steps'])
        prior_window['range_end'] = event['start']
        result = ingest(self.store, 'device-1', prior_window, self.now + timedelta(seconds=1))
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(list_events(self.store)[0]['available_at'], self.now.isoformat())
        malformed = self.payload([])
        malformed["complete_scopes"] = [[]]
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", malformed, self.now)

    def test_manual_clear_tombstone_prevents_stale_resurrection(self):
        modified = self.now - timedelta(minutes=10)
        old = self.event("manual:day:caffeine:v1", "caffeine", 80, modified, "manual:day:caffeine")
        ingest(self.store, "device-1", self.payload([old]), self.now)
        clear_at = self.now - timedelta(minutes=1)
        payload = self.payload([])
        payload["cleared_groups"] = [{"group_id": "manual:day:caffeine", "modified_at": clear_at.isoformat()}]
        ingest(self.store, "device-1", payload, self.now + timedelta(seconds=1))
        self.assertEqual(list_events(self.store), [])
        self.assertEqual(ingest(self.store, "device-1", self.payload([old]), self.now + timedelta(seconds=2))["stale"], 1)

    def test_empty_read_does_not_create_context_schema(self):
        before = Path(self.store.path).read_bytes()
        self.assertEqual(list_events(self.store), [])
        self.assertEqual(Path(self.store.path).read_bytes(), before)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='mobile_context_events'").fetchone())

    def test_same_device_event_id_is_isolated_by_participant(self):
        first = self.payload([self.event()])
        second = self.payload([self.event(value=200)])
        second["participant_key"] = "b" * 32
        ingest(self.store, "device-1", first, self.now)
        ingest(self.store, "device-1", second, self.now + timedelta(seconds=1))
        self.assertEqual(len(list_events(self.store, "device-1")), 2)
        self.assertEqual(list_events(self.store, "device-1", "a" * 32)[0]["value"], 100.0)
        self.assertEqual(list_events(self.store, "device-1", "b" * 32)[0]["value"], 200.0)

    def test_strict_as_of_uses_actual_current_version_pc_receipt(self):
        cutoff = datetime(2026, 10, 26, 11, tzinfo=timezone.utc)

        schedule = self.event("manual:2026-10-26:schedule:v1", "schedule", None,
                              cutoff - timedelta(minutes=35))
        schedule["start"] = datetime(2026, 10, 26, 22, tzinfo=timezone.utc).isoformat()
        schedule_payload = self.payload([schedule])
        schedule_payload["generated_at"] = (cutoff - timedelta(minutes=30)).isoformat()
        schedule_payload["range_end"] = schedule_payload["generated_at"]
        ingest(self.store, "device-1", schedule_payload, cutoff - timedelta(minutes=30))

        exact = self.event("manual:2026-10-26:caffeine:exact", "caffeine", 60,
                           cutoff - timedelta(hours=2))
        exact["start"] = (cutoff - timedelta(hours=3)).isoformat()
        exact_payload = self.payload([exact])
        exact_payload["generated_at"] = cutoff.isoformat()
        exact_payload["range_end"] = cutoff.isoformat()
        ingest(self.store, "device-1", exact_payload, cutoff)

        late = self.event("manual:2026-10-26:caffeine:late", "caffeine", 80,
                          cutoff - timedelta(hours=2))
        late["start"] = (cutoff - timedelta(hours=2)).isoformat()
        late_payload = self.payload([late])
        late_payload["generated_at"] = (cutoff + timedelta(minutes=10)).isoformat()
        late_payload["range_end"] = late_payload["generated_at"]
        ingest(self.store, "device-1", late_payload, cutoff + timedelta(minutes=10))

        eligible = {row["event_id"] for row in list_events(self.store, as_of=cutoff)}
        self.assertIn(schedule["id"], eligible)
        self.assertNotIn(exact["id"], eligible)
        self.assertNotIn(late["id"], eligible)
        rows = {row["event_id"]: row for row in list_events(self.store)}
        self.assertEqual(rows[schedule["id"]]["occurrence_start"], schedule["start"])
        self.assertEqual(rows[schedule["id"]]["available_at"],
                         (cutoff - timedelta(minutes=30)).isoformat())
        self.assertEqual(rows[late["id"]]["available_at"],
                         (cutoff + timedelta(minutes=10)).isoformat())

    def test_typed_manual_metadata_round_trip(self):
        schedule = self.event("manual:day:schedule:v1", "schedule", None,
                              self.now - timedelta(minutes=20), "manual:day:schedule")
        nap = self.event("manual:day:nap:v1", "nap", None,
                         self.now - timedelta(minutes=15), "manual:day:nap")
        caffeine = self.event("manual:day:caffeine:v1", "caffeine", 125,
                              self.now - timedelta(minutes=10), "manual:day:caffeine")
        ingest(self.store, "device-1", self.payload([schedule, nap, caffeine]), self.now)
        rows = {row["kind"]: row for row in list_events(self.store)}
        self.assertEqual(rows["schedule"]["details"], {
            "night_id": "2026-10-26", "schedule_kind": "work", "preparation_minutes": 30,
        })
        self.assertEqual(rows["nap"]["details"], {"night_id": "2026-10-26"})
        self.assertEqual(rows["caffeine"]["details"], {"night_id": "2026-10-26"})
        self.assertEqual(rows["schedule"]["device_observed_at"], schedule["observed_at"])

    def test_rejects_forged_receipt_and_invalid_schema_two_metadata(self):
        forged = self.payload([])
        forged["received_at"] = self.now.isoformat()
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", forged, self.now)
        forged_event = self.event()
        forged_event["available_at"] = self.now.isoformat()
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([forged_event]), self.now)
        version_one = self.payload([])
        version_one["schema_version"] = 1
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", version_one, self.now)

        invalid_schedule = self.event("manual:day:schedule:bad", "schedule", None)
        invalid_schedule["details"]["schedule_kind"] = "appointment"
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([invalid_schedule]), self.now)
        invalid_schedule["details"]["schedule_kind"] = "class"
        invalid_schedule["details"]["preparation_minutes"] = True
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([invalid_schedule]), self.now)
        invalid_schedule["details"] = {
            "night_id": "2026-02-30", "schedule_kind": "first", "preparation_minutes": 0,
        }
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([invalid_schedule]), self.now)

        invalid_caffeine = self.event("manual:day:caffeine:bad", "caffeine", float("nan"))
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([invalid_caffeine]), self.now)
        invalid_sensor = self.event()
        invalid_sensor["details"] = {"night_id": "2026-10-26"}
        with self.assertRaises(ValueError):
            ingest(self.store, "device-1", self.payload([invalid_sensor]), self.now)

    def test_legacy_payload_details_and_unknown_receipt_stay_unknown(self):
        ingest(self.store, "device-1", self.payload([self.event()]), self.now)
        with self.store.connect() as db:
            packed = db.execute(
                "SELECT payload FROM mobile_context_events WHERE event_id='steps:1'"
            ).fetchone()[0]
            legacy_packed = packed.replace('"details":null,', '')
            self.assertNotEqual(legacy_packed, packed)
            db.execute(
                "UPDATE mobile_context_events SET payload=? WHERE event_id='steps:1'",
                (legacy_packed,),
            )
        self.assertIsNone(list_events(self.store)[0]["details"])
        with self.store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT payload FROM mobile_context_events WHERE event_id='steps:1'"
            ).fetchone()[0], legacy_packed)
            db.execute(
                "UPDATE mobile_context_events SET first_received_at='unknown' WHERE event_id='steps:1'"
            )
        self.assertIsNone(list_events(self.store)[0]["available_at"])
        self.assertEqual(list_events(self.store, as_of=self.now + timedelta(days=1)), [])


if __name__ == "__main__":
    unittest.main()
