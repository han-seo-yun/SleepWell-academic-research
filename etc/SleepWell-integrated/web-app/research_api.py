"""Read-only researcher REST API (/api/v1) for the unified SleepWell shell.

Modelled on the participant/compliance APIs of Beiwe and RADAR-base: one versioned,
self-describing, read-only surface that the GUI, the research lab and external
scripts share. Writes stay on the existing CSRF-protected POST routes.

Every participant view is computed from the same identity-separated snapshots as
the research lab (research_lab_import.AppLabBridge), so a participant's rows here,
in the lab, and in the CSV export are identical.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import io
import re

from research_lab import CANONICAL_FIELDS, LabInputError, _normalize_rows
from research_lab_import import AppLabBridge
from sleep_observation import ASSUMPTIONS, describe_observations

API_VERSION = "v1"
RECENT_DAYS = 14
STALE_RECORD_DAYS = 3
STALE_SYNC_DAYS = 2
LOW_COVERAGE = 0.5
HIGH_LATE_SHARE = 0.3
_PARTICIPANT_ID = re.compile(r"^(pc|phone-[0-9a-f]{16})$")


def _bridge(store, version):
    return AppLabBridge(store, "", version)


def _instant(value):
    return datetime.fromisoformat(value) if value else None


def _device_syncs(store):
    try:
        with store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "mobile_devices" not in tables:
                return {}
            return {device: (name, last_sync) for device, name, last_sync in
                    db.execute("SELECT id,name,last_sync FROM mobile_devices")}
    except Exception:
        return {}


def _identities(bridge):
    sources = bridge._sources()
    return [("pc", None)] + [(key, sources[key][0]) for key in sources]


def _participant_rows(bridge, participant_id):
    snapshot = bridge.snapshot(participant_id)
    records = snapshot["payload"]["records"]
    rows = _normalize_rows(records) if records else []
    return snapshot, rows


def _recent_strip(completeness, today):
    grid = {cell["date"]: cell["status"] for cell in (completeness or {}).get("grid", [])}
    strip = []
    for offset in range(RECENT_DAYS - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        strip.append({"date": day, "status": grid.get(day, "missing")})
    return strip


def _summary(participant_id, device, bridge, syncs, now):
    snapshot, rows = _participant_rows(bridge, participant_id)
    observed = describe_observations(rows) if rows else {"regularity": [], "completeness": []}
    regularity = observed["regularity"][0] if observed["regularity"] else {}
    completeness = observed["completeness"][0] if observed["completeness"] else {}
    # Today's 20:00 decision has no outcome yet; the strip ends at yesterday's night.
    strip = _recent_strip(completeness, now.date() - timedelta(days=1))
    observed_recent = sum(cell["status"] in ("timely", "late") for cell in strip)
    labeled = [row for row in rows if row["sleep_start"] is not None]
    last_wake = max((row["sleep_end"] for row in labeled), default=None)
    days_since = (now - last_wake).total_seconds() / 86400.0 if last_wake else None
    name, last_sync = syncs.get(device, (None, None)) if device else (None, None)
    sync_age = (now - _instant(last_sync)).total_seconds() / 86400.0 if last_sync else None

    alerts = []
    if not labeled:
        alerts.append({"code": "no_records", "level": "info", "text": "분석 가능한 수면 기록이 아직 없습니다."})
    elif days_since is not None and days_since >= STALE_RECORD_DAYS:
        alerts.append({"code": "stale_records", "level": "warning",
                       "text": f"마지막 수면 기록 후 {days_since:.0f}일이 지났습니다."})
    if labeled and observed_recent / RECENT_DAYS < LOW_COVERAGE:
        alerts.append({"code": "low_coverage", "level": "warning",
                       "text": f"최근 {RECENT_DAYS}일 중 관측된 밤이 {observed_recent}일입니다."})
    share = completeness.get("timely_share_of_labeled")
    if share is not None and labeled and 1.0 - share > HIGH_LATE_SHARE:
        alerts.append({"code": "late_reports", "level": "warning",
                       "text": f"관측 밤의 {round((1.0 - share) * 100)}%가 다음 20시 이후에 입력되어 다음 예측에 쓰이지 못했습니다."})
    if device and (sync_age is None or sync_age >= STALE_SYNC_DAYS):
        alerts.append({"code": "stale_sync", "level": "warning",
                       "text": "휴대폰 동기화 기록이 없습니다." if sync_age is None
                       else f"휴대폰 마지막 동기화 후 {sync_age:.0f}일이 지났습니다."})

    return {
        "id": participant_id,
        "kind": "pc" if participant_id == "pc" else "phone",
        "label": "현재 PC 참가자" if participant_id == "pc" else f"휴대폰 참가자 {participant_id[6:14]}",
        "device_name": name,
        "last_sync": last_sync,
        "nights": len(labeled),
        "first_date": completeness.get("first_date"),
        "last_date": completeness.get("last_date"),
        "last_wake": last_wake.isoformat() if last_wake else None,
        "days_since_last_record": None if days_since is None else round(days_since, 1),
        "recent_days": RECENT_DAYS,
        "recent_observed": observed_recent,
        "recent_strip": strip,
        "sri": regularity.get("sri"),
        "midpoint_mean_clock": regularity.get("midpoint_mean_clock"),
        "midpoint_sd_min": regularity.get("midpoint_sd_min"),
        "timely_share_of_labeled": share,
        "context_coverage": snapshot["summary"].get("coverage", {}),
        "alerts": alerts,
    }, snapshot, rows, observed


def list_participants(store, version, now):
    bridge = _bridge(store, version)
    syncs = _device_syncs(store)
    items = []
    for participant_id, device in _identities(bridge):
        try:
            items.append(_summary(participant_id, device, bridge, syncs, now)[0])
        except (ValueError, LabInputError) as error:
            items.append({"id": participant_id, "kind": "pc" if participant_id == "pc" else "phone",
                          "label": participant_id, "nights": 0, "recent_strip": [], "error": str(error),
                          "alerts": [{"code": "unreadable", "level": "warning",
                                      "text": "기록을 분석 형식으로 읽지 못했습니다."}]})
    return {"api_version": API_VERSION, "generated_at": now.isoformat(), "participants": items,
            "thresholds": {"stale_record_days": STALE_RECORD_DAYS, "stale_sync_days": STALE_SYNC_DAYS,
                           "low_coverage": LOW_COVERAGE, "high_late_share": HIGH_LATE_SHARE,
                           "recent_days": RECENT_DAYS},
            "note": "관측 기록의 기술 요약입니다. 모델 정확도·임상 판단·추천 효과가 아닙니다."}


def participant_detail(store, version, participant_id, now):
    if not _PARTICIPANT_ID.match(participant_id or ""):
        raise ValueError("참여자 식별자를 확인해주세요.")
    bridge = _bridge(store, version)
    identities = dict(_identities(bridge))
    if participant_id not in identities:
        raise LookupError("현재 연결된 참여자가 아닙니다.")
    summary, snapshot, rows, observed = _summary(
        participant_id, identities[participant_id], bridge, _device_syncs(store), now)
    nights = []
    for row in rows[-120:]:
        cutoff = row["cutoff"]
        item = {"cutoff": cutoff.isoformat(), "sleep_start": None, "sleep_end": None,
                "observed_at": None, "onset_offset_min": None, "wake_offset_min": None}
        if row["sleep_start"] is not None:
            item.update(
                sleep_start=row["sleep_start"].isoformat(), sleep_end=row["sleep_end"].isoformat(),
                observed_at=row["observed_at"].isoformat(),
                onset_offset_min=round((row["sleep_start"] - cutoff).total_seconds() / 60.0, 1),
                wake_offset_min=round((row["sleep_end"] - cutoff).total_seconds() / 60.0, 1),
                report_delay_min=round((row["observed_at"] - row["sleep_end"]).total_seconds() / 60.0, 1),
                timely=row["observed_at"] < cutoff + timedelta(days=1))
        nights.append(item)
    return {
        "api_version": API_VERSION, "generated_at": now.isoformat(), "participant": summary,
        "regularity": observed["regularity"][0] if observed["regularity"] else None,
        "completeness": observed["completeness"][0] if observed["completeness"] else None,
        "nights": nights, "nights_truncated": max(0, len(rows) - len(nights)),
        "assumptions": ASSUMPTIONS, "warnings": snapshot.get("warnings", []),
        "links": {"records_csv": f"/api/{API_VERSION}/participants/{participant_id}/records.csv"},
    }


def participant_csv(store, version, participant_id):
    if not _PARTICIPANT_ID.match(participant_id or ""):
        raise ValueError("참여자 식별자를 확인해주세요.")
    bridge = _bridge(store, version)
    if participant_id not in dict(_identities(bridge)):
        raise LookupError("현재 연결된 참여자가 아닙니다.")
    records = bridge.snapshot(participant_id)["payload"]["records"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CANONICAL_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({field: record.get(field) or "" for field in CANONICAL_FIELDS})
    return output.getvalue().encode("utf-8-sig")


def overview(app, version, now, extras=None):
    participants = list_participants(app.store, version, now)["participants"]
    alerts = [{"participant_id": item["id"], "participant": item.get("label"), **alert}
              for item in participants for alert in item.get("alerts", []) if alert["level"] != "info"]
    prospective = {}
    try:
        state = app.prospective_state(now)
        summary = (state or {}).get("primary_diary_summary") or (state or {}).get("summary") or {}
        current = (state or {}).get("current") or {}
        prospective = {
            "tonight_captured": current.get("night_id") == now.date().isoformat(),
            "tonight_eligible": bool(current.get("eligible_capture")) if current else False,
            "next_decision_at": (state or {}).get("next_decision_at"),
            "total_forecasts": summary.get("total_forecasts", 0),
            "eligible_captures": summary.get("eligible_captures", 0),
            "evaluated_nights": summary.get("evaluated_nights", 0),
            "pending_nights": summary.get("pending_nights", 0),
            "capture_error": (state or {}).get("capture_error") or app.capture_error,
        }
    except Exception:
        prospective = {"error": "사전 예측 상태를 읽지 못했습니다."}
    fusion = app.fusion_models.status()
    return {
        "api_version": API_VERSION, "generated_at": now.isoformat(), "app_version": version,
        "today": now.date().isoformat(),
        "participants": {"total": len(participants),
                         "phone": sum(item["kind"] == "phone" for item in participants),
                         "with_records": sum((item.get("nights") or 0) > 0 for item in participants),
                         "nights": sum(item.get("nights") or 0 for item in participants)},
        "alerts": alerts,
        "prospective": prospective,
        "profile_ready": bool(app.store.profile()),
        "models": {"available": bool(fusion.get("available")),
                   "runtime_model_version": fusion.get("runtime_model_version") or fusion.get("model_version"),
                   "reason": None if fusion.get("available") else fusion.get("reason")},
        **(extras or {}),
        "scope": "연구 비교용 로컬 소프트웨어입니다. 임상 판단·전향 성능·추천 효과의 증거가 아닙니다.",
    }


def openapi(version):
    def get(summary, description, schema="object", produces="application/json", parameters=None):
        operation = {"summary": summary, "description": description,
                     "responses": {"200": {"description": "OK", "content": {produces: {"schema": {"type": schema}}}}}}
        if parameters:
            operation["parameters"] = parameters
        return {"get": operation}

    participant = [{"name": "participant_id", "in": "path", "required": True,
                    "schema": {"type": "string", "pattern": _PARTICIPANT_ID.pattern}}]
    return {
        "openapi": "3.1.0",
        "info": {"title": "SleepWell researcher API", "version": f"{API_VERSION} (app {version})",
                 "description": "로컬 전용 읽기 API입니다. 127.0.0.1에서만 응답하며 쓰기 요청은 화면의 CSRF 보호 경로만 사용합니다."},
        "servers": [{"url": "/"}],
        "paths": {
            f"/api/{API_VERSION}": get("API 색인", "사용 가능한 경로와 버전."),
            f"/api/{API_VERSION}/system": get("시스템 정보", "앱·모델 버전, 연구 분석실 주소, 휴대폰 수신 상태."),
            f"/api/{API_VERSION}/overview": get("대시보드 요약", "참여자 수, 경고, 오늘의 20시 사전 예측, 모델 상태."),
            f"/api/{API_VERSION}/participants": get("참여자 목록", "참여자별 최근 14일 완결성, SRI, 경고."),
            f"/api/{API_VERSION}/participants/{{participant_id}}": get(
                "참여자 상세", "규칙성·완결성·최근 120밤의 관측 수면 구간.", parameters=participant),
            f"/api/{API_VERSION}/participants/{{participant_id}}/records.csv": get(
                "표준 5열 CSV", "연구 분석실·CLI 입력과 같은 형식(participant_id,cutoff,sleep_start,sleep_end,observed_at).",
                schema="string", produces="text/csv", parameters=participant),
            "/api/export": get("전체 기록 JSON", "현재 PC 참가자의 전체 기록·변경 이력 내려받기."),
            f"/api/{API_VERSION}/openapi.json": get("이 문서", "OpenAPI 3.1 설명."),
        },
    }


def index(version):
    return {"api_version": API_VERSION, "app_version": version,
            "links": {name: f"/api/{API_VERSION}/{name}" for name in
                      ("system", "overview", "participants", "openapi.json")},
            "read_only": True}
