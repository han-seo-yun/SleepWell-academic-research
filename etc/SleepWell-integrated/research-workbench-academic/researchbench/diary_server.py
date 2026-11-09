"""Small, privacy-bounded HTTP server for prospective morning diaries."""
from __future__ import annotations

import argparse
import ipaddress
import json
import ssl
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


MAX_BODY_BYTES = 32 * 1024
SEOUL = ZoneInfo("Asia/Seoul")
RESOURCE = Path(__file__).with_name("resources") / "prospective_diary.html"
DIARY_FIELDS = frozenset({
    "night_date", "bed_entry", "sleep_attempt", "latency_min", "final_wake",
    "out_of_bed", "awakenings_count", "waso_min", "quality", "flags",
    "missing_reason",
})


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _aware(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    if parsed > datetime.now(parsed.tzinfo):
        raise ValueError(f"{field} cannot be in the future")
    return parsed


def _validated_diary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("diary must be an object")
    unknown = set(value) - DIARY_FIELDS
    if unknown:
        raise ValueError("unsupported diary fields")
    diary = {key: item for key, item in value.items() if item not in (None, "")}
    try:
        night = date.fromisoformat(str(diary["night_date"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid night_date") from error
    if night > datetime.now(SEOUL).date():
        raise ValueError("night_date cannot be in the future")
    if diary.get("missing_reason"):
        if not isinstance(diary["missing_reason"], str) or len(diary["missing_reason"]) > 200:
            raise ValueError("invalid missing_reason")
        return {"night_date": night.isoformat(), "missing_reason": diary["missing_reason"].strip()}

    attempt = _aware(diary.get("sleep_attempt"), "sleep_attempt")
    wake = _aware(diary.get("final_wake"), "final_wake")
    for field in ("bed_entry", "out_of_bed"):
        if field in diary:
            _aware(diary[field], field)
    try:
        latency = float(diary["latency_min"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid latency_min") from error
    if not 0 <= latency <= 480 or wake <= attempt:
        raise ValueError("invalid sleep interval")
    for field, lower, upper in (
        ("awakenings_count", 0, 100), ("waso_min", 0, 960), ("quality", 1, 5)
    ):
        if field in diary:
            try:
                number = float(diary[field])
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid {field}") from error
            if not lower <= number <= upper:
                raise ValueError(f"invalid {field}")
    if "flags" in diary:
        if (not isinstance(diary["flags"], list) or len(diary["flags"]) > 20
                or any(not isinstance(item, str) or len(item) > 80 for item in diary["flags"])):
            raise ValueError("invalid flags")
    diary["night_date"] = night.isoformat()
    diary["latency_min"] = latency
    return diary


class DiaryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, study: Any, *, scheduler_interval: float = 30.0):
        super().__init__(address, DiaryRequestHandler)
        self.study = study
        self.study_lock = threading.Lock()
        self.scheduler_interval = scheduler_interval
        self.scheduler_stop = threading.Event()
        self.scheduler_thread: threading.Thread | None = None
        self.scheduler_status = "waiting"
        self.scheduler_error_type: str | None = None

    def start_scheduler(self) -> None:
        if self.scheduler_thread is not None:
            return
        self.scheduler_thread = threading.Thread(
            target=self._scheduler, name="sleepwell-forecast-scheduler", daemon=True
        )
        self.scheduler_thread.start()

    def _scheduler(self) -> None:
        while not self.scheduler_stop.wait(self.scheduler_interval):
            try:
                with self.study_lock:
                    self.study.tick()
                self.scheduler_status = "ok"
                self.scheduler_error_type = None
            except Exception as error:
                if isinstance(error, ValueError) and str(error).startswith("forecast tick is only allowed"):
                    self.scheduler_status = "waiting"
                    self.scheduler_error_type = None
                    continue
                # Expose only the exception class, never participant data or paths.
                self.scheduler_status = "error"
                self.scheduler_error_type = type(error).__name__
                continue

    def server_close(self) -> None:
        self.scheduler_stop.set()
        thread = self.scheduler_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(1.0, self.scheduler_interval + 0.1))
        super().server_close()


class DiaryRequestHandler(BaseHTTPRequestHandler):
    server: DiaryHTTPServer
    server_version = "SleepWellDiary"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        """Disable request logging so tokens and participant activity are not emitted."""

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.lower()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            try:
                body = RESOURCE.read_bytes()
            except OSError:
                self._json(503, {"status": "unavailable"})
                return
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/health":
            state = "preparation"
            try:
                with self.server.study_lock:
                    status = self.server.study.status()
                candidate = str(status.get("state") or status.get("status") or "").lower()
                if candidate in {"active", "closed", "synthetic"}:
                    state = candidate
                elif status.get("synthetic_rehearsal") is True:
                    state = "synthetic"
                elif status.get("ready_for_enrollment") is True:
                    state = "active"
            except Exception:
                state = "unavailable"
            health = {"status": "ok", "study_state": state,
                      "scheduler_status": self.server.scheduler_status}
            if self.server.scheduler_error_type:
                health["scheduler_error_type"] = self.server.scheduler_error_type
            self._json(200, health)
            return
        self._json(404, {"status": "not_found"})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/diary":
            self._json(404, {"status": "not_found"})
            return
        if not self._same_origin():
            self._json(403, {"status": "error", "message": "요청 출처를 확인해 주세요."})
            return
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            self._json(401, {"status": "error", "message": "참여자 토큰을 확인해 주세요."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(400, {"status": "error", "message": "제출 형식을 확인해 주세요."})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("invalid body length")
            raw = self.rfile.read(length)
            diary = _validated_diary(json.loads(raw.decode("utf-8")))
            with self.server.study_lock:
                receipt = self.server.study.submit_diary(authorization[7:].strip(), diary)
        except PermissionError:
            self._json(401, {"status": "error", "message": "참여자 토큰을 확인해 주세요."})
            return
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"status": "error", "message": "제출 형식을 확인해 주세요."})
            return
        except Exception:
            self._json(503, {"status": "error", "message": "현재 제출을 저장할 수 없습니다."})
            return
        safe = {"status": "accepted"}
        if isinstance(receipt, dict):
            for key in ("receipt_id", "received_at", "duplicate", "timing_class",
                        "primary_revision", "forecast_status", "synthetic_rehearsal"):
                if key in receipt and isinstance(receipt[key], (str, bool, int)):
                    safe[key] = receipt[key]
        self._json(201, safe)


def make_server(study: Any, host: str = "127.0.0.1", port: int = 8767,
                certfile: str | None = None, keyfile: str | None = None, *,
                scheduler_interval: float = 30.0) -> DiaryHTTPServer:
    if bool(certfile) != bool(keyfile):
        raise ValueError("certfile and keyfile must be supplied together")
    if not _loopback(host) and not certfile:
        raise ValueError("non-loopback listening requires TLS certificate and key")
    server = DiaryHTTPServer((host, int(port)), study, scheduler_interval=scheduler_interval)
    try:
        if certfile and keyfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            server.socket = context.wrap_socket(server.socket, server_side=True)
    except Exception:
        server.server_close()
        raise
    server.start_scheduler()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SleepWell prospective diary server")
    parser.add_argument("--study", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--certfile")
    parser.add_argument("--keyfile")
    args = parser.parse_args(argv)
    from .prospective_study import ProspectiveStudy

    server = make_server(ProspectiveStudy(args.study), args.host, args.port,
                         args.certfile, args.keyfile)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
