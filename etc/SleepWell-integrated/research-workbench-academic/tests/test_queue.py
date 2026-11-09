from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from researchbench.data import fingerprint, load_data
from researchbench.engine import make_split_plan
from researchbench.queueing import ExperimentQueue
from researchbench.storage import Project


ROOT = Path(__file__).resolve().parents[1]


def _config(df, *, model: str = "dummy", epochs: int = 10) -> dict:
    return {
        "id": "queue-template",
        "name": f"queue-{model}",
        "task": "classification",
        "target": "response",
        "features": ["age", "biomarker", "treatment", "site"],
        "model": model,
        "params": (
            {"epochs": epochs, "hidden_layer_sizes": [32, 16], "batch_size": 16}
            if model == "mlp"
            else {}
        ),
        "imputer": "median",
        "encoding": "onehot",
        "scaling": "standard",
        "positive_class": "yes",
        "threshold": 0.5,
        "average": "macro",
        "class_weight": None,
        "target_transform": "none",
        "device": "cpu",
        "primary_metric": "roc_auc",
        "data_hash": fingerprint(df),
        "seed": 19,
        "max_seconds": 30,
    }


def _wait_until(queue: ExperimentQueue, predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue.poll()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("queue condition was not reached before timeout")


class ExperimentQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.df = load_data(ROOT / "samples" / "classification_mixed.csv")
        self.project = Project.create(Path(self.folder.name) / "queue.rwb", self.df, "queue")

    def tearDown(self) -> None:
        if hasattr(self, "queue"):
            self.queue.close()
        self.project.close()
        self.folder.cleanup()

    def _queue_and_plan(self, config: dict) -> tuple[ExperimentQueue, dict]:
        plan = make_split_plan(
            self.df,
            {**config, "split": "random", "test_size": 0.2, "folds": 3, "seed": config["seed"]},
        )
        self.queue = ExperimentQueue(self.project)
        return self.queue, plan

    def test_child_process_completes_persists_and_poll_is_nonblocking(self) -> None:
        config = _config(self.df)
        queue, plan = self._queue_and_plan(config)
        experiment_id = queue.submit(config, plan)
        queue.poll()  # Starts the child process; process creation itself is not a UI tick assertion.
        self.assertIsNotNone(queue.active)

        longest_poll = 0.0
        deadline = time.monotonic() + 20.0
        while queue.busy and time.monotonic() < deadline:
            started = time.monotonic()
            queue.poll()
            longest_poll = max(longest_poll, time.monotonic() - started)
            time.sleep(0.02)
        self.assertFalse(queue.busy)
        self.assertLess(longest_poll, 0.1, "regular GUI polling was blocked")
        self.assertEqual(queue.records[experiment_id]["status"], "completed", queue.records[experiment_id])
        persisted = {record["id"]: record for record in self.project.experiments()}
        self.assertEqual(persisted[experiment_id]["status"], "completed")
        self.assertGreater(persisted[experiment_id]["summary"]["accuracy"]["n"], 0)

    def test_resolved_positive_class_is_immutable_through_queue_lifecycle(self) -> None:
        config = {**_config(self.df), "positive_class": None, "name": "resolve-positive"}
        queue, plan = self._queue_and_plan(config)
        experiment_id = queue.submit(config, plan)
        queued_config = queue.records[experiment_id]["config"]
        self.assertIsNotNone(queued_config["positive_class"])
        self.assertIn(queued_config["positive_class"], set(self.df["response"].dropna()))
        immutable_snapshot = json.loads(json.dumps(queued_config))

        _wait_until(queue, lambda: not queue.busy)
        self.assertEqual(queue.records[experiment_id]["status"], "completed")
        persisted = {record["id"]: record for record in self.project.experiments()}[experiment_id]
        self.assertEqual(persisted["config"], immutable_snapshot)
        self.assertEqual(persisted["config"], queue.records[experiment_id]["config"])

    def test_binned_binary_target_resolves_positive_before_persistence(self) -> None:
        config = {
            **_config(self.df),
            "name": "binned-biomarker",
            "target": "biomarker",
            "features": ["age", "treatment", "site"],
            "positive_class": None,
            "bin_edges": [0.0],
            "bin_rationale": "synthetic queue regression boundary",
        }
        queue, plan = self._queue_and_plan(config)
        experiment_id = queue.submit(config, plan)
        resolved = queue.records[experiment_id]["config"]
        self.assertIsNotNone(resolved["positive_class"])
        _wait_until(queue, lambda: not queue.busy)
        self.assertEqual(queue.records[experiment_id]["status"], "completed")
        persisted = {record["id"]: record for record in self.project.experiments()}[experiment_id]
        self.assertEqual(persisted["config"], resolved)

    def test_queued_job_can_be_cancelled_without_starting(self) -> None:
        config = _config(self.df)
        queue, plan = self._queue_and_plan(config)
        first = queue.submit(config, plan)
        second = queue.submit({**config, "name": "second"}, plan)
        queue.cancel(second)
        self.assertEqual(queue.records[second]["status"], "cancelled")
        self.assertEqual(len(queue.pending), 1)
        self.assertEqual(queue.pending[0][0]["id"], first)
        persisted = {record["id"]: record for record in self.project.experiments()}
        self.assertEqual(persisted[second]["status"], "cancelled")

    def test_active_cancellation_and_shutdown_wait_for_process_exit(self) -> None:
        config = _config(self.df, model="mlp", epochs=500)
        queue, plan = self._queue_and_plan(config)
        experiment_id = queue.submit(config, plan)
        queue.poll()
        process = queue.active["process"]
        queue.cancel(experiment_id)
        _wait_until(queue, lambda: queue.active is None)
        self.assertFalse(process.is_alive())
        self.assertEqual(queue.records[experiment_id]["status"], "cancelled")

        shutdown_id = queue.submit({**config, "name": "shutdown"}, plan)
        queue.poll()
        shutdown_process = queue.active["process"]
        queue.close()
        self.assertFalse(shutdown_process.is_alive())
        self.assertIsNone(queue.active)
        self.assertEqual(queue.records[shutdown_id]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
