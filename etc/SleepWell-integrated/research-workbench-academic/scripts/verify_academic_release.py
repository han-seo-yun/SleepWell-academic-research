"""Create and verify the reproducible SleepWell Academic demonstration project.

This script uses only the declared synthetic 50-night sample.  It executes the
same adapter entry point used by the native queue, persists immutable terminal
records, and exports figures/tables from those recorded results.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from researchbench.academic_backend import experiment_catalog  # noqa: E402
from researchbench.academic_jobs import execute  # noqa: E402
from researchbench.academic_plotting import (  # noqa: E402
    export_academic_tables,
    export_figure,
    make_academic_figure,
)
from researchbench.academic_prospective import default_profile  # noqa: E402
from researchbench.academic_store import AcademicProject, timestamp, write_json  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"participant_id", "cutoff", "sleep_start", "sleep_end", "observed_at"}
    if len(rows) != 50 or not rows or not required.issubset(rows[0]):
        raise ValueError("sleepwell_50days.csv must contain exactly 50 canonical rows")
    if len({row["participant_id"] for row in rows}) != 1:
        raise ValueError("the academic playback sample must contain one synthetic participant")
    for row in rows:
        for field in required - {"participant_id"}:
            value = datetime.fromisoformat(row[field])
            if value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
    return rows


def derived_records(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    audits, history = [], []
    for index, row in enumerate(rows, 1):
        onset = datetime.fromisoformat(row["sleep_start"])
        wake = datetime.fromisoformat(row["sleep_end"])
        duration = int(round((wake - onset).total_seconds() / 60))
        audits.append({
            "id": f"synthetic-diary-{index:03d}",
            "day": wake.date().isoformat(),
            "bedtime": row["sleep_start"],
            "wake_time": row["sleep_end"],
            "duration_minutes": duration,
            "source": "diary",
            "modified_at": row["observed_at"],
        })
        history.append({
            "source": "research",
            "sleep_start": row["sleep_start"],
            "wake": row["sleep_end"],
            "observed_at": row["observed_at"],
        })
    return audits, history


def input_config(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_snapshot": item["snapshot"],
        "data_hash": item["sha256"],
        "source_name": item["source_name"],
        "input_synthetic": True,
    }


def verification_configs(item: dict[str, Any], rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    source = input_config(item)
    profile = default_profile()
    profile.update({
        "name": "synthetic-001",
        "created_at": rows[0]["cutoff"],
        "enrollment_id": "academic-verification-synthetic-001",
    })
    audits, history = derived_records(rows)
    as_of = (max(datetime.fromisoformat(row["observed_at"]) for row in rows)
             + timedelta(days=1)).isoformat()
    forecast_cutoff = (datetime.fromisoformat(rows[-1]["cutoff"]) + timedelta(days=1)).isoformat()
    configs: list[dict[str, Any]] = [
        {"mode": "retrospective", "name": "verify::retrospective_50",
         "input_mode": "real", "cohorts": ["cmi", "mesa"], **source},
        {"mode": "personalization", "name": "verify::personalization_50",
         "input_mode": "real", "cohorts": ["cmi", "mesa"], **source},
        {"mode": "prospective_demo", "name": "verify::prospective_demo_50",
         "cohorts": ["cmi", "mesa"], "profile": profile,
         "online_personalization": True, **source},
    ]
    report_configs: dict[str, dict[str, Any]] = {
        "research_diagnostics": {"source_name": "bundled verified artifacts"},
        "measurement_audit": {"records": audits, "as_of": as_of, "profile": profile},
        "target_validity": {"records": audits, "as_of": as_of},
        "phone_baseline": {"outcomes": {}, "as_of": as_of,
                           "source_name": "synthetic verification with no phone outcomes",
                           "input_synthetic": True},
        "population_prior": {"profile": {"age": 30, "sex": "unspecified",
                                             "activity_hours_per_day": 1},
                             "source_name": "bundled population prior"},
    }
    for report_type in (
        "continuous_constants_validation_v2",
        "full_validity_tables_v1",
        "personalization_validation_v1",
        "target_validity_validation_v1",
    ):
        report_configs[report_type] = {"source_name": "bundled stored evaluation artifact"}
    for report_type, extra in report_configs.items():
        relevance = source if report_type in {"measurement_audit", "target_validity"} else {}
        configs.append({"mode": "report", "report_type": report_type,
                        "name": f"verify::report::{report_type}", **extra, **relevance})
    for cohort in ("cmi", "samsung"):
        cohort_profile = dict(profile, link_history=(cohort == "samsung"))
        configs.append({
            "mode": "report", "report_type": "night_model", "cohort": cohort,
            "name": f"verify::report::night_model::{cohort}", "cutoff": forecast_cutoff,
            "profile": cohort_profile, "records": history, "research_context": cohort == "cmi",
            "personalization_policy": "gradual_v1", **source,
        })
    return configs


def open_or_create_project(path: Path) -> AcademicProject:
    if path.exists() and any(path.iterdir()):
        return AcademicProject.open(path)
    return AcademicProject.create(path, "SleepWell 학술제 전용 검증")


def completed_by_name(project: AcademicProject) -> dict[str, dict[str, Any]]:
    records = {}
    for record in project.runs():
        if record.get("name", "").startswith("verify::"):
            records[record["name"]] = record
    return records


def execute_and_store(project: AcademicProject, config: dict[str, Any]) -> dict[str, Any]:
    existing = completed_by_name(project).get(config["name"])
    if existing is not None:
        return existing
    record = project.new_record(config)
    record.update(status="running", stage="running")
    project.save_run(record)
    started = time.perf_counter()
    try:
        result = execute(config, project.path)
        record.update(status="completed", stage="completed", result=result)
    except Exception as error:  # Persist truthful bounded failure evidence.
        record.update(status="failed", stage="failed", error=f"{type(error).__name__}: {error}")
    record["duration"] = round(time.perf_counter() - started, 6)
    record["finished"] = timestamp()
    project.save_run(record)
    return record


def export_record(record: dict[str, Any], output: Path) -> list[Path]:
    if record.get("status") != "completed":
        return []
    key = record["name"].replace("verify::", "").replace("::", "_")
    target = output / key
    target.mkdir(parents=True, exist_ok=True)
    result = record["result"]
    kinds = (["comparison", "predictions", "personalization", "errors", "source_report"]
             if record["config"]["mode"] != "report" else ["source_report"])
    written: list[Path] = []
    for language in ("ko", "en"):
        for kind in kinds:
            figure = make_academic_figure(result, kind=kind, lang=language,
                                           options={"width": 9.2, "height": 5.8, "font_size": 10})
            for suffix in ("png", "pdf", "svg"):
                path = target / f"{key}_{kind}_{language}.{suffix}"
                export_figure(figure, path, dpi=300)
                written.append(path)
            figure.clear()
        for suffix in ("csv", "xlsx"):
            path = target / f"{key}_tables_{language}.{suffix}"
            export_academic_tables(result, path, lang=language)
            written.append(path)
    return written


def summarize(project: AcademicProject, input_path: Path, output: Path,
              exported: list[Path]) -> dict[str, Any]:
    records = [record for record in project.runs() if record.get("name", "").startswith("verify::")]
    runs = []
    for record in records:
        item: dict[str, Any] = {
            "id": record["id"], "name": record["name"], "mode": record["config"]["mode"],
            "report_type": record["config"].get("report_type"), "status": record["status"],
            "duration_seconds": record.get("duration"),
        }
        if record["status"] == "completed":
            result = record["result"]
            raw = result.get("raw") or {}
            item.update({
                "summary": result.get("summary", []),
                "run_summary": raw.get("summary"),
                "prospective_aggregate": raw.get("aggregate"),
                "provenance": result.get("provenance"),
                "warnings": result.get("warnings", []),
            })
        else:
            item["error"] = record.get("error")
        runs.append(item)
    primary_paths = {path.resolve() for path in exported if path.is_file()}
    all_paths = [path for path in output.rglob("*")
                 if path.is_file() and path.name != "verification_summary.json"]
    exported_manifest = []
    for path in sorted(set(all_paths)):
        if path.is_file():
            exported_manifest.append({"path": path.relative_to(ROOT).as_posix(),
                                      "bytes": path.stat().st_size, "sha256": sha256(path),
                                      "primary": path.resolve() in primary_paths})
    summary = {
        "schema_version": 1,
        "generated_at": timestamp(),
        "scope": "synthetic_release_verification_only",
        "project": project.path.relative_to(ROOT).as_posix(),
        "input": {"path": input_path.relative_to(ROOT).as_posix(), "rows": 50,
                  "sha256": sha256(input_path), "declared_synthetic": True},
        "catalog": experiment_catalog(),
        "counts": {"runs": len(runs),
                   "completed": sum(row["status"] == "completed" for row in runs),
                   "failed": sum(row["status"] == "failed" for row in runs),
                   "exported_primary_files": len(primary_paths),
                   "exported_files": len(exported_manifest)},
        "runs": runs,
        "exports": exported_manifest,
    }
    write_json(output / "verification_summary.json", summary)
    return summary


def set_default_result_view(project: AcademicProject, records: list[dict[str, Any]],
                            rows: list[dict[str, str]]) -> None:
    retrospective = next(
        record for record in records
        if record.get("name") == "verify::retrospective_50" and record.get("status") == "completed"
    )
    as_of = (max(datetime.fromisoformat(row["observed_at"]) for row in rows)
             + timedelta(days=1)).isoformat()
    cutoff = (datetime.fromisoformat(rows[-1]["cutoff"]) + timedelta(days=1)).isoformat()
    ui = dict(project.state.get("ui") or {})
    variables = dict(ui.get("vars") or {})
    variables.update({
        "mode": "retrospective", "input_mode": "file", "input_synthetic": "1",
        "metric": "joint_mae_min", "kind": "comparison", "as_of": as_of,
        "cutoff": cutoff, "cohort": "cmi", "record_source": "diary",
    })
    project.state["ui"] = {"vars": variables, "page": "results",
                           "selected_run": retrospective["id"]}
    project.save()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the SleepWell Academic release")
    parser.add_argument("--sample", type=Path, default=ROOT / "samples" / "sleepwell_50days.csv")
    parser.add_argument("--project", type=Path, default=ROOT / "projects" / "SleepWell_Academic.swa")
    parser.add_argument("--output", type=Path, default=ROOT / "example_output_academic")
    arguments = parser.parse_args()
    sample = arguments.sample.resolve()
    rows = load_rows(sample)
    project = open_or_create_project(arguments.project.resolve())
    if project.state.get("input") is None:
        item = project.import_data(sample, synthetic=True)
    else:
        item = project.state["input"]
        if item.get("source_sha256") != sha256(sample) or item.get("synthetic") is not True:
            raise ValueError("existing project input does not match the declared synthetic 50-night sample")
    arguments.output.mkdir(parents=True, exist_ok=True)
    records = [execute_and_store(project, config) for config in verification_configs(item, rows)]
    set_default_result_view(project, records, rows)
    exported: list[Path] = []
    for record in records:
        exported.extend(export_record(record, arguments.output))
    result = summarize(project, sample, arguments.output, exported)
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    for run in result["runs"]:
        print(f"{run['status']:9s} {run['name']}")
    return 0 if result["counts"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
