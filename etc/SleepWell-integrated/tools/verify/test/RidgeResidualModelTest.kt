package test

import app.sleepwell.health.RidgeResidualModel

object RidgeResidualModelTest {
    fun run() {
        predictsWithTrainingOnlyPreprocessing()
        parsesExportedJson()
        rejectsWrongFeatureOrder()
    }

    private fun predictsWithTrainingOnlyPreprocessing() {
        T.group("RidgeResidualModel — Kotlin 직접 계산")
        val model = RidgeResidualModel(
            schemaVersion = 1,
            modelVersion = "unit-test",
            status = "trained",
            featureOrder = listOf("fatigue", "caffeine_mg"),
            imputationMedian = listOf(3.0, 100.0),
            mean = listOf(2.0, 80.0),
            scale = listOf(2.0, 20.0),
            bedtimeIntercept = 10.0,
            bedtimeCoefficients = listOf(4.0, -3.0),
            wakeIntercept = -5.0,
            wakeCoefficients = listOf(2.0, 6.0)
        )

        // fatigue=(4-2)/2=1, caffeine=(100-80)/20=1 (null은 학습 median 100으로 대치)
        val p = model.predict(mapOf("fatigue" to 4.0, "caffeine_mg" to null))
        T.assertClose("취침 잔차", 11.0, p.bedtimeResidualMin, 1e-12)
        T.assertClose("기상 잔차", 3.0, p.wakeResidualMin, 1e-12)
    }

    private fun parsesExportedJson() {
        T.group("RidgeResidualModel — Python JSON 계약")
        val json = """
            {
              "schema_version": 1,
              "model_version": "ridge-2026-10",
              "status": "trained",
              "feature_order": ["fatigue", "caffeine_mg"],
              "preprocessing": {
                "imputation_median": [3.0, 100.0],
                "mean": [2.0, 80.0],
                "scale": [2.0, 20.0]
              },
              "targets": {
                "bedtime_residual_min": {"intercept": 10.0, "coefficients": [4.0, -3.0]},
                "wake_residual_min": {"intercept": -5.0, "coefficients": [2.0, 6.0]}
              }
            }
        """.trimIndent()
        val model = RidgeResidualModel.fromJson(json)
        val p = model.predict(mapOf("fatigue" to 4.0, "caffeine_mg" to 100.0))
        T.assertEquals("버전", "ridge-2026-10", model.modelVersion)
        T.assertClose("JSON 취침 계산", 11.0, p.bedtimeResidualMin, 1e-12)
        T.assertClose("JSON 기상 계산", 3.0, p.wakeResidualMin, 1e-12)
    }

    private fun rejectsWrongFeatureOrder() {
        T.group("RidgeResidualModel — 스키마 검증")
        var rejected = false
        try {
            RidgeResidualModel(
                schemaVersion = 1,
                modelVersion = "bad",
                status = "trained",
                featureOrder = listOf("a", "b"),
                imputationMedian = listOf(0.0),
                mean = listOf(0.0, 0.0),
                scale = listOf(1.0, 1.0),
                bedtimeIntercept = 0.0,
                bedtimeCoefficients = listOf(0.0, 0.0),
                wakeIntercept = 0.0,
                wakeCoefficients = listOf(0.0, 0.0)
            )
        } catch (_: IllegalArgumentException) {
            rejected = true
        }
        T.assertTrue("배열 길이가 feature_order와 다르면 거절", rejected, "잘못된 모델이 통과했다")
    }
}
