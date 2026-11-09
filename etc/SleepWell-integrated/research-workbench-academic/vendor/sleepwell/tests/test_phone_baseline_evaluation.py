import copy
from pathlib import Path
import unittest
from fusion_forecast import PortableCohortModel
from phone_baseline_evaluation import (BASELINES, METHODS, PROFILE_ROLE, REFERENCE,
    SCHEDULE_PROFILE_ROLE, evaluate_phone_baselines)


MODELS = Path(__file__).resolve().parents[1] / 'models'


def baseline(key):
    if key == 'next_day_first_schedule_only_profile_duration':
        return {'available':False,'onset_offset_min':None,'wake_offset_min':None,'interval_min':None,
                'method':METHODS[key],'fallback':None,'contributing_records':0,'reason':'no_schedule_context'}
    fallback = PROFILE_ROLE if key == 'profile_clock_assumption' else None
    count = 0 if fallback else 1
    return {'available':True,'onset_offset_min':180,'wake_offset_min':660,'interval_min':480,
            'method':METHODS[key],'fallback':fallback,'contributing_records':count,'reason':None}


def sample():
    frozen = {'schema_version':1,'version':'android-research-baselines-v1',
        'cutoff':'2026-10-24T11:00:00Z','model_identifier':'research_comparator_only_no_recommendation',
        'history_count':1,
        'history_policy':'manual only; wake and modified_at strictly before cutoff; latest version per KST wake date',
        'profile_fallback_role':PROFILE_ROLE,
        'baselines':{key:baseline(key) for key in BASELINES}}
    forecast={'night_id':'2026-10-24','cutoff':frozen['cutoff'],'generated_at':'2026-10-24T11:05:00Z',
              'forecast_status':'prospective_20_window','research_baselines':frozen}
    outcome={'night_id':'2026-10-24','status':'matched','actual_source':'manual',
             'target':'first_self_reported_sleep_onset_and_final_wake',
             'available_at':'2026-10-25T00:00:00Z','outcome_at':'2026-10-24T22:00:00Z',
             'actual_onset_offset_min':200,'actual_wake_offset_min':660,'source_research_baselines':copy.deepcopy(frozen)}
    return {'prospective_forecasts':[forecast],'prospective_outcomes':[outcome]}


def add_model_pair(value):
    forecast, outcome = value['prospective_forecasts'][0], value['prospective_outcomes'][0]
    artifacts = {}
    predictions = {}
    for cohort in ('cmi','mesa'):
        model = PortableCohortModel(MODELS / f'robust_{cohort}_v2.json', cohort)
        artifacts[cohort] = {'cohort':cohort,'sha256':model.sha256,
            'model_version':model.raw['model_version'],'feature_contract':model.raw['feature_contract'],
            'physical_provider':model.raw['physical_provider'],
            'source_model_sha256':copy.deepcopy(model.raw['source_model_sha256'])}
        for arm in ('physical','data','hybrid'):
            predictions[f'{arm}_{cohort}']={'onset_offset_min':190,'wake_offset_min':650}
    forecast.update(model_version='robust-next-night-research-v2',
        model_feature_contract='sleepwell_next_night_robust_v2',
        research_artifacts=artifacts,research_predictions=predictions)
    outcome.update(source_model_version=forecast['model_version'],
        source_model_feature_contract=forecast['model_feature_contract'],
        source_research_artifacts=copy.deepcopy(artifacts))


class FrozenBaselineTests(unittest.TestCase):
    def evaluate(self,value):return evaluate_phone_baselines(value,'2026-10-25T11:00:00Z')
    def test_recomputes_errors_and_true_first_report_lag(self):
        value=sample();value['prospective_outcomes'][0]['research_baseline_residuals']={'fake':999}
        result,lag=self.evaluate(value)
        self.assertEqual(result['n_matched_nights'],1)
        reference=next(row for row in result['models'] if row['name']==REFERENCE)
        self.assertEqual(reference['joint_mae_min'],10)
        self.assertIsNone(reference['paired_skill_vs_mean14'])
        self.assertEqual(lag['median_hours'],2)
    def test_no_backfill_excluded_outcome_or_late_forecast(self):
        for key,value in [('status','excluded'),('actual_source','samsung_health'),('available_at','2026-10-24T21:00:00Z')]:
            data=sample();data['prospective_outcomes'][0][key]=value
            self.assertEqual(self.evaluate(data)[0]['models'],[])
        data=sample();data['prospective_forecasts'][0]['generated_at']='2026-10-24T11:16:00Z'
        self.assertEqual(self.evaluate(data)[0]['models'],[])
    def test_conflicting_duplicate_cannot_replace_first_exclusion(self):
        data=sample();first=copy.deepcopy(data['prospective_outcomes'][0]);first['status']='excluded'
        data['prospective_outcomes'].insert(0,first)
        result,_=self.evaluate(data)
        self.assertEqual(result['models'],[])
        self.assertEqual(result['excluded']['conflicting_duplicate_nights'],1)
    def test_frozen_provenance_and_timestamp_mismatch(self):
        for key,value in [('source_research_baselines',{}),('outcome_at','2026-10-24T22:15:00Z'),('actual_onset_offset_min',float('nan'))]:
            data=sample();data['prospective_outcomes'][0][key]=value
            self.assertEqual(self.evaluate(data)[0]['models'],[])
    def test_duplicate_identical_is_single_night(self):
        data=sample();data['prospective_outcomes']*=2;data['prospective_forecasts']*=2
        self.assertEqual(self.evaluate(data)[0]['n_matched_nights'],1)
    def test_missing_arm_and_invalid_interval_fail_closed(self):
        data=sample();data['prospective_forecasts'][0]['research_baselines']['baselines'].pop('circular_mean_7_calendar_days')
        data['prospective_outcomes'][0]['source_research_baselines']=copy.deepcopy(data['prospective_forecasts'][0]['research_baselines'])
        self.assertEqual(self.evaluate(data)[0]['models'],[])
        data=sample();data['prospective_forecasts'][0]['research_baselines']['baselines']['arithmetic_mean_14_calendar_days']['interval_min']=479
        data['prospective_outcomes'][0]['source_research_baselines']=copy.deepcopy(data['prospective_forecasts'][0]['research_baselines'])
        self.assertEqual(self.evaluate(data)[0]['models'],[])
    def test_installed_model_identity_and_self_consistent_tamper(self):
        data=sample();add_model_pair(data)
        result,_=evaluate_phone_baselines(data,'2026-10-25T11:00:00Z',MODELS)
        model_names={row['name'] for row in result['models'] if not row['name'].startswith('baseline:')}
        self.assertEqual(len(model_names),6)
        data['prospective_forecasts'][0]['research_artifacts']['cmi']['sha256']='0'*64
        data['prospective_outcomes'][0]['source_research_artifacts']=copy.deepcopy(
            data['prospective_forecasts'][0]['research_artifacts'])
        result,_=evaluate_phone_baselines(data,'2026-10-25T11:00:00Z',MODELS)
        names={row['name'] for row in result['models']}
        self.assertIn(REFERENCE,names)
        self.assertFalse(any(name.endswith('_cmi') for name in names))
        self.assertTrue(any(name.endswith('_mesa') for name in names))
    def test_missing_model_directory_excludes_only_model_arms(self):
        data=sample();add_model_pair(data)
        result,_=evaluate_phone_baselines(data,'2026-10-25T11:00:00Z',MODELS/'missing')
        self.assertIn(REFERENCE,{row['name'] for row in result['models']})
        self.assertFalse(any(row['name'].startswith('robust-') for row in result['models']))


if __name__=='__main__':unittest.main()
