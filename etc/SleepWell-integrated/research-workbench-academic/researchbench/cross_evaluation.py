"""Frozen, matched cross-cohort transfer evaluation; never promotes an app model."""
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
from . import pooled_training as pt

PRIMARY_MODELS = ('cmi_data', 'mesa_data', 'pooled_data', 'mean50_data',
                  'known_source_data', 'recent_median')
PRIMARY_PAIR = ('recent_median', 'mean50_data')
HISTORY_BANDS = ('0', '1-3', '4-6', '7-13', '14+')
SOURCE_FILES = ('separate_data.joblib', 'pooled_data.joblib',
                'separate_hybrid.joblib', 'pooled_hybrid.joblib')


def history_band(count):
    if count < 0:
        raise ValueError('History counts must be nonnegative')
    return '0' if count == 0 else '1-3' if count <= 3 else '4-6' if count <= 6 else '7-13' if count <= 13 else '14+'


def constrain_predictions(values):
    values = np.asarray(values, float)
    if values.ndim != 2 or values.shape[1] != 2 or not np.isfinite(values).all():
        raise ValueError('Expected finite onset/wake predictions')
    core = pt.vendor_module('night_core')
    return np.asarray([core.constrain(*row) for row in values], float)


def mean_predictions(left, right):
    left, right = np.asarray(left, float), np.asarray(right, float)
    if left.shape != right.shape:
        raise ValueError('Mean predictions require identical rows and endpoints')
    return constrain_predictions((left + right) / 2)


def predict_source(bundle, scope, features):
    """Apply the chosen training source to EVERY target row, without target routing."""
    expected = scope if bundle['strategy'] == 'separate' else 'ALL'
    if bundle.get('source_encoding') or expected not in bundle['models']:
        raise ValueError('Transfer requires an available, source-agnostic predictor')
    matrix = bundle['preprocessors'][expected].transform(features[bundle['features']])
    return constrain_predictions(bundle['models'][expected].predict(matrix))


def load_frozen_sources(source, database_hash, train):
    source = Path(source).resolve()
    manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    selection = json.loads((source/'selection_lock.json').read_text(encoding='utf-8'))
    protocol = json.loads((source/'protocol.json').read_text(encoding='utf-8'))
    if manifest['database_sha256'] != database_hash or protocol['database_sha256'] != database_hash:
        raise ValueError('Frozen models belong to another database')
    for name, digest in manifest['artifacts_sha256'].items():
        if pt.sha256(source/name) != digest:
            raise ValueError(f'Changed source artifact: {name}')
    if pt.sha256(source/'protocol.json') != selection['protocol_sha256']:
        raise ValueError('Original protocol lock mismatch')
    if pt.sha256(cc.__file__) != protocol['code_sha256']:
        raise ValueError('Original controlled engine changed; investigate before transfer evaluation')
    common = selection['common_config']
    if (common['candidate'], common['preprocessing']) != ('huber', 'none'):
        raise ValueError('This extension requires the locked common Huber/no-clipping setting')
    bundles = {}
    for filename in SOURCE_FILES:
        name = Path(filename).stem
        if pt.sha256(source/filename) != selection['models_sha256'][name]:
            raise ValueError('Original model lock mismatch')
        bundle = joblib.load(source/filename)
        if bundle['candidate'] != 'huber' or bundle['preprocessing'] != 'none' or bundle.get('source_encoding'):
            raise ValueError('Unexpected frozen model settings')
        if bundle['database_sha256'] != database_hash:
            raise ValueError('Frozen model database mismatch')
        descriptors = name.rsplit('_', 1)[1]
        if bundle['features'] != cc.feature_names(descriptors):
            raise ValueError('Feature contract changed')
        for scope, prior in bundle['priors'].items():
            fit = train if scope == 'ALL' else train.loc[train.cohort.eq(scope)]
            expected = pt.prior_from_train(fit)
            if prior != expected:
                raise ValueError(f'Frozen prior not reproducible from its declared Train source: {name}/{scope}')
        bundles[name] = bundle
    return bundles, selection


def evaluate_split(frame, bundles, progress=None, cancelled=None):
    """Replay identical causal histories under the three frozen population priors."""
    views, counts, recent_counts = {}, None, None
    priors = {**bundles['separate_data']['priors'], 'ALL': bundles['pooled_data']['priors']['ALL']}
    for scope, prior in priors.items():
        features, _, current_counts = pt.replay_features(
            frame, prior, progress=progress, cancelled=cancelled, stage=f'replay_{frame.iloc[0].split}_{scope}')
        current_recent = features.recent_count14.to_numpy(int)
        if counts is not None and (counts != current_counts or not np.array_equal(recent_counts, current_recent)):
            raise ValueError('Source priors changed the available-history row set')
        counts, recent_counts, views[scope] = current_counts, current_recent, features
    predictions = {}
    for descriptors in ('data', 'hybrid'):
        separate, pooled = bundles['separate_'+descriptors], bundles['pooled_'+descriptors]
        for scope in pt.COHORTS:
            if separate['priors'][scope] != priors[scope]:
                raise ValueError('Descriptor priors differ')
            predictions[scope.lower()+'_'+descriptors] = predict_source(separate, scope, views[scope])
        if pooled['priors']['ALL'] != priors['ALL']:
            raise ValueError('Pooled descriptor priors differ')
        predictions['pooled_'+descriptors] = predict_source(pooled, 'ALL', views['ALL'])
        cmi, mesa = predictions['cmi_'+descriptors], predictions['mesa_'+descriptors]
        predictions['mean50_'+descriptors] = mean_predictions(cmi, mesa)
        predictions['known_source_'+descriptors] = np.where(frame.cohort.eq('CMI').to_numpy()[:, None], cmi, mesa)
    for scope, name in (('ALL', 'recent_median'), ('CMI', 'recent_median_cmi'), ('MESA', 'recent_median_mesa')):
        predictions[name] = constrain_predictions(views[scope][['median14_bed', 'median14_wake']].to_numpy())
    # With recent observations the median baseline must be independent of the prior.
    warm = recent_counts > 0
    for name in ('recent_median_cmi', 'recent_median_mesa'):
        if not np.array_equal(predictions['recent_median'][warm], predictions[name][warm]):
            raise ValueError('Observed-history median depends on source identity')
    return predictions, counts, recent_counts


def paired_rows(errors, bootstraps, pairs):
    rows = []
    for reference, model in pairs:
        if not errors[reference].index.equals(errors[model].index):
            raise ValueError('Participant alignment changed between paired models')
        difference = errors[reference].joint_mae_min - errors[model].joint_mae_min
        means = difference.groupby(level='cohort').mean()
        for scope in (*pt.COHORTS, 'MACRO'):
            if scope == 'MACRO':
                boots = sum(bootstraps[reference][c] - bootstraps[model][c] for c in pt.COHORTS) / len(pt.COHORTS)
            else:
                boots = bootstraps[reference][scope] - bootstraps[model][scope]
            lo, hi = np.quantile(boots, [.025, .975])
            rows.append({'reference': reference, 'model': model, 'cohort': scope,
                         'improvement_min': float(means.mean() if scope == 'MACRO' else means[scope]),
                         'ci95_low': float(lo), 'ci95_high': float(hi),
                         'interpretation': 'positive = model lower MAE than reference; conditional exploratory 95% interval'})
    return rows


def history_summaries(frame, predictions, counts, replicates, seed):
    """Keep absent cohort/band cells absent; never fabricate a two-cohort macro."""
    bands = np.asarray([history_band(n) for n in counts])
    rows, differences = [], []
    for band in HISTORY_BANDS:
        mask = bands == band
        if not mask.any():
            continue
        available = set(frame.loc[mask, 'cohort'])
        model_stats, model_boots = {}, {}
        for model in PRIMARY_MODELS:
            people = pt.participant_errors(frame.loc[mask], predictions[model][mask])
            model_stats[model], model_boots[model] = people, {}
            means = people.groupby(level='cohort').mean()
            for number, scope in enumerate(pt.COHORTS):
                if scope not in available:
                    continue
                subset = people.xs(scope, level='cohort')
                boots = pt.bootstrap_means(subset.joint_mae_min, replicates, seed+number)
                model_boots[model][scope] = boots
                lo, hi = np.quantile(boots, [.025, .975])
                rows.append({'history_band': band, 'cohort': scope, 'model': model,
                             'n_participants': len(subset), 'n_nights': int((mask & frame.cohort.eq(scope)).sum()),
                             'joint_mae_min': float(means.loc[scope, 'joint_mae_min']),
                             'onset_mae_min': float(means.loc[scope, 'onset_mae_min']),
                             'wake_mae_min': float(means.loc[scope, 'wake_mae_min']),
                             'ci95_low': float(lo), 'ci95_high': float(hi)})
            if available == set(pt.COHORTS):
                boots = (model_boots[model]['CMI']+model_boots[model]['MESA'])/2
                model_boots[model]['MACRO'] = boots
                lo, hi = np.quantile(boots, [.025, .975])
                rows.append({'history_band': band, 'cohort': 'MACRO', 'model': model,
                             'n_participants': len(people), 'n_nights': int(mask.sum()),
                             **{key: float(means[key].mean()) for key in ('joint_mae_min', 'onset_mae_min', 'wake_mae_min')},
                             'ci95_low': float(lo), 'ci95_high': float(hi)})
        for model in PRIMARY_MODELS:
            if model == 'recent_median':
                continue
            delta = model_stats['recent_median'].joint_mae_min - model_stats[model].joint_mae_min
            if delta.isna().any():
                raise ValueError('History-stratum pairing failed')
            mean = delta.groupby(level='cohort').mean()
            for scope in model_boots[model]:
                boots = model_boots['recent_median'][scope] - model_boots[model][scope]
                lo, hi = np.quantile(boots, [.025, .975])
                differences.append({'history_band': band, 'cohort': scope, 'reference': 'recent_median', 'model': model,
                                    'improvement_min': float(mean.mean() if scope == 'MACRO' else mean[scope]),
                                    'ci95_low': float(lo), 'ci95_high': float(hi)})
    return rows, differences


def check_original_predictions(source, frame, predictions, split):
    original = pd.read_csv(Path(source)/f'private_{split}_predictions.csv', dtype={'subject_id': str})
    key = ['cohort', 'subject_id', 'cutoff']
    order = pd.MultiIndex.from_frame(frame[key])
    deltas = {}
    for name in ('separate_data', 'separate_hybrid', 'pooled_data', 'pooled_hybrid'):
        previous = original.loc[original.model.eq(name)].set_index(key)
        if not previous.index.is_unique or set(previous.index) != set(order):
            raise ValueError('Historical comparison uses different nights')
        expected = previous.reindex(order)[['predicted_onset_min', 'predicted_wake_min']].to_numpy(float)
        current = predictions[name.replace('separate_', 'known_source_')]
        delta = float(np.max(np.abs(expected-current)))
        if delta > 1e-8:
            raise ValueError(f'Frozen predictions changed: {split}/{name}: {delta}')
        deltas[name] = delta
    return deltas


def run_cross_evaluation(config, progress=None, cancelled=None):
    output = Path(config['output_dir']).resolve()
    source = Path(config['source_run_dir']).resolve()
    database = Path(config['database_path']).resolve()
    if output.exists():
        raise FileExistsError('Use a new output directory')
    seed, replicates = int(config.get('seed', 20261105)), int(config.get('bootstrap_replicates', 5000))
    if not 100 <= replicates <= 20000:
        raise ValueError('Invalid bootstrap count')
    pt.checkpoint(progress, cancelled, 'verify_database_and_frozen_models', 0)
    frame, _, database_hash = pt.load_database(database)
    train = frame.loc[frame.split.eq('train')]
    bundles, inherited_selection = load_frozen_sources(source, database_hash, train)
    hashes = {name: pt.sha256(source/name) for name in SOURCE_FILES}
    output.mkdir(parents=True, exist_ok=False)
    protocol = {
        'schema': 'sleepwell.cross_cohort_protocol.v1', 'created_utc': datetime.now(timezone.utc).isoformat(),
        'status': pt.STATUS, 'database_sha256': database_hash, 'seed': seed,
        'source_run_dir': str(source), 'source_models_sha256': hashes,
        'code_sha256': {Path(p).name: pt.sha256(p) for p in (__file__, cc.__file__, pt.__file__)},
        'feature_code_sha256': {name: pt.sha256(pt.vendor_module(name).__file__) for name in ('night_core', 'robust_features')},
        'fitting': 'No refit: reuse original Train-only Huber models, original source-specific preprocessing and priors',
        'hyperparameters': 'Common Huber/none selected previously using BOTH cohorts Train-CV; not development-naive external validation',
        'primary_models': list(PRIMARY_MODELS), 'primary_pair': list(PRIMARY_PAIR),
        'primary_scope': 'CMI/MESA equal-cohort macro; all other comparisons exploratory without multiplicity adjustment',
        'source_transfer': 'CMI model uses CMI-only prior/transform/coefficients on both cohorts; MESA likewise; target past history is allowed',
        'ensemble': '50:50 arithmetic mean of BOTH onset and wake offsets after source model projection; project mean to same support',
        'median': 'Separate endpoint medians of at most 14 available nights within past 56 days; source-unaware fallback is shared Train-only prior',
        'source_median_controls': 'Identical median with source-only prior on zero-recent-history rows for transfer fairness',
        'known_source': 'CMI->CMI and MESA->MESA reference only; unavailable source routing for general new users, not a performance bound',
        'history': 'Predict then append; wake and observed_at strictly before local 20:00; same causal history on every arm',
        'history_bands': list(HISTORY_BANDS), 'history_band_unit': 'all currently valid past nights; bands may contain different people and overlap across time',
        'preprocessing': 'Frozen weighted median imputation, constant removal, weighted scaling; no extra clipping/deletion; age/sex/source labels not predictors',
        'metrics': 'Mean onset/wake absolute error per night, then per person, then equal people within source; macro 50:50',
        'bootstrap': {'replicates': replicates, 'unit': 'paired participants within each cohort',
                      'scope': 'conditional percentile 95%; no fitting/selection uncertainty; bands descriptive'},
        'validation_role': 'diagnostic only, no new selection or tuning', 'test_role': 'previously reused historical holdout',
        'service_promotion': False, 'environment': {'python': platform.python_version(), 'sklearn': sklearn.__version__, 'numpy': np.__version__},
    }
    pt.write_json(output/'protocol.json', protocol)
    lock = {'locked_utc': datetime.now(timezone.utc).isoformat(), 'common_config': inherited_selection['common_config'],
            'models_sha256': hashes, 'source_selection_sha256': pt.sha256(source/'selection_lock.json'),
            'protocol_sha256': pt.sha256(output/'protocol.json'), 'test_used_for_selection': False,
            'validation_used_for_selection': False, 'automatic_winner_or_promotion': False, 'refitted': False}
    pt.write_json(output/'selection_lock.json', lock)
    summaries, replay_deltas = {}, {}
    with threadpool_limits(limits=2):
        for split in ('validation', 'test'):
            current = frame.loc[frame.split.eq(split)].copy()
            predictions, counts, recent = evaluate_split(current, bundles, progress, cancelled)
            replay_deltas[split] = check_original_predictions(source, current, predictions, split)
            summary, errors, boots = pt.metric_summaries(current, predictions, replicates, seed, split)
            summaries[split] = summary
            private = cc.private_predictions(current, predictions, counts, split)
            private['recent_count14'] = np.tile(recent, len(predictions))
            private['history_band'] = private.history_count.map(history_band)
            private.to_csv(output/f'private_{split}_predictions.csv', index=False)
            pd.DataFrame(summary).to_csv(output/f'{split}_metrics.csv', index=False)
            if split == 'test':
                pairs = [('recent_median', name) for name in PRIMARY_MODELS if name != 'recent_median']
                pairs += [('pooled_data', 'mean50_data'), ('known_source_data', 'mean50_data'),
                          ('mesa_data', 'cmi_data'), ('recent_median_cmi', 'cmi_data'),
                          ('recent_median_mesa', 'mesa_data'), ('mean50_data', 'mean50_hybrid')]
                comparisons = paired_rows(errors, boots, pairs)
                history_metrics, history_pairs = history_summaries(current, predictions, counts, replicates, seed)
                recent_history_metrics, recent_history_pairs = history_summaries(current, predictions, recent, replicates, seed)
    transfer = []
    for training, model in (('CMI', 'cmi_data'), ('MESA', 'mesa_data')):
        for target in pt.COHORTS:
            metric = next(r for r in summaries['test'] if r['model'] == model and r['cohort'] == target)
            transfer.append({**metric, 'training_cohort': training, 'target_cohort': target,
                             'transfer_type': 'within_source' if training == target else 'cross_source_past_history_allowed'})
    for filename, rows in (('paired_comparisons.csv', comparisons), ('history_metrics.csv', history_metrics),
                           ('history_paired_comparisons.csv', history_pairs), ('transfer_metrics.csv', transfer),
                           ('recent_history_metrics.csv', recent_history_metrics),
                           ('recent_history_paired_comparisons.csv', recent_history_pairs)):
        pd.DataFrame(rows).to_csv(output/filename, index=False)
    if pt.sha256(database) != database_hash or any(pt.sha256(source/name) != digest for name, digest in hashes.items()):
        raise ValueError('Inputs changed during evaluation')
    limitations = [
        '재사용 Test의 동결 모델 조건부 비교입니다. 새 일반 사용자·외부·전향 검증이 아닙니다.',
        '공통 Huber/전처리 설정은 이전에 CMI·MESA 양쪽 Train-CV로 선택했습니다. 출처별 계수만 분리됐으며 개발 단계에서 완전히 보지 못한 코호트는 아닙니다.',
        '교차 평가도 해당 대상자의 cutoff 이전 수면 이력을 사용합니다. 이력 0 구간을 제외하면 무이력 예측이 아닙니다.',
        'CMI 사건 라벨과 MESA 활동계 파생 라벨, 인구·기록 길이가 다릅니다. 실제 앱 일지·기기 입력과 동일하지 않습니다.',
        '출처 일치 개별 모델은 비교용입니다. 일반 사용자를 CMI/MESA로 라우팅하는 검증된 규칙은 없습니다.',
        '95% 구간은 같은 참가자를 재표집한 조건부 구간으로 학습·설계·후보 선택 불확실성을 제외합니다. 보조 비교는 다중성 보정하지 않았습니다.',
        '이력 구간마다 참가자 구성이 달라 성능 차이를 개인화의 인과효과로 해석할 수 없습니다. 없는 코호트 구간의 Macro는 생성하지 않습니다.',
        '50:50은 두 끝점을 평균한 연구 모델입니다. 기존 Android의 입면 보조 평균·정책·동결 계수와 동일한 앱 성능 평가가 아닙니다.',
        '최근 중앙값도 기록이 없으면 Train 집단 초기값을 사용합니다. 이 초기값이 일반 사용자의 필요수면을 의미하지 않습니다.',
        '삼성 데이터는 사용하지 않았고 앱 모델 교체, 수면 개선 효과 또는 임상 효용 검증은 수행하지 않았습니다.',
    ]
    evaluation = {'schema': 'sleepwell.cross_cohort_evaluation.v1', 'status': pt.STATUS,
                  'completed_utc': datetime.now(timezone.utc).isoformat(), 'selection': lock,
                  'primary_pair': list(PRIMARY_PAIR), 'test_metrics': summaries['test'], 'validation_metrics': summaries['validation'],
                  'paired_comparisons': comparisons, 'history_metrics': history_metrics,
                  'history_paired_comparisons': history_pairs, 'recent_history_metrics': recent_history_metrics,
                  'recent_history_paired_comparisons': recent_history_pairs,
                  'transfer_metrics': transfer, 'limitations': limitations,
                  'verification': {'database_unchanged': True, 'source_models_unchanged': True, 'source_priors_recomputed_from_train': True,
                                   'test_used_for_selection': False, 'validation_used_for_selection': False, 'samsung_rows': 0,
                                   'test_participants': len(errors['recent_median']), 'test_nights': len(current),
                                   'median_fallback_nights': {c: int(((recent == 0) & current.cohort.eq(c)).sum()) for c in pt.COHORTS},
                                   'old_history_without_recent_nights': {c: int(((recent == 0) & (np.asarray(counts) > 0) & current.cohort.eq(c)).sum()) for c in pt.COHORTS},
                                   'same_nights_all_models': True, 'historical_replay_max_abs_error_min': replay_deltas},
                  'artifacts': {'output_dir': str(output), 'private_test_predictions': str(output/'private_test_predictions.csv')}}
    pt.write_json(output/'evaluation.json', evaluation)
    pt.write_json(output/'manifest.json', {'database_sha256': database_hash, 'source_models_sha256': hashes,
                                         'artifacts_sha256': {p.name: pt.sha256(p) for p in output.iterdir() if p.is_file()}})
    pt.checkpoint(progress, cancelled, 'completed', 1)
    return {'mode': 'cross_cohort_evaluation', 'raw': evaluation, 'summary': summaries['test'], 'warnings': limitations,
            'provenance': {'evidence': 'frozen_cross_cohort_reused_holdout', 'input_synthetic': False,
                           'input_sha256': database_hash, 'input_source': database.name, 'database_path': str(database),
                           'source_run_dir': str(source), 'source_models_sha256': hashes,
                           'protocol_sha256': lock['protocol_sha256'], 'evaluation_path': str(output/'evaluation.json')}}
