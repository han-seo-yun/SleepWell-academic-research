from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from researchbench.academic_backend import (
    AcademicRunCancelled,
    experiment_catalog,
    model_status,
    run_research,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "work" / "dayone-personalization-20261027" / "web-app"
VENDOR = ROOT / "vendor" / "sleepwell"


def synthetic_config(mode="retrospective"):
    return {
        "mode": mode,
        "input_mode": "synthetic",
        "cohorts": ["cmi", "mesa"],
        "synthetic": {"n_nights": 30, "seed": 17},
    }


def without_elapsed(value):
    copied = json.loads(json.dumps(value, ensure_ascii=False))
    copied.pop("elapsed_ms", None)
    return copied


class AcademicBackendTests(unittest.TestCase):
    def test_vendor_manifest_proves_every_copy_is_byte_identical(self):
        manifest = json.loads((VENDOR / "SOURCE_MANIFEST.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["canonical_source"], "work/dayone-personalization-20261027/web-app")
        self.assertGreaterEqual(len(manifest["files"]), 70)
        for item in manifest["files"]:
            source = ROOT.parent / item["source_path"]
            bundled = VENDOR / item["bundled_path"]
            self.assertEqual(source.read_bytes(), bundled.read_bytes(), item["bundled_path"])

    def test_catalog_and_model_status_cover_all_retrospective_arms(self):
        catalog = experiment_catalog()
        self.assertEqual([row["model_id"] for row in catalog["retrospective_arms"]], [
            "cmi.physical_fixed", "cmi.physical_personal", "cmi.data", "cmi.hybrid",
            "mesa.physical_fixed", "mesa.physical_personal", "mesa.data", "mesa.hybrid",
            "baseline.recent7",
        ])
        status = model_status()["status"]
        self.assertTrue(status["available"])
        self.assertEqual(status["personalization_policy"], "gradual_v1")
        self.assertTrue(all(status["cohorts"][cohort]["loaded"] for cohort in ("cmi", "mesa")))

    def test_adapter_raw_result_equals_direct_original_api_for_same_input(self):
        config = synthetic_config()
        adapter = run_research(config)
        payload = {"schema_version": 1, "mode": "synthetic", "cohorts": ["cmi", "mesa"],
                   "synthetic": config["synthetic"]}
        script = ("import json,sys; import research_lab; payload=json.load(sys.stdin); "
                  "json.dump(research_lab.evaluate_run(payload),sys.stdout,ensure_ascii=False,allow_nan=False)")
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=SOURCE, input=json.dumps(payload),
            text=True, encoding="utf-8", capture_output=True, check=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        direct = json.loads(completed.stdout)
        self.assertEqual(without_elapsed(adapter["raw"]), without_elapsed(direct))
        self.assertEqual(len(adapter["summary"]), 9)
        self.assertTrue(all(row["n_nights"] == adapter["raw"]["summary"]["common_nights"]
                            for row in adapter["summary"]))

    def test_personalization_mode_preserves_gradual_weight_rule(self):
        raw = run_research(synthetic_config("personalization"))["raw"]
        self.assertEqual(raw["personalization_policy"], "gradual_v1")
        for diagnostic in raw["personalization"]:
            n = diagnostic["history_count"]
            self.assertAlmostEqual(diagnostic["state"]["personal_weight"], n / (n + 14))
            self.assertEqual(diagnostic["state"]["personalization_policy"], "gradual_v1")

    def test_saved_report_is_explicitly_labeled_and_not_recomputed(self):
        result = run_research({"mode": "report", "report_type": "target_validity_validation_v1"})
        self.assertEqual(result["mode"], "report")
        self.assertEqual(result["raw"]["test_status"], "retrospective_reused_test")
        self.assertEqual(len(result["summary"]), 24)
        self.assertTrue(all(row["evidence"] == "stored_retrospective_evaluation"
                            for row in result["summary"]))
        self.assertIn("not recomputed", result["warnings"][0])

    def test_report_apis_execute_without_invented_metrics(self):
        audit = run_research({"mode": "report", "report_type": "measurement_audit", "records": [],
                              "as_of": "2026-11-01T20:00:00+09:00"})
        self.assertEqual(audit["raw"]["artifact_type"], "sleepwell_measurement_audit")
        self.assertEqual(audit["summary"], [])
        prior = run_research({"mode": "report", "report_type": "population_prior",
                              "profile": {"age": 40, "sex": "female", "activity_level": "moderate"}})
        self.assertEqual(prior["report_type"], "population_prior")
        self.assertEqual(prior["summary"], [])
        night = run_research({
            "mode": "report", "report_type": "night_model", "cohort": "cmi",
            "cutoff": "2026-11-01T20:00:00+09:00",
            "profile": {"created_at": "2026-11-01T20:00:00+09:00"}, "records": [],
        })
        self.assertTrue(night["raw"]["available"])
        self.assertEqual(set(night["raw"]["predictions"]),
                         {"physical", "data", "hybrid", "hybrid_frozen"})
        self.assertEqual(night["summary"], [])
        diagnostics = run_research({"mode": "report", "report_type": "research_diagnostics"})
        self.assertEqual(diagnostics["raw"]["status"], "available")
        self.assertEqual(len(diagnostics["raw"]["cohorts"]), 2)

    def test_cancel_is_checked_before_original_atomic_call(self):
        with self.assertRaises(AcademicRunCancelled):
            run_research(synthetic_config(), cancelled=lambda: True)

    def test_report_csv_mapping_is_explicit_nonempty_and_single_participant(self):
        header = "participant_id,cutoff,sleep_start,sleep_end,observed_at\n"
        first = ("p1,2026-01-01T20:00:00+09:00,2026-01-01T23:00:00+09:00,"
                 "2026-01-02T07:00:00+09:00,2026-01-02T08:00:00+09:00\n")
        second = ("p1,2026-01-02T20:00:00+09:00,2026-01-02T23:10:00+09:00,"
                  "2026-01-03T07:10:00+09:00,2026-01-03T08:10:00+09:00\n")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "canonical.csv"
            path.write_text(header + first + second, encoding="utf-8")
            result = run_research({
                "mode": "report", "report_type": "measurement_audit",
                "csv_file": str(path), "record_source": "diary",
                "as_of": "2026-01-04T20:00:00+09:00",
            })
            self.assertEqual(result["raw"]["record_validation"]["input"], 2)
            self.assertEqual(result["raw"]["record_validation"]["valid_diary"], 2)
            mapping = result["provenance"]["field_mapping"]
            self.assertEqual(mapping["record_count"], 2)
            self.assertEqual(mapping["source_declared"], "diary")
            self.assertFalse(mapping["timestamp_values_changed"])
            path.write_text(header + first + second.replace("p1,", "p2,"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly one participant"):
                run_research({"mode": "report", "report_type": "target_validity",
                              "csv_file": str(path), "record_source": "diary",
                              "as_of": "2026-01-04T20:00:00+09:00"})


if __name__ == "__main__":
    unittest.main()

