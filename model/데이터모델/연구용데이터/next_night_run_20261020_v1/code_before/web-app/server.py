#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from contextlib import contextmanager
import hashlib
from urllib.parse import urlparse

from engine import ZONE, parameters, recommend, validate_profile, validate_record, parse_date
from research import ResearchBridge

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

    def predictions(self):
        with self.connect() as db:
            return [dict(json.loads(r[1]), id=r[0]) for r in db.execute("SELECT id,data FROM predictions ORDER BY id DESC")]

    def event(self, db, kind, data):
        db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (datetime.now(ZONE).isoformat(), kind, json.dumps(data, ensure_ascii=False)))

    def save_profile(self, p, archive):
        previous = self.profile()
        p["created_at"] = previous["created_at"] if previous else datetime.now(ZONE).isoformat()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO profile VALUES(1,?)", (json.dumps(p, ensure_ascii=False),))
            if p["link_history"]:
                for r in archive:
                    db.execute("INSERT OR IGNORE INTO records VALUES(?,?)", (r["date"], json.dumps(r, ensure_ascii=False)))
            self.event(db, "profile_updated" if previous else "enrolled", p)

    def save_record(self, record):
        with self.connect() as db:
            old = db.execute("SELECT data FROM records WHERE date=?", (record["date"],)).fetchone()
            self.event(db, "record_saved", {"previous": json.loads(old[0]) if old else None, "record": record})
            db.execute("INSERT OR REPLACE INTO records VALUES(?,?)", (record["date"], json.dumps(record, ensure_ascii=False)))

    def save_prediction(self, result):
        with self.connect() as db:
            cur = db.execute("INSERT INTO predictions(generated_at,data) VALUES(?,?)", (result["generated_at"], json.dumps(result, ensure_ascii=False)))
            return cur.lastrowid


class Application:
    def __init__(self, store, research):
        self.store, self.research = store, research
        self.token = secrets.token_urlsafe(32)

    def visible_records(self, profile):
        return [r for r in self.store.records() if profile["link_history"] or r["source"] != "archive"]

    def generate(self):
        profile = self.store.profile()
        if not profile: raise ValueError("먼저 시작 정보를 입력해주세요")
        records = self.visible_records(profile)
        now = datetime.now(ZONE)
        result = recommend(profile, records, now, self.research.summary)
        result["behavioral_forecast"] = self.research.behavioral_forecast(profile, records, now)
        result["input_hash"] = hashlib.sha256(json.dumps([profile, records], sort_keys=True).encode()).hexdigest()
        result["profile_snapshot"] = profile
        result["observed_record_count"] = len(records)
        result["id"] = self.store.save_prediction(result)
        return result

    def state(self):
        profile = self.store.profile()
        records = self.visible_records(profile) if profile else []
        predictions = self.store.predictions() if profile else []
        now = datetime.now(ZONE)
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
                "model_status": model_status,
                "data_readiness": getattr(self.research, "data_readiness", {"available": False, "status": "not_generated", "reason": "데이터 준비 상태 정보가 없습니다."}),
                "storage": str(self.store.path), "today": now.date().isoformat()}


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
            if path == "/api/export":
                state = app.state(); state.pop("csrf_token"); state["predictions"] = app.store.predictions()
                with app.store.connect() as db:
                    state["change_events"] = [{"at": r[0], "kind": r[1], "data": json.loads(r[2])} for r in db.execute("SELECT at,kind,data FROM events ORDER BY id")]
                return self.respond(state, download=True)
            routes = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in routes: return self.respond({"error": "Not found"}, 404)
            file, mime = routes[path]
            return self.respond((ROOT / "static" / file).read_bytes(), content_type=mime)
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
            if path == "/api/profile":
                p = validate_profile(data)
                app.store.save_profile(p, app.research.records)
                app.generate()
            elif path == "/api/record":
                if not app.store.profile(): raise ValueError("먼저 시작 정보를 입력해주세요")
                record = validate_record(data, datetime.now(ZONE))
                app.store.save_record(record)
                app.generate()
            elif path == "/api/recommend": app.generate()
            else: return self.respond({"error": "Not found"}, 404)
            return self.respond(app.state())
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            return self.respond({"error": str(error)}, 400)
        except Exception:
            return self.respond({"error": "저장 또는 계산을 완료하지 못했습니다. 기록을 새로고침해 확인해주세요."}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=ROOT / ".local/sleepwell.sqlite3")
    parser.add_argument("--workspace", type=Path, default=ROOT.parent.parent)
    parser.add_argument("--forecast-model", type=Path,
                        help="엄격한 web_completed_history_pre20_v1 행동 예측 JSON (선택)")
    parser.add_argument("--mesa-inventory", type=Path, default=ROOT / ".local/mesa_inventory.json",
                        help="원본/ID/경로가 없는 MESA 집계 점검 JSON")
    args = parser.parse_args()
    app = Application(Store(args.database), ResearchBridge(args.workspace, args.forecast_model, args.mesa_inventory))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.application = app
    print(f"SleepWell: http://127.0.0.1:{args.port}  |  기록: {args.database}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
