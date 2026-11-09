from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from researchbench.diary_server import MAX_BODY_BYTES, make_server


class FakeStudy:
    def __init__(self):
        self.submissions = []
        self.ticks = 0
        self.raise_tick = False
        self.synthetic = False
        self.outside_window = False

    def status(self):
        if self.synthetic:
            return {"synthetic_rehearsal": True, "participants": 999,
                    "predictions": ["secret"]}
        return {"state": "active", "participants": 999, "predictions": ["secret"]}

    def submit_diary(self, token, diary):
        if token != "valid-secret":
            raise PermissionError("invalid token")
        self.submissions.append((token, diary))
        return {"receipt_id": "receipt-1", "received_at": "server-time",
                "timing_class": "sensitivity_24h", "primary_revision": True,
                "predictions": {"data_cmi": "must-not-leak"}}

    def tick(self):
        self.ticks += 1
        if self.outside_window:
            raise ValueError("forecast tick is only allowed from 20:00 through 20:15 Asia/Seoul")
        if self.raise_tick:
            raise RuntimeError("private path and participant details must not leak")


class DiaryServerTests(unittest.TestCase):
    def setUp(self):
        self.study = FakeStudy()
        self.server = make_server(self.study, port=0, scheduler_interval=0.03)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, data=None, token=None, origin=True, content_type="application/json"):
        headers = {}
        if origin:
            headers["Origin"] = self.base
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if data is not None:
            headers["Content-Type"] = content_type
            data = json.dumps(data).encode("utf-8") if not isinstance(data, bytes) else data
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()
        with response:
            return response.status, dict(response.headers), response.read()

    @staticmethod
    def valid_diary():
        zone = timezone(timedelta(hours=9))
        wake = datetime.now(zone) - timedelta(hours=1)
        attempt = wake - timedelta(hours=8)
        return {
            "night_date": attempt.date().isoformat(),
            "sleep_attempt": attempt.isoformat(timespec="minutes"),
            "latency_min": 20,
            "final_wake": wake.isoformat(timespec="minutes"),
            "quality": 4,
            "flags": ["교대근무"],
        }

    def test_form_and_health_disclose_no_counts_or_predictions(self):
        status, headers, body = self.request("/", origin=False)
        self.assertEqual(status, 200)
        self.assertIn("아침 수면일지", body.decode("utf-8"))
        self.assertIn("예측 기준 저녁 날짜", body.decode("utf-8"))
        self.assertIn("가상 시험입니다. 실제 연구 자료", body.decode("utf-8"))
        self.assertIn("늦은 제출로 민감도 분석", body.decode("utf-8"))
        self.assertNotIn("localStorage", body.decode("utf-8"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, body = self.request("/health", origin=False)
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["study_state"], "active")
        self.assertIn(health["scheduler_status"], {"waiting", "ok"})
        self.assertNotIn("participants", health)
        self.assertNotIn("predictions", health)

    def test_authenticated_same_origin_submission_returns_safe_receipt(self):
        diary = self.valid_diary()
        status, headers, body = self.request(
            "/api/diary", data=diary, token="valid-secret"
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Cache-Control"], "no-store")
        response = json.loads(body)
        self.assertEqual(response["receipt_id"], "receipt-1")
        self.assertEqual(response["timing_class"], "sensitivity_24h")
        self.assertNotIn("predictions", response)
        self.assertEqual(self.study.submissions, [("valid-secret", diary)])

    def test_token_and_same_origin_are_required_without_leaking_details(self):
        diary = self.valid_diary()
        status, _, body = self.request("/api/diary", data=diary)
        self.assertEqual(status, 401)
        self.assertNotIn(b"valid-secret", body)
        status, _, body = self.request(
            "/api/diary", data=diary, token="valid-secret", origin=False
        )
        self.assertEqual(status, 403)
        status, _, body = self.request(
            "/api/diary", data=diary, token="wrong-secret"
        )
        self.assertEqual(status, 401)
        self.assertNotIn(b"wrong-secret", body)

    def test_rejects_unknown_backdating_fields_future_values_and_large_body(self):
        diary = self.valid_diary()
        diary["observed_at"] = datetime.now(timezone.utc).isoformat()
        status, _, _ = self.request("/api/diary", data=diary, token="valid-secret")
        self.assertEqual(status, 400)
        future = self.valid_diary()
        future["final_wake"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        status, _, _ = self.request("/api/diary", data=future, token="valid-secret")
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "/api/diary", data=b"{" + b"x" * MAX_BODY_BYTES + b"}",
            token="valid-secret"
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.study.submissions, [])

    def test_missing_night_payload_is_minimal_and_scheduler_ticks(self):
        payload = {"night_date": datetime.now().date().isoformat(),
                   "missing_reason": "기억나지 않음", "flags": ["ignored"]}
        status, _, _ = self.request("/api/diary", data=payload, token="valid-secret")
        self.assertEqual(status, 201)
        self.assertEqual(self.study.submissions[0][1], {
            "night_date": payload["night_date"], "missing_reason": "기억나지 않음"
        })
        deadline = time.monotonic() + 1
        while self.study.ticks == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(self.study.ticks, 0)

    def test_non_loopback_plaintext_is_refused(self):
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            make_server(self.study, host="0.0.0.0", port=0)

    def test_outside_capture_window_is_waiting_not_a_server_failure(self):
        self.study.outside_window = True
        start = self.study.ticks
        deadline = time.monotonic() + 1
        while self.study.ticks == start and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertGreater(self.study.ticks, start)
        self.assertEqual(self.server.scheduler_status, "waiting")
        self.assertIsNone(self.server.scheduler_error_type)

    def test_synthetic_and_scheduler_failure_are_visible_without_private_detail(self):
        self.study.synthetic = True
        self.study.raise_tick = True
        deadline = time.monotonic() + 1
        while self.server.scheduler_status != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.scheduler_status, "error")
        status, _, body = self.request("/health", origin=False)
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["study_state"], "synthetic")
        self.assertEqual(health["scheduler_status"], "error")
        self.assertEqual(health["scheduler_error_type"], "RuntimeError")
        self.assertNotIn("private path", body.decode("utf-8"))

    def test_quality_range_and_tls_initialization_failure_closes_bound_socket(self):
        diary = self.valid_diary()
        diary["quality"] = 6
        status, _, _ = self.request("/api/diary", data=diary, token="valid-secret")
        self.assertEqual(status, 400)
        diary = self.valid_diary()
        diary["latency_min"] = 481
        status, _, _ = self.request("/api/diary", data=diary, token="valid-secret")
        self.assertEqual(status, 400)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(OSError):
            make_server(self.study, port=port, certfile="missing-cert.pem",
                        keyfile="missing-key.pem")
        rebound = socket.socket()
        try:
            rebound.bind(("127.0.0.1", port))
        finally:
            rebound.close()


if __name__ == "__main__":
    unittest.main()
