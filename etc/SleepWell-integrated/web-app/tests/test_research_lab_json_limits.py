"""Regression tests for bounded JSON inputs rejected before model evaluation."""
from email.message import Message
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_lab_server import ResearchLabHandler, RequestJSONError, make_server


PAYLOADS = (
    b'{"n":' + b"9" * 5000 + b"}",
    b'{"n":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
)


class JsonBoundaryTests(unittest.TestCase):
    def test_pathological_json_is_normalized(self):
        for payload in PAYLOADS:
            with self.subTest(length=len(payload)):
                handler = object.__new__(ResearchLabHandler)
                handler.headers = Message()
                handler.headers["Content-Length"] = str(len(payload))
                handler.headers["Content-Type"] = "application/json"
                handler.rfile = BytesIO(payload)
                with self.assertRaises(RequestJSONError):
                    handler._read_json_body()

    def test_pathological_json_returns_http_400(self):
        server = make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for payload in PAYLOADS:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                try:
                    connection.request("POST", "/api/lab/evaluate", body=payload, headers={
                        "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{server.server_port}",
                        "X-SleepWell-Token": server.csrf_token,
                    })
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 400)
                    self.assertIsInstance(result["error"], str)
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
