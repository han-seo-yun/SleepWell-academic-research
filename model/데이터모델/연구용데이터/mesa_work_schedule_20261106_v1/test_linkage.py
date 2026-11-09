import tempfile
import unittest
from pathlib import Path

import pandas as pd

from linkage import LINKED_COLUMNS, load_schedule, merge_mesa_schedule, mesa_subject_token


class LoadScheduleTests(unittest.TestCase):
    def write_csv(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "schedule.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_leading_zero_and_decimal_ids_use_existing_token_contract(self):
        first = mesa_subject_token("00042")
        second = mesa_subject_token("42.0")
        expected = "mesa_" + __import__("hashlib").sha256(
            b"mesa_subject_v1|42"
        ).hexdigest()[:20]
        self.assertEqual(first, second)
        self.assertEqual(first, expected)

    def test_duplicate_after_key_normalization_is_rejected(self):
        path = self.write_csv(
            "mesaid,wrksched5,extrahrs5\n00042,1,2\n42.0,2,3\n"
        )
        with self.assertRaisesRegex(ValueError, "duplicate participants"):
            load_schedule(path)

    def test_missing_and_bad_values_remain_distinct_and_are_not_zero_filled(self):
        path = self.write_csv(
            "mesaid,wrksched5,extrahrs5\n1,,\n2,99,32\n3,3,0\n"
        )
        result = load_schedule(path)
        self.assertEqual(result["work_schedule_status"].tolist(), ["missing", "invalid", "observed"])
        self.assertEqual(result["work_schedule_category"].tolist(), ["missing", "unknown_invalid", "night_shift"])
        self.assertEqual(result["extra_hours_status"].tolist(), ["missing", "invalid", "observed"])
        self.assertTrue(pd.isna(result.loc[0, "extra_hours_days_per_month"]))
        self.assertTrue(pd.isna(result.loc[1, "extra_hours_days_per_month"]))
        self.assertEqual(result.loc[2, "extra_hours_days_per_month"], 0)


class MergeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.mesa_id = mesa_subject_token(7)
        self.metadata = pd.DataFrame(
            {
                "subject_id": pd.Series([self.mesa_id], dtype="string"),
                "work_schedule_code": pd.Series([1], dtype="Int64"),
                "work_schedule_category": pd.Series(["day_shift"], dtype="string"),
                "work_schedule_status": pd.Series(["observed"], dtype="string"),
                "extra_hours_days_per_month": pd.Series([2], dtype="Int64"),
                "extra_hours_status": pd.Series(["observed"], dtype="string"),
            }
        )

    def test_mixed_cohort_collision_never_links_cmi_and_preserves_rows_and_splits(self):
        episodes = pd.DataFrame(
            {
                "cohort": ["CMI", "MESA", "MESA"],
                "subject_id": [self.mesa_id, self.mesa_id, "mesa_unmatched"],
                "split": ["test", "train", "validation"],
                "age": [20, 60, 70],
                "marker": ["first", "second", "third"],
            },
            index=[9, 4, 8],
        )
        before = episodes.copy(deep=True)
        joined, audit = merge_mesa_schedule(episodes, self.metadata)

        pd.testing.assert_frame_equal(joined[list(before.columns)], before)
        self.assertEqual(joined.index.tolist(), [9, 4, 8])
        self.assertTrue(joined.loc[9, list(LINKED_COLUMNS)].isna().all())
        self.assertEqual(joined.loc[4, "work_schedule_category"], "day_shift")
        self.assertTrue(joined.loc[8, list(LINKED_COLUMNS)].isna().all())
        self.assertEqual(audit["episode_rows"], 3)
        self.assertEqual(audit["mesa_matched_rows"], 1)
        self.assertEqual(audit["mesa_unmatched_rows"], 1)
        self.assertTrue(audit["row_order_preserved"])

    def test_duplicate_metadata_is_rejected(self):
        duplicate = pd.concat([self.metadata, self.metadata], ignore_index=True)
        episodes = pd.DataFrame(
            {"cohort": ["MESA"], "subject_id": [self.mesa_id], "split": ["train"]}
        )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            merge_mesa_schedule(episodes, duplicate)

    def test_existing_linked_column_is_rejected_instead_of_overwritten(self):
        episodes = pd.DataFrame(
            {
                "cohort": ["MESA"],
                "subject_id": [self.mesa_id],
                "split": ["train"],
                "work_schedule_code": [7],
            }
        )
        with self.assertRaisesRegex(ValueError, "already contain linked columns"):
            merge_mesa_schedule(episodes, self.metadata)


if __name__ == "__main__":
    unittest.main()
