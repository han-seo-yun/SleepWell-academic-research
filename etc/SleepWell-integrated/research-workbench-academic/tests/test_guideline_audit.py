import csv
import json
import tempfile
import unittest
from pathlib import Path

from researchbench.guideline_audit import GuidelineAuditService


class GuidelineAuditServiceTests(unittest.TestCase):
    def test_remaining_work_revision_preserves_previous_record(self):
        audit = self.service.create()
        tasks = {key: list(items) for key, items in audit["remaining_tasks"].items()}
        tasks["existing_data_and_code"] = ["Completed: evaluation and methods are attached."]
        revised = self.service.revise(audit["audit_id"], {"remaining_tasks":tasks}, assessed_by="Evidence review")
        self.assertEqual(revised["remaining_tasks"], tasks)
        self.assertNotEqual(self.service.get(audit["audit_id"])["remaining_tasks"], tasks)
        with self.assertRaisesRegex(ValueError, "invalid_remaining_tasks"):
            self.service.revise(revised["audit_id"], {"remaining_tasks":{"unknown":[]}}, assessed_by="Evidence review")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project.swa"
        self.project.mkdir()
        self.service = GuidelineAuditService(self.project)

    def tearDown(self):
        self.temporary.cleanup()

    def evaluation(self, **updates):
        payload = {
            "schema": "sleepwell.gradual_evaluation.v1",
            "policy": "gradual_v1",
            "comparator": "legacy14",
            "status": "retrospective_reused_holdout",
            "metrics": [{"cohort":"cmi", "split":"test", "policy":"gradual_v1",
                         "arm":"data", "weighting":"participant", "metric":"joint_mae_min",
                         "value":48.1, "CI":[45.0, 51.0],
                         "denominators":{"n_participants":44, "n_nights":673}}],
            "limitations": ["Previously exposed test set; exploratory reassessment."],
        }
        payload.update(updates)
        path = self.project / "evaluation.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_blank_catalog_is_pending_and_not_a_score(self):
        audit = self.service.create()
        self.assertEqual(52, len(audit["tripod"]["items"]))
        self.assertEqual(34, len(audit["probast"]["questions"]))
        self.assertEqual({"pending"}, {row["status"] for row in audit["tripod"]["items"]})
        self.assertEqual({"NI"}, {row["answer"] for row in audit["probast"]["questions"]})
        self.assertTrue(audit["not_a_score_or_certification"])
        result = self.service.check(audit["audit_id"])
        self.assertTrue(result["valid"])
        self.assertIn("performance_evaluation_not_attached", result["warnings"])

    def test_sleepwell_seed_is_explicit_and_retains_uncertainty(self):
        audit = self.service.create(template="sleepwell_fixed")
        self.assertEqual("High", next(row["judgment"] for row in audit["probast"]["overall_judgments"]
                                      if row["domain_id"] == "overall:evaluation_risk"))
        self.assertGreater(sum(row["status"] == "partially_satisfied"
                               for row in audit["tripod"]["items"]), 20)
        self.assertEqual("NI", next(row["answer"] for row in audit["probast"]["questions"]
                                    if row["phase"] == "development" and row["item_id"] == "1.3"))
        item = next(row for row in audit["tripod"]["items"] if row["item_id"] == "23a")
        self.assertTrue(item["remaining_action"])
        self.assertEqual("Performance and uncertainty", item["label_en"])
        seed = next(row for row in audit["sources"] if row["id"] == "SLEEPWELL_AUDIT_20261103")
        self.assertEqual(64, len(seed["sha256"]))
        self.assertEqual(52, len(seed["snapshot"]["tripod"]))

    def test_imported_evaluation_is_linked_without_erasing_reused_holdout_limits(self):
        audit = self.service.create(self.evaluation(), template="sleepwell_fixed")
        for item_id in ("23a", "24"):
            row = next(row for row in audit["tripod"]["items"] if row["item_id"] == item_id)
            self.assertIn("GRADUAL_V1_EVALUATION", row["evidence_ids"])
            self.assertEqual("partially_satisfied", row["status"])
            self.assertIn("새 참여자", row["remaining_action"])
        signal = next(row for row in audit["probast"]["questions"]
                      if row["phase"] == "evaluation" and row["item_id"] == "4.7")
        self.assertIn("GRADUAL_V1_EVALUATION", signal["evidence_ids"])
        domain = next(row for row in audit["probast"]["domains"] if row["domain_id"] == "evaluation:4")
        self.assertEqual("High", domain["judgment"])

    def test_evaluation_snapshot_contract_and_sensitive_rows(self):
        audit = self.service.create(self.evaluation())
        self.assertEqual("gradual_v1", audit["evaluation"]["snapshot"]["policy"])
        self.assertEqual(64, len(audit["evaluation"]["source_sha256"]))
        with self.assertRaisesRegex(ValueError, "evaluation_policy"):
            self.service.create(self.evaluation(policy="legacy14"))
        with self.assertRaisesRegex(ValueError, "row_identifiers"):
            self.service.create(self.evaluation(participant_ids=["secret-person"]))

    def test_revision_is_immutable_and_records_human_reviewer(self):
        original = self.service.create()
        revised = self.service.revise(
            original["audit_id"],
            {"tripod":{"23a":{"status":"partially_satisfied",
                                "note":"MAE, CI and denominators are attached",
                                "evidence_ids":["TRIPOD_AI"]}},
             "probast":{"evaluation:4.5":{"answer":"PN", "rationale":"Reused exposed test data"}},
             "domains":{"evaluation:4":{"judgment":"High", "rationale":"Adaptive test reuse"}},
             "reason":"Evidence review"},
            assessed_by="Reviewer A",
        )
        unchanged = self.service.get(original["audit_id"])
        self.assertEqual("pending", next(row["status"] for row in unchanged["tripod"]["items"]
                                         if row["item_id"] == "23a"))
        self.assertEqual(original["audit_id"], revised["parent_audit_id"])
        self.assertEqual(2, revised["version"])
        self.assertEqual("Reviewer A", revised["review_history"][-1]["assessed_by"])
        self.assertEqual(2, len(self.service.list()))

    def test_na_is_restricted_to_officially_allowed_questions(self):
        audit = self.service.create()
        with self.assertRaisesRegex(ValueError, "na_not_allowed"):
            self.service.revise(audit["audit_id"], {"probast":{"development:1.1":{"answer":"NA"}}},
                                assessed_by="Reviewer")
        with self.assertRaisesRegex(ValueError, "na_rationale_required"):
            self.service.revise(audit["audit_id"], {"probast":{"development:4.4":{"answer":"NA"}}},
                                assessed_by="Reviewer")
        revised = self.service.revise(audit["audit_id"],
                                      {"probast":{"development:4.4":{"answer":"NA", "rationale":"Continuous regression; no classes."}}},
                                      assessed_by="Reviewer")
        self.assertTrue(self.service.check(revised["audit_id"])["valid"])

    def test_low_domain_with_unresolved_signals_warns_not_autopasses(self):
        audit = self.service.create()
        revised = self.service.revise(
            audit["audit_id"],
            {"domains":{"development:1":{"judgment":"Low", "rationale":"Reviewer judgment"}}},
            assessed_by="Reviewer",
        )
        result = self.service.check(revised["audit_id"])
        self.assertTrue(result["valid"])
        self.assertIn("low_judgment_with_unresolved_signals:development:1", result["warnings"])

    def test_overall_judgment_is_derived_and_labels_tasks_are_bilingual(self):
        korean = self.service.create(language="ko")
        self.assertEqual("제목", korean["tripod"]["items"][0]["label"])
        self.assertIn("새 참여자", korean["remaining_tasks"]["new_participants_or_data"][0])
        revised = self.service.revise(
            korean["audit_id"],
            {"domains":{"development:1":{"judgment":"High", "rationale":"Major selection concern"}}},
            assessed_by="Reviewer",
        )
        overall = next(row for row in revised["probast"]["overall_judgments"]
                       if row["domain_id"] == "overall:development_quality")
        self.assertEqual("High", overall["judgment"])
        with self.assertRaisesRegex(ValueError, "derived_overall_is_read_only"):
            self.service.revise(revised["audit_id"],
                                {"domains":{"overall:development_quality":{"judgment":"Low", "rationale":"invalid"}}},
                                assessed_by="Reviewer")
        english = self.service.create(language="en")
        self.assertEqual("Title", english["tripod"]["items"][0]["label"])
        self.assertIn("new participants", english["remaining_tasks"]["new_participants_or_data"][0])

    def test_tampering_is_detected(self):
        audit = self.service.create()
        path = self.service._path(audit["audit_id"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["title"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact_hash_mismatch"):
            self.service.get(audit["audit_id"])

    def test_document_attachment_is_snapshotted_linked_and_hash_checked(self):
        audit = self.service.create()
        document = self.project / "methods, reviewed.txt"
        document.write_text("reviewed methods evidence", encoding="utf-8")
        revised = self.service.attach_document(
            audit["audit_id"], document, "Methods evidence", assessed_by="Reviewer",
            item_links={"tripod":["7"], "probast":["development:2.2"]},
        )
        source = next(row for row in revised["sources"] if row["id"].startswith("EVIDENCE_"))
        self.assertTrue(Path(source["snapshot_path"]).exists())
        self.assertIn(source["id"], next(row for row in revised["tripod"]["items"]
                                         if row["item_id"] == "7")["evidence_ids"])
        self.assertTrue(self.service.check(revised["audit_id"])["valid"])
        Path(source["snapshot_path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_snapshot_hash_mismatch"):
            self.service.get(revised["audit_id"])

    def test_export_writes_all_formats_and_quotes_csv(self):
        audit = self.service.create(self.evaluation())
        revised = self.service.revise(
            audit["audit_id"],
            {"tripod":{"1":{"status":"partially_satisfied", "note":"comma, quote \" and newline\nkept"}}},
            assessed_by="Reviewer",
        )
        paths = self.service.export(revised["audit_id"])
        for name in ("json", "markdown", "tripod_csv", "probast_csv", "metrics_csv",
                     "diagnostics_csv", "paired_differences_csv", "docx", "pdf", "manifest"):
            self.assertGreater(Path(paths[name]).stat().st_size, 10)
        with Path(paths["tripod_csv"]).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual("comma, quote \" and newline\nkept", rows[0]["note"])
        markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
        self.assertIn("retrospective_reused_holdout", markdown)
        self.assertIn("new_participants_or_data", markdown)
        self.assertIn("Paired MAE reduction (legacy14 - gradual_v1; positive favors gradual_v1)", markdown)
        self.assertNotIn("Paired gradual_v1 minus legacy14 differences", markdown)
        self.assertEqual("47.485", self.service._human_number(47.484959238))
        with self.assertRaisesRegex(FileExistsError, "immutable_export_package_exists"):
            self.service.export(revised["audit_id"])


if __name__ == "__main__":
    unittest.main()
