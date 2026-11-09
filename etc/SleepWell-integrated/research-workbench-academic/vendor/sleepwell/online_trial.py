"""Additive, immutable trial of past-residual correction; never replaces base arms."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from statistics import mean

from adaptive_personalization import compute_adjustment
from night_core import constrain, local_time

ARMS = ('data_cmi', 'hybrid_cmi', 'data_mesa', 'hybrid_mesa')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def enrollment_identity(profile):
    return profile.get('enrollment_id') or digest({
        key: profile.get(key) for key in ('created_at', 'name', 'age', 'sex')})


def build_online_trial(current, forecasts, assessments):
    cutoff = local_time(current['cutoff'])
    identity = (current.get('fusion_model') or {}).get('artifact_identity_sha256')
    if not identity:
        return {'available': False, 'reason': 'no_frozen_base_model'}
    enrollment = enrollment_identity(current['profile_snapshot'])
    by_id = {row['id']: row for row in forecasts if row.get('id') is not None}
    histories = {arm: [] for arm in ARMS}
    seen = set()
    # Store assessments are append-only: first diary report, even if ineligible,
    # remains the primary report. Corrections are not substituted after the fact.
    for assessment in sorted(assessments, key=lambda row: row.get('assessment_id', 0)):
        original = by_id.get(assessment.get('forecast_id'))
        outcome = assessment.get('outcome_snapshot') or {}
        source = outcome.get('source') or (assessment.get('reported_record_snapshot') or {}).get('source')
        if original is None or source != 'diary' or original['id'] in seen:
            continue
        seen.add(original['id'])
        if not assessment.get('eligible'):
            continue
        if enrollment_identity(original.get('profile_snapshot') or {}) != enrollment:
            continue
        if (original.get('fusion_model') or {}).get('artifact_identity_sha256') != identity:
            continue
        try:
            previous_cutoff = local_time(original['cutoff'])
            generated = local_time(original['generated_at'])
            observed = local_time(outcome['observed_at'])
            onset, wake = local_time(outcome['onset']), local_time(outcome['wake'])
            if not (previous_cutoff < cutoff and previous_cutoff <= generated <= previous_cutoff + timedelta(minutes=15)
                    and generated < onset < wake <= observed < cutoff):
                continue
            actual = [float(outcome[k]) for k in ('onset_offset_min', 'wake_offset_min')]
            if not all(math.isfinite(v) for v in actual):
                continue
            for arm in ARMS:
                base = original['predictions'][arm]
                histories[arm].append({'night_id': original['night_id'], 'available_at': observed.isoformat(),
                    'outcome_at': wake.isoformat(),
                    'onset_residual_min': actual[0] - base['onset_offset_min'],
                    'wake_residual_min': actual[1] - base['wake_offset_min']})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    predictions, adjustments = {}, {}
    for arm in ARMS:
        adjustment = compute_adjustment(histories[arm], cutoff)
        adjustments[arm] = adjustment
        base = current['predictions'][arm]
        onset, wake = constrain(base['onset_offset_min'] + adjustment['onset_correction_min'],
                                base['wake_offset_min'] + adjustment['wake_correction_min'])
        predictions[arm] = {'onset_offset_min': onset, 'wake_offset_min': wake,
                            'interval_min': wake - onset,
                            'bed': (cutoff + timedelta(minutes=onset)).isoformat(),
                            'wake': (cutoff + timedelta(minutes=wake)).isoformat()}
    result = {'available': True, 'schema_version': 1,
              'enrollment_identity': enrollment,
              'policy_version': adjustments[ARMS[0]]['policy_version'],
              'base_artifact_identity_sha256': identity, 'cutoff': cutoff.isoformat(),
              'predictions': predictions, 'adjustments': adjustments,
              'status': 'additional_research_comparison_not_recommendation_policy',
              'baseline': 'same_frozen_model_already_uses_personal_history',
              'outcome_policy': 'first_diary_report_per_prior_forecast_ineligible_first_not_replaced'}
    result['snapshot_sha256'] = digest(result)
    return result


def evaluate_online_trial(forecast, outcome):
    trial = forecast.get('online_personalization') or {}
    if not trial.get('available'):
        return None
    check = {k: v for k, v in trial.items() if k != 'snapshot_sha256'}
    if (trial.get('snapshot_sha256') != digest(check)
            or trial.get('base_artifact_identity_sha256') != (forecast.get('fusion_model') or {}).get('artifact_identity_sha256')
            or trial.get('cutoff') != forecast.get('cutoff') or set(trial.get('predictions', {})) != set(ARMS)):
        return {'eligible': False, 'reason': 'invalid_trial_snapshot'}
    if trial.get('enrollment_identity') != enrollment_identity(forecast.get('profile_snapshot') or {}):
        return {'eligible': False, 'reason': 'different_enrollment'}
    if outcome.get('source') != 'diary':
        return {'eligible': False, 'reason': 'wearable_auxiliary_excluded_from_personalization_trial'}
    errors = {}
    for arm in ARMS:
        base = forecast['predictions'][arm]
        corrected = trial['predictions'][arm]
        onset = abs(corrected['onset_offset_min'] - outcome['onset_offset_min'])
        wake = abs(corrected['wake_offset_min'] - outcome['wake_offset_min'])
        before = (abs(base['onset_offset_min'] - outcome['onset_offset_min'])
                  + abs(base['wake_offset_min'] - outcome['wake_offset_min'])) / 2
        errors[arm] = {'onset_min': onset, 'wake_min': wake, 'joint_min': (onset + wake) / 2,
                       'base_joint_min': before, 'improvement_min': before - (onset + wake) / 2,
                       'active': trial['adjustments'][arm]['active']}
    return {'eligible': True, 'policy_version': trial['policy_version'],
            'enrollment_identity': trial['enrollment_identity'],
            'base_artifact_identity_sha256': trial['base_artifact_identity_sha256'], 'errors': errors}


def summarize_online_trials(assessments):
    grouped, seen = {}, set()
    for row in sorted(assessments, key=lambda item: item.get('assessment_id', 0)):
        source = ((row.get('outcome_snapshot') or {}).get('source')
                  or (row.get('reported_record_snapshot') or {}).get('source'))
        if source != 'diary':
            continue
        key = row.get('forecast_id')
        if key in seen:
            continue
        seen.add(key)
        trial = row.get('online_personalization') or {}
        if not row.get('eligible') or not trial.get('eligible'):
            continue
        version = (trial['policy_version'], trial['base_artifact_identity_sha256'], trial.get('enrollment_identity'))
        bucket = grouped.setdefault(version, {arm: [] for arm in ARMS})
        for arm in ARMS:
            bucket[arm].append(trial['errors'][arm])
    versions = []
    for (policy, identity, enrollment), arms in grouped.items():
        versions.append({'policy_version': policy, 'base_artifact_identity_sha256': identity,
            'enrollment_identity': enrollment,
            'arms': {arm: {'n': len(rows), 'active_n': sum(x['active'] for x in rows),
                'base_joint_mae_min': mean(x['base_joint_min'] for x in rows),
                'corrected_joint_mae_min': mean(x['joint_min'] for x in rows),
                'paired_improvement_min': mean(x['improvement_min'] for x in rows)}
                for arm, rows in arms.items() if rows}})
    return {'versions': versions, 'interpretation': '한 참여자의 최초 일지와 사전 저장 예측 비교; 앱 효능이나 집단 일반화 근거가 아님'}
