import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fusion_forecast import (
    ARTIFACT_TYPE, MODEL_VERSION, PHYSICAL_PROVIDER, RUNTIME_MODEL_VERSION, SCHEMA_VERSION,
    FusionForecasts, PortableCohortModel, _pipeline_predict,
)
from night_core import CONTRACT, GRADUAL_POLICY, LEGACY_POLICY, local_time
from prospective import _fusion_snapshot
from robust_features import DATA_V2, FEATURE_CONTRACT, HYBRID_V2


def pipeline(names, onset=180.0, wake=660.0):
    size = len(names)
    return {
        "feature_order": list(names),
        "preprocessing": {
            "imputer_median": [0.0] * size,
            "missing_indicator_features": [],
            "clip_low": [-1.0e6] * size,
            "clip_high": [1.0e6] * size,
            "variance_keep": list(range(size)),
            "scale_mean": [0.0] * size,
            "scale": [1.0] * size,
        },
        "estimators": [
            {"kind": "linear", "intercept": onset, "coefficients": [0.0] * size},
            {"kind": "linear", "intercept": wake, "coefficients": [0.0] * size},
        ],
    }


def artifact(cohort):
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "feature_contract": FEATURE_CONTRACT,
        "model_version": MODEL_VERSION,
        "status": "research_comparison_only",
        "cohort": cohort,
        "decision_time": "20:00 Asia/Seoul",
        "target": "next main-sleep onset and final wake offsets",
        "physical_provider": PHYSICAL_PROVIDER,
        "prior": {"bed_offset": 180.0, "wake_offset": 660.0,
                  "n_people": 1, "n_nights": 1, "method": "fixture"},
        "models": {"data": pipeline(DATA_V2), "hybrid": pipeline(HYBRID_V2)},
        "source_model_sha256": {"data": "0" * 64, "hybrid": "1" * 64},
        "research_metrics": {"test": {}, "parity": {}},
        "provenance": {"study_cohort": cohort},
        "limitations": ["fixture"],
    }


class FusionForecastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for cohort in ("cmi", "mesa"):
            (self.root / f"robust_{cohort}_v2.json").write_text(
                json.dumps(artifact(cohort)), encoding="utf-8"
            )
        self.profile = {"created_at": "2026-10-01T10:00:00+09:00"}

    def tearDown(self):
        self.temp.cleanup()

    def test_six_separate_research_predictions_and_contract(self):
        result = FusionForecasts(self.root).predict_at(
            self.profile, [], local_time("2026-10-20T20:00:00+09:00")
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["feature_contract"], CONTRACT)
        self.assertEqual(result["model_feature_contract"], FEATURE_CONTRACT)
        self.assertEqual(result["physical_provider"], PHYSICAL_PROVIDER)
        self.assertEqual(result["model_version"], RUNTIME_MODEL_VERSION)
        self.assertEqual(result["personalization_policy"], GRADUAL_POLICY)
        self.assertNotEqual(result["model_version"], MODEL_VERSION)
        self.assertEqual(result["research_metrics"], {})
        self.assertEqual(
            result["research_metrics_scope"],
            "not_validated_for_gradual_v1_runtime",
        )
        self.assertEqual(set(result["predictions"]), {
            "physical_cmi", "data_cmi", "hybrid_cmi",
            "physical_mesa", "data_mesa", "hybrid_mesa",
        })
        for prediction in result["predictions"].values():
            bed = local_time(prediction["bed"])
            wake = local_time(prediction["wake"])
            self.assertAlmostEqual(
                (wake - bed).total_seconds() / 60, prediction["interval_min"]
            )
            self.assertAlmostEqual(
                prediction["wake_offset_min"] - prediction["onset_offset_min"],
                prediction["interval_min"],
            )
        self.assertEqual(set(result["artifacts"]), {"cmi", "mesa"})
        self.assertTrue(all(
            item["personalization_policy"] == GRADUAL_POLICY
            for item in result["artifacts"].values()
        ))
        self.assertNotIn("preferred_model", result)

    def test_portable_default_is_legacy_but_runtime_explicitly_uses_gradual(self):
        cutoff = local_time("2026-10-20T20:00:00+09:00")
        record = {
            "source": "diary",
            "bed": "2026-10-18T23:00:00+09:00",
            "wake": "2026-10-19T07:00:00+09:00",
            "observed_at": "2026-10-19T08:00:00+09:00",
        }
        portable = PortableCohortModel(self.root / "robust_cmi_v2.json", "cmi")
        default = portable.predict(self.profile, [record], cutoff)
        explicit_legacy = portable.predict(
            self.profile, [record], cutoff, personalization_policy=LEGACY_POLICY
        )
        gradual = portable.predict(
            self.profile, [record], cutoff, personalization_policy=GRADUAL_POLICY
        )
        self.assertEqual(default, explicit_legacy)
        self.assertEqual(default["personalization"]["personal_weight"], 0)
        self.assertAlmostEqual(gradual["personalization"]["personal_weight"], 1 / 15)
        runtime = FusionForecasts(self.root).predict_at(self.profile, [record], cutoff)
        self.assertEqual(runtime["personalization"]["cmi"], gradual["personalization"])
        with self.assertRaisesRegex(ValueError, "personalization policy"):
            portable.predict(
                self.profile, [record], cutoff, personalization_policy="unknown"
            )

    def test_status_labels_frozen_metrics_separately_from_runtime(self):
        status = FusionForecasts(self.root).status()
        self.assertEqual(status["model_version"], MODEL_VERSION)
        self.assertEqual(status["runtime_model_version"], RUNTIME_MODEL_VERSION)
        self.assertEqual(status["runtime_personalization_policy"], GRADUAL_POLICY)
        self.assertEqual(
            status["research_metrics_scope"], "frozen_legacy14_artifact_metrics"
        )

    def test_prospective_identity_separates_gradual_from_legacy_artifacts(self):
        cutoff = local_time("2026-10-20T20:00:00+09:00")
        result = FusionForecasts(self.root).predict_at(self.profile, [], cutoff)
        _, gradual_metadata = _fusion_snapshot(result, cutoff)
        legacy_result = json.loads(json.dumps(result))
        legacy_result["model_version"] = MODEL_VERSION
        legacy_result.pop("personalization_policy")
        for item in legacy_result["artifacts"].values():
            item.pop("personalization_policy")
        _, legacy_metadata = _fusion_snapshot(legacy_result, cutoff)
        self.assertNotEqual(
            gradual_metadata["artifact_identity_sha256"],
            legacy_metadata["artifact_identity_sha256"],
        )

    def test_twenty_hundred_gate(self):
        result = FusionForecasts(self.root).predict(
            self.profile, [], local_time("2026-10-20T19:59:59+09:00")
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["next_decision_at"], "2026-10-20T20:00:00+09:00")
        after_midnight = FusionForecasts(self.root).predict(
            self.profile, [], local_time("2026-10-21T00:00:00+09:00")
        )
        self.assertFalse(after_midnight["available"])
        self.assertEqual(after_midnight["next_decision_at"], "2026-10-21T20:00:00+09:00")

    def test_future_or_post_cutoff_record_is_excluded(self):
        cutoff = local_time("2026-10-20T20:00:00+09:00")
        eligible = {
            "source": "diary", "bed": "2026-10-18T23:00:00+09:00",
            "wake": "2026-10-19T07:00:00+09:00",
            "observed_at": "2026-10-19T08:00:00+09:00",
        }
        future = {
            "source": "diary", "bed": "2026-10-19T01:00:00+09:00",
            "wake": "2026-10-19T09:00:00+09:00",
            "observed_at": "2026-10-20T20:01:00+09:00",
        }
        bridge = FusionForecasts(self.root)
        known = bridge.predict_at(self.profile, [eligible], cutoff)
        contaminated = bridge.predict_at(self.profile, [eligible, future], cutoff)
        self.assertEqual(known["feature_snapshot"], contaminated["feature_snapshot"])
        self.assertEqual(known["predictions"], contaminated["predictions"])

    def test_corrupt_or_missing_cohort_fails_closed(self):
        (self.root / "robust_mesa_v2.json").write_text("{}", encoding="utf-8")
        bridge = FusionForecasts(self.root)
        self.assertFalse(bridge.status()["available"])
        result = bridge.predict_at(
            self.profile, [], local_time("2026-10-20T20:00:00+09:00")
        )
        self.assertFalse(result["available"])
        self.assertNotIn("predictions", result)
        (self.root / "robust_mesa_v2.json").unlink()
        self.assertFalse(FusionForecasts(self.root).status()["available"])

    def test_rejects_duplicate_keys_and_bad_tree(self):
        path = self.root / "robust_cmi_v2.json"
        path.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "중복"):
            PortableCohortModel(path, "cmi")
        payload = artifact("cmi")
        payload["models"]["data"]["estimators"][0] = {
            "kind": "hist_gradient_boosting", "baseline": 0.0,
            "trees": [[{"feature": 0, "threshold": 0.0, "missing_left": True,
                         "left": 0, "right": 0}]],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "순환"):
            PortableCohortModel(path, "cmi")


@unittest.skipUnless(os.environ.get("SLEEPWELL_ROBUST_ANALYSIS"),
                     "set SLEEPWELL_ROBUST_ANALYSIS for original sklearn parity")
class OriginalPipelineParityTests(unittest.TestCase):
    def test_all_cached_and_held_test_rows_match_original_sklearn(self):
        import joblib
        import numpy as np
        import pandas as pd

        analysis = Path(os.environ["SLEEPWELL_ROBUST_ANALYSIS"])
        repository = Path(__file__).resolve().parents[2]
        tools_model = repository / "tools" / "model"
        sys.path.insert(0, str(tools_model))
        try:
            models_dir = Path(__file__).resolve().parents[1] / "models"
            for cohort in ("cmi", "mesa"):
                portable = PortableCohortModel(
                    models_dir / f"robust_{cohort}_v2.json", cohort
                )
                feature_frame = pd.read_parquet(analysis / cohort / "features.parquet")
                test_rows = set(pd.read_csv(analysis / cohort / "test_data.csv")["row_id"].astype(int))
                held = feature_frame["row_id"].astype(int).isin(test_rows).to_numpy()
                for arm in ("data", "hybrid"):
                    original = joblib.load(analysis / cohort / f"{arm}.joblib")
                    names = portable.raw["models"][arm]["feature_order"]
                    matrix = feature_frame[names].to_numpy(float)
                    expected = original.predict(matrix)
                    actual = np.asarray([
                        _pipeline_predict(portable.raw["models"][arm], row._asdict())
                        for row in feature_frame[names].itertuples(index=False)
                    ])
                    self.assertLessEqual(float(np.max(np.abs(expected - actual))), 1e-9)
                    self.assertLessEqual(float(np.max(np.abs(expected[held] - actual[held]))), 1e-9)
        finally:
            sys.path.remove(str(tools_model))


if __name__ == "__main__":
    unittest.main()
