"""Clock-driven, participant-isolated prospective diary validation service.

This service coordinates the existing immutable forecast journals.  It never
backdates forecasts, exposes model predictions to participants, or treats a
sleep diary as a wearable/PSG reference outcome.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import secrets
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from random import Random
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .participant_prospective import ParticipantProspective


SCHEMA = "sleepwell.prospective_diary_study.v1"
ZONE = ZoneInfo("Asia/Seoul")
GOVERNANCE_FIELDS = (
    "ethics_reference", "consent_version", "responsible_researcher", "recruitment_start"
)
PRIMARY_ARMS = ("data_cmi", "data_mesa")
SECONDARY_ARMS = (
    "hybrid_cmi", "hybrid_mesa", "physical_cmi", "physical_mesa",
    "recent_adaptive", "profile_baseline",
)
BASELINE_CODES = {
    "sex": frozenset(("female", "male", "intersex", "another", "prefer_not_to_say", "unknown")),
    "shift_work": frozenset(("yes", "no", "prefer_not_to_say", "unknown")),
    "sleep_medication": frozenset(("yes", "no", "prefer_not_to_say", "unknown")),
    "sleep_diagnosis": frozenset(("yes", "no", "prefer_not_to_say", "unknown")),
    "device_access": frozenset(("personal", "shared", "limited", "prefer_not_to_say", "unknown")),
    "recruitment_source": frozenset(("school", "university", "clinic", "community", "online",
                                      "workplace", "other", "prefer_not_to_say", "unknown")),
}


def _now() -> datetime:
    return datetime.now(ZONE)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(ZONE)


def _clock(value: Any, field: str) -> str:
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"profile.{field} must be HH:MM") from error
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ValueError(f"profile.{field} must be a local HH:MM value")
    return parsed.strftime("%H:%M")


def _validated_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = dict(value)
    required = ("age", "usual_bed", "usual_wake", "target_wake", "weekend_wake",
                "usual_sleep_h", "desired_sleep_h", "latency_min",
                "average_steps", "exercise_min")
    missing = [key for key in required if profile.get(key) in (None, "")]
    if missing:
        raise ValueError("metadata.profile is missing: " + ", ".join(missing))
    for field in ("usual_bed", "usual_wake", "target_wake", "weekend_wake"):
        profile[field] = _clock(profile[field], field)
    limits = {
        "age": (18, 65), "usual_sleep_h": (1, 16), "desired_sleep_h": (1, 16),
        "latency_min": (0, 480), "average_steps": (0, 100000),
        "exercise_min": (0, 1440),
    }
    for field, (lower, upper) in limits.items():
        try:
            number = float(profile[field])
        except (TypeError, ValueError) as error:
            raise ValueError(f"profile.{field} must be numeric") from error
        if not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError(f"profile.{field} must be between {lower} and {upper}")
        profile[field] = int(number) if field in ("age", "average_steps", "exercise_min") else number
    return profile


def _validated_baseline(value: Mapping[str, Any]) -> dict[str, Any]:
    baseline = {}
    missing = [field for field in BASELINE_CODES if value.get(field) in (None, "")]
    if missing:
        raise ValueError("metadata baseline is missing: " + ", ".join(missing))
    for field, allowed in BASELINE_CODES.items():
        code = str(value[field]).strip().lower()
        if code not in allowed:
            raise ValueError(f"metadata.{field} must be one of: " + ", ".join(sorted(allowed)))
        baseline[field] = code
    return baseline


def _age_group(age: float) -> str:
    return "18-29" if age < 30 else "30-44" if age < 45 else "45-65"


def _protocol(value: Mapping[str, Any], synthetic: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("protocol must be an object")
    result = dict(value)
    phase = str(result.get("phase") or "main_validation")
    if phase not in ("main_validation", "feasibility"):
        raise ValueError("protocol phase must be main_validation or feasibility")
    phase_fixed = ({"duration_days": 28, "adaptation_days": 7, "primary_days": 21,
                    "minimum_primary_nights_for_completer": 14}
                   if phase == "main_validation" else
                   {"duration_days": 14, "adaptation_days": 7, "primary_days": 7,
                    "minimum_primary_nights_for_completer": 5})
    fixed = {
        **phase_fixed,
        "primary_receipt_hours": 12,
        "sensitivity_receipt_hours": 24,
        "online_personalization": False,
        "outcome_kind": "user_reported_sleep_diary",
        "prediction_visibility": "withheld_during_accuracy_validation",
        "primary_arms": list(PRIMARY_ARMS),
        "secondary_arms": list(SECONDARY_ARMS),
    }
    for key, expected in fixed.items():
        if key in result and result[key] != expected:
            raise ValueError(f"protocol {key} is fixed at {expected!r}")
        result[key] = expected
    if result.get("recruitment_start"):
        try:
            date.fromisoformat(str(result["recruitment_start"]))
        except ValueError as error:
            raise ValueError("recruitment_start must be an ISO date") from error
    result.update({"schema": SCHEMA, "phase": phase, "synthetic_rehearsal": bool(synthetic),
                   "pooling_policy": "separate_phase_never_pool"})
    return result


class ProspectiveStudy:
    """Append-only real-study ledger over :class:`ParticipantProspective`."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.db_path = self.root / "prospective_study.sqlite3"
        self.journal_root = self.root / "participant_journals"

    @contextmanager
    def _db(self):
        if not self.db_path.exists():
            raise ValueError("study has not been created")
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _source_paths(self) -> list[Path]:
        package = Path(__file__).resolve().parent
        vendor = package.parent / "vendor" / "sleepwell"
        paths = [
            Path(__file__).resolve(), package / "academic_prospective.py",
            package / "participant_prospective.py", vendor / "prospective.py",
            vendor / "fusion_forecast.py", vendor / "night_core.py",
            vendor / "engine.py", vendor / "night_forecast.py",
            vendor / "personalization.py", vendor / "adaptive_personalization.py",
            vendor / "online_trial.py", vendor / "SOURCE_MANIFEST.json",
            vendor / "models" / "robust_cmi_v2.json",
            vendor / "models" / "robust_mesa_v2.json",
        ]
        paths.extend(path for path in (package / "diary_server.py",
                                       package / "resources" / "prospective_diary.html")
                     if path.is_file())
        return paths

    def _manifest(self) -> dict[str, str]:
        package_root = Path(__file__).resolve().parents[1]
        return {path.relative_to(package_root).as_posix(): _sha_file(path)
                for path in self._source_paths()}

    def _study(self, db: sqlite3.Connection) -> dict[str, Any]:
        row = db.execute("SELECT protocol,manifest,synthetic,created_at FROM study WHERE id=1").fetchone()
        if row is None:
            raise ValueError("study metadata is missing")
        return {"protocol": json.loads(row["protocol"]), "manifest": json.loads(row["manifest"]),
                "synthetic": bool(row["synthetic"]), "created_at": row["created_at"]}

    def create(self, protocol: Mapping[str, Any], synthetic: bool = False) -> dict[str, Any]:
        if self.db_path.exists():
            raise FileExistsError("study already exists")
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal_root.mkdir(parents=True, exist_ok=True)
        frozen = _protocol(protocol, synthetic)
        created = _now().isoformat()
        db = sqlite3.connect(self.db_path)
        try:
            db.executescript(
                """
                CREATE TABLE study(id INTEGER PRIMARY KEY CHECK(id=1), protocol TEXT NOT NULL,
                  manifest TEXT NOT NULL, synthetic INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE participants(participant_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                  enrolled_at TEXT NOT NULL, start_date TEXT NOT NULL, metadata TEXT NOT NULL,
                  withdrawn_at TEXT);
                CREATE TABLE diaries(receipt_id TEXT PRIMARY KEY, participant_id TEXT NOT NULL,
                  night_date TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL,
                  valid INTEGER NOT NULL, missing INTEGER NOT NULL, primary_revision INTEGER NOT NULL,
                  timing_class TEXT NOT NULL, forecast_status TEXT NOT NULL,
                  FOREIGN KEY(participant_id) REFERENCES participants(participant_id));
                CREATE INDEX diaries_person_night ON diaries(participant_id,night_date,received_at);
                CREATE TABLE events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                  kind TEXT NOT NULL, participant_id TEXT, night_date TEXT, detail TEXT NOT NULL);
                """
            )
            db.execute("INSERT INTO study VALUES(1,?,?,?,?)",
                       (_json(frozen), _json(self._manifest()), int(synthetic), created))
            db.execute("INSERT INTO events(occurred_at,kind,detail) VALUES(?,?,?)",
                       (created, "study_created", _json({"synthetic_rehearsal": synthetic})))
            db.commit()
        finally:
            db.close()
        missing = [key for key in GOVERNANCE_FIELDS if not str(frozen.get(key) or "").strip()]
        return {"schema": SCHEMA, "created_at": created, "synthetic_rehearsal": synthetic,
                "ready_for_enrollment": bool(synthetic or not missing),
                "missing_governance_fields": missing, "protocol_sha256": _sha_bytes(_json(frozen).encode())}

    @staticmethod
    def _governance(study: Mapping[str, Any]) -> None:
        if study["synthetic"]:
            return
        missing = [key for key in GOVERNANCE_FIELDS
                   if not str(study["protocol"].get(key) or "").strip()]
        if missing:
            raise ValueError("real enrollment is blocked; missing governance fields: " + ", ".join(missing))

    def enroll(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be an object")
        now = _now()
        with self._db() as db:
            study = self._study(db)
            self._governance(study)
            if not study["synthetic"] and metadata.get("consent_confirmed") is not True:
                raise ValueError("consent_confirmed=True is required for real enrollment")
            recruitment = study["protocol"].get("recruitment_start")
            if recruitment and now.date() < date.fromisoformat(recruitment):
                raise ValueError("recruitment has not started")
            participant_id = "ps_" + secrets.token_hex(12)
            token = secrets.token_urlsafe(32)
            token_hash = _sha_bytes(token.encode())
            clean = dict(metadata)
            clean.pop("token", None)
            if "age" in clean:
                raise ValueError("metadata.age is not allowed; provide age only in metadata.profile")
            profile = clean.get("profile")
            if not isinstance(profile, Mapping):
                raise ValueError("metadata.profile is required")
            clean["profile"] = _validated_profile(profile)
            clean.update(_validated_baseline(clean))
            start_date = now.date() + timedelta(days=int(now.hour >= 20))
            db.execute("INSERT INTO participants VALUES(?,?,?,?,?,NULL)",
                       (participant_id, token_hash, now.isoformat(), start_date.isoformat(), _json(clean)))
            db.execute("INSERT INTO events(occurred_at,kind,participant_id,detail) VALUES(?,?,?,?)",
                       (now.isoformat(), "participant_enrolled", participant_id,
                        _json({"consent_version": study["protocol"].get("consent_version")})))
        return {"schema": SCHEMA, "participant_id": participant_id, "token": token,
                "enrolled_at": now.isoformat(), "study_start_date": start_date.isoformat(),
                "synthetic_rehearsal": study["synthetic"]}

    def withdraw(self, token: str) -> dict[str, Any]:
        """Stop future captures while preserving already collected audit records."""
        now = _now()
        with self._db() as db:
            participant = self._participant(db, token)
            already = participant["withdrawn_at"] is not None
            if not already:
                self._withdraw(db, participant["participant_id"], now, "participant_requested")
        return {"schema": SCHEMA, "withdrawn_at": participant["withdrawn_at"] or now.isoformat(),
                "idempotent_existing": already}

    @staticmethod
    def _withdraw(db: sqlite3.Connection, participant_id: str, now: datetime, reason: str) -> None:
        db.execute("UPDATE participants SET withdrawn_at=? WHERE participant_id=?",
                   (now.isoformat(), participant_id))
        db.execute("INSERT INTO events(occurred_at,kind,participant_id,detail) VALUES(?,?,?,?)",
                   (now.isoformat(), "participant_withdrawn", participant_id,
                    _json({"reason": reason})))

    def withdraw_participant(self, participant_id: str, reason: str) -> dict[str, Any]:
        """Researcher-side withdrawal using an enrollment receipt identifier."""
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("withdrawal reason is required")
        now = _now()
        with self._db() as db:
            participant = db.execute("SELECT * FROM participants WHERE participant_id=?",
                                     (str(participant_id),)).fetchone()
            if participant is None:
                raise ValueError("participant was not found")
            already = participant["withdrawn_at"] is not None
            if not already:
                self._withdraw(db, participant["participant_id"], now, reason)
        return {"schema": SCHEMA, "withdrawn_at": participant["withdrawn_at"] or now.isoformat(),
                "idempotent_existing": already}

    @staticmethod
    def _participant(db: sqlite3.Connection, token: str) -> sqlite3.Row:
        if not isinstance(token, str) or not token:
            raise ValueError("participant token is required")
        row = db.execute("SELECT * FROM participants WHERE token_hash=?",
                         (_sha_bytes(token.encode()),)).fetchone()
        if row is None:
            raise PermissionError("participant token is invalid")
        return row

    @staticmethod
    def _validate_diary(diary: Mapping[str, Any], received: datetime) -> tuple[dict[str, Any], bool, str]:
        if not isinstance(diary, Mapping):
            raise ValueError("diary must be an object")
        payload = dict(diary)
        try:
            night = date.fromisoformat(str(payload["night_date"]))
        except (KeyError, ValueError) as error:
            raise ValueError("night_date must be an ISO date") from error
        if night > received.date():
            raise ValueError("night_date cannot be in the future")
        missing_reason = str(payload.get("missing_reason") or "").strip()
        if missing_reason:
            if night >= received.date():
                raise ValueError("a missing night can only be declared after its night_date")
            return {"night_date": night.isoformat(), "missing_reason": missing_reason}, True, "missing_declared"
        attempt = _local(payload.get("sleep_attempt"), "sleep_attempt")
        wake = _local(payload.get("final_wake"), "final_wake")
        try:
            latency = float(payload.get("latency_min"))
        except (TypeError, ValueError) as error:
            raise ValueError("latency_min must be numeric") from error
        if not math.isfinite(latency) or not 0 <= latency <= 480:
            raise ValueError("latency_min must be between 0 and 480")
        onset = attempt + timedelta(minutes=latency)
        if wake > received:
            raise ValueError("final_wake cannot be after receipt")
        cutoff = datetime.combine(night, time(20), ZONE)
        duration = (wake - onset).total_seconds() / 60
        target_window = (cutoff < onset <= cutoff + timedelta(hours=16)
                         and onset + timedelta(minutes=30) <= wake <= cutoff + timedelta(hours=24)
                         and duration <= 960)
        for field in ("bed_entry", "out_of_bed"):
            if payload.get(field):
                optional_time = _local(payload[field], field)
                if optional_time > received:
                    raise ValueError(f"{field} cannot be after receipt")
                payload[field] = optional_time.isoformat()
        for field in ("awakenings_count", "waso_min", "quality"):
            if payload.get(field) not in (None, ""):
                try:
                    value = float(payload[field])
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{field} must be numeric") from error
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{field} cannot be negative")
                if field == "quality" and not 1 <= value <= 5:
                    raise ValueError("quality must be between 1 and 5")
                if field == "waso_min" and value > 960:
                    raise ValueError("waso_min must be at most 960")
                if field == "awakenings_count" and (value > 100 or not value.is_integer()):
                    raise ValueError("awakenings_count must be a whole number from 0 to 100")
                payload[field] = value
        payload.update({"night_date": night.isoformat(), "sleep_attempt": attempt.isoformat(),
                        "latency_min": latency, "sleep_start": onset.isoformat(),
                        "final_wake": wake.isoformat()})
        return payload, False, "valid" if target_window else "outside_target_window"

    def submit_diary(self, token: str, diary: Mapping[str, Any]) -> dict[str, Any]:
        received = _now()
        receipt_id = "dr_" + secrets.token_hex(12)
        with self._db() as db:
            study = self._study(db)
            participant = self._participant(db, token)
            if participant["withdrawn_at"] is not None:
                raise ValueError("participant is no longer active")
            payload, missing, state = self._validate_diary(diary, received)
            night = payload["night_date"]
            start = date.fromisoformat(participant["start_date"])
            day = (date.fromisoformat(night) - start).days + 1
            if not 1 <= day <= int(study["protocol"]["duration_days"]):
                raise ValueError("night_date is outside the participant study window")
            primary_exists = db.execute(
                "SELECT 1 FROM diaries WHERE participant_id=? AND night_date=? AND primary_revision=1",
                (participant["participant_id"], night)).fetchone() is not None
            valid_outcome = state == "valid"
            if missing:
                timing = "missing_declared"
            elif not valid_outcome:
                timing = "excluded_outside_target_window"
            else:
                wake = _local(payload["final_wake"], "final_wake")
                delay = (received - wake).total_seconds() / 3600
                timing = ("primary_12h" if delay <= study["protocol"]["primary_receipt_hours"] else
                          "sensitivity_24h" if delay <= study["protocol"]["sensitivity_receipt_hours"] else
                          "late_over_24h")
            primary = int(not primary_exists and valid_outcome
                          and timing in ("primary_12h", "sensitivity_24h"))
            forecast_status = ("not_applicable_missing" if missing else
                               "excluded_invalid_outcome" if not valid_outcome else "not_found")
            if primary:
                try:
                    service = ParticipantProspective(self.journal_root)
                    forecasts = service.forecasts(participant["participant_id"])
                    match = next((item for item in forecasts if item.get("night_id") == night), None)
                    if match:
                        outcome = {"sleep_start": payload["sleep_start"], "wake": payload["final_wake"],
                                   "observed_at": received.isoformat(), "source": "diary"}
                        result = service.record_outcome(participant["participant_id"], match["forecast_id"], outcome)
                        forecast_status = ("evaluated" if result["result"]["raw"].get("eligible")
                                           else "ineligible_outcome")
                    else:
                        forecast_status = "missing_forecast"
                except Exception as error:
                    # Preserve a valid received diary even if the separate immutable
                    # forecast journal cannot be read or scored.  The bounded type is
                    # enough for an operator to diagnose without exposing predictions.
                    forecast_status = "evaluation_failed_" + type(error).__name__
            stored = {**payload, "received_at": received.isoformat(), "validation_state": state}
            db.execute("INSERT INTO diaries VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (receipt_id, participant["participant_id"], night, received.isoformat(), _json(stored),
                        int(missing or valid_outcome), int(missing), primary, timing, forecast_status))
            db.execute("INSERT INTO events(occurred_at,kind,participant_id,night_date,detail) VALUES(?,?,?,?,?)",
                       (received.isoformat(), "diary_received", participant["participant_id"], night,
                        _json({"receipt_id": receipt_id, "timing_class": timing,
                               "primary_revision": bool(primary), "forecast_status": forecast_status})))
        return {"schema": SCHEMA, "receipt_id": receipt_id, "received_at": received.isoformat(),
                "accepted": True, "night_date": night, "timing_class": timing,
                "primary_revision": bool(primary), "forecast_status": forecast_status,
                "synthetic_rehearsal": study["synthetic"]}

    def reconcile(self) -> dict[str, Any]:
        """Retry transient assessment writes with the original receipt timestamp."""
        with self._db() as db:
            study = self._study(db)
            self._verify_manifest(study["manifest"])
            pending = db.execute(
                "SELECT * FROM diaries WHERE primary_revision=1 AND missing=0 "
                "AND forecast_status LIKE 'evaluation_failed_%' ORDER BY received_at"
            ).fetchall()
            service = ParticipantProspective(self.journal_root)
            reconciled = failed = 0
            for row in pending:
                payload = json.loads(row["payload"])
                try:
                    match = next((item for item in service.forecasts(row["participant_id"])
                                  if item.get("night_id") == row["night_date"]), None)
                    if match is None:
                        status = "missing_forecast"
                    else:
                        result = service.record_outcome(row["participant_id"], match["forecast_id"], {
                            "sleep_start": payload["sleep_start"], "wake": payload["final_wake"],
                            "observed_at": row["received_at"], "source": "diary",
                        })
                        status = ("evaluated" if result["result"]["raw"].get("eligible")
                                  else "ineligible_outcome")
                    db.execute("UPDATE diaries SET forecast_status=? WHERE receipt_id=?",
                               (status, row["receipt_id"]))
                    db.execute("INSERT INTO events(occurred_at,kind,participant_id,night_date,detail) "
                               "VALUES(?,?,?,?,?)", (_now().isoformat(), "outcome_reconciled",
                               row["participant_id"], row["night_date"], _json({"status": status})))
                    reconciled += 1
                except Exception:
                    failed += 1
        return {"schema": SCHEMA, "pending_found": len(pending), "reconciled": reconciled,
                "still_failed": failed, "used_original_receipt_timestamps": True}

    def _verify_manifest(self, manifest: Mapping[str, str]) -> None:
        actual = self._manifest()
        if actual != dict(manifest):
            raise RuntimeError("frozen source/model manifest changed; forecast capture is blocked")

    def tick(self) -> dict[str, Any]:
        """Commit the current night's forecast during the genuine 20:00-20:15 window."""
        now = _now()
        if not ((now.hour, now.minute, now.second, now.microsecond)
                <= (20, 15, 0, 0) and now.hour == 20):
            raise ValueError("forecast tick is only allowed from 20:00 through 20:15 Asia/Seoul")
        with self._db() as db:
            study = self._study(db)
            self._verify_manifest(study["manifest"])
            participants = db.execute(
                "SELECT * FROM participants WHERE withdrawn_at IS NULL ORDER BY enrolled_at"
            ).fetchall()
            service = ParticipantProspective(self.journal_root)
            committed = existing = inactive = failed = 0
            for participant in participants:
                start = date.fromisoformat(participant["start_date"])
                day = (now.date() - start).days + 1
                if not 1 <= day <= study["protocol"]["duration_days"]:
                    inactive += 1
                    continue
                records = []
                rows = db.execute(
                    "SELECT payload,received_at FROM diaries WHERE participant_id=? AND primary_revision=1 "
                    "AND missing=0 AND received_at<? ORDER BY night_date,received_at",
                    (participant["participant_id"], datetime.combine(now.date(), time(20), ZONE).isoformat())
                ).fetchall()
                for row in rows:
                    item = json.loads(row["payload"])
                    records.append({"bed": item["sleep_start"], "sleep_start": item["sleep_start"],
                                    "wake": item["final_wake"], "observed_at": row["received_at"],
                                    "source": "diary"})
                metadata = json.loads(participant["metadata"])
                profile = dict(metadata.get("profile") or {})
                profile.update({"created_at": participant["enrolled_at"],
                                "enrollment_id": participant["participant_id"],
                                "link_history": False})
                try:
                    result = service.commit(participant["participant_id"], {
                        "cutoff": now.date().isoformat(), "records": records, "profile": profile,
                        "online_personalization": False, "context": {},
                    })
                    if result["result"]["raw"].get("idempotent_existing"):
                        existing += 1
                    else:
                        committed += 1
                    detail = {"status": "existing" if result["result"]["raw"].get("idempotent_existing") else "committed"}
                except Exception as error:
                    failed += 1
                    detail = {"status": "failed", "error_type": type(error).__name__}
                db.execute("INSERT INTO events(occurred_at,kind,participant_id,night_date,detail) VALUES(?,?,?,?,?)",
                           (now.isoformat(), "forecast_tick", participant["participant_id"],
                            now.date().isoformat(), _json(detail)))
        reconciliation = self.reconcile()
        return {"schema": SCHEMA, "tick_at": now.isoformat(), "committed": committed,
                "idempotent_existing": existing, "inactive": inactive, "failed": failed,
                "reconciliation": reconciliation,
                "synthetic_rehearsal": study["synthetic"]}

    def status(self) -> dict[str, Any]:
        now = _now()
        with self._db() as db:
            study = self._study(db)
            people = db.execute("SELECT participant_id,start_date,withdrawn_at FROM participants").fetchall()
            diary = db.execute("SELECT * FROM diaries").fetchall()
            events = db.execute("SELECT kind,detail FROM events").fetchall()
        counts = {key: 0 for key in ("primary_12h", "sensitivity_24h", "late_over_24h", "missing_declared")}
        for row in diary:
            counts[row["timing_class"]] = counts.get(row["timing_class"], 0) + 1
        due = not_due = elapsed = open_followup = 0
        diary_nights = {(row["participant_id"], row["night_date"]) for row in diary}
        absent_due = 0
        for person in people:
            start = date.fromisoformat(person["start_date"])
            withdrawn = (_local(person["withdrawn_at"], "withdrawn_at")
                         if person["withdrawn_at"] else None)
            for offset in range(study["protocol"]["duration_days"]):
                night = start + timedelta(days=offset)
                cutoff = datetime.combine(night, time(20), ZONE)
                if withdrawn is not None and cutoff >= withdrawn:
                    continue
                if now >= cutoff:
                    elapsed += 1
                if now >= cutoff + timedelta(hours=48):
                    due += 1
                    absent_due += int((person["participant_id"], night.isoformat()) not in diary_nights)
                elif now >= cutoff:
                    open_followup += 1
                else:
                    not_due += 1
        failed_ticks = sum(json.loads(row["detail"]).get("status") == "failed"
                           for row in events if row["kind"] == "forecast_tick")
        forecast_total = eligible_forecasts = forecasts_with_outcome = forecast_read_failures = 0
        service = ParticipantProspective(self.journal_root)
        for person in people:
            try:
                forecasts = service.forecasts(person["participant_id"])
                forecast_total += len(forecasts)
                eligible_forecasts += sum(item.get("eligible_capture") is True for item in forecasts)
                forecasts_with_outcome += sum(item.get("has_outcome") is True for item in forecasts)
            except Exception:
                forecast_read_failures += 1
        missing_governance = [key for key in GOVERNANCE_FIELDS
                              if not str(study["protocol"].get(key) or "").strip()]
        return {"schema": SCHEMA, "synthetic_rehearsal": study["synthetic"],
                "created_at": study["created_at"], "participants_enrolled": len(people),
                "participants_withdrawn": sum(person["withdrawn_at"] is not None for person in people),
                "nights_due": due, "nights_elapsed": elapsed,
                "nights_open_followup": open_followup,
                "nights_not_yet_due": not_due, "nights_absent_due": absent_due,
                "diary_receipts": len(diary), "timing_counts": counts,
                "primary_revisions": sum(row["primary_revision"] for row in diary),
                "missing_forecasts": sum(row["forecast_status"] == "missing_forecast" for row in diary),
                "forecast_captures_total": forecast_total,
                "eligible_forecast_captures": eligible_forecasts,
                "forecasts_with_outcome": forecasts_with_outcome,
                "forecast_read_failures": forecast_read_failures,
                "pending_outcome_reconciliation": sum(
                    str(row["forecast_status"]).startswith("evaluation_failed_") for row in diary),
                "failed_forecast_ticks": failed_ticks,
                "ready_for_enrollment": bool(study["synthetic"] or not missing_governance),
                "missing_governance_fields": missing_governance,
                "protocol": {key: study["protocol"][key] for key in (
                    "phase", "duration_days", "adaptation_days", "primary_days", "primary_receipt_hours",
                    "sensitivity_receipt_hours", "minimum_primary_nights_for_completer",
                    "outcome_kind", "prediction_visibility", "primary_arms", "secondary_arms")}}

    @staticmethod
    def _percentile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    def _evaluation_rows(self) -> list[dict[str, Any]]:
        rows = []
        with self._db() as db:
            participants = db.execute(
                "SELECT participant_id,start_date,metadata FROM participants"
            ).fetchall()
            included = {(row["participant_id"], row["night_date"]): row["timing_class"]
                        for row in db.execute("SELECT participant_id,night_date,timing_class FROM diaries "
                                              "WHERE primary_revision=1 AND missing=0")}
        for participant in participants:
            metadata = json.loads(participant["metadata"])
            profile = metadata["profile"]
            baseline = {field: metadata[field] for field in BASELINE_CODES}
            baseline["age_group"] = _age_group(float(profile["age"]))
            journal = ParticipantProspective(self.journal_root).journal_path(participant["participant_id"])
            if not journal.exists():
                continue
            db = sqlite3.connect(journal)
            db.row_factory = sqlite3.Row
            try:
                joined = db.execute(
                    "SELECT f.payload forecast,a.payload assessment FROM prospective_forecasts f "
                    "JOIN prospective_assessments a ON a.forecast_id=f.forecast_id ORDER BY a.assessment_id"
                ).fetchall()
            finally:
                db.close()
            seen = set()
            for item in joined:
                forecast, assessment = json.loads(item["forecast"]), json.loads(item["assessment"])
                night = assessment.get("night_id")
                if night in seen or not assessment.get("eligible"):
                    continue
                study_day = ((date.fromisoformat(night) - date.fromisoformat(participant["start_date"])).days + 1)
                timing = included.get((participant["participant_id"], night))
                if timing not in ("primary_12h", "sensitivity_24h"):
                    continue
                seen.add(night)
                outcome = assessment["outcome_snapshot"]
                for arm, errors in assessment.get("errors", {}).items():
                    if arm not in PRIMARY_ARMS + SECONDARY_ARMS:
                        continue
                    prediction = forecast["predictions"][arm]
                    rows.append({"participant": participant["participant_id"], "night": night, "arm": arm,
                                 **errors,
                                 **baseline,
                                 "study_day": study_day, "timing_class": timing,
                                 "predicted_onset_min": prediction["onset_offset_min"],
                                 "predicted_wake_min": prediction["wake_offset_min"],
                                 "observed_onset_min": outcome["onset_offset_min"],
                                 "observed_wake_min": outcome["wake_offset_min"],
                                 "onset_signed_min": prediction["onset_offset_min"] - outcome["onset_offset_min"],
                                 "wake_signed_min": prediction["wake_offset_min"] - outcome["wake_offset_min"]})
        return rows

    def _metric_rows(self, rows: list[dict[str, Any]], bootstrap_replicates: int,
                     seed: int) -> list[dict[str, Any]]:
        """Calculate every analysis variant through one participant-cluster path."""
        rng = Random(seed)
        metrics = []
        for arm in PRIMARY_ARMS + SECONDARY_ARMS:
            selected = [row for row in rows if row["arm"] == arm]
            by_person: dict[str, list[dict[str, Any]]] = {}
            for row in selected:
                by_person.setdefault(row["participant"], []).append(row)
            if not by_person:
                continue
            person_metrics = {person: {key: statistics.mean(row[key] for row in items)
                                      for key in ("onset_min", "wake_min", "joint_min", "interval_min",
                                                  "onset_signed_min", "wake_signed_min")}
                              for person, items in by_person.items()}
            point = {key: statistics.mean(value[key] for value in person_metrics.values())
                     for key in next(iter(person_metrics.values()))}
            keys = list(person_metrics)
            boot = {key: [] for key in point}
            if len(keys) >= 2:
                for _ in range(int(bootstrap_replicates)):
                    sample = [person_metrics[rng.choice(keys)] for _ in keys]
                    for key in boot:
                        boot[key].append(statistics.mean(item[key] for item in sample))
            record = {"arm": arm, "role": "primary" if arm in PRIMARY_ARMS else "secondary",
                      "n_participants": len(keys), "n_nights": len(selected),
                      "aggregation": "equal_weight_per_participant"}
            for key, value in point.items():
                record[key] = value
                tail = .0125 if arm in PRIMARY_ARMS else .025
                record[key + "_ci_low"] = self._percentile(boot[key], tail)
                record[key + "_ci_high"] = self._percentile(boot[key], 1 - tail)
                record[key + "_ci_level"] = .975 if arm in PRIMARY_ARMS else .95
                record[key + "_ci_status"] = ("participant_cluster_bootstrap" if len(keys) >= 2
                                               else "insufficient_participants")
            for endpoint in ("onset", "wake"):
                record[f"{endpoint}_rmse_min"] = math.sqrt(statistics.mean(
                    statistics.mean(row[f"{endpoint}_signed_min"] ** 2 for row in items)
                    for items in by_person.values()))
                xs = [row[f"predicted_{endpoint}_min"] for row in selected]
                ys = [row[f"observed_{endpoint}_min"] for row in selected]
                weights = [1 / len(by_person[row["participant"]]) for row in selected]
                weight_sum = sum(weights)
                x_mean = sum(x * weight for x, weight in zip(xs, weights)) / weight_sum
                y_mean = sum(y * weight for y, weight in zip(ys, weights)) / weight_sum
                denominator = sum(weight * (x - x_mean) ** 2
                                  for x, weight in zip(xs, weights))
                slope = (sum(weight * (x - x_mean) * (y - y_mean)
                             for x, y, weight in zip(xs, ys, weights)) / denominator
                         if denominator > 0 else None)
                record[f"{endpoint}_calibration_slope"] = slope
                record[f"{endpoint}_calibration_intercept_min"] = (y_mean - slope * x_mean
                                                                    if slope is not None else None)
            metrics.append(record)
        return metrics

    @staticmethod
    def _analysis_record(name: str, criteria: str, rows: list[dict[str, Any]],
                         metrics: list[dict[str, Any]]) -> dict[str, Any]:
        pairs = {(row["participant"], row["night"]) for row in rows}
        return {"analysis": name, "criteria": criteria,
                "n_participants": len({person for person, _ in pairs}),
                "n_nights": len(pairs), "metrics": metrics}

    def _baseline_distribution(self) -> dict[str, Any]:
        with self._db() as db:
            metadata = [json.loads(row[0]) for row in db.execute(
                "SELECT metadata FROM participants ORDER BY enrolled_at")]
        values: dict[str, list[str]] = {
            "age_group": [_age_group(float(item["profile"]["age"])) for item in metadata],
            **{field: [str(item[field]) for item in metadata] for field in BASELINE_CODES},
        }
        expected = {"age_group": ("18-29", "30-44", "45-65"),
                    **{field: tuple(sorted(codes)) for field, codes in BASELINE_CODES.items()}}
        result = {}
        for field, items in values.items():
            levels = sorted(set(items) | set(expected.get(field, ())))
            counts = {level: items.count(level) for level in levels}
            suppress_variable = (len(items) < 10
                                 or any(0 < count < 5 for count in counts.values()))
            if field == "recruitment_source":
                records = [{"level": "levels_not_exported", "count": None,
                            "count_display": "not_exported", "suppressed_small_cell": True,
                            "variable_suppressed": True}]
            elif suppress_variable:
                records = [{"level": "all_suppressed", "count": None,
                            "count_display": "suppressed", "suppressed_small_cell": True,
                            "variable_suppressed": True}]
            else:
                records = [{"level": level, "count": counts[level],
                            "count_display": str(counts[level]), "suppressed_small_cell": False,
                            "variable_suppressed": False} for level in levels]
            result[field] = records
        return {"n_participants": len(metadata),
                "small_cell_rule": ("entire variable suppressed when total n<10 or any nonzero cell n<5; "
                                    "only a generic all_suppressed row is exported"),
                "variables": result}

    @staticmethod
    def _subgroup_descriptives(rows: list[dict[str, Any]]) -> dict[str, Any]:
        result = {}
        expected = {"age_group": ("18-29", "30-44", "45-65"),
                    "sex": tuple(sorted(BASELINE_CODES["sex"])),
                    "shift_work": tuple(sorted(BASELINE_CODES["shift_work"]))}
        for variable in ("age_group", "sex", "shift_work"):
            levels = []
            all_people = {row["participant"] for row in rows}
            counts = {level: len({row["participant"] for row in rows
                                  if str(row[variable]) == level})
                      for level in expected[variable]}
            suppress_variable = (len(all_people) < 10
                                 or any(0 < count < 5 for count in counts.values()))
            if suppress_variable:
                result[variable] = [{"level": "all_suppressed", "n_participants": None,
                                     "n_participants_display": "suppressed", "n_nights": None,
                                     "suppressed_small_cell": True, "variable_suppressed": True,
                                     "metrics": []}]
                continue
            for level in expected[variable]:
                selected = [row for row in rows if str(row[variable]) == level]
                pairs = {(row["participant"], row["night"]) for row in selected}
                participants = {person for person, _ in pairs}
                metrics = []
                if not suppress_variable:
                    for arm in PRIMARY_ARMS:
                        arm_rows = [row for row in selected if row["arm"] == arm]
                        by_person: dict[str, list[float]] = {}
                        for row in arm_rows:
                            by_person.setdefault(row["participant"], []).append(row["joint_min"])
                        if by_person:
                            metrics.append({"arm": arm,
                                            "joint_mae_min": statistics.mean(
                                                statistics.mean(items) for items in by_person.values()),
                                            "n_participants": len(by_person),
                                            "n_nights": len(arm_rows),
                                            "ci": None, "inference": "descriptive_only"})
                levels.append({"level": level,
                               "n_participants": len(participants),
                               "n_participants_display": str(len(participants)),
                               "n_nights": len(pairs), "suppressed_small_cell": False,
                               "variable_suppressed": False, "metrics": metrics})
            result[variable] = levels
        return {"status": "exploratory_descriptive_no_fairness_conclusion",
                "small_cell_rule": ("entire variable suppressed when total n<10 or any nonzero group n<5; "
                                    "only a generic all_suppressed row is exported"),
                "variables": result}

    def summary(self, bootstrap_replicates: int = 1000, seed: int = 20261103) -> dict[str, Any]:
        if not 100 <= int(bootstrap_replicates) <= 100000:
            raise ValueError("bootstrap_replicates must be 100 to 100000")
        reconciliation = self.reconcile()
        status = self.status()
        rows = self._evaluation_rows()
        with self._db() as db:
            study = self._study(db)
        current_manifest = self._manifest()
        self._verify_manifest(study["manifest"])
        adaptation = study["protocol"]["adaptation_days"]
        duration = study["protocol"]["duration_days"]
        primary_rows = [row for row in rows if adaptation < row["study_day"] <= duration
                        and row["timing_class"] == "primary_12h"]
        initial_rows = [row for row in rows if 1 <= row["study_day"] <= adaptation
                        and row["timing_class"] == "primary_12h"]
        all_rows = [row for row in rows if 1 <= row["study_day"] <= duration
                    and row["timing_class"] == "primary_12h"]
        late24_rows = [row for row in rows if adaptation < row["study_day"] <= duration
                       and row["timing_class"] in ("primary_12h", "sensitivity_24h")]
        primary_nights: dict[str, set[str]] = {}
        for row in primary_rows:
            if row["arm"] == PRIMARY_ARMS[0]:
                primary_nights.setdefault(row["participant"], set()).add(row["night"])
        completers = {person for person, nights in primary_nights.items()
                      if len(nights) >= study["protocol"]["minimum_primary_nights_for_completer"]}
        completer_rows = [row for row in primary_rows if row["participant"] in completers]
        variants = (
            ("primary", f"study days {adaptation + 1}-{duration}; received within 12h of final wake", primary_rows),
            ("initial_7_days", f"adaptation days 1-{adaptation}; received within 12h", initial_rows),
            ("all_days", f"all scheduled study days 1-{duration}; received within 12h", all_rows),
            ("completers", "primary period and protocol minimum scored nights", completer_rows),
            ("receipt_24h_sensitivity", "primary period; received within 24h", late24_rows),
        )
        analyses = {}
        for index, (name, criteria, selected) in enumerate(variants):
            calculated = self._metric_rows(selected, int(bootstrap_replicates), seed + index)
            analyses[name] = self._analysis_record(name, criteria, selected, calculated)
        metrics = analyses["primary"]["metrics"]
        baseline_distribution = self._baseline_distribution()
        subgroup_descriptives = self._subgroup_descriptives(primary_rows)
        analysis_status = ("synthetic_rehearsal_not_validation" if study["synthetic"] else
                           "feasibility_only_no_validation_claim" if study["protocol"]["phase"] == "feasibility"
                           else "no_eligible_outcomes" if not metrics else "prospective_validation")
        return {"schema": SCHEMA, "analysis_status": analysis_status,
                "synthetic_rehearsal": study["synthetic"], "scope": "user_reported_sleep_diary_only",
                "phase": study["protocol"]["phase"], "pooling_policy": "separate_phase_never_pool",
                "participant_weighting": "equal_weight_per_participant", "bootstrap_unit": "participant",
                "bootstrap_replicates": int(bootstrap_replicates), "seed": int(seed),
                "primary_ci": "97.5% per fixed primary arm (Bonferroni simultaneous family 95%)",
                "signed_error_definition": "predicted_minutes_minus_observed_minutes",
                "participants_with_scored_outcomes": analyses["primary"]["n_participants"],
                "completers_at_protocol_minimum": len(completers),
                "protocol_minimum_primary_nights": study["protocol"]["minimum_primary_nights_for_completer"],
                "sensitivity_24h_scored_nights": analyses["receipt_24h_sensitivity"]["n_nights"],
                "flow": status, "metrics": metrics, "analyses": analyses,
                "baseline_distribution": baseline_distribution,
                "subgroup_descriptives": subgroup_descriptives,
                "reconciliation": reconciliation,
                "provenance": {"protocol_sha256": _sha_bytes(_json(study["protocol"]).encode()),
                               "frozen_manifest": study["manifest"],
                               "current_manifest": current_manifest,
                               "manifest_verified": True},
                "limitations": ["Not PSG or actigraphy validation.",
                                "Does not establish sleep improvement or clinical benefit.",
                                "Calibration point estimates omit clustered confidence intervals and may be undefined when predictions do not vary."]}

    def export(self, destination: str | Path, bootstrap_replicates: int = 1000,
               seed: int = 20261103) -> Path:
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise FileExistsError("export destination already exists")
        target.mkdir(parents=True)
        result = self.summary(bootstrap_replicates, seed)
        (target / "aggregate_results.json").write_text(_json(result) + "\n", encoding="utf-8")
        flat_metrics = [{"analysis": name, **row}
                        for name, analysis in result["analyses"].items()
                        for row in analysis["metrics"]]
        fields = sorted({key for row in flat_metrics for key in row})
        with (target / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or ["arm"])
            writer.writeheader()
            writer.writerows(flat_metrics)
        baseline_rows = [{"variable": variable, **row}
                         for variable, records in result["baseline_distribution"]["variables"].items()
                         for row in records]
        with (target / "baseline_distribution.csv").open(
                "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("variable", "level", "count",
                                                        "count_display", "suppressed_small_cell",
                                                        "variable_suppressed"))
            writer.writeheader()
            writer.writerows(baseline_rows)
        subgroup_rows = []
        for variable, levels in result["subgroup_descriptives"]["variables"].items():
            for level in levels:
                common = {"variable": variable, "level": level["level"],
                          "n_participants": level["n_participants"],
                          "n_participants_display": level["n_participants_display"],
                          "n_nights": level["n_nights"],
                          "suppressed_small_cell": level["suppressed_small_cell"]}
                if level["metrics"]:
                    subgroup_rows.extend({**common, **metric} for metric in level["metrics"])
                else:
                    subgroup_rows.append(common)
        subgroup_fields = ("variable", "level", "n_participants", "n_participants_display",
                           "n_nights", "suppressed_small_cell", "arm", "joint_mae_min", "ci",
                           "inference")
        with (target / "subgroup_descriptives.csv").open(
                "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=subgroup_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(subgroup_rows)
        def number(value: Any) -> str:
            return "NA" if value is None else f"{float(value):.3f}"

        lines = ["# Prospective diary validation aggregate", "",
                 f"- Status: {result['analysis_status']}",
                 f"- Synthetic rehearsal: {result['synthetic_rehearsal']}",
                 f"- Enrolled participants: {result['flow']['participants_enrolled']}",
                 f"- Participants with scored outcomes: {result['participants_with_scored_outcomes']}", "",
                 f"- Protocol SHA-256: {result['provenance']['protocol_sha256']}",
                 f"- Frozen source/model manifest verified: {result['provenance']['manifest_verified']}",
                 "- Signed bias direction: predicted minutes minus observed minutes", "",
                 "## Analysis variants", ""]
        for name, analysis in result["analyses"].items():
            lines.extend([f"### {name}", "", analysis["criteria"], "",
                          f"Participants: {analysis['n_participants']}; nights: {analysis['n_nights']}", "",
                          "| Arm | Role | Participants | Nights | Joint MAE min | CI | Onset bias min | Wake bias min |",
                          "|---|---:|---:|---:|---:|---:|---:|---:|"])
            if not analysis["metrics"]:
                lines.append("| No eligible outcomes | — | 0 | 0 | NA | NA | NA | NA |")
            for metric in analysis["metrics"]:
                interval = (f"{number(metric.get('joint_min_ci_low'))} to "
                            f"{number(metric.get('joint_min_ci_high'))} "
                            f"({metric.get('joint_min_ci_status')})")
                lines.append("| {arm} | {role} | {people} | {nights} | {joint} | {ci} | {onset} | {wake} |".format(
                    arm=metric["arm"], role=metric["role"], people=metric["n_participants"],
                    nights=metric["n_nights"], joint=number(metric.get("joint_min")), ci=interval,
                    onset=number(metric.get("onset_signed_min")),
                    wake=number(metric.get("wake_signed_min"))))
            lines.append("")
        flow = result["flow"]
        lines.extend(["## Participant and night flow", "",
                      "| Measure | Count |", "|---|---:|",
                      f"| Enrolled participants | {flow['participants_enrolled']} |",
                      f"| Withdrawn participants | {flow['participants_withdrawn']} |",
                      f"| Nights elapsed | {flow['nights_elapsed']} |",
                      f"| Nights still in follow-up window | {flow['nights_open_followup']} |",
                      f"| Closed nights due | {flow['nights_due']} |",
                      f"| Closed nights with no diary receipt | {flow['nights_absent_due']} |",
                      f"| Nights not yet due | {flow['nights_not_yet_due']} |",
                      f"| Eligible forecast captures | {flow['eligible_forecast_captures']} |",
                      f"| Forecasts with an outcome | {flow['forecasts_with_outcome']} |", "",
                      "## Baseline distribution", "",
                      "Counts from 1 to 4 are suppressed in every shareable aggregate.", "",
                      "| Variable | Level | Count |", "|---|---|---:|"])
        for variable in ("age_group", "sex", "shift_work"):
            for row in result["baseline_distribution"]["variables"][variable]:
                lines.append(f"| {variable} | {row['level']} | {row['count_display']} |")
        lines.extend(["", "## Prespecified descriptive subgroups", "",
                      "These are exploratory descriptions of joint MAE, not fairness conclusions. "
                      "A variable is fully suppressed when total n<10 or any nonzero group has fewer than 5 participants; "
                      "no subgroup confidence intervals are computed.", "",
                      "| Variable | Level | Arm | Participants | Nights | Joint MAE min |",
                      "|---|---|---|---:|---:|---:|"])
        for variable, levels in result["subgroup_descriptives"]["variables"].items():
            for level in levels:
                if not level["metrics"]:
                    lines.append(f"| {variable} | {level['level']} | suppressed | "
                                 f"{level['n_participants_display']} | NA | NA |")
                for metric in level["metrics"]:
                    lines.append(f"| {variable} | {level['level']} | {metric['arm']} | "
                                 f"{metric['n_participants']} | {metric['n_nights']} | "
                                 f"{number(metric['joint_mae_min'])} |")
        lines.extend(["",
                      "## Limitations", ""])
        lines.extend(f"- {item}" for item in result["limitations"])
        lines.extend(["", "This evaluates user-reported sleep diary timing only; it is not PSG/actigraphy or benefit evidence."])
        (target / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest = {path.name: _sha_file(path) for path in target.iterdir() if path.is_file()}
        (target / "MANIFEST.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        return target
