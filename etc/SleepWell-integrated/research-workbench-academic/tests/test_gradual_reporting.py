from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from researchbench.gradual_reporting import evaluation_path
from researchbench.guideline_audit import GuidelineAuditService
from researchbench.reporting import ReportService


class GradualReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project.swa"
        self.project.mkdir()
        self.service = ReportService(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_no_run_needed_and_evidence_is_filled(self) -> None:
        report = self.service.create_gradual()
        metrics = [r for r in report["claims"] if r["evidence"]["kind"] == "gradual_metric"]
        self.assertEqual(report["run_refs"], [])
        self.assertEqual(len(metrics), 22)
        self.assertEqual([len(r["items"]) for r in report["checklists"]], [52, 34])
        self.assertNotIn("제공되지 않음", str(report["sections"]))
        joint = {(r["evidence"]["cohort"], r["evidence"]["arm"]): r for r in metrics
                 if r["evidence"]["weighting"] == "participant" and r["evidence"]["metric"] == "joint_mae_min"}
        self.assertAlmostEqual(joint[("cmi", "data")]["evidence"]["value"], 47.48534430733627)
        self.assertAlmostEqual(joint[("mesa", "data")]["evidence"]["value"], 68.11247089102953)
        self.assertEqual(self.service.check(report)["state"], "automatic_check_passed")

    def test_only_true_gaps_enter_remediation_and_external_route_uses_existing_data(self) -> None:
        report = self.service.create_gradual()
        items = [r for f in report["checklists"] for r in f["items"]]
        self.assertTrue(all("source_evidence_ids" in r for r in items))
        self.assertLess(len(report["remediation_gaps"]), len(items))
        satisfied = [r for r in report["checklists"][0]["items"] if r["source_status"] == "satisfied"]
        self.assertTrue(all(not r["remaining_action"] for r in satisfied))
        self.assertTrue(any(r["maintenance_action"] for r in satisfied))
        external = [r for r in items if r["gap_category"] == "external_existing_data" and r["remaining_action"]]
        self.assertTrue(external)
        self.assertTrue(all("외부 기관" in r["remaining_action"] for r in external))
        self.assertTrue(all("전향" in r["evidence_limit"] for r in external))

    def test_content_and_cross_hash_tampering_fail(self) -> None:
        report = self.service.create_gradual()
        changed = copy.deepcopy(report)
        changed.pop("artifact_sha256", None)
        changed["claims"][0]["text"] = "MAE 1.0"
        self.assertIn("gradual_claims_mismatch", {r["code"] for r in self.service.check(changed)["errors"]})
        changed = copy.deepcopy(report)
        changed.pop("artifact_sha256", None)
        changed["guideline_evidence"]["evaluation_snapshot_sha256"] = "0" * 64
        codes = {r["code"] for r in self.service.check(changed)["errors"]}
        self.assertIn("gradual_embedded_evaluation_snapshot_mismatch", codes)
        self.assertIn("current_gradual_evaluation_changed", codes)

    def test_blank_audit_cannot_be_selected(self) -> None:
        blank = GuidelineAuditService(self.project).create(evaluation_path(), template="blank")
        with self.assertRaisesRegex(ValueError, "audit_is_not_sleepwell_evidence_seeded"):
            self.service.create_gradual(audit_id=blank["audit_id"])

    def test_translation_and_create_dispatch_remain_valid(self) -> None:
        source = self.service.create([], template="gradual_guidelines", lang="ko")
        translated = self.service.translate(source["report_id"], "en")
        self.assertIn("Purpose and scope", [r["title"] for r in translated["sections"]])
        self.assertEqual(self.service.check(translated)["state"], "automatic_check_passed")


if __name__ == "__main__":
    unittest.main()
