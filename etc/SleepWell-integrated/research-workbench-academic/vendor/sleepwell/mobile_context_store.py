"""Versioned, fail-closed storage for non-sleep mobile context observations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re


KINDS = {"steps", "exercise", "caffeine", "schedule", "nap"}
SENSOR_KINDS = {"steps", "exercise"}
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
PARTICIPANT_RE = re.compile(r"[0-9a-f]{32}\Z")


def _instant(value, name):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(name) from error
    if parsed.tzinfo is None:
        raise ValueError(name)
    return parsed.astimezone(timezone.utc)


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
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise ValueError("schema_version")
    participant = payload.get("participant_key")
    if not isinstance(participant, str) or not PARTICIPANT_RE.fullmatch(participant):
        raise ValueError("participant_key")
    generated = _instant(payload.get("generated_at"), "generated_at")
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
        if not isinstance(item, dict) or set(item) - {"id", "group_id", "kind", "start", "end", "value", "modified_at", "observed_at", "source"}:
            raise ValueError("event")
        event_id, kind = item.get("id"), item.get("kind")
        if not isinstance(event_id, str) or not ID_RE.fullmatch(event_id) or not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("event identity")
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
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1000:
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
            "superseded": superseded, "deleted": deleted}


def list_events(store, device_id=None, participant_key=None):
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mobile_context_events'").fetchone()
    if not exists:
        return []
    sql = "SELECT device_id,participant_key,event_id,kind,start,end,value,first_received_at,client_observed_at,payload FROM mobile_context_events WHERE deleted=0"
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
    return [{"device_id": row[0], "participant_key": row[1], "event_id": row[2], "kind": row[3],
             "start": row[4], "end": row[5], "value": row[6], "observed_at": row[7],
             "client_observed_at": row[8], "source": json.loads(row[9])["source"]} for row in rows]


def summary(store, device_id=None):
    rows = list_events(store, device_id)
    counts = {kind: 0 for kind in sorted(KINDS)}
    for row in rows:
        counts[row["kind"]] += 1
    return {"total": len(rows), "by_kind": counts}
