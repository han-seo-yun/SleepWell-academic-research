"""Integration tests exercise real Tk widgets, without claiming visual review."""
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from researchbench.academic_app import AcademicApp
from researchbench.academic_store import AcademicProject

class PlatformUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=tk.Tk(); self.root.withdraw()
        self.project=AcademicProject.create(Path(self.temp.name)/'one.swa')
        self.project.import_data(Path(__file__).resolve().parents[1]/'samples/sleepwell_50days.csv',synthetic=True)
        rec=self.project.new_record({'mode':'report'})
        rec.update(status='completed',result={'mode':'report','summary':[], 'raw':{'predictions':{'physical':{}}}})
        self.project.save_run(rec)
        self.app=AcademicApp(self.root,self.project,'ko',False)
    def tearDown(self):
        self.root.update_idletasks(); self.app.close(); self.temp.cleanup()
    def test_every_native_page_in_both_languages_preserves_data_and_drafts(self):
        before=self.project.runs()
        for language in ('ko','en'):
            self.app.change_language(language)
            for page in ('data','design','queue','results','evidence','reports','prospective','settings'):
                self.app.navigate(page); self.root.update_idletasks()
        self.assertEqual(before,self.project.runs())
        self.assertEqual(self.root.title(),'SleepWell Research Software')
    def test_project_switch_clears_external_artifact_and_evidence_state(self):
        self.app.var('eval_artifact_path').set('old/project/model')
        self.app.var('optional_ai_endpoint').set('https://old.example')
        self.app.knowledge_citations=[{'source_id':'old'}]
        second=AcademicProject.create(Path(self.temp.name)/'two.swa')
        self.app.attach(second)
        self.assertEqual(self.app.var('eval_artifact_path').get(),'')
        self.assertEqual(self.app.knowledge_citations,[])
    def test_mapping_snapshot_source_and_hash_are_recorded(self):
        self.app.navigate('data')
        # Archived mapping panel is an internal compatibility surface, not a main step.
        self.app.build_data(self.app.scroll.body)
        self.app.mapping_tree.selection_set('participant_id')
        self.root.update()
        self.app.save_mapping()
        mapping=json.loads((self.project.path/'research/column_mapping.json').read_text(encoding='utf-8'))
        self.assertEqual(mapping['source_project'],str(self.project.path))
        self.assertEqual(len(mapping['data_hash']),64)
    def test_project_switch_is_blocked_while_worker_owns_project(self):
        self.app._knowledge_worker_active=True
        with self.assertRaises(ValueError):self.app.ensure_switch()
        self.app._knowledge_worker_active=False

    def test_linked_data_quality_excludes_final_test_rows(self):
        import pandas as pd
        from researchbench.storage import Project
        from researchbench.engine import make_split_plan
        frame=pd.DataFrame({'x':list(range(20)),'y':list(range(20))})
        general=Project.create(Path(self.temp.name)/'general.rwb',frame,'quality')
        plan=make_split_plan(frame,{'task':'regression','target':'y','features':['x'],'folds':2,'seed':42,'test_size':.2,'split':'random'})
        general.state['splits']={plan['id']:plan}; general.save(); general.close()
        self.app.var('workspace').set('tabular'); self.app.var('linked_general').set(str(general.path))
        visible=self.app.data_frame()
        self.assertEqual(set(visible.index),set(plan['train_rows']))
        self.assertTrue(set(visible.index).isdisjoint(plan['test_rows']))

    def test_shared_evidence_workspace_lists_both_engines(self):
        import pandas as pd
        from researchbench.storage import Project
        from researchbench.engine import make_split_plan, run_experiment
        frame=pd.DataFrame({'x':list(range(20)),'y':list(range(20))})
        general=Project.create(Path(self.temp.name)/'general.rwb',frame,'shared')
        config={'id':'general-baseline','task':'regression','target':'y','features':['x'],
                'model':'dummy','folds':2,'seed':42,'test_size':.2,'split':'random',
                'data_hash':general.state['data_hash']}
        plan=make_split_plan(frame,config)
        general.save_experiment(run_experiment(frame,config,plan)); general.close()
        self.app.var('linked_general').set(str(general.path))
        self.app.var('workspace').set('knowledge')
        self.app.navigate('reports')
        self.app.build_reports(self.app.scroll.body)
        self.assertEqual({v['engine'] for v in self.app.knowledge_run_views.values()},
                         {'sleepwell','general'})
