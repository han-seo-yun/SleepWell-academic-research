"""Exercise the actual bundled interpreter/server with disposable local data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PACKAGE = Path(__file__).resolve().parent.parent / "package/SleepWell"
PYTHON = PACKAGE / "runtime/python.exe"
HOST = PACKAGE / "app/desktop_host.py"


class DesktopHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="SleepWell 한글 공백 ")
        self.directory = Path(self.temporary.name)
        self.ready = self.directory / "desktop-ready.json"
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                try:
                    child.stdin.write("shutdown\n")
                    child.stdin.flush()
                    child.wait(timeout=10)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    child.terminate()
                    child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream and not stream.closed:
                    stream.close()
        self.temporary.cleanup()

    def spawn(self, wait=True, ready=None):
        process = subprocess.Popen(
            [str(PYTHON), "-B", "-X", "utf8", str(HOST), "--data-dir",
             str(self.directory), "--ready-file", str(ready or self.ready)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(process)
        if not wait:
            return process
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("Host exited before readiness: " + process.stderr.read())
            if self.ready.exists():
                metadata = json.loads(self.ready.read_text(encoding="utf-8"))
                if metadata["pid"] == process.pid:
                    self.base = metadata["url"]
                    self.metadata = metadata
                    return process
            time.sleep(0.05)
        self.fail("Host did not become ready")

    def get(self, path, headers=None):
        with urlopen(Request(self.base + path.lstrip("/"), headers=headers or {}), timeout=5) as response:
            return json.load(response)

    def post(self, path, value, token):
        request = Request(self.base + path.lstrip("/"),
                          data=json.dumps(value).encode(),
                          headers={"Content-Type": "application/json",
                                   "Origin": self.base.rstrip("/"),
                                   "X-SleepWell-Token": token})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def stop(self, child, eof=False):
        if eof:
            child.stdin.close()
        else:
            child.stdin.write("shutdown\n")
            child.stdin.flush()
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertFalse(self.ready.exists())

    def test_runtime_ignores_installed_python_and_site_packages(self):
        environment = dict(os.environ, PYTHONPATH="C:/Python312/Lib/site-packages",
                           PYTHONHOME="Z:/not-a-python-install")
        result = subprocess.run([str(PYTHON), "-B", "-X", "utf8", "-c",
                                 "import sys,importlib.util,sqlite3,zoneinfo;"
                                 "assert sys.version_info[:2]==(3,14);"
                                 "assert importlib.util.find_spec('pandas') is None;"
                                 "print(zoneinfo.ZoneInfo('Asia/Seoul'))"],
                                env=environment, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Asia/Seoul", result.stdout)

    def test_fresh_data_health_and_request_boundaries(self):
        child = self.spawn()
        health = self.get("/api/desktop-health")
        self.assertEqual(health["pid"], child.pid)
        self.assertEqual(health["instance_id"], self.metadata["instance_id"])
        state = self.get("/api/state")
        self.assertIsNone(state["profile"])
        self.assertEqual(state["records"], [])
        self.assertFalse(state["research"]["available"])
        self.assertEqual(state["prediction_count"], 0)
        self.assertEqual(Path(state["storage"]), self.directory / "sleepwell.sqlite3")
        with self.assertRaises(HTTPError) as caught:
            self.get("/api/desktop-health", {"Host": "untrusted.example"})
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/profile", {}, "incorrect-token")
        self.assertEqual(caught.exception.code, 403)
        self.stop(child)

    def test_profile_context_persist_across_restart_and_export(self):
        child = self.spawn()
        token = self.get("/api/state")["csrf_token"]
        state = self.post("/api/profile", {
            "name": "패키지 검증용", "age": 25, "sex": "unspecified", "occupation": "student",
            "usual_bed": "23:30", "usual_wake": "07:00",
            "target_wake": "07:00", "weekend_wake": "08:00",
            "usual_sleep_h": 7, "desired_sleep_h": 8, "average_steps": 6000,
            "exercise_min": 20, "latency_min": 15, "link_history": False}, token)
        state = self.post("/api/context", {
            "night_id": state["today"], "preparation_min": 45,
            "schedules": [], "naps": [], "caffeine": []}, token)
        self.assertEqual(state["daily_context"]["preparation_min"], 45)
        self.assertTrue(state["personalization"]["population_prior"]["available"])
        exported = self.get("/api/export")
        self.assertNotIn("csrf_token", exported)
        self.assertEqual(len(exported["daily_context_versions"]), 1)
        self.stop(child)
        restarted = self.spawn()
        state = self.get("/api/state")
        self.assertEqual(state["profile"]["name"], "패키지 검증용")
        self.assertEqual(state["daily_context"]["preparation_min"], 45)
        self.assertNotEqual(self.metadata["pid"], child.pid)
        self.stop(restarted)

    def test_second_host_cannot_open_same_database(self):
        first = self.spawn()
        original = self.ready.read_bytes()
        second = self.spawn(wait=False)
        self.assertEqual(second.wait(timeout=10), 1)
        self.assertEqual(self.ready.read_bytes(), original)
        self.assertEqual(self.get("/api/desktop-health")["pid"], first.pid)
        self.stop(first)

    def test_parent_pipe_eof_stops_owned_host(self):
        child = self.spawn()
        self.stop(child, eof=True)

    def test_ready_path_cannot_escape_data_directory(self):
        child = self.spawn(wait=False, ready=self.directory.parent / "not-owned-ready.json")
        self.assertEqual(child.wait(timeout=10), 1)
        self.assertFalse((self.directory / "sleepwell.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
