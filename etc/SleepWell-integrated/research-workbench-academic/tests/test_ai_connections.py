import json
import io
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from researchbench.ai_connections import (
    MAX_OUTPUT_TOKENS,
    ProviderClient,
    ProviderConnectionError,
    credential_status,
    delete_key,
    list_models,
    load_settings,
    provider_catalog,
    save_settings,
    store_key,
    test_connection as check_connection,
)
from researchbench.ai_connections import _request_json, _request_sse


class AIConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project.swa"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def settings(self, provider="openai", **changes):
        value = load_settings(self.project)
        value.update(provider=provider)
        if provider == "anthropic":
            value["endpoint"] = "https://api.anthropic.com/v1"
        elif provider == "openai_compatible":
            value["endpoint"] = "https://local.example/v1"
        elif provider.endswith("subscription"):
            value["endpoint"] = ""
        value.update(model="fixture-model", **changes)
        return value

    def test_catalog_has_all_providers_and_honest_subscription_flags(self):
        catalog = {item["id"]: item for item in provider_catalog()}
        self.assertEqual(set(catalog), {"openai", "anthropic", "openai_compatible",
                                        "chatgpt_subscription", "codex_subscription",
                                        "claude_subscription"})
        for item in catalog.values():
            for key in ("id", "label", "default_endpoint", "subscription"):
                self.assertIn(key, item)
        self.assertFalse(catalog["codex_subscription"]["login_supported"])
        self.assertTrue(catalog["chatgpt_subscription"]["login_supported"])
        self.assertFalse(catalog["chatgpt_subscription"]["output_token_limit_supported"])
        self.assertFalse(catalog["chatgpt_subscription"]["usd_budget_supported"])
        self.assertFalse(catalog["claude_subscription"]["login_supported"])

    def test_settings_roundtrip_never_persists_secret(self):
        saved = save_settings(self.project, self.settings())
        self.assertEqual(load_settings(self.project), saved)
        self.assertNotIn("api_key", (self.project / "ai_connection.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "unapproved"):
            save_settings(self.project, {**self.settings(), "api_key": "secret"})
        with self.assertRaisesRegex(ValueError, "unapproved"):
            save_settings(self.project, {**self.settings(), "unexpected": "x"})

    def test_settings_bounds_and_budget_prices(self):
        with self.assertRaisesRegex(ValueError, "invalid_max_calls"):
            save_settings(self.project, self.settings(max_calls=7))
        with self.assertRaisesRegex(ValueError, "invalid_max_output_tokens"):
            save_settings(self.project, self.settings(max_output_tokens=MAX_OUTPUT_TOKENS + 1))
        with self.assertRaisesRegex(ValueError, "budget_requires"):
            save_settings(self.project, self.settings(budget_usd=1.0))

    def test_chatgpt_subscription_drops_inapplicable_api_usd_budget(self):
        value = save_settings(self.project, self.settings(
            "chatgpt_subscription", budget_usd=1, input_price_per_million=2,
            output_price_per_million=3))
        self.assertIsNone(value["budget_usd"])
        self.assertIsNone(value["input_price_per_million"])
        self.assertIsNone(value["output_price_per_million"])

    def test_endpoint_ssrf_and_embedded_secrets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "official"):
            save_settings(self.project, self.settings(endpoint="https://169.254.169.254/v1"))
        with self.assertRaisesRegex(ValueError, "invalid_endpoint"):
            save_settings(self.project, self.settings(provider="openai_compatible",
                                                       endpoint="https://user:pass@example.com/v1"))
        with self.assertRaisesRegex(ValueError, "endpoint_"):
            save_settings(self.project, self.settings(provider="openai_compatible",
                                                       endpoint="https://example.com/v1?api_key=secret"))

    def test_compatible_private_literal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "private_address"):
            save_settings(self.project, self.settings(provider="openai_compatible",
                                                       endpoint="https://169.254.169.254/v1"))

    @patch("researchbench.ai_connections.OptionalAI.credential_available", return_value=True)
    @patch("researchbench.ai_connections.OptionalAI.store_api_key")
    def test_keys_use_keyring_and_status_never_contains_secret(self, store, available):
        status = store_key(self.settings(), "super-secret")
        store.assert_called_once()
        self.assertTrue(status["available"])
        self.assertNotIn("secret", json.dumps(status).lower())
        available.assert_called_once()

    def test_empty_or_missing_key_is_rejected_without_network(self):
        with self.assertRaisesRegex(ValueError, "api_key_required"):
            store_key(self.settings(), "  ")
        with patch("researchbench.ai_connections.OptionalAI._get_api_key",
                   side_effect=RuntimeError("raw backend secret")):
            with self.assertRaisesRegex(RuntimeError, "api_key_not_found_in_os_keyring") as context:
                list_models(self.settings())
        self.assertNotIn("backend", str(context.exception).lower())

    @patch("researchbench.ai_connections.OptionalAI.delete_api_key")
    @patch("researchbench.ai_connections.OptionalAI.credential_available", return_value=False)
    def test_delete_key_uses_keyring(self, available, delete):
        self.assertFalse(delete_key(self.settings())["available"])
        delete.assert_called_once()

    @patch("researchbench.ai_connections._request_json")
    @patch("researchbench.ai_connections.OptionalAI._get_api_key", return_value="secret-key")
    def test_model_listing_is_get_only_and_bounded(self, get_key, request):
        request.return_value = ({"data": [{"id": "z-model"}, {"id": "a-model"}]}, {})
        result = list_models(self.settings())
        self.assertEqual(result, ["a-model", "z-model"])
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertIsNone(request.call_args.args[3])
        self.assertNotIn("secret-key", repr(result))

    def test_cancel_prevents_model_request(self):
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            list_models(self.settings(), event)

    @patch("researchbench.ai_connections.shutil.which", return_value="C:/bin/codex.exe")
    @patch("researchbench.ai_connections.subprocess.run")
    def test_codex_status_uses_literal_read_only_status_and_scrubs_api_env(self, run, which):
        run.return_value = Mock(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "must-not-leak", "SAFE": "kept"}):
            status = credential_status(self.settings("codex_subscription"))
        self.assertTrue(status["connected"])
        self.assertFalse(status["supported"])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["C:/bin/codex.exe", "login", "status"])
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["env"]["SAFE"], "kept")

    def test_claude_subscription_is_explicitly_unsupported(self):
        with patch("researchbench.ai_connections.shutil.which", return_value="claude.exe"):
            status = check_connection(self.settings("claude_subscription"))
        self.assertFalse(status["supported"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["test"], "authentication_status_only")

    def test_chatgpt_model_catalog_uses_entitled_visible_slugs(self):
        models = {"models": [
            {"slug": "eligible-b", "display_name": "B", "visibility": "list"},
            {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"},
            {"slug": "eligible-a", "display_name": "A", "visibility": "list"},
        ]}
        with patch("researchbench.chatgpt_auth.get_access_token", return_value="oauth-secret"), \
                patch("researchbench.ai_connections._request_json", return_value=(models, {})) as request:
            result = list_models(self.settings("chatgpt_subscription"))
        self.assertEqual(result, ["eligible-b", "eligible-a"])
        self.assertNotIn("oauth-secret", repr(result))
        self.assertEqual(request.call_args.args[0], "GET")

    def test_chatgpt_responses_payload_has_preview_required_fields_only(self):
        captured = {}

        def transport(method, url, headers, payload, timeout):
            captured.update(url=url, payload=payload)
            return {"id": "resp-1", "_streamed_output_text": "{}",
                    "usage": {"input_tokens": 9, "output_tokens": 2}}

        result = ProviderClient(self.settings("chatgpt_subscription"), transport=transport).complete(
            [{"role": "system", "content": "JSON only"},
             {"role": "user", "content": "Analyze"}], role="report_writer")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(captured["payload"]["instructions"], "JSON only")
        self.assertEqual(captured["payload"]["input"], [{"role": "user", "content": "Analyze"}])
        self.assertIs(captured["payload"]["store"], False)
        self.assertIs(captured["payload"]["stream"], True)
        self.assertNotIn("tools", captured["payload"])
        self.assertNotIn("max_output_tokens", captured["payload"])
        self.assertEqual(result["content"], "{}")
        self.assertFalse(result["actual_llm"])
        self.assertFalse(result["cost_known"])
        self.assertEqual(result["limit_status"]["output_token_limit"],
                         "not_supported_by_chatgpt_plan_preview")

    @patch("researchbench.ai_connections._assert_safe_destination")
    @patch("researchbench.ai_connections.urllib.request.build_opener")
    def test_chatgpt_sse_requires_completed_and_reads_terminal_usage(self, build_opener, safe):
        class Response:
            headers = {"x-request-id": "header-id"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                events = [
                    {"type": "response.output_text.delta", "delta": "{"},
                    {"type": "response.output_text.delta", "delta": "}"},
                    {"type": "response.completed", "response": {
                        "id": "resp-complete", "usage": {"input_tokens": 3, "output_tokens": 1}}},
                ]
                return iter((f"data: {json.dumps(event)}\n".encode() for event in events))

        build_opener.return_value.open.return_value = Response()
        result, headers = _request_sse(
            "https://api.openai.com/v1/responses", {"Authorization": "Bearer hidden"},
            {"model": "fixture", "store": False, "stream": True}, 2, None)
        self.assertEqual(result["_streamed_output_text"], "{}")
        self.assertEqual(result["usage"]["input_tokens"], 3)
        self.assertEqual(headers["x-request-id"], "header-id")

    @patch("researchbench.ai_connections._assert_safe_destination")
    @patch("researchbench.ai_connections.urllib.request.build_opener")
    def test_periodic_sse_cannot_extend_monotonic_deadline(self, build_opener, safe):
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): return iter([b": heartbeat\n"])
        build_opener.return_value.open.return_value = Response()
        with patch("researchbench.ai_connections.time.monotonic", side_effect=[10.0, 10.2, 11.1]):
            with self.assertRaisesRegex(RuntimeError, "provider_stream_timeout"):
                _request_sse("https://api.openai.com/v1/responses", {},
                             {"store": False, "stream": True}, 1, None)

    @patch("researchbench.ai_connections._assert_safe_destination")
    @patch("researchbench.ai_connections.urllib.request.build_opener")
    def test_failed_sse_preserves_only_sanitized_recovery_metadata(self, build_opener, safe):
        class Response:
            headers = {"x-request-id": "req-header"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self):
                event = {"type": "response.failed", "response": {"id": "resp-safe", "error": {
                    "code": "subscription_sharing_usage_limit_exceeded", "param": "model",
                    "message": "contains bearer secret"}}}
                return iter([f"data: {json.dumps(event)}\n".encode()])
        build_opener.return_value.open.return_value = Response()
        with self.assertRaises(ProviderConnectionError) as context:
            _request_sse("https://api.openai.com/v1/responses", {},
                         {"store": False, "stream": True}, 2, None)
        safe_error = context.exception.as_dict()
        self.assertEqual(safe_error["error_code"], "subscription_sharing_usage_limit_exceeded")
        self.assertEqual(safe_error["param"], "model")
        self.assertEqual(safe_error["request_id"], "resp-safe")
        self.assertNotIn("secret", json.dumps(safe_error).lower())

    def test_openai_completion_contract_and_unknown_cost(self):
        calls = []

        def transport(method, url, headers, payload, timeout):
            calls.append((method, url, headers, payload, timeout))
            return {"id": "req-1", "choices": [{"message": {"content": '{"answer": 1}'}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4}}

        result = ProviderClient(self.settings(), transport=transport).complete(
            [{"role": "system", "content": "Return JSON."},
             {"role": "user", "content": "Analyze."}], role="report_writer")
        self.assertEqual(result["content"], '{"answer": 1}')
        self.assertEqual(result["provider"], "openai")
        self.assertFalse(result["actual_llm"])
        self.assertFalse(result["cost_known"])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][3]["max_completion_tokens"], MAX_OUTPUT_TOKENS)
        self.assertIs(calls[0][3]["store"], False)
        self.assertNotIn("max_tokens", calls[0][3])

    def test_openai_length_and_refusal_are_safe_failures(self):
        messages = [{"role": "user", "content": "Return JSON"}]
        length = lambda *args: {"id": "x", "choices": [{"finish_reason": "length",
            "message": {"content": "partial secret"}}], "usage": {}}
        with self.assertRaises(ProviderConnectionError) as context:
            ProviderClient(self.settings(), transport=length).complete(messages, role="writer")
        self.assertEqual(context.exception.as_dict()["reason"], "provider_output_truncated")
        self.assertNotIn("secret", str(context.exception.as_dict()).lower())

        refusal = lambda *args: {"id": "x", "choices": [{"finish_reason": "stop",
            "message": {"content": None, "refusal": "private policy explanation"}}], "usage": {}}
        with self.assertRaises(ProviderConnectionError) as context:
            ProviderClient(self.settings(), transport=refusal).complete(messages, role="writer")
        self.assertEqual(context.exception.as_dict()["reason"], "provider_response_refused")
        self.assertNotIn("private", str(context.exception.as_dict()).lower())

    def test_compatible_provider_retains_max_tokens(self):
        captured = {}
        def transport(method, url, headers, payload, timeout):
            captured.update(payload)
            return {"id": "x", "choices": [{"message": {"content": "{}"}}], "usage": {}}
        ProviderClient(self.settings("openai_compatible"), transport=transport).complete(
            [{"role": "user", "content": "Go"}], role="writer")
        self.assertEqual(captured["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertNotIn("max_completion_tokens", captured)
        self.assertNotIn("store", captured)

    def test_anthropic_completion_shape_and_known_cost(self):
        captured = {}

        def transport(method, url, headers, payload, timeout):
            captured.update(url=url, payload=payload)
            return ({"id": "msg-1", "content": [{"type": "text", "text": "{}"}],
                     "usage": {"input_tokens": 100, "output_tokens": 50}}, {})

        settings = self.settings("anthropic", input_price_per_million=1,
                                 output_price_per_million=2)
        result = ProviderClient(settings, transport=transport).complete(
            [{"role": "system", "content": "JSON only"},
             {"role": "user", "content": "Go"}], role="reviewer")
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured["payload"]["system"], "JSON only")
        self.assertTrue(result["cost_known"])
        self.assertAlmostEqual(result["cost_usd"], 0.0002)

    def test_no_automatic_retry_and_transport_errors_are_sanitized(self):
        attempts = []

        def failing(*args):
            attempts.append(args)
            raise urllib.error.URLError("secret-key internal hostname")

        with self.assertRaisesRegex(RuntimeError, "injected_transport_failed") as context:
            ProviderClient(self.settings(), transport=failing).complete(
                [{"role": "user", "content": "Go"}], role="writer")
        self.assertEqual(len(attempts), 1)
        self.assertNotIn("secret", str(context.exception).lower())

    @patch("researchbench.ai_connections._assert_safe_destination")
    @patch("researchbench.ai_connections.urllib.request.build_opener")
    def test_http_redirect_and_raw_error_are_not_exposed(self, build_opener, safe):
        opener = build_opener.return_value
        opener.open.side_effect = urllib.error.HTTPError(
            "https://api.openai.com/v1/models?secret=x", 302, "redirect to private host", {}, None)
        with self.assertRaisesRegex(RuntimeError, "provider_http_error_302") as context:
            _request_json("GET", "https://api.openai.com/v1/models", {}, None, 1, None)
        self.assertNotIn("secret", str(context.exception).lower())
        self.assertNotIn("private", str(context.exception).lower())
        self.assertEqual(opener.open.call_count, 1)

    @patch("researchbench.ai_connections._assert_safe_destination")
    @patch("researchbench.ai_connections.urllib.request.build_opener")
    def test_http_error_exposes_bounded_code_param_and_request_id_only(self, build_opener, safe):
        body = json.dumps({"error": {"code": "usage_unavailable", "param": "model",
                                     "message": "secret diagnostic"}}).encode()
        build_opener.return_value.open.side_effect = urllib.error.HTTPError(
            "https://api.openai.com/v1/models", 429, "raw secret", {"x-request-id": "req-safe"}, io.BytesIO(body))
        with self.assertRaises(ProviderConnectionError) as context:
            _request_json("GET", "https://api.openai.com/v1/models", {}, None, 1, None)
        self.assertEqual(context.exception.as_dict(), {
            "reason": "provider_http_error_429", "error_code": "usage_unavailable",
            "param": "model", "request_id": "req-safe", "http_status": 429})

    def test_chatgpt_auth_cancellation_is_not_mislabeled_as_disconnected(self):
        with patch("researchbench.chatgpt_auth.get_access_token",
                   side_effect=RuntimeError("chatgpt_request_cancelled")):
            with self.assertRaisesRegex(RuntimeError, "ai_request_cancelled"):
                list_models(self.settings("chatgpt_subscription"))

    def test_call_limit_and_subscription_generation_block(self):
        settings = self.settings(max_calls=1)
        transport = lambda *args: {"id": "x", "choices": [{"message": {"content": "{}"}}], "usage": {}}
        client = ProviderClient(settings, transport=transport)
        client.complete([{"role": "user", "content": "Go"}], role="writer")
        with self.assertRaisesRegex(RuntimeError, "call_limit"):
            client.complete([{"role": "user", "content": "Again"}], role="writer")
        with self.assertRaisesRegex(RuntimeError, "subscription_generation"):
            ProviderClient(self.settings("codex_subscription"), transport=transport).complete(
                [{"role": "user", "content": "Go"}], role="writer")


if __name__ == "__main__":
    unittest.main()
