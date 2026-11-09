from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


def subject_token(participant: int) -> str:
    digest = hashlib.sha256(f"mesa_subject_v1|{int(participant)}".encode()).hexdigest()
    return f"mesa_{digest[:20]}"


def raw_path(root: Path, participant: int) -> Path:
    candidates = {root / f"mesa-sleep-{participant:04d}.csv", root / f"mesa-sleep-{participant}.csv"}
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise FileNotFoundError(participant)
    return found[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    run = workspace / "데이터모델/연구용데이터/mesa_psg_run_20261020_v2"
    raw = workspace / "데이터셋/MESA/actigraphy"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    test = [row for row in manifest["subjects"] if row["split"] == "test"]
    ids = {subject_token(value): value for value in range(1, 10000)}
    starts = Counter()
    any_pre20 = 0
    contiguous_to20 = []
    for subject in test:
        participant = ids[subject["token"]]
        first = pd.read_csv(raw_path(raw, participant),
                            usecols=["line", "date_shifted", "linetime"], nrows=1).iloc[0]
        origin = datetime.fromisoformat(f"{first['date_shifted']}T{first['linetime']}")
        origin -= timedelta(seconds=(int(first["line"]) - 1) * 30)
        with np.load(run / "subjects" / f"{subject['token']}.npz", allow_pickle=False) as saved:
            lines = saved["line"].astype(np.int64)
        stamps = [origin + timedelta(seconds=(int(line) - 1) * 30) for line in lines]
        starts[str(stamps[0].hour)] += 1
        cutoff = stamps[0].replace(hour=20, minute=0, second=0, microsecond=0)
        indices = [index for index, stamp in enumerate(stamps)
                   if stamp + timedelta(seconds=30) <= cutoff]
        if indices:
            any_pre20 += 1
            selected = lines[indices]
            if (np.diff(selected) == 1).all() and stamps[indices[-1]] + timedelta(seconds=30) == cutoff:
                contiguous_to20.append(len(indices) * 0.5)
    report = {
        "schema_version": 1,
        "estimand": "strict same-night 20:00 current-S support using only complete PSG/sensor rows ending by 20:00",
        "test_people": len(test),
        "recording_first_eligible_hour_counts": dict(sorted(starts.items(), key=lambda item: int(item[0]))),
        "people_with_any_eligible_pre20_rows": any_pre20,
        "people_with_contiguous_support_ending_exactly_at20": len(contiguous_to20),
        "contiguous_minutes": contiguous_to20,
        "verdict": "not_estimable" if len(contiguous_to20) < 20 else "potentially_estimable",
        "reason": "Current PSG recordings generally begin at or after 20:00; only one Test participant has contiguous pre-20 support, lasting 16 minutes.",
        "labels_read_for_selection": False,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
