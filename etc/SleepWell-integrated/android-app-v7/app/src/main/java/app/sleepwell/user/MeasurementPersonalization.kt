package app.sleepwell.user

import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt
import kotlin.math.roundToInt

/** Android port of the web v7 measurement-evidence personalization policy. */
internal object MeasurementPersonalization {
    const val HALF_LIFE_DAYS = 14.0
    const val SHRINKAGE_LAMBDA = 14.0
    const val ADEQUATE_EFFECTIVE_RECORDS = 5.0
    const val STALE_HISTORY_DAYS = 14.0
    const val MIN_PHASE_RESULTANT = 0.5

    data class DiaryEvidence(
        val midpointMinutes: Double,
        val wakeAgeDays: Double,
        val observationAgeDays: Double,
        val timeInBedMinutes: Double,
        val latencyMinutes: Int?,
        val awakeMinutes: Int?,
        val rested: Int?,
        val fatigue: Int?,
        val steps: Int?,
        val exerciseMinutes: Double?
    ) {
        val sleepMinutes: Double?
            get() {
                val latency = latencyMinutes ?: return null
                val awake = awakeMinutes ?: return null
                if (latency !in 0..180 || awake !in 0..600) return null
                val value = timeInBedMinutes - latency - awake
                return value.takeIf { it > 0.0 }
            }
    }

    data class Flag(val code: String, val active: Boolean, val threshold: Double, val observed: Double?)

    data class Result(
        val targetSleepMinutes: Int,
        val phaseMinutes: Double,
        val phaseResultantLength: Double?,
        val phaseStatus: String,
        val active: Boolean,
        val conservative: Boolean,
        val flags: List<Flag>,
        val personalizationWeight: Double,
        val effectiveRecords: Double,
        val kishEffectiveN: Double,
        val lastRecordGapDays: Double?,
        val latestObservationAgeDays: Double?,
        val goodNightCount: Int,
        val goodNightEffectiveRecords: Double,
        val sleepTargetWeight: Double,
        val latencyEffectiveRecords: Double,
        val activityEffectiveRecords: Double,
        val exerciseEffectiveRecords: Double,
        val latencyMinutes: Double,
        val averageSteps: Double,
        val exerciseMinutes: Double,
        val weights: List<Double>
    )

    fun compute(
        declaredTargetMinutes: Int,
        usualBedMinutes: Double,
        usualWakeMinutes: Double,
        baselineLatencyMinutes: Double,
        baselineSteps: Double,
        baselineExerciseMinutes: Double,
        evidence: List<DiaryEvidence>
    ): Result {
        require(baselineLatencyMinutes.isFinite() && baselineLatencyMinutes in 0.0..120.0)
        require(baselineSteps.isFinite() && baselineSteps in 0.0..100000.0)
        require(baselineExerciseMinutes.isFinite() && baselineExerciseMinutes in 0.0..600.0)
        val target = declaredTargetMinutes.coerceIn(420, 540)
        val profilePhase = positiveModulo(
            usualBedMinutes + positiveModulo(usualWakeMinutes - usualBedMinutes, 1440.0) / 2.0,
            1440.0
        )
        val weights = evidence.map { 2.0.pow(-it.wakeAgeDays.coerceAtLeast(0.0) / HALF_LIFE_DAYS) }
        val effective = weights.sum()
        val squares = weights.sumOf { it * it }
        val active = evidence.isNotEmpty() && effective > 0.0
        val alpha = if (active) effective / (effective + SHRINKAGE_LAMBDA) else 0.0
        val (phaseCenter, resultant) = weightedClockMean(evidence.map { it.midpointMinutes }, weights)
        val lastGap = evidence.minOfOrNull { it.wakeAgeDays.coerceAtLeast(0.0) }
        val latestObservation = evidence.minOfOrNull { it.observationAgeDays.coerceAtLeast(0.0) }
        val flags = listOf(
            Flag("insufficient_effective_records", effective < ADEQUATE_EFFECTIVE_RECORDS,
                ADEQUATE_EFFECTIVE_RECORDS, effective),
            Flag("stale_history", lastGap != null && lastGap > STALE_HISTORY_DAYS,
                STALE_HISTORY_DAYS, lastGap),
            Flag("irregular_midpoint", resultant != null && resultant < MIN_PHASE_RESULTANT,
                MIN_PHASE_RESULTANT, resultant)
        )
        val conservative = flags.any { it.active }
        val phaseStatus = when {
            evidence.isEmpty() -> "profile_no_history"
            phaseCenter == null -> if (resultant != null) "profile_undefined_resultant" else "profile_insufficient_evidence"
            flags[0].active -> "profile_insufficient_evidence"
            flags[1].active -> "profile_stale_history"
            flags[2].active -> "profile_irregular_history"
            else -> "personalized"
        }
        val phase = if (!conservative && phaseCenter != null) {
            positiveModulo(profilePhase + alpha * clockDelta(phaseCenter, profilePhase), 1440.0)
        } else profilePhase

        val good = evidence.zip(weights).mapNotNull { (row, weight) ->
            row.sleepMinutes?.takeIf {
                row.rested != null && row.rested >= 4 && row.fatigue != null && row.fatigue <= 3
            }?.let { it to weight }
        }
        val goodEffective = good.sumOf { it.second }
        val applySleepTarget = !conservative && good.size >= 5 && goodEffective >= ADEQUATE_EFFECTIVE_RECORDS
        val targetWeight = if (applySleepTarget) goodEffective / (goodEffective + SHRINKAGE_LAMBDA) else 0.0
        val personalizedTarget = if (applySleepTarget) {
            ((1.0 - targetWeight) * target + targetWeight * weightedMedian(good))
                .coerceIn(maxOf(420.0, target - 30.0), minOf(540.0, target + 30.0))
                .toIntRounded()
        } else target
        fun shrunkField(initial: Double, values: List<Pair<Double, Double>>): Pair<Double, Double> {
            val mass = values.sumOf { it.second }
            if (mass <= 0.0) return initial to 0.0
            val fieldAlpha = mass / (mass + SHRINKAGE_LAMBDA)
            val observed = values.sumOf { it.first * it.second } / mass
            return ((1.0 - fieldAlpha) * initial + fieldAlpha * observed) to mass
        }
        val latency = shrunkField(baselineLatencyMinutes, evidence.zip(weights).mapNotNull { (row, weight) ->
            row.latencyMinutes?.toDouble()?.let { it to weight }
        })
        val activity = shrunkField(baselineSteps, evidence.zip(weights).mapNotNull { (row, weight) ->
            row.steps?.toDouble()?.let { it to weight }
        })
        val exercise = shrunkField(baselineExerciseMinutes, evidence.zip(weights).mapNotNull { (row, weight) ->
            row.exerciseMinutes?.let { it to weight }
        })
        return Result(
            targetSleepMinutes = personalizedTarget,
            phaseMinutes = phase,
            phaseResultantLength = resultant,
            phaseStatus = phaseStatus,
            active = active,
            conservative = conservative,
            flags = flags,
            personalizationWeight = alpha,
            effectiveRecords = effective,
            kishEffectiveN = if (squares > 0.0) effective * effective / squares else 0.0,
            lastRecordGapDays = lastGap,
            latestObservationAgeDays = latestObservation,
            goodNightCount = good.size,
            goodNightEffectiveRecords = goodEffective,
            sleepTargetWeight = targetWeight,
            latencyEffectiveRecords = latency.second,
            activityEffectiveRecords = activity.second,
            exerciseEffectiveRecords = exercise.second,
            latencyMinutes = latency.first,
            averageSteps = activity.first,
            exerciseMinutes = exercise.first,
            weights = weights
        )
    }

    private fun weightedClockMean(values: List<Double>, weights: List<Double>): Pair<Double?, Double?> {
        val mass = weights.sum()
        if (values.isEmpty() || mass <= 0.0) return null to null
        val x = values.zip(weights).sumOf { (value, weight) -> cos(2 * PI * value / 1440.0) * weight }
        val y = values.zip(weights).sumOf { (value, weight) -> sin(2 * PI * value / 1440.0) * weight }
        val resultant = sqrt(x * x + y * y) / mass
        if (resultant <= 1e-12) return null to resultant
        return positiveModulo(atan2(y, x) * 1440.0 / (2 * PI), 1440.0) to resultant
    }

    private fun weightedMedian(values: List<Pair<Double, Double>>): Double {
        val ordered = values.sortedBy { it.first }
        val threshold = ordered.sumOf { it.second } / 2.0
        var cumulative = 0.0
        ordered.forEach { pair ->
            cumulative += pair.second
            if (cumulative >= threshold) return pair.first
        }
        return ordered.last().first
    }

    private fun clockDelta(value: Double, reference: Double): Double =
        positiveModulo(value - reference + 720.0, 1440.0) - 720.0

    private fun positiveModulo(value: Double, modulus: Double): Double = ((value % modulus) + modulus) % modulus
    private fun Double.toIntRounded(): Int = roundToInt()
}
