import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store
from research_lab_import import AppLabBridge
from research_lab import evaluate_run


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'app.sqlite')
        with self.store.connect() as db:
            db.execute('INSERT INTO profile VALUES(1,?)', (json.dumps({'enrollment_id':'current'}),))
        self.bridge = AppLabBridge(self.store, 'http://127.0.0.1:12345/', 'test')

    def record(self, **changes):
        return dict(date='2026-01-02', bed='2026-01-01T23:00:00+09:00',
                    wake='2026-01-02T07:00:00+09:00', latency_min=20,
                    observed_at='2026-01-02T08:00:00+09:00', **changes)

    def test_pc_latency_identity_and_read_only_snapshot(self):
        with self.store.connect() as db:
            rows = [self.record(enrollment_id='current'),
                    self.record(enrollment_id='previous') | {'date':'2026-01-03'},
                    self.record(enrollment_id='current', provenance={'device_id':'phone'}) | {'date':'2026-01-04'}]
            db.executemany('INSERT INTO records VALUES(?,?)', [(r['date'], json.dumps(r)) for r in rows])
        before = self.store.path.read_bytes()
        snapshot = self.bridge.snapshot()
        self.assertEqual(len(snapshot['payload']['records']), 1)
        self.assertEqual(snapshot['payload']['records'][0]['sleep_start'], '2026-01-01T23:20:00+09:00')
        self.assertEqual(self.store.path.read_bytes(), before)
        evaluate_run(snapshot['payload'])
        with self.assertRaises(ValueError):
            self.bridge.snapshot('phone-does-not-exist')

    def test_context_replacement_removes_old_events(self):
        self.store.save_record(self.record())
        for caffeine in [[{'at':'2026-01-01T15:00:00+09:00', 'mg':100}], []]:
            self.store.save_context(dict(night_id='2026-01-01', observed_at='2026-01-01T19:00:00+09:00',
                                         caffeine=caffeine, naps=[], schedules=[]))
        self.assertEqual(self.bridge.snapshot()['payload']['context'], [])

    def test_context_is_not_reassigned_after_enrollment_change(self):
        self.store.save_context(dict(night_id='2026-01-01', observed_at='2026-01-01T19:00:00+09:00',
                                    caffeine=[{'at':'2026-01-01T15:00:00+09:00','mg':100}], naps=[], schedules=[]))
        with self.store.connect() as db:
            db.execute('UPDATE profile SET data=?', (json.dumps({'enrollment_id':'next-person'}),))
        self.store.save_record(self.record())
        snapshot = self.bridge.snapshot()
        self.assertEqual(len(snapshot['payload']['records']), 1)
        self.assertEqual(snapshot['payload']['context'], [])

    def test_phone_snapshot_first_receipt_and_removal(self):
        from mobile_backup import initialize
        initialize(self.store)
        raw = {'id':'manual:2026-01-02', 'source':'manual', 'bedtime':'2026-01-01T23:00:00+09:00',
               'wake_time':'2026-01-02T07:00:00+09:00'}
        with self.store.connect() as db:
            for revision, received in [(1,'2026-01-03T08:00:00+09:00'), (2,'2026-01-04T08:00:00+09:00')]:
                payload = json.dumps({'data': {'records':[raw]}})
                db.execute('INSERT INTO mobile_daily_backups VALUES(?,?,?,?,?,?,?,?)',
                           ('device','participant','2026-01-03',revision,received,received,'test',payload))
        source = self.bridge.session()['sources'][1]['id']
        snapshot = self.bridge.snapshot(source)
        self.assertEqual(snapshot['payload']['records'][0]['observed_at'], '2026-01-03T08:00:00+09:00')
        self.assertNotIn('device', json.dumps(self.bridge.session()))
        with self.store.connect() as db:
            db.execute('UPDATE mobile_daily_backups SET payload=? WHERE revision=2', (json.dumps({'data':{'records':[]}}),))
        self.assertEqual(self.bridge.snapshot(source)['payload']['records'], [])

    def test_phone_context_uses_pc_availability_and_retains_typed_provenance(self):
        from datetime import datetime, timedelta, timezone
        from mobile_backup import initialize
        from mobile_context_store import ingest

        device = "device-context"
        participant_key = "a" * 32
        cutoff = datetime(2026, 10, 26, 11, tzinfo=timezone.utc)
        with self.store.connect() as db:
            db.execute("CREATE TABLE mobile_devices(id TEXT PRIMARY KEY,last_sync TEXT)")
            db.execute("INSERT INTO mobile_devices VALUES(?,NULL)", (device,))
        initialize(self.store)
        sleep_record = {
            "id": "manual:2026-10-27", "source": "manual",
            "bedtime": "2026-10-26T23:00:00+09:00",
            "wake_time": "2026-10-27T07:00:00+09:00",
        }
        backup_received = "2026-10-27T08:00:00+09:00"
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO mobile_daily_backups VALUES(?,?,?,?,?,?,?,?)",
                (device, participant_key, "2026-10-27", 1, backup_received,
                 backup_received, "test", json.dumps({"data": {"records": [sleep_record]}})),
            )

        def send(event, received):
            payload = {
                "schema_version": 2,
                "participant_key": participant_key,
                "generated_at": received.isoformat(),
                "range_start": (received - timedelta(days=30)).isoformat(),
                "range_end": received.isoformat(),
                "complete_scopes": [],
                "events": [event],
                "cleared_groups": [],
            }
            ingest(self.store, device, payload, received)

        schedule_saved = cutoff - timedelta(minutes=35)
        send({
            "id": "manual:2026-10-26:schedule:v1", "group_id": None,
            "kind": "schedule", "start": "2026-10-27T07:00:00+09:00", "end": None,
            "value": None, "modified_at": schedule_saved.isoformat(),
            "observed_at": schedule_saved.isoformat(), "source": "manual_android",
            "details": {"night_id": "2026-10-26", "schedule_kind": "class",
                        "preparation_minutes": 45},
        }, cutoff - timedelta(minutes=30))
        send({
            "id": "manual:2026-10-26:caffeine:exact", "group_id": None,
            "kind": "caffeine", "start": "2026-10-26T17:00:00+09:00", "end": None,
            "value": 60, "modified_at": "2026-10-26T17:05:00+09:00",
            "observed_at": "2026-10-26T17:05:00+09:00", "source": "manual_android",
            "details": {"night_id": "2026-10-26"},
        }, cutoff)
        send({
            "id": "manual:2026-10-26:caffeine:late", "group_id": None,
            "kind": "caffeine", "start": "2026-10-26T18:00:00+09:00", "end": None,
            "value": 80, "modified_at": "2026-10-26T18:05:00+09:00",
            "observed_at": "2026-10-26T18:05:00+09:00", "source": "manual_android",
            "details": {"night_id": "2026-10-26"},
        }, cutoff + timedelta(minutes=10))

        source = self.bridge.session()["sources"][1]["id"]
        snapshot = self.bridge.snapshot(source)
        canonical = {row["kind"] + ":" + row["start"]: row
                     for row in snapshot["payload"]["context"]}
        schedule_key = "schedule:2026-10-27T07:00:00+09:00"
        late_key = "caffeine:2026-10-26T18:00:00+09:00"
        self.assertEqual(datetime.fromisoformat(canonical[schedule_key]["observed_at"]), cutoff - timedelta(minutes=30))
        self.assertEqual(datetime.fromisoformat(canonical[late_key]["observed_at"]), cutoff + timedelta(minutes=10))

        provenance = {row["event_id"]: row for row in snapshot["context_provenance"]}
        schedule_provenance = provenance["manual:2026-10-26:schedule:v1"]
        self.assertEqual(schedule_provenance["details"], {
            "night_id": "2026-10-26", "schedule_kind": "class",
            "preparation_minutes": 45,
        })
        self.assertEqual(schedule_provenance["occurrence_start"],
                         "2026-10-26T22:00:00+00:00")
        self.assertEqual(schedule_provenance["source_modified_at"],
                         schedule_saved.isoformat())
        self.assertEqual(schedule_provenance["version_received_at"],
                         (cutoff - timedelta(minutes=30)).isoformat())
        self.assertEqual(schedule_provenance["participant_id"], source)
        self.assertNotIn("device_id", schedule_provenance)
        self.assertNotIn("participant_key", schedule_provenance)

        evaluated = evaluate_run(snapshot["payload"])
        features = evaluated["context_features"][0]
        self.assertEqual(features["event_counts"], {"schedule": 1})
        self.assertIsNone(features["caffeine_mg"])
        self.assertEqual(features["unavailable_events"], 2)

    def test_pc_onset_requires_explicit_start_or_valid_latency(self):
        base = self.record(enrollment_id='current')
        missing = base | {
            'date': '2026-01-02',
            'sleep_min': 480,
        }
        missing.pop('latency_min')
        zero = base | {
            'date': '2026-01-03',
            'bed': '2026-01-02T23:00:00+09:00',
            'wake': '2026-01-03T07:00:00+09:00',
            'observed_at': '2026-01-03T08:00:00+09:00',
            'latency_min': 0,
        }
        explicit = base | {
            'date': '2026-01-04',
            'bed': '2026-01-03T23:00:00+09:00',
            'sleep_start': '2026-01-03T23:25:00+09:00',
            'wake': '2026-01-04T07:00:00+09:00',
            'observed_at': '2026-01-04T08:00:00+09:00',
        }
        explicit.pop('latency_min')
        with self.store.connect() as db:
            db.executemany(
                'INSERT INTO records VALUES(?,?)',
                [(row['date'], json.dumps(row)) for row in (missing, zero, explicit)],
            )

        snapshot = self.bridge.snapshot()
        starts = {row['sleep_start'] for row in snapshot['payload']['records']}
        self.assertEqual(starts, {
            '2026-01-02T23:00:00+09:00',
            '2026-01-03T23:25:00+09:00',
        })
        self.assertNotIn('2026-01-01T23:00:00+09:00', starts)
        self.assertTrue(any('1건은 제외' in warning for warning in snapshot['warnings']))


if __name__ == '__main__':
    unittest.main()
