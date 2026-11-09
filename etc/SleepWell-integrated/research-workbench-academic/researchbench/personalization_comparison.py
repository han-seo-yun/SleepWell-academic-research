"""Four fixed procedures, grouped CV and causal per-person personalization.

Existing experiment modules/artifacts remain immutable. Online state updates use
only earlier available outcomes; global regressors never fit evaluation people.
"""
from __future__ import annotations

import itertools
import json
import platform
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.exceptions import ConvergenceWarning
from threadpoolctl import threadpool_limits

from . import controlled_comparison as cc
from . import cross_evaluation as cross
from . import participant_revalidation as rv
from . import pooled_training as pt

STATUS = 'retrospective_grouped_cv'
MODELS = ('data33', 'data33_residual', 'hybrid39', 'hybrid39_personalized')
FIT_MODELS = ('data33', 'hybrid39', 'hybrid39_personalized')
PAIRS = tuple(itertools.combinations(MODELS, 2))
OFFSETS = np.array([(0., 0., 0.)] + [x for x in itertools.product(
    (-60., 0., 60.), (-.04, 0., .04), (-.04, 0., .04)) if x != (0., 0., 0.)])
PHYSICS_POLICY = {
    'offset_candidates': OFFSETS.tolist(), 'half_life_days': 14.,
    'huber_delta_min': 60., 'penalty_per_normalized_squared_offset': 8400.,
    'score': 'sum(2**(-outcome_age_days/14) * mean_endpoint_huber60(actual-candidate_prediction)) + 8400*sum((offset/[60,.04,.04])**2)',
    'huber60': '.5*r**2 if abs(r)<=60 else 60*(abs(r)-30)',
    'tie_rule': 'first enumerated minimum; zero offset first, then lexicographic product',
    'parameter_scope': 'bounded engineering offsets, not identified individual physiology; tau values fixed',
    'availability': 'same person, earlier cutoff, wake and observed_at strictly before current cutoff',
    'candidate_prediction': 'forecast made from history available at that past cutoff; never reconstructed using future history',
    'fallback': 'same existing habit onset/wake fallback for invalid transitions; fallback losses retained',
    'penalty_basis': '14 * huber60(60) / 3 = 8400; prespecified engineering regularization, not literature-validated',
}
RESIDUAL_POLICY = {'half_life_days': 14., 'shrinkage_lambda': 14., 'minimum_history': 1,
                   'residual_clip_min': 180., 'correction_cap_min': 60.,
                   'base': 'actual minus same-fold projected DATA33 prediction, never corrected prediction',
                   'projection': 'common constrain applied again after adding correction'}
SELECTION_RULE = {
    'default': 'data33', 'minimum_macro_improvement_min': 1., 'paired_ci_lower_above': 0.,
    'maximum_cohort_worsening_min': 1., 'maximum_cold_start_macro_worsening_min': 1.,
    'tie_order': list(MODELS[1:]),
    'rule': 'Eligible only if all thresholds pass; lowest Macro MAE among eligible, exact ties use listed order; otherwise DATA33',
    'meaning': 'engineering presentation recommendation, not clinical meaningfulness or independent validation of a selected model',
}
LIMITATIONS = [
    '기존 결과를 본 뒤 설계한 고정 절차의 내부 참여자 교차검증입니다. 과거 모델 선택 편향이 사라진 깨끗한 최종 Test가 아닙니다.',
    '학습·평가 참여자는 분리하고 전처리·회귀계수는 학습 참여자만 사용합니다. 평가 참여자의 이전에 관측 완료된 결과는 이후 개인 상태 갱신에만 사용합니다.',
    '위상·임계값 개인화는 제한된 27개 오프셋 후보의 과거 예측오차에 따른 선택입니다. 생리상수 측정·식별이 아니며 수면압력 시간상수는 고정합니다.',
    '95% 구간은 반복별 손실을 평균한 참여자 손실의 조건부 기술적 부트스트랩입니다. 재학습·겹치는 fold·과거 선택·최종 모델 추천의 불확실성을 모두 반영하지 않습니다.',
    '발표 모델의 1분 기준은 사전 고정한 공학적 복잡도 판단 기준이며 임상적 최소 차이·동등성 기준이 아닙니다. 추천 모델의 CV 점수는 독립 확증 성능이 아닙니다.',
    '앞서 평가한 MESA 20명은 이미 노출되어 이번 실행에서 사용하지 않습니다. 새로운 외부·전향·일반 사용자 검증은 없습니다.',
    'CMI/MESA 라벨·인구·기기 차이, MESA의 짧은 이력은 남습니다. 삼성은 제외하고 설치 앱 모델은 변경하지 않습니다.',
]


def code_hashes():
    files = [__file__, cc.__file__, cross.__file__, rv.__file__, pt.__file__]
    files += [pt.vendor_module(n).__file__ for n in ('night_core', 'robust_features', 'adaptive_personalization')]
    return {str(Path(p).resolve()): pt.sha256(p) for p in files}


def physics_candidates(features):
    """Vectorize the existing 5-minute two-process solver across rows/candidates.

    No outcome column is accepted here. Offset zero is the original solver.
    """
    core = pt.vendor_module('night_core')
    n, k = len(features), len(OFFSETS)
    habit = features[['habit_bed', 'habit_wake']].to_numpy(float)
    phase = (1200. + habit.mean(axis=1)) % 1440
    phase = phase[:, None] + OFFSETS[None, :, 0]
    high = features.sleep_threshold.to_numpy()[:, None] + OFFSETS[None, :, 1]
    low = features.wake_threshold.to_numpy()[:, None] + OFFSETS[None, :, 2]
    s = np.broadcast_to(features.current_s.to_numpy()[:, None], (n, k)).copy()
    onset, wake = np.full((n, k), np.nan), np.full((n, k), np.nan)
    awake_factor = np.exp(-(5 / 60) / core.TAU_WAKE)
    sleep_factor = np.exp(-(5 / 60) / core.TAU_SLEEP)
    for offset in range(0, 1441, 5):
        sleeping = np.isfinite(onset)
        if offset:
            s = np.where(sleeping, s * sleep_factor, 1 - (1 - s) * awake_factor)
        drive = s + core.C_AMPLITUDE * np.cos(np.pi * 2 * (1200 + offset - phase) / 1440)
        begin = ~sleeping & (offset <= 960) & (drive >= high)
        end = sleeping & np.isnan(wake) & (offset >= onset + 30) & (drive <= low)
        onset[begin], wake[end] = float(offset), float(offset)
    fallback = (~np.isfinite(onset) | ~np.isfinite(wake) | (wake-onset < 30)
                | (wake-onset > 960) | (high <= low))
    onset = np.where(fallback, habit[:, 0, None], onset)
    wake = np.where(fallback, habit[:, 1, None], wake)
    return np.stack([onset, wake], axis=-1), fallback, OFFSETS.copy()


def huber60(residual):
    a = np.abs(np.asarray(residual, float))
    return np.where(a <= 60, .5*a*a, 60*(a-30))


def physics_features(frame, features, candidates=None):
    """Replay bounded physics offsets; current/future outcomes cannot select them."""
    if not frame.index.equals(features.index):
        raise ValueError('Feature rows must align with episode rows')
    prediction, fallback, offsets = physics_candidates(features) if candidates is None else candidates
    if prediction.shape != (len(frame), len(OFFSETS), 2):
        raise ValueError('Unexpected physics candidate shape')
    core = pt.vendor_module('night_core')
    result = features.copy()
    position = {label: i for i, label in enumerate(frame.index)}
    cutoff = pd.to_datetime(frame.cutoff, utc=True)
    wake = pd.to_datetime(frame.wake, utc=True)
    available = pd.to_datetime(frame.observed_at, utc=True)
    targets = frame[pt.TARGETS].to_numpy(float)
    penalty = 8400. * ((offsets / np.array([60., .04, .04]))**2).sum(axis=1)
    diagnostics = []
    for _, group in frame.groupby(['cohort', 'subject_id'], sort=True):
        previous = []
        for label in group.sort_values('cutoff').index:
            i = position[label]
            eligible = [j for j in previous if wake.iloc[j] < cutoff.iloc[i] and available.iloc[j] < cutoff.iloc[i]]
            score = penalty.copy()
            mass = 0.
            if eligible:
                ages = (cutoff.iloc[i] - wake.iloc[eligible]).dt.total_seconds().to_numpy()/86400
                weights = 2.**(-ages/14)
                loss = huber60(targets[eligible, None, :] - prediction[eligible]).mean(axis=2)
                score += (weights[:, None]*loss).sum(axis=0)
                mass = float(weights.sum())
            chosen = int(np.argmin(score))
            delta = offsets[chosen]
            phase = (1200. + (features.loc[label, 'habit_bed'] + features.loc[label, 'habit_wake'])/2 + delta[0]) % 1440
            result.loc[label, ['current_c', 'sleep_threshold', 'wake_threshold', 'physical_bed', 'physical_wake']] = [
                core.C_AMPLITUDE*np.cos(2*np.pi*(1200-phase)/1440),
                features.loc[label, 'sleep_threshold']+delta[1], features.loc[label, 'wake_threshold']+delta[2],
                *prediction[i, chosen]]
            diagnostics.append({'row_index': label, 'physics_candidate': chosen,
                'phase_offset_min': float(delta[0]), 'sleep_threshold_offset': float(delta[1]),
                'wake_threshold_offset': float(delta[2]), 'physics_eligible_count': len(eligible),
                'physics_decayed_mass': mass, 'physics_fallback': bool(fallback[i, chosen]),
                'physics_zero_fallback': bool(fallback[i, 0])})
            previous.append(i)
    return result, pd.DataFrame(diagnostics).set_index('row_index').loc[frame.index]


def replay_features(frame, prior, progress=None, cancelled=None):
    normal, _, counts = pt.replay_features(frame, prior, progress, cancelled, 'causal_history')
    pt.checkpoint(progress, cancelled, 'physics_candidate_replay', 0)
    personal, diagnostics = physics_features(frame, normal)
    pt.checkpoint(progress, cancelled, 'physics_candidate_replay', 1)
    return normal, personal, counts, diagnostics


def residual_predictions(frame, base_predictions):
    """Residuals always reference the frozen DATA33 output, never recursive output."""
    module = pt.vendor_module('adaptive_personalization')
    core = pt.vendor_module('night_core')
    base_predictions = np.asarray(base_predictions, float)
    if base_predictions.shape != (len(frame), 2) or not np.isfinite(base_predictions).all():
        raise ValueError('Invalid DATA33 predictions')
    corrected = base_predictions.copy()
    positions = {label: i for i, label in enumerate(frame.index)}
    diagnostics = []
    for _, group in frame.groupby(['cohort', 'subject_id'], sort=True):
        history = []
        for row in group.sort_values('cutoff').itertuples():
            i = positions[row.Index]
            adjustment = module.compute_adjustment(history, row.cutoff, half_life_days=14., shrinkage_lambda=14., minimum_history=1)
            correction = np.array([adjustment['onset_correction_min'], adjustment['wake_correction_min']])
            corrected[i] = core.constrain(*(base_predictions[i]+correction))
            diagnostics.append({'row_index': row.Index, 'residual_eligible_count': adjustment['eligible_count'],
                                'residual_weight': adjustment['weight'],
                                'onset_correction_min': float(correction[0]), 'wake_correction_min': float(correction[1])})
            # Outcome becomes usable only at its actual availability timestamp.
            history.append({'night_id': row.cutoff, 'outcome_at': row.wake, 'available_at': row.observed_at,
                            'onset_residual_min': float(row.bedtime_min-base_predictions[i, 0]),
                            'wake_residual_min': float(row.wake_min-base_predictions[i, 1])})
    return corrected, pd.DataFrame(diagnostics).set_index('row_index').loc[frame.index]


def fit_fold(train, seed, progress=None, cancelled=None):
    if set(train.split) != {'train'}:
        raise ValueError('Only fold-training participants may fit models')
    prior = pt.prior_from_train(train)
    normal, personal, _, _ = replay_features(train, prior, progress, cancelled)
    weights = pt.balanced_weights(train)
    bundles = {'prior': prior}
    for name in FIT_MODELS:
        names = cc.feature_names('data' if name == 'data33' else 'hybrid')
        source = personal if name == 'hybrid39_personalized' else normal
        preprocessor = cc.WeightedPreprocessor(None).fit(source[names], sample_weight=weights)
        estimator = pt.make_model('huber', seed).named_steps['model']
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ConvergenceWarning)
            estimator.fit(preprocessor.transform(source[names]), train[pt.TARGETS].to_numpy(float), sample_weight=weights)
        if any(issubclass(w.category, ConvergenceWarning) for w in caught):
            raise ValueError('Fixed Huber did not converge; do not tune after looking at OOF')
        bundles[name] = {'features': names, 'preprocessor': preprocessor, 'model': estimator}
    return bundles


def evaluate_fold(held, bundles, progress=None, cancelled=None):
    normal, personal, counts, diagnostics = replay_features(held, bundles['prior'], progress, cancelled)
    predictions = {}
    for name in FIT_MODELS:
        bundle = bundles[name]
        source = personal if name == 'hybrid39_personalized' else normal
        predictions[name] = cross.constrain_predictions(bundle['model'].predict(bundle['preprocessor'].transform(source[bundle['features']])))
    predictions['data33_residual'], residual = residual_predictions(held, predictions['data33'])
    return {name: predictions[name] for name in MODELS}, counts, normal.recent_count14.to_numpy(int), diagnostics.join(residual)


def prepare_comparison(config):
    output = Path(config['output_dir']).resolve()
    if output.exists():
        raise FileExistsError('New experiment directory required')
    frame, manifest, digest = pt.load_database(config['database_path'])
    values = rv.settings(config)
    assignments = rv.fold_assignments(frame, values['cv_folds'], values['cv_repeats'], values['seed'])
    output.mkdir(parents=True, exist_ok=False)
    assignments.to_csv(output/'private_fold_assignments.csv', index=False)
    protocol = {
        'schema': 'sleepwell.personalization_comparison_protocol.v1', 'created_utc': rv.now(), 'status': STATUS,
        **values, 'database_path': str(Path(config['database_path']).resolve()), 'database_sha256': digest,
        'database_counts': manifest['summary'], 'cohorts': list(pt.COHORTS), 'models': list(MODELS),
        'code_sha256': code_hashes(), 'features': {d: cc.feature_names(d) for d in cc.DESCRIPTORS},
        'huber': {'alpha': 1., 'epsilon': 1.35, 'max_iter': 1000, 'tol': 1e-5},
        'preprocessing': 'Training-only weighted median imputation, constant removal, weighted standardization; no new clipping, QC or outcome deletion',
        'weights': 'CMI:MESA 50:50 total weight; equal participants within each cohort, normalized mean sample weight one',
        'prior': 'same equal-cohort/equal-participant fold-training prior for all four arms',
        'partition': '(cohort,subject_id), 5-fold x 2 defaults, same original cohort-stratified assignments; historical Train/Validation/Test all development',
        'history': 'Finished and available earlier records only; each person starts empty; predict before any outcome enters personalization',
        'physics_personalization': PHYSICS_POLICY, 'residual_personalization': RESIDUAL_POLICY,
        'physical_time_constants_hours': {'wake': 18.2, 'sleep': 4.2},
        'physics_state': 'current_s unchanged; phase/threshold offsets update current_c, sleep_threshold, wake_threshold, physical_bed/wake; DATA33 part identical',
        'fit_policy': 'Refit all 3 regression procedures and preprocessing within each fold; DATA residual shares exact DATA33 fit; adaptive physics replay also used in training features',
        'lock_order': 'Protocol and partitions locked first; all 10 CV sets plus full-development set fitted/hashed before ANY OOF prediction or score',
        'projection': 'same night_core.constrain for every final prediction; residual correction followed by projection',
        'primary_metric': 'per-night endpoint absolute-error mean -> per-person mean -> mean repeat losses -> equal-person cohort -> equal-cohort Macro',
        'uncertainty': '5000 default paired participant bootstrap on repeat-averaged losses, identical resampling across arms, conditional descriptive intervals only',
        'comparisons': [list(pair) for pair in PAIRS], 'history_bands': list(cross.HISTORY_BANDS),
        'primary_comparisons': [['data33', 'data33_residual'], ['hybrid39', 'hybrid39_personalized']],
        'selection_rule': SELECTION_RULE, 'historical_results_influenced_design': True,
        'new_oof_results_used_for_tuning': False, 'nested_cv': False,
        'pristine_holdout': False, 'mesa20_policy': 'Excluded from this experiment because its previous results have already been inspected',
        'service_promotion': False, 'limitations': LIMITATIONS,
        'environment': {'python': platform.python_version(), 'numpy': np.__version__, 'pandas': pd.__version__, 'sklearn': sklearn.__version__},
        'references': ['https://scikit-learn.org/stable/common_pitfalls.html#data-leakage',
                       'https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators-for-grouped-data',
                       'https://pubmed.ncbi.nlm.nih.gov/20394318/', 'https://doi.org/10.48550/arXiv.2609.00065'],
    }
    pt.write_json(output/'protocol.json', protocol)
    lock = {'locked_utc': rv.now(), 'protocol_sha256': pt.sha256(output/'protocol.json'),
            'fold_assignments_sha256': pt.sha256(output/'private_fold_assignments.csv'),
            'status': 'procedure_locked_before_refitting'}
    pt.write_json(output/'procedure_lock.json', lock)
    return lock


def verify_lock(config):
    output = Path(config['output_dir']).resolve()
    protocol = json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    lock = json.loads((output/'procedure_lock.json').read_text(encoding='utf-8'))
    for key, name in [('protocol_sha256', 'protocol.json'), ('fold_assignments_sha256', 'private_fold_assignments.csv')]:
        if pt.sha256(output/name) != lock[key]:
            raise ValueError('Procedure lock changed: '+name)
    if protocol['code_sha256'] != code_hashes():
        raise ValueError('Procedure code changed after lock')
    if any(protocol[k] != v for k, v in rv.settings(config).items()):
        raise ValueError('Settings differ from lock')
    if (str(Path(config['database_path']).resolve()) != protocol['database_path']
            or pt.sha256(config['database_path']) != protocol['database_sha256']):
        raise ValueError('Database differs from lock')
    return protocol, lock, pd.read_csv(output/'private_fold_assignments.csv', dtype={'subject_id': str})


def recommendation(metrics, paired, history):
    lookup = {(r['model'], r['cohort']): r['joint_mae_min'] for r in metrics}
    cold = {(r['model'], r['cohort']): r['joint_mae_min'] for r in history if r['history_band'] == '0'}
    rows = []
    for model in MODELS[1:]:
        pair = next(r for r in paired if r['reference'] == 'data33' and r['model'] == model and r['cohort'] == 'MACRO')
        improvement = lookup['data33', 'MACRO'] - lookup[model, 'MACRO']
        worsening = max(lookup[model, c]-lookup['data33', c] for c in pt.COHORTS)
        cold_worsening = cold[model, 'MACRO']-cold['data33', 'MACRO']
        eligible = improvement >= 1 and pair['ci95_low'] > 0 and worsening <= 1 and cold_worsening <= 1
        rows.append({'model': model, 'macro_improvement_min': improvement,
                     'paired_ci95_low': pair['ci95_low'], 'worst_cohort_worsening_min': worsening,
                     'cold_start_macro_worsening_min': cold_worsening, 'eligible': bool(eligible)})
    eligible = [r['model'] for r in rows if r['eligible']]
    selected = min(eligible, key=lambda m: (lookup[m, 'MACRO'], MODELS.index(m))) if eligible else 'data33'
    return {'recommended_model': selected, 'rule': SELECTION_RULE, 'candidates': rows,
            'interpretation': 'Exploratory presentation recommendation; selected-model score is not independent confirmation',
            'automatic_app_promotion': False}


def check_coverage(private, frame, repeats):
    keys = ['cohort', 'subject_id', 'cutoff']
    expected = set(map(tuple, frame[keys].to_numpy()))
    if len(expected) != len(frame):
        raise ValueError('Duplicate input episodes')
    for repeat in range(repeats):
        for model in MODELS:
            part = private.loc[private.repeat.eq(repeat) & private.model.eq(model)]
            if len(part) != len(frame) or set(map(tuple, part[keys].to_numpy())) != expected:
                raise ValueError('Incomplete OOF coverage')
    if len(private) != len(frame)*len(MODELS)*repeats:
        raise ValueError('Unexpected OOF row count')
    if private.groupby(keys).recent_count14.nunique().max() != 1:
        raise ValueError('History differs across arms or repeats')


def summarize(private, frame, protocol):
    replicates, seed = protocol['bootstrap_replicates'], protocol['seed']
    by_repeat, people = rv.participant_losses(private)
    metrics, errors, boots = rv.summarize_losses(people, frame.groupby('cohort').size().to_dict(), replicates, seed)
    paired = rv.paired_summaries(errors, boots, PAIRS)
    for row in paired:
        row['primary'] = [row['reference'], row['model']] in protocol['primary_comparisons']
    history, history_paired, folds, repeats = [], [], [], []
    for band in cross.HISTORY_BANDS:
        subset = private.loc[private.history_band.eq(band)]
        if subset.empty:
            continue
        _, p = rv.participant_losses(subset)
        nights = subset[['cohort', 'subject_id', 'cutoff']].drop_duplicates().groupby('cohort').size().to_dict()
        hm, he, hb = rv.summarize_losses(p, nights, replicates, seed)
        history.extend(dict(r, history_band=band) for r in hm)
        history_paired.extend(dict(r, history_band=band) for r in rv.paired_summaries(he, hb, PAIRS))
    for repeat, group in private.groupby('repeat'):
        for fold, subset in [(None, group), *list(group.groupby('fold'))]:
            _, p = rv.participant_losses(subset)
            means = p.groupby(['model', 'cohort']).mean()
            target = repeats if fold is None else folds
            for (model, cohort), row in means.iterrows():
                target.append({'repeat': repeat, **({} if fold is None else {'fold': fold}),
                               'model': model, 'cohort': cohort, 'joint_mae_min': float(row.joint_mae_min)})
            for model, rows in means.groupby(level='model'):
                target.append({'repeat': repeat, **({} if fold is None else {'fold': fold}),
                               'model': model, 'cohort': 'MACRO', 'joint_mae_min': float(rows.joint_mae_min.mean())})
    return {'metrics': metrics, 'paired_comparisons': paired, 'history_metrics': history,
            'history_paired_comparisons': history_paired, 'fold_metrics': folds, 'repeat_metrics': repeats}, people


def run_comparison(config, progress=None, cancelled=None):
    output = Path(config['output_dir']).resolve()
    protocol, lock, assignments = verify_lock(config)
    if (output/'fit_started.json').exists():
        raise FileExistsError('Locked run already started; refusing overwrite')
    frame, _, _ = pt.load_database(config['database_path'])
    folds, repeats, seed = (protocol[k] for k in ('cv_folds', 'cv_repeats', 'seed'))
    pd.testing.assert_frame_equal(assignments, rv.fold_assignments(frame, folds, repeats, seed))
    pt.write_json(output/'fit_started.json', {'started_utc': rv.now(), 'procedure_lock_sha256': pt.sha256(output/'procedure_lock.json')})
    model_dir = output/'fold_models'
    model_dir.mkdir()
    hashes, audit = {}, []
    with threadpool_limits(limits=2):
        for repeat in range(repeats):
            for fold in range(folds):
                pt.checkpoint(progress, cancelled, f'fit_repeat_{repeat+1}_fold_{fold+1}', (repeat*folds+fold)/(folds*repeats))
                train, held = rv.fold_frames(frame, assignments, repeat, fold)
                bundles = fit_fold(train, seed, progress, cancelled)
                name = f'repeat_{repeat}_fold_{fold}.joblib'
                joblib.dump(bundles, model_dir/name)
                hashes[name] = pt.sha256(model_dir/name)
                for cohort in pt.COHORTS:
                    audit.append({'repeat': repeat, 'fold': fold, 'cohort': cohort,
                                  'train_participants': int(train.loc[train.cohort.eq(cohort)].subject_id.nunique()),
                                  'evaluation_participants': int(held.loc[held.cohort.eq(cohort)].subject_id.nunique()),
                                  'train_nights': int(train.cohort.eq(cohort).sum()), 'evaluation_nights': int(held.cohort.eq(cohort).sum()),
                                  'participant_overlap': 0, 'converged': True})
                pd.DataFrame(audit).to_csv(output/'fold_audit.csv', index=False)
        all_train = frame.copy()
        all_train['split'] = 'train'
        pt.checkpoint(progress, cancelled, 'fit_all_development_before_oof', 0)
        joblib.dump(fit_fold(all_train, seed, progress, cancelled), model_dir/'all_development.joblib')
        hashes['all_development.joblib'] = pt.sha256(model_dir/'all_development.joblib')
        model_lock = {'locked_utc': rv.now(), 'models_sha256': hashes, 'protocol_sha256': lock['protocol_sha256'],
                      'all_models_locked_before_oof_prediction': True}
        pt.write_json(output/'all_fold_models_lock.json', model_lock)
        pt.checkpoint(progress, cancelled, 'ALL_MODELS_LOCKED_BEFORE_OOF', 1)
        parts, diagnostics = [], []
        pt.write_json(output/'evaluation_started.json', {'started_utc': rv.now(), 'models_lock_sha256': pt.sha256(output/'all_fold_models_lock.json')})
        for repeat in range(repeats):
            for fold in range(folds):
                pt.checkpoint(progress, cancelled, f'evaluate_repeat_{repeat+1}_fold_{fold+1}', (repeat*folds+fold)/(folds*repeats))
                _, held = rv.fold_frames(frame, assignments, repeat, fold)
                name = f'repeat_{repeat}_fold_{fold}.joblib'
                if pt.sha256(model_dir/name) != hashes[name]:
                    raise ValueError('Frozen model changed')
                predictions, counts, recent, diag = evaluate_fold(held, joblib.load(model_dir/name), progress, cancelled)
                part = cc.private_predictions(held, predictions, counts, 'out_of_fold')
                part['repeat'], part['fold'] = repeat, fold
                part['recent_count14'] = np.tile(recent, len(MODELS))
                part['history_band'] = part.recent_count14.map(cross.history_band)
                parts.append(part)
                diag = held[['cohort', 'subject_id', 'cutoff', 'observed_at']].join(diag)
                diag['repeat'], diag['fold'], diag['history_band'] = repeat, fold, [cross.history_band(x) for x in recent]
                diagnostics.append(diag)
    private = pd.concat(parts, ignore_index=True)
    check_coverage(private, frame, repeats)
    private.to_csv(output/'private_oof_predictions.csv', index=False)
    diagnostic = pd.concat(diagnostics, ignore_index=True)
    diagnostic.to_csv(output/'private_personalization_diagnostics.csv', index=False)
    summaries, people = summarize(private, frame, protocol)
    people.to_csv(output/'private_participant_losses.csv')
    for name, rows in summaries.items():
        pd.DataFrame(rows).to_csv(output/('oof_metrics.csv' if name == 'metrics' else name+'.csv'), index=False)
    selected = recommendation(summaries['metrics'], summaries['paired_comparisons'], summaries['history_metrics'])
    pt.write_json(output/'model_recommendation.json', selected)
    diagnostic_rows = []
    for (cohort, band), group in diagnostic.groupby(['cohort', 'history_band']):
        diagnostic_rows.append({'cohort': cohort, 'history_band': band, 'n_person_night_repeats': len(group),
            'n_participants': int(group.subject_id.nunique()),
            'zero_candidate_fraction': float(group.physics_candidate.eq(0).mean()),
            'physics_fallback_fraction': float(group.physics_fallback.mean()),
            'physics_zero_fallback_fraction': float(group.physics_zero_fallback.mean()),
            'mean_physics_eligible_count': float(group.physics_eligible_count.mean()),
            'mean_physics_decayed_mass': float(group.physics_decayed_mass.mean()),
            'residual_active_fraction': float(group.residual_eligible_count.gt(0).mean()),
            'mean_abs_onset_correction_min': float(group.onset_correction_min.abs().mean()),
            'mean_abs_wake_correction_min': float(group.wake_correction_min.abs().mean())})
    pd.DataFrame(diagnostic_rows).to_csv(output/'personalization_diagnostics.csv', index=False)
    verify_lock(config)
    for name, digest in hashes.items():
        if pt.sha256(model_dir/name) != digest:
            raise ValueError('Model changed during evaluation')
    raw = {'schema': 'sleepwell.personalization_comparison.v1', 'status': STATUS, 'completed_utc': rv.now(),
           'protocol': protocol, 'selection': selected, 'model_lock': model_lock,
           'primary_pair': ['data33', 'data33_residual'], **summaries,
           'personalization_diagnostics': diagnostic_rows, 'limitations': LIMITATIONS,
           'verification': {'n_participants': int(frame[['cohort', 'subject_id']].drop_duplicates().shape[0]),
               'n_unique_nights': len(frame), 'oof_rows': len(private), 'models': 4, 'fold_models': len(hashes),
               'participant_overlap': 0, 'oof_once_per_repeat': True,
               'same_nights_all_models': True, 'all_fits_before_any_oof_prediction': True,
               'repeat_losses_averaged_not_predictions': True, 'pristine_external_validation': False},
           'artifacts': {'output_dir': str(output)}}
    pt.write_json(output/'evaluation.json', raw)
    pt.write_json(output/'manifest.json', {'database_sha256': protocol['database_sha256'],
                  'artifacts_sha256': {p.relative_to(output).as_posix(): pt.sha256(p) for p in output.rglob('*') if p.is_file()}})
    pt.checkpoint(progress, cancelled, 'completed', 1)
    return {'mode': 'personalization_comparison', 'raw': raw, 'summary': summaries['metrics'], 'warnings': LIMITATIONS,
            'provenance': {'evidence': STATUS, 'input_synthetic': False, 'input_sha256': protocol['database_sha256'],
                           'input_source': Path(config['database_path']).name, 'database_path': str(config['database_path']),
                           'protocol_sha256': lock['protocol_sha256'], 'evaluation_path': str(output/'evaluation.json')}}
