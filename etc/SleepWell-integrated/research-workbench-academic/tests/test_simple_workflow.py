import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

import pandas as pd

from researchbench.academic_app import AcademicApp, PAGES
from researchbench.academic_store import AcademicProject


class SimpleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / 'simple.swa')
        self.project.state['ui'] = {'page': 'prospective', 'vars': {'workspace': 'knowledge'}}
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AcademicApp(self.root, self.project, language='ko', preferences=False)

    def tearDown(self):
        self.app.close()
        self.app = self.root = None
        gc.collect()
        self.temp.cleanup()

    def test_only_three_workflow_steps_and_old_state_opens_quality(self):
        self.assertEqual(PAGES, ['data', 'experiments', 'reports'])
        self.assertEqual(self.app.page, 'data')
        self.assertEqual(self.app.var('workspace').get(), 'sleepwell')
        self.assertEqual(len(self.app.workflow_buttons), 3)

    def test_import_stays_on_quality_and_experiment_uses_immutable_slice(self):
        sample = Path(__file__).resolve().parents[1] / 'samples/sleepwell_50days.csv'
        self.app.import_data(sample, synthetic=True)
        original = dict(self.project.state['input'])
        self.assertEqual(self.app.page, 'data')
        self.app.var('experiment_people').set('1')
        self.app.var('experiment_nights').set('12')
        self.app.start_simple_experiment()
        self.assertEqual(self.app.page, 'experiments')
        self.assertEqual(len(self.app.runner.pending), 1)
        config = self.app.runner.pending[0]['config']
        self.assertEqual(config['selection']['rows'], 12)
        self.assertTrue(config['input_synthetic'])
        self.assertEqual(config['selection']['source_sha256'], original['sha256'])
        self.assertEqual(self.project.state['input'], original)
        self.assertNotEqual(config['input_snapshot'], original['snapshot'])
        self.assertEqual(self.app.selected_run, self.app.runner.pending[0]['id'])

    def test_no_data_run_cannot_fall_back_to_synthetic_or_prior_result(self):
        with self.assertRaises(ValueError):
            self.app.start_simple_experiment()
        self.assertFalse(self.app.runner.busy)

    def test_pending_or_failed_run_is_not_replaced_by_older_success(self):
        previous = self.project.new_record({'mode':'personalization', 'name':'Previous'})
        previous.update(status='completed', result={'summary':[{'model_id':'cmi.data', 'joint_mae_min':1}]})
        self.project.save_run(previous)
        failed = self.project.new_record({'mode':'personalization', 'name':'Failed'})
        failed.update(status='failed', error='input_invalid')
        self.project.save_run(failed)
        self.app.selected_run = failed['id']
        self.app.navigate('experiments')
        self.assertEqual(self.app.selected_run, failed['id'])
        self.assertEqual(self.app.simple_result_combo.current(), -1)
        self.assertIsNone(self.app.figure)

    def test_empty_legacy_selection_displays_saved_success(self):
        previous = self.project.new_record({'mode':'personalization', 'name':'Previous'})
        previous.update(status='completed', result={'summary':[{'model_id':'cmi.data', 'joint_mae_min':1}]})
        self.project.save_run(previous)
        self.app.selected_run = ''
        self.app.navigate('experiments')
        self.assertEqual(self.app.selected_run, previous['id'])
        self.assertIsNotNone(self.app.figure)


class ExperimentSliceTests(unittest.TestCase):
    def test_cohort_subject_ids_are_separate_and_train_is_explicit(self):
        from researchbench.simple_workflow import experiment_slice
        rows=[]
        for cohort in ['CMI', 'MESA']:
            for split in ['train', 'test']:
                rows.append(dict(cohort=cohort, subject_id='same', split=split,
                    cutoff='2026-01-01T20:00:00+09:00', bed='2026-01-01T23:00:00+09:00',
                    wake='2026-01-02T07:00:00+09:00', observed_at='2026-01-02T07:00:00+09:00'))
        frame,scope=experiment_slice(pd.DataFrame(rows),people=2,nights=30,cohort='all',split='train')
        self.assertEqual(set(frame.participant_id), {'CMI.same','MESA.same'})
        self.assertEqual(len(frame),2)
        self.assertEqual(scope['split'],'train')
        self.assertEqual(scope['source_rows'],4)
        self.assertEqual(scope['actual_cohort_participants'], {'CMI':1,'MESA':1})
        from researchbench.academic_backend import vendor_module
        vendor_module('research_lab')._validate_payload({'schema_version':1, 'mode':'real',
            'cohorts':['cmi'], 'csv_text':frame.to_csv(index=False)})

    def test_engine_budget_and_missing_schema_reject_instead_of_truncating(self):
        from researchbench.simple_workflow import experiment_slice
        with self.assertRaises(ValueError):
            experiment_slice(pd.DataFrame({'x':[1]}),people=1,nights=30)
        with self.assertRaises(ValueError):
            experiment_slice(pd.DataFrame(),people=10,nights=60)


if __name__ == '__main__':
    unittest.main()
