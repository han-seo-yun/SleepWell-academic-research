import json
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from mobile_backup import receive, snapshots
from fusion_forecast import PortableCohortModel
from research_console import (
    _load_continuous_constants_validation,
    _load_personalization_validation,
    build_console,
    replay_feature_outputs,
)
from server import Application, Handler, Store


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-10-24T21:00:00+09:00")


def model_outcomes(weight=0.25):
    return {
        "schema_version": 1,
        "model_version": "robust-next-night-research-v2",
        "model_feature_contract": "robust_history_v2",
        "physical_provider": "two_process_fixed_v1",
        "forecast_status": "prospective_20_window",
        "input_coverage": {"diary_primary_records": 1, "wearable_auxiliary_records": 1},
        "personalization": {"mode": "recent_recency_shrinkage", "weight": weight},
        "research_predictions": {
            "physical_cmi": {"onset_offset_min": 100.0, "wake_offset_min": 650.0},
            "data_cmi": {"onset_offset_min": 110.0, "wake_offset_min": 660.0},
            "hybrid_cmi": {"onset_offset_min": 105.0, "wake_offset_min": 655.0},
        },
        "research_feature_snapshot": {"cmi": {"history_count_7": 1, "current_s": 0.6}},
        "research_personalization": {"cmi": {"personalization_weight": weight}},
        "research_artifacts": {"cmi": {"model_version": "robust-next-night-research-v2"}},
        "recommendation_feasibility": {"plan_feasible": True},
        "primary_observed_outcomes": {"n_records": 1},
        "baseline_observed_outcomes": {},
        "baseline_comparable": False,
        "wearable_auxiliary": {"n_records": 1},
    }


def payload(participant, day="2026-10-24", source="manual", malformed=False):
    record = {
        "id": "record-1", "day": day, "bedtime": "2026-10-23T23:00:00+09:00",
        "wake_time": "2026-10-24T07:00:00+09:00", "duration_minutes": 480,
        "rested": 4, "fatigue": 2, "source": source,
        "modified_at": "2026-10-24T08:00:00+09:00",
    }
    if malformed:
        record.pop("source")
    return {
        "schema_version": 1, "day": day, "revision": 1,
        "generated_at": f"{day}T20:30:00+09:00", "participant_key": participant,
        "data": {
            "profile": {"created_at": "2026-10-01T10:00:00+09:00"},
            "records": [record],
            "recommendation": {"generated_at": f"{day}T20:00:00+09:00", "duration_minutes": 480},
            "forecast": {"generated_at": f"{day}T20:00:00+09:00", "cmi_bed_offset": 100.0},
            "outcomes": {
                "record_count": 1, "primary_diary": {"record_count": 1},
                "wearable_auxiliary": {"record_count": 0}, "current_model": model_outcomes(),
                "prospective_forecasts": [], "realized_comparisons": [],
            },
        },
    }


def actual_replay_diagnostics():
    cutoff = datetime.fromisoformat("2026-10-24T20:00:00+09:00")
    profile = {"created_at": "2026-10-01T10:00:00+09:00"}
    result = {
        "model_version": "robust-next-night-research-v2",
        "model_feature_contract": "sleepwell_next_night_robust_v2",
        "physical_provider": "two_process_fixed_v1",
        "research_artifacts": {}, "research_feature_snapshot": {}, "research_predictions": {},
    }
    for cohort in ("cmi", "mesa"):
        model = PortableCohortModel(ROOT / "models" / f"robust_{cohort}_v2.json", cohort)
        prediction = model.predict(profile, [], cutoff)
        result["research_artifacts"][cohort] = {
            "cohort": cohort, "sha256": model.sha256,
            "model_version": model.raw["model_version"],
            "feature_contract": model.raw["feature_contract"],
            "physical_provider": model.raw["physical_provider"],
            "source_model_sha256": model.raw["source_model_sha256"],
        }
        result["research_feature_snapshot"][cohort] = prediction["features"]
        for arm in ("physical", "data", "hybrid"):
            onset, wake = prediction[arm]
            result["research_predictions"][f"{arm}_{cohort}"] = {
                "onset_offset_min": onset, "wake_offset_min": wake,
            }
    return result


class ResearchConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "sleepwell.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_real_artifact_metrics_and_no_pc_profile_required(self):
        result = build_console(self.store, ROOT / "models", NOW)
        self.assertEqual(result["research"]["status"], "available")
        self.assertEqual(result["phone"]["status"], "no_snapshots")
        cohorts = {item["cohort"]: item for item in result["research"]["cohorts"]}
        self.assertAlmostEqual(cohorts["cmi"]["test_metrics"]["data"]["joint_mae_min"], 47.50976161355656)
        self.assertAlmostEqual(cohorts["mesa"]["test_metrics"]["hybrid"]["joint_mae_min"], 68.23932002291205)
        self.assertIn("reused Validation and Test", cohorts["cmi"]["test_caveat"])
        validation = result["research"]["personalization_validation"]
        self.assertTrue(validation["available"])
        self.assertEqual(len(validation["cohorts"]), 4)
        self.assertNotIn("samsung", {item["cohort"] for item in validation["cohorts"]})
        cmi_data = next(item for item in validation["cohorts"]
                        if item["cohort"] == "cmi" and item["base_arm"] == "data")
        self.assertAlmostEqual(cmi_data["paired_improvement"]["joint"]["error_reduction_min"],
                               -0.07938274815335183)
        continuous = result["research"]["continuous_constants_validation"]
        self.assertTrue(continuous["available"])
        self.assertEqual({item["cohort"] for item in continuous["cohorts"]}, {"cmi", "mesa"})

    def test_continuous_constants_loader_keeps_aggregate_research_contract(self):
        path = Path(self.temp.name) / "continuous_constants_validation_v2.json"
        cohort = {
            "selected_policy": "daily", "n_people": 10, "n_nights": 50,
            "legacy_active_nights": 20, "daily_active_nights": 45, "max_history": 8,
            "scores": {
                "legacy": {"data": 50.0, "hybrid": 48.0, "physical": 90.0},
                "daily": {"data": 49.0, "hybrid": 47.0, "physical": 90.0},
            },
            "comparisons": {
                "data": {"gain_min": 1.0, "ci95_min": [-1.0, 3.0], "holm_p": 0.2},
                "hybrid": {"gain_min": 1.0, "ci95_min": [0.1, 2.0], "holm_p": 0.04},
            },
            "validation_gate": True, "validation_gain_min": 0.8,
            "physics_addition": {"gain_min": 2.0, "ci95_min": [0.5, 3.5]},
            "participant_predictions": [{"participant_id": "must-not-leak"}],
        }
        path.write_text(json.dumps({
            "schema_version": 1,
            "artifact_type": "sleepwell_continuous_constants_validation",
            "version": "2026-10-25-v2",
            "test_status": "retrospective_reused_test",
            "interpretation": "aggregate comparison",
            "cohorts": {"cmi": cohort, "mesa": cohort},
            "no_service_promotion": True,
        }), encoding="utf-8")
        result = _load_continuous_constants_validation(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "2026-10-25-v2")
        self.assertEqual(result["cohorts"][0]["daily_active_nights"], 45)
        self.assertNotIn("participant_predictions", result["cohorts"][0])
        self.assertTrue(result["no_service_promotion"])

    def test_missing_continuous_constants_validation_is_explicitly_unavailable(self):
        result = _load_continuous_constants_validation(Path(self.temp.name) / "missing.json")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "FileNotFoundError")

    def test_personalization_validation_loader_keeps_only_aggregate_contract(self):
        path = Path(self.temp.name) / "personalization_validation_v1.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "artifact_type": "sleepwell_personalization_validation",
            "generated_at": NOW.isoformat(),
            "evaluation_status": {"reused_test": True, "fresh_external_confirmation": False,
                                  "estimand": "online temporal update using only earlier resolved outcomes"},
            "prediction_target": "next main sleep onset and final wake",
            "correction_policy": {"minimum_history": 14, "service_promoted": False},
            "cohorts": {"cmi": {"data": {"base_arm_label": "frozen data",
                "n_people": 10, "n_nights": 50, "eligible_personalized_nights": 20, "coverage": 0.4,
                "all_test_rows_including_zero_correction": {
                    "base_metrics": {"joint_mae_min": 50.0},
                    "corrected_metrics": {"joint_mae_min": 48.0},
                    "paired_improvement": {"metrics": {"joint": {"error_reduction_min": 2.0, "ci95": [-1, 5]}}}},
                "history_strata": {}, "gap_strata": {}, "qc_strata": {},
                "participant_predictions": [{"participant_id": "must-not-leak"}]},
                "naive_recent_baseline": {}, "notes": []},
                "unknown": {"base_arm": "data"}},
            "limitations": ["reused Test"], "provenance": {}
        }, ensure_ascii=False), encoding="utf-8")
        result = _load_personalization_validation(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["cohorts"][0]["eligible_personalized_nights"], 20)
        self.assertNotIn("participant_predictions", result["cohorts"][0])
        self.assertIn("추가 온라인 잔차 보정 없음", result["interpretation"])

    def test_missing_personalization_validation_is_explicitly_unavailable(self):
        result = _load_personalization_validation(Path(self.temp.name) / "missing.json")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "FileNotFoundError")

    def test_http_route_does_not_require_profile(self):
        class Research:
            summary = {"available": False}
            records = []
            metrics = None

            def behavioral_forecast(self, *args):
                return {"available": False}

        app = Application(self.store, Research(), clock=lambda: NOW)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.application = app
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/research-console") as response:
                result = json.load(response)
            self.assertEqual(result["research"]["status"], "available")
            self.assertTrue(result["research"]["full_validity_report"]["available"])
            self.assertEqual(len(result["research"]["full_validity_report"]["panels"]),12)
            self.assertTrue(result["scope"]["read_only"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_participants_are_isolated_and_sources_preserved(self):
        first, second = "a" * 32, "b" * 32
        receive(self.store, "phone-a", payload(first, source="manual"), NOW)
        receive(self.store, "phone-b", payload(second, source="samsung_health"), NOW)
        result = build_console(self.store, ROOT / "models", NOW)
        self.assertEqual(result["phone"]["participant_count"], 2)
        participants = {(item["device_id"], item["participant_key"]): item for item in result["phone"]["participants"]}
        self.assertEqual(participants[("phone-a", first)]["latest"]["record_summary"]["source_roles"], {"diary_primary": 1})
        self.assertEqual(participants[("phone-b", second)]["latest"]["record_summary"]["source_roles"], {"wearable_auxiliary": 1})
        self.assertEqual(participants[("phone-a", first)]["latest"]["model_diagnostics"]["replay"]["status"], "unavailable")

    def test_android_user_entry_is_primary_but_unknown_source_is_not_inferred(self):
        receive(self.store, "phone-u", payload("1" * 32, source="USER_ENTRY"), NOW)
        receive(self.store, "phone-x", payload("2" * 32, source="csv_import"), NOW)
        result = build_console(self.store, ROOT / "models", NOW)
        roles = {item["participant_key"]: item["latest"]["record_summary"]["source_roles"]
                 for item in result["phone"]["participants"]}
        self.assertEqual(roles["1" * 32], {"diary_primary": 1})
        self.assertEqual(roles["2" * 32], {"other": 1})

    def test_stored_features_match_all_six_installed_model_outputs(self):
        replay = replay_feature_outputs(actual_replay_diagnostics(), ROOT / "models")
        self.assertEqual(replay["status"], "feature_outputs_match")
        self.assertEqual(set(replay["comparisons"]), {
            "physical_cmi", "data_cmi", "hybrid_cmi",
            "physical_mesa", "data_mesa", "hybrid_mesa",
        })
        self.assertTrue(all(item["matches"] for item in replay["comparisons"].values()))
        self.assertIn("does not validate feature derivation", replay["scope"])

    def test_tampered_phone_output_is_reported_as_arithmetic_mismatch(self):
        diagnostics = actual_replay_diagnostics()
        diagnostics["research_predictions"]["hybrid_mesa"]["wake_offset_min"] += 0.01
        replay = replay_feature_outputs(diagnostics, ROOT / "models")
        self.assertEqual(replay["status"], "feature_outputs_mismatch")
        self.assertFalse(replay["comparisons"]["hybrid_mesa"]["matches"])
        self.assertAlmostEqual(
            replay["comparisons"]["hybrid_mesa"]["absolute_delta_min"]["wake"], 0.01)

    def test_missing_or_mismatched_artifact_hash_disables_feature_replay(self):
        missing = actual_replay_diagnostics()
        missing["research_artifacts"]["cmi"].pop("sha256")
        self.assertEqual(replay_feature_outputs(missing, ROOT / "models")["status"], "unavailable")
        mismatch = actual_replay_diagnostics()
        mismatch["research_artifacts"]["mesa"]["sha256"] = "0" * 64
        self.assertEqual(replay_feature_outputs(mismatch, ROOT / "models")["status"], "unavailable")
        missing_output = actual_replay_diagnostics()
        missing_output["research_predictions"].pop("data_cmi")
        self.assertEqual(replay_feature_outputs(missing_output, ROOT / "models")["status"], "unavailable")

    def test_malformed_accepted_payload_is_rejected_from_analysis(self):
        participant = "c" * 32
        receive(self.store, "phone-c", payload(participant, malformed=True), NOW)
        result = build_console(self.store, ROOT / "models", NOW)
        self.assertEqual(result["phone"]["participant_count"], 0)
        self.assertEqual(result["phone"]["accepted_days"], 0)
        self.assertEqual(result["phone"]["rejected_snapshots"][0]["reason"], "record_missing_fields")

    def test_missing_optional_outcomes_retains_participant_without_claiming_replay(self):
        item = payload("e" * 32)
        item["data"]["outcomes"] = None
        receive(self.store, "phone-e", item, NOW)
        result = build_console(self.store, ROOT / "models", NOW)
        latest = result["phone"]["participants"][0]["latest"]
        self.assertFalse(latest["model_diagnostics"]["available"])
        self.assertEqual(latest["model_diagnostics"]["reporting_scope"],
                         "phone_reported_not_independently_replayed")

    def test_every_day_retains_its_reported_model_diagnostics(self):
        participant = "f" * 32
        receive(self.store, "phone-f", payload(participant, day="2026-10-23"), NOW)
        receive(self.store, "phone-f", payload(participant, day="2026-10-24"), NOW)
        days = build_console(self.store, ROOT / "models", NOW)["phone"]["participants"][0]["days"]
        self.assertEqual(len(days), 2)
        self.assertTrue(all(day["model_diagnostics"]["available"] for day in days))

    def test_android_export_preserves_adaptive_sidecar_and_immutable_first_outcome(self):
        item = payload("9" * 32)
        outcomes = item["data"]["outcomes"]
        outcomes["prospective_forecasts"] = [{
            "night_id": "2026-10-23", "forecast_status": "prospective_20_window",
            "cutoff": "2026-10-23T11:00:00Z", "generated_at": "2026-10-23T11:02:00Z",
            "model_version": "robust-next-night-research-v2",
            "adaptive_research_predictions": {"data_cmi": {
                "base_onset_offset_min": 180.0, "base_wake_offset_min": 660.0,
                "adjusted_onset_offset_min": 175.0, "adjusted_wake_offset_min": 650.0,
                "adjustment": {"policy_version": "online-residual-shrinkage-v1",
                               "eligible_count": 16, "active": True}}}
        }]
        outcomes["prospective_outcomes"] = [{
            "night_id": "2026-10-23", "record_id": "manual:2026-10-24", "status": "matched",
            "actual_source": "manual", "available_at": "2026-10-24T00:00:00Z",
            "actual_onset_offset_min": 185.0, "actual_wake_offset_min": 665.0,
            "source_model_version": "robust-next-night-research-v2",
            "source_research_artifacts": {"cmi": {"sha256": "a" * 64}},
            "rule": "first manual report is immutable",
            "residuals": {"data_cmi": {"onset_residual_min": 5.0, "wake_residual_min": 5.0}}
        }]
        receive(self.store, "phone-android", item, NOW)
        latest = build_console(self.store, ROOT / "models", NOW)["phone"]["participants"][0]["latest"]
        sidecar = latest["prospective_forecasts"][0]["sidecar_evidence"]
        self.assertEqual(sidecar["source"], "android_adaptive_research_predictions")
        self.assertEqual(sidecar["policy_version"], "online-residual-shrinkage-v1")
        self.assertEqual(sidecar["arms"][0]["base"]["onset_offset_min"], 180.0)
        self.assertEqual(sidecar["arms"][0]["corrected"]["wake_offset_min"], 650.0)
        frozen = latest["prospective_outcomes"][0]
        self.assertEqual(frozen["status"], "matched")
        self.assertEqual(frozen["residuals"]["data_cmi"]["wake_residual_min"], 5.0)
        self.assertEqual(frozen["source_research_artifacts"]["cmi"]["sha256"], "a" * 64)

    def test_windows_online_sidecar_keeps_identity_and_base_corrected_values(self):
        item = payload("8" * 32)
        item["data"]["outcomes"]["prospective_forecasts"] = [{
            "night_id": "2026-10-23", "predictions": {
                "data_cmi": {"onset_offset_min": 180.0, "wake_offset_min": 660.0}},
            "online_personalization": {"available": True, "status": "additional_research_comparison_not_recommendation_policy",
                "policy_version": "online-residual-shrinkage-v1", "base_artifact_identity_sha256": "b" * 64,
                "snapshot_sha256": "c" * 64,
                "predictions": {"data_cmi": {"onset_offset_min": 170.0, "wake_offset_min": 650.0}},
                "adjustments": {"data_cmi": {"active": True, "eligible_count": 20}}}}
        ]
        receive(self.store, "phone-windows", item, NOW)
        sidecar = build_console(self.store, ROOT / "models", NOW)["phone"]["participants"][0]["latest"]["prospective_forecasts"][0]["sidecar_evidence"]
        self.assertEqual(sidecar["source"], "windows_online_personalization")
        self.assertEqual(sidecar["base_artifact_identity_sha256"], "b" * 64)
        self.assertEqual(sidecar["arms"][0]["corrected"]["onset_offset_min"], 170.0)

    def test_diagnostics_do_not_change_snapshots(self):
        receive(self.store, "phone-a", payload("d" * 32), NOW)
        before = snapshots(self.store, limit=365)
        build_console(self.store, ROOT / "models", NOW)
        after = snapshots(self.store, limit=365)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
