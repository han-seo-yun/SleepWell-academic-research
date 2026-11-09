from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import research_lab_server
from research_lab_server import MAX_BODY_BYTES, make_server


KST = timezone(timedelta(hours=9))


def synthetic_payload() -> dict:
    return {
        "schema_version": 1,
        "mode": "synthetic",
        "cohorts": ["cmi"],
        "synthetic": {
            "n_nights": 10,
            "seed": 42,
            "start_date": "2026-01-01",
            "bed_hour": 23.5,
            "duration_hours": 8,
            "jitter_minutes": 45,
            "phase_shift_minutes": 60,
            "shift_after_night": 5,
            "missing_every": 0,
            "report_delay_hours": 1,
        },
    }


def real_payload() -> dict:
    records = []
    first_day = date(2026, 1, 1)
    for index in range(10):
        cutoff = datetime.combine(first_day + timedelta(days=index), time(20), KST)
        sleep_start = cutoff + timedelta(hours=3, minutes=(index % 3) * 10)
        sleep_end = sleep_start + timedelta(hours=7, minutes=50 + (index % 2) * 20)
        records.append(
            {
                "participant_id": "P001",
                "cutoff": cutoff.isoformat(),
                "sleep_start": sleep_start.isoformat(),
                "sleep_end": sleep_end.isoformat(),
                "observed_at": (sleep_end + timedelta(hours=1)).isoformat(),
            }
        )
    return {
        "schema_version": 1,
        "mode": "real",
        "cohorts": ["cmi"],
        "records": records,
    }


class RunningServerMixin:
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.server = make_server("127.0.0.1", 0)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        status, _headers, session, _raw = cls.request("GET", "/api/lab/session")
        if status != 200:
            raise AssertionError(f"session endpoint failed with HTTP {status}: {session}")
        cls.token = session["csrf_token"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.worker.join(timeout=5)
        cls.server.server_close()
        super().tearDownClass()

    @classmethod
    def request(
        cls,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: list[tuple[str, str]] | None = None,
        host: str | None = None,
        declared_length: int | None = None,
        timeout: float = 120,
    ) -> tuple[int, dict[str, str], object, bytes]:
        connection = HTTPConnection("127.0.0.1", cls.server.server_port, timeout=timeout)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{cls.server.server_port}")
        supplied = {name.lower() for name, _value in (headers or [])}
        for name, value in headers or []:
            connection.putheader(name, value)
        if declared_length is not None:
            connection.putheader("Content-Length", str(declared_length))
        elif body is not None and "content-length" not in supplied:
            connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Connection", "close")
        connection.endheaders(body)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        if response_headers.get("content-type", "").startswith("application/json"):
            parsed = json.loads(raw.decode("utf-8"))
        else:
            parsed = raw
        return status, response_headers, parsed, raw

    @classmethod
    def post_json(
        cls,
        payload: dict,
        *,
        token: str | None = None,
        origin: str | None = None,
        host: str | None = None,
    ) -> tuple[int, dict[str, str], object, bytes]:
        authority = host or f"127.0.0.1:{cls.server.server_port}"
        request_headers = [
            ("Content-Type", "application/json"),
            ("Origin", origin or f"http://{authority}"),
        ]
        if token is not None:
            request_headers.append(("X-SleepWell-Token", token))
        return cls.request(
            "POST",
            "/api/lab/evaluate",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            host=host,
        )


class ResearchLabServerSecurityTests(RunningServerMixin, unittest.TestCase):
    def test_publication_download_is_real_zip_with_matching_run_and_manifest(self):
        import hashlib
        from io import BytesIO
        import zipfile
        from research_lab import evaluate_run
        payload = synthetic_payload()
        headers = [("Content-Type", "application/json"),
                   ("Origin", f"http://127.0.0.1:{self.server.server_port}"),
                   ("X-SleepWell-Token", self.token)]
        status, response_headers, _, raw = self.request(
            "POST", "/api/lab/export", body=json.dumps(payload).encode(), headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["content-type"], "application/zip")
        self.assertIn("attachment", response_headers["content-disposition"])
        self.assertEqual(response_headers["cache-control"], "no-store")
        with zipfile.ZipFile(BytesIO(raw)) as bundle:
            manifest = json.loads(bundle.read("figure-manifest.json"))
            exact = json.loads(bundle.read("result.json"))
            self.assertEqual(exact["run_id"], evaluate_run(payload)["run_id"])
            self.assertEqual(manifest["run_id"], exact["run_id"])
            self.assertEqual(manifest["mode"], "synthetic")
            for item in manifest["files"]:
                self.assertEqual(hashlib.sha256(bundle.read(item["path"])).hexdigest(), item["sha256"])
            self.assertTrue(any(name.endswith(".svg") for name in bundle.namelist()))
            self.assertTrue(any(name.endswith(".pdf") for name in bundle.namelist()))

    def test_home_navigation_survives_session_failure_and_rejects_external_target(self):
        from unittest.mock import Mock
        bridge = Mock()
        self.server.app_bridge = bridge
        try:
            bridge.app_url = "http://127.0.0.1:12345/"
            status, headers, _, _ = self.request("GET", "/app")
            self.assertEqual(status, 303)
            self.assertEqual(headers["location"], "http://127.0.0.1:12345/")
            for target in ("https://example.com/", "http://127.0.0.1:12345/?redirect=evil",
                           "http://secret@localhost:12345/", "http://localhost:invalid/"):
                bridge.app_url = target
                status, headers, _, body = self.request("GET", "/app")
                self.assertEqual(status, 200)
                self.assertNotIn("location", headers)
                self.assertIn("독립 실행", body.decode("utf-8"))
            bridge.session.side_effect = ValueError("broken session")
            bridge.app_url = "http://127.0.0.1:12345/"
            self.assertEqual(self.request("GET", "/app")[0], 303)
            bridge.session.assert_not_called()
        finally:
            self.server.app_bridge = None
        self.assertEqual(self.request("GET", "/app", host="example.com")[0], 403)

    def test_export_uses_same_security_and_input_validation(self):
        path = "/api/lab/export"
        headers = [("Content-Type", "application/json"),
                   ("Origin", f"http://127.0.0.1:{self.server.server_port}")]
        self.assertEqual(self.request("POST", path, body=b"{}", headers=headers)[0], 403)
        headers.append(("X-SleepWell-Token", self.token))
        self.assertEqual(self.request("POST", path, body=b"{}", headers=headers)[0], 400)
        self.assertEqual(self.request("POST", path, body=b'{"models_dir":"outside"}', headers=headers)[0], 400)
        self.server.evaluation_gate.acquire()
        try:
            status, response_headers, _, _ = self.request("POST", path, body=b"{}", headers=headers)
            self.assertEqual(status, 429)
            self.assertEqual(response_headers["retry-after"], "1")
        finally:
            self.server.evaluation_gate.release()

    def test_import_requires_same_origin_token_and_connected_bridge(self):
        from unittest.mock import Mock
        path = '/api/lab/import'
        headers = [('Content-Type', 'application/json'), ('Origin', f'http://127.0.0.1:{self.server.server_port}')]
        self.assertEqual(self.request('POST', path, body=b'{}', headers=headers)[0], 403)
        headers.append(('X-SleepWell-Token', self.token))
        self.assertEqual(self.request('POST', path, body=b'{}', headers=headers)[0], 409)
        bridge = Mock()
        bridge.snapshot.return_value = {'payload': {'records': [], 'context': []}}
        self.server.app_bridge = bridge
        try:
            status, _, body, _ = self.request('POST', path, body=b'{"source":"pc"}', headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(body['payload']['records'], [])
            bridge.snapshot.assert_called_once_with('pc')
            self.assertEqual(self.request('POST', path, body=b'{"path":"outside"}', headers=headers)[0], 400)
        finally:
            self.server.app_bridge = None

    def test_make_server_rejects_non_loopback_host_and_invalid_port(self) -> None:
        with self.assertRaises(ValueError):
            make_server("0.0.0.0", 0)
        with self.assertRaises(ValueError):
            make_server("127.0.0.1", -1)
        with self.assertRaises(ValueError):
            make_server("127.0.0.1", True)

    def test_session_and_only_allowlisted_static_routes(self) -> None:
        status, headers, session, _raw = self.request("GET", "/api/lab/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["schema_version"], 1)
        self.assertEqual(session["limits"]["max_body_bytes"], MAX_BODY_BYTES)
        self.assertTrue(session["limits"]["one_evaluation_at_a_time"])
        self.assertIn("model_status", session)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("default-src 'none'", headers["content-security-policy"])

        expected = {
            "/": "text/html",
            "/research-lab.js": "text/javascript",
            "/research-lab.css": "text/css",
        }
        for route, content_type in expected.items():
            with self.subTest(route=route):
                status, headers, _parsed, raw = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith(content_type))
                self.assertTrue(raw)
        status, _headers, _parsed, root = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("SleepWell 연구 분석실", root.decode("utf-8"))

        for route in (
            "/server.py",
            "/research_lab_server.py",
            "/../research_lab_server.py",
            "/%2e%2e/research_lab_server.py",
            "/static/research-lab.html",
            "/C:/Windows/win.ini",
            "/research-lab.js?cache=1",
            "//example.invalid/",
        ):
            with self.subTest(route=route):
                status, _headers, body, _raw = self.request("GET", route)
                self.assertEqual(status, 404)
                self.assertEqual(body, {"error": "Not found"})

    def test_missing_or_wrong_token_and_hostile_host_or_origin_are_rejected(self) -> None:
        valid_origin = f"http://127.0.0.1:{self.server.server_port}"
        status, _headers, _body, _raw = self.post_json({}, token=None)
        self.assertEqual(status, 403)
        status, _headers, _body, _raw = self.post_json({}, token="wrong-token")
        self.assertEqual(status, 403)

        body = b"{}"
        status, _headers, _body, _raw = self.request(
            "POST",
            "/api/lab/evaluate",
            body=body,
            headers=[
                ("Content-Type", "application/json"),
                ("X-SleepWell-Token", self.token),
            ],
        )
        self.assertEqual(status, 403, "Origin is mandatory for the browser POST")

        status, _headers, _body, _raw = self.post_json(
            {}, token=self.token, origin="http://example.invalid"
        )
        self.assertEqual(status, 403)
        status, _headers, _body, _raw = self.post_json(
            {},
            token=self.token,
            host="example.invalid",
            origin="http://example.invalid",
        )
        self.assertEqual(status, 403)
        status, _headers, _body, _raw = self.request(
            "GET", "/api/lab/session", host="example.invalid"
        )
        self.assertEqual(status, 403)
        self.assertNotEqual(valid_origin, "http://example.invalid")

    def test_body_limit_duplicate_keys_nonfinite_and_content_type(self) -> None:
        authority = f"127.0.0.1:{self.server.server_port}"
        auth_headers = [
            ("Content-Type", "application/json"),
            ("Origin", f"http://{authority}"),
            ("X-SleepWell-Token", self.token),
        ]
        status, _headers, _body, _raw = self.request(
            "POST",
            "/api/lab/evaluate",
            headers=auth_headers,
            declared_length=MAX_BODY_BYTES + 1,
        )
        self.assertEqual(status, 413)

        duplicate = b'{"schema_version":1,"mode":"synthetic","mode":"real"}'
        status, _headers, body, _raw = self.request(
            "POST", "/api/lab/evaluate", body=duplicate, headers=auth_headers
        )
        self.assertEqual(status, 400)
        self.assertIn("중복", body["error"])

        nonfinite = b'{"schema_version":1,"mode":"synthetic","value":NaN}'
        status, _headers, body, _raw = self.request(
            "POST", "/api/lab/evaluate", body=nonfinite, headers=auth_headers
        )
        self.assertEqual(status, 400)
        self.assertIn("유한", body["error"])

        status, _headers, _body, _raw = self.request(
            "POST",
            "/api/lab/evaluate",
            body=b"{}",
            headers=[
                ("Content-Type", "text/plain"),
                ("Origin", f"http://{authority}"),
                ("X-SleepWell-Token", self.token),
            ],
        )
        self.assertEqual(status, 415)

    def test_models_directory_cannot_be_selected_by_request(self) -> None:
        status, _headers, body, _raw = self.post_json(
            {"models_dir": "C:/outside/models"}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertIn("CLI", body["error"])

    def test_only_one_evaluation_executes_at_a_time(self) -> None:
        started = threading.Event()
        release = threading.Event()
        first_response = []

        def slow_evaluate(_payload, models_dir=None):
            self.assertIsNone(models_dir)
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test release timed out")
            return {"schema_version": 1, "mode": "synthetic"}

        def first_request() -> None:
            first_response.append(self.post_json({}, token=self.token)[0])

        with patch.object(research_lab_server, "evaluate_run", side_effect=slow_evaluate), patch.object(
            research_lab_server, "render_charts", return_value=[]
        ):
            worker = threading.Thread(target=first_request, daemon=True)
            worker.start()
            self.assertTrue(started.wait(timeout=5))
            try:
                status, headers, body, _raw = self.post_json({}, token=self.token)
                self.assertEqual(status, 429)
                self.assertEqual(headers["retry-after"], "1")
                self.assertIn("진행 중", body["error"])
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertEqual(first_response, [200])


class ResearchLabServerIntegrationTests(RunningServerMixin, unittest.TestCase):
    def assert_chart_contract(self, result: dict) -> None:
        self.assertEqual(
            [chart["id"] for chart in result["charts"]],
            ["prediction-timeline", "model-mae-comparison", "personalization-traces",
             "participant-mae-heatmap", "joint-error-distribution", "signed-residual-bias"],
        )
        for chart in result["charts"]:
            self.assertTrue(chart["data_uri"].startswith("data:image/png;base64,iVBORw0KGgo"))
            self.assertTrue(chart["title"])
            self.assertTrue(chart["alt"])

    def test_synthetic_evaluation_uses_real_models_and_embeds_png_charts(self) -> None:
        status, headers, result, raw = self.post_json(synthetic_payload(), token=self.token)
        self.assertEqual(status, 200, result)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["mode"], "synthetic")
        self.assertEqual(result["summary"]["rows"], 10)
        self.assertGreater(len(result["models"]), 0)
        self.assertGreater(len(result["predictions"]), 0)
        self.assertTrue(result["evidence_label"])
        self.assert_chart_contract(result)
        json.loads(raw.decode("utf-8"), parse_constant=lambda value: self.fail(value))

    def test_real_evaluation_exposes_metrics_and_actual_personalization_state(self) -> None:
        status, _headers, result, raw = self.post_json(real_payload(), token=self.token)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["mode"], "real")
        self.assertEqual(result["summary"]["participants"], ["P001"])
        self.assertEqual(result["summary"]["rows"], 10)
        self.assertTrue(result["metrics"])
        self.assertTrue(all(metric["averaging"] == "participant_equal" for metric in result["metrics"]))
        self.assertTrue(result["personalization"])
        snapshot = result["personalization"][-1]
        self.assertIn("history_count", snapshot)
        self.assertTrue(
            {
                "personal_weight",
                "phase_min",
                "sleep_threshold",
                "wake_threshold",
                "tau_wake_h",
                "tau_sleep_h",
            }.issubset(snapshot["state"])
        )
        self.assertTrue({"current_s", "current_c"}.issubset(snapshot["features"]))
        self.assert_chart_contract(result)
        json.loads(raw.decode("utf-8"), parse_constant=lambda value: self.fail(value))

    def test_prediction_only_real_rows_do_not_fabricate_metrics(self) -> None:
        payload = real_payload()
        for record in payload["records"]:
            record["sleep_start"] = None
            record["sleep_end"] = None
            record["observed_at"] = None
        status, _headers, result, _raw = self.post_json(payload, token=self.token)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["summary"]["labeled_nights"], 0)
        for metric in result["available_metrics"]:
            self.assertEqual(metric["n"], 0)
            for name in (
                "onset_mae_min",
                "wake_mae_min",
                "interval_mae_min",
                "joint_mae_min",
                "onset_rmse_min",
                "wake_rmse_min",
                "skill_vs_recent7",
            ):
                self.assertIsNone(metric[name])
        self.assert_chart_contract(result)


if __name__ == "__main__":
    unittest.main()
