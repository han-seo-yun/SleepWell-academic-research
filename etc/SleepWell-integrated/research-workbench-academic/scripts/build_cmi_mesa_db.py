"""Build a local CMI/MESA episode database without changing source data or splits."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


COHORTS = ("CMI", "MESA")
COLUMNS = (
    "cohort", "subject_id", "split", "cutoff", "bed", "wake", "observed_at",
    "bedtime_min", "wake_min", "duration_min", "age", "sex", "label_source",
)
DEFAULT_SOURCE = Path(
    "E:/학술제/model/데이터모델/연구용데이터/next_night_run_20261020_v1/dataset/episodes.csv"
)
DEFAULT_OUTPUT = Path("E:/학술제/model/데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1")
SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;
CREATE TABLE datasets (
    cohort TEXT PRIMARY KEY CHECK (cohort IN ('CMI', 'MESA')),
    metadata_json TEXT NOT NULL
);
CREATE TABLE participants (
    cohort TEXT NOT NULL REFERENCES datasets(cohort),
    subject_id TEXT NOT NULL CHECK (length(subject_id) > 0),
    split TEXT NOT NULL CHECK (split IN ('train', 'validation', 'test')),
    PRIMARY KEY (cohort, subject_id),
    UNIQUE (cohort, subject_id, split)
);
CREATE TABLE sleep_episodes (
    cohort TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    split TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    bed TEXT NOT NULL,
    wake TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    bedtime_min REAL NOT NULL CHECK (bedtime_min BETWEEN 0 AND 960),
    wake_min REAL NOT NULL CHECK (wake_min <= 1440),
    duration_min REAL NOT NULL CHECK (duration_min BETWEEN 30 AND 960),
    age REAL,
    sex TEXT,
    label_source TEXT NOT NULL,
    source_csv_line INTEGER NOT NULL UNIQUE,
    row_sha256 TEXT NOT NULL,
    PRIMARY KEY (cohort, subject_id, cutoff),
    FOREIGN KEY (cohort, subject_id, split)
        REFERENCES participants(cohort, subject_id, split),
    CHECK (abs((wake_min - bedtime_min) - duration_min) < 0.000001)
);
CREATE INDEX episodes_by_split ON sleep_episodes(split, cohort);
CREATE TABLE build_info (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE VIEW model_data AS
    SELECT cohort, subject_id, split, cutoff, bed, wake, observed_at,
           bedtime_min, wake_min, duration_min, age, sex, label_source
    FROM sleep_episodes;
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def row_hash(values: tuple) -> str:
    return hashlib.sha256(json.dumps(
        values, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def normalize(row: dict) -> tuple:
    """Validate the existing local-wall-clock episode contract; do not impute."""
    if any(row.get(key) in (None, "") for key in COLUMNS if key not in ("age", "sex")):
        raise ValueError("missing required episode value")
    times = {key: datetime.fromisoformat(row[key])
             for key in ("cutoff", "bed", "wake", "observed_at")}
    if any(value.utcoffset() != timedelta(hours=9) for value in times.values()):
        raise ValueError("timestamps must preserve the source +09:00 representation")
    cutoff = times["cutoff"]
    if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
        raise ValueError("cutoff must be 20:00 local wall clock")
    if times["observed_at"] != times["wake"]:
        raise ValueError("observed_at must preserve the source wake-time assumption")
    values = dict(row)
    for key in ("bedtime_min", "wake_min", "duration_min", "age"):
        values[key] = None if key == "age" and row[key] == "" else float(row[key])
        if values[key] is not None and not math.isfinite(values[key]):
            raise ValueError("numeric episode values must be finite")
    for key, start, end in (
        ("bedtime_min", "cutoff", "bed"), ("wake_min", "cutoff", "wake"),
        ("duration_min", "bed", "wake"),
    ):
        minutes = (times[end] - times[start]).total_seconds() / 60
        if abs(values[key] - minutes) > 1e-6:
            raise ValueError("episode minutes disagree with timestamps")
    values["sex"] = row["sex"] or None
    return tuple(values[key] for key in COLUMNS)


def build_database(source: Path, metadata_path: Path, output: Path) -> dict:
    source, metadata_path, output = (Path(p).resolve() for p in (source, metadata_path, output))
    if output.exists():
        raise FileExistsError("Output directory already exists; choose a new version")
    input_hashes = {str(path): sha256(path) for path in (source, metadata_path)}
    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    dataset_metadata = {cohort: metadata["cohorts"][cohort] for cohort in COHORTS}
    info = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "included_cohorts": list(COHORTS),
        "input_sha256": input_hashes,
        "builder_sha256": sha256(Path(__file__)),
        "time_contract": metadata["time_contract"],
        "scope": "QC-passing next-night episodes only; no raw sensor streams or retraining",
        "split_policy": "Preserve source splits; identity is (cohort, subject_id)",
        "evaluation_status": "Existing test sets have been reused; not independent external validation",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cmi_mesa_build_", dir=output.parent) as temp:
        staging = Path(temp)
        db_path = staging / "cmi_mesa.sqlite3"
        connection = sqlite3.connect(db_path)
        excluded = Counter()
        try:
            connection.executescript(SCHEMA)
            with connection:
                connection.executemany("INSERT INTO datasets VALUES (?, ?)", [
                    (cohort, json.dumps(dataset_metadata[cohort], ensure_ascii=False))
                    for cohort in COHORTS
                ])
                participants = {}
                with source.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames != list(COLUMNS):
                        raise ValueError("Unexpected source CSV columns")
                    for row in reader:
                        if row["cohort"] not in COHORTS:
                            excluded[row["cohort"]] += 1
                            continue
                        if None in row or any(value is None for value in row.values()):
                            raise ValueError(f"Malformed CSV at line {reader.line_num}")
                        try:
                            values = normalize(row)
                            identity = (row["cohort"], row["subject_id"])
                            if identity not in participants:
                                connection.execute("INSERT INTO participants VALUES (?, ?, ?)",
                                                   (*identity, row["split"]))
                                participants[identity] = row["split"]
                            elif participants[identity] != row["split"]:
                                raise ValueError("Participant crosses train/validation/test splits")
                            connection.execute(
                                "INSERT INTO sleep_episodes VALUES (" + ",".join("?" * 15) + ")",
                                (*values, reader.line_num, row_hash(values)),
                            )
                        except (ValueError, sqlite3.IntegrityError) as error:
                            raise ValueError(f"Invalid included record at CSV line {reader.line_num}: {error}") from error
                summary = []
                for cohort in COHORTS:
                    records = connection.execute(
                        "SELECT split, COUNT(*), COUNT(DISTINCT subject_id) FROM model_data "
                        "WHERE cohort = ? GROUP BY split ORDER BY split", (cohort,),
                    ).fetchall()
                    episodes = sum(record[1] for record in records)
                    people = sum(record[2] for record in records)
                    qc = dataset_metadata[cohort]["qc"]
                    expected_people = qc.get("output_subjects", qc.get("subjects"))
                    if episodes != qc["common_output_rows"] or people != expected_people or not episodes:
                        raise ValueError(f"Source counts disagree with metadata for {cohort}")
                    summary.append({"cohort": cohort, "episodes": episodes, "participants": people,
                                    "splits": [{"split": s, "episodes": n, "participants": p}
                                               for s, n, p in records]})
                info.update(summary=summary, excluded_rows=dict(excluded))
                connection.executemany("INSERT INTO build_info VALUES (?, ?)", [
                    (key, json.dumps(value, ensure_ascii=False, allow_nan=False))
                    for key, value in info.items()
                ])
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("SQLite foreign-key check failed")
            query = "SELECT * FROM model_data ORDER BY cohort, subject_id, cutoff"
            with (staging / "cmi_mesa_episodes.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(COLUMNS)
                writer.writerows(connection.execute(query))
        finally:
            connection.close()
        if any(sha256(Path(path)) != digest for path, digest in input_hashes.items()):
            raise ValueError("Source files changed during the build")
        manifest = dict(info)
        manifest["verification"] = {"integrity_check": "ok", "foreign_key_violations": 0,
                                    "source_files_unchanged": True}
        manifest["output_sha256"] = {path.name: sha256(path) for path in (
            db_path, staging / "cmi_mesa_episodes.csv",
        )}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
        )
        (staging / "README.md").write_text(
            "# CMI·MESA 통합 데이터베이스\n\n"
            "Samsung 기록을 제외한 다음날 수면 예측용 QC 완료 에피소드입니다. "
            "원시 센서·PSG·설문 전체를 합친 자료가 아니며 모델을 재학습하지 않았습니다.\n\n"
            "- `cmi_mesa.sqlite3`: datasets, participants, sleep_episodes, build_info 테이블\n"
            "- `model_data` 뷰: 기존 13개 열을 제공하는 통합 분석 입력\n"
            "- `cmi_mesa_episodes.csv`: model_data의 전체 CSV 내보내기\n"
            "- `manifest.json`: 출처 해시, 출처별 인원·행수·분할, 검증과 산출물 해시\n\n"
            "참여자 키는 (cohort, subject_id)입니다. 출처가 다른 동일 ID를 같은 사람으로 연결하지 않습니다. "
            "기존 train/validation/test를 유지했고 결측 인구 정보는 NULL입니다.\n\n"
            "CMI 사건쌍과 MESA ACT 유래 라벨의 의미는 datasets에 보존했습니다. "
            "+09:00은 원본의 벽시계 표현이며 한국에서 측정했다는 의미가 아닙니다. "
            "MESA 재구성 날짜는 실제 계절·측정일로 해석하지 않습니다. "
            "observed_at은 기상 시각이라는 후향 가정입니다.\n\n"
            "기존 Test는 재사용된 자료입니다. 통합 후에도 독립 검증으로 바뀌지 않습니다. "
            "이용 제한 파생자료로 원본과 같은 로컬 저장소에 보관하며 외부에 공개하지 않습니다.\n\n"
            "```sql\nSELECT cohort, split, COUNT(*) FROM model_data GROUP BY cohort, split;\n"
            "SELECT * FROM model_data WHERE split = 'train';\n```\n",
            encoding="utf-8",
        )
        staging.rename(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_database(args.source, args.metadata or args.source.with_name("dataset_metadata.json"), args.output)
    print(json.dumps({"output": str(args.output.resolve()), "summary": result["summary"],
                      "excluded_rows": result["excluded_rows"], "verification": result["verification"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
