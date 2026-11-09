"""Official Sign in with ChatGPT authentication for the local SleepWell app.

This module deliberately implements only the local/OSS SIWC flow.  It does
not inspect, reuse, or proxy Codex credentials.  Registration information is
kept in an application-local JSON file; OAuth tokens live only in the OS
keyring.  It exposes a deliberately small provider-facing API:

``credential_status()``, ``login()``, ``logout()``, and
``get_access_token()``.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import math
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import keyring


APP_NAME = "SleepWell Academic Workbench"
KEYRING_SERVICE = "SleepWell Academic Workbench ChatGPT SIWC"
SCHEMA_VERSION = 1
ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/api/accounts/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
REQUIRED_SCOPES = frozenset(("openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"))
IDENTITY_REQUIRED_SCOPES = REQUIRED_SCOPES - {"chatgpt.tokens.use.direct"}
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
LOGIN_TIMEOUT_SECONDS = 300.0
CLOCK_SKEW_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 20.0
MAX_HTTP_BYTES = 1_000_000
TOKEN_CHUNK_CHARS = 1_000
MAX_TOKEN_CHUNKS = 128
TOKEN_BUNDLE_KIND = "sleepwell_siwc_token_bundle"
TOKEN_BUNDLE_SCHEMA = 1
_REFRESH_LOCK = threading.RLock()


def _data_dir() -> Path:
    """Return the per-user local application data directory, not a project directory."""
    configured = os.environ.get("SLEEPWELL_USER_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if root:
        return Path(root) / "SleepWell Academic Workbench"
    return Path.home() / ".local" / "share" / "SleepWell Academic Workbench"


def _metadata_path() -> Path:
    return _data_dir() / "chatgpt_siwc.json"


def _empty_metadata() -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "host_id": f"urn:uuid:{uuid.uuid4()}", "active_profile": None, "profiles": {}}


def _load_metadata() -> dict[str, Any]:
    path = _metadata_path()
    if not path.exists():
        return _empty_metadata()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("chatgpt_registration_metadata_invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION:
        raise RuntimeError("chatgpt_registration_metadata_invalid")
    host_id, profiles = value.get("host_id"), value.get("profiles")
    if not isinstance(host_id, str) or not host_id.startswith("urn:") or not isinstance(profiles, dict):
        raise RuntimeError("chatgpt_registration_metadata_invalid")
    active = value.get("active_profile")
    if active is not None and (not isinstance(active, str) or active not in profiles):
        raise RuntimeError("chatgpt_registration_metadata_invalid")
    for profile in profiles.values():
        if (not isinstance(profile, dict) or not isinstance(profile.get("client_id"), str)
                or not isinstance(profile.get("scopes"), list)
                or not all(isinstance(scope, str) for scope in profile["scopes"])
                or isinstance(profile.get("expires_at"), bool)
                or not isinstance(profile.get("expires_at"), int)):
            raise RuntimeError("chatgpt_registration_metadata_invalid")
    return value


def _save_metadata(value: dict[str, Any]) -> None:
    path = _metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _profile_id(subject: str, client_id: str) -> str:
    return hashlib.sha256(f"{ISSUER}\0{subject}\0{client_id}".encode("utf-8")).hexdigest()


def _bundle_part_name(profile_id: str, bundle_id: str, index: int) -> str:
    return f"{profile_id}:bundle:{bundle_id}:{index}"


def _bundle_manifest(value: Any) -> tuple[str, int, str] | None:
    """Validate a chunk manifest; return ``None`` only for legacy token JSON."""
    if not isinstance(value, dict) or value.get("kind") != TOKEN_BUNDLE_KIND:
        return None
    if set(value) != {"kind", "schema", "bundle_id", "chunk_count", "sha256"}:
        raise RuntimeError("chatgpt_os_keyring_invalid")
    bundle_id, count, digest = value.get("bundle_id"), value.get("chunk_count"), value.get("sha256")
    if (value.get("schema") != TOKEN_BUNDLE_SCHEMA or not isinstance(bundle_id, str)
            or len(bundle_id) != 32 or any(char not in "0123456789abcdef" for char in bundle_id)
            or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_TOKEN_CHUNKS
            or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        raise RuntimeError("chatgpt_os_keyring_invalid")
    return bundle_id, count, digest


def _read_keyring(account: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, account)
    except Exception as exc:
        raise RuntimeError("chatgpt_os_keyring_unavailable") from exc


def _delete_keyring(account: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, account)
    except Exception as exc:
        if exc.__class__.__name__ not in {"PasswordDeleteError", "KeyError"}:
            raise RuntimeError("chatgpt_os_keyring_unavailable") from exc


def _tokens_get(profile_id: str) -> dict[str, Any] | None:
    raw = _read_keyring(profile_id)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("chatgpt_os_keyring_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("chatgpt_os_keyring_invalid")
    manifest = _bundle_manifest(value)
    if manifest is None:  # Small pre-chunking bundles remain readable.
        return value
    bundle_id, count, digest = manifest
    chunks: list[str] = []
    for index in range(count):
        chunk = _read_keyring(_bundle_part_name(profile_id, bundle_id, index))
        if not isinstance(chunk, str) or not chunk or len(chunk) > TOKEN_CHUNK_CHARS or not chunk.isascii():
            raise RuntimeError("chatgpt_os_keyring_invalid")
        chunks.append(chunk)
    serialized = "".join(chunks)
    if hashlib.sha256(serialized.encode("ascii")).hexdigest() != digest:
        raise RuntimeError("chatgpt_os_keyring_invalid")
    try:
        tokens = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise RuntimeError("chatgpt_os_keyring_invalid") from exc
    if not isinstance(tokens, dict) or _bundle_manifest(tokens) is not None:
        raise RuntimeError("chatgpt_os_keyring_invalid")
    return tokens


def _tokens_set(profile_id: str, value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("chatgpt_token_bundle_invalid")
    try:
        serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except Exception as exc:
        raise ValueError("chatgpt_token_bundle_invalid") from exc
    if not serialized or not serialized.isascii() or len(serialized) > TOKEN_CHUNK_CHARS * MAX_TOKEN_CHUNKS:
        raise ValueError("chatgpt_token_bundle_invalid")
    chunks = [serialized[offset:offset + TOKEN_CHUNK_CHARS] for offset in range(0, len(serialized), TOKEN_CHUNK_CHARS)]
    old_manifest = None
    old_raw = _read_keyring(profile_id)
    if old_raw is not None:
        try:
            old_manifest = _bundle_manifest(json.loads(old_raw))
        except (json.JSONDecodeError, RuntimeError):
            old_manifest = None
    bundle_id, written = uuid.uuid4().hex, []
    try:
        for index, chunk in enumerate(chunks):
            account = _bundle_part_name(profile_id, bundle_id, index)
            keyring.set_password(KEYRING_SERVICE, account, chunk)
            written.append(account)
        manifest = {"kind": TOKEN_BUNDLE_KIND, "schema": TOKEN_BUNDLE_SCHEMA, "bundle_id": bundle_id,
                    "chunk_count": len(chunks), "sha256": hashlib.sha256(serialized.encode("ascii")).hexdigest()}
        keyring.set_password(KEYRING_SERVICE, profile_id, json.dumps(manifest, separators=(",", ":")))
    except Exception as exc:
        for account in written:
            _delete_keyring(account)
        raise RuntimeError("chatgpt_os_keyring_unavailable") from exc
    if old_manifest is not None:
        old_bundle, old_count, _ = old_manifest
        for index in range(old_count):
            _delete_keyring(_bundle_part_name(profile_id, old_bundle, index))


def _tokens_delete(profile_id: str) -> None:
    raw = _read_keyring(profile_id)
    if raw is not None:
        try:
            manifest = _bundle_manifest(json.loads(raw))
        except (json.JSONDecodeError, RuntimeError):
            manifest = None
        if manifest is not None:
            bundle_id, count, _ = manifest
            for index in range(count):
                _delete_keyring(_bundle_part_name(profile_id, bundle_id, index))
    _delete_keyring(profile_id)


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback:
        callback(message)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ValueError("invalid_base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_b64url(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_b64url_decode(value).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("chatgpt_id_token_invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("chatgpt_id_token_invalid")
    return decoded


def _fetch_json(url: str, *, timeout: float = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Fetch a fixed official endpoint, rejecting redirects and oversized bodies."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_: Any, **__: Any) -> None:
            return None

    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"{APP_NAME}/1"})
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise RuntimeError("chatgpt_unexpected_redirect")
            payload = response.read(MAX_HTTP_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RuntimeError("chatgpt_network_request_failed") from exc
    if len(payload) > MAX_HTTP_BYTES:
        raise RuntimeError("chatgpt_response_too_large")
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("chatgpt_response_invalid") from exc
    if not isinstance(result, dict):
        raise RuntimeError("chatgpt_response_invalid")
    return result


def _post_form(url: str, values: dict[str, str], *, timeout: float = HTTP_TIMEOUT_SECONDS,
               allow_empty: bool = False) -> dict[str, Any]:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_: Any, **__: Any) -> None:
            return None

    encoded = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": f"{APP_NAME}/1",
    })
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise RuntimeError("chatgpt_unexpected_redirect")
            payload = response.read(MAX_HTTP_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RuntimeError("chatgpt_token_exchange_failed") from exc
    if len(payload) > MAX_HTTP_BYTES:
        raise RuntimeError("chatgpt_response_too_large")
    if not payload and allow_empty:
        return {}
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("chatgpt_token_response_invalid") from exc
    if not isinstance(result, dict):
        raise RuntimeError("chatgpt_token_response_invalid")
    return result


@contextmanager
def _refresh_lock(cancel_event: threading.Event | None):
    """Serialize refresh-token rotation across threads and local Windows processes."""
    with _REFRESH_LOCK:
        if os.name != "nt":
            yield
            return
        import msvcrt
        lock_path = _data_dir() / "chatgpt_siwc.refresh.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            while True:
                if _cancelled(cancel_event):
                    raise RuntimeError("chatgpt_request_cancelled")
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _official_configuration() -> dict[str, str]:
    value = _fetch_json(DISCOVERY_URL)
    expected = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "jwks_uri": JWKS_URI,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise RuntimeError("chatgpt_oidc_metadata_invalid")
    revocation = value.get("revocation_endpoint")
    if not isinstance(revocation, str) or not revocation.startswith(f"{ISSUER}/"):
        raise RuntimeError("chatgpt_oidc_metadata_invalid")
    return {**expected, "revocation_endpoint": revocation}


def _verify_windows_rs256(signing_input: bytes, signature: bytes, jwk: dict[str, Any]) -> bool:
    """Verify RS256 with Windows' OS crypto; token material only reaches stdin."""
    if os.name != "nt":
        raise RuntimeError("chatgpt_jwt_signature_verifier_unavailable")
    if jwk.get("kty") != "RSA" or jwk.get("alg") not in (None, "RS256"):
        raise RuntimeError("chatgpt_jwt_algorithm_unsupported")
    try:
        modulus, exponent = _b64url_decode(str(jwk["n"])), _b64url_decode(str(jwk["e"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("chatgpt_jwks_key_invalid") from exc
    script = r'''$ErrorActionPreference = 'Stop'
$d = [Console]::In.ReadToEnd() | ConvertFrom-Json
$p = New-Object System.Security.Cryptography.RSAParameters
$p.Modulus = [Convert]::FromBase64String($d.n)
$p.Exponent = [Convert]::FromBase64String($d.e)
$rsa = [System.Security.Cryptography.RSA]::Create()
$rsa.ImportParameters($p)
$ok = $rsa.VerifyData([Convert]::FromBase64String($d.input), [Convert]::FromBase64String($d.signature), [System.Security.Cryptography.HashAlgorithmName]::SHA256, [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
if ($ok) { [Console]::Out.Write('true') } else { [Console]::Out.Write('false') }'''
    wire = json.dumps({"n": base64.b64encode(modulus).decode("ascii"), "e": base64.b64encode(exponent).decode("ascii"),
                       "input": base64.b64encode(signing_input).decode("ascii"), "signature": base64.b64encode(signature).decode("ascii")})
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], input=wire,
                                   text=True, capture_output=True, timeout=10, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("chatgpt_jwt_signature_verifier_unavailable") from exc
    if completed.returncode != 0:
        raise RuntimeError("chatgpt_jwt_signature_verification_failed")
    return completed.stdout.strip() == "true"


def _validate_id_token(id_token: str, *, client_id: str, nonce: str, now: float | None = None) -> dict[str, Any]:
    if not isinstance(id_token, str):
        raise RuntimeError("chatgpt_id_token_invalid")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise RuntimeError("chatgpt_id_token_invalid")
    header, claims = _json_b64url(parts[0]), _json_b64url(parts[1])
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
        raise RuntimeError("chatgpt_jwt_algorithm_unsupported")
    jwks = _fetch_json(JWKS_URI).get("keys")
    if not isinstance(jwks, list):
        raise RuntimeError("chatgpt_jwks_invalid")
    key = next((item for item in jwks if isinstance(item, dict) and item.get("kid") == header["kid"]), None)
    if key is None:
        raise RuntimeError("chatgpt_jwks_key_not_found")
    try:
        signature = _b64url_decode(parts[2])
    except ValueError as exc:
        raise RuntimeError("chatgpt_id_token_invalid") from exc
    if not _verify_windows_rs256(f"{parts[0]}.{parts[1]}".encode("ascii"), signature, key):
        raise RuntimeError("chatgpt_jwt_signature_verification_failed")
    current = time.time() if now is None else now
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    exp, issued = claims.get("exp"), claims.get("iat")
    if (claims.get("iss") != ISSUER or client_id not in audiences or claims.get("nonce") != nonce
            or not isinstance(claims.get("sub"), str) or not claims["sub"]
            or isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp) or exp <= current - CLOCK_SKEW_SECONDS
            or isinstance(issued, bool) or not isinstance(issued, (int, float)) or not math.isfinite(issued) or issued > current + CLOCK_SKEW_SECONDS):
        raise RuntimeError("chatgpt_id_token_claims_invalid")
    return claims


class _CallbackServer(http.server.HTTPServer):
    allow_reuse_address = False


def _receive_callback(server: _CallbackServer, expected_state: str, cancel_event: threading.Event | None, *, timeout: float) -> dict[str, str]:
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != CALLBACK_PATH or len(parsed.query) > 10_000:
                self.send_response(404)
                self.end_headers()
                return
            pairs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            for key in ("state", "code", "error", "client_id", "scope"):
                values = pairs.get(key)
                if values and len(values) == 1:
                    result[key] = values[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body>You may close this window and return to SleepWell.</body></html>")

    server.RequestHandlerClass = Handler
    server.timeout = 0.2
    deadline = time.monotonic() + timeout
    while not result:
        if _cancelled(cancel_event):
            raise RuntimeError("chatgpt_login_cancelled")
        if time.monotonic() >= deadline:
            raise RuntimeError("chatgpt_login_timed_out")
        server.handle_request()
    if result.get("state") != expected_state:
        raise RuntimeError("chatgpt_callback_state_invalid")
    if result.get("error") or not result.get("code"):
        raise RuntimeError("chatgpt_login_not_completed")
    return result


def credential_status() -> dict[str, Any]:
    """Return non-secret connection status for the active ChatGPT SIWC profile."""
    try:
        metadata = _load_metadata()
    except RuntimeError as exc:
        return {"provider": "chatgpt_siwc", "supported": True, "connected": False, "reason": str(exc)}
    profile_id = metadata.get("active_profile")
    if not profile_id:
        return {"provider": "chatgpt_siwc", "supported": True, "connected": False, "method": "siwc_oauth"}
    profile = metadata["profiles"][profile_id]
    try:
        tokens = _tokens_get(profile_id)
    except RuntimeError:
        tokens = None
    scopes = set(profile.get("scopes", []))
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    token_shape_valid = isinstance(access, str) and bool(access) and isinstance(refresh, str) and bool(refresh)
    usable = token_shape_valid and (profile["expires_at"] > time.time() + 60 or bool(refresh))
    return {
        "provider": "chatgpt_siwc", "supported": True, "connected": bool(usable and DIRECT_SCOPE in scopes),
        "method": "siwc_oauth", "profile": profile_id[:12], "expires_at": profile.get("expires_at"),
        "plan_scope_granted": DIRECT_SCOPE in scopes,
    }


def select_profile(profile_id: str) -> dict[str, Any]:
    """Select a locally retained registration before an explicit reauthentication."""
    metadata = _load_metadata()
    if not isinstance(profile_id, str) or profile_id not in metadata["profiles"]:
        raise ValueError("chatgpt_profile_not_found")
    metadata["active_profile"] = profile_id
    _save_metadata(metadata)
    return credential_status()


def login(cancel_event: threading.Event | None = None, on_progress: Callable[[str], None] | None = None,
          *, new_account: bool = False) -> dict[str, Any]:
    """Run explicit browser sign-in; account changes require ``new_account=True``."""
    if _cancelled(cancel_event):
        raise RuntimeError("chatgpt_login_cancelled")
    _progress(on_progress, "Preparing secure ChatGPT sign-in.")
    configuration = _official_configuration()
    metadata = _load_metadata()
    # The host ID must exist before the first authorization attempt, including a cancelled one.
    _save_metadata(metadata)
    active = metadata.get("active_profile")
    existing = metadata["profiles"].get(active) if active else None
    initial_registration = bool(new_account) or not existing
    client_id = "dynamic_agent_client" if initial_registration else existing["client_id"]
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state, nonce = _b64url(secrets.token_bytes(32)), _b64url(secrets.token_bytes(32))
    server = _CallbackServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
        params = {
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "scope": " ".join(sorted(REQUIRED_SCOPES)), "resource": RESOURCE, "state": state, "nonce": nonce,
            "code_challenge_method": "S256", "code_challenge": challenge, "ext_agent_host_id": metadata["host_id"],
        }
        if initial_registration:
            params["agent_name_hint"] = APP_NAME
        else:
            tokens = _tokens_get(active)
            if tokens and isinstance(tokens.get("id_token"), str):
                params["id_token_hint"] = tokens["id_token"]
        authorize_url = f"{configuration['authorization_endpoint']}?{urllib.parse.urlencode(params)}"
        _progress(on_progress, "Opening browser for ChatGPT consent.")
        if not webbrowser.open(authorize_url, new=1):
            raise RuntimeError("chatgpt_browser_open_failed")
        callback = _receive_callback(server, state, cancel_event, timeout=LOGIN_TIMEOUT_SECONDS)
    finally:
        server.server_close()
    issued_client_id = callback.get("client_id", client_id)
    if initial_registration and (issued_client_id == "dynamic_agent_client" or not issued_client_id.startswith("oaiapp_")):
        raise RuntimeError("chatgpt_registration_incomplete")
    if not initial_registration and issued_client_id != client_id:
        raise RuntimeError("chatgpt_client_id_mismatch")
    if _cancelled(cancel_event):
        raise RuntimeError("chatgpt_login_cancelled")
    _progress(on_progress, "Verifying ChatGPT authorization.")
    token_response = _post_form(configuration["token_endpoint"], {
        "grant_type": "authorization_code", "client_id": issued_client_id, "code": callback["code"],
        "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE,
    })
    if _cancelled(cancel_event):
        raise RuntimeError("chatgpt_login_cancelled")
    scope = set(str(token_response.get("scope", "")).split())
    if not IDENTITY_REQUIRED_SCOPES.issubset(scope):
        raise RuntimeError("chatgpt_identity_scopes_not_granted")
    id_token = token_response.get("id_token")
    claims = _validate_id_token(id_token, client_id=issued_client_id, nonce=nonce)
    access, refresh = token_response.get("access_token"), token_response.get("refresh_token")
    expires_in = token_response.get("expires_in")
    if (not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
            or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in) or expires_in <= 0):
        raise RuntimeError("chatgpt_token_response_invalid")
    profile_id = _profile_id(claims["sub"], issued_client_id)
    if existing and profile_id != active:
        raise RuntimeError("chatgpt_account_identity_mismatch")
    if _cancelled(cancel_event):
        raise RuntimeError("chatgpt_login_cancelled")
    expires_at = int(time.time() + float(expires_in))
    _tokens_set(profile_id, {"access_token": access, "refresh_token": refresh, "id_token": id_token})
    metadata["profiles"][profile_id] = {"client_id": issued_client_id, "scopes": sorted(scope), "expires_at": expires_at}
    metadata["active_profile"] = profile_id
    _save_metadata(metadata)
    _progress(on_progress, "ChatGPT plan connection is ready." if DIRECT_SCOPE in scope else "ChatGPT sign-in completed; plan usage was not granted.")
    return credential_status()


def get_access_token(cancel_event: threading.Event | None = None) -> str:
    """Return an in-memory token to the provider, refreshing it only when necessary."""
    with _refresh_lock(cancel_event):
        if _cancelled(cancel_event):
            raise RuntimeError("chatgpt_request_cancelled")
        metadata = _load_metadata()
        profile_id = metadata.get("active_profile")
        if not profile_id:
            raise RuntimeError("chatgpt_not_connected")
        profile = metadata["profiles"][profile_id]
        if DIRECT_SCOPE not in set(profile.get("scopes", [])):
            raise RuntimeError("chatgpt_required_scopes_not_granted")
        tokens = _tokens_get(profile_id)
        if not tokens:
            raise RuntimeError("chatgpt_not_connected")
        access = tokens.get("access_token")
        if int(profile.get("expires_at", 0)) > time.time() + 60 and isinstance(access, str) and access:
            if _cancelled(cancel_event):
                raise RuntimeError("chatgpt_request_cancelled")
            return access
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError("chatgpt_not_connected")
        response = _post_form(TOKEN_ENDPOINT, {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": profile["client_id"], "resource": RESOURCE,
        })
        if _cancelled(cancel_event):
            raise RuntimeError("chatgpt_request_cancelled")
        returned_scopes = set(str(response.get("scope", " ".join(profile["scopes"]))).split())
        if not IDENTITY_REQUIRED_SCOPES.issubset(returned_scopes):
            raise RuntimeError("chatgpt_identity_scopes_not_granted")
        if DIRECT_SCOPE not in returned_scopes:
            profile["scopes"] = sorted(returned_scopes)
            _save_metadata(metadata)
            raise RuntimeError("chatgpt_required_scopes_not_granted")
        refreshed = response.get("access_token")
        expires_in = response.get("expires_in")
        if (not isinstance(refreshed, str) or not refreshed or isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float)) or not math.isfinite(expires_in) or expires_in <= 0):
            raise RuntimeError("chatgpt_token_response_invalid")
        replacement = response.get("refresh_token")
        if replacement is not None and (not isinstance(replacement, str) or not replacement):
            raise RuntimeError("chatgpt_token_response_invalid")
        tokens["access_token"] = refreshed
        if replacement:
            tokens["refresh_token"] = replacement
        if _cancelled(cancel_event):
            raise RuntimeError("chatgpt_request_cancelled")
        _tokens_set(profile_id, tokens)
        profile["expires_at"] = int(time.time() + float(expires_in))
        profile["scopes"] = sorted(returned_scopes)
        _save_metadata(metadata)
        if _cancelled(cancel_event):
            raise RuntimeError("chatgpt_request_cancelled")
        return refreshed


def logout() -> dict[str, Any]:
    """Attempt renewable-session revocation, then always clear local OAuth tokens."""
    metadata = _load_metadata()
    profile_id = metadata.get("active_profile")
    if not profile_id:
        return {**credential_status(), "revocation_attempted": False, "revoked": False, "warning": None}
    profile = metadata["profiles"][profile_id]
    try:
        tokens = _tokens_get(profile_id)
    except RuntimeError:
        tokens = None
    attempted, revoked, warning = False, False, None
    try:
        if tokens and isinstance(tokens.get("refresh_token"), str):
            attempted = True
            configuration = _official_configuration()
            _post_form(configuration["revocation_endpoint"], {
                "token": tokens["refresh_token"], "token_type_hint": "refresh_token", "client_id": profile["client_id"],
            }, allow_empty=True)
            revoked = True
    except RuntimeError:
        warning = "chatgpt_revocation_failed"
    finally:
        _tokens_delete(profile_id)
        # Keep the issued client ID/profile binding so ordinary reauthentication reuses it.
        _save_metadata(metadata)
    return {**credential_status(), "revocation_attempted": attempted, "revoked": revoked, "warning": warning}
