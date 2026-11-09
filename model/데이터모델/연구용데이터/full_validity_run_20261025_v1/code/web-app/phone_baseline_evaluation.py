"""Recompute phone-reported frozen comparator errors; never refit or backfill."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from statistics import mean, median

from fusion_forecast import FusionModelError, PortableCohortModel

ZONE = timezone(timedelta(hours=9))
BASELINES = {
    'profile_clock_assumption', 'last_28_calendar_days',
    'arithmetic_mean_7_calendar_days', 'arithmetic_mean_14_calendar_days',
    'arithmetic_mean_28_calendar_days', 'circular_mean_7_calendar_days',
    'recency_weighted_28_calendar_days_half_life_14_days',
    'target_daytype_28_calendar_days', 'next_day_first_schedule_only_profile_duration',
}
REFERENCE = 'baseline:arithmetic_mean_14_calendar_days'
HISTORY_POLICY = 'manual only; wake and modified_at strictly before cutoff; latest version per KST wake date'
PROFILE_ROLE = 'user_profile_assumption_not_cohort_train_population_prior'
SCHEDULE_PROFILE_ROLE = 'fixed_user_profile_desired_duration_not_population_trained'
MODEL_FILES = {'cmi': 'robust_cmi_v2.json', 'mesa': 'robust_mesa_v2.json'}
METHODS = {
    'profile_clock_assumption': 'usual_profile_clock_assumption',
    'last_28_calendar_days': 'last_observation_within_28_calendar_days',
    'arithmetic_mean_7_calendar_days': 'arithmetic_mean_within_7_calendar_days',
    'arithmetic_mean_14_calendar_days': 'arithmetic_mean_within_14_calendar_days',
    'arithmetic_mean_28_calendar_days': 'arithmetic_mean_within_28_calendar_days',
    'circular_mean_7_calendar_days': 'circular_clock_mean_within_7_calendar_days',
    'recency_weighted_28_calendar_days_half_life_14_days': 'recency_weighted_28_calendar_days_half_life_14_days',
    'target_daytype_28_calendar_days': 'matching_target_weekday_or_weekend_within_28_calendar_days',
    'next_day_first_schedule_only_profile_duration': 'next_day_first_schedule_minus_preparation_with_profile_duration',
}
SCHEDULE_REASONS = {
    'no_schedule_context', 'invalid_context_observed_at', 'context_not_available_by_cutoff',
    'no_next_day_first_schedule', 'first_schedule_outside_next_day_window',
    'fixed_profile_duration_outside_prospective_prediction_window',
}


def stamp(value):
    if not isinstance(value, str): raise ValueError('missing time')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None: raise ValueError('naive time')
    return result


def offsets(item, actual=False):
    prefix = 'actual_' if actual else ''
    pair = tuple(item.get(prefix + key + '_offset_min') for key in ('onset', 'wake'))
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in pair):
        raise ValueError('invalid offset')
    if not (0 <= pair[0] <= 960 and 30 <= pair[1] <= 1440 and 30 <= pair[1]-pair[0] <= 960):
        raise ValueError('outside frozen target window')
    return pair


def unique_entries(value):
    result, conflicts = {}, set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('night_id'), str): continue
        key = item['night_id']
        if key in result and item != result[key]: conflicts.add(key)
        else: result[key] = item
    return {key: item for key, item in result.items() if key not in conflicts}, conflicts


def validated_baselines(frozen):
    expected_top = {'schema_version', 'version', 'cutoff', 'model_identifier', 'history_count',
                    'history_policy', 'profile_fallback_role', 'baselines'}
    if (set(frozen) != expected_top or frozen.get('schema_version') != 1
            or frozen.get('version') != 'android-research-baselines-v1'
            or frozen.get('model_identifier') != 'research_comparator_only_no_recommendation'
            or frozen.get('history_policy') != HISTORY_POLICY
            or frozen.get('profile_fallback_role') != PROFILE_ROLE
            or type(frozen.get('history_count')) is not int or frozen['history_count'] < 0):
        raise ValueError('invalid baseline envelope')
    entries = frozen.get('baselines')
    if not isinstance(entries, dict) or set(entries) != BASELINES:
        raise ValueError('missing or extra baseline')
    expected_fields = {'available', 'onset_offset_min', 'wake_offset_min', 'interval_min',
                       'method', 'fallback', 'contributing_records', 'reason'}
    for key, value in entries.items():
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ValueError('invalid baseline fields')
        available, count = value['available'], value['contributing_records']
        if type(available) is not bool or type(count) is not int or count < 0 or value['method'] != METHODS[key]:
            raise ValueError('invalid baseline metadata')
        if key == 'next_day_first_schedule_only_profile_duration':
            if count != 0:
                raise ValueError('schedule baseline used history')
            if available:
                if value['fallback'] != SCHEDULE_PROFILE_ROLE or value['reason'] is not None:
                    raise ValueError('invalid available schedule metadata')
            else:
                if (value['reason'] not in SCHEDULE_REASONS
                        or any(value[name] is not None for name in ('onset_offset_min', 'wake_offset_min', 'interval_min'))
                        or value['fallback'] not in (None, SCHEDULE_PROFILE_ROLE)):
                    raise ValueError('invalid unavailable schedule metadata')
                continue
        elif key == 'profile_clock_assumption':
            if not available or count != 0 or value['fallback'] != PROFILE_ROLE or value['reason'] is not None:
                raise ValueError('invalid profile baseline metadata')
        else:
            if not available or value['reason'] is not None or count > frozen['history_count']:
                raise ValueError('invalid history baseline metadata')
            if value['fallback'] == PROFILE_ROLE:
                if count != 0: raise ValueError('fallback cannot use history')
            elif value['fallback'] is None:
                if count < 1 or (key == 'last_28_calendar_days' and count != 1):
                    raise ValueError('invalid history contribution count')
            else:
                raise ValueError('unknown baseline fallback')
        pair = offsets(value)
        interval = value['interval_min']
        if type(interval) not in (float, int) or not math.isfinite(interval) or abs(interval - (pair[1]-pair[0])) > 1e-9:
            raise ValueError('baseline interval mismatch')
    return entries


def installed_models(models_dir):
    if models_dir is None: return {}
    result = {}
    for cohort, filename in MODEL_FILES.items():
        try: result[cohort] = PortableCohortModel(Path(models_dir) / filename, cohort)
        except (FusionModelError, OSError, ValueError, TypeError): pass
    return result


def artifact_matches(metadata, model, cohort):
    return (isinstance(metadata, dict) and metadata.get('cohort') == cohort
            and metadata.get('sha256') == model.sha256
            and metadata.get('model_version') == model.raw['model_version']
            and metadata.get('feature_contract') == model.raw['feature_contract']
            and metadata.get('physical_provider') == model.raw['physical_provider']
            and metadata.get('source_model_sha256') == model.raw['source_model_sha256'])


def evaluate_phone_baselines(outcomes, as_of, models_dir=None):
    """Each night contributes once. Conflicting duplicates fail closed.

    This verifies timestamps and recomputes errors, but does not authenticate
    the phone clock or prove the app was unmodified. No retrospective records
    replace the append-only first outcome, including excluded outcomes.
    """
    result = {'models': [], 'excluded': {}, 'n_matched_nights': 0,
              'interpretation': '휴대폰이 사전에 저장했다고 보고한 예측과 최초 자기보고를 비교합니다. 시각·범위를 확인하고 오차를 재계산했으나 휴대폰 시계의 진위는 검증하지 않았습니다. 서로 다른 유효 밤 수의 MAE를 직접 비교하지 마세요. Skill은 동일 밤의 14일 평균 대비이며, 표본이 적으면 확정적 효과로 해석할 수 없습니다.'}
    lag = {'available': False, 'n_reports': 0, 'target': 'immutable_first_manual_outcome_available_at_minus_wake',
           'interpretation': '앱에 동결된 최초 자기보고의 등록 지연입니다. 일반 기록의 수정시각이나 백업 수신 지연과 구분합니다.'}
    try: now = stamp(as_of)
    except ValueError: return result, lag
    forecasts, conflict_f = unique_entries(outcomes.get('prospective_forecasts'))
    actuals, conflict_a = unique_entries(outcomes.get('prospective_outcomes'))
    canonical_models = installed_models(models_dir)
    excluded = Counter(conflicting_duplicate_nights=len(conflict_f | conflict_a))
    errors, lags, matched = defaultdict(dict), [], set()
    for night, forecast in forecasts.items():
        actual = actuals.get(night)
        if actual is None: continue
        try:
            cutoff, generated = stamp(forecast.get('cutoff')), stamp(forecast.get('generated_at'))
            local = cutoff.astimezone(ZONE)
            if local.hour != 20 or local.minute or local.second or local.microsecond or local.date().isoformat() != night:
                raise ValueError('cutoff')
            if forecast.get('forecast_status') != 'prospective_20_window' or not cutoff <= generated <= cutoff + timedelta(minutes=15):
                raise ValueError('not prospectively frozen')
            if actual.get('status') != 'matched' or actual.get('actual_source') != 'manual' or actual.get('target') != 'first_self_reported_sleep_onset_and_final_wake':
                raise ValueError('ineligible first outcome')
            truth = offsets(actual, actual=True)
            available, wake = stamp(actual.get('available_at')), stamp(actual.get('outcome_at'))
            if not generated < cutoff + timedelta(minutes=truth[0]) < wake <= available <= now:
                raise ValueError('outcome chronology')
            if abs((wake-cutoff).total_seconds()/60-truth[1]) >= 1.01:
                raise ValueError('wake mismatch')
            frozen = forecast.get('research_baselines', {})
            if (not isinstance(frozen, dict) or stamp(frozen.get('cutoff')) != cutoff
                    or actual.get('source_research_baselines') != frozen):
                raise ValueError('baseline provenance mismatch')
            entries = validated_baselines(frozen)
        except (ValueError, TypeError, AttributeError, OverflowError):
            excluded['invalid_or_ineligible_frozen_pair'] += 1
            continue
        matched.add(night); lags.append((available-wake).total_seconds()/3600)
        predictions = {f'baseline:{key}': value for key, value in entries.items() if value['available'] is True}
        version = forecast.get('model_version')
        # Keep model generations separate. The source identity is part of the frozen outcome.
        if (version in ('robust-next-night-research-v2',) and actual.get('source_model_version') == version
                and actual.get('source_model_feature_contract') == forecast.get('model_feature_contract')
                and actual.get('source_research_artifacts') == forecast.get('research_artifacts')):
            model_predictions = forecast.get('research_predictions', {})
            artifacts = forecast.get('research_artifacts', {})
            if isinstance(model_predictions, dict) and isinstance(artifacts, dict):
                for cohort, model in canonical_models.items():
                    if (version != model.raw['model_version']
                            or forecast.get('model_feature_contract') != model.raw['feature_contract']
                            or not artifact_matches(artifacts.get(cohort), model, cohort)):
                        continue
                    for arm in ('physical', 'data', 'hybrid'):
                        key = f'{arm}_{cohort}'
                        if isinstance(model_predictions.get(key), dict): predictions[f'{version}:{key}'] = model_predictions[key]
        for name, prediction in predictions.items():
            try: pair = offsets(prediction)
            except (ValueError, TypeError):
                excluded['invalid_prediction'] += 1
                continue
            errors[name][night] = tuple(abs(p-t) for p,t in zip(pair,truth))
    reference = errors.get(REFERENCE, {})
    for name, values in sorted(errors.items()):
        onset, wake = (mean(pair[i] for pair in values.values()) for i in (0,1))
        common = sorted(values.keys() & reference.keys())
        ref = mean(mean(reference[n]) for n in common) if common else None
        candidate = mean(mean(values[n]) for n in common) if common else None
        result['models'].append({'name': name, 'n_nights': len(values), 'onset_mae_min': onset,
            'wake_mae_min': wake, 'joint_mae_min': (onset+wake)/2,
            'paired_reference_nights': len(common), 'paired_delta_mae_min': candidate-ref if common else None,
            'paired_skill_vs_mean14': 1-candidate/ref if len(common)>=4 and ref>=1 else None})
    result.update(n_matched_nights=len(matched), excluded=dict(excluded))
    if lags: lag.update(available=True, n_reports=len(lags), mean_hours=mean(lags), median_hours=median(lags), max_hours=max(lags))
    return result, lag
