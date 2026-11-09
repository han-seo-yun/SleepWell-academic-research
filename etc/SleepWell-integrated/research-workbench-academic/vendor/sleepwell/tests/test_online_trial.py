import copy
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from night_core import local_time
from online_trial import build_online_trial, evaluate_online_trial, summarize_online_trials
from prospective import build_forecast, evaluate_forecast
from test_fusion_prospective import profile, fusion


def make_forecast(cutoff, identity='a' * 64):
    model = fusion(identity)
    for item in model['predictions'].values():
        item['bed'] = (cutoff + timedelta(minutes=item['onset_offset_min'])).isoformat()
        item['wake'] = (cutoff + timedelta(minutes=item['wake_offset_min'])).isoformat()
    return build_forecast(profile(), [], {}, cutoff, cutoff, {'available': False}, model)


class OnlineTrialTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = local_time('2026-10-25T20:00:00+09:00')
        self.forecasts, self.assessments = [], []
        for index in range(18):
            cutoff = self.cutoff - timedelta(days=18 - index)
            forecast = make_forecast(cutoff)
            forecast['id'] = index + 1
            outcome = {'source': 'diary', 'sleep_start': (cutoff + timedelta(minutes=270)).isoformat(),
                       'wake': (cutoff + timedelta(minutes=720)).isoformat(),
                       'observed_at': (cutoff + timedelta(minutes=780)).isoformat()}
            result = evaluate_forecast(forecast, outcome)
            result['assessment_id'] = index + 1
            self.forecasts.append(forecast)
            self.assessments.append(result)
        self.current = make_forecast(self.cutoff)

    def test_only_past_first_reports_same_identity_and_enrollment_are_used(self):
        actual = build_online_trial(self.current, self.forecasts, self.assessments)
        self.assertEqual(actual['adjustments']['data_cmi']['eligible_count'], 18)
        self.assertTrue(actual['adjustments']['data_cmi']['active'])
        self.assertGreater(actual['predictions']['data_cmi']['onset_offset_min'], 240)
        correction = copy.deepcopy(self.assessments[0])
        correction['assessment_id'] = 100
        correction['outcome_snapshot']['onset_offset_min'] = 900
        self.assertEqual(actual, build_online_trial(self.current, self.forecasts, self.assessments + [correction]))
        other = make_forecast(self.cutoff, 'b' * 64)
        self.assertEqual(build_online_trial(other, self.forecasts, self.assessments)['adjustments']['data_cmi']['eligible_count'], 0)
        other = copy.deepcopy(self.current)
        other['profile_snapshot']['created_at'] = '2026-10-01T00:00:00+09:00'
        self.assertEqual(build_online_trial(other, self.forecasts, self.assessments)['adjustments']['data_cmi']['eligible_count'], 0)
        other = copy.deepcopy(self.current)
        other['profile_snapshot']['name'] = 'different participant'
        self.assertEqual(build_online_trial(other, self.forecasts, self.assessments)['adjustments']['data_cmi']['eligible_count'], 0)
        other = copy.deepcopy(self.current)
        other['profile_snapshot']['enrollment_id'] = 'new-enrollment'
        self.assertEqual(build_online_trial(other, self.forecasts, self.assessments)['adjustments']['data_cmi']['eligible_count'], 0)

    def test_current_future_and_delayed_outcomes_cannot_enter(self):
        assessments = copy.deepcopy(self.assessments)
        for item in assessments:
            item['outcome_snapshot']['observed_at'] = self.cutoff.isoformat()
        result = build_online_trial(self.current, self.forecasts, assessments)
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 0)
        self.assertEqual(result['predictions']['data_cmi'], self.current['predictions']['data_cmi'])

    def test_snapshot_tampering_rejected_and_paired_future_evaluation(self):
        self.current['online_personalization'] = build_online_trial(self.current, self.forecasts, self.assessments)
        self.current['id'] = 100
        outcome = {'source': 'diary', 'sleep_start': (self.cutoff + timedelta(minutes=270)).isoformat(),
                   'wake': (self.cutoff + timedelta(minutes=720)).isoformat(),
                   'observed_at': (self.cutoff + timedelta(minutes=780)).isoformat()}
        result = evaluate_forecast(self.current, outcome)
        self.assertTrue(result['online_personalization']['eligible'])
        self.assertGreater(result['online_personalization']['errors']['data_cmi']['improvement_min'], 0)
        summary = summarize_online_trials([result])
        self.assertEqual(summary['versions'][0]['arms']['data_cmi']['n'], 1)
        excluded_first = {'assessment_id': 1, 'forecast_id': 100, 'eligible': False,
                          'reason': 'invalid_outcome', 'reported_record_snapshot': {'source': 'diary'}}
        result['assessment_id'] = 2
        self.assertEqual(summarize_online_trials([excluded_first, result])['versions'], [])
        self.current['online_personalization']['predictions']['data_cmi']['onset_offset_min'] += 1
        self.assertFalse(evaluate_online_trial(self.current, result['outcome_snapshot'])['eligible'])

    def test_wearable_outcomes_do_not_train_correction(self):
        for item in self.assessments:
            item['outcome_snapshot']['source'] = 'health_connect'
        result = build_online_trial(self.current, self.forecasts, self.assessments)
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 0)

    def test_ineligible_first_diary_cannot_be_replaced_in_training(self):
        excluded = {'assessment_id': 0, 'forecast_id': 1, 'eligible': False,
                    'reported_record_snapshot': {'source': 'diary'}}
        result = build_online_trial(self.current, self.forecasts, [excluded, *self.assessments])
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 17)


if __name__ == '__main__':
    unittest.main()
