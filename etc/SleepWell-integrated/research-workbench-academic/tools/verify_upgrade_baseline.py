"""Record or verify the pre-upgrade reproducibility baseline.

This utility is deliberately independent from the desktop UI.  It hashes the
protected engines and immutable saved project artifacts, reruns deterministic
general/SleepWell fixtures, and optionally captures the full unittest suite.

Examples::

    python -X utf8 tools/verify_upgrade_baseline.py --record --run-tests
    python -X utf8 tools/verify_upgrade_baseline.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
OUTPUT = ROOT / "deliverables" / "upgrade_validation" / "baseline"
BASELINE = OUTPUT / "baseline.json"
TEST_LOG = OUTPUT / "unittest_baseline.log"
VOLATILE_RESULT_KEYS = {"duration", "elapsed_ms"}
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def normalized_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalized_result(item) for key, item in value.items()
                if key not in VOLATILE_RESULT_KEYS}
    if isinstance(value, list):
        return [normalized_result(item) for item in value]
    return value


def file_set(files: list[tuple[str, Path]]) -> dict[str, Any]:
    rows = []
    for label, path in sorted(files):
        rows.append({"path": label.replace("\\", "/"), "sha256": sha256_file(path),
                     "size_bytes": path.stat().st_size})
    aggregate = sha256_bytes(stable_json(rows))
    return {"aggregate_sha256": aggregate, "file_count": len(rows), "files": rows}


def source_files(root: Path, prefixes: tuple[str, ...]) -> list[tuple[str, Path]]:
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                "projects", "deliverables", "release", "example_output",
                "example_output_verified"}
    allowed = {".py", ".json", ".md", ".txt", ".toml", ".cmd", ".ps1", ".yml", ".yaml"}
    rows = []
    for prefix in prefixes:
        candidate = root / prefix
        paths = [candidate] if candidate.is_file() else candidate.rglob("*") if candidate.is_dir() else []
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in allowed:
                continue
            relative = path.relative_to(root)
            if any(part in excluded for part in relative.parts):
                continue
            rows.append((relative.as_posix(), path))
    return rows


def protected_sources() -> dict[str, Any]:
    academic_names = [
        "researchbench/engine.py", "researchbench/metrics.py", "researchbench/storage.py",
        "researchbench/academic_backend.py", "researchbench/academic_prospective.py",
        "researchbench/academic_store.py",
    ]
    academic = file_set([(name, ROOT / name) for name in academic_names])

    manifest_path = ROOT / "vendor" / "sleepwell" / "SOURCE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    vendor_files = [("SOURCE_MANIFEST.json", manifest_path)]
    canonical_files = []
    for item in manifest["files"]:
        vendor_files.append((item["bundled_path"], ROOT / "vendor" / "sleepwell" / item["bundled_path"]))
        canonical_files.append((item["source_path"], WORKSPACE / item["source_path"]))

    original_general_root = WORKSPACE / "research_workbench"
    original_general = file_set(source_files(
        original_general_root,
        ("researchbench", "tests", "samples", "README.md", "CONTRACT.md", "VALIDATION.md",
         "SOURCES.md", "requirements.txt", "requirements-dev.txt", "install.ps1", "install.cmd",
         "launch.cmd", "launch_sleepwell_demo.cmd"),
    ))
    return {
        "academic_engine": academic,
        "vendored_sleepwell_exact_copy": file_set(vendor_files),
        "canonical_sleepwell_manifest_sources": file_set(canonical_files),
        "preserved_general_edition_source": original_general,
    }


def existing_project_snapshot() -> dict[str, Any]:
    project = ROOT / "projects" / "SleepWell_Academic.swa"
    state = json.loads((project / "project.json").read_text(encoding="utf-8"))
    input_meta = state.get("input") or {}
    snapshot = project / input_meta["snapshot"] if input_meta.get("snapshot") else None
    run_rows = []
    selected_summaries = []
    for path in sorted((project / "runs").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") != "completed":
            continue
        run_rows.append({"id": record["id"], "file_sha256": sha256_file(path),
                         "record_hash": state.get("run_hashes", {}).get(record["id"])})
        summary = record.get("result", {}).get("summary") or []
        numeric = []
        for row in summary:
            if not isinstance(row, dict):
                continue
            values = {key: value for key, value in row.items()
                      if isinstance(value, (int, float)) and not isinstance(value, bool)}
            if values:
                numeric.append({"identity": row.get("model_id", row.get("name", "")), "values": values})
        if numeric:
            selected_summaries.append({"run_id": record["id"], "mode": record.get("config", {}).get("mode"),
                                       "numeric_summary": numeric})
    return {
        "project": project.relative_to(ROOT).as_posix(),
        "schema": state.get("schema"),
        "input": input_meta,
        "input_snapshot_sha256": sha256_file(snapshot) if snapshot and snapshot.is_file() else None,
        "completed_run_count": len(run_rows),
        "completed_runs_aggregate_sha256": sha256_bytes(stable_json(run_rows)),
        "completed_runs": run_rows,
        "numeric_summaries": selected_summaries,
    }


def general_config(task: str, target: str, digest: str, experiment_id: str) -> dict[str, Any]:
    classification = task == "classification"
    return {
        "id": experiment_id, "name": experiment_id, "task": task, "target": target,
        "features": ["age", "biomarker", "treatment", "site"], "model": "linear",
        "params": {"max_iter": 400}, "imputer": "median", "encoding": "onehot",
        "scaling": "standard", "positive_class": "yes" if classification else None,
        "threshold": 0.5, "average": "macro",
        "class_weight": "balanced" if classification else None,
        "target_unit": None if classification else "score", "target_transform": "none",
        "device": "cpu", "primary_metric": "roc_auc" if classification else "rmse",
        "data_hash": digest, "seed": 73,
    }


def experiment_baselines() -> dict[str, Any]:
    from researchbench.academic_backend import run_research
    from researchbench.data import fingerprint, load_data
    from researchbench.engine import make_split_plan, run_experiment

    results: dict[str, Any] = {}
    general_cases = [
        ("classification", "classification_mixed.csv", "response", "roc_auc"),
        ("regression", "regression_mixed.xlsx", "outcome_score", "rmse"),
    ]
    for task, filename, target, primary in general_cases:
        frame = load_data(ROOT / "samples" / filename)
        digest = fingerprint(frame)
        config = general_config(task, target, digest, f"baseline-{task}")
        plan = make_split_plan(frame, {**config, "split": "random", "test_size": 0.2,
                                       "folds": 3, "seed": 73})
        value = run_experiment(frame, config, plan)
        normalized = normalized_result(value)
        results[f"general_{task}"] = {
            "fixture": f"samples/{filename}", "fixture_sha256": sha256_file(ROOT / "samples" / filename),
            "config": config, "split_id": plan["id"], "primary_metric": primary,
            "summary": value["summary"], "row_scope": value["row_scope"],
            "normalized_result_sha256": sha256_bytes(stable_json(normalized)),
        }

    sample = ROOT / "samples" / "sleepwell_50days.csv"
    for mode in ("retrospective", "personalization"):
        config = {"mode": mode, "input_mode": "real", "csv_file": str(sample),
                  "cohorts": ["cmi", "mesa"]}
        value = run_research(config)
        normalized = normalized_result(value)
        summary = sorted(value["summary"], key=lambda row: row["model_id"])
        results[f"sleepwell_{mode}"] = {
            "fixture": "samples/sleepwell_50days.csv", "fixture_sha256": sha256_file(sample),
            "config": {**config, "csv_file": "samples/sleepwell_50days.csv"},
            "summary": summary,
            "common_nights": value["raw"].get("summary", {}).get("common_nights"),
            "personalization_records": len(value["raw"].get("personalization", [])),
            "normalized_result_sha256": sha256_bytes(stable_json(normalized)),
        }
    return results


def run_tests() -> dict[str, Any]:
    command = [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-X", "utf8", "-m", "unittest",
               "discover", "-s", "tests", "-v"]
    completed = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    TEST_LOG.write_text(completed.stdout, encoding="utf-8")
    return {"command": command, "exit_code": completed.returncode,
            "log": TEST_LOG.relative_to(ROOT).as_posix(), "log_sha256": sha256_file(TEST_LOG)}


def build(include_tests: bool) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "protected_sources": protected_sources(),
        "existing_academic_project": existing_project_snapshot(),
        "deterministic_experiments": experiment_baselines(),
    }
    if include_tests:
        payload["tests"] = run_tests()
    return payload


def compare(expected: Any, actual: Any, path: str = "root") -> list[str]:
    mismatches = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key in {"recorded_at", "tests"}:
                continue
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing")
            else:
                mismatches.extend(compare(expected[key], actual[key], f"{path}.{key}"))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            mismatches.append(f"{path}: length {len(expected)} != {len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            mismatches.extend(compare(left, right, f"{path}[{index}]"))
    elif expected != actual:
        mismatches.append(f"{path}: {expected!r} != {actual!r}")
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--record", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.record:
        payload = build(args.run_tests)
        BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                            encoding="utf-8")
        print(json.dumps({"baseline": str(BASELINE), "sha256": sha256_file(BASELINE),
                          "tests_exit_code": payload.get("tests", {}).get("exit_code")}, ensure_ascii=False))
        return 0 if payload.get("tests", {}).get("exit_code", 0) == 0 else 1
    if not BASELINE.is_file():
        raise FileNotFoundError(f"Record the baseline first: {BASELINE}")
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    actual = build(args.run_tests)
    mismatches = compare(expected, actual)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "baseline": str(BASELINE),
              "passed": not mismatches, "mismatches": mismatches}
    check_path = OUTPUT / "check_latest.json"
    check_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
