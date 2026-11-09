import copy
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from matplotlib.figure import Figure

from researchbench.academic_app import AcademicApp
from researchbench.academic_store import AcademicProject


class ComparisonUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / 'comparison.swa')
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AcademicApp(self.root, self.project, language='ko', preferences=False)
        record = self.project.new_record({'mode': 'personalization', 'name': 'Comparison'})
        predictions = []
        for model, error in [('cmi.data', 10), ('mesa.data', 40)]:
            for person, cutoff in [('p1', 'night1'), ('p1', 'night2'), ('p2', 'night1')]:
                predictions.append(dict(model_id=model, participant_id=person, cutoff=cutoff,
                    actual_onset_offset_min=180, onset_offset_min=180 + error,
                    actual_wake_offset_min=660, wake_offset_min=660 + error * 2,
                    actual_interval_min=480, interval_min=480 + error))
        record.update(status='completed', result={'mode': 'personalization',
            'summary': [{'model_id': model, 'n_nights': 3, 'onset_mae_min': error,
                         'wake_mae_min': error * 2, 'joint_mae_min': error * 1.5}
                        for model, error in [('cmi.data', 10), ('mesa.data', 40)]],
            'raw': {'predictions': predictions}})
        self.project.save_run(record)
        self.record = copy.deepcopy(record)
        self.app.selected_run = record['id']
        self.app.navigate('experiments')

    def tearDown(self):
        self.app.close()
        self.app = self.root = None
        gc.collect()
        self.temp.cleanup()

    def test_tabs_use_matplotlib_and_classification_is_explanation_only(self):
        self.assertEqual(len(self.app.comparison_tabs.tabs()), 8)
        for kind in ('mae', 'rmse', 'bias', 'tolerance', 'predictions', 'errors', 'participants'):
            self.app.comparison_tabs.select(self.app.comparison_frames[kind])
            self.app.refresh_simple_result()
            self.assertIsInstance(self.app.figure, Figure)
            self.assertEqual(len(self.app.simple_comparison_data['rows']), 2)
        self.app.comparison_tabs.select(self.app.comparison_frames['classification'])
        self.app.refresh_simple_result()
        self.assertIsNone(self.app.figure)
        self.assertIn('disabled', self.app.simple_chart_export_button.state())
        self.assertEqual(self.project.runs()[0], self.record)

    def test_model_target_weighting_controls_recompute_and_empty_selection_clears(self):
        self.app.simple_model_vars['mesa.data'].set(False)
        self.app.simple_target_combo.current(1)
        self.app.simple_averaging_combo.current(1)
        self.app.refresh_simple_result()
        data = self.app.simple_comparison_data
        self.assertEqual(data['model_ids'], ['cmi.data'])
        self.assertEqual(data['target'], 'wake')
        self.assertEqual(data['averaging'], 'night_equal')
        self.assertAlmostEqual(data['rows'][0]['mae'], 20)
        self.app.set_simple_models(False)
        self.assertEqual(self.app.simple_comparison_data['rows'], [])
        self.assertEqual(len(self.app.simple_metric_tree.get_children()), 0)
        self.assertIn('disabled', self.app.simple_table_export_button.state())

    def test_export_matches_selected_models_and_visible_metric_definition(self):
        self.app.simple_model_vars['mesa.data'].set(False)
        self.app.refresh_simple_result()
        destination = Path(self.temp.name) / 'comparison.csv'
        with patch('researchbench.simple_workflow.filedialog.asksaveasfilename', return_value=str(destination)):
            self.app.export_simple_comparison_table()
        import pandas as pd
        exported = pd.read_csv(destination)
        self.assertEqual(exported.model_id.tolist(), ['cmi.data'])
        self.assertEqual(exported.averaging.tolist(), ['participant_equal'])
        self.assertEqual(exported.run_id.tolist(), [self.record['id']])
        self.assertEqual(exported.tolerance_min.tolist(), [30])
        self.assertEqual(exported.excluded_records.tolist(), [0])
        self.assertEqual(exported.within_tolerance_percent.tolist(), [100])
        self.assertEqual(exported.mae_min.tolist(), [10])

    def test_report_captures_selected_models_and_restores_settings(self):
        self.app.simple_model_vars['mesa.data'].set(False)
        self.app.simple_target_combo.current(1)
        self.app.simple_averaging_combo.current(1)
        self.app.simple_tolerance_combo.set('60')
        self.app.refresh_simple_result()
        self.app.navigate('reports')
        view = self.app.simple_report_sources[self.record['id']]
        selection = self.app._simple_report_comparison_selection(view)
        self.assertEqual(selection['model_ids'], ['cmi.data'])
        self.assertEqual(selection['target'], 'wake')
        self.assertEqual(selection['averaging'], 'night_equal')
        self.assertEqual(selection['tolerance'], 60)
        self.assertIn('±60', self.app.simple_report_comparison_label.cget('text'))
        with patch('researchbench.knowledge_ui.filedialog.asksaveasfilename', return_value=str(Path(self.temp.name) / 'capture.pdf')), \
             patch.object(self.app, '_knowledge_async') as asynchronous:
            self.app._simple_report_create_async()
        self.project.state['comparison_selections'][self.record['id']]['model_ids'] = ['mesa.data']
        with patch.object(self.app, '_simple_report_generate_sync') as generate:
            asynchronous.call_args.args[0]()
        self.assertEqual(generate.call_args.kwargs['comparison_selection']['model_ids'], ['cmi.data'])
        self.project.state['comparison_selections'][self.record['id']] = selection
        self.app.navigate('experiments')
        self.assertFalse(self.app.simple_model_vars['mesa.data'].get())
        self.assertEqual(self.app.simple_target_combo.current(), 1)
        self.assertEqual(self.app.simple_averaging_combo.current(), 1)

    def test_empty_selection_does_not_fall_back_to_all_models_in_report(self):
        self.app.set_simple_models(False)
        self.app.navigate('reports')
        self.assertIn('disabled', self.app.simple_report_create_button.state())
        selection = self.app._simple_report_comparison_selection(self.app.simple_report_sources[self.record['id']])
        self.assertEqual(selection['model_ids'], [])


if __name__ == '__main__':
    unittest.main()
