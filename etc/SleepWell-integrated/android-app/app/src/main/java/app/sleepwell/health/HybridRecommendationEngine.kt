package app.sleepwell.health

import java.time.Instant
import java.time.ZoneId
import kotlin.math.roundToLong

object HybridRecommendationEngine {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun recommend(
        future: List<PredictedPoint>,
        directInput: DirectInput?,
        residualModel: RidgeResidualModel?,
        features: Map<String, Double?>,
        targetSleepMin: Double
    ): Recommendation {
        require(future.isNotEmpty()) { "미래 물리모델 예측점이 없습니다" }
        val sleepTarget = targetSleepMin.coerceIn(360.0, 600.0)
        val nightCandidates = future.filter { point ->
            val hour = Instant.ofEpochMilli(point.timestampMs).atZone(seoul).hour
            hour >= 20 || hour < 5
        }
        val physicalBed = (nightCandidates.ifEmpty { future })
            .maxByOrNull { it.predictedPropensity }!!.timestampMs
        val physicalWake = directInput?.targetWakeMs
            ?.takeIf { it > physicalBed }
            ?: (physicalBed + (sleepTarget * 60_000.0).roundToLong())

        val trained = residualModel?.takeIf { it.status == "trained" }
        val residual = trained?.predict(features) ?: ResidualPrediction(0.0, 0.0)
        val predictedBed = physicalBed + (residual.bedtimeResidualMin * 60_000.0).roundToLong()
        val predictedWake = physicalWake + (residual.wakeResidualMin * 60_000.0).roundToLong()

        val recommendedWake = directInput?.targetWakeMs ?: predictedWake
        val latestBedForTarget = recommendedWake - (sleepTarget * 60_000.0).roundToLong()
        val recommendedBed = minOf(predictedBed, latestBedForTarget)
        val recommendedSleep = (recommendedWake - recommendedBed) / 60_000.0

        return Recommendation(
            recommendedBedTimeMs = recommendedBed,
            recommendedWakeTimeMs = recommendedWake,
            recommendedSleepMin = recommendedSleep,
            physicalBedTimeMs = physicalBed,
            physicalWakeTimeMs = physicalWake,
            predictedBedTimeMs = predictedBed,
            predictedWakeTimeMs = predictedWake,
            bedtimeResidualMin = residual.bedtimeResidualMin,
            wakeResidualMin = residual.wakeResidualMin,
            residualModelVersion = trained?.modelVersion,
            modelStatus = if (trained == null) "physics_only_untrained" else "hybrid_trained",
            usedFallback = trained == null
        )
    }
}
