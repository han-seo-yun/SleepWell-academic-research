import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from measurement_audit import audit_measurements


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 2, 28, 20, tzinfo=KST)


def record(identity="r1", day="2026-02-02", source="manual",
           bed="2026-02-01T23:00:00+09:00", wake="2026-02-02T07:00:00+09:00",
           duration=450, modified="2026-02-02T08:00:00+09:00", **extra):
    return {"id": identity, "day": day, "bedtime": bed, "wake_time": wake,
            "duration_minutes": duration, "source": source, "modified_at": modified, **extra}


def snapshot(day, revision, generated, records, participant="a" * 32, received=None):
    return {"day": day, "revision": revision, "participant_key": participant,
            "generated_at": generated, "received_at": received or generated,
            "data": {"records": records}}


class MeasurementAuditTests(unittest.TestCase):
    def test_enrollment_coverage_is_explicit_and_not_wear_adherence(self):
        result = audit_measurements(
            [record(), record("r2", day="2026-02-04", bed="2026-02-03T23:00:00+09:00",
                              wake="2026-02-04T07:00:00+09:00", modified="2026-02-04T08:00:00+09:00")],
            "2026-02-05T20:00:00+09:00", {"created_at": "2026-02-01T10:00:00+09:00"})
        coverage = result["enrollment_coverage"]
        self.assertEqual(coverage["denominator_days"], 5)
        self.assertEqual(coverage["by_role"]["diary_primary"]["recorded_day_fraction"], .4)
        self.assertIn("not sensor wear", coverage["interpretation"])
        missing = audit_measurements([], NOW, {})["enrollment_coverage"]
        self.assertFalse(missing["available"])

    def test_duration_difference_is_not_called_efficiency_or_error(self):
        old = record(duration=480, modified="2026-02-02T07:30:00+09:00")
        corrected = record(duration=420)
        result = audit_measurements([corrected, old], NOW)
        duration = result["duration_semantics"]
        self.assertEqual(duration["median_declared_minus_timestamp_interval_min"], -60)
        self.assertEqual(result["record_validation"]["superseded_versions"], 1)
        self.assertEqual(result["record_validation"]["latest_known_episode_versions"], 1)
        self.assertNotIn("efficiency", duration["interpretation"].lower().split("not")[0])
        self.assertIn("not labeled sleep efficiency", duration["interpretation"])

    def test_episode_policies_flag_disagreement_without_relabeling_nap(self):
        rows = [
            record("wear", source="samsung_health", bed="2026-02-01T22:00:00+09:00",
                   wake="2026-02-02T07:00:00+09:00", modified="2026-02-02T07:15:00+09:00"),
            record("diary", source="manual", bed="2026-02-01T23:30:00+09:00",
                   wake="2026-02-02T06:30:00+09:00", modified="2026-02-02T09:00:00+09:00"),
        ]
        value = audit_measurements(rows, NOW)["episode_selection_sensitivity"]
        self.assertEqual(value["multiple_episode_wake_dates"], 1)
        self.assertEqual(value["overlapping_episode_wake_dates"], 1)
        self.assertEqual(value["policy_disagreement_wake_dates"]["longest_vs_latest_modified"], 1)
        self.assertIn("not automatically naps", value["interpretation"])

    def test_fixed_windows_are_source_specific_nonoverlapping_and_supported(self):
        rows = []
        for offset in (1, 2, 3, 25, 26, 27):
            wake = datetime(2026, 2, 1, 7, tzinfo=KST) + timedelta(days=offset)
            bed = wake - timedelta(hours=8)
            rows.append(record(str(offset), day=wake.date().isoformat(),
                               bed=bed.isoformat(), wake=wake.isoformat(),
                               modified=(wake + timedelta(hours=1)).isoformat()))
        value = audit_measurements(rows, NOW, {"created_at": "2026-02-01T10:00:00+09:00"})
        manual = value["fixed_14_day_periods"]["by_source"]["manual"]
        self.assertTrue(manual["periods_nonoverlapping"])
        self.assertTrue(manual["descriptive_difference_available"])
        self.assertEqual(manual["first_14_days"]["recorded_days"], 3)
        self.assertFalse(value["fixed_14_day_periods"]["by_source"]["healthkit"]["descriptive_difference_available"])

    def test_first_snapshot_appearance_uses_history_not_modified_at(self):
        item = record(modified="2026-02-20T12:00:00+09:00")
        first = snapshot("2026-02-02", 1, "2026-02-02T08:00:00+09:00", [item],
                         received="2026-02-02T09:00:00+09:00")
        later = snapshot("2026-02-03", 1, "2026-02-03T08:00:00+09:00", [item])
        duplicate = copy.deepcopy(first)
        value = audit_measurements([item], NOW, snapshots=[later, duplicate, first])["first_snapshot_appearance_lag"]
        self.assertTrue(value["available"])
        self.assertEqual(value["accepted_snapshot_versions"], 2)
        self.assertEqual(value["first_snapshot_generation_lag_median_min"], 60)
        self.assertEqual(value["first_pc_receipt_lag_median_min"], 120)
        self.assertFalse(value["modified_at_used"])

    def test_future_snapshot_and_record_cannot_affect_results(self):
        future_record = record("future", day="2026-03-02", bed="2026-03-01T23:00:00+09:00",
                               wake="2026-03-02T07:00:00+09:00", modified="2026-03-02T08:00:00+09:00")
        future_snapshot = snapshot("2026-03-02", 1, "2026-03-02T09:00:00+09:00", [future_record])
        value = audit_measurements([record(), future_record], NOW, snapshots=[future_snapshot])
        self.assertEqual(value["record_validation"]["not_yet_available"], 1)
        self.assertEqual(value["first_snapshot_appearance_lag"]["future_snapshot_versions_excluded"], 1)
        self.assertFalse(value["first_snapshot_appearance_lag"]["available"])

    def test_conflicting_revision_is_not_used_and_multiple_people_are_not_mixed(self):
        first = snapshot("2026-02-02", 1, "2026-02-02T08:00:00+09:00", [record()])
        conflict = copy.deepcopy(first); conflict["data"]["records"] = [record("other")]
        value = audit_measurements([], NOW, snapshots=[first, conflict])["first_snapshot_appearance_lag"]
        self.assertEqual(value["conflicting_revision_keys"], 1)
        self.assertFalse(value["available"])
        other_person = snapshot("2026-02-03", 1, "2026-02-03T08:00:00+09:00", [], "b" * 32)
        separated = audit_measurements([], NOW, snapshots=[first, other_person])["first_snapshot_appearance_lag"]
        self.assertEqual(separated["reason"], "multiple_participants_require_separate_audits")

    def test_unknown_metadata_does_not_create_device_wear_os_or_shift_claims(self):
        value = audit_measurements(
            [record(device_model="Watch X", os_version="99", wear_minutes=1440)], NOW,
            profile={"created_at": "2026-02-01T10:00:00+09:00", "occupation": "worker",
                     "shift_work": True})
        unavailable = value["unavailable_dimensions"]
        self.assertEqual(unavailable["wear_time"], "not_in_current_android_record_contract")
        self.assertEqual(unavailable["device_model"], "not_in_current_android_record_contract")
        self.assertIn("not_measured", unavailable["shift_work"])
        self.assertEqual(value["record_validation"]["records_with_ignored_unregistered_fields"], 1)

    def test_validated_nap_and_sync_context_are_direct_not_inferred(self):
        context = {"daily_contexts": [{
            "schema_version": 1, "night_id": "2026-02-20",
            "observed_at": "2026-02-20T18:00:00+09:00",
            "naps": [{"start": "2026-02-20T13:00:00+09:00", "end": "2026-02-20T13:30:00+09:00"}],
        }], "mobile_sync_summary": {"sessions_total": 10, "sessions_eligible": 6,
              "records_held": 4, "conflicts_pending": 1,
              "held_reasons": {"stage_gap": 2, "unknown_stage": 1, "secret": 99}}}
        value = audit_measurements([], NOW, context=context)["direct_context"]
        self.assertEqual(value["reported_completed_naps"], 1)
        self.assertEqual(value["reported_completed_nap_minutes"], 30)
        self.assertNotIn("secret", value["mobile_sync_ingestion"]["held_reasons"])
        self.assertIn("not wear time", value["mobile_sync_ingestion"]["interpretation"])

    def test_output_contains_no_record_or_participant_identifiers(self):
        secret_record = record("SECRET-RECORD")
        secret_snapshot = snapshot("2026-02-02", 1, "2026-02-02T08:00:00+09:00",
                                   [secret_record], participant="SECRET-PARTICIPANT")
        serialized = repr(audit_measurements([secret_record], NOW, snapshots=[secret_snapshot]))
        self.assertNotIn("SECRET-RECORD", serialized)
        self.assertNotIn("SECRET-PARTICIPANT", serialized)
        self.assertFalse(audit_measurements([], NOW)["quality_subset_comparison"]["available"])


if __name__ == "__main__":
    unittest.main()
