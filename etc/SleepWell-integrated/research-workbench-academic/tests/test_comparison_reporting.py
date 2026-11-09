import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from researchbench.comparison_reporting import comparison_data_for_report
from researchbench.reporting import ReportService, _artifact_hash
from researchbench.result_adapter import adapt_run, digest


class ComparisonReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "study.swa"
        (self.project / "runs").mkdir(parents=True)
        self.record = self._record()
        self.run_path = self.project / "runs" / "run_compare.json"
        self.run_path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.view = adapt_run(self.record, "sleepwell", self.project)
        self.service = ReportService(self.project)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _record(predictions=None):
        keys = [("p1", "n1"), ("p1", "n2"), ("p2", "n1")]
        onset = {"a": [0, 0, 10], "b": [3, 3, 3], "c": [-3, -3, -3]}
        wake = {"a": [1, 1, 1], "b": [2, 2, 2], "c": [4, 4, 4]}
        interval = {"a": [4, 4, 4], "b": [1, 1, 1], "c": [2, 2, 2]}
        if predictions is None:
            predictions = []
            for model in ("a", "b", "c"):
                for index, (participant, cutoff) in enumerate(keys):
                    predictions.append({
                        "participant_id": participant, "cutoff": cutoff, "model_id": model,
                        "actual_onset_offset_min": 0, "onset_offset_min": onset[model][index],
                        "actual_wake_offset_min": 480, "wake_offset_min": 480 + wake[model][index],
                        "actual_interval_min": 480, "interval_min": 480 + interval[model][index],
                    })
        return {
            "id": "run_compare", "name": "Comparison fixture", "status": "completed",
            "config": {"mode": "report", "data_hash": "fixture-data", "input_synthetic": False,
                       "split": "reused_test", "participants": 2},
            "result": {"mode": "report", "summary": [], "raw": {"predictions": predictions},
                       "provenance": {"executed": True, "versions": {"fixture": "1"}}},
        }

    def selection(self, **updates):
        value = {
            "run_id": self.view["run_id"], "result_hash": self.view["result_hash"],
            "model_ids": ["a", "b"], "target": "onset",
            "averaging": "participant_equal", "tolerance": 2,
        }
        value.update(updates)
        return value

    def create(self, **updates):
        return self.service.create_comparison(self.view, self.selection(**updates), lang="ko")

    def assert_tamper_blocked(self, report, mutate, expected_code):
        tampered = copy.deepcopy(report)
        mutate(tampered)
        tampered["artifact_sha256"] = _artifact_hash(tampered)
        codes = {item["code"] for item in self.service.check(tampered)["errors"]}
        self.assertIn(expected_code, codes)

    def test_selected_subset_hand_calculation_and_nested_private_snapshot(self):
        before = self.run_path.read_bytes()
        report = self.create()
        evidence = report["comparison_evidence"]
        self.assertEqual(set(evidence), {"selection", "summary", "snapshot_sha256"})
        self.assertEqual(evidence["selection"], self.selection())
        rows = {row["model_id"]: row for row in evidence["summary"]["rows"]}
        self.assertEqual(set(rows), {"a", "b"})
        self.assertAlmostEqual(rows["a"]["mae"], 5.0)
        self.assertAlmostEqual(rows["b"]["mae"], 3.0)
        self.assertAlmostEqual(rows["a"]["within_tolerance"], 0.5)
        self.assertAlmostEqual(rows["b"]["within_tolerance"], 0.0)
        self.assertEqual(evidence["summary"]["n_nights"], 3)
        self.assertEqual(evidence["summary"]["n_participants"], 2)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('"participant_id"', encoded)
        self.assertNotIn('"predictions"', encoded)
        interpretation = next(s for s in report["sections"] if s["section_id"] == "interpretation")
        self.assertIn("b (3.00분)", interpretation["paragraphs"][0])
        self.assertIn("절충", interpretation["paragraphs"][2])
        self.assertEqual(self.service.check(report)["state"], "automatic_check_passed")
        self.assertEqual(before, self.run_path.read_bytes())

        regenerated = comparison_data_for_report(self.service, report)
        self.assertEqual(regenerated["model_ids"], ["a", "b"])
        self.assertTrue(regenerated["predictions"])

    def test_target_averaging_ties_and_metric_claim_contract(self):
        tied = self.create(model_ids=["b", "c"])
        interpretation = next(s for s in tied["sections"] if s["section_id"] == "interpretation")
        self.assertIn("고유 선두는 없다", interpretation["paragraphs"][0])
        claims = [c for c in tied["claims"] if c["evidence"]["kind"] == "comparison_metric"]
        self.assertEqual(len(claims), 8)
        self.assertEqual({c["evidence"]["metric"] for c in claims},
                         {"mae", "rmse", "bias", "within_tolerance_percent"})
        self.assertTrue(all(c["evidence"]["source_hash"] == self.view["result_hash"] for c in claims))

        wake = self.create(target="wake", averaging="night_equal", tolerance=1)
        rows = {r["model_id"]: r for r in wake["comparison_evidence"]["summary"]["rows"]}
        self.assertEqual(rows["a"]["mae"], 1)
        self.assertEqual(rows["a"]["within_tolerance"], 1)
        self.assertEqual(rows["b"]["mae"], 2)
        self.assertEqual(rows["b"]["within_tolerance"], 0)

        single = self.create(model_ids=["a"])
        single_text = " ".join(next(s for s in single["sections"]
                                    if s["section_id"] == "interpretation")["paragraphs"])
        self.assertIn("모델 간 순위·우열·차이 해석은 생략", single_text)
        self.assertEqual(self.service.check(single)["state"], "automatic_check_passed")

    def test_tamper_is_detected_even_when_outer_hash_is_recomputed(self):
        report = self.create()
        self.assert_tamper_blocked(
            report, lambda value: value["comparison_evidence"]["summary"]["rows"][0].update(mae=999),
            "comparison_evidence_invalid")
        self.assert_tamper_blocked(
            report, lambda value: value["sections"][4]["paragraphs"].append("b is universally best."),
            "comparison_narrative_mismatch")
        self.assert_tamper_blocked(
            report, lambda value: value["comparison_evidence"]["selection"].update(target="wake"),
            "comparison_evidence_invalid")
        self.assert_tamper_blocked(
            report, lambda value: value["run_refs"][0].update(selected_scope={"split": "forged"}),
            "comparison_evidence_invalid")
        self.assert_tamper_blocked(
            report, lambda value: value["run_refs"][0].update(source_file="forged.json"),
            "comparison_evidence_invalid")
        self.assert_tamper_blocked(
            report,
            lambda value: value["run_refs"][0].update(
                origin_source_file="forged.json", origin_project_path="forged", origin_verified_at_create=True),
            "comparison_evidence_invalid")

    def test_bad_selection_unknown_models_and_missing_predictions_are_rejected(self):
        for selection in (
            {**self.selection(), "extra": True},
            self.selection(model_ids=[]),
            self.selection(model_ids=["unknown"]),
        ):
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    self.service.create_comparison(self.view, selection)

        empty_record = self._record(predictions=[])
        empty_path = self.project / "runs" / "run_compare.json"
        empty_path.write_text(json.dumps(empty_record), encoding="utf-8")
        empty_view = adapt_run(empty_record, "sleepwell", self.project)
        with self.assertRaisesRegex(ValueError, "comparison_metrics_unavailable"):
            self.service.create_comparison(empty_view, {
                **self.selection(), "result_hash": digest(empty_record), "model_ids": ["a", "b"]
            })

    def test_scoped_guidance_statuses_and_translation_remain_valid(self):
        report = self.create()
        frameworks = {item["framework_id"]: item for item in report["checklists"]}
        self.assertEqual(set(frameworks), {"tripod_ai_comparison_reporting", "probast_ai_comparison_review"})
        tripod = frameworks["tripod_ai_comparison_reporting"]["items"]
        self.assertIn("tripod_ai_12e", {item["item_id"] for item in tripod})
        self.assertTrue(any(item["status"] == "judgment_pending" for item in tripod))
        self.assertTrue(all(item["status"] == "judgment_pending"
                            for item in frameworks["probast_ai_comparison_review"]["items"]))
        limitations = next(s for s in report["sections"] if s["section_id"] == "limitations")
        joined = " ".join(limitations["paragraphs"])
        self.assertIn("현재 자료", joined)
        self.assertIn("외부 자료", joined)
        translated = self.service.translate(report["report_id"], "en")
        self.assertEqual(self.service.check(translated)["state"], "automatic_check_passed")
        english = next(s for s in translated["sections"] if s["section_id"] == "tripod_probast")
        self.assertIn("does not establish", english["paragraphs"][0])

    def test_empty_versions_normalize_and_frozen_report_survives_project_move(self):
        self.record["result"]["provenance"]["versions"] = {}
        self.run_path.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.view = adapt_run(self.record, "sleepwell", self.project)
        report = self.create()
        self.assertIsNone(report["run_refs"][0]["model_versions"])
        self.assertNotIn("source_file", report["run_refs"][0])
        self.assertEqual(self.service.check(report)["state"], "automatic_check_passed")

        moved = self.project.parent / "moved-study.swa"
        shutil.copytree(self.project, moved)
        moved_service = ReportService(moved)
        restored = moved_service.get(report["report_id"])
        self.assertEqual(moved_service.check(restored)["state"], "automatic_check_passed")
        regenerated = comparison_data_for_report(moved_service, restored)
        self.assertEqual(regenerated["n_nights"], 3)
        self.assertEqual(regenerated["model_ids"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
