#!/usr/bin/env python3
"""Stateless localhost host for the SleepWell researcher experiment lab."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from typing import Any
from urllib.parse import urlsplit
import webbrowser

from research_lab import LabInputError, evaluate_run, model_status
from research_lab_charts import render_charts


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
MAX_BODY_BYTES = 2 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
STATIC_ROUTES = {
    "/": ("research-lab.html", "text/html; charset=utf-8"),
    "/research-lab.js": ("research-lab.js", "text/javascript; charset=utf-8"),
    "/research-lab.css": ("research-lab.css", "text/css; charset=utf-8"),
}


class RequestJSONError(ValueError):
    """A client request could not be decoded as the lab JSON contract."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestJSONError("JSON 객체에 중복 키가 있습니다.")
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> None:
    raise RequestJSONError(f"JSON에는 유한한 숫자만 사용할 수 있습니다: {value}")


def _encode_json(data: Any) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


class ResearchLabHTTPServer(ThreadingHTTPServer):
    """HTTP server state contains only configuration, a token, and a concurrency gate."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        public_host: str,
        models_dir: Path | None,
    ) -> None:
        super().__init__(server_address, handler)
        self.public_host = public_host
        self.models_dir = models_dir
        self.csrf_token = secrets.token_urlsafe(32)
        self.evaluation_gate = threading.BoundedSemaphore(1)
        self.app_bridge = None

    @property
    def allowed_authorities(self) -> frozenset[str]:
        port = self.server_port
        authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            authorities.update(ALLOWED_HOSTS)
        return frozenset(authorities)


class ResearchLabHandler(BaseHTTPRequestHandler):
    server: ResearchLabHTTPServer
    server_version = "SleepWellResearchLab/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # Requests can contain health research data, so the local host is silent.
        return

    def _header_once(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            return None
        return values[0].strip()

    def _request_path(self) -> str | None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        return parsed.path

    def _host_authority(self) -> str | None:
        host = self._header_once("Host")
        if host is None:
            return None
        normalized = host.lower()
        return normalized if normalized in self.server.allowed_authorities else None

    def _same_origin_and_token(self, authority: str) -> bool:
        origin = self._header_once("Origin")
        token = self._header_once("X-SleepWell-Token")
        if origin != f"http://{authority}" or token is None:
            return False
        return secrets.compare_digest(token, self.server.csrf_token)

    def _send_bytes(
        self,
        body: bytes,
        status: int,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: Any, status: int = 200) -> None:
        self._send_bytes(_encode_json(data), status, "application/json; charset=utf-8")

    def _send_json_error(self, message: str, status: int) -> None:
        self._send_json({"error": message}, status)

    def do_GET(self) -> None:
        if self._host_authority() is None:
            self._send_json_error("허용되지 않은 Host입니다.", 403)
            return

        path = self._request_path()
        if path == "/app":
            # Navigation must remain available even if model/session loading fails.
            target = None
            try:
                candidate = getattr(self.server.app_bridge, "app_url", None)
                parsed = urlsplit(candidate) if isinstance(candidate, str) else None
                if (parsed and parsed.scheme == "http" and parsed.hostname in ALLOWED_HOSTS
                        and parsed.port and not parsed.username and not parsed.password
                        and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
                    target = candidate
            except (ValueError, TypeError, AttributeError):
                pass
            if target:
                self._send_bytes(b"", 303, "text/plain; charset=utf-8", {"Location": target})
            else:
                self._send_bytes((
                    '<!doctype html><html lang="ko"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>SleepWell 연구자 홈 연결</title><main><h1>독립 실행한 연구 분석실입니다</h1>'
                    '<p>연결된 연구자 홈이 없습니다. SleepWell 연구자용 실행을 열고 '
                    '그 안의 연구 분석실 메뉴를 이용하면 홈과 분석실을 오갈 수 있습니다.</p>'
                    '<p><a href="/">연구 분석실로 돌아가기</a></p></main></html>'
                ).encode("utf-8"), 200, "text/html; charset=utf-8")
            return
        if path == "/api/lab/session":
            try:
                status = model_status(models_dir=self.server.models_dir)
                self._send_json(
                    {
                        "schema_version": 1,
                        "csrf_token": self.server.csrf_token,
                        "limits": {
                            "max_body_bytes": MAX_BODY_BYTES,
                            "max_rows": 365,
                            "max_participants": 10,
                            "one_evaluation_at_a_time": True,
                        },
                        "model_status": status,
                        "integration": self.server.app_bridge.session() if self.server.app_bridge else {"connected": False, "sources": []},
                    }
                )
            except Exception:
                self._send_json_error(
                    "모델 상태를 읽지 못했습니다. 로컬 모델 구성을 확인해주세요.", 500
                )
            return

        static_route = STATIC_ROUTES.get(path or "")
        if static_route is None:
            self._send_json_error("Not found", 404)
            return
        filename, content_type = static_route
        try:
            body = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            self._send_json_error("연구 분석실 화면 파일을 읽지 못했습니다.", 500)
            return
        self._send_bytes(body, 200, content_type)

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get_all("Transfer-Encoding", []):
            raise RequestJSONError("Transfer-Encoding 요청은 지원하지 않습니다.")

        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) != 1:
            raise RequestJSONError("Content-Length가 정확히 하나 필요합니다.")
        try:
            length = int(content_lengths[0], 10)
        except (TypeError, ValueError) as error:
            raise RequestJSONError("Content-Length 형식이 올바르지 않습니다.") from error
        if length < 0:
            raise RequestJSONError("Content-Length는 음수일 수 없습니다.")
        if length > MAX_BODY_BYTES:
            raise OverflowError("요청 본문은 2 MiB 이하여야 합니다.")

        content_type = self._header_once("Content-Type")
        if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise TypeError("Content-Type은 application/json이어야 합니다.")

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestJSONError("요청 본문을 모두 읽지 못했습니다.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RequestJSONError("요청 본문은 UTF-8이어야 합니다.") from error
        try:
            value = json.loads(
                text,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_finite_json,
            )
        except (ValueError, RecursionError) as error:
            if isinstance(error, RequestJSONError):
                raise
            raise RequestJSONError("올바른 JSON 객체를 입력해주세요.") from error
        if not isinstance(value, dict):
            raise RequestJSONError("최상위 JSON 값은 객체여야 합니다.")
        if "models_dir" in value:
            raise RequestJSONError("models_dir은 로컬 관리자 CLI에서만 설정할 수 있습니다.")
        return value

    def do_POST(self) -> None:
        authority = self._host_authority()
        if authority is None:
            self._send_json_error("허용되지 않은 Host입니다.", 403)
            return
        path = self._request_path()
        if path not in ("/api/lab/evaluate", "/api/lab/import", "/api/lab/export"):
            self._send_json_error("Not found", 404)
            return
        if not self._same_origin_and_token(authority):
            self._send_json_error(
                "요청 출처 또는 세션 토큰을 확인할 수 없습니다. 화면을 새로고침해주세요.",
                403,
            )
            return

        try:
            payload = self._read_json_body()
        except OverflowError as error:
            self.close_connection = True
            self._send_json_error(str(error), 413)
            return
        except TypeError as error:
            self._send_json_error(str(error), 415)
            return
        except RequestJSONError as error:
            self._send_json_error(str(error), 400)
            return

        if path == "/api/lab/import":
            if self.server.app_bridge is None:
                self._send_json_error("본 앱에서 연구 분석실을 열어야 기록을 연결할 수 있습니다.", 409)
                return
            if set(payload) - {"source"} or not isinstance(payload.get("source", "pc"), str):
                self._send_json_error("불러올 기록 출처를 확인해주세요.", 400)
                return
            try:
                self._send_json(self.server.app_bridge.snapshot(payload.get("source", "pc")))
            except ValueError as error:
                self._send_json_error(str(error), 400)
            except Exception:
                self._send_json_error("앱 기록을 읽지 못했습니다. 연동 상태를 확인해주세요.", 500)
            return

        if not self.server.evaluation_gate.acquire(blocking=False):
            self._send_bytes(
                _encode_json({"error": "다른 평가가 진행 중입니다. 완료 후 다시 시도해주세요."}),
                429,
                "application/json; charset=utf-8",
                {"Retry-After": "1"},
            )
            return

        try:
            result = evaluate_run(payload, models_dir=self.server.models_dir)
            if not isinstance(result, dict):
                raise TypeError("평가 엔진이 객체를 반환하지 않았습니다.")
            if path == "/api/lab/export":
                from research_lab_charts import export_figures
                response_body = export_figures(result)
            else:
                response = dict(result)
                response["charts"] = render_charts(result)
                response_body = _encode_json(response)
        except LabInputError as error:
            self._send_json_error(str(error), 400)
            return
        except Exception:
            self._send_json_error(
                "평가 또는 차트 생성을 완료하지 못했습니다. 입력과 로컬 모델 구성을 확인해주세요.",
                500,
            )
            return
        finally:
            self.server.evaluation_gate.release()

        if path == "/api/lab/export":
            self._send_bytes(response_body, 200, "application/zip", {
                "Content-Disposition": 'attachment; filename="SleepWell-publication-figures.zip"'
            })
        else:
            self._send_bytes(response_body, 200, "application/json; charset=utf-8")

    def _method_not_allowed(self) -> None:
        if self._host_authority() is None:
            self._send_json_error("허용되지 않은 Host입니다.", 403)
            return
        self._send_bytes(
            _encode_json({"error": "Method not allowed"}),
            405,
            "application/json; charset=utf-8",
            {"Allow": "GET, POST"},
        )

    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    models_dir: str | Path | None = None,
) -> HTTPServer:
    """Create an unstarted loopback-only lab server for tests or local launch."""
    if host not in ALLOWED_HOSTS:
        raise ValueError("host는 127.0.0.1 또는 localhost여야 합니다.")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port는 0부터 65535 사이의 정수여야 합니다.")
    configured_models_dir = Path(models_dir).expanduser().resolve() if models_dir is not None else None
    return ResearchLabHTTPServer(
        (host, port),
        ResearchLabHandler,
        public_host=host,
        models_dir=configured_models_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SleepWell 로컬 연구 분석실(기존 앱 저장소와 분리된 무상태 평가 도구)"
    )
    parser.add_argument("--host", choices=sorted(ALLOWED_HOSTS), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--models-dir",
        type=Path,
        help="로컬 관리자가 선택한 모델 디렉터리(HTTP 요청에서는 변경 불가)",
    )
    args = parser.parse_args()

    try:
        server = make_server(args.host, args.port, args.models_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    url = f"http://{args.host}:{server.server_port}/"
    print(f"SleepWell 연구 분석실: {url}", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open_new_tab(url)
        except webbrowser.Error as error:
            print(f"브라우저를 열지 못했습니다: {error}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
