import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import ZONE
from research_lab_server import make_server
from server import Application, Handler, Store


class Research:
    summary = {"available": False}
    records = []
    metrics = None

    def behavioral_forecast(self, *args):
        return {"available": False}


PROFILE = {"name": "TEST", "age": 20, "occupation": "student", "usual_bed": "23:30", "usual_wake": "07:00",
           "target_wake": "07:00", "weekend_wake": "08:00", "usual_sleep_h": 7, "desired_sleep_h": 8,
           "average_steps": 6000, "exercise_min": 30, "latency_min": 20, "link_history": False}


class ResearchAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = [datetime(2026, 10, 1, 21, 0, tzinfo=ZONE)]
        self.app = Application(Store(Path(self.directory.name) / "db.sqlite3"), Research(), clock=lambda: self.clock[0])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.application = self.app
        self.server.app_version = "test"
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.root = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.directory.cleanup()

    def get(self, path):
        with urlopen(self.root + path) as response:
            return response.status, dict(response.headers), response.read()

    def post(self, path, data):
        request = Request(self.root + path, data=json.dumps(data).encode(),
                          headers={"Content-Type": "application/json", "X-SleepWell-Token": self.app.token})
        with urlopen(request) as response:
            return json.load(response)

    def enroll_with_diary(self, nights):
        self.post("/api/profile", PROFILE)
        for night in range(nights):
            # Enter each night the morning after, so it is available before the next 20:00.
            self.clock[0] = datetime(2026, 10, 2 + night, 8, 0, tzinfo=ZONE)
            bed = datetime(2026, 10, 1 + night, 23, 30, tzinfo=ZONE)
            self.post("/api/record", {"bed": bed.isoformat(), "wake": (bed + timedelta(hours=7, minutes=30)).isoformat(),
                                      "latency_min": 10, "awake_min": 0})
        self.clock[0] = datetime(2026, 10, 2 + nights, 9, 0, tzinfo=ZONE)

    def test_index_openapi_and_read_only_surface(self):
        status, headers, body = self.get("/api/v1")
        index = json.loads(body)
        self.assertEqual(index["api_version"], "v1")
        self.assertTrue(index["read_only"])
        spec = json.loads(self.get("/api/v1/openapi.json")[2])
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/api/v1/participants/{participant_id}/records.csv", spec["paths"])
        self.assertTrue(all(set(item) == {"get"} for item in spec["paths"].values()))
        with self.assertRaises(HTTPError) as error:
            self.get("/api/v1/unknown")
        self.assertEqual(error.exception.code, 404)

    def test_participant_summary_detail_and_csv_match_lab_rows(self):
        self.enroll_with_diary(10)
        participants = json.loads(self.get("/api/v1/participants")[2])["participants"]
        self.assertEqual([item["id"] for item in participants], ["pc"])
        pc = participants[0]
        self.assertEqual(pc["nights"], 10)
        self.assertEqual(len(pc["recent_strip"]), 14)
        self.assertEqual(sum(cell["status"] == "timely" for cell in pc["recent_strip"]), 10)
        self.assertEqual(pc["alerts"], [])
        self.assertIsNotNone(pc["sri"])

        detail = json.loads(self.get("/api/v1/participants/pc")[2])
        self.assertEqual(detail["completeness"]["timely_nights"], 10)
        self.assertEqual(len(detail["nights"]), 10)
        self.assertTrue(all(night["timely"] for night in detail["nights"]))

        status, headers, body = self.get("/api/v1/participants/pc/records.csv")
        self.assertTrue(headers["Content-Type"].startswith("text/csv"))
        self.assertIn('filename="sleepwell-pc.csv"', headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 10)
        self.assertEqual(list(rows[0]), ["participant_id", "cutoff", "sleep_start", "sleep_end", "observed_at"])

    def test_alerts_for_stale_records_and_invalid_ids(self):
        self.enroll_with_diary(3)
        self.clock[0] = datetime(2026, 10, 20, 9, 0, tzinfo=ZONE)
        alerts = {alert["code"] for alert in json.loads(self.get("/api/v1/participants")[2])["participants"][0]["alerts"]}
        self.assertIn("stale_records", alerts)
        self.assertIn("low_coverage", alerts)
        overview = json.loads(self.get("/api/v1/overview")[2])
        self.assertEqual(overview["participants"]["total"], 1)
        self.assertTrue(any(alert["code"] == "stale_records" for alert in overview["alerts"]))
        for bad, code in (("/api/v1/participants/PC", 404), ("/api/v1/participants/phone-0000000000000000", 404),
                          ("/api/v1/participants/phone-zz", 400)):
            with self.subTest(path=bad):
                with self.assertRaises(HTTPError) as error:
                    self.get(bad)
                self.assertEqual(error.exception.code, code)

    def test_frame_src_only_when_desktop_host_configures_it(self):
        headers = self.get("/")[1]
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-src 'self';", headers["Content-Security-Policy"])
        self.server.frame_sources = ("http://127.0.0.1:5555",)
        headers = self.get("/")[1]
        self.assertIn("frame-src 'self' http://127.0.0.1:5555;", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.get("/shell-views.js")[0], 200)
        self.assertEqual(self.get("/tokens.css")[0], 200)


class LabFramingTests(unittest.TestCase):
    def test_lab_denies_framing_unless_a_loopback_shell_is_configured(self):
        server = make_server("127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        root = f"http://127.0.0.1:{server.server_port}/"
        try:
            with urlopen(root) as response:
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            server.frame_ancestors = ("http://127.0.0.1:4444", "https://evil.example")
            with urlopen(root) as response:
                self.assertIsNone(response.headers["X-Frame-Options"])
                policy = response.headers["Content-Security-Policy"]
                self.assertIn("frame-ancestors http://127.0.0.1:4444", policy)
                self.assertNotIn("evil", policy)
            with urlopen(root + "tokens.css") as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
