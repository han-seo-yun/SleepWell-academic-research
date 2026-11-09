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


if __name__ == '__main__':
    unittest.main()
