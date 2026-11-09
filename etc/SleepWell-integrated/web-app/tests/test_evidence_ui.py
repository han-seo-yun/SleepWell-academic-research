import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


class InputCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        values = dict(attrs)
        if values.get("name"):
            self.inputs[values["name"]] = values


class EvidenceUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = InputCollector()
        parser.feed(INDEX)
        cls.inputs = parser.inputs

    def test_optional_diary_measurements_start_blank_and_accept_explicit_zero(self):
        for name in ("latency_min", "awake_min", "sleepiness", "burden"):
            field = self.inputs[name]
            self.assertEqual(field["min"], "0")
            self.assertNotIn("required", field)
            self.assertNotIn("value", field)
        survey = self.inputs["survey_occurred_at"]
        self.assertEqual(survey["type"], "datetime-local")
        self.assertNotIn("required", survey)
        self.assertNotIn("value", survey)
        self.assertIn("0분도 유효한 응답입니다.", INDEX)
        self.assertIn("실제로 응답한 시각만 입력", INDEX)
        self.assertIn("기상 시각으로 자동 대체하지 않습니다", INDEX)

    def test_edit_and_reset_contract_preserves_missing_and_zero(self):
        for name in ("latency_min", "awake_min", "sleepiness", "burden", "survey_occurred_at"):
            self.assertIn(f"'{name}'", APP)
        self.assertIn("record[key]??''", APP)
        self.assertNotIn("record[key]||''", APP)
        self.assertIn("key==='survey_occurred_at'?inputDateTime(record[key])", APP)
        self.assertIn("Object.fromEntries(new FormData(event.target))", APP)
        self.assertIn("form.reset()", APP)

    def test_time_in_bed_and_sleep_estimate_have_distinct_missing_semantics(self):
        self.assertIn("잠자리 체류 (취침~기상)", INDEX)
        self.assertIn("추정 실제 수면", INDEX)
        self.assertIn("x.time_in_bed_min", APP)
        self.assertIn("sleepEstimate(x.sleep_min)", APP)
        self.assertIn("추정 불가/입면·각성 미입력", APP)
        self.assertNotIn("duration(x.sleep_min)", APP)
        self.assertIn("사용자 요청 수면 목표", INDEX)
        self.assertIn("실행 가능한 잠자리 기회", APP)
        self.assertIn("희망 목표 대비 차이", INDEX)
        self.assertIn('id="goal-difference"', INDEX)
        self.assertIn("사용자 희망 목표 − 완전 일지 수면 추정치", INDEX)
        self.assertIn("생리적 수면부족이나 필요량 아님", INDEX)
        self.assertIn("sleep_debt_status==='available'", APP)
        self.assertIn("계산 불가/완전 일지 없음", APP)
        self.assertIn("nonBiologicalDifferenceWording", APP)

    def test_population_and_quality_copy_do_not_claim_calibrated_need(self):
        self.assertIn("집단 관측 평균은 기술 비교 정보일 뿐 목표나 개인의 필요 수면 추정에 사용하지 않습니다", INDEX)
        self.assertIn("설명용, 목표 미사용", APP)
        self.assertIn("insufficient_effective_records", APP)
        self.assertIn("stale_history", APP)
        self.assertIn("irregular_midpoint", APP)
        self.assertIn("보정된 확률·임상적 확실성 아님", APP)
        self.assertNotIn("유효 일지가 아직 없어 ${warmText}을 사용합니다", APP)

    def test_forecast_intervals_and_old_policy_are_never_presented_as_current_validation(self):
        for field in (
            "monitoring_phase",
            "prediction_intervals",
            "calibration_n",
            "nominal_joint_coverage",
            "empirical_coverage",
        ):
            self.assertIn(field, APP)
        self.assertIn("검증 완료 아님", APP)
        self.assertIn("전향 구간 포함률 관측은 시계열 의존성이 있는 기술통계", APP)
        self.assertIn("연구 평가 집단의 평균 오차이며 오늘 개인 예측의 검증된 구간이나 보장 범위가 아닙니다", APP)
        self.assertIn("state?.recommendation?.stale_policy", APP)
        self.assertIn("이전 정책으로 계산된 역사 기록", APP)
        self.assertIn('id="stale-policy-warning"', INDEX)

    def test_recommendation_follow_up_is_labeled_noncausal_and_exposure_based(self):
        self.assertIn("실제 취침 전에 화면을 본 기록", APP)
        self.assertIn("생성만 된 추천은 준수로 간주하지 않습니다", APP)
        self.assertIn("인과 효과 추정: 없음", APP)
        self.assertIn("별도 동의 · 사전 선언한 배정 · 이후 미래 추적", APP)
        self.assertIn("현재 운영 중인 무작위 시험", APP)
        self.assertIn("기상이나 수신 시각으로 대체하지 않습니다", APP)


if __name__ == "__main__":
    unittest.main()
