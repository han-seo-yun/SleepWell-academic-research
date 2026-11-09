"""Matched-learner physics ablation with fold-local causal replay and a Test gate."""
from __future__ import annotations
import os
for _name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[_name]='1'
import argparse, hashlib, json, platform, sys, time, warnings
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.neural_network import MLPRegressor
from sklearn.model_selection import GroupKFold
from sklearn.multioutput import MultiOutputRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.feature_selection import VarianceThreshold
from threadpoolctl import threadpool_limits

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'compat'))
from next_night_experiments import prior_from_train, score, project, TARGETS
from robust_night_experiments import replay, balanced_weights, target_flagger
from night_features_v2 import DATA_V2
from night_core import PHYSICAL_FEATURES
from night_estimators import TrainQuantileClip

SEED=20261025
REPS=('raw_absolute','clock_quality_absolute','clock_quality_residual')
FAMILIES=('ridge','hgb','extra_trees','mlp')
ARMS=('data','hybrid')
TIME_NAMES=['population_bed','population_wake','habit_bed','habit_wake','recent_bed','recent_wake',
            'previous_bed','previous_wake','median14_bed','median14_wake','ew_bed','ew_wake','weekday_bed','weekday_wake']

def dump(path,obj):
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def log(msg): print(msg,flush=True)

def verify_run_sources(run):
    manifest=json.loads((run/'source_manifest.json').read_text('utf8'))
    if manifest['protocol_sha256']!=sha(run/'protocol.json'):
        raise ValueError('Protocol changed after freeze')
    for relative,expected in manifest['sources'].items():
        if sha(run/relative)!=expected:raise ValueError('Source changed after freeze: '+relative)

def verify_test_locks(run):
    verify_run_sources(run)
    for cohort in ('cmi','mesa'):
        out=run/cohort;path=out/'calibration_lock.json'
        if not path.exists():raise ValueError('Both cohort calibration locks are required')
        lock=json.loads(path.read_text('utf8'))
        for key,target in [('protocol_sha256',run/'protocol.json'),
                           ('train_selection_sha256',out/'train_selection.json'),
                           ('personalization_source_sha256',HERE/'personalization_ablation.py'),
                           ('experiment_source_sha256',Path(__file__))]:
            if lock[key]!=sha(target):raise ValueError('Frozen dependency changed: '+str(target))
        for name,expected in lock['model_hashes'].items():
            if sha(out/'models'/name)!=expected:raise ValueError('Frozen model changed: '+name)

def encode(frame,x,states,representation,arm):
    """Stateless descriptors; all learned preprocessing remains in Pipeline."""
    z=x[list(DATA_V2)].astype(float).copy().reset_index(drop=True)
    x=x.reset_index(drop=True); states=states.reset_index(drop=True); frame=frame.reset_index(drop=True)
    if arm=='hybrid':
        for name in PHYSICAL_FEATURES: z[name]=x[name].astype(float)
    if representation!='raw_absolute':
        times=TIME_NAMES+(['physical_bed','physical_wake'] if arm=='hybrid' else [])
        for name in times:
            z[name+'_sin']=np.sin(2*np.pi*x[name]/1440)
            z[name+'_cos']=np.cos(2*np.pi*x[name]/1440)
        for key in ('bed','wake'):
            for name in ('previous','ew','weekday'):
                z[f'{name}_minus_median_{key}']=x[f'{name}_{key}']-x[f'median14_{key}']
        z['valid_days']=states.valid_days.astype(float)
        z['elapsed_days']=states.elapsed_days.astype(float)
        for name in ('valid_days','elapsed_days','history_age_days','gap_days_mean','recent_count14'):
            z[name+'_log1p']=np.log1p(z[name].clip(lower=0))
        z['no_recent_history']=(x.recent_count14==0).astype(float)
        z['age']=pd.to_numeric(frame.age,errors='coerce')
        z['age_missing']=z.age.isna().astype(float)
        sex=frame.sex.fillna('missing').astype(str)
        z['sex_female']=(sex=='female').astype(float)
        z['sex_male']=(sex=='male').astype(float)
        z['sex_missing']=(~sex.isin(['female','male'])).astype(float)
        if arm=='hybrid':
            z['physical_fallback']=states.physical_fallback.astype(float)
            z['sleep_margin']=x.current_s+x.current_c-x.sleep_threshold
            z['threshold_hysteresis']=x.sleep_threshold-x.wake_threshold
            z['physical_minus_weekday_bed']=x.physical_bed-x.weekday_bed
            z['physical_minus_weekday_wake']=x.physical_wake-x.weekday_wake
    return z

def reference(x,prior,representation):
    if representation=='clock_quality_residual':
        return x[['weekday_bed','weekday_wake']].to_numpy(float)
    return np.tile([prior['bed_offset'],prior['wake_offset']],(len(x),1))

def make_model(c,seed):
    family=c['family']
    if family=='ridge': model=Ridge(alpha=c['alpha'])
    elif family=='hgb':
        model=MultiOutputRegressor(HistGradientBoostingRegressor(loss='absolute_error',
            max_leaf_nodes=c['leaves'],min_samples_leaf=c['min_leaf'],max_iter=c['iterations'],
            learning_rate=.05,l2_regularization=10,early_stopping=False,random_state=seed))
    elif family=='extra_trees':
        model=ExtraTreesRegressor(n_estimators=c['trees'],min_samples_leaf=c['min_leaf'],
            max_features=1.0,random_state=seed,n_jobs=1)
    else:
        model=MLPRegressor(hidden_layer_sizes=tuple(c['hidden']),activation='relu',solver='adam',
            alpha=c['alpha'],batch_size=128,learning_rate_init=.003,max_iter=c['epochs'],
            early_stopping=False,n_iter_no_change=c['epochs']+1,tol=0.,random_state=seed)
    return Pipeline([('impute',SimpleImputer(strategy='median',add_indicator=True,keep_empty_features=True)),
        ('clip',TrainQuantileClip()),('variance',VarianceThreshold()),('scale',StandardScaler()),('model',model)])

def fit_one(c,frame,x,ref,seed):
    model=make_model(c,seed)
    begin=time.perf_counter()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        model.fit(x.to_numpy(float),(frame[TARGETS].to_numpy(float)-ref)/120,
                  model__sample_weight=balanced_weights(frame))
    warnings_list=sorted(set(str(w.message) for w in caught))
    est=model.named_steps['model']
    info={'seconds':time.perf_counter()-begin,'warnings':warnings_list,'features_input':x.shape[1],
          'features_used':int(model.named_steps['variance'].get_support().sum())}
    if c['family']=='mlp':
        info.update(iterations=int(est.n_iter_),loss=float(est.loss_),
                    parameter_count=int(sum(a.size for a in est.coefs_+est.intercepts_)))
    return model,info

def predict(models,x,ref):
    raw=np.mean([m.predict(x.to_numpy(float))*120+ref for m in models],axis=0)
    if not np.isfinite(raw).all(): raise ValueError('Nonfinite predictions')
    return project(raw)

def metrics(frame,pred): return score(frame[TARGETS].to_numpy(float),pred,frame.subject_id,True)
def participant_errors(frame,pred):
    e=np.abs(frame[TARGETS].to_numpy(float)-pred).mean(axis=1)
    return pd.Series(e).groupby(frame.subject_id.reset_index(drop=True)).mean()

def paired(frame,baseline,changed,replicates=10000):
    a=participant_errors(frame,baseline); b=participant_errors(frame,changed)
    delta=(a-b).to_numpy(); rng=np.random.default_rng(SEED)
    boot=delta[rng.integers(0,len(delta),(replicates,len(delta)))].mean(axis=1)
    lo,hi=np.quantile(boot,[.025,.975])
    signs=rng.choice([-1.,1.],size=(replicates,len(delta)))
    p=(1+np.sum(np.abs((signs*delta).mean(axis=1))>=abs(delta.mean())-1e-12))/(replicates+1)
    return {'improvement_min':float(delta.mean()),'ci95_min':[float(lo),float(hi)],
            'sign_flip_p':float(p),'n_people':len(delta),'n_nights':len(frame),
            'improved_people':int((delta>1e-8).sum()),'worse_people':int((delta< -1e-8).sum()),
            'unchanged_people':int((abs(delta)<=1e-8).sum()),'method':'paired participant bootstrap',
            'replicates':replicates,'seed':SEED}

def load_frame(project_root,cohort):
    path=project_root/'데이터모델/연구용데이터/next_night_run_20261020_v1/analysis'/cohort/'episodes.parquet'
    frame=pd.read_parquet(path).reset_index(drop=True)
    assert frame.groupby('subject_id').split.nunique().max()==1
    assert not frame.row_id.duplicated().any()
    assert not frame.duplicated(['subject_id','cutoff']).any()
    return frame,path

def train_phase(project_root,run,cohort,protocol):
    out=run/cohort;out.mkdir(parents=True,exist_ok=False)
    frame,source=load_frame(project_root,cohort)
    train=frame.loc[frame.split.eq('train')].reset_index(drop=True)
    val=frame.loc[frame.split.eq('validation')].reset_index(drop=True)
    candidates=protocol['candidates'];results=[];fold_meta=[]
    for fold,(ii,jj) in enumerate(GroupKFold(3).split(train,groups=train.subject_id)):
        fit=train.iloc[ii].copy().reset_index(drop=True);held=train.iloc[jj].copy().reset_index(drop=True)
        fit['split']='train';held['split']='validation'
        assert not set(fit.subject_id)&set(held.subject_id)
        prior=prior_from_train(fit)
        xf,sf=replay(fit,prior);xv,sv=replay(held,prior)
        fold_meta.append({'fold':fold,'prior':prior,'fit_people':fit.subject_id.nunique(),
                          'held_people':held.subject_id.nunique(),'fit_rows':len(fit),'held_rows':len(held)})
        log(f'{cohort} fold {fold+1}/3: causal features rebuilt')
        for rep in REPS:
            rf=reference(xf,prior,rep);rv=reference(xv,prior,rep)
            for arm in ARMS:
                zf=encode(fit,xf,sf,rep,arm);zv=encode(held,xv,sv,rep,arm)
                for name,c in candidates.items():
                    model,info=fit_one(c,fit,zf,rf,SEED)
                    pred=predict([model],zv,rv)
                    results.append({'fold':fold,'representation':rep,'arm':arm,'candidate':name,
                                    'family':c['family'],**metrics(held,pred),**info})
            pd.DataFrame([{k:v for k,v in r.items() if k!='warnings'} for r in results]).to_csv(out/'inner_cv_progress.csv',index=False)
            log(f'{cohort} fold {fold+1}/3 {rep} complete ({len(results)} fitted models)')
    dump(out/'inner_cv_details.json',results);dump(out/'fold_provenance.json',fold_meta)
    cv=pd.DataFrame(results);selected=[]
    for rep in REPS:
        for family in FAMILIES:
            vals=cv.loc[cv.representation.eq(rep)&cv.family.eq(family)].groupby('candidate').joint_mae_min.mean()
            name=str(vals.sort_values(kind='stable').index[0])
            selected.append({'pair_id':family+'__'+rep,'representation':rep,'family':family,'candidate':name,
                             'cv_pair_mean_mae_min':float(vals[name]),
                             'cv_data_mae_min':float(cv.loc[cv.representation.eq(rep)&cv.candidate.eq(name)&cv.arm.eq('data')].joint_mae_min.mean()),
                             'cv_hybrid_mae_min':float(cv.loc[cv.representation.eq(rep)&cv.candidate.eq(name)&cv.arm.eq('hybrid')].joint_mae_min.mean())})
    primary=min(selected,key=lambda r:(r['cv_pair_mean_mae_min'],r['pair_id']))['pair_id']
    prior=prior_from_train(train)
    dump(out/'train_selection.json',{'primary_pair':primary,'pairs':selected,'prior':prior,
        'source_sha256':sha(source),'protocol_sha256':sha(run/'protocol.json'),
        'selection_scope':'original TRAIN only; paired mean across arms and folds',
        'test_scored':False})
    log(f'{cohort} TRAIN selection locked: {primary}')
    xf,sf=replay(train,prior);xv,sv=replay(val,prior)
    val.to_parquet(out/'validation_rows_private.parquet',index=False)
    pd.concat([val[['row_id','subject_id','split','cutoff']],xv.drop(columns='row_id')],axis=1).to_parquet(out/'validation_features_private.parquet',index=False)
    models_dir=out/'models';models_dir.mkdir()
    val_preds={};fits=[]
    for pair in selected:
        rep=pair['representation']; c=candidates[pair['candidate']]
        rf=reference(xf,prior,rep);rv=reference(xv,prior,rep)
        for arm in ARMS:
            zf=encode(train,xf,sf,rep,arm);zv=encode(val,xv,sv,rep,arm)
            seeds=protocol['mlp_final_seeds'] if c['family']=='mlp' else [SEED]
            models=[]
            for seed in seeds:
                model,info=fit_one(c,train,zf,rf,seed);models.append(model)
                fits.append({'pair_id':pair['pair_id'],'arm':arm,'seed':seed,**info})
            key=pair['pair_id']+'__'+arm
            artifact={'models':models,'representation':rep,'arm':arm,'feature_order':list(zf.columns),
                      'prior':prior,'candidate':pair['candidate'],'seeds':seeds}
            joblib.dump(artifact,models_dir/(key+'.joblib'))
            val_preds[key]=predict(models,zv,rv)
        log(f'{cohort} full TRAIN fit {pair["pair_id"]} complete')
    np.savez_compressed(out/'validation_predictions_private.npz',**val_preds)
    dump(out/'final_fit_details.json',fits)
    pd.DataFrame([{'pair_arm':k,**metrics(val,p)} for k,p in val_preds.items()]).to_csv(out/'validation_model_scores.csv',index=False)
    dump(out/'training_done.json',{'cohort':cohort,'models':len(val_preds),'primary_pair':primary,
         'test_scored':False,'software':{'python':platform.python_version(),'sklearn':sklearn.__version__,
         'numpy':np.__version__,'pandas':pd.__version__}})
    log(f'{cohort} TRAIN complete; Test untouched')

def calibrate_phase(run,cohort,protocol):
    from personalization_ablation import POLICIES,replay_personalization,policy_specs
    out=run/cohort
    if (out/'calibration_lock.json').exists():raise ValueError('Calibration lock already exists')
    if any(run.glob('*/test_started.json')) or any(run.glob('*/test_results.json')):
        raise ValueError('Cannot recalibrate after Test evaluation has begun')
    selection=json.loads((out/'train_selection.json').read_text('utf8'))
    frame=pd.read_parquet(out/'validation_rows_private.parquet')
    loaded=np.load(out/'validation_predictions_private.npz')
    key=selection['primary_pair'];base={a:loaded[key+'__'+a] for a in ARMS}
    rows=[];corrected={};commons=[]
    for policy in POLICIES:
        for arm in ARMS:
            pred,trace=replay_personalization(frame,base[arm],policy)
            corrected[policy,arm]=pred
            rows.append({'policy':policy,'arm':arm,**metrics(frame,pred),'active_nights':int(trace.active.sum())})
        avg=(participant_errors(frame,corrected[policy,'data'])+participant_errors(frame,corrected[policy,'hybrid']))/2
        commons.append({'policy':policy,'pair_mean_mae_min':float(avg.mean())})
    best=min(commons,key=lambda r:(r['pair_mean_mae_min'],r['policy']!='none',r['policy']))
    common_base=(participant_errors(frame,base['data'])+participant_errors(frame,base['hybrid']))/2
    common_new=(participant_errors(frame,corrected[best['policy'],'data'])+participant_errors(frame,corrected[best['policy'],'hybrid']))/2
    deltas=(common_base-common_new).to_numpy();rng=np.random.default_rng(SEED)
    boots=deltas[rng.integers(0,len(deltas),(10000,len(deltas)))].mean(axis=1)
    ci=np.quantile(boots,[.025,.975]).tolist();gain=float(deltas.mean())
    policy=best['policy'] if gain>=.5 and ci[0]>0 else 'none'
    blends=[]
    for w in protocol['blend_weights']:
        p=project((1-w)*base['data']+w*base['hybrid'])
        blends.append({'physics_weight':w,**metrics(frame,p)})
    chosen=min(blends,key=lambda r:(r['joint_mae_min'],r['physics_weight']))
    blend_gain=metrics(frame,base['data'])['joint_mae_min']-chosen['joint_mae_min']
    weight=chosen['physics_weight'] if blend_gain>=.5 else 0
    lock={'cohort':cohort,'primary_pair':key,'common_personalization_policy':policy,
          'candidate_best_policy':best['policy'],'candidate_gain_min':gain,'candidate_gain_ci95':ci,
          'blend_physics_weight':weight,'candidate_blend_gain_min':blend_gain,'policies':policy_specs(),
          'personalization_source_sha256':sha(HERE/'personalization_ablation.py'),
          'experiment_source_sha256':sha(Path(__file__)),
          'common_policy_validation_scores':commons,'blend_validation_scores':blends,
          'train_selection_sha256':sha(out/'train_selection.json'),
          'protocol_sha256':sha(run/'protocol.json'),'test_scored':False,
          'model_hashes':{p.name:sha(p) for p in sorted((out/'models').glob('*.joblib'))}}
    pd.DataFrame(rows).to_csv(out/'validation_personalization_scores.csv',index=False)
    dump(out/'calibration_lock.json',lock)
    log(f'{cohort} VALIDATION lock: personal={policy}, physics blend={weight}; Test untouched')

def evaluate_phase(project_root,run,cohort,protocol):
    from personalization_ablation import POLICIES,replay_personalization
    # Verify every dependency of BOTH cohort locks before reading Test outcomes.
    verify_test_locks(run)
    out=run/cohort
    if (out/'test_results.json').exists():raise ValueError('Test already evaluated; do not overwrite')
    with (out/'test_started.json').open('x',encoding='utf8') as marker:
        json.dump({'cohort':cohort,'calibration_lock_sha256':sha(out/'calibration_lock.json')},marker)
    lock=json.loads((out/'calibration_lock.json').read_text('utf8'))
    selection=json.loads((out/'train_selection.json').read_text('utf8'))
    if lock['protocol_sha256']!=sha(run/'protocol.json'):raise ValueError('Protocol hash mismatch')
    frame,source=load_frame(project_root,cohort)
    assert sha(source)==selection['source_sha256']
    train=frame.loc[frame.split.eq('train')].reset_index(drop=True)
    test=frame.loc[frame.split.eq('test')].reset_index(drop=True)
    x,states=replay(test,selection['prior'])
    preds={};comparisons=[];model_rows=[]
    for pair in selection['pairs']:
        for arm in ARMS:
            key=pair['pair_id']+'__'+arm
            artifact=joblib.load(out/'models'/(key+'.joblib'))
            z=encode(test,x,states,pair['representation'],arm)
            assert list(z.columns)==artifact['feature_order']
            p=predict(artifact['models'],z,reference(x,selection['prior'],pair['representation']))
            preds[key]=p
            model_rows.append({'pair_id':pair['pair_id'],'family':pair['family'],
                'representation':pair['representation'],'candidate':pair['candidate'],'arm':arm,**metrics(test,p)})
        comparisons.append({'pair_id':pair['pair_id'],'primary':pair['pair_id']==lock['primary_pair'],
            **paired(test,preds[pair['pair_id']+'__data'],preds[pair['pair_id']+'__hybrid'])})
    # Holm control for the 12 exploratory matched-pair tests within each cohort.
    order=np.argsort([r['sign_flip_p'] for r in comparisons]);running=0.
    for i,ix in enumerate(order):
        running=max(running,min(1.,(len(order)-i)*comparisons[ix]['sign_flip_p']))
        comparisons[ix]['holm_p']=running
    key=lock['primary_pair'];base={a:preds[key+'__'+a] for a in ARMS};policy=lock['common_personalization_policy']
    factorial={};policy_scores=[]
    for arm in ARMS:
        factorial[arm]=base[arm]
        for name in POLICIES:
            corrected,trace=replay_personalization(test,base[arm],name)
            policy_scores.append({'arm':arm,'policy':name,'selected':name==policy,
                **metrics(test,corrected),'active_nights':int(trace.active.sum()),**paired(test,base[arm],corrected)})
            if name==policy:
                factorial[arm+'_personalized']=corrected
                trace.to_parquet(out/(arm+'_personalization_trace_private.parquet'),index=False)
    w=lock['blend_physics_weight']
    factorial['validation_blend']=project((1-w)*base['data']+w*base['hybrid'])
    factorial['validation_blend_personalized'],_=replay_personalization(test,factorial['validation_blend'],policy)
    reference_baselines={'physical':project(x[['physical_bed','physical_wake']].to_numpy(float)),
                        'recent_median':project(x[['median14_bed','median14_wake']].to_numpy(float))}
    old=project_root/'데이터모델/연구용데이터/robust_next_night_run_20261020_v2/analysis'/cohort
    for arm in ARMS:
        f=pd.read_csv(old/('test_'+arm+'.csv')).set_index('row_id').loc[test.row_id]
        assert np.allclose(f[TARGETS].to_numpy(),test[TARGETS].to_numpy())
        assert f.subject_id.astype(str).tolist()==test.subject_id.astype(str).tolist()
        reference_baselines['previous_v2_'+arm]=f[['predicted_bed_offset','predicted_wake_offset']].to_numpy(float)
    contrasts={name:paired(test,base['data'],p) for name,p in factorial.items() if name!='data'}
    contrasts['hybrid_personalization_increment']=paired(test,base['hybrid'],factorial['hybrid_personalized'])
    contrasts['final_vs_previous_data']=paired(test,reference_baselines['previous_v2_data'],factorial['validation_blend_personalized'])
    rare,rare_definition=target_flagger(train,test)
    qc=[]
    for group,mask in [('all',np.ones(len(test),bool)),('rare',rare),('other',~rare)]:
        for name,p in factorial.items():qc.append({'stratum':group,'model':name,**metrics(test.loc[mask].reset_index(drop=True),p[mask])})
    history=states.valid_days.to_numpy()
    for label,mask in [('0',history==0),('1-3',(history>=1)&(history<=3)),('4-7',(history>=4)&(history<=7)),
                       ('8-13',(history>=8)&(history<=13)),('14+',history>=14)]:
        if mask.any():
            for name,p in factorial.items():qc.append({'stratum':'history_'+label,'model':name,**metrics(test.loc[mask].reset_index(drop=True),p[mask])})
    np.savez_compressed(out/'test_predictions_private.npz',**preds,**factorial,**reference_baselines)
    test.to_parquet(out/'test_rows_private.parquet',index=False)
    states.to_parquet(out/'test_states_private.parquet',index=False)
    pd.DataFrame(model_rows).to_csv(out/'test_matched_models.csv',index=False)
    pd.DataFrame(qc).to_csv(out/'test_qc_history_strata.csv',index=False)
    dump(out/'test_policy_exploration.json',policy_scores)
    dump(out/'test_results.json',{'cohort':cohort,'primary_pair':key,'calibration_lock_sha256':sha(out/'calibration_lock.json'),
        'common_policy':policy,'physics_blend_weight':w,'matched_pairs':comparisons,
        'factorial_metrics':{k:metrics(test,p) for k,p in factorial.items()},
        'reference_metrics':{k:metrics(test,p) for k,p in reference_baselines.items()},
        'contrasts':contrasts,'rare_definition':rare_definition,'test_previously_exposed':True,
        'no_service_promotion':True})
    log(f'{cohort} TEST evaluated once: {key}; {len(comparisons)} matched pairs retained')

def main():
    p=argparse.ArgumentParser();p.add_argument('--project-root',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--cohort',choices=['cmi','mesa'],required=True)
    p.add_argument('--phase',choices=['train','calibrate','evaluate'],required=True);a=p.parse_args()
    protocol=json.loads((a.run/'protocol.json').read_text('utf8'))
    verify_run_sources(a.run)
    with threadpool_limits(limits=1):
        if a.phase=='train':train_phase(a.project_root,a.run,a.cohort,protocol)
        elif a.phase=='calibrate':calibrate_phase(a.run,a.cohort,protocol)
        else:evaluate_phase(a.project_root,a.run,a.cohort,protocol)
if __name__=='__main__':main()
