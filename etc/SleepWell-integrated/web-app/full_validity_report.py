"""Read-only, bounded public research tables (no participant-level artifacts)."""
import json
import math
import re
from pathlib import Path

PANEL_IDS = {'baselines','profiles','within','stability','variability','feasibility',
             'repeat','labels','quality','constructs','psg','applicability'}


def load_full_validity_report(path):
    try:
        raw=Path(path).read_bytes()
        if len(raw)>250000:raise ValueError('Oversize')
        value=json.loads(raw)
        if set(value)!={'schema_version','artifact_type','version','test_status','no_service_promotion','panels'}:
            raise ValueError('Unexpected fields')
        if type(value['schema_version']) is not int or value['schema_version']!=1 or value['artifact_type']!='sleepwell_full_validity_tables':
            raise ValueError('Invalid schema')
        if value['no_service_promotion'] is not True or value['test_status']!='retrospective_reused_test':
            raise ValueError('Research-only contract required')
        def text(v,limit=1500):
            if not isinstance(v,str) or len(v)>limit or re.search(r'[A-Za-z]:[\\/]|mesa_[0-9a-f]{20}|@',v):
                raise ValueError('Invalid public text')
        text(value['version'],80)
        if not isinstance(value['panels'],list) or not 1<=len(value['panels'])<=len(PANEL_IDS):raise ValueError('Invalid panels')
        seen=set()
        for panel in value['panels']:
            if set(panel)!={'id','title','columns','rows','notes'}:raise ValueError('Invalid panel contract')
            if panel['id'] not in PANEL_IDS or panel['id'] in seen:raise ValueError('Invalid panel id')
            seen.add(panel['id']);text(panel['title'],120)
            if not isinstance(panel['columns'],list) or not 1<=len(panel['columns'])<=12:raise ValueError('Invalid columns')
            for col in panel['columns']:text(col,120)
            if not isinstance(panel['rows'],list) or len(panel['rows'])>150:raise ValueError('Too many aggregate rows')
            for row in panel['rows']:
                if not isinstance(row,list) or len(row)!=len(panel['columns']):raise ValueError('Invalid row width')
                for cell in row:
                    if isinstance(cell,str):text(cell)
                    elif cell is not None and (type(cell) not in (int,float) or not math.isfinite(cell) or abs(cell)>1e9):
                        raise ValueError('Invalid aggregate value')
            if not isinstance(panel['notes'],list) or len(panel['notes'])>12:raise ValueError('Invalid notes')
            for note in panel['notes']:text(note)
        return {'available':True,**value}
    except (OSError,ValueError,TypeError,KeyError):
        return {'available':False,'reason':'validated_public_research_tables_unavailable'}
