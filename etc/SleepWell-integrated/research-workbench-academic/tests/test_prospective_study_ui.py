import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from urllib.request import urlopen

from researchbench.academic_app import AcademicApp
from researchbench.academic_store import AcademicProject


class ProspectiveStudyUITests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.project = AcademicProject.create(Path(self.temp.name) / "test.swa", "Diary test")
        self.app = AcademicApp(self.root, self.project, language="ko", preferences=False)
        self.app.navigate("prospective")

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_create_draft_and_serve_form_without_enabling_recruitment(self):
        self.app._create_diary_study()
        service = self.app._study_service()
        self.assertFalse(service.status()["ready_for_enrollment"])
        with self.assertRaisesRegex(ValueError, "governance"):
            service.enroll({"consent_confirmed": True})
        self.app.var("study_port").set("0")
        self.app._start_diary_server()
        port = self.app._study_http_server.server_port
        with urlopen(f"http://127.0.0.1:{port}/health") as response:
            self.assertEqual(json.load(response)["study_state"], "preparation")
        self.app.stop_diary_server()
        self.assertIsNone(self.app._study_http_server)

    def test_pilot_is_separate_and_enrollment_input_not_saved(self):
        self.app._create_diary_study(True)
        status = self.app._study_service().status()
        self.assertEqual(status["protocol"]["phase"], "feasibility")
        self.assertEqual(status["protocol"]["duration_days"], 14)
        self.app.study_enrollment_editor.delete("1.0", "end")
        self.app.study_enrollment_editor.insert("1.0", '{"private_marker": "DO_NOT_SAVE"}')
        self.app.save()
        saved = (self.project.path / "project.json").read_text(encoding="utf-8")
        self.assertNotIn("DO_NOT_SAVE", saved)
        self.app.change_language("en")
        self.assertEqual(self.app._study_service().status()["protocol"]["phase"], "feasibility")

    def test_project_switch_stops_server_and_clears_study(self):
        self.app._create_diary_study()
        self.app.var("study_port").set("0")
        self.app._start_diary_server()
        other = AcademicProject.create(Path(self.temp.name) / "other.swa", "Other")
        self.app.attach(other)
        self.assertIsNone(self.app._study_http_server)
        self.assertEqual(self.app.var("study_root").get(), "")


if __name__ == "__main__":
    unittest.main()
