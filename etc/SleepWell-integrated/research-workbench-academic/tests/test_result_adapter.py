import copy
import tempfile
import unittest
from pathlib import Path
from researchbench.result_adapter import adapt_run, compare, load_runs, verify_run, model_color
from researchbench.academic_store import AcademicProject

def run(run_id="run1", target="y", split="s1"):
    return {"id":run_id,"status":"completed","config":{"task":"regression","target":target,"model":"linear","data_hash":"data","target_unit":"kg"},
        "result":{"task":"regression","phase":"validation","split_id":split,"summary":{"rmse":{"mean":2.,"std":.2,"n":3}},
        "predictions":[{"row_id":1,"y_true":2,"y_pred":3},{"row_id":2,"y_true":4,"y_pred":5}]}}

class AdapterTests(unittest.TestCase):
    def test_original_single_forecast_dictionary_is_not_a_prediction_table(self):
        rec={'id':'legacy','status':'completed','config':{'mode':'report'},'result':{'mode':'report','raw':{'predictions':{'physical':{'bed':'2026-01-01'}}},'summary':[]}}
        view=adapt_run(rec,'sleepwell')
        self.assertEqual(view['predictions'],[])
        self.assertIsNone(view['evaluation']['scope_hash'])
    def test_incompatible_scope_never_delta(self):
        a=adapt_run(run(),"general")
        for field,value in (("target","other"),("split_id","different")):
            b=copy.deepcopy(a); b["evaluation"][field]=value
            self.assertIsNone(compare(a,b,"rmse")["delta"])
        b=copy.deepcopy(a); b["metrics"][0]["value"]=1
        self.assertEqual(compare(a,b,"rmse")["improvement"],1)
        self.assertFalse(a["metrics"][0]["variation"]["is_confidence_interval"])

    def test_missing_scope_is_reference_only(self):
        a=adapt_run(run(),"general"); a["evaluation"]["scope_hash"]=None
        self.assertEqual(compare(a,a,"rmse")["status"],"reference_only")
        self.assertEqual(model_color("linear"),model_color("linear"))

    def test_source_hash_checked_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=AcademicProject.create(Path(tmp)/"demo.swa")
            rec=p.new_record({"mode":"report"}); rec.update(status="completed",result={"mode":"report","summary":[],"warnings":["stored historical report"]})
            p.save_run(rec)
            view=load_runs(p.path)[0]
            self.assertEqual(view["evaluation"]["calculation"],"historical_reuse")
            self.assertEqual(verify_run(view)["id"],rec["id"])
            forged=copy.deepcopy(view); forged['evaluation']['task']='forged'
            with self.assertRaisesRegex(ValueError,'normalized_view_mismatch'): verify_run(forged)
            Path(view["source_file"]).write_text('{}',encoding="utf-8")
            with self.assertRaises(ValueError): verify_run(view)

    def test_squared_error_uses_squared_units(self):
        rec=run(); rec['result']['summary']['mse']={'mean':4.,'std':.1,'n':3}
        metrics=adapt_run(rec,'general')['metrics']
        self.assertEqual(next(m['unit'] for m in metrics if m['metric']=='mse'),'(kg)²')

if __name__=="__main__": unittest.main()
