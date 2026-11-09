from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from researchbench.prospective_study import ProspectiveStudy


PROFILE = {
    "age": 30, "usual_bed": "23:30", "usual_wake": "07:00", "target_wake": "07:00",
    "weekend_wake": "08:00", "usual_sleep_h": 7.5, "desired_sleep_h": 8.0,
    "latency_min": 20, "average_steps": 5000, "exercise_min": 30,
}
GOVERNED = {
    "ethics_reference": "IRB-draft-confirmed-reference",
    "consent_version": "v1", "responsible_researcher": "Researcher",
    "recruitment_start": "2026-11-01",
}
BASELINE = {
    "sex": "prefer_not_to_say", "shift_work": "no", "recruitment_source": "unknown",
    "sleep_medication": "unknown", "sleep_diagnosis": "unknown",
    "device_access": "personal",
}


class ProspectiveStudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "study"
        self.study = ProspectiveStudy(self.root)
        self.morning = datetime.fromisoformat("2026-11-04T08:00:00+09:00")

    def tearDown(self):
        self.temp.cleanup()

    def create_and_enroll(self, *, synthetic=True):
        protocol = {} if synthetic else GOVERNED
        with patch("researchbench.prospective_study._now", return_value=datetime.fromisoformat(
                "2026-11-03T12:00:00+09:00")):
            self.study.create(protocol, synthetic=synthetic)
            return self.study.enroll({**BASELINE, "consent_confirmed": True, "profile": PROFILE})

    def test_real_draft_is_frozen_but_blocks_enrollment_without_governance(self):
        with patch("researchbench.prospective_study._now", return_value=self.morning):
            created = self.study.create({}, synthetic=False)
            self.assertFalse(created["ready_for_enrollment"])
            with self.assertRaisesRegex(ValueError, "missing governance"):
                self.study.enroll({**BASELINE, "consent_confirmed": True, "profile": PROFILE})
        with self.assertRaises(FileExistsError):
            self.study.create(GOVERNED)

    def test_real_enrollment_requires_consent_and_complete_declared_profile(self):
        with patch("researchbench.prospective_study._now", return_value=self.morning):
            self.study.create(GOVERNED)
            with self.assertRaisesRegex(ValueError, "consent_confirmed"):
                self.study.enroll({**BASELINE, "profile": PROFILE})
            with self.assertRaisesRegex(ValueError, "metadata.profile is missing"):
                self.study.enroll({**BASELINE, "consent_confirmed": True,
                                   "profile": {"usual_bed": "23:00"}})
            incomplete = dict(BASELINE)
            incomplete.pop("device_access")
            with self.assertRaisesRegex(ValueError, "baseline is missing"):
                self.study.enroll({**incomplete, "consent_confirmed": True, "profile": PROFILE})
            receipt = self.study.enroll({**BASELINE, "consent_confirmed": True, "profile": PROFILE})
        raw = self.study.db_path.read_bytes()
        self.assertNotIn(receipt["token"].encode(), raw)
        status = self.study.status()
        self.assertNotIn(receipt["participant_id"], json.dumps(status))
        self.assertEqual(status["participants_enrolled"], 1)

    def test_enrollment_at_or_after_cutoff_starts_next_day_and_profile_ranges_are_checked(self):
        at_cutoff = datetime.fromisoformat("2026-11-03T20:00:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=at_cutoff):
            self.study.create({}, synthetic=True)
            with self.assertRaisesRegex(ValueError, "metadata.age is not allowed"):
                self.study.enroll({**BASELINE, "age": 30, "profile": PROFILE})
            with self.assertRaisesRegex(ValueError, "profile.age"):
                self.study.enroll({**BASELINE, "profile": {**PROFILE, "age": 17}})
            receipt = self.study.enroll({**BASELINE, "profile": PROFILE})
        self.assertEqual(receipt["study_start_date"], "2026-11-04")

    def test_diary_derives_onset_timestamps_receipt_and_keeps_first_revision(self):
        enrollment = self.create_and_enroll()
        diary = {"night_date": "2026-11-03", "sleep_attempt": "2026-11-03T23:30:00+09:00",
                 "latency_min": 20, "final_wake": "2026-11-04T07:00:00+09:00",
                 "quality": 3, "flags": ["illness"]}
        with patch("researchbench.prospective_study._now", return_value=self.morning), patch(
                "researchbench.prospective_study.ParticipantProspective.forecasts", return_value=[]):
            first = self.study.submit_diary(enrollment["token"], diary)
            revision = self.study.submit_diary(enrollment["token"], {**diary, "latency_min": 30})
        self.assertEqual(first["timing_class"], "primary_12h")
        self.assertTrue(first["primary_revision"])
        self.assertEqual(first["forecast_status"], "missing_forecast")
        self.assertFalse(revision["primary_revision"])
        self.assertNotIn("prediction", json.dumps(first).lower())
        db = sqlite3.connect(self.study.db_path)
        try:
            values = [json.loads(row[0]) for row in db.execute("SELECT payload FROM diaries ORDER BY received_at")]
        finally:
            db.close()
        self.assertEqual(values[0]["sleep_start"], "2026-11-03T23:50:00+09:00")
        self.assertEqual(len(values), 2)

    def test_missing_late_invalid_and_cross_token_are_audited_safely(self):
        enrollment = self.create_and_enroll()
        with patch("researchbench.prospective_study._now", return_value=self.morning):
            missing = self.study.submit_diary(enrollment["token"],
                                              {"night_date": "2026-11-03", "missing_reason": "forgot"})
            self.assertEqual(missing["timing_class"], "missing_declared")
            with self.assertRaisesRegex(PermissionError, "token is invalid"):
                self.study.submit_diary("wrong-token", {"night_date": "2026-11-03",
                                                         "missing_reason": "forgot"})
            with self.assertRaisesRegex(ValueError, "final_wake cannot be after receipt"):
                self.study.submit_diary(enrollment["token"], {
                    "night_date": "2026-11-03", "sleep_attempt": "2026-11-03T23:00:00+09:00",
                    "latency_min": 10, "final_wake": "2026-11-04T09:00:00+09:00"})
        late_time = datetime.fromisoformat("2026-11-05T09:00:01+09:00")
        with patch("researchbench.prospective_study._now", return_value=late_time):
            late = self.study.submit_diary(enrollment["token"], {
                "night_date": "2026-11-04", "sleep_attempt": "2026-11-04T23:00:00+09:00",
                "latency_min": 10, "final_wake": "2026-11-05T07:00:00+09:00"})
        self.assertEqual(late["timing_class"], "primary_12h")

    def test_wrong_target_window_is_retained_as_excluded_and_withdrawn_token_is_closed(self):
        enrollment = self.create_and_enroll()
        with patch("researchbench.prospective_study._now", return_value=self.morning):
            excluded = self.study.submit_diary(enrollment["token"], {
                "night_date": "2026-11-03", "sleep_attempt": "2026-11-03T00:30:00+09:00",
                "latency_min": 10, "final_wake": "2026-11-03T07:00:00+09:00"})
            self.assertEqual(excluded["timing_class"], "excluded_outside_target_window")
            self.assertFalse(excluded["primary_revision"])
            self.study.withdraw(enrollment["token"])
            with self.assertRaisesRegex(ValueError, "no longer active"):
                self.study.submit_diary(enrollment["token"], {
                    "night_date": "2026-11-03", "missing_reason": "forgot"})

    def test_tick_has_no_caller_clock_and_uses_only_prior_received_history(self):
        enrollment = self.create_and_enroll()
        morning = datetime.fromisoformat("2026-11-04T08:00:00+09:00")
        diary = {"night_date": "2026-11-03", "sleep_attempt": "2026-11-03T23:00:00+09:00",
                 "latency_min": 10, "final_wake": "2026-11-04T07:00:00+09:00"}
        with patch("researchbench.prospective_study._now", return_value=morning), patch(
                "researchbench.prospective_study.ParticipantProspective.forecasts", return_value=[]):
            self.study.submit_diary(enrollment["token"], diary)
        seen = []

        def commit(_service, participant, config):
            seen.append((participant, config))
            return {"result": {"raw": {"idempotent_existing": False}}}

        evening = datetime.fromisoformat("2026-11-04T20:05:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=evening), patch(
                "researchbench.prospective_study.ParticipantProspective.commit", new=commit):
            result = self.study.tick()
        self.assertEqual(result["committed"], 1)
        self.assertEqual(len(seen[0][1]["records"]), 1)
        self.assertFalse(seen[0][1]["online_personalization"])
        self.assertEqual(seen[0][1]["profile"]["created_at"], "2026-11-03T12:00:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=morning):
            with self.assertRaisesRegex(ValueError, "20:00"):
                self.study.tick()

    def test_tick_blocks_changed_frozen_code_or_model_manifest(self):
        self.create_and_enroll()
        evening = datetime.fromisoformat("2026-11-04T20:05:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=evening), patch.object(
                self.study, "_manifest", return_value={"changed": "hash"}):
            with self.assertRaisesRegex(RuntimeError, "manifest changed"):
                self.study.tick()

    def test_tick_window_ends_at_exactly_201500(self):
        self.create_and_enroll()
        exact = datetime.fromisoformat("2026-11-04T20:15:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=exact), patch(
                "researchbench.prospective_study.ParticipantProspective.commit",
                return_value={"result": {"raw": {"idempotent_existing": False}}}):
            self.assertEqual(self.study.tick()["committed"], 1)
        late = datetime.fromisoformat("2026-11-04T20:15:00.000001+09:00")
        with patch("researchbench.prospective_study._now", return_value=late):
            with self.assertRaisesRegex(ValueError, "20:00"):
                self.study.tick()

    def test_two_enrollments_receive_isolated_forecast_inputs(self):
        first = self.create_and_enroll()
        with patch("researchbench.prospective_study._now", return_value=datetime.fromisoformat(
                "2026-11-03T12:01:00+09:00")):
            second = self.study.enroll({**BASELINE, "profile": {**PROFILE, "usual_bed": "00:30"}})
        seen = []

        def commit(_service, participant, config):
            seen.append((participant, config["profile"]["usual_bed"]))
            return {"result": {"raw": {"idempotent_existing": False}}}

        evening = datetime.fromisoformat("2026-11-04T20:05:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=evening), patch(
                "researchbench.prospective_study.ParticipantProspective.commit", new=commit):
            result = self.study.tick()
        self.assertEqual(result["committed"], 2)
        self.assertEqual({item[0] for item in seen}, {first["participant_id"], second["participant_id"]})
        self.assertEqual({item[1] for item in seen}, {"23:30", "00:30"})

    def test_withdraw_stops_future_due_nights_and_is_idempotent(self):
        enrollment = self.create_and_enroll()
        at = datetime.fromisoformat("2026-11-05T10:00:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=at):
            first = self.study.withdraw(enrollment["token"])
            second = self.study.withdraw(enrollment["token"])
            status = self.study.status()
        self.assertFalse(first["idempotent_existing"])
        self.assertTrue(second["idempotent_existing"])
        self.assertEqual(status["participants_withdrawn"], 1)
        self.assertEqual(status["nights_open_followup"], 2)
        self.assertEqual(status["nights_not_yet_due"], 0)

    def test_newly_elapsed_night_is_open_followup_not_prematurely_missing(self):
        self.create_and_enroll()
        just_after_midnight = datetime.fromisoformat("2026-11-04T00:01:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=just_after_midnight):
            status = self.study.status()
        self.assertEqual(status["nights_elapsed"], 1)
        self.assertEqual(status["nights_open_followup"], 1)
        self.assertEqual(status["nights_due"], 0)
        self.assertEqual(status["nights_absent_due"], 0)

    def test_summary_is_participant_weighted_and_empty_study_is_honest(self):
        self.create_and_enroll()
        empty = self.study.summary(bootstrap_replicates=100)
        self.assertEqual(empty["analysis_status"], "synthetic_rehearsal_not_validation")
        self.assertEqual(empty["metrics"], [])
        rows = []
        for participant, joint in (("a", 10.0), ("a", 20.0), ("b", 40.0)):
            rows.append({"participant": participant, "night": str(len(rows)), "arm": "data_cmi",
                         "age_group": "30-44", "sex": "unknown", "shift_work": "no",
                         "onset_min": joint, "wake_min": joint, "joint_min": joint,
                         "interval_min": joint, "onset_signed_min": joint, "wake_signed_min": joint,
                         "predicted_onset_min": 200 + joint, "predicted_wake_min": 700 + joint,
                         "observed_onset_min": 200, "observed_wake_min": 700,
                         "timing_class": "primary_12h", "study_day": 8})
        with patch.object(self.study, "_evaluation_rows", return_value=rows):
            result = self.study.summary(bootstrap_replicates=100, seed=3)
        metric = result["metrics"][0]
        self.assertEqual(metric["joint_min"], 27.5)  # mean(mean(10,20), mean(40))
        self.assertEqual(metric["n_participants"], 2)
        self.assertEqual(metric["joint_min_ci_level"], .975)

    def test_analysis_variants_preserve_initial_days_and_n1_ci_is_null(self):
        self.create_and_enroll()

        def row(day, joint, timing="primary_12h"):
            return {"participant": "one", "night": f"n{day}", "arm": "data_cmi",
                    "age_group": "30-44", "sex": "unknown", "shift_work": "no",
                    "onset_min": joint, "wake_min": joint, "joint_min": joint,
                    "interval_min": joint, "onset_signed_min": joint, "wake_signed_min": joint,
                    "predicted_onset_min": 200 + joint, "predicted_wake_min": 700 + joint,
                    "observed_onset_min": 200, "observed_wake_min": 700,
                    "timing_class": timing, "study_day": day}

        with patch.object(self.study, "_evaluation_rows",
                          return_value=[row(1, 5), row(8, 10), row(9, 20, "sensitivity_24h")]):
            result = self.study.summary(bootstrap_replicates=100)
        self.assertEqual(result["analyses"]["initial_7_days"]["n_nights"], 1)
        self.assertEqual(result["analyses"]["primary"]["n_nights"], 1)
        self.assertEqual(result["analyses"]["all_days"]["n_nights"], 2)
        self.assertEqual(result["analyses"]["receipt_24h_sensitivity"]["n_nights"], 2)
        metric = result["analyses"]["primary"]["metrics"][0]
        self.assertIsNone(metric["joint_min_ci_low"])
        self.assertIsNone(metric["joint_min_ci_high"])
        self.assertEqual(metric["joint_min_ci_status"], "insufficient_participants")

    def test_transient_outcome_failure_reconciles_with_original_receipt_time(self):
        enrollment = self.create_and_enroll()
        diary = {"night_date": "2026-11-03", "sleep_attempt": "2026-11-03T23:00:00+09:00",
                 "latency_min": 10, "final_wake": "2026-11-04T07:00:00+09:00"}
        forecast = {"night_id": "2026-11-03", "forecast_id": "f1"}
        with patch("researchbench.prospective_study._now", return_value=self.morning), patch(
                "researchbench.prospective_study.ParticipantProspective.forecasts",
                return_value=[forecast]), patch(
                "researchbench.prospective_study.ParticipantProspective.record_outcome",
                side_effect=OSError("temporary")):
            receipt = self.study.submit_diary(enrollment["token"], diary)
        self.assertTrue(receipt["forecast_status"].startswith("evaluation_failed_"))
        seen = []

        def record(_service, participant, forecast_id, outcome):
            seen.append(outcome)
            return {"result": {"raw": {"eligible": True}}}

        with patch("researchbench.prospective_study._now", return_value=datetime.fromisoformat(
                "2026-11-04T09:00:00+09:00")), patch(
                "researchbench.prospective_study.ParticipantProspective.forecasts",
                return_value=[forecast]), patch(
                "researchbench.prospective_study.ParticipantProspective.record_outcome", new=record):
            reconciled = self.study.reconcile()
        self.assertEqual(reconciled["reconciled"], 1)
        self.assertEqual(seen[0]["observed_at"], self.morning.isoformat())

    def test_immutable_aggregate_export_contains_no_identifiers(self):
        enrollment = self.create_and_enroll()
        target = Path(self.temp.name) / "export"
        exported = self.study.export(target, bootstrap_replicates=100)
        content = "".join(path.read_text(encoding="utf-8-sig") for path in exported.iterdir())
        self.assertNotIn(enrollment["participant_id"], content)
        self.assertNotIn(enrollment["token"], content)
        report = (exported / "REPORT.md").read_text(encoding="utf-8")
        for heading in ("primary", "initial_7_days", "all_days", "completers",
                        "receipt_24h_sensitivity"):
            self.assertIn(f"### {heading}", report)
        self.assertIn("Participant and night flow", report)
        self.assertIn("Signed bias direction", report)
        self.assertIn("Baseline distribution", report)
        self.assertTrue((exported / "baseline_distribution.csv").is_file())
        self.assertTrue((exported / "subgroup_descriptives.csv").is_file())
        aggregate = json.loads((exported / "aggregate_results.json").read_text(encoding="utf-8"))
        self.assertTrue(all(":" not in key and "\\" not in key
                            for key in aggregate["provenance"]["frozen_manifest"]))
        self.assertEqual(aggregate["baseline_distribution"]["n_participants"], 1)
        for variable, records in aggregate["baseline_distribution"]["variables"].items():
            self.assertEqual(len(records), 1)
            expected = "levels_not_exported" if variable == "recruitment_source" else "all_suppressed"
            self.assertEqual(records[0]["level"], expected)
            self.assertIsNone(records[0]["count"])
        for records in aggregate["subgroup_descriptives"]["variables"].values():
            self.assertEqual([row["level"] for row in records], ["all_suppressed"])
        with self.assertRaises(FileExistsError):
            self.study.export(target, bootstrap_replicates=100)

    def test_n6_complementary_categories_are_fully_suppressed(self):
        at = datetime.fromisoformat("2026-11-03T12:00:00+09:00")
        with patch("researchbench.prospective_study._now", return_value=at):
            self.study.create({}, synthetic=True)
            for index in range(6):
                baseline = {**BASELINE, "sex": "male" if index < 5 else "female"}
                self.study.enroll({**baseline, "profile": PROFILE})
        distribution = self.study._baseline_distribution()
        self.assertEqual(distribution["variables"]["sex"], [{
            "level": "all_suppressed", "count": None, "count_display": "suppressed",
            "suppressed_small_cell": True, "variable_suppressed": True,
        }])
        rows = []
        for index in range(6):
            rows.append({"participant": f"p{index}", "night": "n1", "arm": "data_cmi",
                         "joint_min": 10.0, "age_group": "30-44",
                         "sex": "male" if index < 5 else "female", "shift_work": "no"})
        subgroup = self.study._subgroup_descriptives(rows)
        self.assertEqual([item["level"] for item in subgroup["variables"]["sex"]],
                         ["all_suppressed"])
        self.assertIsNone(subgroup["variables"]["sex"][0]["n_participants"])

    def test_feasibility_phase_is_short_separate_and_never_claims_validation(self):
        with patch("researchbench.prospective_study._now", return_value=self.morning):
            created = self.study.create({"phase": "feasibility"}, synthetic=True)
            self.study.enroll({**BASELINE, "profile": PROFILE})
            status = self.study.status()
            summary = self.study.summary(bootstrap_replicates=100)
        self.assertTrue(created["synthetic_rehearsal"])
        self.assertEqual(status["protocol"]["duration_days"], 14)
        self.assertEqual(status["protocol"]["primary_days"], 7)
        self.assertEqual(status["protocol"]["minimum_primary_nights_for_completer"], 5)
        self.assertEqual(summary["analysis_status"], "synthetic_rehearsal_not_validation")
        self.assertEqual(summary["pooling_policy"], "separate_phase_never_pool")


if __name__ == "__main__":
    unittest.main()
