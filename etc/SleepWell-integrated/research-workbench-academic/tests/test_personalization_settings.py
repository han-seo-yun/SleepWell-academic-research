from __future__ import annotations

import csv
import importlib.util
import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compare_personalization_settings", ROOT / "scripts" / "compare_personalization_settings.py")
compare = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = compare
SPEC.loader.exec_module(compare)
ZONE = timezone(timedelta(hours=9))


def result_row(setting, participant, error, *, cohort="cmi", arm="data", split="validation"):
    return {
        "cohort": cohort, "participant_id": participant, "split": split, "arm": arm,
        "setting": setting, "joint_error": error, "bed_error": error, "wake_error": error,
        "eligible": 1, "cap_hits": 0, "clipped_values": 0, "history_values": 2,
    }


class PersonalizationSettingsTests(unittest.TestCase):
    def test_current_adjustment_matches_production_and_excludes_future_availability(self):
        adaptive, _ = compare._load_production_modules()
        cutoff = datetime(2026, 1, 10, 20, tzinfo=ZONE)
        history = [
            {"night_id": "old", "outcome_at": (cutoff - timedelta(days=2)).isoformat(),
             "available_at": (cutoff - timedelta(days=1)).isoformat(),
             "onset_residual_min": 75.0, "wake_residual_min": -45.0},
            {"night_id": "future", "outcome_at": (cutoff - timedelta(hours=1)).isoformat(),
             "available_at": (cutoff + timedelta(minutes=1)).isoformat(),
             "onset_residual_min": 999.0, "wake_residual_min": 999.0},
        ]
        actual = compare.adjustment(history, cutoff, compare.CURRENT)
        expected = adaptive.compute_adjustment(history, cutoff)
        self.assertAlmostEqual(actual["onset"], expected["onset_correction_min"])
        self.assertAlmostEqual(actual["wake"], expected["wake_correction_min"])
        self.assertEqual(actual["eligible"], 1)

    def test_setting_specific_clip_and_cap_change_correction(self):
        cutoff = datetime(2026, 1, 10, 20, tzinfo=ZONE)
        history = [{"night_id": "a", "outcome_at": (cutoff - timedelta(days=1)).isoformat(),
                    "available_at": (cutoff - timedelta(hours=12)).isoformat(),
                    "onset_residual_min": 1000.0, "wake_residual_min": -1000.0}]
        low_cap = compare.adjustment(history, cutoff, compare.replace(compare.CURRENT, correction_cap_min=1.0))
        high_cap = compare.adjustment(history, cutoff, compare.replace(compare.CURRENT, correction_cap_min=90.0))
        low_clip = compare.adjustment(history, cutoff, compare.replace(compare.CURRENT, residual_clip_min=10.0))
        self.assertEqual((low_cap["onset"], low_cap["wake"]), (1.0, -1.0))
        self.assertGreater(high_cap["onset"], low_cap["onset"])
        self.assertLess(low_clip["onset"], high_cap["onset"])

    def test_validation_selection_never_reads_test_values(self):
        rows = []
        for cohort in compare.COHORTS:
            for arm in compare.ARMS:
                rows.append(result_row(compare.CURRENT_ID, f"{cohort}-{arm}", 10.0,
                                       cohort=cohort, arm=arm))
                for setting in compare.CANDIDATES:
                    error = 8.0 if setting.id == "half_life_7" else 10.0
                    rows.append(result_row(setting.id, f"{cohort}-{arm}", error,
                                           cohort=cohort, arm=arm))
                rows.append(result_row("half_life_7", f"test-{cohort}-{arm}", 9999.0,
                                       cohort=cohort, arm=arm, split="test"))
        selected, _, gate = compare.choose_candidate(rows)
        self.assertEqual(selected, "half_life_7")
        self.assertTrue(gate)

    def test_participant_macro_weights_participants_equally(self):
        rows = [result_row(compare.CURRENT_ID, "many", 0.0),
                result_row(compare.CURRENT_ID, "many", 0.0),
                result_row(compare.CURRENT_ID, "one", 100.0)]
        cells = compare.participant_values(rows, "validation", compare.CURRENT_ID)
        self.assertEqual(compare.mean(cells[("cmi", "data")].values()), 50.0)

    def test_load_rows_rejects_participant_split_overlap(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            input_path, prediction_path = base / "input.csv", base / "pred.csv"
            input_columns = ["cohort", "participant_id", "split", "row_id", "cutoff", "bed", "wake",
                             "observed_at", "bed_offset", "wake_offset"]
            cutoff = datetime(2026, 1, 1, 20, tzinfo=ZONE)
            with input_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, input_columns)
                writer.writeheader()
                for index, split in enumerate(("train", "test")):
                    row_cutoff = cutoff + timedelta(days=index)
                    writer.writerow({"cohort": "cmi", "participant_id": "same", "split": split,
                                     "row_id": index, "cutoff": row_cutoff.isoformat(),
                                     "bed": (row_cutoff + timedelta(hours=2)).isoformat(),
                                     "wake": (row_cutoff + timedelta(hours=10)).isoformat(),
                                     "observed_at": (row_cutoff + timedelta(hours=10)).isoformat(),
                                     "bed_offset": 120, "wake_offset": 600})
            prediction_path.write_text("cohort,row_id,policy,arm,cutoff,predicted_bed_offset,predicted_wake_offset\n",
                                       encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "participant-disjoint"):
                compare.load_rows(input_path, prediction_path)

    def _write_minimal_contract(self, base, *, prediction_change=None):
        input_path, prediction_path = base / "input.csv", base / "pred.csv"
        input_columns = ["cohort", "participant_id", "split", "row_id", "cutoff", "bed", "wake",
                         "observed_at", "bed_offset", "wake_offset"]
        prediction_columns = ["cohort", "participant_id", "split", "row_id", "cutoff", "policy", "arm",
                              "bed_offset", "wake_offset", "predicted_bed_offset", "predicted_wake_offset"]
        input_rows, prediction_rows = [], []
        for index, cohort in enumerate(compare.COHORTS):
            cutoff = datetime(2026, 1, index + 1, 20, tzinfo=ZONE)
            source = {"cohort": cohort, "participant_id": f"p-{cohort}", "split": "validation",
                      "row_id": str(index), "cutoff": cutoff.isoformat(),
                      "bed": (cutoff + timedelta(hours=2)).isoformat(),
                      "wake": (cutoff + timedelta(hours=10)).isoformat(),
                      "observed_at": (cutoff + timedelta(hours=10)).isoformat(),
                      "bed_offset": 120, "wake_offset": 600}
            input_rows.append(source)
            for arm in compare.ARMS:
                prediction_rows.append({**source, "policy": "gradual_v1", "arm": arm,
                                        "predicted_bed_offset": 125, "predicted_wake_offset": 605})
        if prediction_change:
            prediction_change(prediction_rows[0])
        with input_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, input_columns, extrasaction="ignore")
            writer.writeheader(); writer.writerows(input_rows)
        with prediction_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, prediction_columns, extrasaction="ignore")
            writer.writeheader(); writer.writerows(prediction_rows)
        return input_path, prediction_path

    def test_load_rows_rejects_prediction_identity_target_and_finite_mismatch(self):
        cases = (
            (lambda row: row.update(participant_id="wrong"), "participant mismatch"),
            (lambda row: row.update(split="test"), "split mismatch"),
            (lambda row: row.update(bed_offset="121"), "target mismatch"),
            (lambda row: row.update(predicted_bed_offset="nan"), "non-finite"),
        )
        for mutation, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                paths = self._write_minimal_contract(Path(temp), prediction_change=mutation)
                with self.assertRaisesRegex(ValueError, message):
                    compare.load_rows(*paths)

    def test_protocol_rejects_candidate_value_tamper(self):
        protocol = json.loads(compare.DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        compare._protocol_matches(protocol)
        protocol["one_factor_alternatives"][0]["half_life_days"] = 8.0
        with self.assertRaisesRegex(ValueError, "candidate settings mismatch"):
            compare._protocol_matches(protocol)

    def test_bootstrap_reuses_participant_sample_across_arms(self):
        rows = []
        vectors = {"cmi": [0.0, 2.0], "mesa": [1.0, 3.0, 5.0]}
        for cohort, differences in vectors.items():
            for arm in compare.ARMS:
                for index, difference in enumerate(differences):
                    participant = f"{cohort}-{index}"
                    rows.append(result_row(compare.CURRENT_ID, participant, 10.0,
                                           cohort=cohort, arm=arm, split="test"))
                    rows.append(result_row("half_life_7", participant, 10.0 - difference,
                                           cohort=cohort, arm=arm, split="test"))
        replicates, seed = 200, 41
        _, aggregate = compare.bootstrap_gains(rows, "half_life_7", replicates=replicates, seed=seed)
        rng = random.Random(seed)
        expected = []
        for _ in range(replicates):
            cohort_draws = []
            for cohort in compare.COHORTS:
                values = vectors[cohort]
                indices = [rng.randrange(len(values)) for _ in values]
                cohort_draws.append(compare.mean(values[index] for index in indices))
            expected.append(compare.mean(cohort_draws))
        self.assertAlmostEqual(aggregate["ci95_low"], compare._quantile(expected, 0.025))
        self.assertAlmostEqual(aggregate["ci95_high"], compare._quantile(expected, 0.975))

    def test_bootstrap_rejects_arm_participant_misalignment(self):
        rows = []
        for cohort in compare.COHORTS:
            for arm in compare.ARMS:
                for participant in ("a", "b"):
                    rows.append(result_row(compare.CURRENT_ID, participant, 10.0,
                                           cohort=cohort, arm=arm, split="test"))
                    if not (cohort == "cmi" and arm == "hybrid" and participant == "b"):
                        rows.append(result_row("half_life_7", participant, 9.0,
                                               cohort=cohort, arm=arm, split="test"))
        with self.assertRaisesRegex(ValueError, "coverage mismatch|alignment mismatch"):
            compare.bootstrap_gains(rows, "half_life_7", replicates=10, seed=1)


if __name__ == "__main__":
    unittest.main()
