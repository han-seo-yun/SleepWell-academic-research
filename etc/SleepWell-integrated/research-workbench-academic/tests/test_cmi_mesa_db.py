from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_cmi_mesa_db", ROOT / "scripts" / "build_cmi_mesa_db.py"
)
builder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(builder)


class CmiMesaDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "episodes.csv"
        self.metadata = self.root / "dataset_metadata.json"
        self.output = self.root / "unified"
        self.rows = [
            self.row("CMI", "shared", "train", "2026-01-01", age="", sex=""),
            self.row("MESA", "shared", "test", "2026-01-02", age="70", sex="F"),
            self.row("Samsung", "watch-1", "validation", "2026-01-03", age="33", sex="M"),
        ]
        self.write_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def row(cohort, subject_id, split, day, *, age, sex):
        return {
            "cohort": cohort,
            "subject_id": subject_id,
            "split": split,
            "cutoff": f"{day}T20:00:00+09:00",
            "bed": f"{day}T22:00:00+09:00",
            "wake": f"{day}T23:00:00+09:00",
            "observed_at": f"{day}T23:00:00+09:00",
            "bedtime_min": "120",
            "wake_min": "180",
            "duration_min": "60",
            "age": age,
            "sex": sex,
            "label_source": f"{cohort} fixture label",
        }

    def write_fixture(self, *, counts=None):
        with self.source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=builder.COLUMNS)
            writer.writeheader()
            writer.writerows(self.rows)
        counts = counts or {"CMI": (1, 1), "MESA": (1, 1)}
        value = {
            "time_contract": {"cutoff": "20:00 fixture"},
            "cohorts": {
                cohort: {
                    "qc": {
                        "common_output_rows": episodes,
                        ("output_subjects" if cohort == "MESA" else "subjects"): participants,
                    },
                    "label_source": f"{cohort} fixture label",
                }
                for cohort, (episodes, participants) in counts.items()
            },
        }
        self.metadata.write_text(json.dumps(value), encoding="utf-8")

    def build(self):
        return builder.build_database(self.source, self.metadata, self.output)

    def assert_failed_without_publication(self, message):
        with self.assertRaisesRegex(ValueError, message):
            self.build()
        self.assertFalse(self.output.exists())

    def test_build_preserves_cohort_identity_splits_nulls_and_csv_equivalence(self):
        manifest = self.build()
        self.assertEqual(manifest["included_cohorts"], ["CMI", "MESA"])
        self.assertEqual(manifest["excluded_rows"], {"Samsung": 1})
        self.assertEqual(manifest["verification"]["integrity_check"], "ok")

        database = self.output / "cmi_mesa.sqlite3"
        connection = sqlite3.connect(database)
        try:
            db_rows = connection.execute(
                "SELECT * FROM model_data ORDER BY cohort, subject_id, cutoff"
            ).fetchall()
            participants = connection.execute(
                "SELECT cohort, subject_id, split FROM participants ORDER BY cohort"
            ).fetchall()
            samsung = connection.execute(
                "SELECT COUNT(*) FROM model_data WHERE cohort = 'Samsung'"
            ).fetchone()[0]
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

        self.assertEqual(participants, [("CMI", "shared", "train"), ("MESA", "shared", "test")])
        self.assertEqual(len(db_rows), 2)
        self.assertEqual(samsung, 0)
        cmi = db_rows[0]
        self.assertIsNone(cmi[10])
        self.assertIsNone(cmi[11])

        with (self.output / "cmi_mesa_episodes.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            csv_rows = list(csv.reader(handle))
        normalized_db_rows = [
            ["" if value is None else str(value) for value in row] for row in db_rows
        ]
        self.assertEqual(csv_rows[0], list(builder.COLUMNS))
        self.assertEqual(csv_rows[1:], normalized_db_rows)

    def test_participant_cannot_cross_splits(self):
        self.rows.insert(1, self.row("CMI", "shared", "test", "2026-01-04", age="", sex=""))
        self.write_fixture(counts={"CMI": (2, 1), "MESA": (1, 1)})
        self.assert_failed_without_publication("crosses train/validation/test splits")

    def test_duplicate_cohort_subject_cutoff_is_rejected(self):
        self.rows.insert(1, dict(self.rows[0]))
        self.write_fixture(counts={"CMI": (2, 1), "MESA": (1, 1)})
        self.assert_failed_without_publication("Invalid included record")

    def test_non_finite_numeric_value_is_rejected(self):
        self.rows[0]["age"] = "NaN"
        self.write_fixture()
        self.assert_failed_without_publication("numeric episode values must be finite")

    def test_timestamp_and_derived_minutes_mismatch_is_rejected(self):
        invalid_cases = (
            ("observed_at", "2026-01-01T22:59:00+09:00", "observed_at must preserve"),
            ("bedtime_min", "121", "episode minutes disagree"),
        )
        for field, value, message in invalid_cases:
            with self.subTest(field=field):
                self.rows[0][field] = value
                self.write_fixture()
                self.assert_failed_without_publication(message)
                self.rows[0] = self.row("CMI", "shared", "train", "2026-01-01", age="", sex="")

    def test_truncated_input_is_rejected_by_metadata_counts(self):
        self.rows = [row for row in self.rows if row["cohort"] != "MESA"]
        self.write_fixture()
        self.assert_failed_without_publication("Source counts disagree with metadata for MESA")

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("unchanged", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self.build()
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()
