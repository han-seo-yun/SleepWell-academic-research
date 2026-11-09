"""Real frozen pipeline -> adapter -> bilingual verified report integration."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from researchbench.advanced_evaluation import evaluate_external, freeze_general
from researchbench.engine import evaluate_final, make_split_plan
from researchbench.reporting import ReportService
from researchbench.result_adapter import compare, load_runs, verify_run
from researchbench.storage import Project


class ExternalReportTests(unittest.TestCase):
    def test_external_result_is_available_for_verified_bilingual_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rng = np.random.default_rng(620)
            frame = pd.DataFrame({'x': rng.normal(size=48), 'z': rng.normal(size=48)})
            frame['y'] = 3 * frame.x - frame.z + rng.normal(size=48) * .2
            project = Project.create(root / 'general.rwb', frame, 'synthetic fixture')
            config = {'id': 'frozen-source', 'name': 'fixture final', 'task': 'regression',
                'target': 'y', 'target_unit': 'kg', 'features': ['x', 'z'], 'model': 'linear',
                'split': 'random', 'folds': 3, 'test_size': .2, 'seed': 6,
                'imputer': 'median', 'encoding': 'onehot', 'scaling': 'standard',
                'data_hash': project.state['data_hash']}
            plan = make_split_plan(frame, config)
            project.state['splits'] = {plan['id']: plan}
            project.save()
            project.save_experiment(evaluate_final(frame, config, plan))
            project.close()
            original = (project.path / 'experiments' / 'frozen-source.json').read_bytes()
            manifest = freeze_general(project.path, 'frozen-source')
            artifact = project.path / 'research' / 'frozen' / manifest['artifact_id']
            external = pd.DataFrame({'x': rng.normal(size=17), 'z': rng.normal(size=17)})
            external['y'] = 3 * external.x - external.z + rng.normal(size=17) * .2
            external_path = root / 'synthetic_external.csv'
            external.to_csv(external_path, index=False)
            result = evaluate_external(artifact, external_path, 'synthetic frozen check',
                input_designation='declared_synthetic')
            views = load_runs(project.path)
            view = next(v for v in views if v['run_id'] == result['id'])
            self.assertEqual(verify_run(view), result)
            self.assertEqual(view['data_hash'], result['external_sha256'])
            self.assertEqual(view['evaluation']['evaluation_type'], 'external')
            self.assertTrue(view['evaluation']['synthetic'])
            metric = next(m for m in view['metrics'] if m['metric'] == 'mse')
            self.assertEqual(metric['unit'], '(kg)²')
            self.assertEqual(metric['n_rows'], 17)
            self.assertIsNone(metric['n_participants'])
            self.assertIsNone(metric['variation'])
            self.assertEqual(compare(view, view, 'rmse')['delta'], 0)
            source_view = next(v for v in views if v['run_id'] == 'frozen-source')
            self.assertEqual(compare(source_view, view, 'rmse')['status'], 'incomparable')
            service = ReportService(project.path)
            report = service.create([view], lang='ko')
            checked = service.apply_check(report['report_id'])
            self.assertEqual(checked['automatic_check']['errors'], [])
            english = service.new_language(checked['report_id'], 'en')
            self.assertEqual([c['evidence'] for c in checked['claims']],
                             [c['evidence'] for c in english['claims']])
            self.assertEqual(service.check(english)['errors'], [])
            self.assertEqual((project.path / 'experiments' / 'frozen-source.json').read_bytes(), original)
            path = Path(view['source_file'])
            changed = json.loads(path.read_text(encoding='utf-8'))
            changed['metrics']['rmse'] += 1
            path.write_text(json.dumps(changed), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'external_result_hash_mismatch'):
                load_runs(project.path)


if __name__ == '__main__':
    unittest.main()
