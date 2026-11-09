import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from night_forecast import NightModel, NightModelError, NightForecasts
from night_core import (
    CONTRACT,
    DATA_FEATURES,
    GRADUAL_POLICY,
    LEGACY_POLICY,
    PHYSICAL_FEATURES,
    local_time,
)


def artifact():
    models = {}
    for arm in ("data", "hybrid", "hybrid_frozen"):
        names = list(DATA_FEATURES if arm == "data" else DATA_FEATURES + PHYSICAL_FEATURES)
        models[arm] = {"feature_order": names, "mean": [0.]*len(names), "scale": [1.]*len(names),
                       "intercepts": [180., 660.], "coefficients": [[0.]*len(names)]*2}
    return {"schema_version": 1, "artifact_type": "sleepwell_next_night_ridge",
            "feature_contract": CONTRACT, "status": "research_evaluated", "decision_time": "20:00",
            "cohort": "cmi", "version": "test", "requires_personal_archive_link": False,
            "supports_general_population": False, "runtime_eligibility": "research_only",
            "prior": {"bed_offset": 180., "wake_offset": 660.}, "models": models,
            "validation_winner": "data/ridge_10"}


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/"cmi_night_model.json"
        self.path.write_text(json.dumps(artifact()), encoding="utf-8")
        self.profile = {"created_at": "2026-10-01T10:00:00+09:00", "link_history": False}

    def tearDown(self):
        self.temp.cleanup()

    def test_decision_boundaries_and_target_night(self):
        bridge = NightForecasts(self.root)
        self.assertFalse(bridge.predict(self.profile, [], local_time("2026-10-20T19:59:00+09:00"))["available"])
        p = bridge.models["cmi"].predict(self.profile, [], local_time("2026-10-20T20:00:00+09:00"), research_context=True)
        self.assertTrue(p["available"])
        self.assertEqual(p["bed"], "2026-10-20T23:00:00+09:00")
        self.assertEqual(p["wake"], "2026-10-21T07:00:00+09:00")
        self.assertEqual(p["preferred_model"], "data")
        midnight = bridge.predict(self.profile, [], local_time("2026-10-21T00:00:00+09:00"))
        self.assertFalse(midnight["available"])
        self.assertEqual(midnight["next_decision_at"], "2026-10-21T20:00:00+09:00")

    def test_sensor_process_s_auto_selection_and_explicit_diary_mode(self):
        cutoff = local_time("2026-10-20T20:00:00+09:00")
        model = NightModel(self.path)
        baseline = model.predict(self.profile, [], cutoff, research_context=True)
        observed = model.predict(
            self.profile,
            [],
            cutoff,
            research_context=True,
            sensor_observations=[{
                "start": "2026-10-20T19:55:00+09:00",
                "end": "2026-10-20T20:00:00+09:00",
                "available_at": "2026-10-20T20:00:00+09:00",
                "sleep_binary": 0,
                "sleep_probability": 0.1,
                "probability_calibrated": True,
                "observation_model_id": "mesa_psg_activity_light_hgb_300s",
                "calibration_id": "validation_isotonic_20261028",
            }],
        )
        diary = model.predict(
            self.profile,
            [],
            cutoff,
            research_context=True,
            sensor_observations=[{
                "start": "2026-10-20T19:55:00+09:00",
                "end": "2026-10-20T20:00:00+09:00",
                "available_at": "2026-10-20T20:00:00+09:00",
                "sleep_binary": 0,
                "sleep_probability": 0.1,
                "probability_calibrated": True,
                "observation_model_id": "mesa_psg_activity_light_hgb_300s",
                "calibration_id": "validation_isotonic_20261028",
            }],
            process_s_mode="diary",
        )
        self.assertEqual(observed["bed"], baseline["bed"])
        self.assertEqual(observed["wake"], baseline["wake"])
        self.assertNotEqual(observed["feature_snapshot"]["current_s"],
                            baseline["feature_snapshot"]["current_s"])
        self.assertEqual(diary["feature_snapshot"], baseline["feature_snapshot"])
        paths = observed["personalization"]["process_s_paths"]
        self.assertEqual(paths["official_path"], "S_sensor_probability")
        self.assertEqual(paths["requested_mode"], "auto")
        self.assertEqual(paths["applied_path"], "S_sensor_probability")
        self.assertFalse(paths["audit"]["official_forecast_unchanged"])
        self.assertEqual(paths["recommendation"]["recommended_path"],
                         "S_sensor_probability")

    def test_direct_model_default_remains_legacy_and_gradual_starts_with_first_record(self):
        cutoff = local_time("2026-10-20T20:00:00+09:00")
        record = {
            "source": "diary",
            "bed": "2026-10-19T23:00:00+09:00",
            "wake": "2026-10-20T07:00:00+09:00",
            "observed_at": "2026-10-20T08:00:00+09:00",
        }
        model = NightModel(self.path)
        default = model.predict(self.profile, [record], cutoff, research_context=True)
        legacy = model.predict(
            self.profile,
            [record],
            cutoff,
            research_context=True,
            personalization_policy=LEGACY_POLICY,
        )
        gradual = model.predict(
            self.profile,
            [record],
            cutoff,
            research_context=True,
            personalization_policy=GRADUAL_POLICY,
        )
        self.assertEqual(default, legacy)
        self.assertEqual(default["model_version"], "test")
        self.assertNotIn("personalization_policy", default)
        self.assertEqual(default["personalization"]["personal_weight"], 0)
        self.assertEqual(gradual["model_version"], "test+gradual_v1")
        self.assertEqual(gradual["artifact_model_version"], "test")
        self.assertEqual(gradual["personalization_policy"], GRADUAL_POLICY)
        self.assertAlmostEqual(gradual["personalization"]["personal_weight"], 1 / 15)
        self.assertEqual(gradual["test_metrics"], {})
        self.assertEqual(
            gradual["test_metrics_scope"],
            "not_validated_for_gradual_v1_runtime",
        )
        self.assertEqual(
            default["predictions"]["hybrid_frozen"],
            gradual["predictions"]["hybrid_frozen"],
        )

    def test_service_bridge_explicitly_uses_gradual_policy(self):
        samsung = artifact()
        samsung.update(
            cohort="samsung",
            requires_personal_archive_link=True,
            runtime_eligibility="linked_personal_history",
        )
        (self.root / "samsung_night_model.json").write_text(
            json.dumps(samsung), encoding="utf-8"
        )
        profile = dict(self.profile, link_history=True)
        record = {
            "source": "diary",
            "bed": "2026-10-19T23:00:00+09:00",
            "wake": "2026-10-20T07:00:00+09:00",
            "observed_at": "2026-10-20T08:00:00+09:00",
        }
        result = NightForecasts(self.root).predict(
            profile, [record], local_time("2026-10-20T20:00:00+09:00")
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["personalization_policy"], GRADUAL_POLICY)
        self.assertEqual(result["model_version"], "test+gradual_v1")
        self.assertAlmostEqual(result["personalization"]["personal_weight"], 1 / 15)

    def test_invalid_file_has_no_silent_fallback(self):
        self.path.write_text("{}", encoding="utf-8")
        result = NightForecasts(self.root).predict(self.profile, [], local_time("2026-10-20T20:00:00+09:00"))
        self.assertFalse(result["available"])
        self.assertIn("계약", result["reason"])

    def test_personal_model_requires_explicit_link(self):
        a = artifact()
        a.update(cohort="samsung", requires_personal_archive_link=True, runtime_eligibility="linked_personal_history")
        self.path.write_text(json.dumps(a), encoding="utf-8")
        model = NightModel(self.path)
        self.assertFalse(model.predict(self.profile, [], local_time("2026-10-20T20:00:00+09:00"))["available"])

    def test_research_cohort_not_silently_served_to_new_users(self):
        result = NightForecasts(self.root).predict(self.profile, [], local_time("2026-10-20T20:00:00+09:00"))
        self.assertFalse(result["available"])
        self.assertIn("검증", result["reason"])

    def test_reject_nonfinite_or_wrong_feature_contract(self):
        a = artifact()
        a["models"]["hybrid"]["scale"][0] = float("nan")
        self.path.write_text(json.dumps(a), encoding="utf-8")
        with self.assertRaises(NightModelError):
            NightModel(self.path)

    def test_stored_old_predictions_remain_readable(self):
        from server import Store, Application
        from test_workflow import profile
        class FakeResearch:
            summary = {}
            metrics = None
            def behavioral_forecast(self, *args):
                return {"available": False, "reason": "fixture"}
        store = Store(self.root/"old.sqlite3")
        p = profile(local_time("2026-10-01T10:00:00+09:00"))
        store.save_profile(p, [])
        old = {"generated_at": "2026-10-19T20:00:00+09:00",
               "bed": "2026-10-19T23:00:00+09:00", "wake": "2026-10-20T07:00:00+09:00"}
        store.save_prediction(old)
        app = Application(store, FakeResearch(), NightForecasts(self.root))
        self.assertEqual(app.state()["recommendation"]["bed"], old["bed"])
        new = app.generate()
        self.assertIn("next_night_forecast", new)
        self.assertEqual(len(store.predictions()), 2)

    def test_post_cutoff_correction_preserves_earlier_available_version(self):
        from server import Store
        from night_core import normalize_history
        store = Store(self.root/"edits.sqlite3")
        row = {"date": "2026-10-20", "bed": "2026-10-19T23:00:00+09:00",
               "wake": "2026-10-20T07:00:00+09:00", "source": "diary",
               "observed_at": "2026-10-20T10:00:00+09:00"}
        store.save_record(row)
        store.save_record(dict(row, bed="2026-10-20T01:00:00+09:00",
                               observed_at="2026-10-20T21:00:00+09:00"))
        known = normalize_history(store.record_versions(), local_time("2026-10-20T20:00:00+09:00"))
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["bed_offset"], 180.)


if __name__ == "__main__":
    unittest.main()
