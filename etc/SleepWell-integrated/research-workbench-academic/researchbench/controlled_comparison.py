"""Matched CMI/MESA experiments with participant CV and frozen reused-Test scoring."""
from __future__ import annotations

import json
import platform
import warnings
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.exceptions import ConvergenceWarning
from threadpoolctl import threadpool_limits

from . import pooled_training as pt

STRATEGIES = ('separate', 'pooled', 'pooled_source', 'separate_shared')
PREPROCESSING = {'none': None, 'weighted_005_995': .005, 'weighted_01_99': .01}
DESCRIPTORS = ('data', 'hybrid')
PRIMARY = ('separate_data', 'pooled_data')


def weighted_quantile(values, quantiles, weights):
    """Inverse weighted empirical CDF; aggregate ties before taking quantiles."""
    values, weights = np.asarray(values, float), np.asarray(weights, float)
    q = np.asarray(quantiles, float)
    if values.ndim != 1 or weights.shape != values.shape or np.any((q < 0) | (q > 1)):
        raise ValueError('Invalid weighted quantile dimensions or probabilities')
    if not np.isfinite(weights).all() or np.any(weights < 0):
        raise ValueError('Weights must be finite and nonnegative')
    valid = np.isfinite(values) & (weights > 0)
    if not valid.any():
        raise ValueError('Weighted quantile needs a finite observation with positive weight')
    unique, inverse = np.unique(values[valid], return_inverse=True)
    mass = np.bincount(inverse, weights=weights[valid])
    cdf = np.cumsum(mass) / mass.sum()
    return unique[np.clip(np.searchsorted(cdf, q, side='left'), 0, len(unique) - 1)]


class WeightedPreprocessor(TransformerMixin, BaseEstimator):
    """Fit weighted imputation, optional clipping and scaling on numeric fit rows.

    Source one-hot columns are appended later and never clipped or standardized.
    """
    def __init__(self, clip_quantile=None):
        self.clip_quantile = clip_quantile

    def fit(self, X, y=None, sample_weight=None):
        X = np.asarray(X, float)
        if X.ndim != 2 or not len(X):
            raise ValueError('Expected nonempty 2D training features')
        weights = np.ones(len(X)) if sample_weight is None else np.asarray(sample_weight, float)
        if weights.shape != (len(X),) or not np.isfinite(weights).all() or np.any(weights < 0) or weights.sum() <= 0:
            raise ValueError('Invalid training weights')
        if self.clip_quantile is not None and not 0 < self.clip_quantile < .5:
            raise ValueError('clip_quantile must be between 0 and .5')
        self.n_features_in_ = X.shape[1]
        self.median_ = np.array([float(weighted_quantile(x, .5, weights))
                                if np.any(np.isfinite(x) & (weights > 0)) else 0.
                                for x in X.T])
        X = np.where(np.isfinite(X), X, self.median_)
        self.low_, self.high_ = np.full(X.shape[1], -np.inf), np.full(X.shape[1], np.inf)
        if self.clip_quantile is not None:
            bounds = np.array([weighted_quantile(x, [self.clip_quantile, 1-self.clip_quantile], weights) for x in X.T])
            self.low_, self.high_ = bounds[:, 0], bounds[:, 1]
        X = np.clip(X, self.low_, self.high_)
        self.mean_ = np.average(X, axis=0, weights=weights)
        variance = np.average((X-self.mean_)**2, axis=0, weights=weights)
        self.keep_ = (np.ptp(X[weights > 0], axis=0) > 1e-10) & (variance > 1e-20)
        if not self.keep_.any():
            raise ValueError('No varying numeric predictors')
        self.scale_ = np.sqrt(np.maximum(variance, 1e-20))
        return self

    def transform(self, X):
        X = np.asarray(X, float)
        if X.ndim != 2 or X.shape[1] != self.n_features_in_:
            raise ValueError('Feature dimensions changed')
        X = np.where(np.isfinite(X), X, self.median_)
        return ((np.clip(X, self.low_, self.high_)-self.mean_)/self.scale_)[:, self.keep_]


def encode_features(X, cohort, include_source=False):
    X, cohort = np.asarray(X, float), np.asarray(cohort)
    if len(X) != len(cohort) or not set(cohort).issubset(pt.COHORTS):
        raise ValueError('Only the known CMI/MESA sources can be encoded')
    if include_source:
        return np.column_stack([X, *(cohort == name for name in pt.COHORTS)]).astype(float)
    return X


def participant_folds(frame, folds=3, repeats=2, seed=20261104):
    """Stratify by cohort, group by person; return original dataframe index labels."""
    if folds < 2 or repeats < 1 or set(frame.split) != {'train'} or not frame.index.is_unique:
        raise ValueError('Participant CV requires unique-index Train rows and valid fold counts')
    for repeat in range(repeats):
        assignments = {}
        for number, cohort in enumerate(pt.COHORTS):
            people = sorted(frame.loc[frame.cohort.eq(cohort), 'subject_id'].unique())
            if len(people) < folds:
                raise ValueError('Too few people in a cohort for CV')
            rng = np.random.default_rng(seed + 1009*repeat + number)
            for fold, chunk in enumerate(np.array_split(rng.permutation(people), folds)):
                assignments.update({(cohort, person): fold for person in chunk})
        allocation = np.array([assignments[(row.cohort, row.subject_id)] for row in frame.itertuples()])
        for fold in range(folds):
            yield repeat, fold, frame.index[allocation != fold], frame.index[allocation == fold]


def feature_names(descriptors):
    module = pt.vendor_module('robust_features')
    return list(module.DATA_V2 if descriptors == 'data' else module.HYBRID_V2)


def build_feature_views(fit_frame, evaluation_frame, progress=None, cancelled=None):
    """Refit priors inside the fold, then causally replay each independent person."""
    shared_prior = pt.prior_from_train(fit_frame)
    shared_fit, _, _ = pt.replay_features(fit_frame, shared_prior, progress, cancelled, 'shared_fit_features')
    shared_eval, _, history_counts = pt.replay_features(evaluation_frame, shared_prior, progress, cancelled, 'shared_evaluation_features')
    own_fit, own_eval, priors = [], [], {}
    for cohort in pt.COHORTS:
        local_fit = fit_frame.loc[fit_frame.cohort.eq(cohort)]
        local_eval = evaluation_frame.loc[evaluation_frame.cohort.eq(cohort)]
        priors[cohort] = pt.prior_from_train(local_fit)
        a, _, _ = pt.replay_features(local_fit, priors[cohort], progress, cancelled, 'separate_fit_features')
        b, _, _ = pt.replay_features(local_eval, priors[cohort], progress, cancelled, 'separate_evaluation_features')
        own_fit.append(a)
        own_eval.append(b)
    return {'shared_fit': shared_fit, 'shared_eval': shared_eval,
            'separate_fit': pd.concat(own_fit).loc[fit_frame.index],
            'separate_eval': pd.concat(own_eval).loc[evaluation_frame.index],
            'shared_prior': shared_prior, 'separate_prior': priors,
            'history_counts': history_counts}


def prepare_setting(fit_frame, evaluation_frame, views, descriptors, preprocessing):
    names = feature_names(descriptors)
    weights = pt.balanced_weights(fit_frame)
    shared = WeightedPreprocessor(PREPROCESSING[preprocessing]).fit(
        views['shared_fit'][names], sample_weight=weights)
    state = {'names': names, 'shared': shared, 'separate': {},
             'shared_fit': shared.transform(views['shared_fit'][names]),
             'shared_eval': shared.transform(views['shared_eval'][names]),
             'separate_fit': {}, 'separate_eval': {}, 'weights': weights}
    for cohort in pt.COHORTS:
        a = fit_frame.cohort.eq(cohort).to_numpy()
        b = evaluation_frame.cohort.eq(cohort).to_numpy()
        local_weights = pt.balanced_weights(fit_frame.loc[a])
        transformer = WeightedPreprocessor(PREPROCESSING[preprocessing]).fit(
            views['separate_fit'].loc[a, names], sample_weight=local_weights)
        state['separate'][cohort] = transformer
        state['separate_fit'][cohort] = transformer.transform(views['separate_fit'].loc[a, names])
        state['separate_eval'][cohort] = transformer.transform(views['separate_eval'].loc[b, names])
    return state


def fit_strategy(strategy, fit_frame, evaluation_frame, views, prepared, candidate, seed):
    if strategy not in STRATEGIES:
        raise ValueError('Unknown strategy')
    core = pt.vendor_module('night_core')
    models = {}
    prediction = np.empty((len(evaluation_frame), 2))
    converged = True
    scopes = pt.COHORTS if strategy.startswith('separate') else ('ALL',)
    for scope in scopes:
        a = np.ones(len(fit_frame), bool) if scope == 'ALL' else fit_frame.cohort.eq(scope).to_numpy()
        b = np.ones(len(evaluation_frame), bool) if scope == 'ALL' else evaluation_frame.cohort.eq(scope).to_numpy()
        if strategy == 'separate':
            X, Z = prepared['separate_fit'][scope], prepared['separate_eval'][scope]
        else:
            X, Z = prepared['shared_fit'][a], prepared['shared_eval'][b]
        X = encode_features(X, fit_frame.loc[a, 'cohort'], strategy == 'pooled_source')
        Z = encode_features(Z, evaluation_frame.loc[b, 'cohort'], strategy == 'pooled_source')
        # All fits normalize the same per-cohort/equal-person rule to mean weight 1.
        # The candidate and penalty parameter are identical, not separately retuned.
        weights = pt.balanced_weights(fit_frame.loc[a])
        estimator = pt.make_model(candidate, seed).named_steps['model']
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ConvergenceWarning)
            estimator.fit(X, fit_frame.loc[a, pt.TARGETS].to_numpy(float), sample_weight=weights)
        converged &= not any(issubclass(w.category, ConvergenceWarning) for w in caught)
        raw = np.asarray(estimator.predict(Z), float)
        if raw.shape != (int(b.sum()), 2) or not np.isfinite(raw).all():
            raise ValueError('Invalid candidate predictions')
        prediction[b] = np.asarray([core.constrain(*row) for row in raw])
        models[scope] = estimator
    bundle = {'strategy': strategy, 'models': models, 'features': prepared['names'],
              'preprocessors': prepared['separate'] if strategy == 'separate' else {'ALL': prepared['shared']},
              'priors': views['separate_prior'] if strategy == 'separate' else {'ALL': views['shared_prior']},
              'candidate': candidate, 'source_encoding': list(pt.COHORTS) if strategy == 'pooled_source' else [],
              'personalization_policy': core.GRADUAL_POLICY}
    return bundle, prediction, bool(converged)


def predict_bundle(bundle, frame, views):
    names, strategy = bundle['features'], bundle['strategy']
    source = views['separate_eval'] if strategy == 'separate' else views['shared_eval']
    prediction = np.empty((len(frame), 2))
    core = pt.vendor_module('night_core')
    for scope, estimator in bundle['models'].items():
        mask = np.ones(len(frame), bool) if scope == 'ALL' else frame.cohort.eq(scope).to_numpy()
        preprocessor = bundle['preprocessors'][scope if strategy == 'separate' else 'ALL']
        X = preprocessor.transform(source.loc[mask, names])
        X = encode_features(X, frame.loc[mask, 'cohort'], strategy == 'pooled_source')
        raw = np.asarray(estimator.predict(X), float)
        if raw.shape != (int(mask.sum()), 2) or not np.isfinite(raw).all():
            raise ValueError('Invalid saved-model prediction')
        prediction[mask] = [core.constrain(*row) for row in raw]
    return prediction


def scores(frame, prediction):
    errors = pt.participant_errors(frame, prediction).groupby(level='cohort').mean().joint_mae_min
    return {'macro_joint_mae_min': float(errors.mean()),
            **{c.lower()+'_joint_mae_min': float(errors[c]) for c in pt.COHORTS}}


def select_common_config(cv_rows):
    table = pd.DataFrame(cv_rows)
    primary = table.loc[table.descriptors.eq('data') & table.strategy.isin(('separate', 'pooled'))]
    eligible = primary.groupby(['preprocessing', 'candidate'], sort=False).converged.all()
    common = primary.groupby(['preprocessing', 'candidate'], sort=False).macro_joint_mae_min.mean()
    common = common.loc[eligible]
    if common.empty:
        raise ValueError('No fully converged shared configuration')
    # Enumeration order is protocol-fixed; idxmin returns its first exact tie.
    preprocessing, candidate = common.idxmin()
    return {'preprocessing': preprocessing, 'candidate': candidate,
            'cv_balanced_strategy_macro_mae_min': float(common.min()),
            'criterion': 'mean Train-CV macro MAE across separate_data and pooled_data; same configuration for all eight models'}


def comparison_rows(errors, bootstraps):
    pairs = [('separate_data', 'pooled_data'), ('separate_data', 'pooled_source_data'),
             ('separate_shared_data', 'pooled_data'), ('pooled_data', 'pooled_source_data'),
             *[(s+'_data', s+'_hybrid') for s in STRATEGIES]]
    results = []
    for reference, model in pairs:
        diffs, draws = [], []
        for cohort in pt.COHORTS:
            a = errors[reference].xs(cohort, level='cohort').joint_mae_min
            b = errors[model].xs(cohort, level='cohort').joint_mae_min
            if not a.index.equals(b.index):
                raise ValueError('Unpaired participants')
            value = float((a-b).mean())
            boot = bootstraps[reference][cohort]-bootstraps[model][cohort]
            low, high = np.quantile(boot, [.025, .975])
            results.append({'reference': reference, 'model': model, 'cohort': cohort,
                            'improvement_min': value, 'ci95_low': float(low), 'ci95_high': float(high),
                            'n_participants': len(a), 'primary': (reference, model) == PRIMARY})
            diffs.append(value)
            draws.append(boot)
        low, high = np.quantile(np.mean(draws, axis=0), [.025, .975])
        results.append({'reference': reference, 'model': model, 'cohort': 'MACRO',
                        'improvement_min': float(np.mean(diffs)), 'ci95_low': float(low),
                        'ci95_high': float(high), 'n_participants': len(errors[model]),
                        'primary': (reference, model) == PRIMARY})
    return results


def private_predictions(frame, predictions, history_counts, split):
    rows = []
    for model, prediction in predictions.items():
        for i, row in enumerate(frame.itertuples()):
            rows.append({'cohort': row.cohort, 'subject_id': row.subject_id, 'cutoff': row.cutoff,
                         'split': split, 'model': model, 'history_count': history_counts[i],
                         'actual_onset_min': row.bedtime_min, 'actual_wake_min': row.wake_min,
                         'predicted_onset_min': float(prediction[i, 0]),
                         'predicted_wake_min': float(prediction[i, 1]),
                         'duration_screen': 'short_lt180' if row.duration_min < 180 else
                         'long_gt720' if row.duration_min > 720 else 'central_180_720'})
    return pd.DataFrame(rows)


def preprocessing_audit(frame, views, prepared, descriptors, preprocessing, split):
    rows = []
    for view in ('shared', 'separate'):
        for cohort in pt.COHORTS:
            mask = frame.cohort.eq(cohort).to_numpy()
            transform = prepared['shared'] if view == 'shared' else prepared['separate'][cohort]
            X = views[view+'_eval'].loc[mask, prepared['names']].to_numpy(float)
            changed = ((X < transform.low_) | (X > transform.high_))
            rows.append({'view': view, 'cohort': cohort, 'split': split, 'descriptors': descriptors,
                         'preprocessing': preprocessing, 'n_nights': len(X),
                         'clipped_nights': int(changed.any(axis=1).sum()),
                         'clipped_nights_pct': float(100*changed.any(axis=1).mean()),
                         'clipped_cells_pct': float(100*changed.mean())})
    return rows


def run_controlled_comparison(config, progress=None, cancelled=None):
    output, database = Path(config['output_dir']).resolve(), Path(config['database_path']).resolve()
    if output.exists():
        raise FileExistsError('Choose a new experiment output directory')
    seed = int(config.get('seed', 20261104))
    folds, repeats = int(config.get('cv_folds', 3)), int(config.get('cv_repeats', 2))
    replicates = int(config.get('bootstrap_replicates', 5000))
    if not 2 <= folds <= 5 or not 1 <= repeats <= 5 or not 100 <= replicates <= 20000:
        raise ValueError('Invalid CV or bootstrap bounds')
    pt.checkpoint(progress, cancelled, 'verify_controlled_database', 0)
    frame, _, database_hash = pt.load_database(database)
    train = frame.loc[frame.split.eq('train')].copy()
    validation = frame.loc[frame.split.eq('validation')].copy()
    output.mkdir(parents=True, exist_ok=False)
    protocol = {
        'schema': 'sleepwell.controlled_comparison.v1', 'created_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'retrospective_reused_holdout', 'database_sha256': database_hash,
        'code_sha256': pt.sha256(__file__), 'seed': seed, 'cv_folds': folds, 'cv_repeats': repeats,
        'strategies': list(STRATEGIES), 'descriptors': {d: feature_names(d) for d in DESCRIPTORS},
        'preprocessing': PREPROCESSING, 'candidates': pt.CANDIDATES, 'primary_pair': list(PRIMARY),
        'primary_scope': 'CMI/MESA 50:50 macro; cohort rows are supporting analyses',
        'selection': 'single shared config selected by mean Train-CV separate/pooled DATA33 macro MAE; no Validation or Test tuning',
        'cv': f'cohort-stratified participant-disjoint {folds}-fold repeated {repeats} times with independent seeds; every prior and transform fit inside each fold',
        'separate': 'each cohort alone fits prior, numeric preprocessing and regressor',
        'separate_shared': 'common prior and numeric preprocessing; regressors fitted per cohort',
        'pooled_source': 'two fixed one-hot source columns appended after preprocessing; no ordinal or target encoding',
        'weights': 'equal people within cohort; shared fits give each cohort half; each fit normalized to mean sample weight 1',
        'numeric_preprocessing': 'weighted empirical-CDF median imputation, optional weighted quantile clipping, constant removal, weighted mean/SD scaling',
        'hgb': {'max_iter': 120, 'learning_rate': .05, 'l2_regularization': 10., 'early_stopping': False},
        'fit_split': 'original Train only; no Train+Validation refit',
        'validation_role': 'locked-model diagnostic evaluation only; no selection',
        'test_role': 'audit-informed reused historical holdout, not fresh external validation',
        'demographics': 'age/sex excluded: CMI entirely missing; no person ID or dates encoded',
        'qc': 'upstream target rules preserved; no outcome-based deletion; durations below180/above720 flagged for descriptive sensitivity only',
        'bootstrap': {'unit': 'participant within cohort', 'replicates': replicates, 'macro_weights': [.5, .5],
                      'scope': 'paired percentile 95%, conditional on fitted models; excludes fitting/selection uncertainty; secondary comparisons exploratory'},
        'plotting': 'cohort facets; joint-MAE CI only; paired-difference CI; full-range participant-balanced residual/MAE ECDF, no hidden fliers',
        'service_promotion': False,
        'environment': {'python': platform.python_version(), 'sklearn': sklearn.__version__, 'numpy': np.__version__},
    }
    pt.write_json(output/'protocol.json', protocol)
    cv_rows, fold_rows = [], []
    with threadpool_limits(limits=2):
        for repeat, fold, fit_indices, eval_indices in participant_folds(train, folds, repeats, seed):
            fit_frame, eval_frame = train.loc[fit_indices], train.loc[eval_indices]
            pt.checkpoint(progress, cancelled, f'cv_repeat_{repeat+1}_fold_{fold+1}_features', (repeat*folds+fold)/(repeats*folds))
            views = build_feature_views(fit_frame, eval_frame, cancelled=cancelled)
            for cohort in pt.COHORTS:
                fold_rows.append({'repeat': repeat, 'fold': fold, 'cohort': cohort,
                                  'fit_people': fit_frame.loc[fit_frame.cohort.eq(cohort)].subject_id.nunique(),
                                  'holdout_people': eval_frame.loc[eval_frame.cohort.eq(cohort)].subject_id.nunique(),
                                  'fit_nights': int(fit_frame.cohort.eq(cohort).sum()),
                                  'holdout_nights': int(eval_frame.cohort.eq(cohort).sum())})
            for descriptors in DESCRIPTORS:
                for preprocessing in PREPROCESSING:
                    prepared = prepare_setting(fit_frame, eval_frame, views, descriptors, preprocessing)
                    for candidate in pt.CANDIDATES:
                        pt.checkpoint(progress, cancelled, f'cv_{repeat+1}_{fold+1}_{descriptors}_{preprocessing}_{candidate}', len(cv_rows)/(folds*repeats*len(DESCRIPTORS)*len(PREPROCESSING)*len(pt.CANDIDATES)*len(STRATEGIES)))
                        for strategy in STRATEGIES:
                            _, predicted, converged = fit_strategy(strategy, fit_frame, eval_frame, views, prepared, candidate, seed)
                            cv_rows.append({'repeat': repeat, 'fold': fold, 'descriptors': descriptors,
                                            'preprocessing': preprocessing, 'candidate': candidate, 'strategy': strategy,
                                            'converged': converged, **scores(eval_frame, predicted)})
            # Checkpoint aggregate evidence only; no participant identifiers in project results.
            pd.DataFrame(cv_rows).to_csv(output/'cv_scores.csv', index=False)
            pd.DataFrame(fold_rows).to_csv(output/'cv_folds.csv', index=False)
        common = select_common_config(cv_rows)
        pt.write_json(output/'configuration_lock.json', {'locked_utc': datetime.now(timezone.utc).isoformat(),
                                                       **common, 'test_used_for_selection': False,
                                                       'validation_used_for_selection': False})
        pt.checkpoint(progress, cancelled, 'common_configuration_locked', 1)
        views = build_feature_views(train, validation, cancelled=cancelled)
        bundles, val_predictions, prep_rows = {}, {}, []
        for descriptors in DESCRIPTORS:
            prepared = prepare_setting(train, validation, views, descriptors, common['preprocessing'])
            prep_rows.extend(preprocessing_audit(validation, views, prepared, descriptors, common['preprocessing'], 'validation'))
            for strategy in STRATEGIES:
                name = strategy+'_'+descriptors
                bundle, prediction, converged = fit_strategy(strategy, train, validation, views, prepared, common['candidate'], seed)
                if not converged:
                    raise ValueError('Locked final model failed to converge; Test remains unopened')
                bundle.update(preprocessing=common['preprocessing'], database_sha256=database_hash)
                bundles[name], val_predictions[name] = bundle, prediction
                joblib.dump(bundle, output/(name+'.joblib'))
        lock = {'locked_utc': datetime.now(timezone.utc).isoformat(), 'common_config': common,
                'models_sha256': {name: pt.sha256(output/(name+'.joblib')) for name in bundles},
                'protocol_sha256': pt.sha256(output/'protocol.json'), 'test_used_for_selection': False,
                'validation_used_for_selection': False, 'automatic_winner_or_promotion': False}
        pt.write_json(output/'selection_lock.json', lock)
        pt.checkpoint(progress, cancelled, 'all_models_locked_before_test', 1)
        val_summary, _, _ = pt.metric_summaries(validation, val_predictions, replicates, seed, 'validation')
        private_predictions(validation, val_predictions, views['history_counts'], 'validation').to_csv(output/'private_validation_predictions.csv', index=False)
        # No Test feature construction or scores before both configuration and artifact locks.
        test = frame.loc[frame.split.eq('test')].copy()
        test_views = build_feature_views(train, test, cancelled=cancelled)
        test_predictions, reload_errors = {}, {}
        for name, bundle in bundles.items():
            if pt.sha256(output/(name+'.joblib')) != lock['models_sha256'][name]:
                raise ValueError('Locked artifact changed')
            prediction = predict_bundle(bundle, test, test_views)
            restored = joblib.load(output/(name+'.joblib'))
            delta = float(np.max(np.abs(prediction-predict_bundle(restored, test, test_views))))
            if delta > 1e-10:
                raise ValueError('Reloaded predictions differ')
            test_predictions[name], reload_errors[name] = prediction, delta
        for descriptors in DESCRIPTORS:
            # Reuse the saved fit transforms, not a refitted Test preprocessing stage.
            prepared = {'names': bundles['pooled_'+descriptors]['features'],
                        'shared': bundles['pooled_'+descriptors]['preprocessors']['ALL'],
                        'separate': bundles['separate_'+descriptors]['preprocessors']}
            prep_rows.extend(preprocessing_audit(test, test_views, prepared, descriptors, common['preprocessing'], 'test'))
    pt.checkpoint(progress, cancelled, 'controlled_participant_bootstrap', 0)
    test_summary, errors, boots = pt.metric_summaries(test, test_predictions, replicates, seed, 'test')
    comparisons = comparison_rows(errors, boots)
    private_predictions(test, test_predictions, test_views['history_counts'], 'test').to_csv(output/'private_test_predictions.csv', index=False)
    pd.DataFrame(test_summary).to_csv(output/'test_metrics.csv', index=False)
    pd.DataFrame(val_summary).to_csv(output/'validation_metrics.csv', index=False)
    pd.DataFrame(comparisons).to_csv(output/'paired_comparisons.csv', index=False)
    pd.DataFrame(prep_rows).to_csv(output/'preprocessing_audit.csv', index=False)
    table = pd.DataFrame(cv_rows)
    cv_summary = table.groupby(['descriptors', 'preprocessing', 'candidate', 'strategy'], sort=False).agg(
        cv_macro_mae=('macro_joint_mae_min', 'mean'), cv_fold_sd=('macro_joint_mae_min', 'std'),
        cmi_mae=('cmi_joint_mae_min', 'mean'), mesa_mae=('mesa_joint_mae_min', 'mean'),
        all_converged=('converged', 'all')).reset_index()
    cv_summary.to_csv(output/'cv_summary.csv', index=False)
    if pt.sha256(database) != database_hash:
        raise ValueError('Input database changed')
    limitations = [
        '재사용 Test 평가이며 새 외부·전향 검증이 아닙니다. 감사에서 본 결과도 실험 설계에 영향을 주었습니다.',
        '95% CI는 고정된 학습 모델의 사람 단위 조건부 구간이며 학습·후보 선택 불확실성은 포함하지 않습니다.',
        'CMI 사건 라벨과 MESA ACT 유래 라벨의 측정 차이는 유지됩니다. Macro는 두 출처 50:50 평균입니다.',
        '공통 설정의 통제 비교입니다. 각 전략의 최적 모델을 확정한 결과가 아닙니다. 보조 비교는 탐색적입니다.',
        '출처 인코딩은 알려진 CMI/MESA에만 적용됩니다. 나이·성별·개인 ID는 예측 입력에서 제외했습니다.',
        '기존 설치 모델은 교체하지 않았으며 수면 개선이나 임상 효용을 검증하지 않았습니다.',
    ]
    evaluation = {'schema': 'sleepwell.controlled_evaluation.v1', 'status': pt.STATUS,
                  'completed_utc': datetime.now(timezone.utc).isoformat(), 'selection': lock,
                  'primary_pair': list(PRIMARY), 'test_metrics': test_summary, 'validation_metrics': val_summary,
                  'paired_comparisons': comparisons, 'cv_summary': cv_summary.to_dict('records'),
                  'preprocessing_audit': prep_rows, 'limitations': limitations,
                  'verification': {'database_unchanged': True, 'test_used_for_selection': False,
                                   'validation_used_for_selection': False, 'samsung_rows': 0,
                                   'test_participants': len(errors[PRIMARY[0]]), 'test_nights': len(test),
                                   'reload_max_abs_error_min': reload_errors, 'cv_score_rows': len(cv_rows)},
                  'artifacts': {'output_dir': str(output), 'private_test_predictions': str(output/'private_test_predictions.csv')}}
    pt.write_json(output/'evaluation.json', evaluation)
    source = pt.get_provenance()
    pt.write_json(output/'manifest.json', {'database_sha256': database_hash, 'source_provenance': source,
                                         'artifacts_sha256': {p.name: pt.sha256(p) for p in output.iterdir() if p.is_file()}})
    pt.checkpoint(progress, cancelled, 'completed', 1)
    return {'mode': 'controlled_comparison', 'raw': evaluation, 'summary': test_summary,
            'warnings': limitations, 'provenance': {'evidence': 'matched_retraining_reused_holdout',
            'input_synthetic': False, 'input_sha256': database_hash, 'input_source': database.name,
            'database_path': str(database), 'protocol_sha256': lock['protocol_sha256'],
            'evaluation_path': str(output/'evaluation.json'), 'source_provenance': source}}
