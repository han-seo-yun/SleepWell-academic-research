import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from target_validity import summarize_target_validity


KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2026, 1, 31, 12, tzinfo=KST)


def record(
    record_id,
    source,
    bed="2026-01-01T23:00:00+09:00",
    wake="2026-01-02T07:00:00+09:00",
    modified="2026-01-02T08:00:00+09:00",
):
    return {
        "id": record_id,
        "day": "2026-01-02",
        "bedtime": bed,
        "wake_time": wake,
        "duration_minutes": 480,
        "source": source,
        "modified_at": modified,
    }


class TargetValidityTests(unittest.TestCase):
    def test_midnight_timing_uses_timestamps_not_clock_subtraction(self):
        rows = [
            record("d", "manual", bed="2026-01-01T23:55:00+09:00", wake="2026-01-02T07:00:00+09:00"),
            record("w", "health_connect", bed="2026-01-02T00:05:00+09:00", wake="2026-01-02T07:10:00+09:00"),
        ]
        result = summarize_target_validity(rows, AS_OF)
        comparison = result["agreement"]["by_wearable_source"]["health_connect"]
        self.assertEqual(comparison["paired_nights"], 1)
        self.assertEqual(comparison["onset"]["mean_signed_difference_min"], 10.0)
        self.assertEqual(comparison["wake"]["mean_signed_difference_min"], 10.0)

    def test_utc_timestamps_are_grouped_by_kst_wake_date(self):
        rows = [
            record("d", "diary", bed="2026-01-01T14:00:00Z", wake="2026-01-01T22:00:00Z",
                   modified="2026-01-01T23:00:00Z"),
            record("w", "healthkit", bed="2026-01-01T14:15:00Z", wake="2026-01-01T22:15:00Z",
                   modified="2026-01-01T23:15:00Z"),
        ]
        result = summarize_target_validity(rows, AS_OF)
        self.assertEqual(result["coverage"]["span_start_wake_date"], "2026-01-02")
        self.assertEqual(result["agreement"]["by_wearable_source"]["healthkit"]["paired_nights"], 1)

    def test_duplicate_diary_is_ambiguous_and_no_favorable_pair_is_selected(self):
        rows = [
            record("d1", "manual"),
            record("d2", "diary", bed="2026-01-01T23:05:00+09:00"),
            record("w", "samsung_health", bed="2026-01-01T23:05:00+09:00"),
        ]
        result = summarize_target_validity(rows, AS_OF)
        comparison = result["agreement"]["by_wearable_source"]["samsung_health"]
        self.assertEqual(comparison["paired_nights"], 0)
        self.assertEqual(comparison["ambiguous_duplicate_nights_excluded"], 1)
        self.assertIsNone(comparison["onset"]["median_absolute_difference_min"])

    def test_future_invalid_naive_and_nonpositive_are_counted(self):
        future = record("future", "manual", modified="2026-02-01T08:00:00+09:00")
        naive = record("naive", "manual")
        naive["bedtime"] = "2026-01-01T23:00:00"
        reversed_interval = record("reverse", "manual", bed="2026-01-02T08:00:00+09:00")
        malformed = {"id": "bad"}
        result = summarize_target_validity([future, naive, reversed_interval, malformed], AS_OF)
        exclusions = result["exclusions"]
        self.assertEqual(exclusions["future_record_count"], 1)
        self.assertEqual(exclusions["timezone_naive_count"], 1)
        self.assertEqual(exclusions["nonpositive_interval_count"], 1)
        self.assertEqual(exclusions["missing_required_field_count"], 1)
        self.assertEqual(result["record_counts"]["valid_known_source"], 0)

    def test_no_pairs_returns_null_metrics_instead_of_perfect_agreement(self):
        result = summarize_target_validity([record("d", "user_entry")], AS_OF)
        comparison = result["agreement"]["by_wearable_source"]["health_connect"]
        self.assertEqual(comparison["paired_nights"], 0)
        self.assertIsNone(comparison["onset"]["median_absolute_difference_min"])
        self.assertIsNone(comparison["wake"]["proportion_absolute_over_30_min"])
        self.assertIsNone(comparison["onset"]["signed_difference_quantiles_2_5_97_5_min"])

    def test_wearable_sources_are_reported_separately_without_overwrite(self):
        rows = [
            record("d", "manual"),
            record("hc", "health_connect", bed="2026-01-01T23:10:00+09:00"),
            record("hk", "healthkit", bed="2026-01-01T23:20:00+09:00"),
            record("sh", "samsung_health", bed="2026-01-01T23:30:00+09:00"),
        ]
        result = summarize_target_validity(rows, AS_OF)
        by_source = result["agreement"]["by_wearable_source"]
        self.assertEqual({key: value["paired_nights"] for key, value in by_source.items()}, {
            "health_connect": 1, "healthkit": 1, "samsung_health": 1,
        })
        self.assertEqual(by_source["health_connect"]["onset"]["mean_signed_difference_min"], 10.0)
        self.assertEqual(by_source["healthkit"]["onset"]["mean_signed_difference_min"], 20.0)
        self.assertEqual(by_source["samsung_health"]["onset"]["mean_signed_difference_min"], 30.0)

    def test_nonoverlapping_intervals_are_not_matched_by_date_alone(self):
        rows = [
            record("d", "manual", bed="2026-01-01T23:00:00+09:00", wake="2026-01-02T01:00:00+09:00"),
            record("w", "health_connect", bed="2026-01-02T02:00:00+09:00", wake="2026-01-02T07:00:00+09:00"),
        ]
        result = summarize_target_validity(rows, AS_OF)
        comparison = result["agreement"]["by_wearable_source"]["health_connect"]
        self.assertEqual(comparison["paired_nights"], 0)
        self.assertEqual(comparison["nonoverlapping_interval_nights_excluded"], 1)

    def test_coverage_denominator_is_observed_span_and_not_enrollment(self):
        rows = [
            record("d1", "manual"),
            record("d3", "manual", bed="2026-01-03T23:00:00+09:00",
                   wake="2026-01-04T07:00:00+09:00", modified="2026-01-04T08:00:00+09:00"),
        ]
        result = summarize_target_validity(rows, AS_OF)
        diary = result["coverage"]["by_role"]["diary_primary"]
        self.assertEqual(diary["observed_span_days"], 3)
        self.assertEqual(diary["observed_wake_days"], 2)
        self.assertEqual(diary["observed_day_coverage"], 0.667)
        self.assertIn("not enrollment adherence", result["coverage"]["interpretation"])

    def test_descriptive_quantiles_require_twenty_unambiguous_pairs(self):
        rows = []
        for index in range(20):
            day = index + 2
            rows.extend([
                record(
                    f"d{index}", "manual",
                    bed=f"2026-01-{day - 1:02d}T23:00:00+09:00",
                    wake=f"2026-01-{day:02d}T07:00:00+09:00",
                    modified=f"2026-01-{day:02d}T08:00:00+09:00",
                ),
                record(
                    f"w{index}", "health_connect",
                    bed=f"2026-01-{day - 1:02d}T23:{index:02d}:00+09:00",
                    wake=f"2026-01-{day:02d}T07:{index:02d}:00+09:00",
                    modified=f"2026-01-{day:02d}T08:00:00+09:00",
                ),
            ])
        comparison = summarize_target_validity(rows, AS_OF)["agreement"]["by_wearable_source"]["health_connect"]
        self.assertEqual(comparison["paired_nights"], 20)
        self.assertTrue(comparison["quantile_policy"]["available"])
        self.assertIsNotNone(comparison["onset"]["signed_difference_quantiles_2_5_97_5_min"])

    def test_output_is_aggregate_and_marks_unmeasured_evidence(self):
        result = summarize_target_validity(
            [record("secret-id", "health_connect")], AS_OF
        )
        serialized = repr(result)
        self.assertNotIn("secret-id", serialized)
        self.assertEqual(result["wear_quality"]["status"], "unavailable")
        self.assertEqual(result["wear_quality"]["valid_wearable_records_without_wear_quality"], 1)
        self.assertEqual(result["evidence_status"]["psg_gold_standard"], "unavailable")
        self.assertFalse(result["evidence_status"]["actual_sleep_quality_measured"])
        self.assertFalse(result["evidence_status"]["sleep_pressure_measured"])
        self.assertIn("later edits", result["update_lag"]["interpretation"])


if __name__ == "__main__":
    unittest.main()
