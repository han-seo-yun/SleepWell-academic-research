from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import execute_verified as verified


class VerifiedSourceBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = Path(__file__).resolve().parents[3]
        cls.experiment_dir = Path(__file__).resolve().parent
        with (cls.experiment_dir / "protocol.json").open("r", encoding="utf-8") as handle:
            cls.protocol = json.load(handle)
        cls.episodes_csv = cls.workspace / cls.protocol["sources"]["episodes"]
        cls.sensor_cache = cls.workspace / cls.protocol["sources"]["sensor_cache"]
        cls.sensor_metadata_path = cls.workspace / cls.protocol["sources"]["sensor_metadata"]
        cls.samsung_parquet = cls.workspace / verified.SAMSUNG_PARQUET_RELATIVE
        with cls.sensor_metadata_path.open("r", encoding="utf-8") as handle:
            cls.sensor_metadata = json.load(handle)

    def test_genuine_csv_samsung_targets_equal_metadata_bound_parquet(self) -> None:
        hashes = verified.validate_sensor_metadata(
            self.sensor_metadata, self.sensor_cache, self.samsung_parquet
        )
        self.assertEqual(
            hashes["samsung_parquet_actual_sha256"],
            "9ff4f36dc0ab07976766c29e2d3f3d75dafbfed26df41826519bdb0f394794ee",
        )
        targets = verified.prepare_verified_targets(
            self.episodes_csv, self.samsung_parquet
        )
        self.assertEqual(len(targets), 196)
        self.assertEqual(
            targets["split"].value_counts().to_dict(),
            {"train": 137, "validation": 29, "test": 30},
        )
        self.assertFalse(targets["cutoff"].duplicated().any())

    def test_altered_parquet_is_rejected_by_hash_and_target_binding(self) -> None:
        altered = pd.read_parquet(self.samsung_parquet)
        mask = (
            altered["cohort"].astype(str).str.casefold().eq("samsung")
            & altered["subject_id"].eq("samsung_personal_001")
        )
        row = altered.index[mask][0]
        altered.loc[row, "bed"] = pd.Timestamp(altered.loc[row, "bed"]) + pd.Timedelta(
            minutes=1
        )
        altered.loc[row, "bedtime_min"] = float(altered.loc[row, "bedtime_min"]) + 1.0
        altered.loc[row, "duration_min"] = float(altered.loc[row, "duration_min"]) - 1.0
        if "bed_offset" in altered.columns:
            altered.loc[row, "bed_offset"] = float(altered.loc[row, "bed_offset"]) + 1.0

        with tempfile.TemporaryDirectory() as temporary:
            altered_path = Path(temporary) / "episodes.parquet"
            altered.to_parquet(altered_path, index=False)
            with self.assertRaisesRegex(ValueError, "Samsung Parquet SHA-256 mismatch"):
                verified.validate_sensor_metadata(
                    self.sensor_metadata, self.sensor_cache, altered_path
                )
            with self.assertRaisesRegex(
                ValueError, "CSV Samsung targets do not exactly match"
            ):
                verified.prepare_verified_targets(self.episodes_csv, altered_path)


if __name__ == "__main__":
    unittest.main()
