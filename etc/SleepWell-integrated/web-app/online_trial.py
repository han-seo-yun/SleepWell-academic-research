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


def monitoring_identity(forecast, *, require_input_hash=True):
    from prospective import _forecast_matches_lock, _validated_monitoring_lock
    try:
        lock = _validated_monitoring_lock(
            forecast.get('monitoring_lock_snapshot'))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not _forecast_matches_lock(
            forecast, lock, require_input_hash=require_input_hash):
        return None
    return (
        lock['monitoring_lock_sha256'],
        enrollment_identity(forecast.get('profile_snapshot') or {}),
        forecast.get('model_version'),
        forecast.get('feature_contract'),
        (forecast.get('fusion_model') or {}).get(
            'artifact_identity_sha256'),
        lock['primary_arm'],
    )


def build_online_trial(current, forecasts, assessments):
    cutoff = local_time(current['cutoff'])
    current_identity = monitoring_identity(
        current, require_input_hash=False)
    if current_identity is None:
        return {'available': False, 'reason': 'no_valid_monitoring_lock'}
    lock_hash, enrollment, model_version, feature_contract, identity, primary_arm = current_identity
    if not identity:
        return {'available': False, 'reason': 'no_frozen_base_model'}
    by_id = {row['id']: row for row in forecasts if row.get('id') is not None}
    histories = {arm: [] for arm in ARMS}
    seen = set()
    for assessment in sorted(assessments, key=lambda row: row.get('assessment_id', 0)):
        original = by_id.get(assessment.get('forecast_id'))
        outcome = assessment.get('outcome_snapshot') or {}
        source = (outcome.get('source')
                  or (assessment.get('reported_record_snapshot') or {}).get('source'))
        if original is None or source != 'diary' or original['id'] in seen:
            continue
        seen.add(original['id'])
        if not assessment.get('eligible'):
            continue
        if (monitoring_identity(original) != current_identity
                or assessment.get('monitoring_lock_sha256') != lock_hash
                or assessment.get('model_version') != model_version
                or assessment.get('feature_contract') != feature_contract
                or assessment.get('enrollment_id') != enrollment
                or assessment.get('artifact_identity_sha256') != identity
                or assessment.get('selected_model') != primary_arm):
            continue
        try:
            previous_cutoff = local_time(original['cutoff'])
            generated = local_time(original['generated_at'])
            observed = local_time(outcome['observed_at'])
            onset = local_time(outcome['onset'])
            wake = local_time(outcome['wake'])
            actual_onset = float(outcome['onset_offset_min'])
            actual_wake = float(outcome['wake_offset_min'])
            offsets_match = (
                abs((onset - previous_cutoff).total_seconds() / 60 - actual_onset) <= 1e-6
                and abs((wake - previous_cutoff).total_seconds() / 60 - actual_wake) <= 1e-6)
            if (not all(math.isfinite(value) for value in (actual_onset, actual_wake))
                    or not offsets_match
                    or not (previous_cutoff < cutoff
                            and previous_cutoff <= generated
                            <= previous_cutoff + timedelta(minutes=15)
                            and generated < onset < wake <= observed
                            and wake < cutoff and observed < cutoff)):
                continue
            for arm in ARMS:
                base = original['predictions'][arm]
                predicted_onset = float(base['onset_offset_min'])
                predicted_wake = float(base['wake_offset_min'])
                if not all(math.isfinite(value) for value in (
                        predicted_onset, predicted_wake)):
                    raise ValueError('non-finite base prediction')
                histories[arm].append({
                    'night_id': original['night_id'],
                    'available_at': observed.isoformat(),
                    'outcome_at': wake.isoformat(),
                    'onset_residual_min': actual_onset - predicted_onset,
                    'wake_residual_min': actual_wake - predicted_wake,
                })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    predictions, adjustments = {}, {}
    for arm in ARMS:
        adjustment = compute_adjustment(histories[arm], cutoff)
        adjustments[arm] = adjustment
        base = current['predictions'][arm]
        onset, wake = constrain(
            base['onset_offset_min'] + adjustment['onset_correction_min'],
            base['wake_offset_min'] + adjustment['wake_correction_min'])
        predictions[arm] = {
            'onset_offset_min': onset,
            'wake_offset_min': wake,
            'interval_min': wake - onset,
            'bed': (cutoff + timedelta(minutes=onset)).isoformat(),
            'wake': (cutoff + timedelta(minutes=wake)).isoformat(),
        }
    result = {
        'available': True,
        'schema_version': 2,
        'enrollment_identity': enrollment,
        'policy_version': adjustments[ARMS[0]]['policy_version'],
        'model_version': model_version,
        'feature_contract': feature_contract,
        'base_artifact_identity_sha256': identity,
        'monitoring_lock_sha256': lock_hash,
        'primary_arm': primary_arm,
        'cutoff': cutoff.isoformat(),
        'predictions': predictions,
        'adjustments': adjustments,
        'status': 'experimental_sidecar_not_promoted',
        'baseline': 'same_frozen_model_already_uses_personal_history',
        'outcome_policy': ('first_complete_diary_report_per_prior_forecast; '
                           'ineligible_or_incomplete_first_not_replaced; '
                           'wake_and_observed_strictly_before_cutoff'),
    }
    result['snapshot_sha256'] = digest(result)
    return result


def evaluate_online_trial(forecast, outcome):
    trial = forecast.get('online_personalization') or {}
    if not trial.get('available'):
        return None
    if trial.get('schema_version') != 2:
        return {'eligible': False, 'reason': 'historical_trial_contract_read_only'}
    check = {key: value for key, value in trial.items()
             if key != 'snapshot_sha256'}
    current_identity = monitoring_identity(forecast)
    if current_identity is None:
        return {'eligible': False, 'reason': 'invalid_monitoring_identity'}
    lock_hash, enrollment, model_version, feature_contract, identity, primary_arm = current_identity
    if (trial.get('snapshot_sha256') != digest(check)
            or trial.get('base_artifact_identity_sha256') != identity
            or trial.get('monitoring_lock_sha256') != lock_hash
            or trial.get('model_version') != model_version
            or trial.get('feature_contract') != feature_contract
            or trial.get('primary_arm') != primary_arm
            or trial.get('cutoff') != forecast.get('cutoff')
            or set(trial.get('predictions', {})) != set(ARMS)):
        return {'eligible': False, 'reason': 'invalid_trial_snapshot'}
    if trial.get('enrollment_identity') != enrollment:
        return {'eligible': False, 'reason': 'different_enrollment'}
    if outcome.get('source') != 'diary':
        return {'eligible': False,
                'reason': 'wearable_auxiliary_excluded_from_personalization_trial'}
    try:
        actual_onset = float(outcome['onset_offset_min'])
        actual_wake = float(outcome['wake_offset_min'])
        if (outcome.get('onset') is None
                or not all(math.isfinite(value) for value in (actual_onset, actual_wake))):
            raise ValueError('incomplete paired outcome')
    except (KeyError, TypeError, ValueError, OverflowError):
        return {'eligible': False, 'reason': 'incomplete_paired_outcome'}
    errors = {}
    for arm in ARMS:
        base = forecast['predictions'][arm]
        corrected = trial['predictions'][arm]
        onset = abs(corrected['onset_offset_min'] - actual_onset)
        wake = abs(corrected['wake_offset_min'] - actual_wake)
        before = (abs(base['onset_offset_min'] - actual_onset)
                  + abs(base['wake_offset_min'] - actual_wake)) / 2
        errors[arm] = {
            'onset_min': onset,
            'wake_min': wake,
            'joint_min': (onset + wake) / 2,
            'base_joint_min': before,
            'improvement_min': before - (onset + wake) / 2,
            'active': trial['adjustments'][arm]['active'],
        }
    return {
        'eligible': True,
        'schema_version': trial['schema_version'],
        'policy_version': trial['policy_version'],
        'model_version': trial['model_version'],
        'feature_contract': trial['feature_contract'],
        'enrollment_identity': trial['enrollment_identity'],
        'base_artifact_identity_sha256': trial['base_artifact_identity_sha256'],
        'monitoring_lock_sha256': trial['monitoring_lock_sha256'],
        'primary_arm': trial['primary_arm'],
        'status': 'experimental_sidecar_not_promoted',
        'errors': errors,
    }


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
        version = (
            trial.get('schema_version', 1),
            trial['policy_version'],
            trial.get('model_version'),
            trial.get('feature_contract'),
            trial['base_artifact_identity_sha256'],
            trial.get('enrollment_identity'),
            trial.get('monitoring_lock_sha256'),
        )
        bucket = grouped.setdefault(version, {arm: [] for arm in ARMS})
        for arm in ARMS:
            bucket[arm].append(trial['errors'][arm])
    versions = []
    for version, arms in grouped.items():
        (schema_version, policy, model_version, feature_contract,
         identity, enrollment, lock_hash) = version
        versions.append({
            'schema_version': schema_version,
            'policy_version': policy,
            'model_version': model_version,
            'feature_contract': feature_contract,
            'base_artifact_identity_sha256': identity,
            'enrollment_identity': enrollment,
            'monitoring_lock_sha256': lock_hash,
            'status': ('experimental_sidecar_not_promoted'
                       if schema_version == 2 else 'historical_read_only'),
            'arms': {
                arm: {
                    'n': len(rows),
                    'active_n': sum(item['active'] for item in rows),
                    'base_joint_mae_min': mean(item['base_joint_min'] for item in rows),
                    'corrected_joint_mae_min': mean(item['joint_min'] for item in rows),
                    'paired_improvement_min': mean(item['improvement_min'] for item in rows),
                }
                for arm, rows in arms.items() if rows
            },
        })
    return {
        'versions': versions,
        'analysis_kind': 'experimental_forecast_accuracy_sidecar',
        'recommendation_effect': {'status': 'not_estimated', 'estimate': None},
        'interpretation': ('동일 잠금·등록·모델·아티팩트의 과거 최초 완전 일지만 사용하는 '
                           '실험적 보정 비교이며 서비스 승격, 앱 효능, 인과효과 또는 집단 일반화 근거가 아님'),
    }
