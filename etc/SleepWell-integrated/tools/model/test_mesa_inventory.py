import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mesa_inventory


HEADER = [
    "mesaid", "line", "linetime", "offwrist", "activity", "marker",
    "whitelight", "redlight", "greenlight", "bluelight", "wake", "interval",
    "dayofweek", "daybymidnight", "daybynoon", "date_shifted", "actigraphy_month_start",
]


def write_actigraphy(path: Path, participant: str, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for index, values in enumerate(rows):
            writer.writerow([
                participant, str(index + 1), values[0], values[1], values[2], "",
                "1", "2", "3", "4", values[3], values[4], "1", "1", "1",
                "2000-01-01", "1",
            ])


class MesaInventoryTest(unittest.TestCase):
    def test_counts_modalities_and_reports_aggregate_qc_without_ids_or_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "MESA"
            rows = [
                ["00:00:00", "0", "10", "1", "REST"],
                ["00:00:30", "1", "", "0", "ACTIVE"],
            ]
            write_actigraphy(root / "actigraphy" / "mesa-sleep-0001.csv", "1", rows)
            write_actigraphy(root / "actigraphy" / "mesa-sleep-0002.csv", "2", rows)
            (root / "polysomnography").mkdir()
            (root / "polysomnography" / "mesa-sleep-0001.edf").write_bytes(b"EDF")
            (root / "annotations").mkdir()
            (root / "annotations" / "mesa-sleep-0001-nsrr.xml").write_text("<xml/>", encoding="utf-8")

            payload = mesa_inventory.build_inventory(root, sample_files=2)
            output = base / "inventory.json"
            mesa_inventory.write_inventory(payload, output, root)
            serialized = output.read_text(encoding="utf-8")

            self.assertEqual(payload["inventory"]["modalities"]["actigraphy"]["files"], 2)
            self.assertEqual(payload["inventory"]["modalities"]["actigraphy"]["unique_participant_ids"], 2)
            self.assertEqual(payload["inventory"]["participant_intersections"]["actigraphy&psg_edf"], 1)
            self.assertEqual(payload["inventory"]["participant_intersections"]["all_named_modalities"], 0)
            self.assertEqual(payload["interpretation"]["psg_matched_file_candidates"], 1)
            self.assertTrue(payload["interpretation"]["psg_matched_file_candidates_present"])
            self.assertFalse(payload["interpretation"]["psg_supervised_stage_training_ready"])
            self.assertTrue(payload["interpretation"]["sixty_second_features_require_separate_causal_aggregation"])
            self.assertEqual(payload["semantic_qc"]["rows"], 4)
            self.assertEqual(payload["semantic_qc"]["missing_by_column"]["activity"], 2)
            self.assertEqual(payload["semantic_qc"]["cadence_seconds"], {"30": 2})
            self.assertEqual(payload["semantic_qc"]["thirty_second_cadence_fraction"], 1.0)
            self.assertNotIn("mesa-sleep-0001", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertEqual(json.loads(serialized), payload)

    def test_malformed_and_row_limited_files_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MESA"
            rows = [
                ["bad-time", "2", "not-a-number", "maybe", "REST"],
                ["00:00:30", "0", "1", "1", "ACTIVE"],
            ]
            write_actigraphy(root / "actigraphy" / "mesa-sleep-0001.csv", "9999", rows)
            payload = mesa_inventory.build_inventory(root, sample_files=1, max_rows_per_file=1)
            sampling = payload["semantic_qc"]["sampling"]
            self.assertEqual(sampling["row_limit_exceeded_files_excluded"], 1)
            self.assertEqual(payload["semantic_qc"]["rows"], 0)

    def test_unstable_sample_is_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MESA"
            write_actigraphy(
                root / "actigraphy" / "mesa-sleep-0001.csv",
                "1",
                [["00:00:00", "0", "1", "1", "REST"]],
            )
            real_signature = mesa_inventory._stat_signature
            calls = 0

            def changing_signature(path: Path):
                nonlocal calls
                calls += 1
                signature = real_signature(path)
                if calls >= 2 and signature is not None:
                    return signature[0] + 1, signature[1]
                return signature

            with mock.patch.object(mesa_inventory, "_stat_signature", side_effect=changing_signature):
                payload = mesa_inventory.build_inventory(root, sample_files=1)
            self.assertEqual(payload["semantic_qc"]["sampling"]["unstable_files_excluded"], 1)
            self.assertEqual(payload["semantic_qc"]["rows"], 0)

    def test_rejects_output_inside_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MESA"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "outside"):
                mesa_inventory.write_inventory({}, root / "inventory.json", root)

    def test_normalizes_numeric_ids_with_leading_zeros(self):
        self.assertEqual(
            mesa_inventory._participant_id(Path("mesa-sleep-0001.csv")),
            mesa_inventory._participant_id(Path("mesa-sleep-1.edf")),
        )

    def test_excludes_symlinks_from_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "MESA"
            root.mkdir()
            link = root / "mesa-sleep-0001.csv"
            link.write_text(",".join(HEADER) + "\n", encoding="utf-8")
            path_type = type(link)
            real_is_symlink = path_type.is_symlink

            def mark_fixture_as_symlink(path):
                return path == link or real_is_symlink(path)

            with mock.patch.object(path_type, "is_symlink", autospec=True, side_effect=mark_fixture_as_symlink):
                payload = mesa_inventory.build_inventory(root)
            self.assertEqual(payload["scope"]["snapshot_file_count"], 0)
            self.assertEqual(payload["scope"]["skipped_at_discovery"]["symlinks"], 1)


if __name__ == "__main__":
    unittest.main()
