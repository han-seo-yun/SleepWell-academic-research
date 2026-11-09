#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from contextlib import contextmanager
import hashlib
import threading
import uuid
from urllib.parse import urlparse, parse_qs

from engine import ZONE, parameters, recommend, validate_profile, validate_record, parse_date, plan_night_id
from research import ResearchBridge, sanitize_archive_latency
from night_forecast import NightForecasts
from fusion_forecast import FusionForecasts
from habit_outcomes import summarize_habits
from night_core import cutoff_for_onset
from daily_context import validate_daily_context
from prospective import build_forecast, evaluate_forecast, summarize_evaluations
from research_console import build_console

ROOT = Path(__file__).resolve().parent


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (date TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, generated_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_context_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, night_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS daily_context_night ON daily_context_versions(night_id, id);
                CREATE TABLE IF NOT EXISTS prospective_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, night_id TEXT NOT NULL UNIQUE,
                    generated_at TEXT NOT NULL, cutoff TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS prospective_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, forecast_id INTEGER NOT NULL,
                    record_date TEXT NOT NULL, observed_at TEXT NOT NULL,
                    outcome_hash TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(forecast_id, outcome_hash));
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        """Commit/rollback and close explicitly; sqlite's own context does not close."""
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def profile(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM profile WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def records(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM records ORDER BY date")]

    def record_versions(self, as_of=None):
        """Retain the version known at a forecast cutoff after a later diary edit."""
        versions = self.records()
        with self.connect() as db:
            for at, payload in db.execute("SELECT at,data FROM events WHERE kind='record_saved' ORDER BY id"):
                event = json.loads(payload)
                previous = event.get("previous")
                if isinstance(previous, dict):
                    replaced_at = event.get("record", {}).get("observed_at") or at
                    if as_of is None or as_of < parse_date(replaced_at):
                        versions.append(previous)
        if as_of is not None:
            versions = [r for r in versions if r.get("observed_at") and parse_date(r["observed_at"]) <= as_of]
        return versions

    def predictions(self):
        with self.connect() as db:
            return [dict(json.loads(r[1]), id=r[0]) for r in db.execute("SELECT id,data FROM predictions ORDER BY id DESC")]

    def event(self, db, kind, data, now=None):
        db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", ((now or datetime.now(ZONE)).isoformat(), kind, json.dumps(data, ensure_ascii=False)))

    def save_profile(self, p, archive, now=None):
        now = now or datetime.now(ZONE)
        previous = self.profile()
        p["created_at"] = previous["created_at"] if previous else now.isoformat()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO profile VALUES(1,?)", (json.dumps(p, ensure_ascii=False),))
            if p["link_history"]:
                for r in archive:
                    db.execute("INSERT OR IGNORE INTO records VALUES(?,?)", (r["date"], json.dumps(r, ensure_ascii=False)))
            self.event(db, "profile_updated" if previous else "enrolled", p, now)

    def save_record(self, record, now=None):
        record = dict(record)
        replaces_date = record.pop("replaces_date", None)
        if replaces_date:
            if datetime.strptime(replaces_date, "%Y-%m-%d").date().isoformat() != replaces_date:
                raise ValueError("수정할 기록의 날짜를 확인해주세요")
        lookup_date = replaces_date or record["date"]
        with self.connect() as db:
            old = db.execute("SELECT data FROM records WHERE date=?", (lookup_date,)).fetchone()
            if replaces_date and not old:
                raise ValueError("수정할 기록이 없습니다. 목록을 새로고침해주세요.")
            if replaces_date and replaces_date != record["date"] and db.execute(
                    "SELECT 1 FROM records WHERE date=?", (record["date"],)).fetchone():
                raise ValueError("변경할 기상 날짜에 이미 다른 수면 기록이 있습니다.")
            previous = json.loads(old[0]) if old else None
            record["record_id"] = (previous or {}).get("record_id") or uuid.uuid4().hex
            if previous:
                previous = dict(previous, record_id=record["record_id"])
            self.event(db, "record_saved", {"previous": previous, "record": record}, now)
            if replaces_date and replaces_date != record["date"]:
                db.execute("DELETE FROM records WHERE date=?", (replaces_date,))
            db.execute("INSERT OR REPLACE INTO records VALUES(?,?)", (record["date"], json.dumps(record, ensure_ascii=False)))
            self._assess_record(db, record, lookup_date)

    def save_prediction(self, result):
        with self.connect() as db:
            cur = db.execute("INSERT INTO predictions(generated_at,data) VALUES(?,?)", (result["generated_at"], json.dumps(result, ensure_ascii=False)))
            return cur.lastrowid

    def profile_at(self, cutoff):
        """Use server-stamped profile revisions, never today's settings retroactively."""
        with self.connect() as db:
            rows = db.execute("SELECT at,data FROM events WHERE kind IN ('enrolled','profile_updated') ORDER BY id DESC")
            for at, data in rows:
                if parse_date(at) <= cutoff:
                    return json.loads(data)
        return None

    def save_context(self, context):
        with self.connect() as db:
            cur = db.execute("INSERT INTO daily_context_versions(night_id,observed_at,data) VALUES(?,?,?)",
                             (context["night_id"], context["observed_at"],
                              json.dumps(context, ensure_ascii=False, allow_nan=False)))
            return cur.lastrowid

    def context_for(self, night_id, as_of=None):
        with self.connect() as db:
            rows = db.execute("SELECT id,observed_at,data FROM daily_context_versions WHERE night_id=? ORDER BY id DESC", (night_id,))
            for identity, observed, data in rows:
                if as_of is None or parse_date(observed) <= as_of:
                    return dict(json.loads(data), revision_id=identity)
        return None

    def contexts(self):
        with self.connect() as db:
            return [dict(json.loads(data), revision_id=identity)
                    for identity, data in db.execute("SELECT id,data FROM daily_context_versions ORDER BY id")]

    def prospective_forecasts(self):
        with self.connect() as db:
            return [dict(json.loads(data), id=identity)
                    for identity, data in db.execute("SELECT id,data FROM prospective_forecasts ORDER BY id")]

    def save_prospective(self, forecast):
        """A unique night and insert-only path freeze the first real-time capture."""
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO prospective_forecasts(night_id,generated_at,cutoff,data) VALUES(?,?,?,?)",
                       (forecast["night_id"], forecast["generated_at"], forecast["cutoff"],
                        json.dumps(forecast, ensure_ascii=False, allow_nan=False)))
            row = db.execute("SELECT id,data FROM prospective_forecasts WHERE night_id=?", (forecast["night_id"],)).fetchone()
            return dict(json.loads(row[1]), id=row[0])

    def _assess_record(self, db, record, prior_date=None):
        if record.get("source") not in ("diary", "healthkit", "health_connect") or not record.get("observed_at"):
            return
        # Once paired, a correction remains attached to the same forecast.
        # A shifted date must not turn one corrected sleep into two primary outcomes.
        paired = set()
        for forecast_id, old_date, data in db.execute(
                "SELECT forecast_id,record_date,data FROM prospective_assessments ORDER BY id"):
            previous = json.loads(data)
            same_id = previous.get("record_id") == record["record_id"]
            legacy_same_day = not previous.get("record_id") and old_date == (prior_date or record["date"])
            if same_id or legacy_same_day:
                paired.add(forecast_id)
        if paired:
            rows = [db.execute("SELECT id,data FROM prospective_forecasts WHERE id=?", (identity,)).fetchone()
                    for identity in paired]
        else:
            onset = parse_date(record.get("sleep_start") or record["bed"])
            if not record.get("sleep_start"):
                onset += timedelta(minutes=float(record.get("latency_min") or 0))
            night = cutoff_for_onset(onset).date().isoformat()
            rows = db.execute("SELECT id,data FROM prospective_forecasts WHERE night_id=?", (night,)).fetchall()
        outcome_hash = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                                allow_nan=False).encode()).hexdigest()
        for identity, data in rows:
            forecast = dict(json.loads(data), id=identity)
            result = evaluate_forecast(forecast, record)
            result["forecast_id"] = identity
            result["record_id"] = record["record_id"]
            result["outcome_hash"] = outcome_hash
            result["reported_record_snapshot"] = dict(record)
            db.execute(
                "INSERT OR IGNORE INTO prospective_assessments(forecast_id,record_date,observed_at,outcome_hash,data) VALUES(?,?,?,?,?)",
                (identity, record["date"], record["observed_at"], outcome_hash,
                 json.dumps(result, ensure_ascii=False, allow_nan=False)))

    def assessments(self):
        with self.connect() as db:
            return [dict(json.loads(data), assessment_id=identity, forecast_id=forecast_id)
                    for identity, forecast_id, data in db.execute(
                        "SELECT id,forecast_id,data FROM prospective_assessments ORDER BY id")]


class Application:
    def __init__(self, store, research, night_models=None, clock=None):
        self.store, self.research = store, research
        self.night_models = night_models or NightForecasts(ROOT / "models")
        self.fusion_models = FusionForecasts(ROOT / "models")
        self.token = secrets.token_urlsafe(32)
        self.clock = clock or (lambda: datetime.now(ZONE))
        self.lock = threading.RLock()
        self.capture_error = None

    def ensure_due(self, now=None):
        """Called by server timer and UI requests. Never fills missed past nights."""
        with self.lock:
            now = now or self.clock()
            cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now < cutoff:
                return None
            night = cutoff.date().isoformat()
            existing = next((f for f in self.store.prospective_forecasts() if f["night_id"] == night), None)
            if existing:
                return existing
            profile = self.store.profile_at(cutoff)
            if profile is None:
                return None
            fusion = self.fusion_models.predict_at(profile, self.store.record_versions(cutoff), cutoff)
            if not fusion.get("available"):
                self.capture_error = "CMI·MESA 연구 모델을 확인할 수 없어 오늘 사전 예측을 저장하지 않았습니다. 배포 파일을 확인해주세요."
                return None
            forecast = build_forecast(profile, self.store.record_versions(cutoff),
                                      self.store.context_for(night, cutoff), cutoff, now,
                                      fusion_result=fusion)
            saved = self.store.save_prospective(forecast)
            self.capture_error = None
            return saved

    def prospective_state(self, now):
        forecasts = self.store.prospective_forecasts()
        assessments = self.store.assessments()
        # Health Connect can retract or supersede an imported episode.  Keep the
        # append-only assessment artifacts, but exclude those no longer backed by
        # a current imported record from both live metric views.  Diary assessments
        # retain the existing first-report contract.
        # A user correction changes source to diary but Store.save_record retains
        # the episode identity.  Presence of that record_id, rather than its current
        # source label, determines whether the original Health Connect report still
        # belongs to a live episode.
        current_record_ids = {record.get("record_id") for record in self.store.records()
                              if record.get("record_id")}
        active_assessments = []
        retracted_health = 0
        for item in assessments:
            snapshot = item.get("reported_record_snapshot") or {}
            is_health = (snapshot.get("source") == "health_connect" or
                         item.get("outcome_snapshot", {}).get("source") == "health_connect")
            if is_health and item.get("record_id") not in current_record_ids:
                retracted_health += 1
                continue
            active_assessments.append(item)
        first, latest, diary_first, wearable_first = {}, {}, {}, {}
        for item in active_assessments:
            first.setdefault(item["forecast_id"], item)
            latest[item["forecast_id"]] = item
            source = (item.get("outcome_snapshot") or {}).get("source")
            if source == "diary":
                diary_first.setdefault(item["forecast_id"], item)
            elif source in ("healthkit", "health_connect"):
                wearable_first.setdefault(item["forecast_id"], item)
        summary = summarize_evaluations(forecasts, list(first.values()))
        corrected = summarize_evaluations(forecasts, list(latest.values()))
        summary["retracted_health_connect_assessments"] = retracted_health
        corrected["retracted_health_connect_assessments"] = retracted_health
        corrected["explanation"] = (
            "동일하게 고정된 사전 예측을 수정된 최신 실제 결과와 비교한 참고 집계입니다. "
            "최초 결과 기준 주 평가를 대체하지 않습니다.")
        summary["corrections_count"] = len(active_assessments) - len(first)
        next_cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now >= next_cutoff:
            next_cutoff += timedelta(days=1)
        return {"current": forecasts[-1] if forecasts else None, "summary": summary,
                "primary_diary_summary": summarize_evaluations(forecasts, list(diary_first.values())),
                "auxiliary_wearable_summary": summarize_evaluations(forecasts, list(wearable_first.values())),
                "corrected_summary": corrected, "next_decision_at": next_cutoff.isoformat(),
                "capture_error": self.capture_error,
                "retracted_health_connect_assessments": retracted_health,
                "note": "매일 20시 이전 입력으로 첫 예측을 고정합니다. 서버가 실행 중이어야 자동 저장됩니다. "
                        "20:15 이후 생성은 주 평가에서 제외하며, 과거 밤을 소급 생성하지 않습니다. "
                        "주 평가는 첫 결과 입력, 수정 결과는 별도 집계합니다."}

    def visible_records(self, profile):
        visible = []
        rejected = 0
        for stored in self.store.records():
            if not profile["link_history"] and stored["source"] == "archive":
                continue
            record = dict(stored)
            if record["source"] == "archive":
                raw_latency = record.get("latency_min")
                record["latency_min"] = sanitize_archive_latency(raw_latency)
                rejected += int(raw_latency is not None and record["latency_min"] is None)
            visible.append(record)
        if isinstance(getattr(self.research, "summary", None), dict):
            self.research.summary["stored_archive_latency_rejected_count"] = rejected
        return visible

    def generate(self):
        profile = self.store.profile()
        if not profile: raise ValueError("먼저 시작 정보를 입력해주세요")
        records = self.visible_records(profile)
        now = self.clock()
        self.ensure_due(now)
        context = self.store.context_for(plan_night_id(profile, now), now)
        result = recommend(profile, records, now, self.research.summary, daily_context=context)
        result["behavioral_forecast"] = self.research.behavioral_forecast(profile, records, now)
        historical_cutoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
        result["next_night_forecast"] = self.night_models.predict(profile, self.store.record_versions(historical_cutoff), now)
        result["fusion_forecast"] = self.fusion_models.predict(
            profile, self.store.record_versions(historical_cutoff), now)
        result["input_hash"] = hashlib.sha256(json.dumps([profile, records, context], sort_keys=True).encode()).hexdigest()
        result["daily_context_snapshot"] = context
        result["profile_snapshot"] = profile
        result["observed_record_count"] = len(records)
        result["id"] = self.store.save_prediction(result)
        return result

    def state(self):
        now = self.clock()
        self.ensure_due(now)
        profile = self.store.profile()
        records = self.visible_records(profile) if profile else []
        predictions = self.store.predictions() if profile else []
        feedback = []
        for r in records:
            if r["source"] == "archive": continue
            # Last plan created before actual bedtime and targeting that morning.
            matched = next((p for p in predictions if parse_date(p["generated_at"]) <= parse_date(r["bed"])
                            and parse_date(p["wake"]).date() == parse_date(r["wake"]).date()), None)
            feedback.append({"date": r["date"], "rested": r.get("rested"), "fatigue": r.get("fatigue"),
                             "recommendation_id": matched["id"] if matched else None,
                             "bed_difference_min": (parse_date(r["bed"]) - parse_date(matched["bed"])).total_seconds() / 60 if matched else None})
        status_fn = getattr(self.research, "model_status", None)
        model_status = status_fn(profile, records, now) if callable(status_fn) else {
            "available": False, "status": "unavailable", "reason": "행동 예측 모델 상태 정보가 없습니다.",
            "model_version": None, "source": None}
        return {"csrf_token": self.token, "profile": profile, "records": records,
                "recommendation": predictions[0] if predictions else None,
                "prediction_count": len(predictions), "feedback": feedback,
                "personalization": parameters(profile, records, now, self.research.summary) if profile else None,
                "research": self.research.summary, "model_metrics": self.research.metrics,
                "model_status": model_status, "fusion_status": self.fusion_models.status(),
                "habit_outcomes": summarize_habits(profile, records, now) if profile else None,
                "daily_context": self.store.context_for(now.date().isoformat(), now),
                "context_night_id": now.date().isoformat(), "prospective": self.prospective_state(now),
                "data_readiness": getattr(self.research, "data_readiness", {"available": False, "status": "not_generated", "reason": "데이터 준비 상태 정보가 없습니다."}),
                "storage": str(self.store.path), "today": now.date().isoformat()}

    def research_console(self):
        """Aggregate model evidence and isolated phone reports without a PC profile."""
        return build_console(self.store, ROOT / "models", self.clock())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Avoid logging profile fields or health records.

    def respond(self, data, status=200, content_type="application/json; charset=utf-8", download=False):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download: self.send_header("Content-Disposition", 'attachment; filename="sleepwell-records.json"')
        self.end_headers(); self.wfile.write(body)

    def allowed_host(self):
        return self.headers.get("Host") in [f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"]

    def do_GET(self):
        if not self.allowed_host(): return self.respond({"error": "Invalid host"}, 403)
        path = urlparse(self.path).path
        app = self.server.application
        try:
            if path == "/api/state": return self.respond(app.state())
            if path == "/api/research-console": return self.respond(app.research_console())
            if path == "/api/context":
                night = parse_qs(urlparse(self.path).query).get("night_id", [app.clock().date().isoformat()])[0]
                if datetime.strptime(night, "%Y-%m-%d").date().isoformat() != night:
                    raise ValueError("날짜를 확인해주세요")
                return self.respond({"night_id": night, "context": app.store.context_for(night, app.clock())})
            if path == "/api/export":
                state = app.state(); state.pop("csrf_token"); state["predictions"] = app.store.predictions()
                state["daily_context_versions"] = app.store.contexts()
                state["prospective_forecasts"] = app.store.prospective_forecasts()
                state["prospective_assessments"] = app.store.assessments()
                with app.store.connect() as db:
                    state["change_events"] = [{"at": r[0], "kind": r[1], "data": json.loads(r[2])} for r in db.execute("SELECT at,kind,data FROM events ORDER BY id")]
                return self.respond(state, download=True)
            routes = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/mobile.js": ("mobile.js", "text/javascript; charset=utf-8"), "/research-console.js": ("research-console.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in routes: return self.respond({"error": "Not found"}, 404)
            file, mime = routes[path]
            return self.respond((ROOT / "static" / file).read_bytes(), content_type=mime)
        except (ValueError, TypeError):
            return self.respond({"error": "요청 날짜 또는 형식을 확인해주세요."}, 400)
        except Exception:
            return self.respond({"error": "자료를 읽는 중 오류가 발생했습니다. 서버 실행 환경을 확인해주세요."}, 500)

    def do_POST(self):
        app = self.server.application
        origin = self.headers.get("Origin")
        allowed = [f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"]
        if not self.allowed_host() or (origin and origin not in allowed) or self.headers.get("X-SleepWell-Token") != app.token:
            return self.respond({"error": "요청 출처를 확인할 수 없습니다. 화면을 새로고침해주세요."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 100000: raise ValueError("입력 크기가 너무 큽니다")
            data = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if not isinstance(data, dict):
                raise ValueError("입력은 객체 형식이어야 합니다")
            with app.lock:
                now = app.clock()
                app.ensure_due(now)
                if path == "/api/profile":
                    p = validate_profile(data)
                    app.store.save_profile(p, app.research.records, now)
                    app.generate()
                elif path == "/api/record":
                    if not app.store.profile(): raise ValueError("먼저 시작 정보를 입력해주세요")
                    record = validate_record(data, now)
                    app.store.save_record(record, now)
                    app.generate()
                elif path == "/api/context":
                    if not app.store.profile(): raise ValueError("먼저 시작 정보를 입력해주세요")
                    app.store.save_context(validate_daily_context(data, now))
                    app.generate()
                elif path == "/api/recommend": app.generate()
                else: return self.respond({"error": "Not found"}, 404)
            return self.respond(app.state())
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            return self.respond({"error": str(error)}, 400)
        except Exception:
            return self.respond({"error": "저장 또는 계산을 완료하지 못했습니다. 기록을 새로고침해 확인해주세요."}, 500)


def capture_loop(app, stop, interval=30):
    while not stop.is_set():
        try:
            app.ensure_due()
        except Exception:
            # No health values or profile information in operational status.
            app.capture_error = "자동 예측 저장에 실패했습니다. 앱과 저장 공간을 확인해주세요."
        stop.wait(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=ROOT / ".local/sleepwell.sqlite3")
    parser.add_argument("--workspace", type=Path, default=next((p for p in ROOT.parents if (p / "WORKSPACE_CONTEXT.md").is_file()), ROOT.parent.parent))
    parser.add_argument("--forecast-model", type=Path,
                        help="엄격한 web_completed_history_pre20_v1 행동 예측 JSON (선택)")
    parser.add_argument("--mesa-inventory", type=Path, default=ROOT / ".local/mesa_inventory.json",
                        help="원본/ID/경로가 없는 MESA 집계 점검 JSON")
    args = parser.parse_args()
    app = Application(Store(args.database), ResearchBridge(args.workspace, args.forecast_model, args.mesa_inventory))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.application = app
    stop = threading.Event()
    timer = threading.Thread(target=capture_loop, args=(app, stop), daemon=True)
    timer.start()
    print(f"SleepWell: http://127.0.0.1:{args.port}  |  기록: {args.database}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        timer.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
