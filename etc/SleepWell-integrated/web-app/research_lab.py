"""Stateless comparison of frozen weights with gradual runtime personalization.

The module accepts only canonical in-memory rows, replays each participant at
20:00 KST with strictly available history, and never trains or writes data.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
from collections import defaultdict
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from statistics import mean
from time import perf_counter

from fusion_forecast import (
    COHORTS,
    MODEL_VERSION,
    RUNTIME_MODEL_VERSION,
    FusionModelError,
    PortableCohortModel,
)
from night_core import (
    C_AMPLITUDE,
    CONTRACT,
    GRADUAL_POLICY,
    TAU_SLEEP,
    TAU_WAKE,
    ZONE,
    features as night_features,
)
from robust_features import FEATURE_CONTRACT
from research_lab_context import normalize_context, context_features
from sleep_observation import describe_observations


SCHEMA_VERSION = 1
MAX_ROWS = 365
MAX_PARTICIPANTS = 10
MAX_CSV_BYTES = 2 * 1024 * 1024
CANONICAL_FIELDS = (
    "participant_id",
    "cutoff",
    "sleep_start",
    "sleep_end",
    "observed_at",
)
SYNTHETIC_DEFAULTS = {
    "n_nights": 42,
    "seed": 42,
    "start_date": "2026-01-01",
    "bed_hour": 23.5,
    "duration_hours": 8.0,
    "jitter_minutes": 45.0,
    "phase_shift_minutes": 60.0,
    "shift_after_night": 21,
    "missing_every": 0,
    "report_delay_hours": 1.0,
}
_PARTICIPANT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class LabInputError(ValueError):
    """A safe, user-correctable lab payload or model-availability error."""

    def __init__(self, message, code="invalid_input"):
        super().__init__(message)
        self.code = code


def _models_root(models_dir):
    if models_dir is None:
        return Path(__file__).resolve().with_name("models")
    try:
        return Path(models_dir)
    except TypeError as error:
        raise LabInputError("models_dir must be a filesystem path.", "model_unavailable") from error


def model_status(models_dir=None):
    """Report exact installed artifact availability without substituting a model."""
    root = _models_root(models_dir)
    statuses = {}
    for cohort in COHORTS:
        path = root / f"robust_{cohort}_v2.json"
        if not path.is_file():
            statuses[cohort] = {
                "loaded": False,
                "cohort": cohort,
                "error": f"Required model artifact is missing: robust_{cohort}_v2.json",
            }
            continue
        try:
            model = PortableCohortModel(path, cohort)
        except (FusionModelError, OSError, UnicodeError, ValueError) as error:
            statuses[cohort] = {
                "loaded": False,
                "cohort": cohort,
                "error": f"Model artifact is invalid: {error}",
            }
            continue
        statuses[cohort] = {
            "loaded": True,
            "cohort": cohort,
            "sha256": model.sha256,
            "model_version": model.raw["model_version"],
            "feature_contract": model.raw["feature_contract"],
            "physical_provider": model.raw["physical_provider"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "available": all(statuses[item]["loaded"] for item in COHORTS),
        "model_version": RUNTIME_MODEL_VERSION,
        "artifact_model_version": MODEL_VERSION,
        "personalization_policy": GRADUAL_POLICY,
        "feature_contract": CONTRACT,
        "model_feature_contract": FEATURE_CONTRACT,
        "cohorts": statuses,
    }


def _require_object(value, label):
    if not isinstance(value, dict):
        raise LabInputError(f"{label} must be an object.")
    return value


def _reject_unknown_fields(value, allowed, label):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise LabInputError(f"{label} has unsupported fields: {', '.join(unknown)}")


def _participant_id(value):
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 64:
        raise LabInputError("participant_id must be a 1-64 character pseudonymous ID.")
    if value[0] not in _PARTICIPANT_CHARS or any(char not in _PARTICIPANT_CHARS for char in value):
        raise LabInputError(
            "participant_id may contain only ASCII letters, digits, period, underscore, and hyphen."
        )
    return value


def _aware_datetime(value, label):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise LabInputError(f"{label} must be an ISO-8601 timestamp with an offset.")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is missing")
        return parsed.astimezone(ZONE)
    except (TypeError, ValueError, OverflowError) as error:
        raise LabInputError(f"{label} must be an ISO-8601 timestamp with an offset.") from error


def _normalize_row(raw, row_number):
    label = f"row {row_number}"
    _require_object(raw, label)
    if set(raw) != set(CANONICAL_FIELDS):
        missing = sorted(set(CANONICAL_FIELDS) - set(raw))
        extra = sorted(set(raw) - set(CANONICAL_FIELDS))
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unsupported {', '.join(extra)}")
        raise LabInputError(f"{label} must contain exactly the canonical fields ({'; '.join(details)}).")

    participant = _participant_id(raw["participant_id"])
    cutoff = _aware_datetime(raw["cutoff"], f"{label}.cutoff")
    if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (20, 0, 0, 0):
        raise LabInputError(f"{label}.cutoff must be exactly 20:00:00 KST.")

    outcomes = (raw["sleep_start"], raw["sleep_end"], raw["observed_at"])
    all_missing = all(value is None for value in outcomes)
    all_present = all(value is not None for value in outcomes)
    if not (all_missing or all_present):
        raise LabInputError(
            f"{label} outcomes must be either all explicit or all null; partial outcomes are invalid."
        )

    normalized = {
        "participant_id": participant,
        "cutoff": cutoff,
        "sleep_start": None,
        "sleep_end": None,
        "observed_at": None,
    }
    if all_missing:
        return normalized

    sleep_start = _aware_datetime(raw["sleep_start"], f"{label}.sleep_start")
    sleep_end = _aware_datetime(raw["sleep_end"], f"{label}.sleep_end")
    observed_at = _aware_datetime(raw["observed_at"], f"{label}.observed_at")
    onset_offset = (sleep_start - cutoff).total_seconds() / 60.0
    wake_offset = (sleep_end - cutoff).total_seconds() / 60.0
    duration = (sleep_end - sleep_start).total_seconds() / 60.0
    if not (0.0 <= onset_offset <= 960.0):
        raise LabInputError(f"{label}.sleep_start is outside the supported 0-960 minute horizon.")
    if not (
        30.0 <= duration <= 960.0
        and onset_offset + 30.0 <= wake_offset <= 1440.0
    ):
        raise LabInputError(
            f"{label}.sleep_end must be 30-960 minutes after onset and no later than cutoff + 1440 minutes."
        )
    if observed_at < sleep_end:
        raise LabInputError(
            f"{label}.observed_at must be at or after sleep_end; planned or future-known outcomes are invalid."
        )
    normalized.update(
        sleep_start=sleep_start,
        sleep_end=sleep_end,
        observed_at=observed_at,
    )
    return normalized


def _normalize_rows(records):
    if not isinstance(records, list):
        raise LabInputError("records must be an array of canonical row objects.")
    if not 1 <= len(records) <= MAX_ROWS:
        raise LabInputError(f"A run must contain between 1 and {MAX_ROWS} rows.")
    rows = [_normalize_row(raw, index) for index, raw in enumerate(records, 1)]
    seen = set()
    for row in rows:
        key = (row["participant_id"], row["cutoff"])
        if key in seen:
            raise LabInputError("Duplicate participant_id + cutoff keys are not allowed.")
        seen.add(key)
    participants = {row["participant_id"] for row in rows}
    if len(participants) > MAX_PARTICIPANTS:
        raise LabInputError(f"A run may contain at most {MAX_PARTICIPANTS} participants.")
    return sorted(rows, key=lambda row: (row["participant_id"], row["cutoff"]))


def _csv_records(csv_text):
    if not isinstance(csv_text, str):
        raise LabInputError("csv_text must be CSV content, not a path or URL.")
    try:
        encoded_size = len(csv_text.encode("utf-8"))
    except UnicodeError as error:
        raise LabInputError("csv_text must be valid UTF-8 text.") from error
    if not csv_text or encoded_size > MAX_CSV_BYTES or "\x00" in csv_text:
        raise LabInputError(f"csv_text must be non-empty and at most {MAX_CSV_BYTES} UTF-8 bytes.")
    source = csv_text[1:] if csv_text.startswith("\ufeff") else csv_text
    try:
        reader = csv.DictReader(io.StringIO(source, newline=""), strict=True)
        if reader.fieldnames != list(CANONICAL_FIELDS):
            raise LabInputError(
                "CSV header must exactly match: " + ",".join(CANONICAL_FIELDS)
            )
        records = []
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise LabInputError("CSV rows must have exactly five columns.")
            cleaned = {key: value.strip() for key, value in raw.items()}
            for field in ("sleep_start", "sleep_end", "observed_at"):
                if cleaned[field] == "":
                    cleaned[field] = None
            records.append(cleaned)
            if len(records) > MAX_ROWS:
                raise LabInputError(f"A run may contain at most {MAX_ROWS} rows.")
    except csv.Error as error:
        raise LabInputError(f"csv_text is malformed: {error}") from error
    return records


def _finite_number(value, label, minimum, maximum, integer=False):
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        kind = "integer" if integer else "number"
        raise LabInputError(f"synthetic.{label} must be a {kind}.")
    number = int(value) if integer else float(value)
    if not math.isfinite(float(number)) or not minimum <= number <= maximum:
        raise LabInputError(
            f"synthetic.{label} must be between {minimum} and {maximum}."
        )
    return number


def _synthetic_config(raw):
    _require_object(raw, "synthetic")
    _reject_unknown_fields(raw, SYNTHETIC_DEFAULTS, "synthetic")
    config = dict(SYNTHETIC_DEFAULTS)
    config.update(raw)
    config["n_nights"] = _finite_number(
        config["n_nights"], "n_nights", 1, MAX_ROWS, integer=True
    )
    config["seed"] = _finite_number(
        config["seed"], "seed", 0, 4_294_967_295, integer=True
    )
    config["bed_hour"] = _finite_number(config["bed_hour"], "bed_hour", 0.0, 24.0)
    if config["bed_hour"] >= 24.0:
        raise LabInputError("synthetic.bed_hour must be less than 24.")
    config["duration_hours"] = _finite_number(
        config["duration_hours"], "duration_hours", 1.0, 16.0
    )
    config["jitter_minutes"] = _finite_number(
        config["jitter_minutes"], "jitter_minutes", 0.0, 360.0
    )
    config["phase_shift_minutes"] = _finite_number(
        config["phase_shift_minutes"], "phase_shift_minutes", -720.0, 720.0
    )
    config["shift_after_night"] = _finite_number(
        config["shift_after_night"],
        "shift_after_night",
        0,
        config["n_nights"],
        integer=True,
    )
    config["missing_every"] = _finite_number(
        config["missing_every"], "missing_every", 0, MAX_ROWS, integer=True
    )
    config["report_delay_hours"] = _finite_number(
        config["report_delay_hours"], "report_delay_hours", 0.0, 168.0
    )
    if not isinstance(config["start_date"], str):
        raise LabInputError("synthetic.start_date must be a strict ISO date.")
    try:
        parsed_date = date.fromisoformat(config["start_date"])
    except ValueError as error:
        raise LabInputError("synthetic.start_date must be a strict ISO date.") from error
    if parsed_date.isoformat() != config["start_date"]:
        raise LabInputError("synthetic.start_date must be a strict ISO date.")
    config["start_date"] = parsed_date.isoformat()
    return config


def _generate_synthetic(config):
    """Generate independent schedule truth; no prediction or artifact is consulted."""
    rng = random.Random(config["seed"])
    start = date.fromisoformat(config["start_date"])
    base_onset = (config["bed_hour"] * 60.0 - 1200.0) % 1440.0
    rows = []
    try:
        for index in range(config["n_nights"]):
            cutoff_date = start + timedelta(days=index)
            cutoff = datetime.combine(cutoff_date, datetime_time(20, 0), tzinfo=ZONE)
            onset_noise = rng.uniform(
                -config["jitter_minutes"], config["jitter_minutes"]
            )
            duration_noise = rng.uniform(
                -config["jitter_minutes"] / 4.0,
                config["jitter_minutes"] / 4.0,
            )
            missing = bool(
                config["missing_every"]
                and (index + 1) % config["missing_every"] == 0
            )
            if missing:
                rows.append(
                    {
                        "participant_id": "synthetic-001",
                        "cutoff": cutoff.isoformat(),
                        "sleep_start": None,
                        "sleep_end": None,
                        "observed_at": None,
                    }
                )
                continue
            shifted = index >= config["shift_after_night"]
            phase = config["phase_shift_minutes"] if shifted else 0.0
            onset_offset = min(960.0, max(0.0, base_onset + phase + onset_noise))
            onset_offset = round(onset_offset * 60.0) / 60.0
            maximum_duration = min(960.0, 1440.0 - onset_offset)
            duration = config["duration_hours"] * 60.0 + duration_noise
            duration = min(maximum_duration, max(60.0, duration))
            duration = round(duration * 60.0) / 60.0
            sleep_start = cutoff + timedelta(minutes=onset_offset)
            sleep_end = sleep_start + timedelta(minutes=duration)
            observed_at = sleep_end + timedelta(hours=config["report_delay_hours"])
            rows.append(
                {
                    "participant_id": "synthetic-001",
                    "cutoff": cutoff.isoformat(),
                    "sleep_start": sleep_start.isoformat(),
                    "sleep_end": sleep_end.isoformat(),
                    "observed_at": observed_at.isoformat(),
                }
            )
    except (OverflowError, ValueError) as error:
        raise LabInputError("synthetic date range is outside supported datetime bounds.") from error
    return _normalize_rows(rows)


def _validate_payload(payload):
    _require_object(payload, "payload")
    common = {"schema_version", "mode", "cohorts", "chart_participant_id"}
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(
        payload.get("schema_version"), bool
    ):
        raise LabInputError(f"schema_version must be {SCHEMA_VERSION}.")
    mode = payload.get("mode")
    if mode not in ("real", "synthetic"):
        raise LabInputError("mode must be 'real' or 'synthetic'.")
    cohorts = payload.get("cohorts")
    if (
        not isinstance(cohorts, list)
        or not cohorts
        or any(not isinstance(item, str) or item not in COHORTS for item in cohorts)
        or len(cohorts) != len(set(cohorts))
    ):
        raise LabInputError("cohorts must be a non-empty unique subset of ['cmi', 'mesa'].")
    cohorts = [cohort for cohort in COHORTS if cohort in cohorts]

    if mode == "real":
        _reject_unknown_fields(payload, common | {"records", "csv_text", "context"}, "payload")
        choices = [name for name in ("records", "csv_text") if name in payload]
        if len(choices) != 1:
            raise LabInputError("Real mode requires exactly one of records or csv_text.")
        records = payload["records"] if choices[0] == "records" else _csv_records(payload["csv_text"])
        rows = _normalize_rows(records)
        synthetic = None
    else:
        _reject_unknown_fields(payload, common | {"synthetic"}, "payload")
        if "synthetic" not in payload:
            raise LabInputError("Synthetic mode requires a synthetic controls object.")
        synthetic = _synthetic_config(payload["synthetic"])
        rows = _generate_synthetic(synthetic)

    participants = sorted({row["participant_id"] for row in rows})
    chart_participant = payload.get("chart_participant_id", participants[0])
    chart_participant = _participant_id(chart_participant)
    if chart_participant not in participants:
        raise LabInputError("chart_participant_id must identify a participant in this run.")
    return mode, cohorts, rows, synthetic, chart_participant


def _load_models(cohorts, models_dir):
    root = _models_root(models_dir)
    loaded = {}
    for cohort in cohorts:
        filename = f"robust_{cohort}_v2.json"
        path = root / filename
        if not path.is_file():
            raise LabInputError(
                f"Required model artifact is missing: {filename}", "model_unavailable"
            )
        try:
            loaded[cohort] = PortableCohortModel(path, cohort)
        except (FusionModelError, OSError, UnicodeError, ValueError) as error:
            raise LabInputError(
                f"Required {cohort.upper()} model artifact is invalid: {error}",
                "model_unavailable",
            ) from error
    return loaded


def _model_metadata(models):
    output = []
    labels = {
        "physical_fixed": "위상·임계값 고정 물리",
        "physical_personal": "개인화 물리",
        "data": "데이터 모델",
        "hybrid": "융합 모델",
    }
    for cohort, model in models.items():
        for kind in ("physical_fixed", "physical_personal", "data", "hybrid"):
            output.append(
                {
                    "id": f"{cohort}.{kind}",
                    "label": labels[kind],
                    "cohort": cohort,
                    "kind": kind,
                    "artifact_sha256": model.sha256,
                    "personalization_policy": "fixed" if kind == "physical_fixed" else GRADUAL_POLICY,
                    "runtime_model_version": RUNTIME_MODEL_VERSION,
                    "artifact_model_version": MODEL_VERSION,
                }
            )
    output.append(
        {
            "id": "baseline.recent7",
            "label": "최근 28일 내 7회 중앙값",
            "cohort": "shared",
            "kind": "baseline_recent7",
            "artifact_sha256": None,
        }
    )
    return output


def _actual_offsets(row):
    if row["sleep_start"] is None:
        return None, None, None
    onset = (row["sleep_start"] - row["cutoff"]).total_seconds() / 60.0
    wake = (row["sleep_end"] - row["cutoff"]).total_seconds() / 60.0
    return onset, wake, wake - onset


def _finite_prediction(values, label):
    try:
        onset, wake = (float(values[0]), float(values[1]))
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LabInputError(f"{label} returned an invalid prediction.", "inference_failed") from error
    if not (
        math.isfinite(onset)
        and math.isfinite(wake)
        and 0.0 <= onset <= 960.0
        and onset + 30.0 <= wake <= 1440.0
        and wake - onset <= 960.0
    ):
        raise LabInputError(f"{label} returned an out-of-support prediction.", "inference_failed")
    return onset, wake


def _prediction_row(row, model_id, values):
    actual_onset, actual_wake, actual_interval = _actual_offsets(row)
    if values is None:
        onset = wake = interval = None
    else:
        onset, wake = _finite_prediction(values, model_id)
        interval = wake - onset
    return {
        "participant_id": row["participant_id"],
        "cutoff": row["cutoff"].isoformat(),
        "model_id": model_id,
        "onset_offset_min": onset,
        "wake_offset_min": wake,
        "interval_min": interval,
        "actual_onset_offset_min": actual_onset,
        "actual_wake_offset_min": actual_wake,
        "actual_interval_min": actual_interval,
    }


def _history_records(rows):
    return [
        {
            "source": "research",
            "sleep_start": row["sleep_start"].isoformat(),
            "wake": row["sleep_end"].isoformat(),
            "observed_at": row["observed_at"].isoformat(),
        }
        for row in rows
        if row["sleep_start"] is not None
    ]


def _replay(rows, models):
    by_participant = defaultdict(list)
    for row in rows:
        by_participant[row["participant_id"]].append(row)
    predictions = []
    personalization = []
    for participant in sorted(by_participant):
        participant_rows = sorted(by_participant[participant], key=lambda item: item["cutoff"])
        enrollment = participant_rows[0]["cutoff"]
        profile = {"created_at": enrollment.isoformat()}
        history = _history_records(participant_rows)
        for row in participant_rows:
            baseline_values = None
            for cohort, model in models.items():
                try:
                    personal = model.predict(
                        profile, history, row["cutoff"],
                        personalization_policy=GRADUAL_POLICY,
                    )
                    fixed_values, _fixed_state = night_features(
                        history,
                        row["cutoff"],
                        model.raw["prior"],
                        personalize=False,
                        enrolled_at=enrollment,
                    )
                except (FusionModelError, KeyError, TypeError, ValueError, OverflowError) as error:
                    raise LabInputError(
                        f"Inference failed for {cohort.upper()} at {row['cutoff'].isoformat()}: {error}",
                        "inference_failed",
                    ) from error
                features = personal["features"]
                state = personal["personalization"]
                predictions.extend(
                    (
                        _prediction_row(
                            row,
                            f"{cohort}.physical_fixed",
                            (fixed_values["physical_bed"], fixed_values["physical_wake"]),
                        ),
                        _prediction_row(
                            row, f"{cohort}.physical_personal", personal["physical"]
                        ),
                        _prediction_row(row, f"{cohort}.data", personal["data"]),
                        _prediction_row(row, f"{cohort}.hybrid", personal["hybrid"]),
                    )
                )
                personalization.append(
                    {
                        "participant_id": participant,
                        "cutoff": row["cutoff"].isoformat(),
                        "cohort": cohort,
                        "history_count": int(state["valid_days"]),
                        "state": state,
                        "features": features,
                    }
                )
                if baseline_values is None and features["history_count_7"] > 0.0:
                    baseline_values = (features["recent_bed"], features["recent_wake"])
            predictions.append(_prediction_row(row, "baseline.recent7", baseline_values))
    return predictions, personalization


def _metric_row(model_id, rows, keys):
    selected = [
        row
        for row in rows
        if (row["participant_id"], row["cutoff"]) in keys
        and row["onset_offset_min"] is not None
        and row["actual_onset_offset_min"] is not None
    ]
    template = {
        "model_id": model_id,
        "n": len(selected),
        "n_participants": len({row["participant_id"] for row in selected}),
        "averaging": "participant_equal",
        "onset_mae_min": None,
        "wake_mae_min": None,
        "interval_mae_min": None,
        "joint_mae_min": None,
        "onset_rmse_min": None,
        "wake_rmse_min": None,
        "skill_vs_recent7": None,
    }
    if not selected:
        return template
    participant_rows = defaultdict(list)
    for row in selected:
        participant_rows[row["participant_id"]].append(row)
    per_participant = []
    for participant in sorted(participant_rows):
        values = participant_rows[participant]
        onset_errors = [
            row["onset_offset_min"] - row["actual_onset_offset_min"] for row in values
        ]
        wake_errors = [
            row["wake_offset_min"] - row["actual_wake_offset_min"] for row in values
        ]
        interval_errors = [
            row["interval_min"] - row["actual_interval_min"] for row in values
        ]
        per_participant.append(
            {
                "onset_mae_min": mean(abs(value) for value in onset_errors),
                "wake_mae_min": mean(abs(value) for value in wake_errors),
                "interval_mae_min": mean(abs(value) for value in interval_errors),
                "joint_mae_min": mean(
                    (abs(onset) + abs(wake)) / 2.0
                    for onset, wake in zip(onset_errors, wake_errors)
                ),
                "onset_rmse_min": math.sqrt(mean(value * value for value in onset_errors)),
                "wake_rmse_min": math.sqrt(mean(value * value for value in wake_errors)),
            }
        )
    for field in (
        "onset_mae_min",
        "wake_mae_min",
        "interval_mae_min",
        "joint_mae_min",
        "onset_rmse_min",
        "wake_rmse_min",
    ):
        template[field] = mean(item[field] for item in per_participant)
    return template


def _metrics(predictions, model_ids):
    by_model = {
        model_id: [row for row in predictions if row["model_id"] == model_id]
        for model_id in model_ids
    }
    available_keys = {}
    for model_id, rows in by_model.items():
        available_keys[model_id] = {
            (row["participant_id"], row["cutoff"])
            for row in rows
            if row["onset_offset_min"] is not None
            and row["actual_onset_offset_min"] is not None
        }
    common = set.intersection(*(available_keys[model_id] for model_id in model_ids))
    primary = [
        _metric_row(model_id, by_model[model_id], common) for model_id in model_ids
    ]
    baseline = next(item for item in primary if item["model_id"] == "baseline.recent7")
    baseline_joint = baseline["joint_mae_min"]
    if baseline_joint is not None and baseline_joint > 0.0:
        for item in primary:
            if item["joint_mae_min"] is not None:
                item["skill_vs_recent7"] = 1.0 - item["joint_mae_min"] / baseline_joint
    available = [
        _metric_row(model_id, by_model[model_id], available_keys[model_id])
        for model_id in model_ids
    ]
    return primary, available, len(common)


def _canonical_rows(rows):
    return [
        {
            "participant_id": row["participant_id"],
            "cutoff": row["cutoff"].isoformat(),
            "sleep_start": row["sleep_start"].isoformat() if row["sleep_start"] else None,
            "sleep_end": row["sleep_end"].isoformat() if row["sleep_end"] else None,
            "observed_at": row["observed_at"].isoformat() if row["observed_at"] else None,
        }
        for row in rows
    ]


def _run_id(mode, cohorts, rows, synthetic, chart_participant, context=None):
    digest_input = {
        "personalization_policy": GRADUAL_POLICY,
        "runtime_model_version": RUNTIME_MODEL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "cohorts": cohorts,
        "chart_participant_id": chart_participant,
        "records": _canonical_rows(rows),
        "synthetic": synthetic,
    }
    if context:
        digest_input["context"] = context
    encoded = json.dumps(
        digest_input,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_contract():
    return {
        "schema_version": SCHEMA_VERSION,
        "canonical_fields": list(CANONICAL_FIELDS),
        "decision_time": "20:00 Asia/Seoul",
        "target": "next main-sleep onset and final wake within the declared offset support",
        "outcome_availability": (
            "sleep_start, sleep_end, and observed_at are either all explicit or all null; "
            "null means unavailable, never zero"
        ),
        "history_rule": (
            "Per-participant research-source rows enter history only when sleep_end < cutoff "
            "and observed_at < cutoff; recent7 is night_core's last 7 within 28 days"
        ),
        "enrollment_rule": "The participant's first supplied cutoff is dataset-start enrollment.",
        "personalization_policy": GRADUAL_POLICY,
        "personalization_rule": (
            "첫 유효 기록부터 n/(n+14) 비중으로 개인화합니다. 14는 초기값의 가중치이며 "
            "대기 일수가 아닙니다. 가용 기록이 없으면 초기값을 사용합니다. "
            "기존 동결 가중치에 새 추론 정책을 적용하므로 legacy14 검증 수치와 구분합니다."
        ),
        "fixed_physical_role": (
            "위상·임계값 고정 물리 uses the cohort prior with personalize=False; homeostatic S "
            "still reconstructs actual eligible past history, so it is not a no-personal-data model"
        ),
        "input_roles": {
            "participant_id": "Pseudonymous grouping key only.",
            "cutoff": "Exactly 20:00 KST prediction decision.",
            "sleep_start": "Outcome at its cutoff and eligible history only after availability.",
            "sleep_end": "Outcome at its cutoff and strict-past wake boundary for later cutoffs.",
            "observed_at": "Availability boundary supplied by the researcher; not authenticated here.",
            "heart_rate": "Not an input of these frozen models.",
            "activity": "Not an input of these frozen models.",
            "caffeine": "Not an input of these frozen models.",
        },
        "physiological_assumptions": {
            "tau_wake_h": TAU_WAKE,
            "tau_sleep_h": TAU_SLEEP,
            "circadian_amplitude": C_AMPLITUDE,
            "role": "Fixed engineering assumptions; they are not fitted personal physiology.",
        },
        "metrics": {
            "primary": (
                "Participant-equal errors on identical labeled nights where every selected arm "
                "and recent7 are available."
            ),
            "available_metrics": (
                "Participant-equal errors on every labeled prediction available to each model; "
                "skill is null because model-specific night sets are not a same-night comparison."
            ),
        },
        "persistence": "Stateless in-memory evaluation; no training, uploads, profile writes, or store writes.",
    }


def evaluate_run(payload, models_dir=None):
    """Validate, replay, and compare a real retrospective or synthetic lab run."""
    started = perf_counter()
    mode, cohorts, rows, synthetic, chart_participant = _validate_payload(payload)
    try:
        context = normalize_context(payload.get("context", []), {row["participant_id"] for row in rows})
        daily_features, context_summary = context_features(rows, context)
    except (ValueError, TypeError, OverflowError) as error:
        raise LabInputError(str(error)) from error
    models = _load_models(cohorts, models_dir)
    model_metadata = _model_metadata(models)
    predictions, personalization = _replay(rows, models)
    model_ids = [item["id"] for item in model_metadata]
    metrics, available_metrics, common_nights = _metrics(predictions, model_ids)
    participants = sorted({row["participant_id"] for row in rows})
    labeled_nights = sum(row["sleep_start"] is not None for row in rows)
    evidence_label = (
        "retrospective_user_input_replay" if mode == "real" else "synthetic_demo"
    )
    warnings = [
        (
            "현재 예측은 첫 유효 기록부터 점진적으로 개인화합니다(gradual_v1). "
            "기존 학습 가중치를 유지한 추론 정책 변경이며, 저장된 legacy14 Test 성능은 "
            "이 정책의 검증 결과가 아닙니다. 아래 수치는 현재 입력을 다시 평가한 결과입니다."
        ),
        (
            "Privacy: use pseudonymous participant IDs. Inputs are processed in memory and are not "
            "uploaded or written to profiles, stores, source data, or model artifacts."
        ),
        (
            "Research comparison only: this result is not a clinical conclusion, treatment effect, "
            "production recommendation, or prospective proof."
        ),
        (
            "Frozen models do not use heart rate, activity, or caffeine, and their physiological "
            "time constants are fixed rather than fitted to a participant."
        ),
    ]
    if mode == "real":
        warnings.append(
            "Evidence scope: retrospective replay of user-provided rows; timestamps and availability were not independently authenticated."
        )
    else:
        warnings.append(
            "Evidence scope: explicitly synthetic demonstration generated independently of model predictions; it is not real-world accuracy evidence."
        )
    if labeled_nights < len(rows):
        warnings.append(
            "Prediction-only rows have unavailable outcomes and are excluded from error metrics; unavailable values are never treated as zero."
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "personalization_policy": GRADUAL_POLICY,
        "runtime_model_version": RUNTIME_MODEL_VERSION,
        "mode": mode,
        "evidence_label": evidence_label,
        "run_id": _run_id(mode, cohorts, rows, synthetic, chart_participant, context),
        "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
        "summary": {
            "participants": participants,
            "rows": len(rows),
            "labeled_nights": labeled_nights,
            "common_nights": common_nights,
            "chart_participant_id": chart_participant,
        },
        "models": model_metadata,
        "metrics": metrics,
        "available_metrics": available_metrics,
        "predictions": predictions,
        "personalization": personalization,
        "context": context,
        "context_features": daily_features,
        "context_summary": context_summary,
        "observations": describe_observations(rows),
        "input_contract": _input_contract(),
        "warnings": warnings,
    }
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise LabInputError(
            "Evaluation produced a non-finite or non-serializable value.", "inference_failed"
        ) from error
    return result
