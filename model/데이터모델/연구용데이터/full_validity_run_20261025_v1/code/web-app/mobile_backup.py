"""Authenticated phone snapshots, isolated from the PC participant diary."""
from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Seoul")
MAX_BYTES = 1_000_000

class BackupConflict(ValueError):
    pass

def initialize(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS mobile_daily_backups(
            device_id TEXT NOT NULL, participant_key TEXT NOT NULL, day TEXT NOT NULL,
            revision INTEGER NOT NULL, generated_at TEXT NOT NULL, received_at TEXT NOT NULL,
            sha256 TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(device_id, participant_key, day, revision))""")

def _bounded(value, depth=0):
    if depth > 16:
        raise ValueError("Backup nesting too deep")
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("Too many backup fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("Invalid field name")
            if re.sub(r"[^a-z0-9]", "", key.lower()) in {"devicetoken", "accesstoken", "authtoken", "refreshtoken", "token", "secret", "password", "privatekey", "pairinguri"}:
                raise ValueError("Credentials do not belong in backups")
            _bounded(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 500:
            raise ValueError("Backup list too long")
        for child in value:
            _bounded(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 20000:
            raise ValueError("Backup text too long")
    elif type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError("Invalid backup number")
    elif value is not None and type(value) is not bool:
        raise ValueError("Invalid backup value")

def _aware(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp required")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(ZONE)

def receive(store, device_id, payload, now):
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "day", "revision", "generated_at", "participant_key", "data"
    }:
        raise ValueError("Invalid backup envelope")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Unsupported backup schema")
    day = payload["day"]
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Invalid backup day")
    revision = payload["revision"]
    if type(revision) is not int or not 1 <= revision <= 2_147_483_647:
        raise ValueError("Invalid backup revision")
    key = payload["participant_key"]
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
        raise ValueError("Invalid participant key")
    generated = _aware(payload["generated_at"])
    now = now.astimezone(ZONE)
    if generated.date().isoformat() != day or generated > now + timedelta(minutes=5):
        raise ValueError("Invalid generation time")
    data = payload["data"]
    if not isinstance(data, dict) or set(data) != {"profile", "records", "recommendation", "forecast", "outcomes"}:
        raise ValueError("Invalid backup data")
    if not isinstance(data["profile"], dict) or not isinstance(data["records"], list):
        raise ValueError("Invalid profile or records")
    enrollment = _aware(data["profile"].get("created_at"))
    if enrollment > generated:
        raise ValueError("Invalid enrollment time")
    if any(not isinstance(record, dict) for record in data["records"]):
        raise ValueError("Invalid record list")
    for field in ("recommendation", "forecast", "outcomes"):
        if data[field] is not None and not isinstance(data[field], dict):
            raise ValueError("Invalid snapshot section")
    _bounded(payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(canonical.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Backup too large")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    initialize(store)
    with store.connect() as db:
        old = db.execute("SELECT sha256 FROM mobile_daily_backups WHERE device_id=? AND participant_key=? AND day=? AND revision=?",
                         (device_id, key, day, revision)).fetchone()
        if old and old[0] != digest:
            raise BackupConflict("Existing backup revision differs")
        if not old:
            db.execute("INSERT INTO mobile_daily_backups VALUES(?,?,?,?,?,?,?,?)",
                       (device_id, key, day, revision, generated.isoformat(), now.isoformat(), digest, canonical))
        latest = db.execute("SELECT MAX(revision) FROM mobile_daily_backups WHERE device_id=? AND participant_key=? AND day=?",
                            (device_id, key, day)).fetchone()[0]
    return {"accepted": True, "day": day, "revision": revision,
            "stored_revision": latest, "duplicate": bool(old)}

def snapshots(store, limit=90):
    initialize(store)
    with store.connect() as db:
        rows = db.execute("""SELECT b.device_id,b.received_at,b.sha256,b.payload
            FROM mobile_daily_backups b JOIN
              (SELECT device_id,participant_key,day,MAX(revision) revision FROM mobile_daily_backups
               GROUP BY device_id,participant_key,day) latest
            USING(device_id,participant_key,day,revision)
            ORDER BY b.received_at DESC,b.day DESC LIMIT ?""", (min(365, max(1, int(limit))),)).fetchall()
    return [dict(json.loads(payload), device_id=device, received_at=received, sha256=digest,
                 validation_scope="envelope_and_bounds_only",
                 analysis_status="phone_reported_snapshot_not_independently_verified")
            for device, received, digest, payload in rows]

def audit_history(store, device_id, participant_key, limit=500):
    """Oldest retained immutable versions for one participant; never mix identities.

    This is backup appearance history, not the phone's initial registration log.
    """
    bound=min(500,max(1,int(limit)))
    with store.connect() as db:
        rows=db.execute("""SELECT device_id,received_at,sha256,payload FROM mobile_daily_backups
            WHERE device_id=? AND participant_key=? ORDER BY received_at,day,revision LIMIT ?""",
            (device_id,participant_key,bound+1)).fetchall()
    return {'truncated':len(rows)>bound,'snapshots':[
        dict(json.loads(payload),device_id=device,received_at=received,sha256=digest)
        for device,received,digest,payload in rows[:bound]]}


def summary(store):
    initialize(store)
    with store.connect() as db:
        versions, received = db.execute("SELECT COUNT(*),MAX(received_at) FROM mobile_daily_backups").fetchone()
        days = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM mobile_daily_backups GROUP BY device_id,participant_key,day)").fetchone()[0]
        people = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM mobile_daily_backups GROUP BY device_id,participant_key)").fetchone()[0]
    return {"days": days, "versions": versions, "participants": people, "last_received_at": received,
            "role": "phone_snapshots_separate_from_pc_diary", "cloud_upload": False,
            "validation_scope": "envelope_and_bounds_only",
            "analysis_status": "phone_reported_snapshot_not_independently_verified"}
