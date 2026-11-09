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


def make_forecast(cutoff, monitoring_lock=None, identity='a' * 64,
                  enrollment='trial-enrollment'):
    model = fusion(identity, cutoff)
    participant = dict(profile(), enrollment_id=enrollment)
    return build_forecast(participant, [], {}, cutoff, cutoff,
                          {'available': False}, model,
                          monitoring_lock=monitoring_lock)


class OnlineTrialTests(unittest.TestCase):
    def setUp(self):
        from prospective import create_monitoring_lock
        self.cutoff = local_time('2026-10-25T20:00:00+09:00')
        seed = make_forecast(self.cutoff)
        self.lock = create_monitoring_lock(
            '2026-09-30T18:00:00+09:00', 'trial-enrollment',
            seed['fusion_model']['artifact_identity_sha256'])
        self.forecasts, self.assessments = [], []
        for index in range(18):
            cutoff = self.cutoff - timedelta(days=18 - index)
            forecast = make_forecast(cutoff, self.lock)
            forecast['id'] = index + 1
            outcome = {
                'source': 'diary',
                'sleep_start': (cutoff + timedelta(minutes=270)).isoformat(),
                'wake': (cutoff + timedelta(minutes=720)).isoformat(),
                'observed_at': (cutoff + timedelta(minutes=780)).isoformat(),
                'enrollment_id': 'trial-enrollment',
            }
            result = evaluate_forecast(forecast, outcome)
            result['assessment_id'] = index + 1
            self.forecasts.append(forecast)
            self.assessments.append(result)
        self.current = make_forecast(self.cutoff, self.lock)

    def test_only_past_first_reports_same_full_identity_are_used(self):
        from prospective import create_monitoring_lock
        actual = build_online_trial(
            self.current, self.forecasts, self.assessments)
        self.assertEqual(actual['adjustments']['data_cmi']['eligible_count'], 18)
        self.assertTrue(actual['adjustments']['data_cmi']['active'])
        self.assertGreater(actual['predictions']['data_cmi']['onset_offset_min'], 240)
        unhashed_current = copy.deepcopy(self.current)
        unhashed_current.pop('input_hash')
        self.assertEqual(
            actual,
            build_online_trial(
                unhashed_current, self.forecasts, self.assessments))
        tampered_history = copy.deepcopy(self.forecasts)
        tampered_history[0]['predictions']['data_cmi']['onset_offset_min'] += 1
        self.assertEqual(
            build_online_trial(
                self.current, tampered_history, self.assessments)
            ['adjustments']['data_cmi']['eligible_count'],
            17)
        correction = copy.deepcopy(self.assessments[0])
        correction['assessment_id'] = 100
        correction['outcome_snapshot']['onset_offset_min'] = 900
        self.assertEqual(
            actual,
            build_online_trial(
                self.current, self.forecasts,
                [*self.assessments, correction]))
        seed_b = make_forecast(self.cutoff, identity='b' * 64)
        lock_b = create_monitoring_lock(
            '2026-09-30T18:00:00+09:00', 'trial-enrollment',
            seed_b['fusion_model']['artifact_identity_sha256'])
        other_artifact = make_forecast(
            self.cutoff, lock_b, identity='b' * 64)
        self.assertEqual(build_online_trial(
            other_artifact, self.forecasts, self.assessments)
            ['adjustments']['data_cmi']['eligible_count'], 0)
        other_enrollment_seed = make_forecast(
            self.cutoff, enrollment='other-enrollment')
        other_lock = create_monitoring_lock(
            '2026-09-30T18:00:00+09:00', 'other-enrollment',
            other_enrollment_seed['fusion_model']['artifact_identity_sha256'])
        other_enrollment = make_forecast(
            self.cutoff, other_lock, enrollment='other-enrollment')
        self.assertEqual(build_online_trial(
            other_enrollment, self.forecasts, self.assessments)
            ['adjustments']['data_cmi']['eligible_count'], 0)
        tampered = copy.deepcopy(self.current)
        tampered['feature_contract'] = 'different-contract'
        self.assertFalse(build_online_trial(
            tampered, self.forecasts, self.assessments)['available'])

    def test_current_future_and_delayed_outcomes_cannot_enter(self):
        delayed = copy.deepcopy(self.assessments)
        for item in delayed:
            item['outcome_snapshot']['observed_at'] = self.cutoff.isoformat()
        result = build_online_trial(self.current, self.forecasts, delayed)
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 0)
        current = copy.deepcopy(self.current)
        current['id'] = 100
        current_assessment = {
            'assessment_id': 100,
            'forecast_id': 100,
            'eligible': True,
            'outcome_snapshot': {
                'source': 'diary',
                'onset': (self.cutoff + timedelta(minutes=270)).isoformat(),
                'wake': (self.cutoff + timedelta(minutes=720)).isoformat(),
                'observed_at': (self.cutoff + timedelta(minutes=780)).isoformat(),
                'onset_offset_min': 270,
                'wake_offset_min': 720,
            },
        }
        future_cutoff = self.cutoff + timedelta(days=1)
        future = make_forecast(future_cutoff, self.lock)
        future['id'] = 101
        future_assessment = copy.deepcopy(current_assessment)
        future_assessment.update(assessment_id=101, forecast_id=101)
        future_assessment['outcome_snapshot'].update(
            onset=(future_cutoff + timedelta(minutes=270)).isoformat(),
            wake=(future_cutoff + timedelta(minutes=720)).isoformat(),
            observed_at=(future_cutoff + timedelta(minutes=780)).isoformat())
        excluded = build_online_trial(
            self.current, [current, future],
            [current_assessment, future_assessment])
        self.assertEqual(excluded['adjustments']['data_cmi']['eligible_count'], 0)

    def test_wake_only_first_report_cannot_train_or_be_replaced(self):
        partial = copy.deepcopy(self.assessments[0])
        partial['assessment_id'] = 0
        partial['outcome_snapshot']['onset'] = None
        partial['outcome_snapshot']['onset_offset_min'] = None
        correction = copy.deepcopy(self.assessments[0])
        correction['assessment_id'] = 100
        result = build_online_trial(
            self.current, self.forecasts,
            [partial, *self.assessments, correction])
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 17)

    def test_history_remains_continuous_across_calibration_heldout_boundary(self):
        heldout_cutoff = local_time(self.lock['calibration_end']) + timedelta(days=5)
        heldout_current = make_forecast(heldout_cutoff, self.lock)
        result = build_online_trial(
            heldout_current, self.forecasts, self.assessments)
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 18)
        self.assertEqual(result['monitoring_lock_sha256'],
                         self.lock['monitoring_lock_sha256'])

    def test_snapshot_tampering_rejected_and_paired_future_evaluation(self):
        from prospective import _canonical_hash
        self.current['online_personalization'] = build_online_trial(
            self.current, self.forecasts, self.assessments)
        self.current['input_hash'] = _canonical_hash({
            key: value for key, value in self.current.items()
            if key not in ('id', 'forecast_id', 'input_hash')
        })
        self.current['id'] = 100
        outcome = {
            'source': 'diary',
            'sleep_start': (self.cutoff + timedelta(minutes=270)).isoformat(),
            'wake': (self.cutoff + timedelta(minutes=720)).isoformat(),
            'observed_at': (self.cutoff + timedelta(minutes=780)).isoformat(),
            'enrollment_id': 'trial-enrollment',
        }
        result = evaluate_forecast(self.current, outcome)
        self.assertTrue(result['online_personalization']['eligible'])
        self.assertGreater(
            result['online_personalization']['errors']['data_cmi']['improvement_min'], 0)
        summary = summarize_online_trials([result])
        self.assertEqual(summary['versions'][0]['arms']['data_cmi']['n'], 1)
        self.assertEqual(summary['versions'][0]['status'],
                         'experimental_sidecar_not_promoted')
        excluded_first = {
            'assessment_id': 1,
            'forecast_id': 100,
            'eligible': False,
            'reason': 'invalid_outcome',
            'reported_record_snapshot': {'source': 'diary'},
        }
        result['assessment_id'] = 2
        self.assertEqual(
            summarize_online_trials([excluded_first, result])['versions'], [])
        self.current['online_personalization']['predictions']['data_cmi'][
            'onset_offset_min'] += 1
        self.assertFalse(evaluate_online_trial(
            self.current, result['outcome_snapshot'])['eligible'])

    def test_wearable_outcomes_do_not_train_correction(self):
        assessments = copy.deepcopy(self.assessments)
        for item in assessments:
            item['outcome_snapshot']['source'] = 'health_connect'
        result = build_online_trial(
            self.current, self.forecasts, assessments)
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 0)

    def test_ineligible_first_diary_cannot_be_replaced_in_training(self):
        excluded = {
            'assessment_id': 0,
            'forecast_id': 1,
            'eligible': False,
            'reported_record_snapshot': {'source': 'diary'},
        }
        result = build_online_trial(
            self.current, self.forecasts, [excluded, *self.assessments])
        self.assertEqual(result['adjustments']['data_cmi']['eligible_count'], 17)


if __name__ == '__main__':
    unittest.main()
