import unittest
import pandas as pd
from researchbench.data_contract import default_mapping, validate_mapping

class MappingTests(unittest.TestCase):
    def test_strict_types_and_source_unchanged(self):
        df=pd.DataFrame({'x':['1','bad']}); original=df.copy()
        m=default_mapping(df); m['x']['type']='numeric'
        with self.assertRaisesRegex(ValueError,'numeric_conversion'): validate_mapping(df,m)
        pd.testing.assert_frame_equal(df,original)
    def test_timezone_ambiguous_missing_and_allowed(self):
        df=pd.DataFrame({'t':['2026-01-01T20:00:00'], 'group':['a']}); m=default_mapping(df)
        m['t'].update(type='datetime',timezone='Asia/Seoul')
        out,_=validate_mapping(df,m); self.assertTrue(out.t[0].endswith('+09:00'))
        m['t']['timezone']=''
        with self.assertRaisesRegex(ValueError,'timezone_required'): validate_mapping(df,m)
        m=default_mapping(df); m['group']['allowed']='b'
        with self.assertRaisesRegex(ValueError,'outside_allowed'): validate_mapping(df,m)
    def test_duplicate_roles_rejected(self):
        df=pd.DataFrame({'x':[1],'y':[2]}); m=default_mapping(df)
        for rule in m.values(): rule['role']='participant_id'
        with self.assertRaisesRegex(ValueError,'duplicate_role'): validate_mapping(df,m)
