import unittest
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from target_validity_experiments import baseline_predictions, replay_baselines, paired_summary, within_person_diagnostic

PRIOR={'bed_offset':180.,'wake_offset':660.}
CUTOFF=datetime.fromisoformat('2026-10-25T20:00:00+09:00')


def record(days, offset=240, observed=None):
    base=CUTOFF-timedelta(days=days)
    bed=base+timedelta(minutes=offset)
    wake=bed+timedelta(hours=8)
    return {'source':'research','bed':bed.isoformat(),'wake':wake.isoformat(),
            'observed_at':(observed or wake).isoformat()}


class ValidityExperimentTests(unittest.TestCase):
    def test_future_and_late_reports_cannot_change_baselines(self):
        old=[record(2)]
        p,d=baseline_predictions(old,CUTOFF,PRIOR,None)
        updated=old+[record(0,800),record(1,500,observed=CUTOFF)]
        self.assertEqual((p,d),baseline_predictions(updated,CUTOFF,PRIOR,None))
        self.assertEqual(p['mean14'],(240,720))

    def test_calendar_days_not_record_count(self):
        p,d=baseline_predictions([record(2,120),record(10,360),record(35,600)],CUTOFF,PRIOR,None)
        self.assertEqual(p['mean7'],(120,600))
        self.assertEqual(p['mean14'],(240,720))
        self.assertEqual(d['prior_records'],3)

    def test_no_history_is_train_prior(self):
        predictions,d=baseline_predictions([],CUTOFF,PRIOR,None)
        self.assertTrue(all(v==(180,660) for v in predictions.values()))
        self.assertEqual(d['prior_records'],0)

    def test_future_target_mutation_keeps_earlier_predictions_and_subjects_isolated(self):
        rows=[]
        for subject in ('a','b'):
            for i in range(3):
                cutoff=CUTOFF+timedelta(days=i)
                rows.append({'subject_id':subject,'split':'test','cutoff':cutoff,'bed':cutoff+timedelta(hours=4),
                             'wake':cutoff+timedelta(hours=12),'observed_at':cutoff+timedelta(hours=12)})
        frame=pd.DataFrame(rows)
        expected,_=replay_baselines(frame,PRIOR)
        frame.loc[2,'bed']=frame.loc[2,'bed']+timedelta(hours=2)
        actual,_=replay_baselines(frame,PRIOR)
        for key in expected:np.testing.assert_array_equal(expected[key],actual[key])
        for key in expected:np.testing.assert_array_equal(expected[key][0],expected[key][3])

    def test_macro_weights_participants_equally_and_zero_skill_guard(self):
        actual=np.zeros((4,2)); subjects=['a','a','a','b']
        pred=np.array([[10,10]]*3+[[30,30]])
        reference=np.full((4,2),40.)
        score,_=paired_summary(actual,pred,reference,subjects)
        self.assertEqual(score['joint_mae_min'],20)
        self.assertEqual(score['skill_vs_recent14'],.5)
        guarded,_=paired_summary(actual,pred,actual,subjects)
        self.assertIsNone(guarded['skill_vs_recent14'])

    def test_centered_metric_excludes_short_constant_series(self):
        frame=pd.DataFrame({'subject_id':['a']*3+['b']*3,'bed_offset':[1,2,3,1,1,1],
                            'wake_offset':[5,6,7,5,5,5]})
        result=within_person_diagnostic(frame,frame[['bed_offset','wake_offset']].to_numpy())
        self.assertEqual(result['bed_offset']['eligible_people'],1)
        self.assertEqual(result['bed_offset']['median_centered_r2'],1.)


if __name__=='__main__':unittest.main()
