"""Package derived summaries as CSV + SQLite + machine-readable candidate priors."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'processed'
tables={'age_activity_sleep':'age_activity_sleep_reference.csv','age_light_activity':'age_light_activity_reference.csv',
        'age_hourly_light':'age_hourly_light_reference.csv','age_steps':'age_steps_reference.csv'}
db=OUT/'supplementary_reference.sqlite3'
with sqlite3.connect(db) as connection:
    for table,file in tables.items():
        frame=pd.read_csv(OUT/file)
        frame.to_sql(table,connection,index=False,if_exists='replace')
activity=pd.read_csv(OUT/tables['age_activity_sleep'])
steps=pd.read_csv(OUT/tables['age_steps'])
entries=[]
for age in ['18-29','30-39','40-49','50-59','60-69','70-79','80+']:
    a=activity[(activity.age_group==age)&(activity.sex=='all')&(activity.metric=='leisure_exercise_min_per_day')].iloc[0]
    s=steps[(steps.age_group==age)&(steps.sex=='all')&(steps.metric=='censored_steps_per_day')].iloc[0]
    entries.append({'age_group':age,'exercise_min_per_day':float(a.weighted_mean),
                    'exercise_n_people':int(a.n_people),'steps_per_day':float(s.weighted_mean),
                    'steps_n_people':int(s.n_people),'exercise_source':'NHANES PAQ_J 2017-2018',
                    'steps_source':'NHANES PAXRAW_D 2005-2006, exploratory strict-zero nonwear',
                    'population':'US civilian noninstitutionalized population; eligible complete-case subsets',
                    'student_specific':False,'korea_validated':False,'auto_apply':False})
payload={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'status':'candidate_priors_not_validated',
         'purpose':'Group reference or weak cold-start prior; not healthy goals or individual measurements',
         'light_policy':'Do not impute individual evening lux from age means; use actual time-resolved exposure',
         'prior_strength_policy':'Not estimated yet; choose with held-out-user evaluation, not arbitrary pseudo-days',
         'entries':entries}
(OUT/'candidate_age_priors.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
for e in entries:print(e['age_group'],round(e['exercise_min_per_day'],1),round(e['steps_per_day']),e['steps_n_people'])
print('SQLite:',db)
