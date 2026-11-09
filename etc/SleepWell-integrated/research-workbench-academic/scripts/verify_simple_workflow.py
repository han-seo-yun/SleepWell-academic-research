"""Exercise the three-step workflow on existing data in an isolated project."""
from __future__ import annotations

import argparse
import json
import logging
import time
import tkinter as tk
from pathlib import Path

from researchbench.academic_app import AcademicApp
from researchbench.academic_store import AcademicProject, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    output = args.output.resolve()
    project = AcademicProject.create(output / 'workflow_smoke.swa', '간단 작업 흐름 확인')
    root = tk.Tk()
    root.withdraw()
    app = AcademicApp(root, project, language='ko', preferences=False)
    try:
        app.import_data(args.source, synthetic=False)
        quality = app.quality_summary
        app.var('experiment_cohort').set('cmi')
        app.var('experiment_split').set('train')
        app.var('experiment_people').set('3')
        app.var('experiment_nights').set('30')
        app.start_simple_experiment()
        run_id = app.runner.pending[0]['id']
        ticks = 0
        deadline = time.monotonic() + 120
        while app.runner.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.02)
            ticks += 1
        record = next(item for item in project.runs() if item['id'] == run_id)
        assert record['status'] == 'completed', record.get('error', record['status'])
        assert app.selected_run == run_id
        assert len(app.simple_metric_tree.get_children()) == 9
        assert app.figure is not None
        app.figure.savefig(output / 'model_performance.png', dpi=140, bbox_inches='tight')
        app.navigate('reports')
        assert app.var('knowledge_simple_report_source').get() == run_id
        view = app.simple_report_sources[run_id]
        result = app._simple_report_generate_sync(view, '기존 CMI Train 자료의 모델 탐색 실험',
                                                  output / 'SleepWell_실험_RAG_보고서.pdf', 'ko')
        app._simple_report_complete(result)
        report = result['report']
        assert report['automatic_check']['errors'] == []
        assert [item['run_id'] for item in report['run_refs']] == [run_id]
        assert all('제공되지 않음' not in paragraph for section in report['sections'] for paragraph in section['paragraphs'])
        evidence = {'quality': quality, 'run_id': run_id, 'scope': record['config']['selection'],
                    'model_metrics': record['result']['summary'], 'tk_event_loop_ticks': ticks,
                    'report_id': report['report_id'], 'pdf': str(result['path']),
                    'automatic_check': report['automatic_check'], 'llm_used': False,
                    'official_citations': len(result['retrieval']['citations']),
                    'visible_steps': ['data', 'experiments', 'reports'],
                    'native_visual_review': 'not_completed_user_stopped_computer_use'}
        write_json(output / 'VERIFICATION.json', evidence)
        print(json.dumps({key:evidence[key] for key in ['run_id','scope','tk_event_loop_ticks','report_id','pdf','official_citations']}, ensure_ascii=False))
    finally:
        app.close()


if __name__ == '__main__':
    main()
