"""Run fixed-procedure participant-grouped CMI/MESA revalidation in Workbench."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from researchbench.academic_jobs import AcademicQueue
from researchbench.academic_plotting import export_academic_tables, export_figure, make_academic_figure
from researchbench.academic_store import AcademicProject


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_config(
    database: Path, participant_audit: Path, output: Path, *, seed: int,
    bootstrap_replicates: int, cv_repeats: int, cv_folds: int, max_seconds: float,
    untouched_holdout: Path | None = None,
) -> dict:
    database = database.resolve()
    participant_audit = participant_audit.resolve()
    output = output.resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    if not participant_audit.is_file():
        raise FileNotFoundError(participant_audit)
    if output.exists():
        raise FileExistsError(f"Output path already exists: {output}")
    if untouched_holdout is not None:
        untouched_holdout = untouched_holdout.resolve()
        if not untouched_holdout.is_file():
            raise FileNotFoundError(untouched_holdout)
    if not 100 <= int(bootstrap_replicates) <= 20000:
        raise ValueError("bootstrap_replicates must be between 100 and 20000")
    if not 1 <= int(cv_repeats) <= 5:
        raise ValueError("cv_repeats must be between 1 and 5")
    if not 2 <= int(cv_folds) <= 5:
        raise ValueError("cv_folds must be between 2 and 5")
    if isinstance(max_seconds, bool) or not math.isfinite(float(max_seconds)) or not 1 <= float(max_seconds) <= 86400:
        raise ValueError("max_seconds must be between 1 and 86400")
    config = {
        "mode": "participant_revalidation",
        "name": "CMI·MESA 고정 절차 참여자 교차검증",
        "database_path": str(database),
        "participant_audit_path": str(participant_audit),
        "participant_audit_sha256": sha256(participant_audit),
        "output_dir": str(output),
        "seed": int(seed),
        "bootstrap_replicates": int(bootstrap_replicates),
        "cv_repeats": int(cv_repeats),
        "cv_folds": int(cv_folds),
        "max_seconds": float(max_seconds),
        "data_hash": sha256(database),
        "source_name": database.name,
        "input_synthetic": False,
    }
    if untouched_holdout is not None:
        config.update(
            untouched_holdout_path=str(untouched_holdout),
            untouched_holdout_sha256=sha256(untouched_holdout),
        )
    return config


def run(
    database: Path, participant_audit: Path, output: Path, project_path: Path, *,
    seed: int = 20261106, bootstrap_replicates: int = 5000,
    cv_repeats: int = 2, cv_folds: int = 5, max_seconds: float = 14400,
    untouched_holdout: Path | None = None,
) -> dict:
    project_path = project_path.resolve()
    if project_path.exists():
        raise FileExistsError(f"Project path already exists: {project_path}")
    config = make_config(
        database, participant_audit, output, seed=seed,
        bootstrap_replicates=bootstrap_replicates, cv_repeats=cv_repeats,
        cv_folds=cv_folds, max_seconds=max_seconds, untouched_holdout=untouched_holdout,
    )
    # The lock is written before the queue can start any fit.
    from researchbench.participant_revalidation import prepare_revalidation
    prepare_revalidation(config)
    project = AcademicProject.create(project_path, "CMI·MESA 고정 절차 참여자 교차검증")
    queue = AcademicQueue(project)
    run_id = queue.submit(config)
    last_progress = None
    try:
        while queue.busy:
            queue.poll()
            progress = (queue.active or {}).get("record", {}).get("progress")
            if progress is not None and progress != last_progress:
                print(json.dumps(progress, ensure_ascii=False), flush=True)
                last_progress = progress
            time.sleep(.1)
    except KeyboardInterrupt:
        queue.cancel(run_id, "keyboard_interrupt")
        while queue.busy:
            queue.poll()
            time.sleep(.1)
        raise
    finally:
        queue.close()
    record = next(row for row in project.runs() if row["id"] == run_id)
    if record["status"] != "completed":
        raise RuntimeError(
            f"Participant revalidation {record['status']}: "
            f"{record.get('error', 'no error message')}"
        )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--participant-audit", type=Path, required=True)
    parser.add_argument("--untouched-holdout", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261106)
    parser.add_argument("--bootstrap-replicates", type=int, default=5000)
    parser.add_argument("--cv-repeats", type=int, default=2)
    parser.add_argument("--cv-folds", type=int, default=5)
    parser.add_argument("--max-seconds", type=float, default=14400)
    parser.add_argument("--export-xlsx", type=Path)
    parser.add_argument("--export-figure", type=Path)
    args = parser.parse_args()
    try:
        record = run(
            args.database, args.participant_audit, args.output, args.project,
            seed=args.seed, bootstrap_replicates=args.bootstrap_replicates,
            cv_repeats=args.cv_repeats, cv_folds=args.cv_folds,
            max_seconds=args.max_seconds, untouched_holdout=args.untouched_holdout,
        )
        if args.export_xlsx:
            export_academic_tables(record["result"], args.export_xlsx, lang="ko")
        if args.export_figure:
            figure = make_academic_figure(
                record["result"], kind="comparison", metric="joint_mae_min", lang="ko",
            )
            export_figure(figure, args.export_figure, dpi=180)
    except KeyboardInterrupt:
        print("Participant revalidation cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Participant revalidation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "project": str(args.project.resolve()),
        "output": str(args.output.resolve()),
        "run_id": record["id"],
        "status": record["status"],
        "xlsx": str(args.export_xlsx.resolve()) if args.export_xlsx else None,
        "figure": str(args.export_figure.resolve()) if args.export_figure else None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
