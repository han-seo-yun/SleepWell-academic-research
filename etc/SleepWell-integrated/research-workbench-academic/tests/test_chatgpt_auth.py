import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from researchbench import chatgpt_auth as auth


def _part(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _token(client_id="oaiapp_fixture", nonce="nonce", subject="subject"):
    return f"{_part({'alg': 'RS256', 'kid': 'fixture'})}.{_part({'iss': auth.ISSUER, 'aud': client_id, 'nonce': nonce, 'sub': subject, 'iat': int(time.time()), 'exp': int(time.time()) + 3600})}.c2ln"


class ChatGPTAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.keyring = {}
        self.env = patch.dict("os.environ", {"SLEEPWELL_USER_DATA_DIR": self.temp.name}, clear=False)
        self.env.start()
        self.get = patch("researchbench.chatgpt_auth.keyring.get_password", side_effect=lambda _s, k: self.keyring.get(k))
        self.set = patch("researchbench.chatgpt_auth.keyring.set_password", side_effect=lambda _s, k, v: self.keyring.__setitem__(k, v))
        self.delete = patch("researchbench.chatgpt_auth.keyring.delete_password", side_effect=lambda _s, k: self.keyring.pop(k, None))
        self.get.start(); self.set.start(); self.delete.start()

    def tearDown(self):
        self.delete.stop(); self.set.stop(); self.get.stop(); self.env.stop(); self.temp.cleanup()

    def seed_profile(self, subject="subject", *, expires_at=0, tokens=None):
        profile = auth._profile_id(subject, "oaiapp_fixture")
        metadata = auth._empty_metadata()
        metadata["profiles"][profile] = {"client_id": "oaiapp_fixture", "scopes": sorted(auth.REQUIRED_SCOPES), "expires_at": expires_at}
        metadata["active_profile"] = profile
        auth._save_metadata(metadata)
        if tokens is not None:
            self.keyring[profile] = json.dumps(tokens)
        return profile

    def test_status_contains_no_tokens(self):
        status = auth.credential_status()
        self.assertFalse(status["connected"])
        self.assertNotIn("token", json.dumps(status).lower())
        self.assertFalse(auth._metadata_path().exists())  # Status checks do not create registration state.

    def test_csrf_mismatch_fails_before_code_exchange(self):
        server = auth._CallbackServer(("127.0.0.1", 0), auth.http.server.BaseHTTPRequestHandler)
        received, errors = [], []
        try:
            def receive():
                try:
                    received.append(auth._receive_callback(server, "expected", None, timeout=2))
                except RuntimeError as exc:
                    errors.append(exc)
            worker = threading.Thread(target=receive)
            worker.start()
            url = f"http://127.0.0.1:{server.server_address[1]}{auth.CALLBACK_PATH}?state=wrong&code=untrusted"
            with auth.urllib.request.urlopen(url, timeout=2):
                pass
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(received, [])
            self.assertEqual(str(errors[0]), "chatgpt_callback_state_invalid")
        finally:
            server.server_close()

    def test_callback_timeout_is_bounded(self):
        server = auth._CallbackServer(("127.0.0.1", 0), auth.http.server.BaseHTTPRequestHandler)
        try:
            with patch.object(server, "handle_request"), patch("researchbench.chatgpt_auth.time.monotonic", side_effect=[0, 1]):
                with self.assertRaisesRegex(RuntimeError, "timed_out"):
                    auth._receive_callback(server, "expected", None, timeout=0.5)
        finally:
            server.server_close()

    def test_cancelled_login_never_opens_browser(self):
        cancelled = threading.Event(); cancelled.set()
        with patch("researchbench.chatgpt_auth.webbrowser.open") as browser:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                auth.login(cancelled)
        browser.assert_not_called()

    def test_id_token_rejects_unsupported_algorithm_without_network(self):
        token = f"{_part({'alg': 'none', 'kid': 'fixture'})}.{_part({'iss': auth.ISSUER})}.c2ln"
        with self.assertRaisesRegex(RuntimeError, "algorithm"):
            auth._validate_id_token(token, client_id="oaiapp_fixture", nonce="nonce")

    @patch("researchbench.chatgpt_auth._verify_windows_rs256", return_value=True)
    @patch("researchbench.chatgpt_auth._fetch_json")
    def test_id_token_requires_signature_and_claims(self, fetch, verify):
        fetch.return_value = {"keys": [{"kid": "fixture", "kty": "RSA", "n": "AQ", "e": "AQAB"}]}
        claims = auth._validate_id_token(_token(), client_id="oaiapp_fixture", nonce="nonce")
        self.assertEqual(claims["sub"], "subject")
        verify.assert_called_once()

    @patch("researchbench.chatgpt_auth._verify_windows_rs256", return_value=False)
    @patch("researchbench.chatgpt_auth._fetch_json")
    def test_id_token_rejects_invalid_signature(self, fetch, verify):
        fetch.return_value = {"keys": [{"kid": "fixture", "kty": "RSA", "n": "AQ", "e": "AQAB"}]}
        with self.assertRaisesRegex(RuntimeError, "signature"):
            auth._validate_id_token(_token(), client_id="oaiapp_fixture", nonce="nonce")

    @patch("researchbench.chatgpt_auth._official_configuration", return_value={"authorization_endpoint": auth.AUTHORIZATION_ENDPOINT, "token_endpoint": auth.TOKEN_ENDPOINT, "revocation_endpoint": f"{auth.ISSUER}/oauth/revoke"})
    @patch("researchbench.chatgpt_auth._validate_id_token", return_value={"sub": "subject"})
    @patch("researchbench.chatgpt_auth._post_form")
    @patch("researchbench.chatgpt_auth._receive_callback", return_value={"state": "ignored", "code": "code", "client_id": "oaiapp_fixture"})
    @patch("researchbench.chatgpt_auth.webbrowser.open", return_value=True)
    def test_mocked_successful_login_stores_tokens_only_in_keyring(self, browser, callback, post, validate, config):
        post.return_value = {"access_token": "access-secret", "refresh_token": "refresh-secret", "id_token": "id-secret", "expires_in": 3600,
                             "scope": " ".join(auth.REQUIRED_SCOPES)}
        status = auth.login()
        self.assertTrue(status["connected"])
        metadata = auth._metadata_path().read_text(encoding="utf-8")
        self.assertNotIn("secret", metadata)
        self.assertIn("access-secret", next(iter(self.keyring.values())))
        browser.assert_called_once()

    @patch("researchbench.chatgpt_auth._official_configuration", return_value={"authorization_endpoint": auth.AUTHORIZATION_ENDPOINT, "token_endpoint": auth.TOKEN_ENDPOINT, "revocation_endpoint": f"{auth.ISSUER}/oauth/revoke"})
    @patch("researchbench.chatgpt_auth._validate_id_token", return_value={"sub": "subject"})
    @patch("researchbench.chatgpt_auth._post_form")
    @patch("researchbench.chatgpt_auth._receive_callback", return_value={"state": "ignored", "code": "code", "client_id": "oaiapp_fixture"})
    @patch("researchbench.chatgpt_auth.webbrowser.open", return_value=True)
    def test_identity_is_retained_when_direct_plan_scope_is_declined(self, browser, callback, post, validate, config):
        post.return_value = {"access_token": "access", "refresh_token": "refresh", "id_token": "id", "expires_in": 3600,
                             "scope": " ".join(auth.IDENTITY_REQUIRED_SCOPES)}
        status = auth.login()
        self.assertFalse(status["connected"])
        self.assertFalse(status["plan_scope_granted"])
        self.assertGreater(len(self.keyring), 1)

    @patch("researchbench.chatgpt_auth.urllib.request.build_opener")
    def test_empty_successful_revocation_response_is_accepted(self, build_opener):
        response = MagicMock()
        response.geturl.return_value = "https://auth.openai.com/api/accounts/revoke"
        response.read.return_value = b""
        build_opener.return_value.open.return_value.__enter__.return_value = response
        result = auth._post_form("https://auth.openai.com/api/accounts/revoke", {"token": "test"}, allow_empty=True)
        self.assertEqual(result, {})

    def test_refresh_checks_cancel_before_network(self):
        self.seed_profile(tokens={"access_token": "old", "refresh_token": "refresh"})
        event = threading.Event(); event.set()
        with patch("researchbench.chatgpt_auth._post_form") as post:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                auth.get_access_token(event)
        post.assert_not_called()

    def test_expired_or_malformed_tokens_are_not_reported_connected(self):
        self.seed_profile(tokens={"access_token": "old"})
        self.assertFalse(auth.credential_status()["connected"])

    def test_long_token_bundle_roundtrips_only_through_chunked_keyring_entries(self):
        profile = "long-profile"
        tokens = {"access_token": "a" * 5_000, "refresh_token": "r" * 4_000, "id_token": "i" * 3_000}
        auth._tokens_set(profile, tokens)
        manifest = json.loads(self.keyring[profile])
        self.assertEqual(manifest["kind"], auth.TOKEN_BUNDLE_KIND)
        self.assertGreater(manifest["chunk_count"], 1)
        self.assertNotIn("a" * 100, self.keyring[profile])
        self.assertEqual(auth._tokens_get(profile), tokens)

    def test_missing_or_corrupt_chunk_fails_closed(self):
        profile = "corrupt-profile"
        auth._tokens_set(profile, {"access_token": "a" * 3_000})
        manifest = json.loads(self.keyring[profile])
        chunk = auth._bundle_part_name(profile, manifest["bundle_id"], 0)
        self.keyring[chunk] = "corrupted"
        with self.assertRaisesRegex(RuntimeError, "keyring_invalid"):
            auth._tokens_get(profile)

    def test_chunk_write_failure_keeps_previous_root_bundle(self):
        profile = "rollback-profile"
        previous = {"access_token": "old", "refresh_token": "old-refresh"}
        auth._tokens_set(profile, previous)
        old_root = self.keyring[profile]
        real_set = auth.keyring.set_password
        calls = []

        def fail_after_first(_service, account, secret):
            calls.append(account)
            if len(calls) == 2:
                raise OSError("synthetic write failure")
            return real_set(_service, account, secret)

        with patch("researchbench.chatgpt_auth.keyring.set_password", side_effect=fail_after_first):
            with self.assertRaisesRegex(RuntimeError, "keyring_unavailable"):
                auth._tokens_set(profile, {"access_token": "new" * 1_000})
        self.assertEqual(self.keyring[profile], old_root)
        self.assertEqual(auth._tokens_get(profile), previous)

    def test_chunked_token_delete_removes_root_and_all_parts(self):
        profile = "delete-profile"
        auth._tokens_set(profile, {"access_token": "a" * 4_000})
        self.assertGreater(len(self.keyring), 1)
        auth._tokens_delete(profile)
        self.assertEqual(self.keyring, {})

    @patch("researchbench.chatgpt_auth._official_configuration", return_value={"authorization_endpoint": auth.AUTHORIZATION_ENDPOINT, "token_endpoint": auth.TOKEN_ENDPOINT, "revocation_endpoint": f"{auth.ISSUER}/oauth/revoke"})
    @patch("researchbench.chatgpt_auth._validate_id_token")
    @patch("researchbench.chatgpt_auth._post_form")
    @patch("researchbench.chatgpt_auth._receive_callback", return_value={"state": "ignored", "code": "code", "client_id": "oaiapp_fixture"})
    @patch("researchbench.chatgpt_auth.webbrowser.open", return_value=True)
    def test_reauth_reuses_client_and_rejects_identity_change(self, browser, callback, post, validate, config):
        profile = self.seed_profile(tokens={"access_token": "old", "refresh_token": "refresh"})
        post.return_value = {"access_token": "new", "refresh_token": "new-refresh", "id_token": "id", "expires_in": 3600,
                             "scope": " ".join(auth.REQUIRED_SCOPES)}
        validate.return_value = {"sub": "different-subject"}
        with self.assertRaisesRegex(RuntimeError, "identity_mismatch"):
            auth.login()
        query = parse_qs(urlsplit(browser.call_args.args[0]).query)
        self.assertEqual(query["client_id"], ["oaiapp_fixture"])
        self.assertNotIn("agent_name_hint", query)
        self.assertEqual(len(self.keyring), 1)
        self.assertIn(profile, self.keyring)

    @patch("researchbench.chatgpt_auth._official_configuration", return_value={"authorization_endpoint": auth.AUTHORIZATION_ENDPOINT, "token_endpoint": auth.TOKEN_ENDPOINT, "revocation_endpoint": f"{auth.ISSUER}/oauth/revoke"})
    @patch("researchbench.chatgpt_auth._validate_id_token", return_value={"sub": "subject"})
    @patch("researchbench.chatgpt_auth._post_form")
    @patch("researchbench.chatgpt_auth._receive_callback", return_value={"state": "ignored", "code": "code", "client_id": "oaiapp_fixture"})
    @patch("researchbench.chatgpt_auth.webbrowser.open", return_value=True)
    def test_cancel_after_exchange_never_persists_tokens(self, browser, callback, post, validate, config):
        cancelled = threading.Event()
        post.side_effect = lambda *_a, **_k: (cancelled.set() or {"access_token": "new", "refresh_token": "new-refresh", "id_token": "id", "expires_in": 3600,
                                                                  "scope": " ".join(auth.REQUIRED_SCOPES)})
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            auth.login(cancelled)
        self.assertEqual(self.keyring, {})

    def test_cancel_after_refresh_never_replaces_token(self):
        profile = self.seed_profile(tokens={"access_token": "old", "refresh_token": "refresh"})
        cancelled = threading.Event()
        response = {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600,
                    "scope": " ".join(auth.REQUIRED_SCOPES)}
        with patch("researchbench.chatgpt_auth._post_form", side_effect=lambda *_a, **_k: (cancelled.set() or response)):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                auth.get_access_token(cancelled)
        self.assertIn("old", self.keyring[profile])

    def test_refresh_rotation_is_serialized(self):
        profile = self.seed_profile(tokens={"access_token": "old", "refresh_token": "refresh"})
        calls, results = [], []

        def refresh(*_args, **_kwargs):
            calls.append(1)
            time.sleep(0.05)
            return {"access_token": "fresh", "refresh_token": "fresh-refresh", "expires_in": 3600,
                    "scope": " ".join(auth.REQUIRED_SCOPES)}

        with patch("researchbench.chatgpt_auth._post_form", side_effect=refresh):
            workers = [threading.Thread(target=lambda: results.append(auth.get_access_token())) for _ in range(2)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(2)
        self.assertEqual(calls, [1])
        self.assertEqual(results, ["fresh", "fresh"])
        self.assertEqual(auth._tokens_get(profile)["refresh_token"], "fresh-refresh")

    def test_logout_clears_local_tokens_when_revocation_is_offline(self):
        profile = self.seed_profile(tokens={"access_token": "old", "refresh_token": "refresh"})
        with patch("researchbench.chatgpt_auth._official_configuration", return_value={"revocation_endpoint": f"{auth.ISSUER}/oauth/revoke"}), \
                patch("researchbench.chatgpt_auth._post_form", side_effect=RuntimeError("offline")):
            status = auth.logout()
        self.assertNotIn(profile, self.keyring)
        self.assertEqual(auth._load_metadata()["active_profile"], profile)
        self.assertFalse(status["connected"])
        self.assertEqual(status["warning"], "chatgpt_revocation_failed")


if __name__ == "__main__":
    unittest.main()
