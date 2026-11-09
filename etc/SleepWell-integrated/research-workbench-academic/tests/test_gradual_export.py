import csv
import json
import tempfile
import unittest
from pathlib import Path

from researchbench.report_export import _metric_rows, _redacted_report, _write_claims_csv


class GradualExportTests(unittest.TestCase):
    def test_shared_audit_hides_local_paths_without_mutating_original(self):
        report = {
            "template": "gradual_guidelines", "artifact_sha256": "original-hash", "run_refs": [],
            "guideline_evidence": {"audit_snapshot": {
                "sources": [{"source_path": "C:/Users/private-account/data/source.json", "sha256": "source-hash"}],
                "evaluation": {"source_path": "E:\\private\\evaluation.json"},
            }},
        }
        exported = _redacted_report(report)
        text = json.dumps(exported)
        self.assertNotIn("private-account", text)
        self.assertNotIn("E:", text)
        self.assertIn("source-hash", text)
        self.assertEqual(exported["source_artifact_sha256"], "original-hash")
        self.assertNotIn("artifact_sha256", exported)
        self.assertIn("not_reimportable", exported["export_view"])
        self.assertIn("private-account", report["guideline_evidence"]["audit_snapshot"]["sources"][0]["source_path"])

    def test_gradual_metrics_export_cohort_weighting_ci_and_source_locator(self):
        evidence = {"kind": "gradual_metric", "cohort": "cmi", "policy": "gradual_v1",
                    "arm": "data", "weighting": "participant", "metric": "joint_mae_min", "value": 47.49,
                    "ci95_low": 41.87, "ci95_high": 54.23, "unit": "min", "n_rows": 673,
                    "n_participants": 44, "evaluation_snapshot_sha256": "eval-hash", "metric_index": 2}
        report = {"claims": [{"claim_id": "metric", "claim_type": "gradual_metric_fact", "text": "metric", "evidence": evidence}]}
        self.assertEqual(_metric_rows(report)[0][0], "cmi.gradual_v1.data.participant")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claims.csv"
            _write_claims_csv(path, report)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
        self.assertEqual(row["ci95_low"], "41.87")
        self.assertEqual(row["weighting"], "participant")
        self.assertEqual(row["evaluation_snapshot_sha256"], "eval-hash")
        self.assertEqual(row["metric_index"], "2")


if __name__ == "__main__":
    unittest.main()
