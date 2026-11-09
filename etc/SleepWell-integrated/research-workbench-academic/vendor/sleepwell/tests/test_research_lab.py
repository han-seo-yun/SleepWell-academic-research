import math
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fusion_forecast import PortableCohortModel, RUNTIME_MODEL_VERSION
from night_core import GRADUAL_POLICY, LEGACY_POLICY, ZONE
from research_lab import LabInputError, evaluate_run, model_status


MODELS = Path(__file__).resolve().parents[1] / "models"


def row(participant, day, onset=180.0, wake=660.0, delay_hours=1.0, labeled=True):
    cutoff = datetime(2026, 1, 1, 20, 0, tzinfo=ZONE) + timedelta(days=day)
    if not labeled:
        sleep_start = sleep_end = observed_at = None
    else:
        sleep_start_value = cutoff + timedelta(minutes=onset)
        sleep_end_value = cutoff + timedelta(minutes=wake)
        sleep_start = sleep_start_value.isoformat()
        sleep_end = sleep_end_value.isoformat()
        observed_at = (sleep_end_value + timedelta(hours=delay_hours)).isoformat()
    return {
        "participant_id": participant,
        "cutoff": cutoff.isoformat(),
        "sleep_start": sleep_start,
        "sleep_end": sleep_end,
        "observed_at": observed_at,
    }


def real_payload(records, cohorts=None, chart_participant_id=None):
    payload = {
        "schema_version": 1,
        "mode": "real",
        "cohorts": cohorts or ["cmi"],
        "records": records,
    }
    if chart_participant_id is not None:
        payload["chart_participant_id"] = chart_participant_id
    return payload


def synthetic_payload(**overrides):
    controls = {
        "n_nights": 5,
        "seed": 123,
        "start_date": "2026-01-01",
        "bed_hour": 23.5,
        "duration_hours": 8,
        "jitter_minutes": 30,
        "phase_shift_minutes": 90,
        "shift_after_night": 3,
        "missing_every": 0,
        "report_delay_hours": 1,
    }
    controls.update(overrides)
    return {
        "schema_version": 1,
        "mode": "synthetic",
        "cohorts": ["cmi"],
        "synthetic": controls,
    }


def prediction(result, model_id, participant, cutoff):
    return next(
        item
        for item in result["predictions"]
        if item["model_id"] == model_id
        and item["participant_id"] == participant
        and item["cutoff"] == cutoff
    )


class ResearchLabTests(unittest.TestCase):
    def test_gradual_policy_starts_at_first_available_record_and_matches_runtime(self):
        records = [row("p1", day, onset=120, wake=600) for day in range(16)]
        result = evaluate_run(real_payload(records), MODELS)
        self.assertEqual(result["personalization_policy"], GRADUAL_POLICY)
        self.assertEqual(result["runtime_model_version"], RUNTIME_MODEL_VERSION)
        history = [dict(source="research", sleep_start=r["sleep_start"],
                        wake=r["sleep_end"], observed_at=r["observed_at"]) for r in records]
        model = PortableCohortModel(MODELS / "robust_cmi_v2.json", "cmi")
        profile = {"created_at": records[0]["cutoff"]}
        for count in (0, 1, 7, 13, 14, 15):
            state = result["personalization"][count]["state"]
            self.assertAlmostEqual(state["personal_weight"], count / (count + 14))
            cutoff = datetime.fromisoformat(records[count]["cutoff"])
            expected = model.predict(profile, history, cutoff, personalization_policy=GRADUAL_POLICY)
            self.assertEqual(state, expected["personalization"])
            for arm in ("data", "hybrid"):
                actual = prediction(result, f"cmi.{arm}", "p1", cutoff.isoformat())
                self.assertEqual((actual["onset_offset_min"], actual["wake_offset_min"]),
                                 tuple(expected[arm]))
        self.assertNotEqual(result["personalization"][0]["state"]["phase_min"],
                            result["personalization"][1]["state"]["phase_min"])
        with mock.patch("research_lab.GRADUAL_POLICY", LEGACY_POLICY):
            legacy = evaluate_run(real_payload(records), MODELS)
        self.assertNotEqual(result["run_id"], legacy["run_id"])
        self.assertEqual(legacy["personalization"][13]["state"]["personal_weight"], 0)
        self.assertEqual(legacy["personalization"][14]["state"]["personal_weight"], .5)

    def test_seeded_generator_is_deterministic_and_independent_truth_is_labeled(self):
        payload = synthetic_payload(missing_every=3)
        first = evaluate_run(payload, MODELS)
        second = evaluate_run(payload, MODELS)
        self.assertEqual(first["evidence_label"], "synthetic_demo")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["predictions"], second["predictions"])
        self.assertEqual(first["personalization"], second["personalization"])
        self.assertEqual(first["summary"]["rows"], 5)
        self.assertEqual(first["summary"]["labeled_nights"], 4)
        data_rows = [
            item for item in first["predictions"] if item["model_id"] == "cmi.data"
        ]
        self.assertEqual(sum(item["actual_onset_offset_min"] is None for item in data_rows), 1)
        self.assertTrue(
            all(
                item["actual_onset_offset_min"] is None
                or item["actual_wake_offset_min"] > item["actual_onset_offset_min"]
                for item in data_rows
            )
        )

    def test_installed_artifacts_are_loaded_and_used_for_both_cohorts(self):
        status = model_status(MODELS)
        self.assertTrue(status["available"])
        for cohort in ("cmi", "mesa"):
            self.assertTrue(status["cohorts"][cohort]["loaded"])
            self.assertEqual(len(status["cohorts"][cohort]["sha256"]), 64)
        payload = synthetic_payload(n_nights=1, shift_after_night=1)
        payload["cohorts"] = ["cmi", "mesa"]
        result = evaluate_run(payload, MODELS)
        ids = {item["id"] for item in result["models"]}
        self.assertEqual(
            ids,
            {
                "cmi.physical_fixed",
                "cmi.physical_personal",
                "cmi.data",
                "cmi.hybrid",
                "mesa.physical_fixed",
                "mesa.physical_personal",
                "mesa.data",
                "mesa.hybrid",
                "baseline.recent7",
            },
        )
        artifact_models = [
            item for item in result["models"] if item["id"] != "baseline.recent7"
        ]
        self.assertTrue(all(len(item["artifact_sha256"]) == 64 for item in artifact_models))
        numeric_predictions = [
            item for item in result["predictions"] if item["model_id"] != "baseline.recent7"
        ]
        self.assertTrue(
            all(math.isfinite(item["onset_offset_min"]) for item in numeric_predictions)
        )

    def test_missing_outcomes_remain_unavailable_and_never_become_zero_error(self):
        record = row("p1", 0, labeled=False)
        result = evaluate_run(real_payload([record]), MODELS)
        self.assertEqual(result["evidence_label"], "retrospective_user_input_replay")
        self.assertEqual(result["summary"]["labeled_nights"], 0)
        self.assertEqual(result["summary"]["common_nights"], 0)
        baseline = prediction(
            result, "baseline.recent7", "p1", record["cutoff"]
        )
        self.assertIsNone(baseline["onset_offset_min"])
        for item in result["predictions"]:
            self.assertIsNone(item["actual_onset_offset_min"])
            self.assertIsNone(item["actual_wake_offset_min"])
            self.assertIsNone(item["actual_interval_min"])
        for collection in (result["metrics"], result["available_metrics"]):
            for metric in collection:
                self.assertEqual(metric["n"], 0)
                self.assertIsNone(metric["joint_mae_min"])

    def test_invalid_partial_naive_and_duplicate_csv_are_rejected(self):
        header = "participant_id,cutoff,sleep_start,sleep_end,observed_at\n"
        duplicate_line = (
            "p1,2026-01-01T20:00:00+09:00,"
            "2026-01-01T23:00:00+09:00,2026-01-02T07:00:00+09:00,"
            "2026-01-02T08:00:00+09:00\n"
        )
        payload = {
            "schema_version": 1,
            "mode": "real",
            "cohorts": ["cmi"],
            "csv_text": header + duplicate_line + duplicate_line,
        }
        with self.assertRaisesRegex(LabInputError, "Duplicate"):
            evaluate_run(payload, MODELS)

        partial = header + (
            "p1,2026-01-01T20:00:00+09:00,"
            "2026-01-01T23:00:00+09:00,,\n"
        )
        payload["csv_text"] = partial
        with self.assertRaisesRegex(LabInputError, "all explicit or all null"):
            evaluate_run(payload, MODELS)

        naive = row("p1", 0)
        naive["cutoff"] = "2026-01-01T20:00:00"
        with self.assertRaisesRegex(LabInputError, "offset"):
            evaluate_run(real_payload([naive]), MODELS)

        before_wake = row("p1", 0)
        before_wake["observed_at"] = before_wake["sleep_start"]
        with self.assertRaisesRegex(LabInputError, "at or after sleep_end"):
            evaluate_run(real_payload([before_wake]), MODELS)

    def test_mutating_same_or_future_outcome_cannot_change_prior_cutoff_prediction(self):
        original = [row("p1", 0, 180, 660), row("p1", 1, 210, 690)]
        mutated = [dict(item) for item in original]
        mutated[0] = row("p1", 0, 420, 900)
        mutated[1] = row("p1", 1, 60, 540)
        first = evaluate_run(real_payload(original), MODELS)
        second = evaluate_run(real_payload(mutated), MODELS)
        cutoff = original[0]["cutoff"]
        for model_id in (
            "cmi.physical_fixed",
            "cmi.physical_personal",
            "cmi.data",
            "cmi.hybrid",
            "baseline.recent7",
        ):
            left = prediction(first, model_id, "p1", cutoff)
            right = prediction(second, model_id, "p1", cutoff)
            for field in ("onset_offset_min", "wake_offset_min", "interval_min"):
                self.assertEqual(left[field], right[field])

    def test_participant_history_is_isolated_and_chart_selection_does_not_filter_metrics(self):
        participant_a = [row("a", 0, 180, 660), row("a", 1, 240, 720)]
        participant_b = [row("b", 0, 60, 540), row("b", 1, 480, 960)]
        alone = evaluate_run(real_payload(participant_a), MODELS)
        together = evaluate_run(
            real_payload(participant_a + participant_b, chart_participant_id="b"), MODELS
        )
        a_alone = [
            item for item in alone["predictions"] if item["participant_id"] == "a"
        ]
        a_together = [
            item for item in together["predictions"] if item["participant_id"] == "a"
        ]
        self.assertEqual(a_alone, a_together)
        state_alone = [
            item for item in alone["personalization"] if item["participant_id"] == "a"
        ]
        state_together = [
            item for item in together["personalization"] if item["participant_id"] == "a"
        ]
        self.assertEqual(state_alone, state_together)
        self.assertEqual(together["summary"]["chart_participant_id"], "b")
        self.assertEqual(together["summary"]["common_nights"], 2)

    def test_primary_metrics_use_identical_nights_and_participant_equal_macro(self):
        records = [
            row("a", 0, 180, 660),
            row("a", 1, 240, 700),
            row("a", 2, 360, 780),
            row("b", 0, 120, 600),
            row("b", 1, 300, 720),
        ]
        result = evaluate_run(real_payload(records), MODELS)
        self.assertEqual(result["summary"]["common_nights"], 3)
        for item in result["metrics"]:
            self.assertEqual(item["n"], 3)
            self.assertEqual(item["n_participants"], 2)
            self.assertEqual(item["averaging"], "participant_equal")

        model_id = "cmi.data"
        eligible = [
            item
            for item in result["predictions"]
            if item["model_id"] == model_id
            and prediction(
                result,
                "baseline.recent7",
                item["participant_id"],
                item["cutoff"],
            )["onset_offset_min"]
            is not None
        ]
        grouped = defaultdict(list)
        for item in eligible:
            grouped[item["participant_id"]].append(
                abs(item["onset_offset_min"] - item["actual_onset_offset_min"])
            )
        expected_macro = mean(mean(values) for values in grouped.values())
        metric = next(item for item in result["metrics"] if item["model_id"] == model_id)
        self.assertAlmostEqual(metric["onset_mae_min"], expected_macro)
        baseline_metric = next(
            item for item in result["metrics"] if item["model_id"] == "baseline.recent7"
        )
        self.assertAlmostEqual(
            metric["skill_vs_recent7"],
            1.0 - metric["joint_mae_min"] / baseline_metric["joint_mae_min"],
        )
        available = next(
            item for item in result["available_metrics"] if item["model_id"] == model_id
        )
        self.assertEqual(available["n"], 5)
        self.assertIsNone(available["skill_vs_recent7"])

    def test_fixed_physical_uses_fixed_phase_thresholds_but_personal_arm_activates(self):
        records = []
        for day in range(18):
            onset = 60.0 + (day % 3) * 30.0
            records.append(row("p1", day, onset, onset + 480.0))
        result = evaluate_run(real_payload(records), MODELS)
        first_cutoff = records[0]["cutoff"]
        fixed_first = prediction(result, "cmi.physical_fixed", "p1", first_cutoff)
        personal_first = prediction(result, "cmi.physical_personal", "p1", first_cutoff)
        self.assertEqual(
            (fixed_first["onset_offset_min"], fixed_first["wake_offset_min"]),
            (personal_first["onset_offset_min"], personal_first["wake_offset_min"]),
        )
        final_state = next(
            item
            for item in result["personalization"]
            if item["participant_id"] == "p1" and item["cutoff"] == records[-1]["cutoff"]
        )
        self.assertEqual(final_state["state"]["mode"], "personalized")
        self.assertGreater(final_state["state"]["personal_weight"], 0.0)
        self.assertEqual(final_state["state"]["tau_wake_h"], 18.2)
        self.assertEqual(final_state["state"]["tau_sleep_h"], 4.2)
        self.assertIn("current_s", final_state["features"])
        self.assertIn("current_c", final_state["features"])
        pairs = []
        for record in records[14:]:
            fixed = prediction(result, "cmi.physical_fixed", "p1", record["cutoff"])
            personal = prediction(result, "cmi.physical_personal", "p1", record["cutoff"])
            pairs.append(
                (fixed["onset_offset_min"], fixed["wake_offset_min"])
                != (personal["onset_offset_min"], personal["wake_offset_min"])
            )
        self.assertTrue(any(pairs))
        fixed_metadata = next(
            item for item in result["models"] if item["id"] == "cmi.physical_fixed"
        )
        self.assertEqual(fixed_metadata["label"], "위상·임계값 고정 물리")

    def test_missing_artifact_fails_closed_without_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            status = model_status(directory)
            self.assertFalse(status["available"])
            self.assertFalse(status["cohorts"]["cmi"]["loaded"])
            with self.assertRaisesRegex(LabInputError, "artifact is missing") as raised:
                evaluate_run(synthetic_payload(n_nights=1, shift_after_night=1), directory)
            self.assertEqual(raised.exception.code, "model_unavailable")

    def test_replay_uses_research_source_and_performs_no_file_writes(self):
        seen_sources = []
        original_predict = PortableCohortModel.predict

        def capture(model, profile, records, cutoff, **kwargs):
            seen_sources.extend(item.get("source") for item in records)
            self.assertEqual(kwargs["personalization_policy"], GRADUAL_POLICY)
            return original_predict(model, profile, records, cutoff, **kwargs)

        records = [row("p1", 0), row("p1", 1)]
        with mock.patch.object(PortableCohortModel, "predict", new=capture), mock.patch.object(
            Path, "write_text", side_effect=AssertionError("unexpected write_text")
        ), mock.patch.object(
            Path, "write_bytes", side_effect=AssertionError("unexpected write_bytes")
        ):
            evaluate_run(real_payload(records), MODELS)
        self.assertTrue(seen_sources)
        self.assertEqual(set(seen_sources), {"research"})


if __name__ == "__main__":
    unittest.main()
