"""Exercise the real TLS boundary using synthetic data on loopback only."""
import http.client
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "web-app"))
sys.path.insert(0, str(HERE))
from server import Application, Store
from mobile_sync import MobileSync, private_ipv4


class Research:
    records = []
    summary = {}
    metrics = None


class MobileTLSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="SleepWell-mobile-test-")
        self.directory = Path(self.temp.name)
        self.app = Application(Store(self.directory / "test.sqlite3"), Research())
        self.mobile = MobileSync(self.app, self.directory, HERE / "bin/SyncCertificate.exe")
        self.ip = patch("mobile_sync.local_addresses", return_value=["127.0.0.1"])
        self.private = patch("mobile_sync.private_ipv4", return_value=True)
        self.ip.start()
        self.private.start()
        self.start = self.mobile.start("127.0.0.1")
        self.code = parse_qs(urlparse(self.start["pairing_uri"]).query)
        self.context = ssl.create_default_context(cafile=str(self.mobile.certificate_file))
        self.context.check_hostname = False

    def tearDown(self):
        self.mobile.stop()
        self.private.stop()
        self.ip.stop()
        self.temp.cleanup()

    def request(self, path, value, token=None, extra=None, context=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.mobile.server.server_port,
                                                context=context or self.context, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        headers.update(extra or {})
        try:
            connection.request("POST", path, json.dumps(value), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def pair(self):
        status, value = self.request("/v1/pair", {
            "secret": self.code["secret"][0], "device_name": "Synthetic phone"})
        self.assertEqual(status, 200)
        return value

    def test_tls_rejects_untrusted_certificate(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.request("/v1/pair", {}, context=ssl.create_default_context())

    def test_pair_single_use_token_not_exported_and_revoke(self):
        first = self.pair()
        code, _ = self.request("/v1/pair", {"secret": self.code["secret"][0]})
        self.assertEqual(code, 403)
        code, _ = self.request("/v1/sync", {"schema_version": 1, "sessions": []},
                               first["device_token"])
        self.assertEqual(code, 200)
        status = json.dumps(self.mobile.status())
        self.assertNotIn(first["device_token"], status)
        self.assertNotIn(self.code["secret"][0], status)
        with self.app.store.connect() as db:
            raw = str(db.execute("SELECT * FROM mobile_devices").fetchall())
        self.assertNotIn(first["device_token"], raw)
        self.mobile.revoke()
        code, _ = self.request("/v1/sync", {"schema_version": 1, "sessions": []},
                               first["device_token"])
        self.assertEqual(code, 401)

    def test_origin_auth_and_body_limits(self):
        paired = self.pair()
        payload = {"schema_version": 1, "sessions": []}
        self.assertEqual(self.request("/v1/sync", payload)[0], 401)
        self.assertEqual(self.request("/v1/sync", payload, "wrong" * 9)[0], 401)
        self.assertEqual(self.request("/v1/sync", payload, paired["device_token"],
                                      {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/v1/sync", payload, paired["device_token"],
                                      {"Content-Length": "1000001"})[0], 413)
        self.assertEqual(self.request("/v1/sync", [], paired["device_token"])[0], 400)

    def test_pair_expiry_and_failed_attempt_limit(self):
        self.mobile.deadline = 0
        self.assertEqual(self.request("/v1/pair", {"secret": self.code["secret"][0]})[0], 403)
        self.mobile.start("127.0.0.1")
        for _ in range(5):
            self.assertEqual(self.request("/v1/pair", {"secret": "wrong"})[0], 403)
        self.assertIsNone(self.mobile.secret)

    def test_restart_preserves_port_and_repair_identity(self):
        first = self.pair()
        port = self.mobile.server.server_port
        self.mobile.stop()
        self.assertFalse(self.mobile.status()["enabled"])
        reopened = self.mobile.start("127.0.0.1")
        self.assertEqual(reopened["port"], port)
        self.assertEqual(self.mobile.authenticate("Bearer " + first["device_token"]), first["device_id"])
        self.code = parse_qs(urlparse(reopened["pairing_uri"]).query)
        second = self.pair()
        self.assertEqual(first["device_id"], second["device_id"])
        self.assertIsNone(self.mobile.authenticate("Bearer " + first["device_token"]))

    def test_partial_certificate_repaired_and_old_device_revoked(self):
        first = self.pair()
        old = self.mobile.certificate_file
        self.mobile.stop()
        old.unlink()
        restarted = self.mobile.start("127.0.0.1")
        self.assertNotEqual(self.mobile.certificate_file, old)
        self.assertIsNone(self.mobile.authenticate("Bearer " + first["device_token"]))
        self.assertIsNotNone(restarted["notice"])

    def test_unexpected_sync_error_has_sanitized_status(self):
        paired = self.pair()
        with patch("mobile_sync.ingest", side_effect=RuntimeError("SECRET-HEALTH-PAYLOAD")):
            self.assertEqual(self.request("/v1/sync", {"schema_version": 1, "sessions": []},
                                          paired["device_token"])[0], 500)
        status = self.mobile.status()
        self.assertEqual(status["last_error"]["type"], "RuntimeError")
        self.assertNotIn("SECRET-HEALTH-PAYLOAD", json.dumps(status))


class AddressTests(unittest.TestCase):
    def test_only_rfc1918_ipv4(self):
        for address in ("10.0.0.1", "172.16.2.3", "192.168.1.4"):
            self.assertTrue(private_ipv4(address))
        for address in ("127.0.0.1", "169.254.1.2", "::1", "8.8.8.8", "0.0.0.0",
                        "192.0.0.1", "224.0.0.1", "localhost", None):
            self.assertFalse(private_ipv4(address))


if __name__ == "__main__":
    unittest.main()
