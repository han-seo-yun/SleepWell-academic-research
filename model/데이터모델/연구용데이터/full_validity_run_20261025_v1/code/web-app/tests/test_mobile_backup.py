import copy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store
from mobile_backup import receive, snapshots, summary, audit_history, BackupConflict, ZONE

class MobileBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'backup.sqlite3')
        self.now = datetime(2026, 10, 24, 20, tzinfo=ZONE)
        self.payload = {'schema_version': 1, 'day': '2026-10-24', 'revision': 1,
                        'generated_at': self.now.isoformat(), 'participant_key': 'a'*32,
                        'data': {'profile': {'created_at': '2026-10-01T10:00:00+09:00'},
                                 'records': [], 'recommendation': None, 'forecast': None, 'outcomes': None}}
    def tearDown(self):
        self.temp.cleanup()
    def test_idempotent_retry_preserves_original_receipt(self):
        first = receive(self.store, 'phone', self.payload, self.now)
        second = receive(self.store, 'phone', self.payload, self.now + timedelta(days=1))
        self.assertTrue(first['accepted'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(summary(self.store)['versions'], 1)
        self.assertEqual(snapshots(self.store)[0]['received_at'], self.now.isoformat())
    def test_revision_conflict_and_late_older_revision_do_not_erase(self):
        newer = copy.deepcopy(self.payload); newer['revision'] = 2
        receive(self.store, 'phone', newer, self.now)
        ack = receive(self.store, 'phone', self.payload, self.now)
        self.assertEqual(ack['stored_revision'], 2)
        changed = copy.deepcopy(newer); changed['data']['outcomes'] = {'rested': 5}
        with self.assertRaises(BackupConflict): receive(self.store, 'phone', changed, self.now)
        self.assertEqual(snapshots(self.store)[0]['revision'], 2)
        self.assertEqual(summary(self.store)['versions'], 2)
    def test_does_not_overwrite_pc_profile_or_diary_and_separates_participants(self):
        receive(self.store, 'phone', self.payload, self.now)
        changed = copy.deepcopy(self.payload); changed['participant_key'] = 'b'*32
        receive(self.store, 'phone', changed, self.now)
        self.assertEqual(summary(self.store)['participants'], 2)
        self.assertIsNone(self.store.profile())
        self.assertEqual(self.store.records(), [])
    def test_invalid_backups_never_commit(self):
        candidates = []
        for key, value in [('revision',True),('day','2026-10-25'),('participant_key','../bad'),('generated_at','2026-10-24T21:00:00+09:00')]:
            p=copy.deepcopy(self.payload); p[key]=value; candidates.append(p)
        p=copy.deepcopy(self.payload); p['data']['profile']['device_token']='secret'; candidates.append(p)
        p=copy.deepcopy(self.payload); p['data']['outcomes']={'x':float('nan')}; candidates.append(p)
        p=copy.deepcopy(self.payload); p['data']['records']=[{}]*501; candidates.append(p)
        for payload in candidates:
            with self.subTest(payload=list(payload)), self.assertRaises(ValueError):
                receive(self.store,'phone',payload,self.now)
        self.assertEqual(summary(self.store)['versions'],0)
    def test_audit_history_keeps_versions_scopes_identity_and_reports_truncation(self):
        receive(self.store,'phone',self.payload,self.now)
        second=copy.deepcopy(self.payload);second['revision']=2
        receive(self.store,'phone',second,self.now+timedelta(minutes=1))
        receive(self.store,'another',self.payload,self.now)
        other=copy.deepcopy(self.payload);other['participant_key']='b'*32
        receive(self.store,'phone',other,self.now)
        history=audit_history(self.store,'phone','a'*32)
        self.assertEqual([s['revision'] for s in history['snapshots']],[1,2])
        self.assertFalse(history['truncated'])
        bounded=audit_history(self.store,'phone','a'*32,limit=1)
        self.assertTrue(bounded['truncated'])
        self.assertEqual(bounded['snapshots'][0]['revision'],1)

if __name__ == '__main__': unittest.main()
