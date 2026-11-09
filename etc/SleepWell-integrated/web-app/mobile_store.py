"""Validated, causal ingestion of sleep sessions from the Android bridge.

The Android application is the trust boundary for Samsung Health attribution.  This
module therefore records the transport as Health Connect and never accepts a source
label from the wire.  All timestamps written to user records are server observed.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import json
import math
import re
import sqlite3
import uuid

from engine import ZONE


_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_ASLEEP = frozenset(("sleeping", "light", "deep", "rem"))
_STAGES = _ASLEEP | frozenset(("awake", "unknown", "out_of_bed"))
_IMPORTER = "sleepwell_mobile_sync_v1"


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}은(는) 객체여야 합니다")
    return value


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} 형식을 확인해주세요")
    return value


def _instant(value, label, now):
    if not isinstance(value, str):
        raise ValueError(f"{label}에 시간대가 포함된 ISO 시각이 필요합니다")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} 시각을 확인해주세요") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label}에 시간대가 필요합니다")
    # datetime values are finite by construction, but utcoffset implementations can
    # still be hostile; converting once also gives comparisons one fixed zone.
    parsed = parsed.astimezone(ZONE)
    if parsed > now:
        raise ValueError(f"{label}은(는) 미래일 수 없습니다")
    return parsed


def _validate_session(raw, now):
    row = _object(raw, "sessions[]")
    required = {"id", "modified_at", "start", "end", "stages"}
    if set(row) != required:
        raise ValueError("session 필드를 확인해주세요")
    identity = _identifier(row["id"], "session.id")
    modified = _instant(row["modified_at"], "session.modified_at", now)
    start = _instant(row["start"], "session.start", now)
    end = _instant(row["end"], "session.end", now)
    hours = (end - start).total_seconds() / 3600
    if not math.isfinite(hours) or not 0.5 <= hours <= 16:
        raise ValueError("완료된 수면 구간은 30분~16시간이어야 합니다")
    stages = row["stages"]
    if not isinstance(stages, list) or len(stages) > 500:
        raise ValueError("stages는 500개 이하의 목록이어야 합니다")
    normalized = []
    previous_end = None
    for index, raw_stage in enumerate(stages):
        stage = _object(raw_stage, f"stages[{index}]")
        if set(stage) != {"start", "end", "stage"} or stage["stage"] not in _STAGES:
            raise ValueError(f"stages[{index}] 형식을 확인해주세요")
        stage_start = _instant(stage["start"], f"stages[{index}].start", now)
        stage_end = _instant(stage["end"], f"stages[{index}].end", now)
        if not start <= stage_start < stage_end <= end:
            raise ValueError("수면 단계는 session 구간 안에 있어야 합니다")
        if previous_end is not None and stage_start < previous_end:
            raise ValueError("수면 단계는 시간순이며 겹치지 않아야 합니다")
        normalized.append({"start": stage_start.isoformat(), "end": stage_end.isoformat(),
                           "stage": stage["stage"]})
        previous_end = stage_end
    return {"id": identity, "modified_at": modified.isoformat(), "start": start.isoformat(),
            "end": end.isoformat(), "stages": normalized}


def _validate(device_id, payload, now):
    device_id = _identifier(device_id, "device_id")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now에 시간대가 필요합니다")
    now = now.astimezone(ZONE)
    payload = _object(payload, "payload")
    if set(payload) - {"schema_version", "sessions", "deleted_ids"}:
        raise ValueError("알 수 없는 payload 필드가 있습니다")
    # bool is an int subclass in Python; accepting True here would weaken the
    # wire-version contract.
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1 or not isinstance(payload.get("sessions"), list):
        raise ValueError("schema_version 1과 sessions 목록이 필요합니다")
    sessions = payload["sessions"]
    deleted = payload.get("deleted_ids", [])
    if not isinstance(deleted, list) or len(sessions) > 100 or len(deleted) > 100:
        raise ValueError("한 번에 sessions/deleted_ids는 각각 100개까지 전송할 수 있습니다")
    sessions = [_validate_session(row, now) for row in sessions]
    deleted = [_identifier(value, "deleted_ids[]") for value in deleted]
    session_ids = [row["id"] for row in sessions]
    if len(session_ids) != len(set(session_ids)) or len(deleted) != len(set(deleted)):
        raise ValueError("한 batch에 중복된 session id가 있습니다")
    if set(session_ids) & set(deleted):
        raise ValueError("하나의 session을 저장과 삭제로 동시에 보낼 수 없습니다")
    return device_id, sessions, deleted, now


def _record_status(row):
    stages = row["stages"]
    asleep = [stage for stage in stages if stage["stage"] in _ASLEEP]
    if not asleep:
        return None, "no_asleep_stage"
    onset = datetime.fromisoformat(asleep[0]["start"])
    wake = datetime.fromisoformat(asleep[-1]["end"])
    within = [stage for stage in stages
              if datetime.fromisoformat(stage["start"]) >= onset
              and datetime.fromisoformat(stage["end"]) <= wake]
    cursor = onset
    has_gap = False
    has_unknown = False
    awake_seconds = 0.0
    asleep_seconds = 0.0
    for stage in within:
        start, end = datetime.fromisoformat(stage["start"]), datetime.fromisoformat(stage["end"])
        if start != cursor:
            has_gap = True
        if stage["stage"] == "unknown":
            has_unknown = True
        seconds = (end - start).total_seconds()
        if stage["stage"] in _ASLEEP:
            asleep_seconds += seconds
        else:
            awake_seconds += seconds
        cursor = end
    if cursor != wake:
        has_gap = True
    sleep_min = asleep_seconds / 60
    # A main sleep must be at least two known-asleep hours and have complete,
    # classified coverage between first sleep and final sleep.
    if sleep_min < 120:
        return None, "short_sleep"
    if has_unknown:
        return None, "unknown_stage"
    if has_gap:
        return None, "stage_gap"
    wake = wake.astimezone(ZONE)
    onset = onset.astimezone(ZONE)
    # Health Connect session boundaries do not establish time-in-bed.  Using the
    # first observed asleep stage for both bed and sleep_start avoids fabricating
    # latency while retaining the existing record schema.
    return {
        "date": wake.date().isoformat(), "bed": onset.isoformat(), "wake": wake.isoformat(),
        "sleep_start": onset.isoformat(), "latency_min": None,
        "awake_min": awake_seconds / 60, "sleep_min": sleep_min,
        "rested": None, "fatigue": None, "steps": None, "exercise_min": None,
        "note": "Samsung Health에서 Health Connect를 통해 가져온 수면 기록",
        "source": "health_connect", "provenance": {
            "transport": "android_health_connect", "provider": "samsung_health",
            "importer": _IMPORTER, "device_id": row["device_id"], "session_id": row["session_id"]}}, None


def _record_from_session(row):
    return _record_status(row)[0]


def _create_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mobile_sleep_sessions (
            device_id TEXT NOT NULL, session_id TEXT NOT NULL, modified_at TEXT NOT NULL,
            observed_at TEXT NOT NULL, deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
            wake_date TEXT, data TEXT NOT NULL, PRIMARY KEY(device_id, session_id));
        CREATE INDEX IF NOT EXISTS mobile_sleep_wake ON mobile_sleep_sessions(wake_date, deleted);
        CREATE TABLE IF NOT EXISTS mobile_sleep_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
            device_id TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mobile_sleep_identities (
            device_id TEXT NOT NULL, session_id TEXT NOT NULL, record_id TEXT NOT NULL UNIQUE,
            PRIMARY KEY(device_id, session_id));
        CREATE TABLE IF NOT EXISTS mobile_sleep_inbox (
            wake_date TEXT PRIMARY KEY, observed_at TEXT NOT NULL, reason TEXT NOT NULL,
            device_id TEXT NOT NULL, session_id TEXT NOT NULL, data TEXT NOT NULL);
    """)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _candidates(db, wake_date):
    result = []
    rows = db.execute("SELECT device_id,session_id,data FROM mobile_sleep_sessions "
                      "WHERE wake_date=? AND deleted=0", (wake_date,))
    for device_id, session_id, raw in rows:
        session = json.loads(raw)
        session.update(device_id=device_id, session_id=session_id)
        record = _record_from_session(session)
        if record is not None:
            span = (datetime.fromisoformat(record["wake"]) -
                    datetime.fromisoformat(record["sleep_start"])).total_seconds()
            result.append((record["sleep_min"], span, device_id, session_id, record))
    result.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return result


def _identity(db, device_id, session_id):
    row = db.execute("SELECT record_id FROM mobile_sleep_identities WHERE device_id=? AND session_id=?",
                     (device_id, session_id)).fetchone()
    if row:
        return row[0]
    identity = uuid.uuid4().hex
    db.execute("INSERT INTO mobile_sleep_identities VALUES(?,?,?)", (device_id, session_id, identity))
    return identity


def _is_ours(record):
    return (record.get("source") == "health_connect" and
            isinstance(record.get("provenance"), dict) and
            record["provenance"].get("importer") == _IMPORTER)


def _summary_db(db):
    held = {}
    total = eligible = 0
    try:
        rows = db.execute("SELECT device_id,session_id,data FROM mobile_sleep_sessions WHERE deleted=0")
    except sqlite3.OperationalError as error:
        # sqlite reports a missing optional additive table this way.  Re-raise all
        # other database failures rather than hiding corruption or I/O problems.
        if "no such table" not in str(error).lower():
            raise
        return {"sessions_total": 0, "sessions_eligible": 0, "records_held": 0,
                "held_reasons": {}, "conflicts_pending": 0}
    for device_id, session_id, raw in rows:
        total += 1
        row = json.loads(raw)
        row.update(device_id=device_id, session_id=session_id)
        record, reason = _record_status(row)
        if record is not None:
            eligible += 1
        else:
            held[reason] = held.get(reason, 0) + 1
    conflicts = db.execute("SELECT COUNT(*) FROM mobile_sleep_inbox").fetchone()[0]
    return {"sessions_total": total, "sessions_eligible": eligible,
            "records_held": sum(held.values()), "held_reasons": held,
            "conflicts_pending": conflicts}


def summary(store) -> dict:
    """Return aggregate mobile-sync readiness without exposing session data."""
    with store.connect() as db:
        return _summary_db(db)


def ingest(app, device_id: str, payload: dict, now: datetime) -> dict:
    """Atomically accept one already-authenticated Android sync batch.

    The HTTP layer must authenticate and bind ``device_id`` to the paired Android
    installation before calling this function.  Validation completes before opening
    the write transaction, so malformed batches have no partial effects.
    """
    device_id, sessions, deleted_ids, now = _validate(device_id, payload, now)
    counts = {"accepted": 0, "unchanged": 0, "stale": 0, "deleted": 0,
              "records_upserted": 0, "records_deleted": 0, "conflicts_pending": 0,
              "records_held": 0, "held_reasons": {}}
    lock = getattr(app, "lock", None)
    with (lock if lock is not None else nullcontext()):
        with app.store.connect() as db:
            _create_tables(db)
            affected = set()
            for session in sessions:
                existing = db.execute(
                    "SELECT modified_at,observed_at,deleted,wake_date,data FROM mobile_sleep_sessions "
                    "WHERE device_id=? AND session_id=?", (device_id, session["id"])).fetchone()
                raw = _canonical(session)
                if existing:
                    # Reconcile the date even for an idempotent resend.  A diary
                    # entry may have replaced our record since the previous sync.
                    if existing[3]:
                        affected.add(existing[3])
                    previous_modified = datetime.fromisoformat(existing[0])
                    incoming_modified = datetime.fromisoformat(session["modified_at"])
                    if incoming_modified < previous_modified:
                        counts["stale"] += 1
                        continue
                    if incoming_modified == previous_modified:
                        if not existing[2] and existing[4] == raw:
                            counts["unchanged"] += 1
                            continue
                        raise ValueError("같은 modified_at의 session 내용이 다릅니다")
                probe = dict(session, device_id=device_id, session_id=session["id"])
                candidate = _record_from_session(probe)
                wake_date = candidate["date"] if candidate else None
                if wake_date:
                    affected.add(wake_date)
                db.execute("INSERT OR REPLACE INTO mobile_sleep_sessions VALUES(?,?,?,?,?,?,?)",
                           (device_id, session["id"], session["modified_at"], now.isoformat(), 0,
                            wake_date, raw))
                db.execute("INSERT INTO mobile_sleep_events(observed_at,device_id,session_id,kind,data) "
                           "VALUES(?,?,?,?,?)", (now.isoformat(), device_id, session["id"],
                                                "session_saved", raw))
                counts["accepted"] += 1
            for session_id in deleted_ids:
                existing = db.execute(
                    "SELECT modified_at,deleted,wake_date,data FROM mobile_sleep_sessions "
                    "WHERE device_id=? AND session_id=?", (device_id, session_id)).fetchone()
                if existing and existing[1]:
                    counts["unchanged"] += 1
                    continue
                if existing and existing[2]:
                    affected.add(existing[2])
                raw = existing[3] if existing else "{}"
                db.execute("INSERT OR REPLACE INTO mobile_sleep_sessions VALUES(?,?,?,?,?,?,?)",
                           (device_id, session_id, now.isoformat(), now.isoformat(), 1, None, raw))
                db.execute("INSERT INTO mobile_sleep_events(observed_at,device_id,session_id,kind,data) "
                           "VALUES(?,?,?,?,?)", (now.isoformat(), device_id, session_id,
                                                "session_deleted", raw))
                counts["deleted"] += 1

            for wake_date in sorted(affected):
                current_row = db.execute("SELECT data FROM records WHERE date=?", (wake_date,)).fetchone()
                current = json.loads(current_row[0]) if current_row else None
                candidates = _candidates(db, wake_date)
                candidate = candidates[0] if candidates else None
                if current is not None and not _is_ours(current):
                    if candidate:
                        record = candidate[4]
                        db.execute("INSERT OR REPLACE INTO mobile_sleep_inbox VALUES(?,?,?,?,?,?)",
                                   (wake_date, now.isoformat(), "existing_record", candidate[2], candidate[3],
                                    _canonical(record)))
                    else:
                        db.execute("DELETE FROM mobile_sleep_inbox WHERE wake_date=?", (wake_date,))
                    continue
                if candidate is None:
                    db.execute("DELETE FROM mobile_sleep_inbox WHERE wake_date=?", (wake_date,))
                    if current is not None and _is_ours(current):
                        # Store.record_versions expects a mapping here.  An empty
                        # mapping marks deletion while retaining the previous version.
                        app.store.event(db, "record_saved", {"previous": current, "record": {}}, now)
                        db.execute("DELETE FROM records WHERE date=?", (wake_date,))
                        counts["records_deleted"] += 1
                    continue
                record = candidate[4]
                record["record_id"] = _identity(db, candidate[2], candidate[3])
                record["observed_at"] = now.isoformat()
                # Avoid a spurious revision when another changed session did not alter
                # the selected main sleep for this date.
                comparable = dict(record)
                if current:
                    comparable["observed_at"] = current.get("observed_at")
                if current is not None and _canonical(current) == _canonical(comparable):
                    db.execute("DELETE FROM mobile_sleep_inbox WHERE wake_date=?", (wake_date,))
                    continue
                app.store.event(db, "record_saved", {"previous": current, "record": record}, now)
                db.execute("INSERT OR REPLACE INTO records VALUES(?,?)",
                           (wake_date, _canonical(record)))
                app.store._assess_record(db, record, wake_date)
                db.execute("DELETE FROM mobile_sleep_inbox WHERE wake_date=?", (wake_date,))
                counts["records_upserted"] += 1
            aggregate = _summary_db(db)
            counts["conflicts_pending"] = aggregate["conflicts_pending"]
            counts["records_held"] = aggregate["records_held"]
            counts["held_reasons"] = aggregate["held_reasons"]
    return counts
