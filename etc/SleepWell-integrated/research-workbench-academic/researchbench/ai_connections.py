"""Bounded, credential-safe connections to optional AI providers.

Project settings never contain credentials. API keys stay in the same OS
keyring used by :mod:`researchbench.optional_ai`.
"""
from __future__ import annotations

import json
import ipaddress
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .academic_store import write_json
from .optional_ai import OptionalAI


MAX_CALLS = 6
MAX_OUTPUT_TOKENS = 2500
MAX_RESPONSE_BYTES = 2_000_000
MAX_ERROR_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 90.0
MAX_TOTAL_TIMEOUT_SECONDS = 300.0
SETTINGS_FILE = "ai_connection.json"

_CATALOG = (
    {"id": "openai", "label": "OpenAI API", "default_endpoint": "https://api.openai.com/v1", "subscription": False,
     "login_supported": False, "output_token_limit_supported": True, "usd_budget_supported": True,
     "official_url": "https://platform.openai.com/api-keys"},
    {"id": "anthropic", "label": "Anthropic API", "default_endpoint": "https://api.anthropic.com/v1", "subscription": False,
     "login_supported": False, "output_token_limit_supported": True, "usd_budget_supported": True,
     "official_url": "https://console.anthropic.com/settings/keys"},
    {"id": "openai_compatible", "label": "OpenAI-compatible API", "default_endpoint": "", "subscription": False,
     "login_supported": False, "output_token_limit_supported": True, "usd_budget_supported": True,
     "official_url": ""},
    {"id": "chatgpt_subscription", "label": "ChatGPT subscription", "default_endpoint": "", "subscription": True,
     "login_supported": True, "output_token_limit_supported": False, "usd_budget_supported": False,
     "official_url": "https://developers.openai.com/siwc/token-sharing-open-source/sign-in"},
    {"id": "codex_subscription", "label": "ChatGPT / Codex subscription", "default_endpoint": "", "subscription": True,
     "login_supported": False, "output_token_limit_supported": False, "usd_budget_supported": False,
     "reason": "app_owned_siwc_oauth_not_configured",
     "official_url": "https://developers.openai.com/siwc/token-sharing-open-source/sign-in"},
    {"id": "claude_subscription", "label": "Claude subscription (not available to third-party apps)", "default_endpoint": "", "subscription": True,
     "login_supported": False, "output_token_limit_supported": False, "usd_budget_supported": False,
     "reason": "third_party_claude_subscription_auth_not_supported",
     "official_url": "https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use"},
)
_PROVIDERS = {item["id"] for item in _CATALOG}
_API_PROVIDERS = {"openai", "anthropic", "openai_compatible"}
_DIRECT_SUBSCRIPTION_PROVIDERS = {"chatgpt_subscription"}
_ALLOWED = {
    "schema", "provider", "endpoint", "model", "account", "max_calls",
    "max_output_tokens", "timeout_seconds", "total_timeout_seconds", "budget_usd",
    "input_price_per_million", "output_price_per_million",
}
_SECRET_WORDS = ("key", "secret", "password", "token", "authorization", "credential")
_SCRUB_ENV = {
    "OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "CODEX_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
}


class ProviderConnectionError(RuntimeError):
    """Safe provider failure metadata without response messages or credentials."""

    def __init__(self, reason: str, *, error_code: str | None = None, param: str | None = None,
                 request_id: str | None = None, http_status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.error_code = _safe_error_field(error_code, 100)
        self.param = _safe_error_field(param, 100)
        self.request_id = _safe_error_field(request_id, 200)
        self.http_status = http_status if isinstance(http_status, int) else None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in {
            "reason": self.reason, "error_code": self.error_code, "param": self.param,
            "request_id": self.request_id, "http_status": self.http_status,
        }.items() if value is not None}


def _safe_error_field(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1," + str(maximum) + r"}", value):
        return None
    return value


def provider_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in _CATALOG]


def _defaults() -> dict[str, Any]:
    return {
        "schema": 1,
        "provider": "openai",
        "endpoint": "https://api.openai.com/v1",
        "model": "",
        "account": "default",
        "max_calls": MAX_CALLS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "total_timeout_seconds": MAX_TOTAL_TIMEOUT_SECONDS,
        "budget_usd": None,
        "input_price_per_million": None,
        "output_price_per_million": None,
    }


def _settings_path(project_path: str | os.PathLike[str]) -> Path:
    return Path(project_path).resolve() / SETTINGS_FILE


def load_settings(project_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = _settings_path(project_path)
    if not path.exists():
        return _defaults()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_ai_connection_settings") from exc
    return _sanitize_settings(raw)


def save_settings(project_path: str | os.PathLike[str], settings: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_settings(settings, reject_unknown=True)
    write_json(_settings_path(project_path), sanitized)
    return dict(sanitized)


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid_{name}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{name}") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"invalid_{name}")
    return result


def _sanitize_settings(settings: Any, *, reject_unknown: bool = False) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("settings_must_be_object")
    unknown = set(settings) - _ALLOWED
    if unknown and (reject_unknown or any(any(word in str(k).lower() for word in _SECRET_WORDS) for k in unknown)):
        raise ValueError("settings_contain_unapproved_fields")
    value = _defaults()
    value.update({key: settings[key] for key in _ALLOWED if key in settings})
    if value.get("schema") != 1:
        raise ValueError("unsupported_ai_connection_schema")
    provider = str(value.get("provider", "")).strip()
    if provider not in _PROVIDERS:
        raise ValueError("unsupported_provider")
    value["provider"] = provider
    value["model"] = str(value.get("model", "")).strip()
    if len(value["model"]) > 200 or any(ord(char) < 32 for char in value["model"]):
        raise ValueError("invalid_model")
    endpoint = str(value.get("endpoint", "")).strip().rstrip("/")
    if provider in _API_PROVIDERS:
        if not endpoint:
            endpoint = next(item["default_endpoint"] for item in _CATALOG if item["id"] == provider)
        endpoint = _validate_endpoint(provider, endpoint)
    else:
        endpoint = ""
    value["endpoint"] = endpoint
    account = str(value.get("account", "default")).strip() or "default"
    if len(account) > 100 or any(ord(char) < 32 for char in account):
        raise ValueError("invalid_account")
    value["account"] = account
    value["max_calls"] = int(_number(value.get("max_calls"), "max_calls", minimum=1, maximum=MAX_CALLS))
    value["max_output_tokens"] = int(_number(value.get("max_output_tokens"), "max_output_tokens", minimum=1, maximum=MAX_OUTPUT_TOKENS))
    value["timeout_seconds"] = _number(value.get("timeout_seconds"), "timeout_seconds", minimum=1, maximum=DEFAULT_TIMEOUT_SECONDS)
    value["total_timeout_seconds"] = _number(value.get("total_timeout_seconds"), "total_timeout_seconds", minimum=1, maximum=MAX_TOTAL_TIMEOUT_SECONDS)
    if value["timeout_seconds"] > value["total_timeout_seconds"]:
        raise ValueError("timeout_exceeds_total_timeout")
    budget = value.get("budget_usd")
    if budget in (None, ""):
        value["budget_usd"] = None
    else:
        value["budget_usd"] = _number(budget, "budget_usd", minimum=0.000001, maximum=1_000_000)
    for name in ("input_price_per_million", "output_price_per_million"):
        raw = value.get(name)
        value[name] = None if raw in (None, "") else _number(raw, name, minimum=0, maximum=1_000_000)
    if provider == "chatgpt_subscription":
        # ChatGPT plan use is not metered as user-supplied per-token API USD
        # pricing. Avoid presenting an estimate as an enforceable billing cap.
        value["budget_usd"] = None
        value["input_price_per_million"] = None
        value["output_price_per_million"] = None
    if value["budget_usd"] is not None and (
        value["input_price_per_million"] is None or value["output_price_per_million"] is None
    ):
        raise ValueError("budget_requires_explicit_input_and_output_prices")
    return {key: value[key] for key in _ALLOWED}


def _validate_endpoint(provider: str, endpoint: str) -> str:
    endpoint = OptionalAI._validate_endpoint(endpoint).rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.query:
        raise ValueError("endpoint_query_not_allowed")
    if provider == "openai" and (parsed.scheme, parsed.hostname, parsed.port) != ("https", "api.openai.com", None):
        raise ValueError("openai_endpoint_must_be_official")
    if provider == "anthropic" and (parsed.scheme, parsed.hostname, parsed.port) != ("https", "api.anthropic.com", None):
        raise ValueError("anthropic_endpoint_must_be_official")
    try:
        literal = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        literal = None
    if literal is not None and not (literal.is_global or literal.is_loopback):
        raise ValueError("endpoint_private_address_not_allowed")
    return endpoint


def _account(settings: dict[str, Any]) -> str:
    value = _sanitize_settings(settings)
    if value["provider"] not in _API_PROVIDERS:
        return value["account"]
    return OptionalAI.credential_account(value["provider"], value["endpoint"])


def store_key(settings: dict[str, Any], key: str) -> dict[str, Any]:
    value = _sanitize_settings(settings)
    if value["provider"] not in _API_PROVIDERS:
        raise ValueError("subscription_provider_does_not_accept_api_keys")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("api_key_required")
    OptionalAI.store_api_key(value["provider"], key, _account(value))
    return credential_status(value)


def delete_key(settings: dict[str, Any]) -> dict[str, Any]:
    value = _sanitize_settings(settings)
    if value["provider"] not in _API_PROVIDERS:
        raise ValueError("subscription_provider_does_not_accept_api_keys")
    try:
        OptionalAI.delete_api_key(value["provider"], _account(value))
    except Exception as exc:
        if exc.__class__.__name__ not in {"PasswordDeleteError", "KeyError"}:
            raise RuntimeError("credential_delete_failed") from exc
    return credential_status(value)


def credential_status(settings: dict[str, Any]) -> dict[str, Any]:
    value = _sanitize_settings(settings)
    provider = value["provider"]
    if provider in _API_PROVIDERS:
        try:
            available = OptionalAI.credential_available(provider, _account(value))
        except Exception:
            available = False
        return {"provider": provider, "available": bool(available), "connected": None,
                "method": "os_keyring", "supported": True}
    if provider == "codex_subscription":
        return _codex_status()
    if provider == "chatgpt_subscription":
        try:
            from . import chatgpt_auth
            result = chatgpt_auth.credential_status()
        except Exception:
            return {"provider": provider, "available": False, "connected": False,
                    "method": "siwc_oauth", "supported": True, "reason": "chatgpt_auth_unavailable"}
        if not isinstance(result, dict):
            raise RuntimeError("invalid_chatgpt_auth_status")
        return {"provider": provider, "available": True, "connected": bool(result.get("connected")),
                "method": "siwc_oauth", "supported": bool(result.get("supported", True)),
                "plan_scope_granted": bool(result.get("plan_scope_granted")),
                "profile": str(result.get("profile", ""))[:200],
                "expires_at": str(result.get("expires_at", ""))[:100],
                "reason": str(result.get("reason", ""))[:200]}
    return {
        "provider": provider, "available": bool(shutil.which("claude")), "connected": False,
        "method": "subscription_cli", "supported": False,
        "reason": "third_party_claude_subscription_auth_not_supported",
        "guidance": "Use an Anthropic API key for third-party integrations.",
    }


def _scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _SCRUB_ENV:
        env.pop(name, None)
    return env


def _codex_status() -> dict[str, Any]:
    executable = shutil.which("codex")
    if not executable:
        return {"provider": "codex_subscription", "available": False, "connected": False,
                "method": "codex_login_status", "supported": False, "reason": "codex_cli_not_installed"}
    startup = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [executable, "login", "status"], shell=False, capture_output=True, text=True,
            timeout=10, env=_scrubbed_env(), creationflags=startup,
        )
    except (OSError, subprocess.SubprocessError):
        return {"provider": "codex_subscription", "available": True, "connected": False,
                "method": "codex_login_status", "supported": False, "reason": "status_check_failed"}
    connected = completed.returncode == 0 and "logged in" in (completed.stdout + completed.stderr).lower()
    return {"provider": "codex_subscription", "available": True, "connected": connected,
            "method": "codex_login_status", "supported": False,
            "reason": "generation_bridge_not_safely_supported",
            "guidance": "Use OpenAI API credentials; Codex subscription status is informational only."}


def list_models(settings: dict[str, Any], cancel_event: threading.Event | None = None) -> list[str]:
    value = _sanitize_settings(settings)
    _check_cancel(cancel_event)
    if value["provider"] == "chatgpt_subscription":
        token = _chatgpt_token(cancel_event)
        payload, _ = _request_json(
            "GET", "https://api.openai.com/v1/models", _auth_headers("openai", token), None,
            value["timeout_seconds"], cancel_event,
        )
        records = payload.get("models", []) if isinstance(payload, dict) else []
        models = []
        if isinstance(records, list):
            for item in records[:500]:
                if not isinstance(item, dict) or item.get("visibility") != "list":
                    continue
                slug = item.get("slug")
                if isinstance(slug, str) and 0 < len(slug) <= 200:
                    models.append(slug)
        return list(dict.fromkeys(models))
    if value["provider"] not in _API_PROVIDERS:
        return [value["model"]] if value["model"] else []
    key = _load_key(value)
    headers = _auth_headers(value["provider"], key)
    payload, _ = _request_json("GET", _url(value["endpoint"], "models"), headers, None,
                               value["timeout_seconds"], cancel_event)
    records = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    models: list[str] = []
    if isinstance(records, list):
        for item in records[:500]:
            name = item.get("id") if isinstance(item, dict) else None
            if isinstance(name, str) and 0 < len(name) <= 200:
                models.append(name)
    return sorted(set(models))


def test_connection(settings: dict[str, Any], cancel_event: threading.Event | None = None) -> dict[str, Any]:
    value = _sanitize_settings(settings)
    if value["provider"] in _API_PROVIDERS or value["provider"] == "chatgpt_subscription":
        models = list_models(value, cancel_event)
        return {"provider": value["provider"], "connected": True, "test": "models_list_get",
                "model_count": len(models), "actual_llm": False}
    status = credential_status(value)
    return {**status, "test": "authentication_status_only", "actual_llm": False}


def _load_key(settings: dict[str, Any]) -> str:
    try:
        return OptionalAI._get_api_key(settings["provider"], _account(settings))
    except Exception as exc:
        raise RuntimeError("api_key_not_found_in_os_keyring") from exc


def _chatgpt_token(cancel_event: threading.Event | None) -> str:
    _check_cancel(cancel_event)
    try:
        from . import chatgpt_auth
        token = chatgpt_auth.get_access_token(cancel_event)
    except Exception as exc:
        code = str(exc)
        if code in {"ai_request_cancelled", "chatgpt_request_cancelled"}:
            raise RuntimeError("ai_request_cancelled") from None
        raise RuntimeError("chatgpt_subscription_not_connected") from None
    if not isinstance(token, str) or not token:
        raise RuntimeError("chatgpt_subscription_not_connected")
    return token


def _auth_headers(provider: str, key: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if provider == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _url(endpoint: str, resource: str) -> str:
    return f"{endpoint.rstrip('/')}/{resource.lstrip('/')}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, "redirect_not_allowed", headers, fp)


def _request_json(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None,
                  timeout: float, cancel_event: threading.Event | None) -> tuple[dict[str, Any], dict[str, str]]:
    _check_cancel(cancel_event)
    _assert_safe_destination(url)
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_RESPONSE_BYTES:
                raise RuntimeError("ai_response_too_large")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("ai_response_too_large")
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raise _safe_http_error(exc) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("provider_connection_failed") from None
    _check_cancel(cancel_event)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("provider_returned_invalid_json") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("provider_returned_invalid_json")
    return parsed, response_headers


def _request_sse(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float,
                 cancel_event: threading.Event | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Consume one Responses SSE stream through its terminal completed event."""
    _check_cancel(cancel_event)
    _assert_safe_destination(url)
    request_headers = dict(headers)
    request_headers["Accept"] = "text/event-stream"
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers=request_headers, method="POST")
    total = 0
    deltas: list[str] = []
    completed: dict[str, Any] | None = None
    response_headers: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            iterator = iter(response)
            while True:
                _check_cancel(cancel_event)
                if time.monotonic() >= deadline:
                    raise RuntimeError("provider_stream_timeout")
                try:
                    raw_line = next(iterator)
                except StopIteration:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("provider_stream_timeout")
                total += len(raw_line)
                if total > MAX_RESPONSE_BYTES:
                    raise RuntimeError("ai_response_too_large")
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    raise RuntimeError("provider_returned_invalid_sse") from None
                if not isinstance(event, dict):
                    raise RuntimeError("provider_returned_invalid_sse")
                event_type = event.get("type")
                if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    deltas.append(event["delta"])
                elif event_type == "response.completed" and isinstance(event.get("response"), dict):
                    completed = event["response"]
                    break
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    response_value = event.get("response") if isinstance(event.get("response"), dict) else {}
                    error = response_value.get("error") if isinstance(response_value.get("error"), dict) else {}
                    if event_type == "error" and isinstance(event.get("error"), dict):
                        error = event["error"]
                    raise ProviderConnectionError(
                        f"provider_stream_{str(event_type).replace('.', '_')}",
                        error_code=error.get("code"), param=error.get("param"),
                        request_id=response_value.get("id") or response_headers.get("x-request-id"),
                    )
    except RuntimeError:
        raise
    except urllib.error.HTTPError as exc:
        raise _safe_http_error(exc) from None
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError):
        raise RuntimeError("provider_connection_failed") from None
    _check_cancel(cancel_event)
    if completed is None:
        raise RuntimeError("provider_stream_ended_without_completion")
    completed = dict(completed)
    if deltas:
        completed["_streamed_output_text"] = "".join(deltas)
    return completed, response_headers


def _safe_http_error(exc: urllib.error.HTTPError) -> ProviderConnectionError:
    payload: dict[str, Any] = {}
    try:
        raw = exc.read(MAX_ERROR_BYTES + 1)
        if len(raw) <= MAX_ERROR_BYTES:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    headers = getattr(exc, "headers", None)
    request_id = headers.get("x-request-id") if headers is not None else None
    return ProviderConnectionError(
        f"provider_http_error_{int(exc.code)}", error_code=error.get("code"),
        param=error.get("param"), request_id=request_id, http_status=int(exc.code),
    )


def _assert_safe_destination(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ""
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise RuntimeError("provider_connection_failed") from None
    if not addresses:
        raise RuntimeError("provider_connection_failed")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address[4][0])
        except ValueError:
            raise RuntimeError("provider_connection_failed") from None
        if not resolved.is_global:
            raise RuntimeError("provider_destination_not_public")


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("ai_request_cancelled")


def _messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise ValueError("messages_must_be_nonempty_list")
    result = []
    total = 0
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"}:
            raise ValueError("invalid_message")
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError("invalid_message")
        total += len(content)
        if total > 200_000:
            raise ValueError("messages_too_large")
        result.append({"role": item["role"], "content": content})
    return result


Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None, float], Any]


class ProviderClient:
    def __init__(self, settings: dict[str, Any], *, cancel_event: threading.Event | None = None,
                 transport: Transport | None = None):
        self.settings = _sanitize_settings(settings)
        self.cancel_event = cancel_event
        self.transport = transport
        self._started = time.monotonic()
        self._calls = 0
        self._spent_usd = 0.0

    def complete(self, messages: list[dict[str, str]], *, role: str,
                 max_output_tokens: int | None = None, timeout_seconds: float | None = None) -> dict[str, Any]:
        _check_cancel(self.cancel_event)
        if self.settings["provider"] not in _API_PROVIDERS | _DIRECT_SUBSCRIPTION_PROVIDERS:
            raise RuntimeError("subscription_generation_bridge_not_supported")
        if self._calls >= self.settings["max_calls"]:
            raise RuntimeError("ai_call_limit_reached")
        if time.monotonic() - self._started >= self.settings["total_timeout_seconds"]:
            raise RuntimeError("ai_total_timeout_reached")
        clean_messages = _messages(messages)
        if not isinstance(role, str) or not role.strip() or len(role) > 100:
            raise ValueError("role_required")
        if not self.settings["model"]:
            raise ValueError("model_must_be_selected")
        output_limit = self.settings["max_output_tokens"] if max_output_tokens is None else int(
            _number(max_output_tokens, "max_output_tokens", minimum=1,
                    maximum=self.settings["max_output_tokens"])
        )
        timeout = self.settings["timeout_seconds"] if timeout_seconds is None else _number(
            timeout_seconds, "timeout_seconds", minimum=1, maximum=self.settings["timeout_seconds"]
        )
        remaining = self.settings["total_timeout_seconds"] - (time.monotonic() - self._started)
        timeout = min(timeout, max(1.0, remaining))
        self._budget_preflight(clean_messages, output_limit)
        provider = self.settings["provider"]
        if self.transport:
            credential = "TEST_CREDENTIAL"
        elif provider == "chatgpt_subscription":
            credential = _chatgpt_token(self.cancel_event)
        else:
            credential = _load_key(self.settings)
        headers = _auth_headers("openai" if provider == "chatgpt_subscription" else provider, credential)
        url, payload = self._payload(clean_messages, role, output_limit)
        self._calls += 1
        if self.transport:
            try:
                raw = self.transport("POST", url, headers, payload, timeout)
            except Exception:
                raise RuntimeError("injected_transport_failed") from None
            if isinstance(raw, tuple):
                response, response_headers = raw
            else:
                response, response_headers = raw, {}
            if not isinstance(response, dict):
                raise RuntimeError("provider_returned_invalid_json")
        elif provider == "chatgpt_subscription":
            response, response_headers = _request_sse(url, headers, payload, timeout, self.cancel_event)
        else:
            response, response_headers = _request_json("POST", url, headers, payload, timeout, self.cancel_event)
        _check_cancel(self.cancel_event)
        result = self._parse(response, response_headers)
        result.update(provider=provider, model=self.settings["model"], actual_llm=self.transport is None)
        result["limit_status"] = {
            "output_token_limit": (
                "not_supported_by_chatgpt_plan_preview"
                if provider == "chatgpt_subscription" else "requested_from_provider"
            ),
            "requested_output_tokens": output_limit,
            "response_byte_limit": MAX_RESPONSE_BYTES,
            "timeout_seconds": timeout,
        }
        self._apply_cost(result)
        return result

    def _payload(self, messages: list[dict[str, str]], role: str, output_limit: int) -> tuple[str, dict[str, Any]]:
        provider = self.settings["provider"]
        if provider == "chatgpt_subscription":
            instructions = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
            input_messages = [item for item in messages if item["role"] != "system"]
            if not input_messages:
                input_messages = [{"role": "user", "content": "Return the requested JSON."}]
            payload: dict[str, Any] = {"model": self.settings["model"], "input": input_messages,
                                       "store": False, "stream": True}
            if instructions:
                payload["instructions"] = instructions
            return "https://api.openai.com/v1/responses", payload
        if provider == "anthropic":
            system = [item["content"] for item in messages if item["role"] == "system"]
            conversation = [item for item in messages if item["role"] != "system"]
            if not conversation:
                conversation = [{"role": "user", "content": "Return the requested JSON."}]
            payload: dict[str, Any] = {"model": self.settings["model"], "max_tokens": output_limit,
                                       "messages": conversation}
            if system:
                payload["system"] = "\n\n".join(system)
            return _url(self.settings["endpoint"], "messages"), payload
        if provider == "openai":
            payload = {"model": self.settings["model"], "messages": messages,
                       "max_completion_tokens": output_limit, "store": False}
        else:
            payload = {"model": self.settings["model"], "messages": messages,
                       "max_tokens": output_limit}
        return _url(self.settings["endpoint"], "chat/completions"), payload

    def _parse(self, response: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        provider = self.settings["provider"]
        if provider == "chatgpt_subscription":
            content = response.get("_streamed_output_text", "")
            if not content:
                output = response.get("output", [])
                if isinstance(output, list):
                    text_parts = []
                    for item in output:
                        if not isinstance(item, dict):
                            continue
                        for block in item.get("content", []):
                            if isinstance(block, dict) and block.get("type") == "output_text" and isinstance(block.get("text"), str):
                                text_parts.append(block["text"])
                    content = "".join(text_parts)
            raw_usage = response.get("usage", {})
            usage = {"input_tokens": raw_usage.get("input_tokens"), "output_tokens": raw_usage.get("output_tokens")}
        elif provider == "anthropic":
            blocks = response.get("content")
            if not isinstance(blocks, list):
                raise RuntimeError("provider_response_missing_content")
            content = "".join(str(block.get("text", "")) for block in blocks
                              if isinstance(block, dict) and block.get("type") == "text")
            raw_usage = response.get("usage", {})
            usage = {"input_tokens": raw_usage.get("input_tokens"), "output_tokens": raw_usage.get("output_tokens")}
        else:
            choices = response.get("choices")
            try:
                choice = choices[0]
                message = choice["message"]
                content = message["content"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError("provider_response_missing_content") from None
            if provider == "openai":
                if isinstance(message.get("refusal"), str) and message["refusal"].strip():
                    raise ProviderConnectionError("provider_response_refused", error_code="refusal")
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    raise ProviderConnectionError("provider_output_truncated", error_code="length")
                if finish_reason not in (None, "stop"):
                    raise ProviderConnectionError(
                        "provider_response_incomplete", error_code=_safe_error_field(finish_reason, 100))
            raw_usage = response.get("usage", {})
            usage = {"input_tokens": raw_usage.get("prompt_tokens"), "output_tokens": raw_usage.get("completion_tokens")}
        if not isinstance(content, str) or not content:
            raise RuntimeError("provider_response_missing_content")
        request_id = response.get("request_id") or response.get("id") or headers.get("x-request-id")
        if not isinstance(request_id, str) or len(request_id) > 200:
            request_id = f"local-{uuid.uuid4().hex}"
        return {"content": content, "usage": usage, "request_id": request_id}

    def _budget_preflight(self, messages: list[dict[str, str]], output_limit: int) -> None:
        budget = self.settings["budget_usd"]
        if budget is None:
            return
        approximate_input = math.ceil(sum(len(item["content"]) for item in messages) / 3)
        estimate = approximate_input * self.settings["input_price_per_million"] / 1_000_000
        estimate += output_limit * self.settings["output_price_per_million"] / 1_000_000
        if self._spent_usd + estimate > budget:
            raise RuntimeError("estimated_cost_exceeds_budget")

    def _apply_cost(self, result: dict[str, Any]) -> None:
        if self.settings["provider"] == "chatgpt_subscription":
            result["cost_usd"] = None
            result["cost_known"] = False
            return
        usage = result["usage"]
        in_tokens, out_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        prices = (self.settings["input_price_per_million"], self.settings["output_price_per_million"])
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (in_tokens, out_tokens, *prices)):
            cost = (in_tokens * prices[0] + out_tokens * prices[1]) / 1_000_000
            result["cost_usd"] = cost
            result["cost_known"] = True
            self._spent_usd += cost
        else:
            result["cost_usd"] = None
            result["cost_known"] = False
