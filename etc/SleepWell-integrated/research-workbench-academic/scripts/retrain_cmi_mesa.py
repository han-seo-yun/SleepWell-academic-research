"""Run pooled CMI/MESA retraining through the Academic Workbench queue."""
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
from researchbench.academic_store import AcademicProject


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_config(
    database: Path, output: Path, *, seed: int, bootstrap_replicates: int,
    max_seconds: float,
) -> dict:
    database = database.resolve()
    output = output.resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    if output.exists():
        raise FileExistsError(f"Output path already exists: {output}")
    if not 100 <= bootstrap_replicates <= 20000:
        raise ValueError("bootstrap_replicates must be between 100 and 20000")
    if isinstance(max_seconds, bool) or not math.isfinite(float(max_seconds)) or not 1 <= float(max_seconds) <= 86400:
        raise ValueError("max_seconds must be between 1 and 86400")
    return {
        "mode": "pooled_retraining",
        "name": "CMI·MESA 통합 재학습",
        "database_path": str(database),
        "output_dir": str(output),
        "seed": int(seed),
        "bootstrap_replicates": int(bootstrap_replicates),
        "max_seconds": float(max_seconds),
        "data_hash": sha256(database),
        "source_name": database.name,
        "input_synthetic": False,
    }


def run(
    database: Path, output: Path, project_path: Path, *, seed: int = 20261104,
    bootstrap_replicates: int = 5000, max_seconds: float = 7200,
) -> dict:
    project_path = project_path.resolve()
    if project_path.exists():
        raise FileExistsError(f"Project path already exists: {project_path}")
    config = make_config(
        database, output, seed=seed, bootstrap_replicates=bootstrap_replicates,
        max_seconds=max_seconds,
    )
    project = AcademicProject.create(project_path, "CMI·MESA 통합 재학습")
    queue = AcademicQueue(project)
    run_id = queue.submit(config)
    last_progress = None
    try:
        while queue.busy:
            queue.poll()
            active = queue.active
            progress = (active or {}).get("record", {}).get("progress")
            if progress is not None and progress != last_progress:
                print(json.dumps(progress, ensure_ascii=False), flush=True)
                last_progress = progress
            time.sleep(0.1)
    except KeyboardInterrupt:
        queue.cancel(run_id, "keyboard_interrupt")
        while queue.busy:
            queue.poll()
            time.sleep(0.1)
        raise
    finally:
        queue.close()
    record = next(row for row in project.runs() if row["id"] == run_id)
    if record["status"] != "completed":
        raise RuntimeError(
            f"Retraining {record['status']}: {record.get('error', 'no error message')}"
        )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--bootstrap-replicates", type=int, default=5000)
    parser.add_argument("--max-seconds", type=float, default=7200)
    args = parser.parse_args()
    try:
        record = run(
            args.database, args.output, args.project, seed=args.seed,
            bootstrap_replicates=args.bootstrap_replicates,
            max_seconds=args.max_seconds,
        )
    except KeyboardInterrupt:
        print("Retraining cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Retraining failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "project": str(args.project.resolve()),
        "output": str(args.output.resolve()),
        "run_id": record["id"],
        "status": record["status"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
