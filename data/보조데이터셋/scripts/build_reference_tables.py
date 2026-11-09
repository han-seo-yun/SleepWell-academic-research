"""Exploratory age-specific reference tables, never clinical targets or Korean norms."""
import argparse
import json
from pathlib import Path
import zipfile
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'processed'
AGE_BINS=[18,30,40,50,60,70,80,151]
AGE_LABELS=['18-29','30-39','40-49','50-59','60-69','70-79','80+']


def read(name):
    path=next((ROOT/'raw').glob('*/'+name+'.xpt'))
    return pd.read_sas(path,format='xport',encoding='utf-8')


def demographics(name):
    d=read(name)[['SEQN','RIDAGEYR','RIAGENDR','WTINT2YR','WTMEC2YR','SDMVSTRA','SDMVPSU']]
    d['age_group']=pd.cut(d.RIDAGEYR,AGE_BINS,labels=AGE_LABELS,right=False)
    return d


def weighted_quantile(x,w,p):
    order=np.argsort(x);x=x[order];w=w[order]
    return float(x[np.searchsorted(np.cumsum(w),p*w.sum(),side='left')])


def summarize(frame,metrics,weight,source):
    rows=[]
    for age in AGE_LABELS:
        for sex in ['all','male','female']:
            group=frame[frame.age_group==age]
            if sex!='all':group=group[group.RIAGENDR==(1 if sex=='male' else 2)]
            for metric in metrics:
                g=group.loc[group[metric].notna() & group[weight].gt(0),[metric,weight,'SEQN']]
                if g.empty:continue
                x=g[metric].to_numpy(float);w=g[weight].to_numpy(float)
                rows.append(dict(age_group=age,sex=sex,metric=metric,n_people=int(g.SEQN.nunique()),
                    weighted_mean=float(np.average(x,weights=w)),weighted_median=weighted_quantile(x,w,.5),
                    weighted_p25=weighted_quantile(x,w,.25),weighted_p75=weighted_quantile(x,w,.75),
                    kish_effective_n=float(w.sum()**2/(w*w).sum()),source=source,
                    uncertainty='No design-based CI; complete-case subset; exploratory only'))
    return pd.DataFrame(rows)


def questionnaires():
    d=demographics('DEMO_J').merge(read('PAQ_J'),on='SEQN',validate='one_to_one').merge(read('SLQ_J'),on='SEQN',how='left',validate='one_to_one')
    d=d[d.RIDAGEYR>=18].copy()
    def activity(flag,days,minutes):
        result=pd.Series(np.nan,index=d.index)
        result[d[flag]==2]=0
        valid=(d[flag]==1)&d[days].between(1,7)&d[minutes].between(10,1439)
        result[valid]=d.loc[valid,days]*d.loc[valid,minutes]/7
        return result
    d['vigorous_leisure_min_per_day']=activity('PAQ650','PAQ655','PAD660')
    d['moderate_leisure_min_per_day']=activity('PAQ665','PAQ670','PAD675')
    d['leisure_exercise_min_per_day']=d.vigorous_leisure_min_per_day+d.moderate_leisure_min_per_day
    d['transport_walk_cycle_min_per_day']=activity('PAQ635','PAQ640','PAD645')
    d['work_activity_min_per_day']=activity('PAQ605','PAQ610','PAD615')+activity('PAQ620','PAQ625','PAD630')
    d['sedentary_min_per_day']=d.PAD680.where(d.PAD680.between(0,1440))
    # Low/high grouped codes (2,15) excluded, not treated as actual measured hours.
    d['weekday_sleep_hours']=d.SLD012.where(d.SLD012.between(3,14))
    d['weekend_sleep_hours']=d.SLD013.where(d.SLD013.between(3,14))
    metrics=['leisure_exercise_min_per_day','vigorous_leisure_min_per_day','moderate_leisure_min_per_day',
             'transport_walk_cycle_min_per_day','work_activity_min_per_day','sedentary_min_per_day',
             'weekday_sleep_hours','weekend_sleep_hours']
    d[['SEQN','RIDAGEYR','RIAGENDR','WTINT2YR','SDMVSTRA','SDMVPSU']+metrics].to_parquet(OUT/'nhanes_2017_adult_activity_sleep.parquet',index=False)
    result=summarize(d,metrics,'WTINT2YR','NHANES 2017-2018 questionnaire')
    result.to_csv(OUT/'age_activity_sleep_reference.csv',index=False)
    return {'adult_rows':len(d),'exercise_complete':int(d.leisure_exercise_min_per_day.notna().sum()),'summary_rows':len(result)}


def light():
    demo=demographics('DEMO_H');day=read('PAXDAY_H')
    day['PAXDAYD']=pd.to_numeric(day.PAXDAYD)
    valid=(day.PAXDAYD.between(2,8)&(day.PAXTMD>=1440)&(day.PAXVMD>=1380)&((day.PAXWWMD+day.PAXSWMD)>=1200))
    q=day[valid].copy()
    q['sensor_mean_lux']=q.PAXLXSD/q.PAXVMD
    q['mims_sum_per_day']=q.PAXMTSD
    q['algorithm_sleep_wear_min']=q.PAXSWMD
    columns=['sensor_mean_lux','mims_sum_per_day','algorithm_sleep_wear_min']
    people=q.groupby('SEQN')[columns].mean()
    people['valid_days']=q.groupby('SEQN').size()
    people=people[people.valid_days>=4].reset_index().merge(demo,on='SEQN',validate='one_to_one')
    people=people[people.RIDAGEYR>=18]
    people.to_parquet(OUT/'nhanes_2013_light_person_summary.parquet',index=False)
    summarize(people,columns,'WTMEC2YR','NHANES 2013-2014 wrist sensor; >=4 eligible days').to_csv(OUT/'age_light_activity_reference.csv',index=False)
    return {'raw_days':len(day),'eligible_days_all_ages':len(q),'adults_with_4_days':len(people),
            'lux_note':'Daily sum of minute means divided by valid minutes; includes <=240 non-wear/unknown minutes. Not personal ocular/melanopic exposure.'}


def hourly_light():
    demo=demographics('DEMO_H');hd=read('PAXHD_H')[['SEQN','PAXFTIME']]
    hd['start_sec']=pd.to_timedelta(hd.PAXFTIME).dt.total_seconds()
    path=next((ROOT/'raw').glob('*/PAXHR_H.xpt'))
    sums=[];raw_rows=0
    for c in pd.read_sas(path,format='xport',encoding='utf-8',chunksize=200000):
        raw_rows+=len(c)
        # Floating-point zero in SAS transport can decode as a tiny positive value.
        keep=(c.PAXVMH>=50)&(c.PAXNWMH.abs()<1e-8)&(c.PAXUMH.abs()<1e-8)
        c=c[keep].merge(hd,on='SEQN',validate='many_to_one')
        c['clock_hour']=np.floor(((c.start_sec+c.PAXSSNHP/80)%86400)/3600).astype(int)
        sums.append(c.groupby(['SEQN','clock_hour'])[['PAXLXSH','PAXVMH']].sum())
    g=pd.concat(sums).groupby(level=[0,1]).sum().reset_index()
    g['sensor_mean_lux']=g.PAXLXSH/g.PAXVMH
    g=g[g.PAXVMH>=4*50].merge(demo,on='SEQN',validate='many_to_one')
    g=g[g.RIDAGEYR>=18]
    g.to_parquet(OUT/'nhanes_2013_light_person_hour.parquet',index=False)
    output=[]
    for hour,group in g.groupby('clock_hour'):
        s=summarize(group,['sensor_mean_lux'],'WTMEC2YR','NHANES 2013-2014; fully classified wear hours')
        s['clock_hour']=int(hour);output.append(s)
    pd.concat(output,ignore_index=True).to_csv(OUT/'age_hourly_light_reference.csv',index=False)
    return {'raw_hours':raw_rows,'adult_person_hour_rows':len(g),'adults':int(g.SEQN.nunique()),'condition':'VM>=50, nonwear=0, unknown=0, >=200 valid minutes across recordings per clock hour'}


def zero_nonwear(counts):
    """Exploratory strict 60-zero rule; NOT the NCI spike-tolerant implementation."""
    zero=np.abs(counts)<1e-8
    changes=np.diff(np.r_[False,zero,False].astype(int))
    starts=np.flatnonzero(changes==1);ends=np.flatnonzero(changes==-1)
    mask=np.zeros(len(counts),dtype=bool)
    for a,b in zip(starts,ends):
        if b-a>=60:mask[a:b]=True
    return mask


def steps():
    demo=demographics('DEMO_D');adult_ids=set(demo.loc[demo.RIDAGEYR>=18,'SEQN'])
    path=next((ROOT/'raw').glob('*/PAXRAW_D.zip'))
    result=[];raw_rows=0;pending=pd.DataFrame()
    def process(frame):
        for seq,g in frame.groupby('SEQN',sort=False):
            if seq not in adult_ids:continue
            g=g.sort_values('PAXN')
            if not ((g.PAXSTAT==1)&(g.PAXCAL==1)).all():continue
            intensity=g.PAXINTEN.to_numpy();steps_=g.PAXSTEP.to_numpy()
            wear=~zero_nonwear(intensity)
            valid=np.isfinite(intensity)&np.isfinite(steps_)&(intensity>=0)&(intensity<32767)&(steps_>=0)&(steps_<=300)
            wear &= valid
            days=((g.PAXN.to_numpy()-1)//1440).astype(int)+1
            for day in np.unique(days):
                select=days==day;w=select&wear
                if select.sum()!=1440 or w.sum()<600:continue
                result.append({'SEQN':seq,'day':int(day),'wear_minutes':int(w.sum()),
                    'uncensored_steps_per_day':float(steps_[w].sum()),
                    'censored_steps_per_day':float(steps_[w&(intensity>=500)].sum())})
    with zipfile.ZipFile(path) as archive:
        names=[n for n in archive.namelist() if n.lower().endswith('.xpt')]
        if len(names)!=1:raise ValueError(f'Unexpected archive members: {names}')
        with archive.open(names[0]) as source:
            for chunk in pd.read_sas(source,format='xport',encoding='utf-8',chunksize=1000000):
                raw_rows+=len(chunk)
                chunk=pd.concat([pending,chunk],ignore_index=True)
                last=chunk.SEQN.iloc[-1]
                pending=chunk[chunk.SEQN==last].copy()
                process(chunk[chunk.SEQN!=last])
                if raw_rows%10000000==0:print(f'Steps: processed {raw_rows:,} minute rows',flush=True)
            process(pending)
    daily=pd.DataFrame(result)
    daily.to_parquet(OUT/'nhanes_2005_steps_valid_days.parquet',index=False)
    columns=['uncensored_steps_per_day','censored_steps_per_day','wear_minutes']
    people=daily.groupby('SEQN')[columns].mean()
    people['valid_days']=daily.groupby('SEQN').size()
    people=people[people.valid_days>=4].reset_index().merge(demo,on='SEQN',validate='one_to_one')
    people.to_parquet(OUT/'nhanes_2005_steps_person_summary.parquet',index=False)
    summary=summarize(people,columns,'WTMEC2YR','NHANES 2005-2006 hip accelerometer; exploratory strict zero rule')
    summary.to_csv(OUT/'age_steps_reference.csv',index=False)
    return {'raw_minutes':raw_rows,'valid_adult_days':len(daily),'adults_with_4_days':len(people),
            'nonwear_rule':'strict >=60 consecutive zeros, no spike allowance; >=600 valid wear min/day; >=4 days/person',
            'censor_rule':'exclude steps where PAXINTEN <500; raw totals also retained',
            'status':'exploratory; device/era mismatch; do not auto-deploy as Apple Watch normal values'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--part',choices=['small','hourly','steps','all'],default='all');args=parser.parse_args()
    OUT.mkdir(exist_ok=True)
    report_path=OUT/'processing_report.json'
    report=json.loads(report_path.read_text()) if report_path.exists() else {}
    if args.part in ('small','all'):
        report['activity_sleep']=questionnaires();report['daily_light']=light()
    if args.part in ('hourly','all'):report['hourly_light']=hourly_light()
    if args.part in ('steps','all'):report['steps']=steps()
    report['notes']=['US historical sample, not Korean/student norms','No design-based uncertainty intervals or PAM nonresponse recalibration',
                     'Cohorts are separate cycles: never join people across cycles','Observed averages are not healthy target values']
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
