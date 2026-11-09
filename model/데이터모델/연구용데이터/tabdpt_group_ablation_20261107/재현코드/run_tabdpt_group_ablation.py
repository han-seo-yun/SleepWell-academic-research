"""Predeclared validation-only drop-group experiment, no source mutations."""
from pathlib import Path
import os,sys,json,hashlib,time,gc
os.environ['HF_HUB_OFFLINE']='1'
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'vendor/sleepwell'))
import numpy as np
import pandas as pd
import torch
from robust_features import DATA_V2
from tabular_foundation_models import _make_model,_seed_runtime
from tabdpt import distribution_median
from deep_tabular_models import constrain_predictions

SRC=Path('E:/학술제/회의/Workbench_보고서_2026-11-06/deep_learning_comparison_20261105/private')
OLD=SRC.parents[1]/'tabicl_tabdpt_comparison_20261105'
OUT=Path('E:/학술제/데이터모델/연구용데이터/tabdpt_group_ablation_20261107')
PRESENT=Path('E:/학술제/데이터모델/연구용데이터/agent_collaboration_real_20261107/발표자료')
GROUPS=json.loads((PRESENT/'DATA33_특성목록.json').read_text(encoding='utf-8'))
WEIGHTS=ROOT/'.models/tabdpt-v1.3/tabdpt1_3.safetensors'
SEED=20261105
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,value):
    p=Path(p);tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');tmp.replace(p)
def log(event,**kw):
    value={'time':time.strftime('%Y-%m-%d %H:%M:%S'),'event':event,**kw}
    line=json.dumps(value,ensure_ascii=False);print(line,flush=True)
    with (OUT/'progress.jsonl').open('a',encoding='utf-8') as f:f.write(line+'\n')

def main():
    OUT.mkdir(exist_ok=True);(OUT/'private').mkdir(exist_ok=True)
    prov=json.loads((SRC/'provenance.json').read_text(encoding='utf-8'))
    old=json.loads((OLD/'protocol.json').read_text(encoding='utf-8'))
    assert sha(WEIGHTS)==old['candidates']['tabdpt_v1_3']['sha256']
    for k in ['input_csv','frozen_predictions']:
        assert sha(prov[k]['path'])==prov[k]['sha256']
    flat=[x for g in GROUPS for x in g['features']]
    assert len(flat)==len(set(flat))==33 and set(flat)==set(DATA_V2)
    source=pd.read_csv(prov['input_csv']['path'],dtype={'participant_id':str,'row_id':str})
    datasets={};configs=[]
    for cohort in ('cmi','mesa'):
        path=SRC/f'{cohort}.npz';assert sha(path)==prov['private_artifacts'][path.name]
        artifact=ROOT/f'vendor/sleepwell/models/robust_{cohort}_v2.json'
        assert sha(artifact)==prov['vendor_provenance']['artifacts'][artifact.name]['sha256']
        pp=json.loads(artifact.read_text(encoding='utf-8'))['models']['data']['preprocessing']
        assert not pp['missing_indicator_features']
        names=[DATA_V2[i] for i in pp['variance_keep']]
        ds={'names':names}
        with np.load(path,allow_pickle=False) as z:
            assert tuple(prov['cohorts'][cohort]['feature_orders']['data'])==DATA_V2
            for split in ['train','validation']:
                m=z['splits']==split
                xx=z['X_data'][m].copy();raw=z['X_data_raw'][m].copy();yy=z['y'][m].copy()
                ids=z['groups'][m].copy();rids=z['row_ids'][m].copy()
                assert np.isfinite(xx).all() and np.isfinite(yy).all()
                processed=(np.clip(np.where(np.isnan(raw),np.array(pp['imputer_median']),raw),pp['clip_low'],pp['clip_high'])[:,pp['variance_keep']]-np.array(pp['scale_mean']))/np.array(pp['scale'])
                np.testing.assert_allclose(xx,processed,atol=1e-12)
                check=source[source.cohort.eq(cohort)].set_index('row_id').loc[rids]
                assert check.split.eq(split).all() and list(check.participant_id)==list(ids)
                np.testing.assert_allclose(yy,check[['bed_offset','wake_offset']].to_numpy(),atol=1e-9)
                ds[split]={'X':xx,'y':yy,'ids':ids,'rowids':rids}
            assert not(set(ds['train']['ids'])&set(ds['validation']['ids']))
        datasets[cohort]=ds
        configs.append({'cohort':cohort,'train_nights':len(ds['train']['y']),'train_people':len(set(ds['train']['ids'])),
                        'validation_nights':len(ds['validation']['y']),'validation_people':len(set(ds['validation']['ids'])),
                        'effective_features':names,'cache_sha256':sha(path),'model_sha256':sha(artifact)})
    protocol={'schema':'tabdpt.dropgroup.v1','created':time.strftime('%Y-%m-%d %H:%M:%S'),
      'model':'TabDPT v1.3','checkpoint_sha256':sha(WEIGHTS),'groups':GROUPS,'datasets':configs,
      'design':'all33 vs removal of each of 6 pre-existing presentation groups; no outcome-based grouping',
      'seed':SEED,'ensembles':4,'context':'all Train rows, context_size=None','point':'predictive distribution median',
      'batch':'one predict call per target, internal batch 4096; OOM fallback 1024/256/64 for all cells if needed',
      'preprocessing':'existing frozen Train-only median, clipping 1/99, constant removal, scaling; subset transformed columns identically for train and validation',
      'primary':'participant equal-weight mean of onset and wake absolute errors, minutes; equal cohort macro',
      'personalization':'gradual_v1 causal input features; no extra residual correction to isolate feature contribution',
      'uncertainty':'paired participant bootstrap 5000; 95% pointwise and 99.5833% Bonferroni across 12 cohort/group comparisons; fixed seed model, no training uncertainty',
      'test':'not used for fit, predictions, metrics, selection; no model promotion',
      'scope':'exploratory reused validation, conditional on fixed pretrained model and existing Train-fitted preprocessing',
      'script_sha256':sha(__file__)}
    lock=OUT/'protocol.json'
    if lock.exists():
        prior=json.loads(lock.read_text(encoding='utf-8'));assert prior['script_sha256']==protocol['script_sha256']
    else:write(lock,protocol)
    log('verified_protocol_locked',datasets=configs)
    variants=[('all33','전체 33개',[])]+[(f'drop_{i+1}',g['group'],g['features']) for i,g in enumerate(GROUPS)]
    for cohort,ds in datasets.items():
        train,val=ds['train'],ds['validation']
        for variant,title,remove in variants:
            dest=OUT/'private'/f'{cohort}_{variant}.npz'
            if dest.exists():
                log('resume_existing',cohort=cohort,variant=variant);continue
            keep=[i for i,n in enumerate(ds['names']) if n not in remove]
            x=train['X'][:,keep];q=val['X'][:,keep];channels=[];metadata=[]
            log('start',cohort=cohort,variant=variant,title=title,effective_features=len(keep))
            for channel in range(2):
                _seed_runtime(SEED);torch.cuda.empty_cache()
                model=_make_model('tabdpt_v1_3',WEIGHTS,SEED,4,None)
                model.fit(x,train['y'][:,channel]);start=time.perf_counter()
                for batch in [4096,1024,256,64]:
                    try:
                        full=model.predict(q,n_ensembles=4,context_size=None,batch_size=batch,seed=SEED,output_type='full')
                        pred=distribution_median(full)
                        if hasattr(pred,'detach'):pred=pred.detach().cpu().numpy()
                        pred=np.asarray(pred).reshape(-1)
                        assert len(pred)==len(q) and np.isfinite(pred).all()
                        break
                    except torch.cuda.OutOfMemoryError:
                        log('oom_retry',cohort=cohort,variant=variant,batch=batch);torch.cuda.empty_cache()
                else:raise RuntimeError('OOM fallback exhausted')
                channels.append(pred);metadata.append({'channel':channel,'seconds':time.perf_counter()-start,'batch':batch})
                # Batch invariance check uses Train labels only, no comparison scores.
                if variant=='all33':
                    fullsmall=model.predict(q[:8],n_ensembles=4,context_size=None,batch_size=8,seed=SEED,output_type='full')
                    small=distribution_median(fullsmall)
                    if hasattr(small,'detach'):small=small.detach().cpu().numpy()
                    gap=float(np.max(abs(pred[:8]-np.asarray(small).reshape(-1))))
                    assert gap<.01,('batch_invariance_failed',gap)
                    metadata[-1]['batch_check_max_min']=gap
                    del fullsmall,small
                log('channel_complete',cohort=cohort,variant=variant,**metadata[-1])
                del model,full;gc.collect();torch.cuda.empty_cache()
            prediction=constrain_predictions(np.column_stack(channels))
            np.savez_compressed(dest,prediction=prediction,actual=val['y'],participant_id=val['ids'],row_id=val['rowids'])
            write(OUT/f'{cohort}_{variant}_metadata.json',{'cohort':cohort,'variant':variant,'title':title,'dropped':remove,
                     'remaining_raw':33-len(remove),'effective':len(keep),'channels':metadata,'sha256':sha(dest)})
            log('variant_complete',cohort=cohort,variant=variant)
    log('all_inference_complete')

if __name__=='__main__':main()
