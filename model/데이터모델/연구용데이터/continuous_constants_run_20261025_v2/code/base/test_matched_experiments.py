import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from unittest.mock import patch
import matched_experiments as m

def fixture():
    rows=[]
    for person in range(4):
        for night in range(16):
            cutoff=pd.Timestamp('2026-01-01 20:00',tz='Asia/Seoul')+pd.Timedelta(days=night)
            bed=cutoff+pd.Timedelta(minutes=120+person*10+night%3*20)
            wake=bed+pd.Timedelta(hours=7+person*.1)
            rows.append(dict(row_id=len(rows),subject_id=f'p{person}',cohort='cmi',split='train',
                cutoff=cutoff,bed=bed,wake=wake,observed_at=wake,bed_offset=(bed-cutoff).total_seconds()/60,
                wake_offset=(wake-cutoff).total_seconds()/60,age=20+person,sex='female' if person%2 else 'male'))
    return pd.DataFrame(rows)

class ExperimentContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame=fixture();cls.prior=m.prior_from_train(cls.frame)
        cls.x,cls.states=m.replay(cls.frame,cls.prior)
    def test_matched_common_features(self):
        for rep in m.REPS:
            d=m.encode(self.frame,self.x,self.states,rep,'data')
            h=m.encode(self.frame,self.x,self.states,rep,'hybrid')
            pd.testing.assert_frame_equal(d,h[d.columns])
            self.assertFalse(set(d.columns)&{'row_id','subject_id','bed_offset','wake_offset','cutoff'})
        self.assertEqual(m.encode(self.frame,self.x,self.states,'raw_absolute','data').shape[1],33)
        self.assertEqual(m.encode(self.frame,self.x,self.states,'raw_absolute','hybrid').shape[1],39)
    def test_future_target_cannot_change_earlier_replayed_features(self):
        altered=self.frame.copy();mask=(altered.subject_id=='p0')&(altered.cutoff>='2026-01-10')
        altered.loc[mask,'bed']+=pd.Timedelta(hours=2)
        altered.loc[mask,'wake']+=pd.Timedelta(hours=2)
        altered.loc[mask,'observed_at']+=pd.Timedelta(hours=2)
        x,_=m.replay(altered,self.prior)
        earlier=self.frame.cutoff<'2026-01-10'
        pd.testing.assert_frame_equal(self.x.loc[earlier],x.loc[earlier])
    def test_current_target_not_encoded(self):
        altered=self.frame.copy();altered['bed_offset']=999.;altered['wake_offset']=1440.
        for rep in m.REPS:
            pd.testing.assert_frame_equal(m.encode(self.frame,self.x,self.states,rep,'hybrid'),
                                          m.encode(altered,self.x,self.states,rep,'hybrid'))
    def test_late_observation_unavailable(self):
        sub=self.frame[self.frame.subject_id=='p0'].copy().reset_index(drop=True)
        sub.loc[0,'observed_at']=sub.cutoff.max()+pd.Timedelta(days=1)
        x,_=m.replay(sub,self.prior)
        self.assertEqual(x.loc[1,'history_count_7'],0)
    def test_prior_uses_only_training_subjects(self):
        altered=self.frame.copy();mask=altered.subject_id=='p3';altered.loc[mask,'split']='validation'
        expected=m.prior_from_train(altered)
        altered.loc[mask,['bed_offset','wake_offset']]=1e9
        self.assertEqual(expected,m.prior_from_train(altered))
    def test_equal_subject_metric(self):
        f=pd.DataFrame({'subject_id':['a','a','b'],'bed_offset':[0.,0,0],'wake_offset':[40.,40,40]})
        pred=np.array([[0,40],[0,40],[20,60]])
        self.assertEqual(m.metrics(f,pred)['joint_mae_min'],10)
        self.assertNotEqual(m.metrics(f,pred)['joint_mae_min'],20/3)
    def test_identical_paired_has_zero(self):
        p=self.frame[m.TARGETS].to_numpy(float)
        r=m.paired(self.frame,p,p,replicates=100)
        self.assertEqual(r['improvement_min'],0)
        self.assertEqual(r['ci95_min'],[0,0])
    def test_prediction_restores_target_offset_and_projects(self):
        class Zero:
            def predict(self,x):return np.zeros((len(x),2))
        ref=np.array([[-50,0],[2000,3000],[100,400]])
        actual=m.predict([Zero()],pd.DataFrame({'x':[1,2,3]}),ref)
        np.testing.assert_array_equal(actual,[[0,30],[960,1440],[100,400]])
    def test_all_learners_and_serialization(self):
        configs=json.loads((Path(__file__).parent/'protocol.json').read_text('utf8'))['candidates']
        z=m.encode(self.frame,self.x,self.states,'clock_quality_residual','hybrid')
        ref=m.reference(self.x,self.prior,'clock_quality_residual')
        for name in ['ridge100','hgb7','et5','mlp32']:
            c=dict(configs[name]);c.update(epochs=2,iterations=2,trees=3)
            model,info=m.fit_one(c,self.frame,z,ref,m.SEED)
            pred=m.predict([model],z,ref)
            self.assertTrue(np.isfinite(pred).all())
            self.assertEqual(pred.shape,(64,2))
            self.assertEqual(model['scale'].n_samples_seen_,64)
            before=model['scale'].mean_.copy()
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'model.joblib';joblib.dump(model,path)
                np.testing.assert_allclose(pred,m.predict([joblib.load(path)],z,ref))
            m.predict([model],z*10,ref)
            np.testing.assert_array_equal(before,model['scale'].mean_)
    def test_calibration_lock_is_serializable_and_no_test_needed(self):
        protocol=json.loads((Path(__file__).parent/'protocol.json').read_text('utf8'))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'cmi';out.mkdir();(out/'models').mkdir()
            m.dump(root/'protocol.json',protocol)
            m.dump(out/'train_selection.json',{'primary_pair':'test'})
            frame=self.frame.copy();frame['split']='validation'
            frame.to_parquet(out/'validation_rows_private.parquet',index=False)
            pred=frame[m.TARGETS].to_numpy(float)-20
            np.savez(out/'validation_predictions_private.npz',test__data=pred,test__hybrid=pred)
            m.calibrate_phase(root,'cmi',protocol)
            lock=json.loads((out/'calibration_lock.json').read_text('utf8'))
            self.assertFalse(lock['test_scored'])
            self.assertEqual(len(lock['policies']),7)
            with self.assertRaisesRegex(ValueError,'already exists'):
                m.calibrate_phase(root,'cmi',protocol)
            (out/'calibration_lock.json').unlink()
            (out/'test_started.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Cannot recalibrate'):
                m.calibrate_phase(root,'cmi',protocol)
    def test_test_gate_detects_all_frozen_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m.dump(root/'protocol.json',{})
            m.dump(root/'source_manifest.json',{'protocol_sha256':m.sha(root/'protocol.json'),'sources':{}})
            for c in ['cmi','mesa']:
                out=root/c;out.mkdir();(out/'models').mkdir()
                m.dump(out/'train_selection.json',{})
                (out/'models'/'model.joblib').write_bytes(b'locked')
                lock={'protocol_sha256':m.sha(root/'protocol.json'),
                    'train_selection_sha256':m.sha(out/'train_selection.json'),
                    'personalization_source_sha256':m.sha(m.HERE/'personalization_ablation.py'),
                    'experiment_source_sha256':m.sha(Path(m.__file__)),
                    'model_hashes':{'model.joblib':m.sha(out/'models'/'model.joblib')}}
                m.dump(out/'calibration_lock.json',lock)
            m.verify_test_locks(root)
            for relative in ['protocol.json','mesa/train_selection.json','cmi/models/model.joblib']:
                p=root/relative;original=p.read_bytes();p.write_bytes(original+b' ')
                with self.assertRaises(ValueError):m.verify_test_locks(root)
                p.write_bytes(original)
            lockpath=root/'mesa/calibration_lock.json';original=lockpath.read_bytes();lockpath.unlink()
            with self.assertRaises(ValueError):m.verify_test_locks(root)
            lockpath.write_bytes(original)
            original_sha=m.sha
            def changed_sha(p):
                return 'tampered' if str(p).endswith('personalization_ablation.py') else original_sha(p)
            with patch.object(m,'sha',changed_sha):
                with self.assertRaises(ValueError):m.verify_test_locks(root)

if __name__=='__main__':unittest.main()
