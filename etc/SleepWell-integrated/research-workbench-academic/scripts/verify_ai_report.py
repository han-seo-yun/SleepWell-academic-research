"""Keyless end-to-end check: actual saved metrics, explicitly simulated AI prose."""
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
from researchbench.ai_connections import load_settings
from researchbench.evidence_library import EvidenceLibrary
from researchbench.report_ai_ui import generate_ai_report
from researchbench.reporting import ReportService
from tests.test_report_ai_ui import FixtureAgentClient


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
    project = AcademicProject.create(output / 'ai_report_verification.swa', 'AI 보고서 모의 통합 검사')
    snapshot = record.get('config', {}).get('input_snapshot')
    if snapshot:
        origin = args.run.resolve().parent.parent
        source_input, copied_input = (origin / snapshot).resolve(), (project.path / snapshot).resolve()
        assert source_input.is_relative_to(origin) and copied_input.is_relative_to(project.path.resolve())
        content = source_input.read_bytes()
        assert hashlib.sha256(content).hexdigest() == record['config']['data_hash']
        copied_input.parent.mkdir(parents=True, exist_ok=True)
        copied_input.write_bytes(content)
    project.save_run(record)
    root = tk.Tk()
    root.withdraw()
    app = AcademicApp(root, project, language='ko', preferences=False)
    try:
        app.selected_run = record['id']
        app.navigate('experiments')
        chosen = ['cmi.data', 'cmi.hybrid', 'baseline.recent7']
        for model, variable in app.simple_model_vars.items():
            variable.set(model in chosen)
        app.simple_target_combo.current(0)
        app.simple_averaging_combo.current(0)
        app.simple_tolerance_combo.set('30')
        app.refresh_simple_result()
        ui_rows = app.simple_comparison_data['rows']
        app.navigate('reports')
        view = app.simple_report_sources[record['id']]
        selection = app._simple_report_comparison_selection(view)
        assert set(selection['model_ids']) == set(chosen)
        client = FixtureAgentClient()
        result = generate_ai_report(project.path, view, selection,
            '저장된 CMI Train 결과의 선택 모델 비교. AI 문장은 통합 검사용 모의 응답입니다.',
            'ko', load_settings(project.path), client=client)
        report = result['report']
        app._report_ai_complete(result)
        preview = app.simple_report_preview.get('1.0', 'end')
        assert '모의' in preview and 'AI 성능 비교 해석' in preview
        assert report['comparison_evidence']['summary']['rows'] == ui_rows
        assert report['automatic_check']['errors'] == []
        assert report['generation']['test_only'] and not report['generation']['llm_used']
        destination = output / 'SleepWell_AI_RAG_통합검사_모의보고서.pdf'
        with EvidenceLibrary(project.path) as library:
            service = ReportService(project.path, library)
            service.export_pdf(report['report_id'], destination, mode='checked')
        reader = PdfReader(destination)
        text = '\n'.join(page.extract_text() for page in reader.pages)
        assert 'TRIPOD+AI' in text and 'PROBAST+AI' in text and '모의' in text
        assert 'mesa.data' not in text
        images = sum(len(page.images) for page in reader.pages)
        assert images == 7
        assert args.run.read_bytes() == source_bytes
        assert len(client.calls) == 3
        write_json(output / 'VERIFICATION.json', {
            'source_sha256': hashlib.sha256(source_bytes).hexdigest(), 'run_id': record['id'],
            'report_id': report['report_id'], 'selection': selection, 'metric_rows': ui_rows,
            'ui_pdf_metrics_match': True, 'pdf': str(destination), 'pages': len(reader.pages),
            'embedded_matplotlib_figures': images, 'automatic_check': report['automatic_check'],
            'official_sources': result['retrieval']['sources'], 'llm_used': False,
            'simulated_agent_calls': len(client.calls), 'external_ai_transfer': False,
            'agent_trace': report['ai_analysis']['trace_relative_path'],
            'agent_trace_sha256': report['ai_analysis']['trace_sha256'],
            'real_api_or_subscription_auth_tested': False,
        })
        print(json.dumps({'pdf': str(destination), 'pages': len(reader.pages), 'images': images,
                          'model_ids': chosen, 'agent_calls': len(client.calls), 'llm_used': False}, ensure_ascii=False))
    finally:
        app.close()
        app = root = None
        gc.collect()


if __name__ == '__main__':
    main()
