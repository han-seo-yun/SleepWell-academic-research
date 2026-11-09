from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from researchbench import academic_jobs


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "retrain_cmi_mesa", ROOT / "scripts" / "retrain_cmi_mesa.py"
)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


class PooledRetrainingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "cmi_mesa.sqlite3"
        self.database.write_bytes(b"fixture database")
        self.output = self.root / "result"

    def tearDown(self):
        self.temporary.cleanup()

    def test_dispatch_uses_pooled_trainer_and_records_real_input_provenance(self):
        envelope = {
            "mode": "pooled_retraining", "raw": {"evaluation": {}},
            "summary": [], "provenance": {}, "warnings": [],
        }
        config = cli.make_config(
            self.database, self.output, seed=20261104,
            bootstrap_replicates=100, max_seconds=30,
        )
        retrain = mock.Mock(return_value=envelope)
        module = types.ModuleType("researchbench.pooled_training")
        module.run_pooled_retraining = retrain
        with mock.patch.dict(sys.modules, {"researchbench.pooled_training": module}):
            result = academic_jobs.execute(config, self.root)
        supplied = retrain.call_args.args[0]
        self.assertEqual(supplied["database_path"], str(self.database.resolve()))
        self.assertEqual(supplied["output_dir"], str(self.output.resolve()))
        self.assertEqual(supplied["journal_path"], str(self.root / "prospective.sqlite3"))
        self.assertFalse(supplied["input_synthetic"])
        self.assertEqual(result["provenance"]["input_sha256"], config["data_hash"])
        self.assertEqual(result["provenance"]["input_source"], self.database.name)
        self.assertFalse(result["provenance"]["input_synthetic"])

    def test_cli_config_is_aggregate_project_safe_and_rejects_existing_output(self):
        config = cli.make_config(
            self.database, self.output, seed=7,
            bootstrap_replicates=5000, max_seconds=7200,
        )
        self.assertEqual(config["mode"], "pooled_retraining")
        self.assertEqual(config["seed"], 7)
        self.assertEqual(config["bootstrap_replicates"], 5000)
        self.assertEqual(config["max_seconds"], 7200)
        self.assertFalse(config["input_synthetic"])
        self.assertNotIn("records", config)
        self.assertNotIn("predictions", config)
        self.assertEqual(len(config["data_hash"]), 64)

        self.output.mkdir()
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            cli.make_config(
                self.database, self.output, seed=7,
                bootstrap_replicates=100, max_seconds=10,
            )

    def test_cli_config_rejects_missing_database_and_invalid_bootstrap(self):
        with self.assertRaises(FileNotFoundError):
            cli.make_config(
                self.root / "missing.sqlite3", self.output, seed=1,
                bootstrap_replicates=1, max_seconds=10,
            )
        with self.assertRaisesRegex(ValueError, "between 100 and 20000"):
            cli.make_config(
                self.database, self.output, seed=1,
                bootstrap_replicates=99, max_seconds=10,
            )
        with self.assertRaisesRegex(ValueError, "between 100 and 20000"):
            cli.make_config(
                self.database, self.output, seed=1,
                bootstrap_replicates=20001, max_seconds=10,
            )
        with self.assertRaisesRegex(ValueError, "max_seconds"):
            cli.make_config(
                self.database, self.output, seed=1,
                bootstrap_replicates=100, max_seconds=float("nan"),
            )

    def test_dispatch_rejects_database_changed_after_submission(self):
        config = cli.make_config(
            self.database, self.output, seed=1,
            bootstrap_replicates=100, max_seconds=10,
        )
        self.database.write_bytes(b"changed after submission")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            academic_jobs.execute(config, self.root)

    def test_cli_run_requires_a_new_project_path(self):
        project = self.root / "existing.swa"
        project.mkdir()
        with self.assertRaisesRegex(FileExistsError, "Project path already exists"):
            cli.run(
                self.database, self.output, project,
                bootstrap_replicates=100, max_seconds=10,
            )


if __name__ == "__main__":
    unittest.main()
