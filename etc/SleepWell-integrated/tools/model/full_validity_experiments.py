"""Extended, exploratory validity audit; fixed predictors, no Test tuning.

Independent implementation inspired by passive-sensing-target-validity.
Participant identifiers stay in *_private outputs and never in the public JSON.
"""
from __future__ import annotations
import argparse
from datetime import timedelta
import json
import math
from pathlib import Path
import sys
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from target_validity_experiments import (baseline_predictions, metrics, paired_summary,
    within_person_diagnostic, sha, dump, local_time, normalize_history, constrain)

SEED = 20261025
NEW_NAMES = ('mean28', 'circular7', 'circular14', 'weighted14', 'daytype28', 'calendar_train')


def circular_center(values, fallback):
    """Clock minutes on a circle; ambiguous resultant <0.1 falls back."""
    if not len(values):
        return float(fallback), False
    angles = np.asarray(values, float)*2*np.pi/1440
    x,y = np.cos(angles).mean(), np.sin(angles).mean()
    if math.hypot(x,y) < .1:
        return float(fallback), False
    return float((math.atan2(y,x)*1440/(2*np.pi))%1440), True


def extended_baselines(records, cutoff, prior, enrolled, calendar_prior):
    cutoff = local_time(cutoff)
    history = normalize_history(records, cutoff, enrolled)
    base, diag = baseline_predictions(records, cutoff, prior, enrolled)
    fallback = np.array([prior['bed_offset'], prior['wake_offset']])
    windows = {n:[r for r in history if r['wake'] >= cutoff-timedelta(days=n)] for n in (7,14,28)}
    def average(rows, weight=False):
        if not rows: return fallback.copy()
        v = np.array([[r['bed_offset'],r['wake_offset']] for r in rows])
        w = [2**(-(cutoff-r['wake']).total_seconds()/86400/14) for r in rows]
        return np.average(v,axis=0,weights=w if weight else None)
    base['mean28'] = constrain(*average(windows[28]))
    base['weighted14'] = constrain(*average(windows[14],True))
    for n in (7,14):
        rows=windows[n]
        b,bok=circular_center([r['bed_offset'] for r in rows],fallback[0])
        w,wok=circular_center([r['wake_offset'] for r in rows],fallback[1])
        if w<=b: w+=1440
        base[f'circular{n}']=constrain(b,w)
        diag[f'circular{n}_supported']=bok and wok
        diag[f'circular{n}_projected']=abs(base[f'circular{n}'][0]-b)>1e-8 or abs(base[f'circular{n}'][1]-w)>1e-8
    target_type=int((cutoff+timedelta(days=1)).weekday()>=5)
    matches=[r for r in windows[28] if int(r['wake'].weekday()>=5)==target_type]
    base['daytype28']=constrain(*average(matches if len(matches)>=2 else windows[14]))
    base['calendar_train']=constrain(*calendar_prior.get(target_type,fallback))
    diag['daytype_supported']=len(matches)>=2
    diag['last_gap_days']=diag.pop('gap_days')
    return base,diag


def replay(frame,train,prior):
    train=train.copy()
    train['daytype']=[int((local_time(str(t))+timedelta(days=1)).weekday()>=5) for t in train.cutoff]
    calendar={int(k):g.groupby('subject_id')[['bed_offset','wake_offset']].mean().mean().to_numpy()
              for k,g in train.groupby('daytype')}
    arrays={k:np.empty((len(frame),2)) for k in ('population','last','mean7','mean14','weighted28','weekday',*NEW_NAMES)}
    diagnostics=[None]*len(frame)
    for _,g in frame.assign(_position=np.arange(len(frame))).groupby('subject_id'):
        g=g.sort_values('cutoff'); history=[]; enrolled=str(g.iloc[0].cutoff)
        for _,r in g.iterrows():
            p,d=extended_baselines(history,str(r.cutoff),prior,enrolled,calendar)
            i=int(r['_position']); diagnostics[i]=d
            for name in arrays: arrays[name][i]=p[name]
            history.append({'source':'research','bed':str(r.bed),'wake':str(r.wake),'observed_at':str(r.observed_at)})
    return arrays,pd.DataFrame(diagnostics)


def block_skill_ci(actual_error,reference_error,dates,draws=1000):
    """Moving observed-day blocks, max3 days; stop blocks at gaps >36 h."""
    a=np.asarray(actual_error,float); b=np.asarray(reference_error,float)
    if len(a)<4 or b.mean()<1: return None
    rng=np.random.default_rng(SEED); n=len(a); blocks=[]
    dates=pd.DatetimeIndex(pd.to_datetime(dates))
    for start in range(n):
        block=[start]
        for j in range(start+1,min(n,start+3)):
            if (dates[j]-dates[j-1]).total_seconds()>36*3600: break
            block.append(j)
        blocks.append(block)
    sims=[]
    for _ in range(draws):
        sampled=[]
        while len(sampled)<n: sampled.extend(blocks[rng.integers(n)])
        ix=np.asarray(sampled[:n]); denominator=b[ix].mean()
        if denominator>=1: sims.append(1-a[ix].mean()/denominator)
    return np.quantile(sims,[.025,.975]).tolist() if len(sims)>=draws*.8 else None


def profile_windows(frame,predictions,reference):
    actual=frame[['bed_offset','wake_offset']].to_numpy(float)
    base=np.abs(actual-reference).mean(axis=1); records=[]
    for subject,g in frame.assign(_position=np.arange(len(frame))).groupby('subject_id'):
        g=g.sort_values('cutoff'); dates=pd.to_datetime(g.cutoff); start=dates.min()
        group_ids=((dates-start).dt.total_seconds()/86400//14).astype(int)
        for window,part in g.groupby(group_ids):
            ix=part['_position'].to_numpy(int); b=float(base[ix].mean()); supported=len(ix)>=4 and b>=1
            for model in ('physical','data','hybrid'):
                e=np.abs(actual[ix]-predictions[model][ix]).mean(axis=1)
                ci=block_skill_ci(e,base[ix],part.cutoff) if supported else None
                records.append({'subject_id':subject,'window':int(window),'model':model,'n_nights':len(ix),
                    'baseline_mae_min':b,'model_mae_min':float(e.mean()),
                    'skill':float(1-e.mean()/b) if supported else None,
                    'skill_ci_low':ci[0] if ci else None,'skill_ci_high':ci[1] if ci else None,
                    'status':'exploratory_short_series' if supported else 'insufficient_nights_or_small_denominator'})
    private=pd.DataFrame(records); summary=[]
    for model,g in private.groupby('model'):
        valid=g.loc[g.skill.notna()]
        summary.append({'model':model,'n_windows':len(g),'eligible_windows':len(valid),
            'n_people':int(valid.subject_id.nunique()),'median_individual_window_skill':float(valid.skill.median()) if len(valid) else None,
            'q25_q75_skill':valid.skill.quantile([.25,.75]).tolist() if len(valid) else None,
            'positive_window_fraction':float(valid.skill.gt(0).mean()) if len(valid) else None})
    return private,summary


def feasibility(frame):
    """Pre-error gates: counts and dates only, no loss/score selection."""
    rows=[]
    for person,g in frame.groupby('subject_id'):
        dates=pd.to_datetime(g.cutoff).sort_values(); n=len(g)
        gaps=dates.diff().dt.total_seconds().dropna()/86400
        rows.append({'subject_id':person,'n_nights':n,'span_days':int((dates.max()-dates.min()).days+1),
            'max_gap_days':float(gaps.max()) if len(gaps) else 0,
            'individual_profile_possible':n>=4,'paired_half_possible':n>=8,
            'longitudinal_28_day_possible':n>=16 and (dates.max()-dates.min()).days>=27 and (not len(gaps) or gaps.max()<=7)})
    p=pd.DataFrame(rows)
    result={'n_people':len(p),'profile_min4_people':int(p.individual_profile_possible.sum()),
        'split_half_min8_people':int(p.paired_half_possible.sum()),
        'longitudinal_min28days_min16nights_gap7_people':int(p.longitudinal_28_day_possible.sum()),
        'longitudinal_gate_min30_people_passed':bool(p.longitudinal_28_day_possible.sum()>=30),
        'interpretation':'Reporting/research feasibility only; never blocks daily behavioral updating and is not proof of efficacy.'}
    return p,result


def split_half(frame,predictions,reference):
    actual=frame[['bed_offset','wake_offset']].to_numpy(float); entries=[]
    for subject,g in frame.assign(_position=np.arange(len(frame))).groupby('subject_id'):
        g=g.sort_values('cutoff')
        if len(g)<8: continue
        ix=g['_position'].to_numpy(int); half=len(ix)//2
        for arm in ('physical','data','hybrid'):
            values=[]
            for part in (ix[:half],ix[half:]):
                b=np.abs(actual[part]-reference[part]).mean(); e=np.abs(actual[part]-predictions[arm][part]).mean()
                values.append((float(e),float(1-e/b) if b>=1 else None))
            entries.append({'subject_id':subject,'model':arm,'early_mae':values[0][0], 'late_mae':values[1][0],
                            'early_skill':values[0][1],'late_skill':values[1][1]})
    private=pd.DataFrame(entries,columns=['subject_id','model','early_mae','late_mae','early_skill','late_skill']); summary=[]
    for model,g in private.groupby('model'):
        v=g.dropna(); corr=spearmanr(v.early_skill,v.late_skill).statistic if len(v)>=5 and v.early_skill.nunique()>1 and v.late_skill.nunique()>1 else None
        delta=g.early_mae-g.late_mae; rng=np.random.default_rng(SEED)
        boot=delta.to_numpy()[rng.integers(len(g),size=(5000,len(g)))].mean(axis=1)
        summary.append({'model':model,'n_people':len(g),'early_mae_min':float(g.early_mae.mean()),
            'late_mae_min':float(g.late_mae.mean()),'early_minus_late_mae_min':float(delta.mean()),
            'ci95_min':np.quantile(boot,[.025,.975]).tolist(),
            'skill_spearman':float(corr) if corr is not None and np.isfinite(corr) else None,
            'interpretation':'Same people, different periods; time/adaptation/measurement confounded, not causal learning gain.'})
    return private,summary


def cohort_audit(project,out,cohort):
    data_root=project/'데이터모델/연구용데이터'; run=data_root/'continuous_constants_run_20261025_v2'/cohort
    source=data_root/'next_night_run_20261020_v1/analysis'/cohort/'episodes.parquet'
    selection=json.loads((run/'train_selection.json').read_text('utf-8'))
    if sha(source)!=selection['source_sha256']: raise ValueError('Source changed')
    full=pd.read_parquet(source); frame=full.loc[full.split.eq('test')].reset_index(drop=True)
    pd.testing.assert_frame_equal(frame,pd.read_parquet(run/'test_rows_private.parquet'))
    if full.groupby('subject_id').split.nunique().max()!=1: raise ValueError('Participant overlap')
    train=full.loc[full.split.eq('train')]
    people,gate=feasibility(frame); dump(out/f'{cohort}_feasibility_pre_error.json',gate)
    people.to_csv(out/f'{cohort}_feasibility_private.csv',index=False)
    predictions,diag=replay(frame,train,selection['prior'])
    archived=np.load(run/'test_predictions_private.npz')
    chosen=selection['selected_daily_policy']
    for arm in ('physical','data','hybrid'):
        predictions[arm]=archived[chosen+'__'+arm]
        predictions['legacy_'+arm]=archived['legacy14__'+arm]
    # Bind to the already independently replay-verified keyed v1 results.
    old=pd.read_parquet(data_root/'target_validity_run_20261025_v1'/f'{cohort}_predictions_private.parquet')
    if not np.array_equal(old.row_id,frame.row_id): raise ValueError('Keyed prediction order changed')
    for name in ('physical','data','hybrid'):
        np.testing.assert_allclose(predictions[name],old[[name+'_onset',name+'_wake']],atol=1e-8,rtol=0)
    actual=frame[['bed_offset','wake_offset']].to_numpy(float); reference=predictions['mean14']
    scores=[]
    for name,p in predictions.items():
        score,_=paired_summary(actual,p,reference,frame.subject_id)
        scores.append({'name':name,**score})
    windows,window_summary=profile_windows(frame,predictions,reference)
    windows.to_csv(out/f'{cohort}_period_skills_private.csv',index=False)
    halves,half_summary=split_half(frame,predictions,reference)
    halves.to_csv(out/f'{cohort}_split_half_private.csv',index=False)
    repeat=np.zeros(len(frame),bool)
    for _,g in frame.assign(_position=np.arange(len(frame))).groupby('subject_id'):
        g=g.sort_values('cutoff'); a=g[['bed_offset','wake_offset']].to_numpy(float)
        r=np.r_[False,np.all(np.abs(a[1:]-a[:-1])<=.5,axis=1)]
        repeat[g['_position'].to_numpy(int)]=r
    sensitivity=[]
    for label,mask in [('all',np.ones(len(frame),bool)),('exclude_exact_repeated_pair',~repeat),
                       ('at_least_4_prior',diag.prior_records.ge(4).to_numpy()),
                       ('gap_over_2_days',diag.last_gap_days.gt(2).to_numpy())]:
        if not mask.any():
            sensitivity.append({'stratum':label,'n_people':0,'n_nights':0,'scores':[]});continue
        rows=[]
        for arm in ('physical','data','hybrid'):
            score,_=paired_summary(actual[mask],predictions[arm][mask],reference[mask],frame.loc[mask,'subject_id'])
            rows.append({'name':arm,**score})
        sensitivity.append({'stratum':label,'n_people':int(frame.loc[mask,'subject_id'].nunique()),'n_nights':int(mask.sum()),'scores':rows})
    variance=frame.groupby('subject_id')[['bed_offset','wake_offset']].std().mean(axis=1)
    _,train_macro=metrics(train[['bed_offset','wake_offset']].to_numpy(float),np.tile([selection['prior']['bed_offset'],selection['prior']['wake_offset']],(len(train),1)),train.subject_id)
    train_sd=float(train[['bed_offset','wake_offset']].std().mean()); confounds=[]
    for arm in ('physical','data','hybrid'):
        e=metrics(actual,predictions[arm],frame.subject_id)[0].joint
        b=metrics(actual,reference,frame.subject_id)[0].joint
        skill=1-e/b.where(b>=1)
        def rho(series):
            aligned=pd.concat([variance,series],axis=1).dropna()
            if len(aligned)<5 or aligned.iloc[:,1].nunique()<2:return None
            value=spearmanr(aligned.iloc[:,0],aligned.iloc[:,1]).statistic
            return float(value) if np.isfinite(value) else None
        confounds.append({'model':arm,'variability_vs_mae_spearman':rho(e),
            'variability_vs_naive_global_scale_score_spearman':rho(1-e/train_sd),
            'variability_vs_baseline_skill_spearman':rho(skill)})
    private=frame[['row_id','subject_id','cutoff','bed_offset','wake_offset']].join(diag)
    for name,p in predictions.items():
        private[name+'_onset']=p[:,0];private[name+'_wake']=p[:,1]
    private.to_parquet(out/f'{cohort}_predictions_private.parquet',index=False)
    return {'n_people':int(frame.subject_id.nunique()),'n_nights':len(frame),'scores':scores,
        'feasibility':gate,'period_profiles':window_summary,'split_half':half_summary,
        'within_person':{a:within_person_diagnostic(frame,predictions[a]) for a in ('physical','data','hybrid')},
        'repeat_sensitivity':sensitivity,'variability_audit':confounds,
        'diagnostics':{'repeat_pair_nights':int(repeat.sum()),'daytype_supported_nights':int(diag.daytype_supported.sum()),
            'circular7_projection_nights':int(diag.circular7_projected.sum()),
            'schedule_baseline_status':'unavailable_no_prospectively_recorded_schedules',
            'source_sha256':sha(source),'frozen_npz_sha256':sha(run/'test_predictions_private.npz')}}


def figures(artifact,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size':10,'pdf.fonttype':42,'svg.fonttype':'none'})
    fig,axes=plt.subplots(1,2,figsize=(12,5),layout='constrained')
    names=['mean7','mean14','mean28','circular7','circular14','weighted14','weighted28','daytype28','calendar_train','physical','data','hybrid']
    for ax,(cohort,item) in zip(axes,artifact['cohorts'].items()):
        rows={s['name']:s for s in item['scores']}; vals=[rows[n]['joint_mae_min'] for n in names]
        ax.barh(names,vals,color=['#91A3B0']*9+['#DAAC56','#4E82A7','#5F977B']);ax.invert_yaxis()
        ax.set_title(cohort.upper());ax.set_xlabel('Participant-macro joint MAE (min)');ax.set_xlim(0,max(vals)*1.18)
        for j,v in enumerate(vals):ax.text(v+.5,j,f'{v:.2f}',va='center',fontsize=8)
    for ext in ('png','pdf','svg'):fig.savefig(out/f'Figure_1_full_baselines.{ext}',dpi=400)
    plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(11,4),layout='constrained')
    for ax,(cohort,item) in zip(axes,artifact['cohorts'].items()):
        rows=item['period_profiles']; vals=[r['median_individual_window_skill'] for r in rows]
        lows=[r['q25_q75_skill'][0] for r in rows];highs=[r['q25_q75_skill'][1] for r in rows]
        ax.errorbar(vals,np.arange(len(rows)),xerr=[np.array(vals)-lows,np.array(highs)-vals],fmt='o',capsize=5)
        ax.set_yticks(np.arange(len(rows)),[r['model'] for r in rows]);ax.axvline(0,color='grey',ls='--')
        ax.set_title(cohort.upper());ax.set_xlabel('Median individual 14-day-window skill (IQR)')
    for ext in ('png','pdf','svg'):fig.savefig(out/f'Figure_2_individual_profiles.{ext}',dpi=400)
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    if (a.output/'full_protocol.json').exists():raise FileExistsError('Use new output; no retuning')
    dump(a.output/'full_protocol.json',{'version':'2026-10-25-full-v1','seed':SEED,'primary_comparator':'mean14',
        'new_baselines':list(NEW_NAMES),'circular_resultant_min':.1,'circular_projection':'same target window as models',
        'individual_windows':'nonoverlapping14-calendar-day since first cutoff; >=4 nights and baselineMAE>=1min',
        'individual_ci':'1000 moving observed-day block resamples, max3 adjacent days, gap>36h breaks blocks; exploratory short series',
        'split_half':'same participants >=8 nights, ordered halves; descriptive, no causal learning claim',
        'feasibility':'counts before errors;>=16 nights over>=28days,maxgap<=7;>=30people to support longterm followup',
        'repeat_rule':'onset and wake offsets both within0.5minute of prior observation; sensitivity only, not duplicate proof',
        'test_previously_exposed':True,'no_model_fit':True,'no_service_promotion':True,'code_sha256':sha(Path(__file__))})
    artifact={'schema_version':2,'artifact_type':'sleepwell_full_target_validity','version':'2026-10-25-full-v1',
        'test_status':'retrospective_reused_test','no_service_promotion':True,
        'cohorts':{c:cohort_audit(a.project,a.output,c) for c in ('cmi','mesa')},
        'limitations':['Individual window skill is not physiological predictability.',
            'Intervals and profiles are exploratory; no multiplicity-adjusted discovery claims.',
            'Schedule-only comparator cannot be scored on CMI/MESA lacking issued-time schedules.',
            'Prior outcome availability equals wake time in offline data.',
            'MESA dates are shifted; calendar-season drift is not identifiable.',
            'Additional horizons/endpoints need separate training and purged outcome windows; not inferred from next-night accuracy.']}
    dump(a.output/'full_target_validity_v2.json',artifact);figures(artifact,a.output)
    print(json.dumps({c:{'people':v['n_people'],'gate':v['feasibility'],'baselines':{r['name']:round(r['joint_mae_min'],3) for r in v['scores']}} for c,v in artifact['cohorts'].items()},indent=2))


if __name__=='__main__':main()
