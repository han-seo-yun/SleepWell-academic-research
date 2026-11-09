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
        return {"schema_version": 1, "participant_key": "a" * 32,
                "generated_at": self.now.isoformat(),
                "range_start": (self.now - timedelta(days=30)).isoformat(),
                "range_end": self.now.isoformat(), "complete_scopes": list(scopes),
                "events": events}

    def event(self, event_id="steps:1", kind="steps", value=100, modified=None, group=None):
        return {"id": event_id, "group_id": group, "kind": kind,
                "start": (self.now - timedelta(hours=2)).isoformat(),
                "end": (self.now - timedelta(hours=1)).isoformat() if kind in {"steps", "exercise", "nap"} else None,
                "value": value, "modified_at": (modified or self.now - timedelta(minutes=10)).isoformat(),
                "observed_at": None if kind in {"steps", "exercise"} else (modified or self.now - timedelta(minutes=10)).isoformat(),
                "source": "samsung_health" if kind in {"steps", "exercise"} else "manual_android"}

    def test_retry_is_idempotent_and_preserves_first_receipt(self):
        payload = self.payload([self.event()], ["steps"])
        self.assertEqual(ingest(self.store, "device-1", payload, self.now)["accepted"], 1)
        later = self.now + timedelta(minutes=1)
        self.assertEqual(ingest(self.store, "device-1", payload, later)["unchanged"], 1)
        row = list_events(self.store)[0]
        self.assertEqual(row["observed_at"], self.now.isoformat())
        self.assertEqual(summary(self.store)["by_kind"]["steps"], 1)

        changed = self.event(value=125, modified=self.now + timedelta(seconds=30))
        ingest(self.store, "device-1", self.payload([changed], ["steps"]), later)
        self.assertEqual(list_events(self.store)[0]["observed_at"], later.isoformat())

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
        self.assertEqual(list_events(self.store)[0]['observed_at'], self.now.isoformat())
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


if __name__ == "__main__":
    unittest.main()
