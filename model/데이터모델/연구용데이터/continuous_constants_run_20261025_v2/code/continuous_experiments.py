"""Frozen, matched HGB evaluation of continuous behavior-derived constants."""
from __future__ import annotations
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
import argparse,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'base'))
import matched_experiments as m
import numpy as np
import pandas as pd
import joblib
from sklearn.model_selection import GroupKFold
from threadpoolctl import threadpool_limits
from night_core import local_time
from daily_features import extended_features,POLICY_SPECS

ARMS=('data','hybrid')
COHORTS=('cmi','mesa')

def verify_sources(run):
    manifest=json.loads((run/'source_manifest.json').read_text('utf8'))
    if m.sha(run/'protocol.json')!=manifest['protocol_sha256']:raise ValueError('Protocol changed')
    for name,digest in manifest['sources'].items():
        if m.sha(run/name)!=digest:raise ValueError('Frozen source changed: '+name)

def replay(frame,prior,policy):
    values=[];states=[]
    for _,group in frame.groupby(['subject_id','split'],sort=True):
        group=group.sort_values('cutoff');records=[];enrolled=str(group.cutoff.iloc[0])
        for row in group.itertuples():
            x,s=extended_features(records,local_time(str(row.cutoff)),prior,enrolled,policy)
            values.append({'row_id':row.row_id,**x});states.append({'row_id':row.row_id,**s})
            records.append({'source':'research','bed':str(row.bed),'wake':str(row.wake),'observed_at':str(row.observed_at)})
    def align(rows):return pd.DataFrame(rows).set_index('row_id').loc[frame.row_id].reset_index()
    return align(values),align(states)

def diagnostics(states):
    active=states.personal_weight.to_numpy(float)>0
    rows={'n_nights':len(states),'active_nights':int(active.sum()),
          'max_valid_days':int(states.valid_days.max()),'mean_weight':float(states.personal_weight.mean()),
          'physical_fallback_nights':int(states.physical_fallback.sum()),'by_history':[]}
    for n in sorted(states.valid_days.unique()):
        g=states.loc[states.valid_days.eq(n)]
        rows['by_history'].append({'prior_records':int(n),'n_nights':len(g),'active_nights':int((g.personal_weight>0).sum()),
                                  'mean_weight':float(g.personal_weight.mean()),'min_weight':float(g.personal_weight.min()),
                                  'max_weight':float(g.personal_weight.max())})
    return rows

def train(project,run,cohort,protocol):
    out=run/cohort;out.mkdir(exist_ok=False)
    old_selection_path=project/'데이터모델/연구용데이터/matched_physics_run_20261025_v1'/cohort/'train_selection.json'
    old_selection=json.loads(old_selection_path.read_text('utf8'))
    old_protocol_path=old_selection_path.parents[1]/'protocol.json'
    old_protocol=json.loads(old_protocol_path.read_text('utf8'))
    old_pair=next(q for q in old_selection['pairs'] if q['pair_id']==old_selection['primary_pair'])
    fixed=protocol['learners'][cohort]
    if old_pair['family']!='hgb' or any(old_pair[k]!=fixed[k] for k in ('representation','candidate')):
        raise ValueError('Fixed learner differs from v1 TRAIN selection')
    if protocol['candidate']!=old_protocol['candidates'][fixed['candidate']]:
        raise ValueError('Fixed candidate parameters differ from v1')
    allrows,source=m.load_frame(project,cohort)
    train=allrows.loc[allrows.split.eq('train')].reset_index(drop=True)
    val=allrows.loc[allrows.split.eq('validation')].reset_index(drop=True)
    rep=protocol['learners'][cohort]['representation'];candidate=protocol['candidate'];results=[];provenance=[]
    for fold,(ii,jj) in enumerate(GroupKFold(3).split(train,groups=train.subject_id)):
        fit=train.iloc[ii].copy().reset_index(drop=True);held=train.iloc[jj].copy().reset_index(drop=True)
        fit['split']='train';held['split']='validation';prior=m.prior_from_train(fit)
        if set(fit.subject_id)&set(held.subject_id):raise ValueError('Participant overlap')
        provenance.append({'fold':fold,'prior':prior,'fit_people':fit.subject_id.nunique(),'held_people':held.subject_id.nunique()})
        for policy in protocol['policies']:
            xf,sf=replay(fit,prior,policy);xv,sv=replay(held,prior,policy)
            for arm in ARMS:
                zf=m.encode(fit,xf,sf,rep,arm);zv=m.encode(held,xv,sv,rep,arm)
                model,info=m.fit_one(candidate,fit,zf,m.reference(xf,prior,rep),protocol['seed'])
                p=m.predict([model],zv,m.reference(xv,prior,rep))
                results.append({'fold':fold,'policy':policy,'arm':arm,**m.metrics(held,p),**info})
            m.log(f'{cohort} fold{fold+1}: {policy}')
        m.dump(out/'cv_progress.json',results)
    cv=pd.DataFrame(results)
    means=cv.groupby('policy').joint_mae_min.mean()
    chosen=str(means.drop('legacy14').sort_values(kind='stable').index[0]);prior=m.prior_from_train(train)
    selection={'cohort':cohort,'selected_daily_policy':chosen,'representation':rep,'candidate':candidate,
               'prior':prior,'policy_cv_mean':means.to_dict(),'source_sha256':m.sha(source),
               'v1_train_selection_sha256':m.sha(old_selection_path),
               'selection_scope':'original TRAIN grouped CV only','policy_specs':POLICY_SPECS}
    m.dump(out/'train_selection.json',selection);m.dump(out/'cv_details.json',results);m.dump(out/'fold_provenance.json',provenance)
    models=out/'models';models.mkdir();preds={};states_by_policy={};fitinfo=[]
    val.to_parquet(out/'validation_rows_private.parquet',index=False)
    for policy in protocol['policies']:
        xf,sf=replay(train,prior,policy);xv,sv=replay(val,prior,policy)
        states_by_policy[policy]=diagnostics(sv)
        sv.to_parquet(out/(policy+'_validation_states_private.parquet'),index=False)
        for arm in ARMS:
            zf=m.encode(train,xf,sf,rep,arm);zv=m.encode(val,xv,sv,rep,arm)
            model,info=m.fit_one(candidate,train,zf,m.reference(xf,prior,rep),protocol['seed'])
            key=policy+'__'+arm
            artifact={'model':model,'policy':policy,'arm':arm,'representation':rep,'prior':prior,'feature_order':list(zf.columns)}
            joblib.dump(artifact,models/(key+'.joblib'))
            preds[key]=m.predict([model],zv,m.reference(xv,prior,rep))
            fitinfo.append({'policy':policy,'arm':arm,**info})
        preds[policy+'__physical']=m.project(xv[['physical_bed','physical_wake']].to_numpy(float))
        m.log(f'{cohort} full TRAIN: {policy}')
    np.savez_compressed(out/'validation_predictions_private.npz',**preds)
    old_val_rows=old_selection_path.parent/'validation_rows_private.parquet'
    old_val_preds=old_selection_path.parent/'validation_predictions_private.npz'
    previous_rows=pd.read_parquet(old_val_rows)
    if (not np.array_equal(val.row_id.to_numpy(),previous_rows.row_id.to_numpy()) or
            not np.array_equal(val.subject_id.to_numpy(),previous_rows.subject_id.to_numpy()) or
            not np.array_equal(val[m.TARGETS].to_numpy(),previous_rows[m.TARGETS].to_numpy())):
        raise ValueError('Legacy validation rows differ')
    previous_preds=np.load(old_val_preds)
    maxdiff={a:float(np.abs(preds['legacy14__'+a]-previous_preds[old_selection['primary_pair']+'__'+a]).max()) for a in ARMS}
    if any(v>1e-8 for v in maxdiff.values()):raise ValueError('Legacy validation predictions do not reproduce v1')
    m.dump(out/'legacy_validation_reproduction.json',{'maximum_absolute_difference_min':maxdiff,
           'external_dependencies':{str(f):m.sha(f) for f in (old_selection_path,old_protocol_path,old_val_rows,old_val_preds)}})
    m.dump(out/'validation_activation.json',states_by_policy);m.dump(out/'fit_details.json',fitinfo)
    m.dump(out/'training_done.json',{'selected_daily_policy':chosen,'test_scored':False})

def calibrate(run,cohort,protocol):
    out=run/cohort
    if (out/'calibration_lock.json').exists() or any(run.glob('*/test_started.json')):raise ValueError('Cannot recalibrate')
    s=json.loads((out/'train_selection.json').read_text('utf8'));policy=s['selected_daily_policy']
    parity=json.loads((out/'legacy_validation_reproduction.json').read_text('utf8'))
    if any(v>1e-8 for v in parity['maximum_absolute_difference_min'].values()):raise ValueError('Legacy parity failed')
    for path,digest in parity['external_dependencies'].items():
        if m.sha(Path(path))!=digest:raise ValueError('Legacy dependency changed')
    frame=pd.read_parquet(out/'validation_rows_private.parquet');p=np.load(out/'validation_predictions_private.npz')
    delta=sum((m.participant_errors(frame,p['legacy14__'+a])-m.participant_errors(frame,p[policy+'__'+a])) for a in ARMS)/2
    rng=np.random.default_rng(protocol['seed']);d=delta.to_numpy();boot=d[rng.integers(0,len(d),(10000,len(d)))].mean(axis=1)
    ci=np.quantile(boot,[.025,.975]).tolist();gain=float(d.mean())
    lock={'cohort':cohort,'selected_daily_policy':policy,'validation_joint_gain_min':gain,'validation_ci95':ci,
          'forecast_promotion_gate':bool(gain>=.5 and ci[0]>0),'research_only':True,
          'protocol_sha256':m.sha(run/'protocol.json'),'selection_sha256':m.sha(out/'train_selection.json'),
          'legacy_parity_sha256':m.sha(out/'legacy_validation_reproduction.json'),
          'external_dependencies':parity['external_dependencies'],
          'model_hashes':{f.name:m.sha(f) for f in sorted((out/'models').glob('*.joblib'))}}
    m.dump(out/'calibration_lock.json',lock);m.log(f'{cohort} locked daily={policy}; validation gain={gain:.4f}')

def verify_locks(run):
    verify_sources(run)
    for cohort in COHORTS:
        out=run/cohort;lock=json.loads((out/'calibration_lock.json').read_text('utf8'))
        if m.sha(run/'protocol.json')!=lock['protocol_sha256'] or m.sha(out/'train_selection.json')!=lock['selection_sha256']:
            raise ValueError('Changed selection/protocol')
        if m.sha(out/'legacy_validation_reproduction.json')!=lock['legacy_parity_sha256']:raise ValueError('Legacy parity record changed')
        for path,digest in lock['external_dependencies'].items():
            if m.sha(Path(path))!=digest:raise ValueError('Legacy dependency changed')
        for name,digest in lock['model_hashes'].items():
            if m.sha(out/'models'/name)!=digest:raise ValueError('Changed model')

def evaluate(project,run,cohort,protocol):
    verify_locks(run);out=run/cohort
    with (out/'test_started.json').open('x',encoding='utf8') as f:json.dump({'cohort':cohort},f)
    s=json.loads((out/'train_selection.json').read_text('utf8'));lock=json.loads((out/'calibration_lock.json').read_text('utf8'))
    old_selection_path=project/'데이터모델/연구용데이터/matched_physics_run_20261025_v1'/cohort/'train_selection.json'
    if m.sha(old_selection_path)!=s['v1_train_selection_sha256']:raise ValueError('v1 TRAIN selection changed')
    frame,source=m.load_frame(project,cohort)
    if m.sha(source)!=s['source_sha256']:raise ValueError('Source changed')
    test=frame.loc[frame.split.eq('test')].reset_index(drop=True);preds={};states={};scores=[]
    for policy in protocol['policies']:
        x,st=replay(test,s['prior'],policy);states[policy]=diagnostics(st)
        st.to_parquet(out/(policy+'_test_states_private.parquet'),index=False)
        for arm in ARMS:
            key=policy+'__'+arm;artifact=joblib.load(out/'models'/(key+'.joblib'))
            z=m.encode(test,x,st,s['representation'],arm)
            if list(z.columns)!=artifact['feature_order']:raise ValueError('Feature schema mismatch')
            preds[key]=m.predict([artifact['model']],z,m.reference(x,s['prior'],s['representation']))
        preds[policy+'__physical']=m.project(x[['physical_bed','physical_wake']].to_numpy(float))
        for arm in (*ARMS,'physical'):scores.append({'policy':policy,'arm':arm,**m.metrics(test,preds[policy+'__'+arm])})
    comparisons=[]
    for policy in protocol['policies'][1:]:
        for arm in ARMS:
            comparisons.append({'policy':policy,'arm':arm,'primary':policy==s['selected_daily_policy'],
                                **m.paired(test,preds['legacy14__'+arm],preds[policy+'__'+arm])})
    order=np.argsort([v['sign_flip_p'] for v in comparisons]);running=0
    for rank,ix in enumerate(order):
        running=max(running,min(1,(len(order)-rank)*comparisons[ix]['sign_flip_p']))
        comparisons[ix]['holm_p']=running
    previous=project/'데이터모델/연구용데이터/matched_physics_run_20261025_v1'/cohort
    previous_preds=np.load(previous/'test_predictions_private.npz')
    maxdiff={a:float(np.abs(preds['legacy14__'+a]-previous_preds[a]).max()) for a in ARMS}
    if any(v>1e-8 for v in maxdiff.values()):raise ValueError('Legacy comparator did not reproduce v1')
    chosen=s['selected_daily_policy'];physical=[{'policy':p,**m.paired(test,preds['legacy14__physical'],preds[p+'__physical'])} for p in protocol['policies'][1:]]
    np.savez_compressed(out/'test_predictions_private.npz',**preds);test.to_parquet(out/'test_rows_private.parquet',index=False)
    pd.DataFrame(scores).to_csv(out/'test_scores.csv',index=False)
    m.dump(out/'test_results.json',{'cohort':cohort,'selected_daily_policy':chosen,'scores':scores,'comparisons':comparisons,
           'physical_comparisons':physical,'selected_daily_physics_addition':m.paired(test,preds[chosen+'__data'],preds[chosen+'__hybrid']),
           'activation':states,'legacy_reproduction_maxabs_min':maxdiff,'validation_lock':lock,
           'test_previously_exposed':True,'no_service_promotion':True})
    m.log(f'{cohort} continuous Test finished; legacy maxdiff={maxdiff}')

def main():
    p=argparse.ArgumentParser();p.add_argument('--project-root',type=Path,required=True);p.add_argument('--run',type=Path,required=True)
    p.add_argument('--cohort',choices=COHORTS,required=True);p.add_argument('--phase',choices=('train','calibrate','evaluate'),required=True);a=p.parse_args()
    protocol=json.loads((a.run/'protocol.json').read_text('utf8'));verify_sources(a.run)
    with threadpool_limits(limits=1):
        if a.phase=='train':train(a.project_root,a.run,a.cohort,protocol)
        elif a.phase=='calibrate':calibrate(a.run,a.cohort,protocol)
        else:evaluate(a.project_root,a.run,a.cohort,protocol)
if __name__=='__main__':main()
