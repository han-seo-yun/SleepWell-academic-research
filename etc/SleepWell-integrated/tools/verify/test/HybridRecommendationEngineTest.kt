package test

import app.sleepwell.health.DirectInput
import app.sleepwell.health.HybridRecommendationEngine
import app.sleepwell.health.PredictedPoint
import app.sleepwell.health.RidgeResidualModel

object HybridRecommendationEngineTest {
    fun run() {
        combinesPhysicsAndRidgeResidual()
        usesExplicitUntrainedFallback()
    }

    private fun combinesPhysicsAndRidgeResidual() {
        T.group("HybridRecommendationEngine — 물리+잔차")
        val bed = Kst.ms("2026-10-08 23:00:00")
        val future = listOf(
            PredictedPoint(Kst.ms("2026-10-08 22:00:00"), .7, .1, .8),
            PredictedPoint(bed, .8, .15, .95),
            PredictedPoint(Kst.ms("2026-10-09 01:00:00"), .85, .05, .90)
        )
        val model = RidgeResidualModel(
            1, "test-ridge", "trained", listOf("fatigue"),
            listOf(3.0), listOf(0.0), listOf(1.0),
            30.0, listOf(0.0), -15.0, listOf(0.0)
        )
        val targetWake = Kst.ms("2026-10-09 08:00:00")
        val rec = HybridRecommendationEngine.recommend(
            future = future,
            directInput = DirectInput(null, null, targetWake, null),
            residualModel = model,
            features = mapOf("fatigue" to 4.0),
            targetSleepMin = 480.0
        )

        T.assertEquals("물리 취침 후보", bed, rec.physicalBedTimeMs)
        T.assertEquals("예측 취침=물리+30분", bed + 30 * 60_000L, rec.predictedBedTimeMs)
        T.assertClose("취침 잔차 기록", 30.0, rec.bedtimeResidualMin!!, 1e-12)
        T.assertEquals("목표 기상은 권장 기상으로 보존", targetWake, rec.recommendedWakeTimeMs)
        T.assertEquals("8시간 확보 한계보다 이른 개인화 예측을 권고", bed + 30 * 60_000L, rec.recommendedBedTimeMs)
        T.assertEquals("모델 버전 표시", "test-ridge", rec.residualModelVersion)
    }

    private fun usesExplicitUntrainedFallback() {
        T.group("HybridRecommendationEngine — 미학습 fallback")
        val bed = Kst.ms("2026-10-08 23:00:00")
        val rec = HybridRecommendationEngine.recommend(
            future = listOf(PredictedPoint(bed, .8, .1, .9)),
            directInput = null,
            residualModel = null,
            features = emptyMap(),
            targetSleepMin = 450.0
        )
        T.assertEquals("fallback은 물리 취침을 그대로 사용", bed, rec.predictedBedTimeMs)
        T.assertEquals("fallback 상태", "physics_only_untrained", rec.modelStatus)
        T.assertTrue("fallback 플래그", rec.usedFallback, "학습 전인데 학습 모델처럼 표시됨")
    }
}
