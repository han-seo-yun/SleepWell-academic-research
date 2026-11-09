"""Create a guideline-grounded PDF from an exact UI comparison of saved results."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import logging
import tkinter as tk
from pathlib import Path

from pypdf import PdfReader

from researchbench.academic_app import AcademicApp
from researchbench.academic_store import AcademicProject, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    source_bytes = args.run.read_bytes()
    record = json.loads(source_bytes)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = AcademicProject.create(output / 'comparison_report.swa', '선택 모델 비교 보고서 확인')
    snapshot = record.get('config', {}).get('input_snapshot')
    if snapshot:
        source_project = args.run.resolve().parent.parent
        source_input = (source_project / snapshot).resolve()
        copied_input = (project.path / snapshot).resolve()
        assert source_input.is_relative_to(source_project)
        assert copied_input.is_relative_to(project.path.resolve())
        input_bytes = source_input.read_bytes()
        assert hashlib.sha256(input_bytes).hexdigest() == record['config']['data_hash']
        copied_input.parent.mkdir(parents=True, exist_ok=True)
        copied_input.write_bytes(input_bytes)
    project.save_run(record)
    root = tk.Tk()
    root.withdraw()
    app = AcademicApp(root, project, language='ko', preferences=False)
    try:
        app.selected_run = record['id']
        app.navigate('experiments')
        selected = ['cmi.data', 'cmi.hybrid', 'baseline.recent7']
        for model, variable in app.simple_model_vars.items():
            variable.set(model in selected)
        app.simple_target_combo.current(0)
        app.simple_averaging_combo.current(0)
        app.simple_tolerance_combo.set('30')
        app.refresh_simple_result()
        ui_data = app.simple_comparison_data
        app.navigate('reports')
        view = app.simple_report_sources[record['id']]
        selection = app._simple_report_comparison_selection(view)
        assert set(selection['model_ids']) == set(selected)
        result = app._simple_report_generate_sync(
            view, '기존 CMI Train 자료에서 선택한 세 모델의 취침 시각 예측 비교',
            output / 'SleepWell_선택모델_비교_RAG_보고서.pdf', 'ko', comparison_selection=selection)
        report = result['report']
        assert report['template'] == 'model_comparison'
        assert report['automatic_check']['errors'] == []
        assert report['comparison_evidence']['selection'] == selection
        assert report['comparison_evidence']['summary']['rows'] == ui_data['rows']
        assert {claim['evidence']['model_id'] for claim in report['claims']
                if claim['evidence']['kind'] == 'comparison_metric'} == set(selected)
        reader = PdfReader(result['path'])
        text = '\n'.join(page.extract_text() for page in reader.pages)
        assert 'TRIPOD+AI' in text and 'PROBAST+AI' in text
        assert 'mesa.data' not in text and 'cmi.physical_fixed' not in text
        image_count = sum(len(page.images) for page in reader.pages)
        assert image_count >= 7, image_count
        assert args.run.read_bytes() == source_bytes
        write_json(output / 'VERIFICATION.json', {
            'run_id':record['id'], 'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
            'report_id':report['report_id'], 'selection':selection,
            'metric_rows':ui_data['rows'], 'ui_pdf_metrics_match':True,
            'pdf':str(result['path']), 'pages':len(reader.pages), 'embedded_images':image_count,
            'automatic_check':report['automatic_check'],
            'official_sources':result['retrieval']['sources'],
            'llm_used':False, 'external_ai_transfer':False,
        })
        print(json.dumps({'pdf':str(result['path']), 'pages':len(reader.pages), 'images':image_count,
                          'model_ids':selected, 'report_id':report['report_id']}, ensure_ascii=False))
    finally:
        app.close()
        app = root = None
        gc.collect()


if __name__ == '__main__':
    main()
