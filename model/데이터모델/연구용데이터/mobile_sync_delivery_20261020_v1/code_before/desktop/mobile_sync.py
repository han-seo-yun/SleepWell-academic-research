"""Opt-in LAN TLS receiver. The browser UI remains strictly on loopback."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

from mobile_store import ingest, summary

MAX_BODY = 1_000_000


def private_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
        return any(address in ipaddress.IPv4Network(net) for net in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    except (ValueError, TypeError):
        return False


def local_addresses():
    return sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                   if private_ipv4(item[4][0])})


def token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, controller, context):
        self.slots = threading.BoundedSemaphore(8)
        self.controller = controller
        self.context = context
        super().__init__(address, SyncHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(10)
            wrapped = self.context.wrap_socket(request, server_side=True)
            super().process_request_thread(wrapped, address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        self.controller.record_error("request_failure", sys.exception())


class SyncHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        controller = self.server.controller
        try:
            # Native app only. There is no CORS or cookie-based LAN authorization.
            if self.headers.get("Origin") or self.headers.get("Transfer-Encoding"):
                return self.reply({"error": "요청 형식을 확인해주세요."}, 403)
            if self.headers.get_content_type() != "application/json":
                return self.reply({"error": "JSON 요청만 지원합니다."}, 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self.reply({"error": "전송 크기를 확인해주세요."}, 413)
            if self.path not in ("/v1/pair", "/v1/sync"):
                return self.reply({"error": "지원하지 않는 주소입니다."}, 404)
            with controller.lock:
                if controller.server is not self.server:
                    return self.reply({"error": "PC 연결이 중지되었습니다."}, 503)
                device = None
                if self.path == "/v1/sync":
                    device = controller.authenticate(self.headers.get("Authorization", ""))
                    if device is None:
                        return self.reply({"error": "PC에서 다시 연결해주세요."}, 401)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("불완전한 전송입니다.")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("전송 형식을 확인해주세요.")
                if self.path == "/v1/pair":
                    return self.reply(controller.pair(payload))
                with controller.app.lock:
                    now = controller.app.clock()
                    # Freeze any due forecast BEFORE importing newly received observations.
                    controller.app.ensure_due(now)
                    result = ingest(controller.app, device, payload, now)
                    with controller.app.store.connect() as db:
                        db.execute("UPDATE mobile_devices SET last_sync=? WHERE id=?",
                                   (now.isoformat(), device))
                controller.last_error = None
                return self.reply(result)
        except PermissionError:
            return self.reply({"error": "연결 코드가 만료됐거나 올바르지 않습니다."}, 403)
        except (ValueError, KeyError, TypeError, OverflowError, UnicodeError):
            return self.reply({"error": "수면 기록 형식을 확인해주세요. 전송을 완료하지 못했습니다."}, 400)
        except Exception as error:
            controller.record_error("sync_failure", error)
            return self.reply({"error": "동기화를 완료하지 못했습니다. 다시 시도해주세요."}, 500)

    def do_GET(self):
        self.reply({"error": "휴대폰 연결 앱 전용 주소입니다."}, 405)


class MobileSync:
    def __init__(self, app, directory, certificate_helper=None):
        self.app = app
        self.directory = Path(directory)
        self.helper = certificate_helper or Path(__file__).with_name("SyncCertificate.exe")
        self.lock = threading.RLock()
        self.server = self.thread = None
        self.secret = None
        self.deadline = 0
        self.attempts = 0
        self.host = None
        self.certificate_file = None
        self.last_error = None
        self.notice = None
        with app.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS mobile_config(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS mobile_devices(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
                paired_at TEXT NOT NULL, last_sync TEXT, active INTEGER NOT NULL)""")

    def status(self):
        with self.lock, self.app.store.connect() as db:
            rows = db.execute("SELECT id,name,paired_at,last_sync FROM mobile_devices WHERE active=1").fetchall()
            return {
                "available": True, "enabled": self.server is not None,
                "addresses": local_addresses(), "port": self.server.server_port if self.server else None,
                "pairing_active": bool(self.secret and time.monotonic() < self.deadline),
                "devices": [dict(zip(("id", "name", "paired_at", "last_sync"), row)) for row in rows],
                "import_summary": summary(self.app.store),
                "last_error": self.last_error, "notice": self.notice,
            }

    def record_error(self, code, error):
        self.last_error = {"code": code, "type": type(error).__name__,
                           "at": self.app.clock().isoformat()}

    def _tls_context(self):
        with self.app.store.connect() as db:
            previous = db.execute("SELECT value FROM mobile_config WHERE key='certificate_dir'").fetchone()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if previous and re.fullmatch(r"tls-[0-9a-f]{32}", previous[0]):
            directory = self.directory / previous[0]
            try:
                valid = subprocess.run([str(self.helper), "--check", str(directory)],
                                       timeout=10, creationflags=flags).returncode == 0
                if valid:
                    context.load_cert_chain(directory / "sync-cert.pem", directory / "sync-key.pem")
                    self.certificate_file = directory / "sync-cert.pem"
                    return context
            except (OSError, ssl.SSLError, subprocess.SubprocessError):
                pass
        # Never publish a half-written pair. The DB pointer changes only after both
        # new files pass TLS validation; interrupted staging folders are not used.
        name = "tls-" + secrets.token_hex(16)
        directory = self.directory / name
        directory.mkdir(mode=0o700)
        subprocess.run([str(self.helper), str(directory)], check=True, timeout=30,
                       creationflags=flags)
        context.load_cert_chain(directory / "sync-cert.pem", directory / "sync-key.pem")
        with self.app.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO mobile_config VALUES('certificate_dir',?)", (name,))
            if previous:
                db.execute("UPDATE mobile_devices SET active=0")
                self.notice = "연결 인증서를 갱신했습니다. 새 코드로 휴대폰을 다시 등록해주세요."
        self.certificate_file = directory / "sync-cert.pem"
        return context

    def start(self, host):
        if not private_ipv4(host) or host not in local_addresses():
            raise ValueError("현재 PC의 내부 네트워크 주소를 선택해주세요.")
        # Bind to the selected private adapter, never all adapters or a public IP.
        if self.server and host != self.host:
            self.stop()
        with self.lock:
            if self.server is None:
                context = self._tls_context()
                with self.app.store.connect() as db:
                    previous = db.execute("SELECT value FROM mobile_config WHERE key='port'").fetchone()
                port = int(previous[0]) if previous else 0
                self.server = LimitedServer((host, port), self, context)
                with self.app.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO mobile_config VALUES('port',?)",
                               (str(self.server.server_port),))
                self.host = host
                self.thread = threading.Thread(target=self.server.serve_forever,
                                               kwargs={"poll_interval": .2}, daemon=True)
                self.thread.start()
            cert = self.certificate_file
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text("ascii"))).hexdigest()
            self.secret = secrets.token_urlsafe(32)
            self.deadline = time.monotonic() + 600
            self.attempts = 0
            uri = "sleepwell://pair?" + urlencode({"v": 1, "host": self.host,
                "port": self.server.server_port, "pin": fingerprint, "secret": self.secret})
            return {"pairing_uri": uri, "expires_in_seconds": 600, **self.status()}

    def pair(self, payload):
        secret = payload.get("secret")
        if (not self.secret or time.monotonic() >= self.deadline or self.attempts >= 5
                or not isinstance(secret, str) or not secrets.compare_digest(secret, self.secret)):
            self.attempts += 1
            if self.attempts >= 5:
                self.secret = None
            raise PermissionError()
        name = payload.get("device_name", "Android")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("기기 이름을 확인해주세요.")
        token = secrets.token_urlsafe(32)
        with self.app.store.connect() as db:
            previous = db.execute("SELECT id FROM mobile_devices ORDER BY paired_at DESC LIMIT 1").fetchone()
            # One participant/one phone slot. Re-pairing must not duplicate the
            # same Health Connect record under a new ingestion identity.
            identity = previous[0] if previous else secrets.token_hex(16)
            db.execute("UPDATE mobile_devices SET active=0")
            db.execute("INSERT OR REPLACE INTO mobile_devices VALUES(?,?,?,?,?,1)",
                       (identity, name.strip(), token_hash(token), self.app.clock().isoformat(), None))
        self.secret = None  # Single use; never export pairing or device secrets.
        return {"device_token": token, "device_id": identity, "schema_version": 1}

    def authenticate(self, authorization):
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if not 40 <= len(token) <= 100 or not token.isascii():
            return None
        with self.app.store.connect() as db:
            row = db.execute("SELECT id FROM mobile_devices WHERE active=1 AND token_hash=?",
                             (token_hash(token),)).fetchone()
        return row[0] if row else None

    def revoke(self):
        with self.lock, self.app.store.connect() as db:
            db.execute("UPDATE mobile_devices SET active=0")
            self.secret = None

    def stop(self):
        # Do not hold controller.lock while joining HTTP request threads.
        with self.lock:
            server, thread = self.server, self.thread
            self.server = self.thread = None
            self.secret = None
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
