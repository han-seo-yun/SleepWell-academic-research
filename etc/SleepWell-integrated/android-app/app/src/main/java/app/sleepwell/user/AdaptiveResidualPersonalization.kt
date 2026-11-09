package app.sleepwell.user

import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import kotlin.math.abs
import kotlin.math.ln
import kotlin.math.pow

/** Prospective online residual correction used only as an additional research sidecar. */
object AdaptiveResidualPersonalization {
    const val POLICY_VERSION = "online-residual-shrinkage-v2-daily"
    private const val DEFAULT_HALF_LIFE_DAYS = 14.0
    private const val DEFAULT_LAMBDA = 14.0
    private const val DEFAULT_MIN_HISTORY = 1
    private const val RESIDUAL_CLIP_MIN = 180.0
    private const val CORRECTION_CLIP_MIN = 60.0

    init {
        // Fixed vector exported from the Python reference implementation.
        val cutoff = Instant.parse("2026-10-25T11:00:00Z")
        val reference = computeAdjustment(listOf(
            HistoryResidual("a", 10.0, 20.0, Instant.parse("2026-10-24T11:00:00Z"), Instant.parse("2026-10-23T11:00:00Z")),
            HistoryResidual("b", -5.0, 15.0, Instant.parse("2026-10-25T10:00:00Z"), Instant.parse("2026-10-24T11:00:00Z")),
            HistoryResidual("future", 999.0, 999.0, cutoff, cutoff)
        ), cutoff, minHistory = 2)
        require(reference.eligibleCount == 2 &&
            abs(reference.decayedMass - 1.8574188172745263) < 1e-12 &&
            abs(reference.kishEffectiveN - 1.9987756051486254) < 1e-12 &&
            abs(reference.weight - 0.11713248156447241) < 1e-12 &&
            abs(reference.rawOnsetResidualMeanMin - 2.3143734938002476) < 1e-12 &&
            abs(reference.rawWakeResidualMeanMin - 17.438124497933416) < 1e-12 &&
            abs(reference.onsetCorrectionMin - 0.27108831059586114) < 1e-12 &&
            abs(reference.wakeCorrectionMin - 2.0425707962731603) < 1e-12
        ) { "온라인 잔차 개인화의 Python 기준 산술과 일치하지 않습니다." }
    }

    data class HistoryResidual(
        val nightId: String?,
        val onsetResidualMin: Double,
        val wakeResidualMin: Double,
        val availableAt: Instant,
        val outcomeAt: Instant = availableAt
    )

    data class Adjustment(
        val eligibleCount: Int,
        val decayedMass: Double,
        val kishEffectiveN: Double,
        val weight: Double,
        val rawOnsetResidualMeanMin: Double,
        val rawWakeResidualMeanMin: Double,
        val onsetCorrectionMin: Double,
        val wakeCorrectionMin: Double,
        val lastOutcomeGapDays: Double?,
        val lastAvailableGapDays: Double?,
        val active: Boolean
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("policy_version", POLICY_VERSION)
            .put("eligible_count", eligibleCount)
            .put("decayed_mass", decayedMass)
            .put("kish_effective_n", kishEffectiveN)
            .put("weight", weight)
            .put("raw_onset_residual_mean_min", rawOnsetResidualMeanMin)
            .put("raw_wake_residual_mean_min", rawWakeResidualMeanMin)
            .put("onset_correction_min", onsetCorrectionMin)
            .put("wake_correction_min", wakeCorrectionMin)
            .put("last_outcome_gap_days", lastOutcomeGapDays ?: JSONObject.NULL)
            .put("last_available_gap_days", lastAvailableGapDays ?: JSONObject.NULL)
            .put("active", active)
            .put("uncertainty_min", JSONObject.NULL)
            .put("uncertainty_reason", "검증된 예측구간이 없어 제공하지 않음")
    }

    fun computeAdjustment(
        history: List<HistoryResidual>,
        cutoff: Instant,
        halfLifeDays: Double = DEFAULT_HALF_LIFE_DAYS,
        lambda: Double = DEFAULT_LAMBDA,
        minHistory: Int = DEFAULT_MIN_HISTORY
    ): Adjustment {
        require(halfLifeDays > 0 && lambda > 0 && minHistory > 0)
        val eligible = history.filter { !it.outcomeAt.isAfter(it.availableAt) && it.availableAt.isBefore(cutoff) }
            .groupBy { it.nightId ?: "@${it.availableAt}" }
            .values.map { rows -> rows.maxBy { it.availableAt } }
        val weighted = eligible.map { row ->
            val ageDays = Duration.between(row.outcomeAt, cutoff).seconds.coerceAtLeast(0) / 86400.0
            row to 2.0.pow(-ageDays / halfLifeDays)
        }
        val mass = weighted.sumOf { it.second }
        val sumSquares = weighted.sumOf { it.second * it.second }
        val kish = if (sumSquares > 0) mass * mass / sumSquares else 0.0
        val active = eligible.size >= minHistory && mass > 0
        val shrinkage = if (active) mass / (mass + lambda) else 0.0
        val rawOnset = if (mass > 0) weighted.sumOf {
            it.first.onsetResidualMin.coerceIn(-RESIDUAL_CLIP_MIN, RESIDUAL_CLIP_MIN) * it.second
        } / mass else 0.0
        val rawWake = if (mass > 0) weighted.sumOf {
            it.first.wakeResidualMin.coerceIn(-RESIDUAL_CLIP_MIN, RESIDUAL_CLIP_MIN) * it.second
        } / mass else 0.0
        val onset = if (active) (shrinkage * rawOnset).coerceIn(-CORRECTION_CLIP_MIN, CORRECTION_CLIP_MIN) else 0.0
        val wake = if (active) (shrinkage * rawWake).coerceIn(-CORRECTION_CLIP_MIN, CORRECTION_CLIP_MIN) else 0.0
        val outcomeGap = eligible.maxOfOrNull { it.outcomeAt }?.let { Duration.between(it, cutoff).seconds / 86400.0 }
        val availableGap = eligible.maxOfOrNull { it.availableAt }?.let { Duration.between(it, cutoff).seconds / 86400.0 }
        return Adjustment(eligible.size, mass, kish, shrinkage, rawOnset, rawWake, onset, wake, outcomeGap, availableGap, active)
    }

    fun project(onset: Double, wake: Double): Pair<Double, Double> {
        val projectedOnset = onset.coerceIn(0.0, 960.0)
        val projectedWake = wake.coerceIn(projectedOnset + 30.0, minOf(projectedOnset + 960.0, 1440.0))
        return projectedOnset to projectedWake
    }
}
