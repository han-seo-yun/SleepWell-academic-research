import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import ZONE
from forecast_model import (
    ARTIFACT_TYPE, FEATURE_CONTRACT, FEATURE_ORDER, FEATURE_UNITS,
    MAX_ARTIFACT_BYTES, ForecastModelError, PortableForecastModel, completed_history_features,
)
from research import ResearchBridge, load_data_readiness


def artifact(**updates):
    n = len(FEATURE_ORDER)
    value = {
        "schema_version": 1,
        "artifact_type": ARTIFACT_TYPE,
        "model_version": "synthetic-contract-test",
        "status": "trained",
        "decision_time": "20:00",
        "timezone": "Asia/Seoul",
        "feature_contract": FEATURE_CONTRACT,
        "feature_order": list(FEATURE_ORDER),
        "feature_units": FEATURE_UNITS,
        "history_lookback_days": 28,
        "minimum_history_records": 2,
        "preprocessing": {"imputation_median": [0] * n, "mean": [0] * n, "scale": [1] * n},
        "targets": {
            "bedtime_offset_min": {"intercept": 180, "coefficients": [0] * n},
            "wake_offset_min": {"intercept": 660, "coefficients": [0] * n},
        },
        "population": {"source": "synthetic test cohort", "age_min": 18, "age_max": 39,
                       "occupations": ["student", "worker", "other"]},
        "release_gate": {"status": "evaluation_passed", "validation_protocol": "synthetic unit test only"},
    }
    value.update(updates)
    return value


def write_model(folder, value):
    path = Path(folder) / "model.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class PortableForecastModelTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime(2026, 10, 9, 20, tzinfo=ZONE)
        self.profile = {"age": 20, "occupation": "student"}
        self.records = []
        for days in (2, 1):
            wake = self.cutoff - timedelta(days=days, hours=13)
            bed = wake - timedelta(hours=8)
            self.records.append({"bed": bed.isoformat(), "wake": wake.isoformat(), "source": "diary",
                                 "observed_at": (wake + timedelta(hours=1)).isoformat()})

    def test_load_and_predict_from_completed_history(self):
        with tempfile.TemporaryDirectory() as td:
            model = PortableForecastModel.load(write_model(td, artifact()))
            result = model.predict(self.profile, self.records, self.cutoff)
        self.assertTrue(result["available"])
        self.assertEqual(datetime.fromisoformat(result["bed"]), self.cutoff + timedelta(minutes=180))
        self.assertEqual(datetime.fromisoformat(result["wake"]), self.cutoff + timedelta(minutes=660))
        self.assertEqual(result["role"], "과거 행동패턴 참고 · 권장 시각 계산에는 미사용")
        self.assertEqual(result["observed_history_records"], 2)

    def test_future_or_unfinished_record_is_excluded(self):
        records = list(self.records)
        records.append({"bed": (self.cutoff - timedelta(hours=1)).isoformat(),
                        "wake": (self.cutoff + timedelta(hours=7)).isoformat(), "source": "diary",
                        "observed_at": (self.cutoff - timedelta(minutes=1)).isoformat()})
        records.append({"bed": (self.cutoff - timedelta(days=3, hours=21)).isoformat(),
                        "wake": (self.cutoff - timedelta(days=3, hours=13)).isoformat(), "source": "archive",
                        "observed_at": (self.cutoff - timedelta(days=3, hours=12)).isoformat()})
        features = completed_history_features(records, self.cutoff)
        self.assertEqual(features["history_count_7"], 2)

    def test_observation_time_freshness_and_wake_date_deduplication(self):
        records = list(self.records)
        records.extend([
            {"bed": (self.cutoff - timedelta(days=3, hours=21)).isoformat(),
             "wake": (self.cutoff - timedelta(days=3, hours=13)).isoformat(), "source": "diary"},
            {"bed": (self.cutoff - timedelta(days=4, hours=21)).isoformat(),
             "wake": (self.cutoff - timedelta(days=4, hours=13)).isoformat(), "source": "diary",
             "observed_at": (self.cutoff + timedelta(minutes=1)).isoformat()},
            {"bed": (self.cutoff - timedelta(days=30, hours=21)).isoformat(),
             "wake": (self.cutoff - timedelta(days=30, hours=13)).isoformat(), "source": "diary",
             "observed_at": (self.cutoff - timedelta(days=30, hours=12)).isoformat()},
        ])
        latest = self.records[-1]
        records.append({"bed": (datetime.fromisoformat(latest["wake"]) - timedelta(hours=7)).isoformat(),
                        "wake": latest["wake"], "source": "diary",
                        "observed_at": (self.cutoff - timedelta(hours=1)).isoformat()})
        features = completed_history_features(records, self.cutoff)
        self.assertEqual(features["history_count_7"], 2)
        self.assertEqual(features["previous_sleep_duration_min"], 420)

    def test_wake_offset_uses_bed_offset_plus_duration(self):
        valid_wake = self.cutoff - timedelta(days=1, hours=13)
        valid = {"bed": (valid_wake - timedelta(hours=8)).isoformat(), "wake": valid_wake.isoformat(),
                 "source": "diary", "observed_at": (valid_wake + timedelta(hours=1)).isoformat()}
        long_wake = self.cutoff - timedelta(days=1, hours=23)
        invalid = {"bed": (long_wake - timedelta(hours=11)).isoformat(), "wake": long_wake.isoformat(),
                   "source": "diary", "observed_at": (long_wake + timedelta(hours=1)).isoformat()}
        features = completed_history_features([valid, invalid], self.cutoff)
        self.assertEqual(features["history_count_7"], 1)
        self.assertEqual(features["prior_bedtime_offset_median_7_min"], 180)
        self.assertEqual(features["prior_wake_offset_median_7_min"], 660)

    def test_population_and_history_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            model = PortableForecastModel.load(write_model(td, artifact()))
            self.assertFalse(model.predict({"age": 55, "occupation": "worker"}, self.records, self.cutoff)["available"])
            self.assertIn("연령 범위", model.predict({"age": 55, "occupation": "worker"}, self.records, self.cutoff)["reason"])
            self.assertFalse(model.predict(self.profile, self.records[:1], self.cutoff)["available"])
            self.assertIn("2회", model.predict(self.profile, self.records[:1], self.cutoff)["reason"])

    def test_rejects_unapproved_or_incompatible_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            bad_gate = artifact(release_gate={"status": "draft", "validation_protocol": "none"})
            with self.assertRaisesRegex(ForecastModelError, "evaluation_passed"):
                PortableForecastModel.load(write_model(td, bad_gate))
            old_ridge = artifact(artifact_type="ridge_residual")
            with self.assertRaisesRegex(ForecastModelError, "artifact_type"):
                PortableForecastModel.load(write_model(td, old_ridge))
            bad_units = artifact(feature_units=dict(FEATURE_UNITS, prior_duration_median_7_min="hours"))
            with self.assertRaisesRegex(ForecastModelError, "순서·단위"):
                PortableForecastModel.load(write_model(td, bad_units))

    def test_json_parser_rejects_duplicate_nonfinite_invalid_utf8_and_oversize(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "model.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(ForecastModelError, "중복 키"):
                PortableForecastModel.load(path)
            path.write_text(json.dumps(artifact()).replace('"intercept": 180', '"intercept": NaN'), encoding="utf-8")
            with self.assertRaisesRegex(ForecastModelError, "비표준 숫자"):
                PortableForecastModel.load(path)
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ForecastModelError, "UTF-8"):
                PortableForecastModel.load(path)
            path.write_bytes(b" " * (MAX_ARTIFACT_BYTES + 1))
            with self.assertRaisesRegex(ForecastModelError, "바이트 이하"):
                PortableForecastModel.load(path)

    def test_rejects_pathological_output(self):
        value = artifact()
        value["targets"]["bedtime_offset_min"]["intercept"] = 900
        value["targets"]["wake_offset_min"]["intercept"] = 600
        with tempfile.TemporaryDirectory() as td:
            model = PortableForecastModel.load(write_model(td, value))
            result = model.predict(self.profile, self.records, self.cutoff)
        self.assertFalse(result["available"])
        self.assertIn("범위", result["reason"])

    def test_research_bridge_prefers_valid_portable_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bridge = ResearchBridge(root, write_model(root, artifact()))
            now = self.cutoff + timedelta(hours=1)
            status = bridge.model_status(self.profile, self.records, now)
            result = bridge.behavioral_forecast(self.profile, self.records, now)
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["source"], "portable")
        self.assertTrue(result["available"])
        self.assertEqual(result["model_version"], "synthetic-contract-test")

    def test_research_bridge_reports_invalid_optional_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bridge = ResearchBridge(root, write_model(root, artifact(status="draft")))
            status = bridge.model_status(self.profile, self.records, self.cutoff)
        self.assertEqual(status["status"], "unavailable")
        self.assertIn("검증 실패", status["reason"])

    def test_model_status_waits_for_cutoff_and_propagates_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = write_model(root, artifact())
            bridge = ResearchBridge(root, path)
            before = bridge.model_status(self.profile, self.records, self.cutoff - timedelta(hours=1))
            self.assertEqual(before["status"], "waiting_for_decision")
            bad = artifact()
            bad["targets"]["bedtime_offset_min"]["intercept"] = 900
            bad["targets"]["wake_offset_min"]["intercept"] = 600
            bridge = ResearchBridge(root, write_model(root, bad))
            refused = bridge.model_status(self.profile, self.records, self.cutoff + timedelta(hours=1))
        self.assertEqual(refused["status"], "prediction_refused")
        self.assertIn("범위", refused["reason"])

    def test_mesa_inventory_exposes_only_bounded_aggregates(self):
        inventory = {
            "schema_version": 1,
            "scope": {"discovered_at_utc": "2026-10-09T01:00:00Z", "completed_at_utc": "2026-10-09T01:02:00Z",
                      "snapshot_file_count": 1630, "ending_file_count": 1637, "active_ingestion_detected": True, "integrity_claim": "snapshot only",
                      "participant_ids": ["must-not-leak"]},
            "inventory": {"total_bytes_at_discovery": 2770000000,
                          "modalities": {name: {"files": count, "unique_participant_ids": 999}
                                         for name, count in {"actigraphy": 1637, "heart_rate": 0, "steps": 0,
                                                             "psg_edf": 0, "psg_annotation": 0}.items()}},
            "headers": {"eligible_csv_files": 1637, "scanned_files": 100, "truncated": True,
                        "variants": [
                            {"columns": ["mesaid", "linetime", "activity", "wake", "interval", "offwrist", "private_extra"], "files": 99},
                            {"columns": None, "files": 1},
                        ]},
            "semantic_qc": {"sampling": {"stable_complete_files_in_aggregate": 25}, "rows": 50000,
                            "categories": {"wake": {"0": 30000, "1": 15000, "": 5000}},
                            "offwrist_flagged_fraction": 0.08, "thirty_second_cadence_fraction": 0.99},
            "interpretation": {"psg_supervised_stage_training_ready": False},
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "inventory.json"
            path.write_text(json.dumps(inventory), encoding="utf-8")
            result = load_data_readiness(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "ingestion_active")
        self.assertEqual(result["file_count"], 1630)
        self.assertEqual(result["ending_file_count"], 1637)
        self.assertNotIn("participant_ids", json.dumps(result))
        self.assertNotIn("private_extra", json.dumps(result))
        self.assertEqual(result["headers"]["unreadable_files"], 1)
        self.assertEqual(result["headers"]["schema_mismatch_files"], 99)
        self.assertEqual(result["headers"]["variants"][0]["unknown_columns"], 1)
        self.assertIn("subject-held-out sleep/wake baseline", result["usable_for"])
        self.assertIn("PSG-supervised 5-stage sleep model", result["blocked_for"])

    def test_mesa_inventory_allows_null_qc_fractions_without_stable_rows(self):
        value = {
            "schema_version": 1,
            "scope": {"discovered_at_utc": "2026-10-09T01:00:00Z", "completed_at_utc": None,
                      "snapshot_file_count": 1, "ending_file_count": 1, "active_ingestion_detected": True,
                      "integrity_claim": "no stable rows"},
            "inventory": {"total_bytes_at_discovery": 0,
                          "modalities": {name: {"files": 0} for name in
                                         ("actigraphy", "heart_rate", "steps", "psg_edf", "psg_annotation")}},
            "headers": {"eligible_csv_files": 0, "scanned_files": 0, "truncated": False, "variants": []},
            "semantic_qc": {"sampling": {"stable_complete_files_in_aggregate": 0}, "rows": 0,
                            "categories": {"wake": {}}, "offwrist_flagged_fraction": None,
                            "thirty_second_cadence_fraction": None},
            "interpretation": {"psg_supervised_stage_training_ready": False},
        }
        with tempfile.TemporaryDirectory() as td:
            result = load_data_readiness(write_model(td, value))
        self.assertTrue(result["available"])
        self.assertIsNone(result["quality"]["offwrist_fraction"])
        self.assertIsNone(result["quality"]["thirty_second_cadence_fraction"])


if __name__ == "__main__":
    unittest.main()
