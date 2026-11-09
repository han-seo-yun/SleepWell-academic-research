import unittest
import numpy as np
import pandas as pd
from full_validity_experiments import circular_center,extended_baselines,block_skill_ci,feasibility,split_half


class FullValidityTests(unittest.TestCase):
    def test_circular_midnight_not_noon_and_ambiguous_fallback(self):
        value,supported=circular_center([1430,10],42)
        self.assertTrue(supported);self.assertLess(min(value,1440-value),1e-8)
        self.assertEqual(circular_center([0,720],42),(42.,False))

    def test_causal_and_calendar_daytype(self):
        prior={'bed_offset':180,'wake_offset':660}
        records=[{'source':'research','bed':'2026-10-23T23:00:00+09:00','wake':'2026-10-24T07:00:00+09:00','observed_at':'2026-10-24T08:00:00+09:00'}]
        cutoff='2026-10-24T20:00:00+09:00';enroll='2026-09-01T00:00:00+09:00'
        p,d=extended_baselines(records,cutoff,prior,enroll,{1:[240,720]})
        self.assertEqual(tuple(p['calendar_train']),(240,720))
        future={'source':'research','bed':'2026-10-24T23:00:00+09:00','wake':'2026-10-25T11:00:00+09:00','observed_at':'2026-10-25T12:00:00+09:00'}
        p2,d2=extended_baselines(records+[future],cutoff,prior,enroll,{1:[240,720]})
        self.assertEqual(p,p2);self.assertEqual(d,d2)

    def test_old_history_not_used_in_recent_window(self):
        prior={'bed_offset':180,'wake_offset':660}
        records=[{'source':'research','bed':'2026-08-01T22:00:00+09:00','wake':'2026-08-02T06:00:00+09:00','observed_at':'2026-08-02T07:00:00+09:00'}]
        p,_=extended_baselines(records,'2026-10-24T20:00:00+09:00',prior,'2026-07-01T00:00:00+09:00',{})
        self.assertEqual(tuple(p['mean28']),(180,660))

    def test_individual_ci_zero_guard_and_index_alignment(self):
        dates=pd.Series(pd.date_range('2026-01-01',periods=5),index=[5,6,7,8,9])
        self.assertIsNone(block_skill_ci([0]*5,[.1]*5,dates))
        self.assertEqual(block_skill_ci([5]*5,[10]*5,dates),[.5,.5])
        self.assertEqual(block_skill_ci([5,8,4,8,3],[10]*5,dates),block_skill_ci([5,8,4,8,3],[10]*5,dates))

    def test_feasibility_outcome_blind_and_half_missing(self):
        f=pd.DataFrame({'subject_id':['a']*4,'cutoff':pd.date_range('2026-01-01',periods=4),'bed_offset':[1,2,3,4],'wake_offset':[500]*4})
        _,gate=feasibility(f)
        f.bed_offset=999
        self.assertEqual(gate,feasibility(f)[1]);self.assertFalse(gate['longitudinal_gate_min30_people_passed'])
        _,summary=split_half(f,{k:np.zeros((4,2)) for k in ('physical','data','hybrid')},np.zeros((4,2)))
        self.assertEqual(summary,[])


if __name__=='__main__':unittest.main()
