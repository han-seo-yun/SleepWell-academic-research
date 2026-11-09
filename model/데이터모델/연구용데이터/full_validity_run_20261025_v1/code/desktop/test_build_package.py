import copy
import json
from pathlib import Path
import tempfile
import unittest

from build_package import assert_exact_paths, validate_prior, WEB_FILES

PRIOR = Path(__file__).resolve().parent.parent / "package/SleepWell/app/web-app/models/population_prior.json"


class PackageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.prior = json.loads(PRIOR.read_text(encoding="utf-8"))

    def test_research_models_are_explicit_and_personal_artifacts_excluded(self):
        self.assertIn("research_console.py", WEB_FILES)
        self.assertIn("target_validity.py", WEB_FILES)
        self.assertIn("measurement_audit.py", WEB_FILES)
        self.assertIn("phone_baseline_evaluation.py", WEB_FILES)
        self.assertIn("full_validity_report.py", WEB_FILES)
        self.assertIn("models/full_validity_tables_v1.json", WEB_FILES)
        self.assertIn("models/target_validity_validation_v1.json", WEB_FILES)
        self.assertIn("static/research-console.js", WEB_FILES)
        self.assertIn("models/robust_cmi_v2.json", WEB_FILES)
        self.assertIn("models/robust_mesa_v2.json", WEB_FILES)
        self.assertFalse(any("samsung" in name.lower() for name in WEB_FILES))
        self.assertFalse(any(name.endswith((".joblib", ".pkl", ".db")) for name in WEB_FILES))

    def test_reviewed_aggregate_is_valid(self):
        validate_prior(self.prior)

    def test_private_top_level_and_group_fields_are_rejected(self):
        for field in ("participants", "records", "profile"):
            value = copy.deepcopy(self.prior)
            value[field] = [{"name": "synthetic-private-example"}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_prior(value)
        self.prior["groups"][0]["person_id"] = "synthetic"
        with self.assertRaises(ValueError):
            validate_prior(self.prior)

    def test_counts_durations_and_labels_are_validated(self):
        mutations = [
            ("n_people", True), ("n_people", -1), ("mean_sleep_min", float("nan")),
            ("mean_sleep_min", 1500), ("mean_sleep_min", True),
            ("age_group", "individual"), ("sex", "personal-name"),
        ]
        for field, value in mutations:
            data = copy.deepcopy(self.prior)
            data["groups"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_prior(data)

    def test_global_and_supported_age_schema_are_strict(self):
        cases = [
            ("global", {"n_people": 10, "mean_sleep_min": 450, "records": []}),
            ("global", {"n_people": False, "mean_sleep_min": 450}),
            ("supported_age", [18, 200]), ("groups", []),
        ]
        for key, value in cases:
            data = copy.deepcopy(self.prior)
            data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_prior(data)

    def test_exports_logs_and_other_extra_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "app.py").write_text("pass", encoding="utf-8")
            assert_exact_paths(directory, {"app.py"})
            for extra in ("records.json", "notes.txt", "backup.zip", "startup.log"):
                path = directory / extra
                path.write_text("synthetic", encoding="utf-8")
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    assert_exact_paths(directory, {"app.py"})
                path.unlink()

    def test_missing_expected_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                assert_exact_paths(Path(temporary), {"app.py"})


if __name__ == "__main__":
    unittest.main()
