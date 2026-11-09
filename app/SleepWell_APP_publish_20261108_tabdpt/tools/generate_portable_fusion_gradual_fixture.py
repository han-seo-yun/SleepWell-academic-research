#!/usr/bin/env python3
"""Generate Android golden fixtures from the canonical Python gradual_v1 runtime.

The generator imports the selected canonical web-app implementation instead of
reimplementing its arithmetic.  It never rewrites the frozen model artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


CUTOFF = datetime.fromisoformat("2026-10-20T20:00:00+09:00")
PROFILE = {
    "age_band": "30-44",
    "sex": "any",
    "activity_minutes": 60,
    "usual_bed": "23:00",
    "usual_wake": "07:00",
    "desired_sleep_minutes": 480,
    "created_at": "2026-08-31T15:00:00Z",
}


def instant_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def android_record(index: str, bed: datetime | str, wake: datetime | str,
                   observed: datetime | str | None, source: str = "diary") -> dict:
    bed_text = instant_text(bed) if isinstance(bed, datetime) else bed
    wake_text = instant_text(wake) if isinstance(wake, datetime) else wake
    observed_text = instant_text(observed) if isinstance(observed, datetime) else observed
    try:
        duration = int((datetime.fromisoformat(wake_text) - datetime.fromisoformat(bed_text)).total_seconds() / 60)
        day = datetime.fromisoformat(wake_text).date().isoformat()
    except (TypeError, ValueError):
        duration = 0
        day = "invalid"
    return {
        "id": index,
        "day": day,
        "bedtime": bed_text,
        "wake_time": wake_text,
        "duration_minutes": duration,
        "rested": 3,
        "fatigue": 3,
        "source": source,
        "modified_at": observed_text,
    }


def regular_rows(count: int) -> list[dict]:
    rows = []
    for ordinal, days_back in enumerate(range(count, 0, -1)):
        wake = CUTOFF.replace(hour=7, minute=0) - timedelta(days=days_back - 1)
        wake += timedelta(minutes=(ordinal * 11) % 37)
        duration = timedelta(minutes=450 + (ordinal % 5) * 12)
        bed = wake - duration
        rows.append(android_record(f"regular:{count}:{ordinal}", bed, wake, wake + timedelta(minutes=5)))
    return rows


def irregular_rows() -> list[dict]:
    rows = []
    for ordinal, (days_back, bed_hour, bed_minute, duration_min) in enumerate((
        (27, 22, 40, 430),
        (15, 0, 5, 390),
        (8, 23, 20, 510),
        (3, 1, 10, 360),
        (1, 22, 55, 475),
    )):
        wake_day = (CUTOFF - timedelta(days=days_back - 1)).date()
        bed = datetime.combine(wake_day - timedelta(days=1), datetime.min.time(), CUTOFF.tzinfo)
        bed = bed.replace(hour=bed_hour, minute=bed_minute)
        wake = bed + timedelta(minutes=duration_min)
        rows.append(android_record(f"irregular:{ordinal}", bed, wake, wake + timedelta(minutes=9)))
    return rows


def fixture_cases() -> list[dict]:
    cases = [{"name": f"history_{count}", "records": regular_rows(count)} for count in (0, 1, 5, 13, 14, 30)]

    missing = regular_rows(5)
    wake = CUTOFF.replace(hour=7) - timedelta(days=7)
    missing.extend((
        android_record("missing:observed", wake - timedelta(hours=8), wake, None),
        android_record("missing:bed", "", wake.isoformat(), wake + timedelta(minutes=5)),
    ))
    cases.append({"name": "missing_fields", "records": missing})
    cases.append({"name": "irregular_gaps", "records": irregular_rows()})

    causal = regular_rows(5)
    first = causal[0]
    causal.extend((
        dict(first, id="causal:late-observation", bedtime="2026-10-19T12:00:00Z",
             modified_at=instant_text(CUTOFF + timedelta(seconds=1))),
        android_record("causal:future-wake", CUTOFF + timedelta(hours=1),
                       CUTOFF + timedelta(hours=9), CUTOFF - timedelta(minutes=1)),
        android_record("causal:pre-enrollment", datetime.fromisoformat("2026-08-20T23:00:00+09:00"),
                       datetime.fromisoformat("2026-08-21T07:00:00+09:00"),
                       datetime.fromisoformat("2026-08-21T07:05:00+09:00")),
        android_record("causal:archive", CUTOFF - timedelta(days=2, hours=20),
                       CUTOFF - timedelta(days=2, hours=12), CUTOFF - timedelta(days=2, hours=11),
                       source="archive"),
    ))
    cases.append({"name": "causal_future_and_late", "records": causal})
    return cases


def python_record(record: dict) -> dict:
    return {
        "id": record["id"],
        "day": record["day"],
        "bed": record["bedtime"],
        "wake": record["wake_time"],
        "observed_at": record["modified_at"],
        "source": record["source"],
        "latency_min": 0,
    }


def generate(web_app: Path, models: Path) -> dict:
    sys.path.insert(0, str(web_app.resolve()))
    from fusion_forecast import (  # pylint: disable=import-error,import-outside-toplevel
        FEATURE_CONTRACT,
        MODEL_VERSION,
        PHYSICAL_PROVIDER,
        RUNTIME_MODEL_VERSION,
        PortableCohortModel,
    )
    from night_core import GRADUAL_POLICY  # pylint: disable=import-error,import-outside-toplevel

    cohort_models = {
        cohort: PortableCohortModel(models / f"robust_{cohort}_v2.json", cohort)
        for cohort in ("cmi", "mesa")
    }
    output_cases = []
    for case in fixture_cases():
        snapshots = {}
        personalization = {}
        predictions = {}
        records = [python_record(record) for record in case["records"]]
        for cohort, model in cohort_models.items():
            result = model.predict(PROFILE, records, CUTOFF, personalization_policy=GRADUAL_POLICY)
            snapshots[cohort] = result["features"]
            personalization[cohort] = result["personalization"]
            for arm in ("physical", "data", "hybrid"):
                predictions[f"{arm}_{cohort}"] = {
                    "onset_offset_min": result[arm][0],
                    "wake_offset_min": result[arm][1],
                }
        output_cases.append({
            "name": case["name"],
            "profile": PROFILE,
            "cutoff": CUTOFF.isoformat(),
            "records": case["records"],
            "expected": {
                "snapshots": snapshots,
                "personalization": personalization,
                "predictions": predictions,
            },
        })
    return {
        "schema_version": 1,
        "generator": "canonical web-app PortableCohortModel gradual_v1",
        "model_version": MODEL_VERSION,
        "runtime_model_version": RUNTIME_MODEL_VERSION,
        "personalization_policy": GRADUAL_POLICY,
        "feature_contract": FEATURE_CONTRACT,
        "physical_provider": PHYSICAL_PROVIDER,
        "research_metrics_scope": "not_validated_for_gradual_v1_runtime",
        "cases": output_cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-app", type=Path, required=True,
                        help="canonical SleepWell-integrated/web-app directory")
    parser.add_argument("--models", type=Path,
                        default=Path(__file__).resolve().parents[1] / "app/src/main/assets/models")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] /
                        "app/src/androidTest/assets/portable_fusion_gradual_v1_golden.json")
    args = parser.parse_args()
    fixture = generate(args.web_app, args.models)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()
