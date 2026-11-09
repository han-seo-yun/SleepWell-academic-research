import json
import tempfile
import unittest
from pathlib import Path

from research import sanitize_archive_latency
from server import Application, Store


class ResearchStub:
    def __init__(self):
        self.summary = {"available": False, "warnings": []}
        self.records = []
        self.metrics = None


class ArchiveLatencyTests(unittest.TestCase):
    def test_archive_latency_accepts_only_finite_verified_minutes(self):
        for value in (0, 20, 180, "17.5"):
            self.assertEqual(sanitize_archive_latency(value), float(value))
        for value in (None, -1, 181, 60000, float("nan"), float("inf"), True, "unknown"):
            self.assertIsNone(sanitize_archive_latency(value))

    def test_visible_records_sanitizes_archives_without_mutating_store(self):
        records = [
            {"date": "2026-01-01", "source": "archive", "latency_min": 1200000},
            {"date": "2026-01-02", "source": "archive", "latency_min": -1},
            {"date": "2026-01-03", "source": "archive", "latency_min": None},
            {"date": "2026-01-04", "source": "archive", "latency_min": 25},
            {"date": "2026-01-05", "source": "diary", "latency_min": 30},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "archive.sqlite3")
            with store.connect() as db:
                for record in records:
                    db.execute(
                        "INSERT INTO records(date,data) VALUES(?,?)",
                        (record["date"], json.dumps(record)),
                    )
            research = ResearchStub()
            application = Application(store, research, night_models=object())
            visible = application.visible_records({"link_history": True})

            self.assertEqual(
                [record["latency_min"] for record in visible],
                [None, None, None, 25.0, 30],
            )
            self.assertEqual(research.summary["stored_archive_latency_rejected_count"], 2)
            self.assertEqual(
                [record["latency_min"] for record in store.records()],
                [1200000, -1, None, 25, 30],
            )

    def test_unlinked_profile_hides_archive_and_keeps_diary_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "archive.sqlite3")
            archive = {"date": "2026-01-01", "source": "archive", "latency_min": 60000}
            diary = {"date": "2026-01-02", "source": "diary", "latency_min": 30}
            with store.connect() as db:
                for record in (archive, diary):
                    db.execute(
                        "INSERT INTO records(date,data) VALUES(?,?)",
                        (record["date"], json.dumps(record)),
                    )
            research = ResearchStub()
            visible = Application(store, research, night_models=object()).visible_records(
                {"link_history": False})
            self.assertEqual(visible, [diary])
            self.assertEqual(research.summary["stored_archive_latency_rejected_count"], 0)


if __name__ == "__main__":
    unittest.main()
