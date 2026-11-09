from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from label_sensitivity import (FIXED_OFFSET, cutoff_for, extract_mesa_candidates, offsets_against,
                               participant_errors, select_variants, valid_target)


class LabelSensitivityTests(unittest.TestCase):
    def test_cutoff_and_window(self):
        bed = pd.Timestamp("2000-01-02T01:00:00+09:00")
        self.assertEqual(cutoff_for(bed), pd.Timestamp("2000-01-01T20:00:00+09:00"))
        self.assertTrue(valid_target(bed, bed+pd.Timedelta(hours=7)))
        self.assertFalse(valid_target(bed, bed+pd.Timedelta(hours=17)))

    def test_alternative_uses_frozen_night_cutoff(self):
        cutoff=pd.Timestamp("2000-01-01T20:00:00+09:00")
        onset,wake=offsets_against(cutoff,pd.Timestamp("2000-01-01T19:59:30+09:00"),
                                   pd.Timestamp("2000-01-02T06:00:00+09:00"))
        self.assertEqual(onset,-.5)
        self.assertEqual(wake,600)

    def test_selection_is_fixed(self):
        base = pd.Timestamp("2000-01-01T20:00:00+09:00")
        rows = [{"bed":base+pd.Timedelta(hours=1),"wake":base+pd.Timedelta(hours=3),"duration_min":120},
                {"bed":base+pd.Timedelta(hours=2),"wake":base+pd.Timedelta(hours=8),"duration_min":360}]
        value = select_variants(rows)[base]
        self.assertEqual(value["original_longest"]["duration_min"],360)
        self.assertEqual(value["earliest_candidate"]["duration_min"],120)
        self.assertEqual(value["latest_candidate"]["duration_min"],360)

    def test_mesa_construct_distinguishes_tst_from_span_and_parent_quality(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/"mesa-sleep-1.csv"
            stamps=pd.date_range("2000-01-01T22:00:00",periods=64,freq="30s")
            wake=[1]+[0]*61+[0,1]
            wake[30]=1
            pd.DataFrame({"mesaid":[1]*64,"line":range(1,65),
              "linetime":stamps.strftime("%H:%M:%S"),"date_shifted":["2000-01-01"]*64,
              "offwrist":[0]*62+[1,0],"wake":wake,"interval":["REST-S"]*64}).to_csv(path,index=False)
            rows=extract_mesa_candidates(path,1)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["act_sleep_min"],30.0)
            self.assertEqual(rows[0]["first_last_sleep_span_min"],30.5)
            self.assertEqual(rows[0]["parent_valid_onwrist_fraction"],63/64)
            self.assertLess(rows[0]["monitoring_valid_onwrist_fraction"],.5)
            self.assertFalse(rows[0]["monitoring_no_cadence_or_timestamp_gap"])

    def test_participant_macro_equal_weight(self):
        frame=pd.DataFrame({"subject_id":["a","a","b"],"model_onset":[0,0,0],"model_wake":[0,0,0],
            "original_longest_onset":[10,10,100],"original_longest_wake":[10,10,100]})
        value=participant_errors(frame,"original_longest","model").joint.mean()
        self.assertEqual(value,55)


if __name__ == "__main__":
    unittest.main()
