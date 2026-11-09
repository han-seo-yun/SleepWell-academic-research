"""Verify real saved predictions through the model comparison UI without rerunning models."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import tempfile
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

import matplotlib
from matplotlib.figure import Figure

from researchbench.academic_app import AcademicApp
from researchbench.academic_store import AcademicProject, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source_bytes = args.run.read_bytes()
    record = json.loads(source_bytes)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        project = AcademicProject.create(Path(temporary) / 'comparison.swa')
        project.save_run(record)
        root = tk.Tk()
        root.withdraw()
        app = AcademicApp(root, project, language='ko', preferences=False)
        try:
            app.selected_run = record['id']
            app.navigate('experiments')
            assert len(app.comparison_tabs.tabs()) == 8
            data = app.simple_comparison_data
            expected = {row['model_id']: row for row in record['result']['summary']}
            for row in data['rows']:
                assert abs(row['mae'] - expected[row['model_id']]['onset_mae_min']) < 1e-9
                assert abs(row['rmse'] - expected[row['model_id']]['onset_rmse_min']) < 1e-9
            for kind in ('mae', 'rmse', 'bias', 'tolerance', 'predictions', 'errors', 'participants'):
                app.comparison_tabs.select(app.comparison_frames[kind])
                app.refresh_simple_result()
                assert isinstance(app.figure, Figure)
                app.figure.savefig(output / f'{kind}.png', dpi=140)
                app.figure.savefig(output / f'{kind}.svg')
                if kind == 'tolerance':
                    assert app.figure.axes[0].get_xlim() == (0.0, 105.0)
            app.comparison_tabs.select(app.comparison_frames['classification'])
            app.refresh_simple_result()
            assert app.figure is None
            assert 'disabled' in app.simple_chart_export_button.state()
            with patch('researchbench.simple_workflow.filedialog.asksaveasfilename', return_value=str(output / 'comparison_metrics.csv')):
                app.export_simple_comparison_table()
            assert args.run.read_bytes() == source_bytes
            write_json(output / 'VERIFICATION.json', {
                'run_id': record['id'], 'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
                'python': platform.python_version(), 'matplotlib': matplotlib.__version__,
                'backend': 'Matplotlib Figure / TkAgg; PNG and SVG via Figure.savefig',
                'metric_source': 'saved real predictions; no model rerun or data changes',
                'tabs': list(app.comparison_frames), 'model_ids': data['model_ids'],
                'n_nights': data['n_nights'], 'n_participants': data['n_participants'],
                'metrics': data['rows'], 'excluded_counts': data['excluded_counts'],
                'preserved_engine_mae_rmse': 'all models matched within 1e-9',
                'classification': 'not applicable; explanation only; no fabricated curves',
            })
            print(json.dumps({'models':len(data['rows']), 'common_nights':data['n_nights'],
                              'participants':data['n_participants'], 'tabs':len(app.comparison_frames)}, ensure_ascii=False))
        finally:
            app.close()
            app = root = None
            gc.collect()


if __name__ == '__main__':
    main()
