import copy
from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_lab_context import normalize_context, context_features, instant
from research_lab import evaluate_run, LabInputError


class ContextTests(unittest.TestCase):
    def event(self, kind='caffeine', start='2026-01-01T15:00:00+09:00', end=None, value=100, observed='2026-01-01T19:00:00+09:00'):
        return dict(participant_id='P1', kind=kind, start=start, end=end, value=value, observed_at=observed)

    def test_cutoff_late_receipts_future_schedule_and_missing_zero(self):
        events = normalize_context([
            self.event(value=0),
            self.event(value=200, observed='2026-01-01T21:00:00+09:00'),
            self.event(value=300, observed='2026-01-01T20:00:00+09:00'),
            self.event('schedule', '2026-01-02T09:00:00+09:00', value=None),
            self.event('exercise', '2026-01-01T17:00:00+09:00', '2026-01-01T18:00:00+09:00', None),
        ], {'P1'})
        rows = [dict(participant_id='P1', cutoff=instant('2026-01-01T20:00:00+09:00'))]
        features, summary = context_features(rows, events)
        self.assertEqual(features[0]['caffeine_mg'], 0)
        self.assertIsNone(features[0]['steps'])
        self.assertEqual(features[0]['exercise_min'], 60)
        self.assertEqual(features[0]['schedule_start_offset_min'], 780)
        self.assertEqual(features[0]['unavailable_events'], 2)
        self.assertEqual(summary['coverage']['caffeine'], 3)

    def test_step_compaction_preserves_cutoff_sum_and_availability(self):
        from research_lab_import import _compact_steps
        events = normalize_context([
            self.event('steps', '2026-01-01T15:00:00+09:00', '2026-01-01T15:10:00+09:00', 500),
            self.event('steps', '2026-01-01T15:10:00+09:00', '2026-01-01T15:20:00+09:00', 700),
            self.event('steps', '2026-01-01T15:20:00+09:00', '2026-01-01T15:30:00+09:00', 900, '2026-01-01T20:00:00+09:00'),
        ], {'P1'})
        compacted = _compact_steps(events)
        self.assertEqual(len(compacted), 2)
        rows = [dict(participant_id='P1', cutoff=instant('2026-01-01T20:00:00+09:00'))]
        before, _ = context_features(rows, events)
        after, _ = context_features(rows, compacted)
        self.assertEqual(before[0]['steps'], after[0]['steps'])
        self.assertEqual(after[0]['steps'], 1200)

    def test_contract_rejects_nonfinite_unknown_identity_and_unfinished_activity(self):
        for event in [self.event(value=float('nan')), self.event(value=True),
                      self.event() | {'participant_id':'P2'}, self.event() | {'kind': []},
                      self.event(observed='2026-01-01T14:00:00+09:00')]:
            with self.subTest(event=event), self.assertRaises((ValueError, TypeError)):
                normalize_context([event], {'P1'})
        self.assertEqual(len(normalize_context([self.event(), self.event()], {'P1'})), 1)

    def test_context_changes_run_identity_but_not_frozen_predictions(self):
        payload = dict(schema_version=1, mode='real', cohorts=['cmi'], records=[dict(
            participant_id='P1', cutoff='2026-01-01T20:00:00+09:00',
            sleep_start='2026-01-01T23:00:00+09:00', sleep_end='2026-01-02T07:00:00+09:00',
            observed_at='2026-01-02T08:00:00+09:00')])
        original = evaluate_run(payload)
        changed = evaluate_run(payload | {'context': [self.event()]})
        self.assertNotEqual(original['run_id'], changed['run_id'])
        self.assertEqual(original['predictions'], changed['predictions'])
        self.assertEqual(changed['context_features'][0]['caffeine_mg'], 100)
        with self.assertRaises(LabInputError):
            evaluate_run(payload | {'context': [self.event(value=-1)]})


if __name__ == '__main__':
    unittest.main()
