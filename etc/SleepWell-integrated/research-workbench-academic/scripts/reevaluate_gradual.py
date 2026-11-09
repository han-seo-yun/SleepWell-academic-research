"""Prepare and run the frozen gradual_v1 reassessment.

Private normalized CSV contract
--------------------------------
Required columns are ``cohort, participant_id, split, row_id, cutoff, bed,
wake, observed_at, bed_offset, wake_offset``. Timestamps must be ISO-8601 with
an offset. Optional ``age, sex, label_source, rare_by_train_mad`` are retained
for aggregate diagnostics. Reassessment inputs also carry six frozen reference
columns named ``legacy_{data,hybrid,physical}_{bed,wake}``; every Test value is
required so the evaluator can prove portable legacy14 replay parity before it
reports gradual_v1.

``--prepare`` requires pandas/pyarrow and reads the original local parquet
files. Evaluation itself is CSV/standard-library portable and uses only the
verified vendored model implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from researchbench.gradual_evaluation import evaluate


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(legacy_root: Path, robust_root: Path, output_csv: Path) -> dict:
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("--prepare requires pandas and pyarrow; use the system Python 3.12 environment") from error
    if output_csv.exists():
        raise FileExistsError(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    source_files = []
    for cohort in ("cmi", "mesa"):
        episodes_path = legacy_root / "analysis" / cohort / "episodes.parquet"
        frame = pd.read_parquet(episodes_path).copy()
        if frame.duplicated(["subject_id", "cutoff"]).any():
            raise ValueError(f"Duplicate subject/cutoff: {cohort}")
        if not frame.row_id.tolist() == list(range(len(frame))):
            raise ValueError(f"Non-canonical row_id sequence: {cohort}")
        frame = frame.rename(columns={"subject_id": "participant_id"})
        frame["cohort"] = cohort
        source_files.append({"role": f"{cohort}_episodes", "path": str(episodes_path),
                             "sha256": sha256(episodes_path)})
        for arm in ("data", "hybrid", "physical"):
            reference_path = robust_root / "analysis" / cohort / f"test_{arm}.csv"
            reference = pd.read_csv(reference_path)
            expected = frame.loc[frame.split.eq("test"), ["row_id", "participant_id", "cutoff"]].copy()
            expected["cutoff"] = pd.to_datetime(expected.cutoff, utc=True)
            check = reference[["row_id", "subject_id", "cutoff"]].copy()
            check["cutoff"] = pd.to_datetime(check.cutoff, utc=True)
            if (len(check) != len(expected)
                    or check.row_id.tolist() != expected.row_id.tolist()
                    or check.subject_id.astype(str).tolist() != expected.participant_id.astype(str).tolist()
                    or check.cutoff.astype("int64").tolist() != expected.cutoff.astype("int64").tolist()):
                raise ValueError(f"Saved Test reference does not align: {cohort}/{arm}")
            keep = reference[["row_id", "predicted_bed_offset", "predicted_wake_offset"]].rename(columns={
                "predicted_bed_offset": f"legacy_{arm}_bed",
                "predicted_wake_offset": f"legacy_{arm}_wake",
            })
            frame = frame.merge(keep, on="row_id", how="left", validate="one_to_one")
            source_files.append({"role": f"{cohort}_{arm}_test_reference", "path": str(reference_path),
                                 "sha256": sha256(reference_path)})
        rarity_path = robust_root / "analysis" / cohort / "test_data.csv"
        rarity = pd.read_csv(rarity_path, usecols=["row_id", "rare_by_train_mad"])
        frame = frame.merge(rarity, on="row_id", how="left", validate="one_to_one")
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    columns = [
        "cohort", "participant_id", "split", "row_id", "cutoff", "bed", "wake",
        "observed_at", "bed_offset", "wake_offset", "age", "sex", "label_source",
        "rare_by_train_mad", "legacy_data_bed", "legacy_data_wake",
        "legacy_hybrid_bed", "legacy_hybrid_wake", "legacy_physical_bed", "legacy_physical_wake",
    ]
    combined[columns].to_csv(output_csv, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    dataset_metadata = legacy_root / "dataset" / "dataset_metadata.json"
    metadata_raw = json.loads(dataset_metadata.read_text(encoding="utf-8"))
    source_qc = {
        cohort.lower(): metadata_raw["cohorts"][name]
        for cohort, name in (("cmi", "CMI"), ("mesa", "MESA"))
    }
    sidecar = {
        "schema": "sleepwell.gradual_input_metadata.v1",
        "privacy": "Private normalized input contains participant identifiers; do not distribute.",
        "normalized_input": {"sha256": sha256(output_csv), "rows": len(combined)},
        "cohorts": {cohort: {
            "rows": int((combined.cohort == cohort).sum()),
            "participants": int(combined.loc[combined.cohort == cohort, "participant_id"].nunique()),
            "split_rows": {split: int((combined.loc[combined.cohort == cohort, "split"] == split).sum())
                           for split in ("test", "train", "validation")},
        } for cohort in ("cmi", "mesa")},
        "source_qc": source_qc, "sources": source_files + [{
            "role": "dataset_metadata", "path": str(dataset_metadata), "sha256": sha256(dataset_metadata),
        }],
    }
    metadata_path = output_csv.with_suffix(".metadata.json")
    metadata_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"input_csv": str(output_csv), "metadata": str(metadata_path), "rows": len(combined)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prepare", action="store_true", help="export normalized private CSV from original parquet")
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--robust-root", type=Path)
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--bootstrap-replicates", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261103)
    args = parser.parse_args()
    if args.prepare:
        if not args.legacy_root or not args.robust_root:
            parser.error("--prepare requires --legacy-root and --robust-root")
        prepared = prepare(args.legacy_root.resolve(), args.robust_root.resolve(), args.input_csv.resolve())
        print(json.dumps(prepared, ensure_ascii=False))
    if args.output_dir:
        result = evaluate(args.input_csv, args.output_dir,
                          bootstrap_replicates=args.bootstrap_replicates, seed=args.seed,
                          progress=lambda update: print(json.dumps(update, ensure_ascii=False), flush=True))
        print(json.dumps({"result_path": result["result_path"],
                          "private_result_path": result["private_result_path"]}, ensure_ascii=False))
    elif not args.prepare:
        parser.error("provide --output-dir to evaluate or --prepare to export")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

