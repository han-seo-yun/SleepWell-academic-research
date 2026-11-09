"""Read/restore the synthetic release examples from this checkout's location.

Run with the prepared interpreter in an extracted source package to test source
and relative evidence paths. This does not claim a clean-machine installation.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from researchbench.evidence_library import EvidenceLibrary
from researchbench.reporting import ReportService
from researchbench.result_adapter import load_runs


def verify():
    examples = ROOT / 'deliverables' / 'upgrade_examples'
    manifest = json.loads((examples / 'manifest.json').read_text(encoding='utf-8'))
    academic = examples / manifest['academic_project']
    result = {'schema': 1, 'source_root': str(ROOT),
              'scope': 'source_and_saved_examples_at_recorded_root_with_existing_environment',
              'runs': {'sleepwell': len(load_runs(academic))}, 'reports_checked': 0,
              'experiment_claims_checked': 0, 'literature_claims_checked': 0,
              'semantic_support_review': 'not_performed',
              'denominator_note': 'Claims across 8 template/language versions reuse the same synthetic runs and fixture documents.',
              'errors': []}
    for relative in manifest['general_projects']:
        project = examples / relative
        result['runs'][project.stem] = len(load_runs(project))
    with EvidenceLibrary(academic) as library:
        service = ReportService(academic, library)
        for item in manifest['reports']:
            report = service.get(item['checked_report_id'])
            checked = service.check(report)
            result['reports_checked'] += 1
            result['errors'].extend(checked['errors'])
            for claim in report['claims']:
                if claim['evidence']['kind'] == 'literature':
                    library.get_chunk(claim['evidence']['chunk_id'])
                    result['literature_claims_checked'] += 1
                elif claim['evidence']['kind'] == 'experiment':
                    result['experiment_claims_checked'] += 1
    result['passed'] = not result['errors'] and result['reports_checked'] == 8
    return result


if __name__ == '__main__':
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
