"""Private loopback server owned by the SleepWell desktop launcher.

Packaged aggregate-trained CMI/MESA models support researcher comparisons.
Personal archives and external research workspaces are not discovered.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlparse

WEB_ROOT = Path(__file__).resolve().parent / "web-app"
sys.path.insert(0, str(WEB_ROOT))

from server import Application, Handler, Store, ThreadingHTTPServer, capture_loop
from mobile_sync import MobileSync
from mobile_backup import snapshots as backup_snapshots

VERSION = "0.4.3"


class DesktopResearch:
    records = []
    metrics = None
    summary = {
        "available": False,
        "reason": "배포판에는 개인 연구 원자료가 포함되어 있지 않습니다.",
    }
    data_readiness = {
        "available": False, "status": "not_packaged",
        "reason": "직접 입력·휴대폰 연동 기록으로 동작합니다. 연구용 원자료는 포함하지 않습니다.",
    }

    def behavioral_forecast(self, *args):
        return {"available": False, "reason": "개인 연구 모델은 배포판에 포함하지 않습니다."}

    def model_status(self, *args):
        return {"available": False, "status": "not_packaged",
                "reason": "이전 개인 모델은 비활성입니다. CMI·MESA 융합 모델은 별도 연구 비교 화면에서 실행합니다.",
                "model_version": None, "source": None}


class DesktopHandler(Handler):
    def do_GET(self):
        if urlparse(self.path).path == "/api/mobile/backups":
            if not self.allowed_host():
                return self.respond({"error": "Invalid host"}, 403)
            return self.respond({"snapshots": backup_snapshots(self.server.application.store),
                                 "limit": 90, "note": "휴대폰 계산 결과의 별도 백업이며 PC 주 참여자 기록을 덮어쓰지 않습니다."})
        if urlparse(self.path).path == "/api/mobile/status":
            if not self.allowed_host():
                return self.respond({"error": "Invalid host"}, 403)
            return self.respond(self.server.mobile.status())
        if urlparse(self.path).path == "/api/desktop-health":
            if not self.allowed_host():
                return self.respond({"error": "Invalid host"}, 403)
            return self.respond({
                "app": "SleepWell", "version": VERSION, "pid": os.getpid(),
                "instance_id": self.server.desktop_instance,
            })
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/mobile/"):
            return super().do_POST()
        origin = self.headers.get("Origin")
        allowed = [f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"]
        if (not self.allowed_host() or (origin and origin not in allowed)
                or self.headers.get("X-SleepWell-Token") != self.server.application.token):
            return self.respond({"error": "화면을 새로고침해주세요."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                raise ValueError("요청 크기를 확인해주세요.")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("요청 형식을 확인해주세요.")
            mobile = self.server.mobile
            if path == "/api/mobile/start":
                if not self.server.application.store.profile():
                    raise ValueError("먼저 시작 정보를 저장해주세요.")
                return self.respond(mobile.start(data.get("host")))
            if path == "/api/mobile/stop":
                mobile.stop()
            elif path == "/api/mobile/revoke":
                mobile.revoke()
            else:
                return self.respond({"error": "Not found"}, 404)
            return self.respond(mobile.status())
        except (ValueError, TypeError, KeyError):
            return self.respond({"error": "시작 정보와 선택한 네트워크 주소를 확인해주세요."}, 400)
        except Exception:
            return self.respond({"error": "휴대폰 연결을 시작하지 못했습니다. 앱을 다시 실행해주세요."}, 500)


@contextmanager
def exclusive_data_directory(directory):
    """A second host must never open the same participant database."""
    handle = (directory / "desktop.lock").open("a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def write_ready(path, value):
    temporary = path.with_name(path.name + "." + value["instance_id"] + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def control_loop(server):
    # Parent-pipe EOF also stops the server if the launcher crashes or exits.
    for line in sys.stdin:
        if line.strip() == "shutdown":
            break
    server.shutdown()


def serve(data_directory, ready_file):
    data_directory = data_directory.resolve()
    ready_file = ready_file.resolve()
    if ready_file.parent != data_directory:
        raise ValueError("Ready file must be inside the data directory")
    data_directory.mkdir(parents=True, exist_ok=True)
    with exclusive_data_directory(data_directory):
        app = Application(Store(data_directory / "sleepwell.sqlite3"), DesktopResearch())
        server = ThreadingHTTPServer(("127.0.0.1", 0), DesktopHandler)
        server.application = app
        server.mobile = MobileSync(app, data_directory)
        server.desktop_instance = secrets.token_hex(24)
        stop = threading.Event()
        timer = threading.Thread(target=capture_loop, args=(app, stop), daemon=True)
        control = threading.Thread(target=control_loop, args=(server,), daemon=True)
        ready = {
            "app": "SleepWell", "version": VERSION, "pid": os.getpid(),
            "instance_id": server.desktop_instance,
            "url": f"http://127.0.0.1:{server.server_port}/",
        }
        try:
            write_ready(ready_file, ready)
            timer.start()
            control.start()
            server.serve_forever(poll_interval=0.2)
        finally:
            stop.set()
            server.mobile.stop()
            if timer.is_alive():
                timer.join(timeout=2)
            server.server_close()
            try:
                current = json.loads(ready_file.read_text(encoding="utf-8"))
                if current.get("instance_id") == server.desktop_instance:
                    ready_file.unlink()
            except (FileNotFoundError, ValueError, OSError):
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        serve(args.data_dir, args.ready_file)
    except Exception:
        # Do not print paths, records or profile fields to operational logs.
        print("SleepWell desktop server could not start or save data.", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
