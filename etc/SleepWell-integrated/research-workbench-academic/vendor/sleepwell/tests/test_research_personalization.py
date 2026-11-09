import copy
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import parameters, recommend
from test_recent_personalization import NO_PRIOR, profile, record
from night_core import local_time


class ResearchPersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.now = local_time('2026-10-25T20:00:00+09:00')
        self.profile = profile(self.now - timedelta(days=40))
        self.records = [record(self.now - timedelta(days=i + 1), hours=9) for i in range(16)]

    def test_missing_fatigue_is_not_evidence_of_good_recovery(self):
        missing = copy.deepcopy(self.records)
        for row in missing:
            row['fatigue'] = None
        base = parameters(self.profile, [], self.now, population_prior=NO_PRIOR)
        result = parameters(self.profile, missing, self.now, population_prior=NO_PRIOR)
        self.assertEqual(result['target_sleep_min'], base['target_sleep_min'])
        self.assertEqual(result['field_effective_records']['sleep_target'], 0)
        self.assertGreater(parameters(self.profile, self.records, self.now,
                                     population_prior=NO_PRIOR)['target_sleep_min'], base['target_sleep_min'])

    def test_wearable_does_not_displace_diary_or_change_recommendation(self):
        devices = copy.deepcopy(self.records)
        for row in devices:
            row['source'] = 'health_connect'
            row['sleep_min'] = 90
            row['observed_at'] = self.now.isoformat()
        expected = recommend(self.profile, self.records, self.now, population_prior=NO_PRIOR)
        actual = recommend(self.profile, self.records + devices, self.now, population_prior=NO_PRIOR)
        self.assertEqual(actual, expected)

    def test_stale_history_mass_decays_without_claiming_more_precision(self):
        initial = parameters(self.profile, self.records, self.now, population_prior=NO_PRIOR)
        stale = parameters(self.profile, self.records, self.now + timedelta(days=56), population_prior=NO_PRIOR)
        self.assertAlmostEqual(stale['decayed_record_mass'], initial['decayed_record_mass'] / 16)
        self.assertAlmostEqual(stale['kish_effective_n'], initial['kish_effective_n'])
        self.assertLess(stale['personalization_weight'], initial['personalization_weight'])
        self.assertIsNone(stale['uncertainty']['physiological_interval'])


if __name__ == '__main__':
    unittest.main()
