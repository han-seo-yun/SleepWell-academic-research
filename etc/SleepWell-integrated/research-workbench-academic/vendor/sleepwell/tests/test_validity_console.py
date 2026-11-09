import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_console import _load_target_validity_validation, _parse_snapshot


def aggregate_artifact():
    cohort = {
        "n_people": 12,
        "n_nights": 80,
        "label_source": "main sleep timing",
        "models": [{
            "name": "hybrid",
            "joint_mae_min": 42.0,
            "onset_mae_min": 40.0,
            "wake_mae_min": 44.0,
            "skill_vs_recent14": 0.12,
            "gain_vs_recent14_min": 6.0,
            "ci95_min": [1.0, 11.0],
            "participant_improved_fraction": 0.75,
        }],
        "history_strata": [{
            "label": "1-6 prior records",
            "n_people": 8,
            "n_nights": 30,
            "data_mae_min": 48.0,
            "hybrid_mae_min": 46.0,
            "baseline_mae_min": 52.0,
        }],
        "feasibility": {
            "max_prior_records": 45,
            "people_with_14_prior_records": 9,
            "people_with_28_prior_records": 4,
        },
        "availability_assumption": "only records available before the forecast cutoff",
    }
    return {
        "schema_version": 1,
        "artifact_type": "sleepwell_target_validity_validation",
        "version": "2026-10-25-v1",
        "test_status": "retrospective_reused_test",
        "no_service_promotion": True,
        "cohorts": {"cmi": copy.deepcopy(cohort), "mesa": copy.deepcopy(cohort)},
        "limitations": ["Retrospective reused test split."],
        "provenance": {"participant_id": "must-never-leak", "local_path": "E:/private/raw.csv"},
    }


def snapshot():
    return {
        "device_id": "device-1",
        "participant_key": "a" * 32,
        "day": "2026-10-24",
        "revision": 1,
        "generated_at": "2026-10-24T20:30:00+09:00",
        "received_at": "2026-10-24T20:31:00+09:00",
        "sha256": "b" * 64,
        "data": {
            "profile": {},
            "records": [{
                "id": "diary-1",
                "day": "2026-10-24",
                "bedtime": "2026-10-23T23:00:00+09:00",
                "wake_time": "2026-10-24T07:00:00+09:00",
                "duration_minutes": 480,
                "source": "manual",
                "modified_at": "2026-10-24T08:00:00+09:00",
            }, {
                "id": "wearable-1",
                "day": "2026-10-24",
                "bedtime": "2026-10-23T23:10:00+09:00",
                "wake_time": "2026-10-24T07:05:00+09:00",
                "duration_minutes": 475,
                "source": "samsung_health",
                "modified_at": "2026-10-24T07:10:00+09:00",
            }],
            "recommendation": None,
            "forecast": None,
            "outcomes": {},
        },
    }


class TargetValidityConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_artifact(self, value):
        path = self.directory / "target_validity_validation_v1.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_missing_and_invalid_artifacts_are_unavailable(self):
        missing = _load_target_validity_validation(self.directory / "missing.json")
        self.assertFalse(missing["available"])
        self.assertEqual(missing["reason"], "FileNotFoundError")

        invalid = aggregate_artifact()
        invalid["participant_predictions"] = [{"participant_id": "private"}]
        result = _load_target_validity_validation(self.write_artifact(invalid))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "ValueError")

    def test_nonfinite_numeric_is_rejected(self):
        artifact = aggregate_artifact()
        artifact["cohorts"]["cmi"]["models"][0]["joint_mae_min"] = float("nan")
        result = _load_target_validity_validation(self.write_artifact(artifact))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "ValueError")

    def test_only_aggregate_whitelist_is_exposed(self):
        result = _load_target_validity_validation(self.write_artifact(aggregate_artifact()))
        self.assertTrue(result["available"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("participant_id", serialized)
        self.assertNotIn("must-never-leak", serialized)
        self.assertNotIn("local_path", serialized)
        self.assertNotIn("E:/private", serialized)
        self.assertEqual(result["cohorts"][0]["models"][0]["name"], "hybrid")

    def test_snapshot_annotation_does_not_mutate_input(self):
        original = snapshot()
        before = copy.deepcopy(original)
        _, day_view, latest_view = _parse_snapshot(original, self.directory)
        self.assertEqual(original, before)
        self.assertEqual(day_view["target_validity"]["artifact_type"], "sleepwell_target_validity_summary")
        agreement = day_view["target_validity"]["agreement"]["by_wearable_source"]["samsung_health"]
        self.assertEqual(agreement["paired_nights"], 1)
        self.assertEqual(day_view["target_validity"], latest_view["target_validity"])

    def test_phone_agreement_uses_recorded_time_not_true_onset_language(self):
        script = (ROOT / "static" / "research-console.js").read_text(encoding="utf-8")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("기록된 취침시각", script)
        self.assertIn("서로 같은 생리학적 입면 측정이 아닙니다", script)
        self.assertIn("PSG 정답 자료가 없으며", html)
        self.assertIn("기기 알고리즘 정의이므로 의미가 같지 않습니다", html)

    def test_baseline_labels_use_calendar_day_windows_and_macro_skill(self):
        script = (ROOT / "static" / "research-console.js").read_text(encoding="utf-8")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("최근 7일 창 평균", script)
        self.assertIn("최근 14일 창 평균", script)
        self.assertIn("최근 28일 창 가중평균 (반감기 14일)", script)
        self.assertIn("최근 28일 같은 요일 평균 (2개 미만이면 14일 평균)", script)
        self.assertIn("개인별 Skill의 평균이 아닙니다", script)
        self.assertNotIn("최근 14건", script)
        self.assertIn("동일 참여자가 시간 경과에 따라 여러 기록 수 구간에 반복 포함", html)
        self.assertIn("인과 효과의 근거가 아닙니다", html)


if __name__ == "__main__":
    unittest.main()
