"""Baseline-relative audit of frozen next-night predictions, no model refitting.

Methodological inspiration: Chatterjee (2026), passive-sensing-target-validity,
commit 627b1006a95b698a0f9f74ff19d499a8a0ef5a60. Independently implemented.
No GLOBEM code, data or fitted coefficients are redistributed by this module.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'web-app'))
from night_core import constrain, local_time, normalize_history

SEED = 20261025
BASELINES = ('population', 'last', 'mean7', 'mean14', 'weighted28', 'weekday')
TARGETS = ['bed_offset', 'wake_offset']


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def baseline_predictions(records, cutoff, prior, enrolled_at):
    """Calendar-day windows and strictly prior available outcomes only.

    Means use bounded minutes since 20:00, the same regression target as models.
    A last-observation baseline requires an observation in the last 28 days.
    All unavailable histories fall back to the TRAIN-only population prior.
    """
    cutoff = local_time(cutoff)
    history = normalize_history(records, cutoff, enrolled_at)
    population = np.array([prior['bed_offset'], prior['wake_offset']], dtype=float)
    windows = {n: [r for r in history if r['wake'] >= cutoff - timedelta(days=n)]
               for n in (7, 14, 28)}

    def center(rows, weighted=False):
        if not rows:
            return population.copy()
        values = np.array([[r['bed_offset'], r['wake_offset']] for r in rows])
        weights = np.array([2 ** (-(cutoff-r['wake']).total_seconds()/86400/14) for r in rows])
        return np.average(values, axis=0, weights=weights if weighted else None)

    recent = windows[28]
    weekday = (cutoff + timedelta(days=1)).weekday()
    same_day = [r for r in recent if r['wake'].weekday() == weekday]
    last = np.array([recent[-1]['bed_offset'], recent[-1]['wake_offset']]) if recent else population
    estimates = dict(population=population, last=last, mean7=center(windows[7]),
                     mean14=center(windows[14]), weighted28=center(recent, True),
                     weekday=center(same_day) if len(same_day) >= 2 else center(windows[14]))
    diagnostics = {'prior_records': len(history), 'recent14_records': len(windows[14]),
                   'weekday_supported': len(same_day) >= 2,
                   'gap_days': (cutoff-history[-1]['wake']).total_seconds()/86400 if history else None}
    return {name: constrain(*value) for name, value in estimates.items()}, diagnostics


def replay_baselines(frame, prior):
    predictions = {name: np.empty((len(frame), 2)) for name in BASELINES}
    diagnostics = [None] * len(frame)
    for _, group in frame.assign(_position=np.arange(len(frame))).groupby(['subject_id', 'split']):
        group = group.sort_values('cutoff')
        enrolled = local_time(str(group.iloc[0].cutoff))
        history = []
        for _, row in group.iterrows():
            index = int(row['_position'])
            pred, diag = baseline_predictions(history, str(row.cutoff), prior, enrolled)
            for name in BASELINES:
                predictions[name][index] = pred[name]
            diagnostics[index] = diag
            # The current target becomes eligible only for a later decision.
            history.append({'source': 'research', 'bed': str(row.bed), 'wake': str(row.wake),
                            'observed_at': str(row.observed_at)})
    return predictions, pd.DataFrame(diagnostics)


def metrics(actual, predicted, subjects):
    if not len(actual):
        return None
    errors = pd.DataFrame(np.abs(actual-predicted), columns=['onset', 'wake'])
    errors['joint'] = errors.mean(axis=1)
    participant = errors.assign(subject=np.asarray(subjects)).groupby('subject').mean()
    return participant, participant.mean()


def paired_summary(actual, predicted, reference, subjects):
    person, macro = metrics(actual, predicted, subjects)
    base, baseline = metrics(actual, reference, subjects)
    delta = base.joint-person.joint
    rng = np.random.default_rng(SEED)
    draws = delta.to_numpy()[rng.integers(0, len(delta), (10000, len(delta)))].mean(axis=1)
    return {
        'onset_mae_min': float(macro.onset), 'wake_mae_min': float(macro.wake),
        'joint_mae_min': float(macro.joint),
        'skill_vs_recent14': float(1-macro.joint/baseline.joint) if baseline.joint >= 1 else None,
        'gain_vs_recent14_min': float(delta.mean()),
        'ci95_min': np.quantile(draws, [.025, .975]).tolist(),
        'participant_improved_fraction': float((delta > 1e-8).mean()),
    }, person


def within_person_diagnostic(frame, predicted):
    """Held-out demeaning is evaluation only, never input or model selection."""
    output = {}
    for index, target in enumerate(TARGETS):
        obs = frame[target].to_numpy(float)
        p = pd.Series(predicted[:, index])
        groups = frame.subject_id.reset_index(drop=True)
        actual_dev = pd.Series(obs)-pd.Series(obs).groupby(groups).transform('mean')
        predicted_dev = p-p.groupby(groups).transform('mean')
        sse = ((actual_dev-predicted_dev)**2).groupby(groups).sum()
        sst = (actual_dev**2).groupby(groups).sum()
        counts = groups.groupby(groups).size()
        eligible = (counts >= 3) & (sst > 1e-8)
        scores = 1-sse[eligible]/sst[eligible]
        output[target] = {'eligible_people': int(eligible.sum()),
                          'excluded_short_or_constant_people': int((~eligible).sum()),
                          'median_centered_r2': float(scores.median()) if len(scores) else None}
    return output


def audit_cohort(project, run, cohort, output):
    source = project/'데이터모델/연구용데이터/next_night_run_20261020_v1/analysis'/cohort/'episodes.parquet'
    saved = run/cohort
    paths = [source] + [saved/f for f in ('train_selection.json', 'test_rows_private.parquet',
                                        'test_predictions_private.npz', 'test_results.json')]
    input_hashes = {str(p): sha(p) for p in paths}
    selection = json.loads(paths[1].read_text('utf-8'))
    if selection['source_sha256'] != sha(source):
        raise ValueError('Original training source hash mismatch')
    full = pd.read_parquet(source)
    if full.row_id.duplicated().any() or full.groupby('subject_id').split.nunique().max() != 1:
        raise ValueError('Duplicate rows or participant split overlap')
    frame = pd.read_parquet(saved/'test_rows_private.parquet').reset_index(drop=True)
    expected = full.loc[full.split.eq('test')].reset_index(drop=True)
    pd.testing.assert_frame_equal(frame, expected)
    # Legacy NPZ lacks row IDs. Independently replay frozen models in explicit
    # row-ID order; a same-length permutation must fail numerical equality.
    subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('verify_frozen_predictions.py')),
                    '--run', str(run), '--cohort', cohort,
                    '--output', str(output/f'{cohort}_frozen_prediction_verification.json')], check=True)
    # Independently verify that the fallback did not use held-out outcomes.
    train = full.loc[full.split.eq('train')]
    computed = train.groupby('subject_id')[TARGETS].mean().mean().to_numpy()
    prior = selection['prior']
    np.testing.assert_allclose(computed, [prior[x] for x in TARGETS], atol=1e-8, rtol=0)
    predictions, diagnostics = replay_baselines(frame, prior)
    archived = np.load(saved/'test_predictions_private.npz')
    chosen = selection['selected_daily_policy']
    for arm in ('physical', 'data', 'hybrid'):
        predictions[arm] = archived[f'{chosen}__{arm}']
        predictions[f'legacy_{arm}'] = archived[f'legacy14__{arm}']
    actual = frame[TARGETS].to_numpy(float)
    if any(v.shape != actual.shape or not np.isfinite(v).all() for v in predictions.values()):
        raise ValueError('Prediction shape/finiteness mismatch')
    reference = predictions['mean14']
    scores, within, private_people = [], {}, []
    for name, prediction in predictions.items():
        score, people = paired_summary(actual, prediction, reference, frame.subject_id)
        scores.append({'name': name, **score})
        within[name] = within_person_diagnostic(frame, prediction)
        people = people.reset_index().assign(model=name)
        private_people.append(people)
    pd.concat(private_people).to_csv(output/f'{cohort}_participant_metrics_private.csv', index=False)
    private = frame[['row_id', 'subject_id', 'cutoff', *TARGETS]].join(diagnostics)
    for name, prediction in predictions.items():
        private[f'{name}_onset'] = prediction[:, 0]
        private[f'{name}_wake'] = prediction[:, 1]
    private.to_parquet(output/f'{cohort}_predictions_private.parquet', index=False)
    strata = []
    for label, lo, hi in [('0',0,0),('1–3',1,3),('4–7',4,7),('8–13',8,13),('14+',14,1000000)]:
        mask = diagnostics.prior_records.between(lo,hi).to_numpy()
        if not mask.any():
            continue
        strata.append({'label':label, 'n_people':int(frame.loc[mask].subject_id.nunique()),
                       'n_nights':int(mask.sum()),
                       **{f'{name}_mae_min':float(metrics(actual[mask],predictions[arm][mask],frame.loc[mask,'subject_id'])[1].joint)
                          for name,arm in [('data','data'),('hybrid','hybrid'),('baseline','mean14')]}})
    max_history = diagnostics.assign(subject=frame.subject_id).groupby('subject').prior_records.max()
    # A descriptive construct audit: low error can reflect low variability.
    variability = frame.groupby('subject_id')[TARGETS].std().mean(axis=1)
    model_person = metrics(actual, predictions['hybrid'], frame.subject_id)[0].joint
    valid = variability.notna() & model_person.notna()
    corr = variability[valid].corr(model_person[valid], method='spearman') if valid.sum() >= 3 else None
    dump(output/f'{cohort}_diagnostics.json', {
        'within_person': within, 'heldout_variability_error_spearman':float(corr) if corr is not None and np.isfinite(corr) else None,
        'interpretation':'Descriptive association, not a physiological trait; centered R2 removes test-period mean only during evaluation.',
        'label_source_counts': frame.label_source.value_counts().to_dict(),
        'input_hashes':input_hashes,
    })
    for p, digest in input_hashes.items():
        if sha(Path(p)) != digest:
            raise ValueError('Input changed during audit')
    return {
        'n_people':int(frame.subject_id.nunique()), 'n_nights':len(frame),
        'label_source':str(frame.label_source.iloc[0]), 'models':scores, 'history_strata':strata,
        'feasibility':{'max_prior_records':int(max_history.max()),
                       'people_with_14_prior_records':int((max_history>=14).sum()),
                       'people_with_28_prior_records':int((max_history>=28).sum())},
        'availability_assumption':'Retrospective outcome availability equals wake time; not a prospective capture log.',
    }


def make_figures(artifact, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size':10, 'pdf.fonttype':42, 'svg.fonttype':'none'})
    names = ['population','last','mean7','mean14','weighted28','weekday','physical','data','hybrid']
    fig, axes = plt.subplots(1,2,figsize=(12,5), layout='constrained')
    for ax,(cohort,item) in zip(axes,artifact['cohorts'].items()):
        lookup={s['name']:s for s in item['models']}
        vals=[lookup[n]['joint_mae_min'] for n in names]
        ax.barh(names,vals,color=['#8997A7']*6+['#E0A44B','#3979A5','#529176'])
        ax.invert_yaxis(); ax.set_xlabel('Participant-macro joint MAE (min)')
        ax.set_title(f'{cohort.upper()}: {item["n_people"]} people, {item["n_nights"]} nights')
        for i,v in enumerate(vals): ax.text(v+.8,i,f'{v:.2f}',va='center',fontsize=8)
        ax.set_xlim(0,max(vals)*1.16); ax.spines[['top','right']].set_visible(False)
    for ext in ('png','svg','pdf'): fig.savefig(output/f'Figure_1_baseline_comparison.{ext}',dpi=400)
    plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(11,4),layout='constrained')
    for ax,(cohort,item) in zip(axes,artifact['cohorts'].items()):
        rows=[x for x in item['models'] if x['name'] in ('physical','data','hybrid')]
        values=np.array([x['gain_vs_recent14_min'] for x in rows]); ci=np.array([x['ci95_min'] for x in rows])
        ax.errorbar(values,np.arange(len(rows)),xerr=np.maximum(0,np.array([values-ci[:,0],ci[:,1]-values])),fmt='o',capsize=4,color='#3979A5')
        ax.axvline(0,color='grey',linestyle='--'); ax.set_yticks(range(len(rows)),[r['name'] for r in rows])
        ax.set_title(cohort.upper()); ax.set_xlabel('MAE reduction vs 14-calendar-day mean (min)')
        ax.spines[['top','right']].set_visible(False)
    for ext in ('png','svg','pdf'): fig.savefig(output/f'Figure_2_baseline_gain.{ext}',dpi=400)
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    protocol={'version':'2026-10-25-v1','seed':SEED,'comparison':'all frozen test nights, equal participant mean',
              'primary_comparator':'mean14: 14 calendar days, completed and available before cutoff',
              'baselines':list(BASELINES),'model_selection':'existing TRAIN-selected continuous constants policy',
              'uncertainty':'10000 paired participant bootstrap draws; unadjusted descriptive 95% intervals, no significance selection',
              'history_buckets':'0, 1-3, 4-7, 8-13, 14+; overlapping participants and changing composition, not causal personalization improvement',
              'weighted28':'28-calendar-day window; half-life 14 days; TRAIN prior when empty',
              'target_policy':'retain original targets; no error-driven exclusion or relabeling',
              'test_previously_exposed':True,'fresh_confirmatory_test':False,'service_promotion':False,
              'code_sha256':sha(Path(__file__)),
              'replay_verifier_sha256':sha(Path(__file__).with_name('verify_frozen_predictions.py')),
              'night_core_sha256':sha(Path(__file__).resolve().parents[2]/'web-app/night_core.py'),
              'software':{'python':platform.python_version(),'numpy':np.__version__,'pandas':pd.__version__}}
    dump(args.output/'protocol.json',protocol)
    run=args.project/'데이터모델/연구용데이터/continuous_constants_run_20261025_v2'
    artifact={'schema_version':1,'artifact_type':'sleepwell_target_validity_validation','version':'2026-10-25-v1',
              'test_status':'retrospective_reused_test','no_service_promotion':True,
              'cohorts':{c:audit_cohort(args.project,run,c,args.output) for c in ('cmi','mesa')},
              'limitations':[
                  '기존 Test를 재사용한 탐색적 검증이며 새 외부 확인시험이 아닙니다.',
                  '주 모델 표는 TRAIN에서 선택한 매일 상수 연구모델입니다. 앱에 탑재된 robust-v2와 구분합니다.',
                  'MAE는 입면·최종 기상 오차 평균입니다. 총수면시간·수면의 질·수면압력·추천 효과는 검증하지 않습니다.',
                  'CMI 사건 라벨과 MESA ACT 약한 라벨은 측정 조건이 다릅니다. PSG와의 밤별 일치도는 이 자료로 확인하지 않았습니다.',
                  '개인 개선 비율은 기술통계이고 각 개인의 유의한 개선 판정이 아닙니다.',
                  '동일 참여자가 여러 이력 구간에 반복 포함되며 구간별 표본 구성도 달라, 구간 간 성능 차이가 개인화의 인과적 효과는 아닙니다.',
                  '신뢰구간은 다중비교 보정 전 기술적 구간입니다. 여러 기준선 중 Test 승자를 서비스로 선택하지 않습니다.',
              ],'provenance':{'inspiration_url':'https://github.com/Reaper2403/passive-sensing-target-validity',
                              'inspiration_commit':'627b1006a95b698a0f9f74ff19d499a8a0ef5a60',
                              'protocol_sha256':sha(args.output/'protocol.json'),'code_sha256':sha(Path(__file__))}}
    dump(args.output/'target_validity_validation_v1.json',artifact)
    rows=[dict(cohort=c,**s) for c,item in artifact['cohorts'].items() for s in item['models']]
    pd.DataFrame(rows).to_csv(args.output/'aggregate_metrics.csv',index=False)
    make_figures(artifact,args.output)
    dump(args.output/'verification.json',{'all_original_test_rows_retained':True,'train_prior_recomputed':True,
                                        'frozen_prediction_row_order_reproduced':True,
                                        'inputs_unchanged':True,'participant_splits_disjoint':True,
                                        'public_artifact_contains_no_row_identifiers':True})
    print(json.dumps({c:{r['name']:round(r['joint_mae_min'],3) for r in item['models']} for c,item in artifact['cohorts'].items()},indent=2))


if __name__=='__main__':
    main()
