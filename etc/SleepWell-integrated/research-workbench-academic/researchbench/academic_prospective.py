"""Native prospective and synthetic-playback adapter for the academic edition.

The numerical forecast and evaluation contracts remain owned by the vendored
SleepWell modules.  This module only supplies a desktop-friendly JSON envelope
and an append-only SQLite journal for the two-stage forecast/outcome workflow.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable


Progress = Callable[[dict[str, Any]], None]
Cancelled = Callable[[], bool]


def _modules() -> dict[str, Any]:
    from .academic_backend import vendor_module

    return {name: vendor_module(name) for name in (
        "night_core", "fusion_forecast", "prospective", "online_trial"
    )}


def default_profile() -> dict[str, Any]:
    """Return the visible, editable default used by the academic demo."""
    return {
        "name": "academic-demo-participant",
        "age": 30,
        "sex": "unspecified",
        "occupation": "research participant",
        "usual_bed": "23:30",
        "usual_wake": "07:00",
        "target_wake": "07:00",
        "weekend_wake": "08:00",
        "usual_sleep_h": 7.5,
        "desired_sleep_h": 8.0,
        "average_steps": 5000,
        "exercise_min": 30,
        "latency_min": 20,
        "activity_hours_per_day": 1,
        "link_history": False,
        "created_at": "2020-01-01T00:00:00+09:00",
        "enrollment_id": "academic-demo-enrollment",
    }


def sample_configs(journal_path: str | Path) -> dict[str, dict[str, Any]]:
    """Return form-ready examples without generating a spoofed commit time."""
    journal = str(Path(journal_path).expanduser().resolve())
    return {
        "prospective_demo": {
            "mode": "prospective_demo", "n_nights": 50, "seed": 42,
            "start_date": "2026-01-01", "online_personalization": True,
            "profile": default_profile(),
        },
        "prospective_commit": {
            "mode": "prospective_commit", "journal_path": journal,
            "cutoff": None, "records": [], "context": {},
            "online_personalization": True, "profile": default_profile(),
        },
        "prospective_outcome": {
            "mode": "prospective_outcome", "journal_path": journal,
            "forecast_id": "<select from list_forecasts>",
            "outcome": {"sleep_start": "<timezone-aware ISO datetime>",
                        "wake": "<timezone-aware ISO datetime>",
                        "observed_at": "<timezone-aware ISO datetime>",
                        "source": "diary"},
        },
        "prospective_summary": {
            "mode": "prospective_summary", "journal_path": journal,
        },
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_sha(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _journal_path(value: Any) -> Path:
    if not value:
        raise ValueError("journal_path is required")
    path = Path(value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _connect(path: Path):
    db = sqlite3.connect(path, timeout=20)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS prospective_forecasts (
                forecast_id TEXT PRIMARY KEY,
                night_id TEXT NOT NULL UNIQUE,
                generated_at TEXT NOT NULL,
                input_hash TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prospective_assessments (
                assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecast_id TEXT NOT NULL,
                outcome_hash TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(forecast_id, outcome_hash),
                FOREIGN KEY(forecast_id) REFERENCES prospective_forecasts(forecast_id)
            );
            """
        )
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _read_forecasts(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT forecast_id,payload FROM prospective_forecasts ORDER BY generated_at,forecast_id"
    )
    return [dict(json.loads(row["payload"]), id=row["forecast_id"]) for row in rows]


def _read_assessments(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT assessment_id,forecast_id,payload FROM prospective_assessments ORDER BY assessment_id"
    )
    return [dict(json.loads(row["payload"]), assessment_id=row["assessment_id"],
                 forecast_id=row["forecast_id"]) for row in rows]


def list_forecasts(journal_path: str | Path) -> list[dict[str, Any]]:
    """List immutable forecast metadata without exposing large snapshots."""
    path = _journal_path(journal_path)
    with _connect(path) as db:
        return [{
            "forecast_id": row["id"],
            "night_id": row.get("night_id"),
            "generated_at": row.get("generated_at"),
            "eligible_capture": row.get("eligible_capture"),
            "capture_reason": row.get("capture_reason"),
            "model_ids": sorted((row.get("predictions") or {}).keys()),
            "has_outcome": bool(db.execute(
                "SELECT 1 FROM prospective_assessments WHERE forecast_id=? LIMIT 1", (row["id"],)
            ).fetchone()),
        } for row in _read_forecasts(db)]


def _progress(callback: Progress | None, stage: str, current: int = 0, total: int = 0) -> None:
    if callback:
        callback({"stage": stage, "current": current, "total": total})


def _check_cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        from .academic_backend import AcademicRunCancelled

        raise AcademicRunCancelled("cancelled")


def _model_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "vendor" / "sleepwell" / "models"


def _load_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    if "records" in config:
        if not isinstance(config["records"], list):
            raise ValueError("records must be a list")
        return [dict(item) for item in config["records"]]
    filename = config.get("csv_file")
    if not filename:
        return []
    with Path(filename).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _profile(config: dict[str, Any]) -> dict[str, Any]:
    profile = default_profile()
    supplied = config.get("profile") or {}
    if not isinstance(supplied, dict):
        raise ValueError("profile must be an object")
    profile.update(supplied)
    return profile


def _now(zone: Any) -> datetime:
    return datetime.now(zone)


def _cutoff(value: Any, now: datetime, night_core: Any) -> datetime:
    if value is None:
        return now.replace(hour=20, minute=0, second=0, microsecond=0)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time(20, 0), tzinfo=night_core.ZONE)
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time(20, 0), tzinfo=night_core.ZONE)
        parsed = night_core.local_time(text)
    except (TypeError, ValueError) as error:
        raise ValueError("cutoff must be an ISO date or timezone-aware datetime") from error
    if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) != (20, 0, 0, 0):
        raise ValueError("cutoff must resolve to exactly 20:00")
    return parsed


def _provenance(modules: dict[str, Any], fusion: Any | None = None) -> dict[str, Any]:
    sources = {name: _file_sha(module.__file__) for name, module in modules.items()}
    models = {}
    if fusion is not None:
        for cohort, item in fusion.status().get("cohorts", {}).items():
            if item.get("loaded"):
                models[cohort] = item.get("sha256")
    else:
        for cohort in ("cmi", "mesa"):
            path = _model_dir() / f"robust_{cohort}_v2.json"
            if path.is_file():
                models[cohort] = _file_sha(path)
    return {
        "adapter_sha256": _file_sha(__file__),
        "source_sha256": sources,
        "model_sha256": models,
        "journal_contract": "append_only_sqlite_v1",
    }


def _flatten(summary: dict[str, Any], assessments: list[dict[str, Any]],
             online_trial: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for version in summary.get("versions", []):
        for stratum, source in version.get("source_strata", {}).items():
            for model_id, metrics in source.get("arms", {}).items():
                rows.append({
                    "model_id": model_id,
                    "onset_mae_min": metrics.get("onset_mae_min"),
                    "wake_mae_min": metrics.get("wake_mae_min"),
                    "joint_mae_min": metrics.get("joint_mae_min"),
                    "interval_mae_min": metrics.get("interval_mae_min"),
                    "n_nights": metrics.get("n", 0),
                    "n_participants": 1,
                    "source_stratum": stratum,
                    "model_version": version.get("model_version"),
                    "fusion_artifact_identity_sha256": version.get(
                        "fusion_artifact_identity_sha256"
                    ),
                })
    online = online_trial.summarize_online_trials(assessments)
    for version in online.get("versions", []):
        for model_id, metrics in version.get("arms", {}).items():
            rows.append({
                "model_id": f"online_{model_id}",
                "onset_mae_min": None,
                "wake_mae_min": None,
                "joint_mae_min": metrics.get("corrected_joint_mae_min"),
                "interval_mae_min": None,
                "n_nights": metrics.get("n", 0),
                "n_participants": 1,
                "source_stratum": "user_reported_primary",
                "model_version": version.get("policy_version"),
                "fusion_artifact_identity_sha256": version.get(
                    "base_artifact_identity_sha256"
                ),
                "base_joint_mae_min": metrics.get("base_joint_mae_min"),
                "paired_improvement_min": metrics.get("paired_improvement_min"),
            })
    return rows


def _summary(forecasts: list[dict[str, Any]], assessments: list[dict[str, Any]],
             modules: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = modules["prospective"].summarize_evaluations(forecasts, assessments)
    return raw, _flatten(raw, assessments, modules["online_trial"])


def _commit(config: dict[str, Any], modules: dict[str, Any]) -> tuple[Any, Any, list, list]:
    path = _journal_path(config.get("journal_path"))
    now = _now(modules["night_core"].ZONE)
    cutoff = _cutoff(config.get("cutoff"), now, modules["night_core"])
    fusion = modules["fusion_forecast"].FusionForecasts(_model_dir())
    fusion_result = fusion.predict_at(_profile(config), _load_records(config), cutoff)
    if not fusion_result.get("available"):
        raise ValueError(f"fusion models unavailable: {fusion_result.get('reason')}")
    with _connect(path) as db:
        forecasts, assessments = _read_forecasts(db), _read_assessments(db)
        artifact = modules["prospective"].build_forecast(
            _profile(config), _load_records(config), config.get("context") or {}, cutoff, now,
            fusion_result=fusion_result,
            online_forecasts=forecasts if config.get("online_personalization", True) else None,
            online_assessments=assessments,
        )
        forecast_id = "pf_" + artifact["input_hash"][:24]
        payload = _json(artifact)
        existed = db.execute(
            "SELECT 1 FROM prospective_forecasts WHERE night_id=?", (artifact["night_id"],)
        ).fetchone() is not None
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO prospective_forecasts VALUES(?,?,?,?,?)",
            (forecast_id, artifact["night_id"], artifact["generated_at"],
             artifact["input_hash"], payload),
        )
        stored = db.execute(
            "SELECT forecast_id,payload FROM prospective_forecasts WHERE night_id=?",
            (artifact["night_id"],),
        ).fetchone()
        stored_artifact = dict(json.loads(stored["payload"]), id=stored["forecast_id"])
        forecasts, assessments = _read_forecasts(db), _read_assessments(db)
    raw = {"forecast": stored_artifact,
           "idempotent_existing": existed}
    return raw, fusion, forecasts, assessments


def _outcome(config: dict[str, Any], modules: dict[str, Any]) -> tuple[Any, None, list, list]:
    path = _journal_path(config.get("journal_path"))
    forecast_id = config.get("forecast_id")
    if not forecast_id:
        raise ValueError("forecast_id is required")
    outcome = config.get("outcome")
    if outcome is None:
        rows = _load_records(config)
        if len(rows) != 1:
            raise ValueError("prospective_outcome requires exactly one outcome")
        outcome = rows[0]
    if not isinstance(outcome, dict):
        raise ValueError("outcome must be an object")
    outcome_hash = _sha(outcome)
    with _connect(path) as db:
        row = db.execute(
            "SELECT payload FROM prospective_forecasts WHERE forecast_id=?", (forecast_id,)
        ).fetchone()
        if row is None:
            raise ValueError("forecast_id was not found")
        forecast = dict(json.loads(row["payload"]), id=forecast_id)
        evaluation = modules["prospective"].evaluate_forecast(forecast, dict(outcome))
        evaluation["forecast_id"] = forecast_id
        evaluation["outcome_hash"] = outcome_hash
        existed = db.execute(
            "SELECT 1 FROM prospective_assessments WHERE forecast_id=? AND outcome_hash=?",
            (forecast_id, outcome_hash),
        ).fetchone() is not None
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO prospective_assessments"
            "(forecast_id,outcome_hash,observed_at,payload) VALUES(?,?,?,?)",
            (forecast_id, outcome_hash, str(outcome.get("observed_at") or ""), _json(evaluation)),
        )
        assessment = db.execute(
            "SELECT assessment_id,payload FROM prospective_assessments "
            "WHERE forecast_id=? AND outcome_hash=?", (forecast_id, outcome_hash)
        ).fetchone()
        stored = dict(json.loads(assessment["payload"]),
                      assessment_id=assessment["assessment_id"], forecast_id=forecast_id)
        forecasts, assessments = _read_forecasts(db), _read_assessments(db)
    return {"assessment": stored, "eligible": bool(stored.get("eligible")),
            "idempotent_existing": existed}, None, forecasts, assessments


def _demo(config: dict[str, Any], modules: dict[str, Any], progress: Progress | None,
          cancelled: Cancelled | None) -> tuple[Any, Any, list, list]:
    seed = int(config.get("seed", 42))
    jitter = float(config.get("jitter_minutes", 45.0))
    phase_shift = float(config.get("phase_shift_minutes", 60.0))
    rng = random.Random(seed)
    profile = _profile(config)
    fusion = modules["fusion_forecast"].FusionForecasts(_model_dir())
    forecasts: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    imported = bool(config.get("csv_file"))
    if imported:
        if config.get("input_synthetic") is not True:
            raise ValueError("prospective_demo CSV requires input_synthetic=true")
        rows = _load_records(config)
        participants = {row.get("participant_id") for row in rows if row.get("participant_id")}
        if len(participants) > 1:
            raise ValueError("prospective_demo supports one synthetic participant per run")
        truth = []
        for row in rows:
            cutoff = _cutoff(row.get("cutoff"), datetime.now(modules["night_core"].ZONE),
                              modules["night_core"])
            truth.append({
                "cutoff": cutoff,
                "sleep_start": row.get("sleep_start") or None,
                "wake": row.get("sleep_end") or row.get("wake") or None,
                "observed_at": row.get("observed_at") or None,
                "source": row.get("source") or "diary",
            })
        truth.sort(key=lambda row: row["cutoff"])
        if len({row["cutoff"] for row in truth}) != len(truth):
            raise ValueError("synthetic CSV cutoffs must be unique")
        input_metadata = {
            "source": "declared_synthetic_csv",
            "csv_file": str(Path(config["csv_file"]).resolve()),
            "input_sha256": _file_sha(config["csv_file"]),
        }
    else:
        count = int(config.get("n_nights", 50))
        if not 1 <= count <= 500:
            raise ValueError("n_nights must be between 1 and 500")
        start = date.fromisoformat(str(config.get("start_date", "2026-01-01")))
        shift_after = int(config.get("shift_after_night", count // 2))
        truth = []
        for index in range(count):
            cutoff = datetime.combine(start + timedelta(days=index), time(20, 0),
                                      tzinfo=modules["night_core"].ZONE)
            phase = phase_shift if index >= shift_after else 0.0
            onset = min(960.0, max(0.0, 210.0 + phase + rng.uniform(-jitter, jitter)))
            duration = min(900.0, max(60.0, 480.0 + rng.uniform(-jitter / 4, jitter / 4)))
            wake = min(1440.0, onset + duration)
            truth.append({
                "cutoff": cutoff,
                "sleep_start": (cutoff + timedelta(minutes=onset)).isoformat(),
                "wake": (cutoff + timedelta(minutes=wake)).isoformat(),
                "observed_at": (cutoff + timedelta(minutes=wake + 60)).isoformat(),
                "source": "diary",
            })
        input_metadata = {
            "source": "seeded_generator", "n_nights": count, "seed": seed,
            "start_date": start.isoformat(), "phase_shift_minutes": phase_shift,
            "shift_after_night": shift_after,
        }
    count = len(truth)
    if not 1 <= count <= 500:
        raise ValueError("synthetic playback must contain between 1 and 500 nights")
    for index, truth_row in enumerate(truth):
        _check_cancelled(cancelled)
        cutoff = truth_row["cutoff"]
        generated = cutoff + timedelta(minutes=5)
        fusion_result = fusion.predict_at(profile, history, cutoff)
        if not fusion_result.get("available"):
            raise ValueError(f"fusion models unavailable: {fusion_result.get('reason')}")
        forecast = modules["prospective"].build_forecast(
            profile, history, {}, cutoff, generated, fusion_result=fusion_result,
            online_forecasts=forecasts if config.get("online_personalization", True) else None,
            online_assessments=assessments,
        )
        forecast["id"] = f"demo-{index + 1:04d}"
        forecasts.append(forecast)
        if not all(truth_row.get(key) for key in ("sleep_start", "wake", "observed_at")):
            _progress(progress, "synthetic_chronological_playback", index + 1, count)
            continue
        outcome = {
            "sleep_start": truth_row["sleep_start"],
            "wake": truth_row["wake"],
            "observed_at": truth_row["observed_at"],
            "source": truth_row["source"],
            "enrollment_id": profile.get("enrollment_id"),
        }
        evaluation = modules["prospective"].evaluate_forecast(forecast, outcome)
        evaluation.update({"forecast_id": forecast["id"], "assessment_id": index + 1})
        assessments.append(evaluation)
        history.append({
            "bed": outcome["sleep_start"], "wake": outcome["wake"],
            "sleep_start": outcome["sleep_start"], "observed_at": outcome["observed_at"],
            "source": "diary", "enrollment_id": profile.get("enrollment_id"),
        })
        _progress(progress, "synthetic_chronological_playback", index + 1, count)
    return {"forecasts": forecasts, "assessments": assessments,
            "synthetic_config": input_metadata}, fusion, forecasts, assessments


def run_prospective(config: dict[str, Any], progress: Progress | None = None,
                    cancelled: Cancelled | None = None) -> dict[str, Any]:
    """Run one native prospective operation and return a JSON-safe envelope."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    mode = config.get("mode")
    if mode not in {"prospective_demo", "prospective_commit", "prospective_outcome",
                    "prospective_summary"}:
        raise ValueError("unsupported prospective mode")
    modules = _modules()
    _check_cancelled(cancelled)
    _progress(progress, "starting", 0, 1)
    if mode == "prospective_demo":
        raw, fusion, forecasts, assessments = _demo(config, modules, progress, cancelled)
        warnings = [
            "Synthetic chronological playback; this is not genuine future validation.",
            "Generated outcomes are independent demonstrations, not observed participant data.",
        ]
    elif mode == "prospective_commit":
        raw, fusion, forecasts, assessments = _commit(config, modules)
        warnings = ["Forecast was committed before outcome import; later outcomes cannot alter it."]
    elif mode == "prospective_outcome":
        raw, fusion, forecasts, assessments = _outcome(config, modules)
        warnings = ["Outcome assessments are append-only; the first eligible report is primary."]
    else:
        path = _journal_path(config.get("journal_path"))
        with _connect(path) as db:
            forecasts, assessments = _read_forecasts(db), _read_assessments(db)
        raw, fusion = {"journal": str(path)}, None
        warnings = ["Pending and ineligible forecasts remain visible and are never scored as zero."]
    aggregate, rows = _summary(forecasts, assessments, modules)
    raw["aggregate"] = aggregate
    _progress(progress, "complete", 1, 1)
    result = {
        "mode": mode,
        "raw": raw,
        "summary": rows,
        "provenance": _provenance(modules, fusion),
        "warnings": warnings,
    }
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result

