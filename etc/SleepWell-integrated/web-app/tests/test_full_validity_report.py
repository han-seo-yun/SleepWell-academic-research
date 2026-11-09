import copy
import json
from pathlib import Path
import tempfile
import unittest
from full_validity_report import load_full_validity_report


class PublicFullReportTests(unittest.TestCase):
    def read(self,value):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'report.json';path.write_text(json.dumps(value),encoding='utf-8')
            return load_full_validity_report(path)
    def fixture(self):
        return {'schema_version':1,'artifact_type':'sleepwell_full_validity_tables','version':'test',
                'test_status':'retrospective_reused_test','no_service_promotion':True,
                'panels':[{'id':'baselines','title':'Baselines','columns':['Name','MAE'],
                           'rows':[['data',47.31]],'notes':['Aggregate only']}]}
    def test_public_aggregate_contract(self):self.assertTrue(self.read(self.fixture())['available'])
    def test_no_private_fields_nonfinite_or_service_promotion(self):
        values=[]
        value=self.fixture();value['participant_id']='secret';values.append(value)
        value=self.fixture();value['panels'][0]['rows'][0][1]=float('nan');values.append(value)
        value=self.fixture();value['panels'][0]['rows'][0][0]='E:/private/data';values.append(value)
        value=self.fixture();value['no_service_promotion']=False;values.append(value)
        value=self.fixture();value['panels'].append(copy.deepcopy(value['panels'][0]));values.append(value)
        for value in values:self.assertFalse(self.read(value)['available'])
    def test_real_artifact_is_available_and_has_all_panels(self):
        result=load_full_validity_report(Path(__file__).resolve().parents[1]/'models/full_validity_tables_v1.json')
        self.assertTrue(result['available'])
        self.assertEqual(len(result['panels']),12)


if __name__=='__main__':unittest.main()
