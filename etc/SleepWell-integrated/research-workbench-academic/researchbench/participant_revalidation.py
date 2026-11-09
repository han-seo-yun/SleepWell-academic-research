"""Locked-procedure, repeated participant CV on historically exposed CMI/MESA.

This is internal revalidation, not a restored pristine test set. All fold models
are fitted and hashed before any out-of-fold predictions or scores are produced.
"""
from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from threadpoolctl import threadpool_limits

from . import controlled_comparison as cc
from . import cross_evaluation as cross
from . import pooled_training as pt


STATUS = 'retrospective_grouped_cv'
MODELS = (
    'cmi_data', 'mesa_data', 'pooled_data', 'mean50_data', 'known_source_data', 'recent_median',
    'cmi_hybrid', 'mesa_hybrid', 'pooled_hybrid', 'mean50_hybrid', 'known_source_hybrid',
    'recent_median_cmi', 'recent_median_mesa', 'pooled_source_data', 'separate_shared_data',
    'pooled_source_hybrid', 'separate_shared_hybrid',
)
PRIMARY_PAIR = ('recent_median', 'pooled_data')
PAIRS = (
    *[('recent_median', m) for m in cross.PRIMARY_MODELS if m != 'recent_median'],
    ('mean50_data', 'pooled_data'), ('known_source_data', 'pooled_data'),
    ('mesa_data', 'cmi_data'), ('recent_median_cmi', 'cmi_data'), ('recent_median_mesa', 'mesa_data'),
    ('pooled_data', 'pooled_source_data'), ('separate_shared_data', 'pooled_data'),
    *[(m+'_data', m+'_hybrid') for m in ('cmi', 'mesa', 'pooled', 'mean50', 'known_source', 'pooled_source', 'separate_shared')],
)
METRICS = ('onset_mae_min', 'wake_mae_min', 'joint_mae_min', 'interval_mae_min', 'onset_mse', 'wake_mse')
LIMITATIONS = [
    '과거 Train·Validation·Test 전체를 재분할한 내부 교차검증입니다. 과거 성능을 본 연구 설계의 선택 편향은 제거되지 않습니다.',
    '모델 종류·Huber 설정·특징·전처리·평가 기준은 이번 OOF 결과 전에 고정했습니다. 각 fold의 계수·집단 prior·전처리 통계는 학습 참여자만으로 다시 적합했습니다.',
    '95% 구간은 반복별 손실을 먼저 평균한 참여자 OOF 손실의 기술적 부트스트랩입니다. 겹치는 학습 집합의 의존성·재학습·과거 선택의 불확실성은 반영하지 않습니다.',
    '같은 평가 참여자의 이전에 관측 완료된 밤은 다음 밤 예측의 입력으로 허용합니다. 계수 업데이트는 없으며 무기록 시작 성능을 별도로 보고합니다.',
    'CMI·MESA의 라벨·연령·기기 차이는 남아 있습니다. Macro는 두 코호트 50:50 가중치이며 일반 인구의 오차가 아닙니다.',
    '출처를 아는 선택·출처 인코딩 대조군은 일반 사용자에게 CMI/MESA 소속을 부여하는 배포 모델이 아닙니다.',
    '삼성은 제외했습니다. 원본·기존 실험·설치 앱의 모델을 변경하지 않으며 외부·전향 검증이나 임상 효용을 주장하지 않습니다.',
]


def now():
    return datetime.now(timezone.utc).isoformat()


def code_hashes():
    paths = [__file__, cc.__file__, cross.__file__, pt.__file__]
    paths += [pt.vendor_module(name).__file__ for name in ('night_core', 'robust_features')]
    return {str(Path(p).resolve()): pt.sha256(p) for p in paths}


def settings(config):
    values = {key: int(config.get(key, default)) for key, default in
              (('seed', 20261106), ('cv_folds', 5), ('cv_repeats', 2), ('bootstrap_replicates', 5000))}
    if not 2 <= values['cv_folds'] <= 5 or not 1 <= values['cv_repeats'] <= 5:
        raise ValueError('Invalid participant CV bounds')
    if not 100 <= values['bootstrap_replicates'] <= 20000:
        raise ValueError('Invalid bootstrap count')
    return values


def fold_assignments(frame, folds, repeats, seed):
    original = frame.copy()
    original['split'] = 'train'
    records = []
    for repeat, fold, _, held_indices in cc.participant_folds(original, folds, repeats, seed):
        held = frame.loc[held_indices, ['cohort', 'subject_id', 'split']].drop_duplicates()
        for row in held.itertuples(index=False):
            records.append({'repeat': repeat, 'fold': fold, 'cohort': row.cohort,
                            'subject_id': str(row.subject_id), 'historical_split': row.split})
    return pd.DataFrame(records).sort_values(['repeat', 'cohort', 'subject_id']).reset_index(drop=True)


def fold_frames(frame, assignments, repeat, fold):
    allocation = assignments.loc[assignments.repeat.eq(repeat)].set_index(['cohort', 'subject_id']).fold
    keys = pd.MultiIndex.from_frame(frame[['cohort', 'subject_id']])
    if not allocation.index.is_unique or set(keys) != set(allocation.index):
        raise ValueError('Fold allocation does not cover exactly the input participants')
    mask = allocation.reindex(keys).to_numpy() == fold
    train, held = frame.loc[~mask].copy(), frame.loc[mask].copy()
    if train.empty or held.empty:
        raise ValueError('Empty training or evaluation fold')
    if set(map(tuple, train[['cohort', 'subject_id']].to_numpy())) & set(map(tuple, held[['cohort', 'subject_id']].to_numpy())):
        raise ValueError('Participant leakage across fold')
    train['split'], held['split'] = 'train', 'out_of_fold'
    return train, held


def load_untouched_holdout(path, development):
    held = pd.read_csv(path, dtype={'subject_id': str})
    required = {'cohort', 'subject_id', 'cutoff', 'bed', 'wake', 'observed_at',
                'bedtime_min', 'wake_min', 'duration_min'}
    if not required.issubset(held.columns) or held.empty or set(held.cohort) != {'MESA'}:
        raise ValueError('Expected nonempty MESA-only holdout with the fixed episode schema')
    if held.duplicated(['cohort', 'subject_id', 'cutoff']).any():
        raise ValueError('Duplicate holdout episodes')
    if not np.isfinite(held[pt.TARGETS].to_numpy(float)).all():
        raise ValueError('Nonfinite holdout targets')
    if set(map(tuple, held[['cohort', 'subject_id']].to_numpy())) & set(map(tuple, development[['cohort', 'subject_id']].to_numpy())):
        raise ValueError('Untouched holdout overlaps historical development participants')
    times = {name: pd.to_datetime(held[name], errors='raise') for name in ('cutoff', 'bed', 'wake', 'observed_at')}
    onset = (times['bed']-times['cutoff']).dt.total_seconds()/60
    wake = (times['wake']-times['cutoff']).dt.total_seconds()/60
    duration = (times['wake']-times['bed']).dt.total_seconds()/60
    if not (times['cutoff'].dt.hour.eq(20) & times['cutoff'].dt.minute.eq(0)
            & onset.between(0, 960) & wake.between(30, 1440) & duration.between(30, 960)
            & times['observed_at'].eq(times['wake'])).all():
        raise ValueError('Holdout violates the frozen next-night time/QC contract')
    if not np.allclose(np.column_stack([onset, wake, duration]), held[['bedtime_min', 'wake_min', 'duration_min']], atol=1e-8, rtol=0):
        raise ValueError('Holdout targets disagree with event timestamps')
    held = held.sort_values(['cohort', 'subject_id', 'cutoff']).reset_index(drop=True)
    held['split'] = 'unused_mesa_holdout'
    return held


def prepare_revalidation(config):
    """Write the procedure and complete ID-only partition lock BEFORE any fit."""
    output, database = Path(config['output_dir']).resolve(), Path(config['database_path']).resolve()
    audit = Path(config['participant_audit_path']).resolve()
    if output.exists():
        raise FileExistsError('Use a new output directory for the prefit lock')
    audit_value = json.loads(audit.read_text(encoding='utf-8-sig'))
    audit_inputs = audit_value.get('locked_input_sha256', audit_value.get('source_sha256', {}))
    for source, expected in audit_inputs.items():
        if pt.sha256(source) != expected:
            raise ValueError(f'Audited source or extractor changed: {source}')
    values = settings(config)
    frame, manifest, digest = pt.load_database(database)
    holdout_path = Path(config['untouched_holdout_path']).resolve() if config.get('untouched_holdout_path') else None
    holdout = load_untouched_holdout(holdout_path, frame) if holdout_path else None
    assignment = fold_assignments(frame, values['cv_folds'], values['cv_repeats'], values['seed'])
    output.mkdir(parents=True, exist_ok=False)
    assignment.to_csv(output/'private_fold_assignments.csv', index=False)
    pt.write_json(output/'participant_audit.json', audit_value)
    protocol = {
        'schema': 'sleepwell.participant_revalidation_protocol.v1', 'created_utc': now(), 'status': STATUS,
        'database_path': str(database), 'database_sha256': digest, **values,
        'cohorts': list(pt.COHORTS), 'database_counts': manifest['summary'],
        'participant_audit_path': str(audit), 'participant_audit_sha256': pt.sha256(audit),
        'audited_input_sha256': audit_inputs,
        'untouched_holdout': ({'path': str(holdout_path), 'sha256': pt.sha256(holdout_path),
                              'cohort': 'MESA', 'n_participants': int(holdout.subject_id.nunique()), 'n_nights': len(holdout),
                              'fit': 'Fit same locked eight bundles on all original development participants before any OOF/holdout scores',
                              'scope': 'Separate small MESA-only local held-out participants, bounded prior-use audit; not external or prospective and not representative of general users',
                              'excluded_from_cv': True, 'primary_pair': list(PRIMARY_PAIR)} if holdout_path else None),
        'code_sha256': code_hashes(), 'models': list(MODELS),
        'model_configuration': {'candidate': 'huber', 'alpha': 1., 'epsilon': 1.35, 'max_iter': 1000, 'tol': 1e-5,
                                'targets': list(pt.TARGETS), 'descriptors': {d: cc.feature_names(d) for d in cc.DESCRIPTORS}},
        'preprocessing': 'Fold-training-only equal-person/cohort weighted median imputation, constant removal and weighted scaling; no clipping or outcome-based deletions',
        'prior': 'Recomputed from fold-training people; CMI/MESA source models use their own training source only; pooled uses equal-cohort prior',
        'encoding': 'No IDs, demographics or source for primary models; pooled_source adds two predefined CMI/MESA one-hot columns as a secondary control',
        'weights': 'Equal people within source, CMI/MESA equal total weight when pooled; normalized to mean one',
        'partition': 'All available QC-passing participants, historical splits retained only in audit; seeded participant permutation within each cohort, one held-out fold per repeat',
        'lock_order': 'Protocol + participant folds first; fit and hash ALL fold models before any OOF prediction or score; no tuning/selection using new OOF results',
        'historical_exposure': True, 'pristine_holdout': False,
        'historical_design_selection_bias_removed': False,
        'nested_cv': 'No new model/hyperparameter/preprocessing selection is performed; fixed-procedure CV, not nested tuning and not an unbiased estimate of prior adaptive model development',
        'history': 'Each held-out person starts empty; predict then append; only completed and available past nights before local 20:00; never update fitted coefficients',
        'median': 'Separate endpoint medians of at most 14 available nights in past 56 days; fold-training prior when no recent observations',
        'ensemble': '50:50 mean of projected CMI and MESA endpoint predictions, then same support projection',
        'projection': 'Identical existing night_core.constrain for every arm, fixed before this run',
        'time_contract': manifest['time_contract'],
        'primary_metric': 'Mean absolute onset/wake error per night, then mean per participant within repeat, then mean across repeats per participant, then equal-person cohort means, then CMI/MESA 50:50 macro',
        'repeated_cv': 'Average LOSSES across repeats, not predictions; repeated observations do not increase participant sample size',
        'primary_pair': list(PRIMARY_PAIR), 'comparisons': [list(p) for p in PAIRS],
        'inference': 'All comparisons retrospective and exploratory; primary_pair is the predeclared focal contrast for this rerun only, not a new confirmatory study',
        'uncertainty': 'Paired participant percentile bootstrap within cohort of repeat-averaged OOF losses; descriptive conditional intervals, not full generalization/selection uncertainty; fold and repeat scores separately reported',
        'history_bands': list(cross.HISTORY_BANDS), 'history_band_unit': 'recent_count14; all observed nights count also retained privately',
        'key_secondary': ['mean50_data versus pooled_data', 'recent_count14 == 0 cold-start; same primary and key-secondary contrasts'],
        'arm_scope': 'Union of the 13 cross-cohort arms and four controlled source/shared-transform controls; unrelated legacy app/physical models excluded',
        'service_promotion': False, 'automatic_winner_selection': False,
        'environment': {'python': platform.python_version(), 'numpy': np.__version__, 'pandas': pd.__version__, 'sklearn': sklearn.__version__},
        'limitations': LIMITATIONS,
        'references': ['https://scikit-learn.org/stable/modules/cross_validation.html',
                       'https://scikit-learn.org/stable/common_pitfalls.html#data-leakage'],
    }
    pt.write_json(output/'protocol.json', protocol)
    lock = {'locked_utc': now(), 'protocol_sha256': pt.sha256(output/'protocol.json'),
            'fold_assignments_sha256': pt.sha256(output/'private_fold_assignments.csv'),
            'participant_audit_sha256': pt.sha256(output/'participant_audit.json'),
            'new_oof_results_used_for_tuning': False, 'historical_results_influenced_design': True,
            'status': 'procedure_locked_before_refitting'}
    pt.write_json(output/'procedure_lock.json', lock)
    return lock


def verify_lock(config):
    output = Path(config['output_dir']).resolve()
    protocol = json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    lock = json.loads((output/'procedure_lock.json').read_text(encoding='utf-8'))
    for key, filename in (('protocol_sha256', 'protocol.json'), ('fold_assignments_sha256', 'private_fold_assignments.csv'),
                          ('participant_audit_sha256', 'participant_audit.json')):
        if pt.sha256(output/filename) != lock[key]:
            raise ValueError(f'Prefit lock changed: {filename}')
    if protocol['code_sha256'] != code_hashes():
        raise ValueError('Evaluation or feature code changed after procedure lock')
    for source, digest in protocol.get('audited_input_sha256', {}).items():
        if pt.sha256(source) != digest:
            raise ValueError(f'Audited source or extractor changed: {source}')
    if any(protocol[key] != value for key, value in settings(config).items()):
        raise ValueError('Runtime settings differ from prefit protocol')
    if Path(config['database_path']).resolve() != Path(protocol['database_path']):
        raise ValueError('Database path differs from prefit protocol')
    if pt.sha256(config['database_path']) != protocol['database_sha256']:
        raise ValueError('Database changed after prefit lock')
    held = protocol.get('untouched_holdout')
    if bool(held) != bool(config.get('untouched_holdout_path')):
        raise ValueError('Holdout presence differs from prefit protocol')
    if held and (str(Path(config['untouched_holdout_path']).resolve()) != held['path'] or pt.sha256(held['path']) != held['sha256']):
        raise ValueError('Holdout changed after prefit lock')
    assignments = pd.read_csv(output/'private_fold_assignments.csv', dtype={'subject_id': str})
    return protocol, lock, assignments


def fit_fold(train, seed, progress=None, cancelled=None):
    """Only training frames enter this function, including preprocessing fit."""
    if set(train.split) != {'train'}:
        raise ValueError('Fold fitting accepts training rows only')
    shared_prior = pt.prior_from_train(train)
    shared, _, _ = pt.replay_features(train, shared_prior, progress, cancelled, 'train_shared_history')
    own, priors = [], {}
    for cohort in pt.COHORTS:
        part = train.loc[train.cohort.eq(cohort)]
        priors[cohort] = pt.prior_from_train(part)
        features, _, _ = pt.replay_features(part, priors[cohort], progress, cancelled, 'train_source_history')
        own.append(features)
    own = pd.concat(own).loc[train.index]
    views = {'shared_fit': shared, 'shared_eval': shared, 'separate_fit': own, 'separate_eval': own,
             'shared_prior': shared_prior, 'separate_prior': priors}
    bundles = {}
    for descriptors in cc.DESCRIPTORS:
        prepared = cc.prepare_setting(train, train, views, descriptors, 'none')
        for strategy in cc.STRATEGIES:
            pt.checkpoint(progress, cancelled, 'fit_'+strategy+'_'+descriptors, 0)
            bundle, _, converged = cc.fit_strategy(strategy, train, train, views, prepared, 'huber', seed)
            if not converged:
                raise ValueError('Locked Huber model did not converge; abort before any OOF scores')
            bundle['preprocessing'] = 'none'
            bundles[strategy+'_'+descriptors] = bundle
    return bundles


def evaluate_fold(held, bundles, progress=None, cancelled=None):
    views, counts, recent = {}, None, None
    priors = {**bundles['separate_data']['priors'], 'ALL': bundles['pooled_data']['priors']['ALL']}
    for scope, prior in priors.items():
        features, _, n = pt.replay_features(held, prior, progress, cancelled, 'oof_history_'+scope)
        r = features.recent_count14.to_numpy(int)
        if counts is not None and (counts != n or not np.array_equal(recent, r)):
            raise ValueError('History availability differs between model priors')
        views[scope], counts, recent = features, n, r
    predictions = {}
    for d in cc.DESCRIPTORS:
        for cohort in pt.COHORTS:
            predictions[cohort.lower()+'_'+d] = cross.predict_source(bundles['separate_'+d], cohort, views[cohort])
        predictions['pooled_'+d] = cross.predict_source(bundles['pooled_'+d], 'ALL', views['ALL'])
        cmi, mesa = predictions['cmi_'+d], predictions['mesa_'+d]
        predictions['mean50_'+d] = cross.mean_predictions(cmi, mesa)
        predictions['known_source_'+d] = np.where(held.cohort.eq('CMI').to_numpy()[:, None], cmi, mesa)
        for strategy in ('pooled_source', 'separate_shared'):
            bundle = bundles[strategy+'_'+d]
            base = bundle['preprocessors']['ALL'].transform(views['ALL'][bundle['features']])
            result = np.empty((len(held), 2))
            for scope, model in bundle['models'].items():
                mask = np.ones(len(held), bool) if scope == 'ALL' else held.cohort.eq(scope).to_numpy()
                if not mask.any():
                    continue
                matrix = cc.encode_features(base[mask], held.loc[mask, 'cohort'], strategy == 'pooled_source')
                result[mask] = cross.constrain_predictions(model.predict(matrix))
            predictions[strategy+'_'+d] = result
    for scope, model in (('ALL', 'recent_median'), ('CMI', 'recent_median_cmi'), ('MESA', 'recent_median_mesa')):
        predictions[model] = cross.constrain_predictions(views[scope][['median14_bed', 'median14_wake']].to_numpy())
    for name in ('recent_median_cmi', 'recent_median_mesa'):
        if not np.array_equal(predictions['recent_median'][recent > 0], predictions[name][recent > 0]):
            raise ValueError('Warm median differs across priors')
    if set(predictions) != set(MODELS):
        raise ValueError('Prediction model registry differs from the protocol')
    return {m: predictions[m] for m in MODELS}, counts, recent


def participant_losses(private):
    """Average night errors within a repeat BEFORE averaging participant repeats."""
    value = private[['repeat', 'model', 'cohort', 'subject_id']].copy()
    onset = private.predicted_onset_min.to_numpy() - private.actual_onset_min.to_numpy()
    wake = private.predicted_wake_min.to_numpy() - private.actual_wake_min.to_numpy()
    value['onset_mae_min'], value['wake_mae_min'] = np.abs(onset), np.abs(wake)
    value['joint_mae_min'] = (np.abs(onset)+np.abs(wake))/2
    value['interval_mae_min'] = np.abs(wake-onset)
    value['onset_mse'], value['wake_mse'] = onset**2, wake**2
    each = value.groupby(['repeat', 'model', 'cohort', 'subject_id'], sort=True)[list(METRICS)].mean()
    return each, each.groupby(['model', 'cohort', 'subject_id'], sort=True).mean()


def summarize_losses(people, nights, replicates, seed, split='out_of_fold'):
    rows, errors, boots = [], {}, {}
    available = sorted(people.index.get_level_values('cohort').unique())
    for model in people.index.get_level_values('model').unique():
        person = people.xs(model, level='model').sort_index()
        errors[model], boots[model] = person, {}
        means = person.groupby(level='cohort').mean()
        for cohort in available:
            part = person.xs(cohort, level='cohort')
            number = pt.COHORTS.index(cohort)
            boots[model][cohort] = pt.bootstrap_means(part.joint_mae_min, replicates, seed+number)
            lo, hi = np.quantile(boots[model][cohort], [.025, .975])
            row = {'model_id': cohort.lower()+'.'+model, 'model': model, 'cohort': cohort,
                   'split': split, 'averaging': 'repeat_loss_then_equal_participant' if split == 'out_of_fold' else 'equal_participant',
                   'n_participants': len(part), 'n_nights': int(nights[cohort]),
                   **{k: float(means.loc[cohort, k]) for k in METRICS[:4]},
                   'onset_rmse_min': float(np.sqrt(means.loc[cohort, 'onset_mse'])),
                   'wake_rmse_min': float(np.sqrt(means.loc[cohort, 'wake_mse'])),
                   'ci95_low': float(lo), 'ci95_high': float(hi)}
            rows.append(row)
        if set(available) == set(pt.COHORTS):
            boots[model]['MACRO'] = (boots[model]['CMI']+boots[model]['MESA'])/2
            lo, hi = np.quantile(boots[model]['MACRO'], [.025, .975])
            rows.append({'model_id': 'macro.'+model, 'model': model, 'cohort': 'MACRO', 'split': 'out_of_fold',
                         'averaging': 'repeat_loss_then_equal_participant_then_equal_cohort',
                         'n_participants': len(person), 'n_nights': int(sum(nights.values())),
                         **{k: float(means[k].mean()) for k in METRICS[:4]},
                         'ci95_low': float(lo), 'ci95_high': float(hi)})
    return rows, errors, boots


def paired_summaries(errors, boots, pairs):
    rows = []
    for reference, model in pairs:
        left, right = errors[reference], errors[model]
        if not left.index.equals(right.index):
            raise ValueError('Unpaired OOF participants')
        means = (left.joint_mae_min-right.joint_mae_min).groupby(level='cohort').mean()
        for scope in boots[model]:
            draw = boots[reference][scope]-boots[model][scope]
            lo, hi = np.quantile(draw, [.025, .975])
            rows.append({'reference': reference, 'model': model, 'cohort': scope,
                         'improvement_min': float(means.mean() if scope == 'MACRO' else means[scope]),
                         'ci95_low': float(lo), 'ci95_high': float(hi), 'primary': (reference, model) == PRIMARY_PAIR,
                         'interpretation': 'positive = model lower MAE; descriptive conditional OOF interval'})
    return rows


def check_prediction_coverage(private, frame, repeats):
    key = ['cohort', 'subject_id', 'cutoff']
    expected = pd.MultiIndex.from_frame(frame[key])
    if not expected.is_unique:
        raise ValueError('Input row identity is not unique')
    for repeat in range(repeats):
        for model in MODELS:
            part = private.loc[private.repeat.eq(repeat) & private.model.eq(model)]
            actual = pd.MultiIndex.from_frame(part[key])
            if not actual.is_unique or set(expected) != set(actual):
                raise ValueError(f'Incomplete or duplicated OOF predictions: {repeat}/{model}')
    if len(private) != len(frame)*len(MODELS)*repeats:
        raise ValueError('Unexpected OOF prediction rows')
    if private.groupby(key).recent_count14.nunique().max() != 1:
        raise ValueError('Available history differs between repeats or arms')


def run_participant_revalidation(config, progress=None, cancelled=None):
    output = Path(config['output_dir']).resolve()
    protocol, lock, assignment = verify_lock(config)
    if (output/'fit_started.json').exists():
        raise FileExistsError('This locked experiment has already started; no silent rerun/overwrite')
    frame, _, _ = pt.load_database(config['database_path'])
    holdout = load_untouched_holdout(protocol['untouched_holdout']['path'], frame) if protocol.get('untouched_holdout') else None
    folds, repeats, seed, replicates = (protocol[k] for k in ('cv_folds', 'cv_repeats', 'seed', 'bootstrap_replicates'))
    # Reproduce the seed-only allocation before accessing the fitting phase.
    expected = fold_assignments(frame, folds, repeats, seed)
    pd.testing.assert_frame_equal(assignment, expected)
    pt.write_json(output/'fit_started.json', {'started_utc': now(), 'procedure_lock_sha256': pt.sha256(output/'procedure_lock.json')})
    model_dir = output/'fold_models'
    model_dir.mkdir()
    fit_records, model_hashes = [], {}
    with threadpool_limits(limits=2):
        for repeat in range(repeats):
            for fold in range(folds):
                pt.checkpoint(progress, cancelled, f'fit_repeat_{repeat+1}_fold_{fold+1}', (repeat*folds+fold)/(repeats*folds))
                train, held = fold_frames(frame, assignment, repeat, fold)
                bundles = fit_fold(train, seed, progress, cancelled)
                filename = f'repeat_{repeat}_fold_{fold}.joblib'
                joblib.dump(bundles, model_dir/filename)
                model_hashes[filename] = pt.sha256(model_dir/filename)
                for cohort in pt.COHORTS:
                    fit_records.append({'repeat': repeat, 'fold': fold, 'cohort': cohort,
                                        'train_participants': int(train.loc[train.cohort.eq(cohort)].subject_id.nunique()),
                                        'evaluation_participants': int(held.loc[held.cohort.eq(cohort)].subject_id.nunique()),
                                        'train_nights': int(train.cohort.eq(cohort).sum()),
                                        'evaluation_nights': int(held.cohort.eq(cohort).sum()),
                                        'participant_overlap': 0, 'all_converged': True})
                pd.DataFrame(fit_records).to_csv(output/'fold_audit.csv', index=False)
        if holdout is not None:
            pt.checkpoint(progress, cancelled, 'fit_all_development_before_holdout', 0)
            train_all = frame.copy()
            train_all['split'] = 'train'
            final_bundles = fit_fold(train_all, seed, progress, cancelled)
            joblib.dump(final_bundles, model_dir/'all_development.joblib')
            model_hashes['all_development.joblib'] = pt.sha256(model_dir/'all_development.joblib')
        all_models_lock = {'locked_utc': now(), 'protocol_sha256': lock['protocol_sha256'],
                           'models_sha256': model_hashes, 'all_models_locked_before_oof_prediction': True,
                           'new_oof_used_for_tuning': False, 'historical_design_exposure': True}
        pt.write_json(output/'all_fold_models_lock.json', all_models_lock)
        pt.checkpoint(progress, cancelled, 'ALL_FOLD_MODELS_LOCKED_BEFORE_OOF', 1)
        private_parts = []
        for repeat in range(repeats):
            for fold in range(folds):
                _, held = fold_frames(frame, assignment, repeat, fold)
                filename = f'repeat_{repeat}_fold_{fold}.joblib'
                if pt.sha256(model_dir/filename) != model_hashes[filename]:
                    raise ValueError('Frozen fold model changed')
                bundles = joblib.load(model_dir/filename)
                predictions, counts, recent = evaluate_fold(held, bundles, progress, cancelled)
                part = cc.private_predictions(held, predictions, counts, 'out_of_fold')
                part['repeat'], part['fold'] = repeat, fold
                part['recent_count14'] = np.tile(recent, len(predictions))
                part['history_band'] = part.recent_count14.map(cross.history_band)
                private_parts.append(part)
        holdout_private = None
        if holdout is not None:
            if pt.sha256(model_dir/'all_development.joblib') != model_hashes['all_development.joblib']:
                raise ValueError('Full-development models changed before holdout evaluation')
            final_bundles = joblib.load(model_dir/'all_development.joblib')
            hp, hc, hr = evaluate_fold(holdout, final_bundles, progress, cancelled)
            holdout_private = cc.private_predictions(holdout, hp, hc, 'unused_mesa_holdout')
            holdout_private['repeat'], holdout_private['fold'] = 0, -1
            holdout_private['recent_count14'] = np.tile(hr, len(hp))
            holdout_private['history_band'] = holdout_private.recent_count14.map(cross.history_band)
            check_prediction_coverage(holdout_private, holdout, 1)
            holdout_private.to_csv(output/'private_unused_mesa_predictions.csv', index=False)
    private = pd.concat(private_parts, ignore_index=True)
    check_prediction_coverage(private, frame, repeats)
    private.to_csv(output/'private_oof_predictions.csv', index=False)
    pt.checkpoint(progress, cancelled, 'aggregate_repeated_oof_losses', 0)
    by_repeat, people = participant_losses(private)
    people.to_csv(output/'private_participant_losses.csv')
    nights = frame.groupby('cohort').size().to_dict()
    metrics, errors, boots = summarize_losses(people, nights, replicates, seed)
    paired = paired_summaries(errors, boots, PAIRS)
    repeat_metrics, fold_metrics = [], []
    for repeat in range(repeats):
        means = by_repeat.xs(repeat, level='repeat').groupby(['model', 'cohort']).mean()
        for (model, cohort), row in means.iterrows():
            repeat_metrics.append({'repeat': repeat, 'model': model, 'cohort': cohort, 'joint_mae_min': float(row.joint_mae_min)})
        for model, part in means.groupby(level='model'):
            repeat_metrics.append({'repeat': repeat, 'model': model, 'cohort': 'MACRO', 'joint_mae_min': float(part.joint_mae_min.mean())})
        for fold in range(folds):
            part = private.loc[private.repeat.eq(repeat) & private.fold.eq(fold)]
            _, fp = participant_losses(part)
            fm = fp.groupby(['model', 'cohort']).mean()
            for (model, cohort), row in fm.iterrows():
                fold_metrics.append({'repeat': repeat, 'fold': fold, 'model': model, 'cohort': cohort,
                                     'joint_mae_min': float(row.joint_mae_min)})
            for model, part in fm.groupby(level='model'):
                fold_metrics.append({'repeat': repeat, 'fold': fold, 'model': model, 'cohort': 'MACRO',
                                     'joint_mae_min': float(part.joint_mae_min.mean())})
    history_metrics, history_paired = [], []
    for band in cross.HISTORY_BANDS:
        subset = private.loc[private.history_band.eq(band)]
        if subset.empty:
            continue
        _, hp = participant_losses(subset)
        unique = subset[['cohort', 'subject_id', 'cutoff']].drop_duplicates()
        hn = unique.groupby('cohort').size().to_dict()
        hm, he, hb = summarize_losses(hp, hn, replicates, seed)
        history_metrics.extend([dict(row, history_band=band) for row in hm])
        history_paired.extend([dict(row, history_band=band) for row in paired_summaries(he, hb, PAIRS[:6])])
    unused_metrics, unused_paired, unused_history = [], [], []
    if holdout_private is not None:
        _, hp = participant_losses(holdout_private)
        unused_metrics, he, hb = summarize_losses(hp, holdout.groupby('cohort').size().to_dict(), replicates, seed, 'unused_mesa_holdout')
        unused_paired = paired_summaries(he, hb, PAIRS)
        for band in cross.HISTORY_BANDS:
            subset = holdout_private.loc[holdout_private.history_band.eq(band)]
            if subset.empty:
                continue
            _, bp = participant_losses(subset)
            bn = subset[['cohort', 'subject_id', 'cutoff']].drop_duplicates().groupby('cohort').size().to_dict()
            bm, _, _ = summarize_losses(bp, bn, replicates, seed, 'unused_mesa_holdout')
            unused_history.extend([dict(row, split='unused_mesa_holdout', history_band=band) for row in bm])
    transfer = []
    for training, model in (('CMI', 'cmi_data'), ('MESA', 'mesa_data')):
        for target in pt.COHORTS:
            row = next(r for r in metrics if r['model'] == model and r['cohort'] == target)
            transfer.append(dict(row, training_cohort=training, target_cohort=target,
                                 transfer_type='within_source_oof' if training == target else 'cross_source_oof_past_history_allowed'))
    for name, rows in (('oof_metrics', metrics), ('paired_comparisons', paired), ('repeat_metrics', repeat_metrics),
                       ('fold_metrics', fold_metrics), ('history_metrics', history_metrics),
                       ('history_paired_comparisons', history_paired), ('transfer_metrics', transfer),
                       ('untouched_holdout_metrics', unused_metrics), ('untouched_holdout_paired_comparisons', unused_paired),
                       ('untouched_holdout_history_metrics', unused_history)):
        pd.DataFrame(rows).to_csv(output/(name+'.csv'), index=False)
    verify_lock(config)
    for filename, digest in model_hashes.items():
        if pt.sha256(model_dir/filename) != digest:
            raise ValueError('Fold model changed during evaluation')
    evaluation = {'schema': 'sleepwell.participant_revalidation.v1', 'status': STATUS, 'completed_utc': now(),
                  'protocol': protocol, 'selection': all_models_lock, 'metrics': metrics, 'paired_comparisons': paired,
                  'fold_metrics': fold_metrics, 'repeat_metrics': repeat_metrics,
                  'history_metrics': history_metrics, 'history_paired_comparisons': history_paired,
                  'untouched_holdout_metrics': unused_metrics, 'untouched_holdout_paired_comparisons': unused_paired,
                  'untouched_holdout_history_metrics': unused_history,
                  'transfer_metrics': transfer, 'limitations': LIMITATIONS,
                  'verification': {'n_participants': int(frame[['cohort', 'subject_id']].drop_duplicates().shape[0]),
                                   'n_unique_nights': len(frame), 'oof_rows': len(private), 'models': len(MODELS),
                                   'fold_models': len(model_hashes), 'participant_overlap': 0,
                                   'oof_once_per_repeat': True, 'same_nights_all_models': True,
                                   'repeat_losses_averaged_not_predictions': True,
                                   'all_fits_before_any_oof_prediction': True, 'pristine_external_validation': False},
                  'artifacts': {'output_dir': str(output), 'private_oof_predictions': str(output/'private_oof_predictions.csv')}}
    pt.write_json(output/'evaluation.json', evaluation)
    pt.write_json(output/'manifest.json', {'database_sha256': protocol['database_sha256'],
                  'artifacts_sha256': {p.relative_to(output).as_posix(): pt.sha256(p) for p in output.rglob('*') if p.is_file()}})
    pt.checkpoint(progress, cancelled, 'completed', 1)
    return {'mode': 'participant_revalidation', 'raw': evaluation, 'summary': metrics, 'warnings': LIMITATIONS,
            'provenance': {'evidence': STATUS, 'input_synthetic': False, 'input_sha256': protocol['database_sha256'],
                           'input_source': Path(config['database_path']).name, 'database_path': str(config['database_path']),
                           'protocol_sha256': lock['protocol_sha256'], 'evaluation_path': str(output/'evaluation.json')}}
