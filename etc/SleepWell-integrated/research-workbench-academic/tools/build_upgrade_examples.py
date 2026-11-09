"""Build isolated, executable upgrade examples from bundled synthetic fixtures.

The builder never edits an existing project. Its output is explicitly labelled as
synthetic software-validation material and must not be interpreted as external or
prospective validation evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.end_to_end import run_case  # noqa: E402
from researchbench.academic_jobs import execute  # noqa: E402
from researchbench.academic_store import AcademicProject, timestamp  # noqa: E402
from researchbench.evidence_library import EvidenceLibrary  # noqa: E402
from researchbench.reporting import ReportService  # noqa: E402
from researchbench.result_adapter import load_runs  # noqa: E402


SCHEMA = "researchbench.upgrade-examples.v1"


def _prepare_output(output: Path, replace: bool) -> Path:
    output = output.expanduser().resolve()
    if output == ROOT or output == ROOT.parent or output == Path(output.anchor):
        raise ValueError("refusing_broad_output_path")
    if output.exists() and any(output.iterdir()):
        marker = output / "manifest.json"
        if not replace:
            raise FileExistsError(f"upgrade_example_directory_not_empty:{output}")
        if not marker.is_file():
            raise ValueError("replace_requires_existing_upgrade_example_manifest")
        manifest = json.loads(marker.read_text(encoding="utf-8"))
        if manifest.get("schema") != SCHEMA:
            raise ValueError("replace_manifest_schema_mismatch")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    return output


def _run_sleepwell(project: AcademicProject, config: dict) -> dict:
    record = project.new_record(config)
    started = time.monotonic()
    result = execute(config, project.path)
    record.update(
        status="completed",
        stage="completed",
        duration=time.monotonic() - started,
        finished=timestamp(),
        result=result,
    )
    project.save_run(record)
    return record


def _build_academic(output: Path) -> tuple[Path, list[dict]]:
    project_path = output / "demo.swa"
    project = AcademicProject.create(project_path, "SleepWell 고도화 합성 시연")
    sample = ROOT / "samples" / "sleepwell_50days.csv"
    snapshot = project.import_data(sample, synthetic=True)
    base = {
        "input_mode": "real",
        "input_snapshot": snapshot["snapshot"],
        "data_hash": snapshot["sha256"],
        "source_name": snapshot["source_name"],
        "input_synthetic": True,
        "cohorts": ["cmi", "mesa"],
        "max_seconds": 600,
    }
    retrospective = _run_sleepwell(
        project,
        {**base, "mode": "retrospective", "name": "합성 표본 후향 재생"},
    )
    personalization = _run_sleepwell(
        project,
        {**base, "mode": "personalization", "name": "합성 표본 개인화 재생"},
    )
    project.state.setdefault("ui", {}).update(
        {
            "page": "reports",
            "selected_run": personalization["id"],
            "vars": {"workspace": "knowledge", "knowledge_report_template": "research"},
            "knowledge": {"drafts": {}, "citations": []},
        }
    )
    project.save()
    return project_path, [retrospective, personalization]


def _build_general(output: Path) -> list[Path]:
    general = output / "general"
    general.mkdir()
    run_case(general, "classification")
    run_case(general, "regression")
    return [general / "classification.rwb", general / "regression.rwb"]


def _import_fixture_evidence(project_path: Path) -> tuple[list[dict], list[dict]]:
    fixture_path = ROOT / "evaluation" / "rag_fixture_v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    imported = []
    with EvidenceLibrary(project_path) as library:
        for item in fixture["documents"]:
            if item["key"] not in {"ko_methods", "en_methods"}:
                continue
            source = library.import_document(fixture_path.parent / item["file"], item["metadata"])
            imported.append(source)
        searches = [
            library.search("반복 측정 독립 단위 최종 테스트 누수", lang="ko", domain="ml_validation"),
            library.search("repeated measures independent unit final test leakage", lang="en", domain="ml_validation"),
        ]
        citations = []
        for result in searches:
            if result["status"] != "ok" or not result["hits"]:
                raise RuntimeError(f"fixture_retrieval_failed:{result['search_id']}")
            hit = dict(result["hits"][0])
            hit["search_id"] = result["search_id"]
            citations.append(hit)
    return imported, citations


def _build_reports(project_path: Path, output: Path, citations: list[dict]) -> list[dict]:
    views = load_runs(project_path)
    if len(views) < 2:
        raise RuntimeError("expected_two_completed_sleepwell_runs")
    summaries: list[dict] = []
    exports_root = output / "report_exports"
    exports_root.mkdir()
    questions = {
        "ko": "합성 SleepWell 표본에서 후향·개인화 재생 결과는 어떻게 기록되는가?",
        "en": "How are retrospective and personalization replays recorded for the synthetic SleepWell sample?",
    }
    with EvidenceLibrary(project_path) as library:
        service = ReportService(project_path, library)
        for language in ("ko", "en"):
            selected_citations = [
                item for item in citations
                if ("반복" in item.get("text", "")) == (language == "ko")
            ] or citations
            for template in ("research", "industrial", "model_card", "comparison"):
                context = {
                    "purpose": questions[language],
                    "data_description": (
                        "번들 합성 CSV의 소프트웨어 동작 검증" if language == "ko"
                        else "Software behavior validation using the bundled synthetic CSV"
                    ),
                    "limitations": (
                        "합성 예제이며 외부 타당성 또는 실제 전향 검증 근거가 아니다."
                        if language == "ko"
                        else "This synthetic example is not evidence of external validity or actual prospective validation."
                    ),
                    "acceptance_predeclared": template == "industrial",
                    "acceptance_criteria": (
                        "실행 해시·수치 참조·인용 위치 자동 검사에 오류가 없어야 한다."
                        if language == "ko"
                        else "Automatic checks must find no run-hash, numeric-reference, or citation-location errors."
                    ) if template == "industrial" else None,
                }
                draft = service.create(
                    views,
                    template=template,
                    lang=language,
                    question=questions[language],
                    context=context,
                    citations=selected_citations,
                )
                checked = service.apply_check(draft["report_id"])
                if checked["automatic_check"]["state"] != "automatic_check_passed":
                    raise RuntimeError(f"example_report_check_failed:{checked['report_id']}")
                paths = service.export(
                    checked["report_id"],
                    exports_root / f"{template}_{language}",
                    mode="checked",
                )
                summaries.append(
                    {
                        "template": template,
                        "language": language,
                        "draft_report_id": draft["report_id"],
                        "checked_report_id": checked["report_id"],
                        "artifact_sha256": checked["artifact_sha256"],
                        "package": paths["package"].relative_to(output).as_posix(),
                    }
                )
    return summaries


def _manifest(output: Path, academic_runs: list[dict], general_projects: list[Path], sources: list[dict], reports: list[dict]) -> dict:
    files = []
    for path in sorted(item for item in output.rglob("*") if item.is_file() and item.name != "manifest.json"):
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "schema": SCHEMA,
        "generated_at": timestamp(),
        "generated_from_synthetic_samples": True,
        "validation_scope": "software_behavior_only",
        "not_evidence_of": ["external_validity", "clinical_performance", "actual_prospective_validation"],
        "academic_project": "demo.swa",
        "academic_runs": [
            {"run_id": item["id"], "mode": item["config"]["mode"], "status": item["status"]}
            for item in academic_runs
        ],
        "general_projects": [path.relative_to(output).as_posix() for path in general_projects],
        "fixture_sources": [
            {
                "source_id": item["source_id"], "title": item["title"], "license": item.get("license"),
                "version": item.get("version"), "original_sha256": item.get("original_sha256"),
            }
            for item in sources
        ],
        "reports": reports,
        "files": files,
    }


def build(output: Path, *, replace: bool = False) -> Path:
    output = _prepare_output(output, replace)
    project_path, academic_runs = _build_academic(output)
    general_projects = _build_general(output)
    sources, citations = _import_fixture_evidence(project_path)
    reports = _build_reports(project_path, output, citations)
    manifest = _manifest(output, academic_runs, general_projects, sources, reports)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build synthetic, executable research-platform upgrade examples")
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables" / "upgrade_examples")
    parser.add_argument("--replace", action="store_true", help="replace only a prior output with this builder's manifest")
    arguments = parser.parse_args()
    destination = build(arguments.output, replace=arguments.replace)
    print(json.dumps({"status": "complete", "output": str(destination)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
