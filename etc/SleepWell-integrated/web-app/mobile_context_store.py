"""Versioned, fail-closed storage for non-sleep mobile context observations."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re


KINDS = {"steps", "exercise", "caffeine", "schedule", "nap"}
SENSOR_KINDS = {"steps", "exercise"}
SCHEDULE_KINDS = {"work", "class", "first"}
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
PARTICIPANT_RE = re.compile(r"[0-9a-f]{32}\Z")
EVENT_FIELDS = {
    "id", "group_id", "kind", "start", "end", "value", "modified_at",
    "observed_at", "source", "details",
}


def _instant(value, name):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(name)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError(name) from error


def _night_id(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("night_id")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("night_id") from error
    if parsed.isoformat() != value:
        raise ValueError("night_id")
    return value


def _details(item, kind):
    details = item.get("details")
    if kind in SENSOR_KINDS:
        if details is not None and (not isinstance(details, dict) or details):
            raise ValueError("sensor details")
        return details
    expected = {"night_id"}
    if kind == "schedule":
        expected |= {"schedule_kind", "preparation_minutes"}
    if not isinstance(details, dict) or set(details) != expected:
        raise ValueError("details")
    _night_id(details.get("night_id"))
    if kind == "schedule":
        if (not isinstance(details.get("schedule_kind"), str)
                or details.get("schedule_kind") not in SCHEDULE_KINDS):
            raise ValueError("schedule_kind")
        preparation = details.get("preparation_minutes")
        if isinstance(preparation, bool) or not isinstance(preparation, int) or not 0 <= preparation <= 240:
            raise ValueError("preparation_minutes")
    return details


def _schema(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS mobile_context_events(
            device_id TEXT NOT NULL, participant_key TEXT NOT NULL,
            event_id TEXT NOT NULL, group_id TEXT, kind TEXT NOT NULL,
            start TEXT NOT NULL, end TEXT, value REAL,
            source_modified_at TEXT NOT NULL, client_observed_at TEXT,
            first_received_at TEXT NOT NULL, last_received_at TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
            PRIMARY KEY(device_id,participant_key,event_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS mobile_context_current ON mobile_context_events(device_id,participant_key,kind,deleted,start)")
        db.execute("""CREATE TABLE IF NOT EXISTS mobile_context_groups(
            device_id TEXT NOT NULL,participant_key TEXT NOT NULL,group_id TEXT NOT NULL,
            source_modified_at TEXT NOT NULL,deleted INTEGER NOT NULL,
            PRIMARY KEY(device_id,participant_key,group_id))""")


def _validate(payload, now):
    if not isinstance(payload, dict) or set(payload) - {"schema_version", "participant_key", "generated_at", "range_start", "range_end", "complete_scopes", "events", "cleared_groups"}:
        raise ValueError("unknown field")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 2:
        raise ValueError("schema_version")
    participant = payload.get("participant_key")
    if not isinstance(participant, str) or not PARTICIPANT_RE.fullmatch(participant):
        raise ValueError("participant_key")
    generated = _instant(payload.get("generated_at"), "generated_at")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now")
    now_utc = now.astimezone(timezone.utc)
    if generated > now_utc + timedelta(minutes=5) or generated < now_utc - timedelta(days=2):
        raise ValueError("generated_at")
    start = _instant(payload.get("range_start"), "range_start")
    end = _instant(payload.get("range_end"), "range_end")
    if end <= start or end - start > timedelta(days=31) or end > generated + timedelta(minutes=5):
        raise ValueError("range")
    scopes = payload.get("complete_scopes")
    if (not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes)
            or len(scopes) != len(set(scopes)) or not set(scopes) <= SENSOR_KINDS):
        raise ValueError("complete_scopes")
    events = payload.get("events")
    if not isinstance(events, list) or len(events) > 1000:
        raise ValueError("events")
    validated = []
    for item in events:
        if not isinstance(item, dict) or set(item) != EVENT_FIELDS:
            raise ValueError("event")
        event_id, kind = item.get("id"), item.get("kind")
        if not isinstance(event_id, str) or not ID_RE.fullmatch(event_id) or not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("event identity")
        _details(item, kind)
        group_id = item.get("group_id")
        if group_id is not None and (kind in SENSOR_KINDS or not isinstance(group_id, str) or not ID_RE.fullmatch(group_id)):
            raise ValueError("group_id")
        event_start = _instant(item.get("start"), "start")
        raw_end = item.get("end")
        event_end = _instant(raw_end, "end") if raw_end is not None else None
        modified = _instant(item.get("modified_at"), "modified_at")
        observed = _instant(item.get("observed_at"), "observed_at") if item.get("observed_at") is not None else None
        if event_end is not None and event_end < event_start:
            raise ValueError("end")
        if kind in {"steps", "exercise", "nap"} and (event_end is None or not timedelta(0) < event_end - event_start <= timedelta(hours=48) or event_end > now_utc):
            raise ValueError("interval")
        if kind != "schedule" and event_start > generated + timedelta(minutes=5):
            raise ValueError("future event")
        if kind == "schedule" and event_start > generated + timedelta(days=7):
            raise ValueError("future schedule")
        if event_start < start - timedelta(days=2) or event_start > end + timedelta(days=7):
            raise ValueError("event range")
        if modified > generated + timedelta(minutes=5) or observed and observed > generated + timedelta(minutes=5):
            raise ValueError("event observation")
        value = item.get("value")
        if kind == "steps":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 200000:
                raise ValueError("steps")
        elif kind == "caffeine":
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 1000 or not math.isfinite(value)):
                raise ValueError("caffeine")
        elif value is not None:
            raise ValueError("value")
        source = item.get("source")
        expected = "samsung_health" if kind in SENSOR_KINDS else "manual_android"
        if source != expected:
            raise ValueError("source")
        validated.append((event_id, group_id, kind, event_start, event_end, float(value) if value is not None else None,
                          modified, observed, dict(item)))
    clears = payload.get("cleared_groups", [])
    if not isinstance(clears, list) or len(clears) > 500:
        raise ValueError("cleared_groups")
    validated_clears = []
    for item in clears:
        if not isinstance(item, dict) or set(item) != {"group_id", "modified_at"}:
            raise ValueError("cleared group")
        group_id = item.get("group_id")
        if not isinstance(group_id, str) or not ID_RE.fullmatch(group_id) or not group_id.startswith("manual:"):
            raise ValueError("cleared group")
        modified = _instant(item.get("modified_at"), "modified_at")
        if modified > generated + timedelta(minutes=5):
            raise ValueError("cleared group")
        validated_clears.append((group_id, modified))
    return participant, start, end, scopes, validated, validated_clears


def ingest(store, device_id, payload, now):
    if not isinstance(device_id, str) or not ID_RE.fullmatch(device_id):
        raise ValueError("device_id")
    participant, range_start, range_end, scopes, events, clears = _validate(payload, now)
    _schema(store)
    received = now.astimezone(timezone.utc).isoformat()
    accepted = unchanged = stale = superseded = deleted = 0
    with store.connect() as db:
        for event_id, group_id, kind, start, end, value, modified, observed, raw in events:
            existing = db.execute("SELECT source_modified_at,payload,deleted FROM mobile_context_events WHERE device_id=? AND participant_key=? AND event_id=?",
                                  (device_id, participant, event_id)).fetchone()
            packed = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if existing:
                old_modified = _instant(existing[0], "stored modified")
                if modified < old_modified:
                    stale += 1
                    continue
                if modified == old_modified and existing[1] == packed and not existing[2]:
                    db.execute("UPDATE mobile_context_events SET last_received_at=? WHERE device_id=? AND participant_key=? AND event_id=?",
                               (received, device_id, participant, event_id))
                    unchanged += 1
                    continue
                if modified == old_modified and existing[1] != packed:
                    raise ValueError("same event version differs")
            if group_id:
                group_state = db.execute("SELECT source_modified_at,deleted FROM mobile_context_groups WHERE device_id=? AND participant_key=? AND group_id=?",
                                         (device_id, participant, group_id)).fetchone()
                if group_state:
                    group_modified = _instant(group_state[0], "stored modified")
                    if group_modified > modified or (group_modified == modified and group_state[1]):
                        stale += 1
                        continue
                rows = db.execute("SELECT event_id,source_modified_at FROM mobile_context_events WHERE device_id=? AND participant_key=? AND group_id=? AND deleted=0",
                                  (device_id, participant, group_id)).fetchall()
                if any(_instant(row[1], "stored modified") > modified for row in rows):
                    stale += 1
                    continue
                for old_id, old_modified in rows:
                    if old_id != event_id and _instant(old_modified, "stored modified") <= modified:
                        db.execute("UPDATE mobile_context_events SET deleted=1,last_received_at=? WHERE device_id=? AND participant_key=? AND event_id=?",
                                   (received, device_id, participant, old_id))
                        superseded += 1
                db.execute("""INSERT INTO mobile_context_groups VALUES(?,?,?,?,0)
                    ON CONFLICT(device_id,participant_key,group_id) DO UPDATE SET
                    source_modified_at=excluded.source_modified_at,deleted=0""",
                    (device_id, participant, group_id, modified.isoformat()))
            db.execute("""INSERT INTO mobile_context_events
                (device_id,participant_key,event_id,group_id,kind,start,end,value,source_modified_at,
                 client_observed_at,first_received_at,last_received_at,deleted,payload)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                ON CONFLICT(device_id,participant_key,event_id) DO UPDATE SET
                 group_id=excluded.group_id,kind=excluded.kind,start=excluded.start,end=excluded.end,
                 value=excluded.value,source_modified_at=excluded.source_modified_at,
                 client_observed_at=excluded.client_observed_at,last_received_at=excluded.last_received_at,
                 first_received_at=excluded.first_received_at,deleted=0,payload=excluded.payload""",
                (device_id, participant, event_id, group_id, kind, start.isoformat(), end.isoformat() if end else None,
                 value, modified.isoformat(), observed.isoformat() if observed else None, received, received, packed))
            accepted += 1
        for group_id, modified in clears:
            group_state = db.execute("SELECT source_modified_at FROM mobile_context_groups WHERE device_id=? AND participant_key=? AND group_id=?",
                                     (device_id, participant, group_id)).fetchone()
            if group_state and _instant(group_state[0], "stored modified") > modified:
                stale += 1
                continue
            db.execute("UPDATE mobile_context_events SET deleted=1,last_received_at=? WHERE device_id=? AND participant_key=? AND group_id=? AND deleted=0",
                       (received, device_id, participant, group_id))
            superseded += db.execute("SELECT changes()").fetchone()[0]
            db.execute("""INSERT INTO mobile_context_groups VALUES(?,?,?,?,1)
                ON CONFLICT(device_id,participant_key,group_id) DO UPDATE SET
                source_modified_at=excluded.source_modified_at,deleted=1""",
                (device_id, participant, group_id, modified.isoformat()))
        for scope in scopes:
            ids = {row[0] for row in events if row[2] == scope}
            candidates = db.execute("SELECT event_id FROM mobile_context_events WHERE device_id=? AND participant_key=? AND kind=? AND deleted=0 AND start>=? AND start<?",
                                    (device_id, participant, scope, range_start.isoformat(), range_end.isoformat())).fetchall()
            for (old_id,) in candidates:
                if old_id not in ids:
                    db.execute("UPDATE mobile_context_events SET deleted=1,last_received_at=? WHERE device_id=? AND participant_key=? AND event_id=?",
                               (received, device_id, participant, old_id))
                    deleted += 1
        db.execute("UPDATE mobile_devices SET last_sync=? WHERE id=?", (received, device_id))
    return {"accepted": accepted, "unchanged": unchanged, "stale": stale,
            "superseded": superseded, "deleted": deleted, "received_at": received}


def list_events(store, device_id=None, participant_key=None, as_of=None):
    cutoff = None
    if as_of is not None:
        if isinstance(as_of, datetime):
            if as_of.tzinfo is None:
                raise ValueError("as_of")
            cutoff = as_of.astimezone(timezone.utc)
        else:
            cutoff = _instant(as_of, "as_of")
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mobile_context_events'").fetchone()
    if not exists:
        return []
    sql = """SELECT device_id,participant_key,event_id,group_id,kind,start,end,value,
        source_modified_at,client_observed_at,first_received_at,last_received_at,payload
        FROM mobile_context_events WHERE deleted=0"""
    clauses, args = [], []
    if device_id is not None:
        clauses.append("device_id=?")
        args.append(device_id)
    if participant_key is not None:
        clauses.append("participant_key=?")
        args.append(participant_key)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY start,event_id"
    with store.connect() as db:
        rows = db.execute(sql, tuple(args)).fetchall()
    result = []
    for row in rows:
        try:
            version_received = _instant(row[10], "stored receipt") if row[10] is not None else None
        except ValueError:
            version_received = None
        if cutoff is not None and (version_received is None or not version_received < cutoff):
            continue
        try:
            last_received = _instant(row[11], "stored receipt") if row[11] is not None else None
        except ValueError:
            last_received = None
        try:
            raw = json.loads(row[12])
            if not isinstance(raw, dict):
                raw = {}
        except (TypeError, ValueError):
            raw = {}
        received_text = row[10] if version_received is not None else None
        result.append({
            "device_id": row[0], "participant_key": row[1], "event_id": row[2],
            "group_id": row[3], "kind": row[4], "occurrence_start": row[5],
            "occurrence_end": row[6], "value": row[7], "source_modified_at": row[8],
            "device_observed_at": row[9], "version_received_at": received_text,
            "last_received_at": row[11] if last_received is not None else None,
            "available_at": received_text, "source": raw.get("source"),
            "details": raw.get("details") if "details" in raw else None,
        })
    return result


def summary(store, device_id=None):
    rows = list_events(store, device_id)
    counts = {kind: 0 for kind in sorted(KINDS)}
    for row in rows:
        counts[row["kind"]] += 1
    return {"total": len(rows), "by_kind": counts}
