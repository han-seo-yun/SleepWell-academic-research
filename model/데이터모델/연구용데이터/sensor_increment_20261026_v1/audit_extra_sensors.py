"""Read-only event-presence audit; not a new feature or outcome-model analysis."""
from pathlib import Path
from zipfile import ZipFile
from io import StringIO
import csv
import hashlib
import json
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent / "extra_sensor_availability.json"


def parse_export(payload):
    rows = list(csv.reader(StringIO(payload.decode("utf-8-sig"))))
    header = rows[1]
    clean = []
    for row in rows[2:]:
        if not row:
            continue
        if len(row) < len(header) or any(row[len(header):]):
            raise ValueError("Unexpected Samsung CSV width; refusing implicit index shifting")
        clean.append(row[:len(header)])
    frame = pd.DataFrame(clean, columns=[name.rsplit(".", 1)[-1] for name in header])
    if frame.columns.duplicated().any():
        raise ValueError("Ambiguous normalized CSV columns")
    return frame


def main():
    if OUTPUT.exists():
        raise FileExistsError(OUTPUT)
    personal = ROOT / "보조데이터셋/개인데이터/삼성헬스_원본"
    predictions_path = ROOT / "데이터모델/연구용데이터/personal_watch_survey_20261025_v1/predictions.csv"
    predictions = pd.read_csv(predictions_path)
    targets = predictions[predictions.arm.eq("baseline_last_night")]
    results, hashes = [], {}
    for pid, directory, suffix, expected_n in [
        ("P02", "신혜지", "20261025094402", 38),
        ("P03", "정채윤", "20261025110878", 17),
    ]:
        base = personal / directory / "원본/20261025"
        streams, source_counts = {}, {}
        for kind, stem, value in [
            ("hr", "heart_rate", "heart_rate"),
            ("steps", "pedometer_step_count", "count"),
        ]:
            name = f"com.samsung.shealth.tracker.{stem}.{suffix}.csv"
            loose = base / name
            if loose.is_file():
                payload = loose.read_bytes()
                source = str(loose.relative_to(ROOT))
            else:
                archives = list(base.glob("*.zip"))
                if len(archives) != 1:
                    raise ValueError("Expected exactly one canonical archive")
                with ZipFile(archives[0]) as archive:
                    members = [n for n in archive.namelist() if Path(n).name == name]
                    if len(members) != 1:
                        raise ValueError("Expected one canonical archive member")
                    payload = archive.read(members[0])
                    source = str(archives[0].relative_to(ROOT)) + ":" + members[0]
            hashes[f"{pid}_{kind}"] = {
                "private_source": source,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            raw = parse_export(payload)
            original_n = len(raw)
            original_offsets = raw.time_offset.value_counts(dropna=False).to_dict()
            known = raw.datauuid.ne("")
            identified = raw[known].sort_values("update_time").drop_duplicates("datauuid", keep="last")
            anonymous = raw[~known].drop_duplicates(["start_time", "end_time", "deviceuuid", value])
            raw = pd.concat([identified, anonymous], ignore_index=True)
            for column in ("start_time", "end_time"):
                raw[column] = pd.to_datetime(raw[column], utc=True, format="mixed", errors="coerce").dt.tz_convert("Asia/Seoul")
            values = pd.to_numeric(raw[value], errors="coerce")
            valid = np.isfinite(values) & (values.gt(0) if kind == "hr" else values.ge(0))
            valid &= raw.time_offset.eq("UTC+0900") & raw.end_time.ge(raw.start_time)
            streams[kind] = raw[valid].copy()
            source_counts[kind] = {
                "raw_data_rows": original_n,
                "deduplicated_rows": len(raw),
                "valid_candidate_events": int(valid.sum()),
                "source_offsets": original_offsets,
            }
        participant_targets = targets[targets.participant_id.eq(pid)]
        if len(participant_targets) != expected_n:
            raise ValueError("Frozen target count changed")
        rows = []
        for target in participant_targets.itertuples():
            cutoff = pd.Timestamp(target.cutoff)
            row = {"split": target.split}
            for kind, events in streams.items():
                selected = events[events.start_time.ge(cutoff.normalize()) & events.end_time.le(cutoff)]
                row[kind] = len(selected)
                row[kind + "_point"] = int(selected.end_time.eq(selected.start_time).sum())
            row["both"] = bool(row["hr"] and row["steps"])
            rows.append(row)
        frame = pd.DataFrame(rows)
        def counts(group):
            return {"target_n": len(group), "hr_target_n": int(group.hr.gt(0).sum()),
                    "steps_target_n": int(group.steps.gt(0).sum()), "both_target_n": int(group.both.sum())}
        results.append({"participant_id": pid, **counts(frame), "source_counts": source_counts,
                        "by_split": {split: counts(group) for split, group in frame.groupby("split")}})
    result = {
        "status": "retrospective_raw_event_presence_only",
        "participants": results,
        "sources": hashes,
        "genuinely_new_measurements_collected": 0,
        "csv_alignment": "Explicit header width; only surplus empty trailing fields allowed. Pandas implicit index inference is not used.",
        "eligibility": "UTC event clocks converted once to KST; start >= local day start and end <=20:00; valid value and nonnegative interval; UUID deduplicated.",
        "limitations": [
            "Presence of scalar/aggregate events is not minute coverage or verified wear time.",
            "HR point and interval classes and multiple step devices are not reconciled into predictive features.",
            "Historical receipt times are unavailable. No day totals or backfilled surveys are used.",
            "Current TRAIN overlap is inadequate for the fixed 40-complete-row association gate; no new outcome model is fitted.",
        ],
    }
    with OUTPUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"participants": [{k: v for k, v in row.items() if k != "source_counts"} for row in results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
