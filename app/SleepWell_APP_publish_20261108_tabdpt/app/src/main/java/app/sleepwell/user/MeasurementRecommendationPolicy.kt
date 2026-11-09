package app.sleepwell.user

import java.time.Duration
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZonedDateTime
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.roundToLong

/** Supported-input subset of web-app engine.py v7 recommendation core. */
internal object MeasurementRecommendationPolicy {
    private const val TAU_WAKE_HOURS = 18.2
    private const val TAU_SLEEP_HOURS = 4.2

    data class CompleteDiary(
        val bed: ZonedDateTime,
        val wake: ZonedDateTime,
        val latencyMinutes: Int,
        val sleepMinutes: Double
    )

    data class SleepInterval(val start: ZonedDateTime, val end: ZonedDateTime)

    data class Result(
        val nightId: LocalDate,
        val bed: ZonedDateTime,
        val wake: ZonedDateTime,
        val windDown: ZonedDateTime,
        val sleepOpportunityMinutes: Int,
        val desiredSleepMinutes: Double,
        val feasible: Boolean,
        val currentS: Double,
        val observedSleepMinutes: Double?,
        val sleepDebtMinutes: Double?,
        val sleepDebtApplied: Boolean,
        val extensionMinutes: Double,
        val napApplied: Boolean,
        val dataPreferenceApplied: Boolean,
        val dataPreferredBed: ZonedDateTime?
    )

    fun nightId(now: ZonedDateTime, usualWake: LocalTime): LocalDate {
        val morningBoundary = minOf(12 * 60, usualWake.hour * 60 + usualWake.minute)
        val nowMinute = now.hour * 60 + now.minute
        val wakeDate = if (nowMinute < morningBoundary) now.toLocalDate() else now.toLocalDate().plusDays(1)
        return wakeDate.minusDays(1)
    }

    fun plan(
        now: ZonedDateTime,
        wake: ZonedDateTime,
        targetSleepMinutes: Int,
        latencyMinutes: Double,
        phaseMinutes: Double,
        personalizedMode: Boolean,
        evidenceAdequate: Boolean,
        usualBed: LocalTime,
        usualWake: LocalTime,
        recentCompleteDiaries: List<CompleteDiary>,
        completedNap: SleepInterval?,
        /**
         * TabDPT's predicted onset, converted to time in bed, is only a bounded
         * preference among candidates that already satisfy the wake and duration
         * constraints. This engineering rule is not a validated efficacy claim.
         */
        dataPreferredBed: ZonedDateTime? = null
    ): Result {
        val recent = recentCompleteDiaries.filter { row ->
            val age = Duration.between(row.wake, now)
            !age.isNegative && age <= Duration.ofDays(7)
        }.sortedBy { it.wake.toInstant() }
        val observedSleep = recent.map { it.sleepMinutes }.takeIf { it.isNotEmpty() }?.average()
        val debt = observedSleep?.let { maxOf(0.0, targetSleepMinutes - it) }
        val extension = if (debt != null && evidenceAdequate) minOf(30.0, debt * 0.25) else 0.0
        val desired = targetSleepMinutes + extension
        val latestBed = wake.minus(Duration.ofMillis(((desired + latencyMinutes) * 60_000.0).roundToLong()))

        val start = now.minusDays(7)
        val intervals = recent.map { row ->
            SleepInterval(row.bed.plusMinutes(row.latencyMinutes.toLong()), row.wake)
        }.toMutableList()
        val validNap = completedNap?.takeIf { it.start.isBefore(it.end) && !it.end.isAfter(now) }
        validNap?.let(intervals::add)
        val observedDates = recent.map { it.wake.toLocalDate() }.toSet()
        var s = 0.5
        var cursor = start
        while (cursor.isBefore(now)) {
            val next = minOf(now, cursor.plusMinutes(5))
            var asleep = intervals.any { !cursor.isBefore(it.start) && cursor.isBefore(it.end) }
            val cursorMinute = clockMinutes(cursor)
            val bedMinute = clockMinutes(usualBed)
            val wakeMinute = clockMinutes(usualWake)
            val expectedWakeDate = if (cursorMinute >= bedMinute && bedMinute > wakeMinute) {
                cursor.toLocalDate().plusDays(1)
            } else cursor.toLocalDate()
            if (!asleep && expectedWakeDate !in observedDates) {
                asleep = positiveModulo(cursorMinute - bedMinute, 1440.0) <
                    positiveModulo(wakeMinute - bedMinute, 1440.0)
            }
            s = advance(s, asleep, Duration.between(cursor, next).seconds / 3600.0)
            cursor = next
        }
        val currentS = s
        val candidates = (-30..0 step 5).mapNotNull { offset ->
            val candidate = latestBed.plusMinutes(offset.toLong())
            if (candidate.isBefore(now) || !candidate.isBefore(wake)) return@mapNotNull null
            val candidateS = advance(currentS, false, Duration.between(now, candidate).seconds / 3600.0)
            val candidateC = 0.15 * cos(2 * PI * (clockMinutes(candidate) - phaseMinutes) / 1440.0)
            val preferredBed = if (personalizedMode) {
                positiveModulo(phaseMinutes - targetSleepMinutes / 2.0, 1440.0)
            } else clockMinutes(usualBed)
            val regularityHours = abs(clockDelta(clockMinutes(candidate), preferredBed)) / 60.0
            val dataPenalty = dataPreferredBed?.let {
                0.08 * minOf(abs(Duration.between(candidate, it).seconds / 60.0) / 60.0, 4.0)
            } ?: 0.0
            val score = candidateS + candidateC - 0.08 * regularityHours -
                abs(offset) / 3000.0 - dataPenalty
            score to candidate
        }
        val enoughWindow = Duration.between(now, wake).seconds / 60.0 >= desired + latencyMinutes
        val selectedCandidate = candidates.maxByOrNull { it.first }?.second
        var bed = selectedCandidate ?: now
        val feasible = enoughWindow && !bed.isAfter(latestBed) && wake.isAfter(bed)
        if (!feasible) bed = minOf(now, wake)
        val opportunity = Duration.between(bed, wake).toMinutes().coerceAtLeast(0).toInt()
        return Result(
            nightId = nightId(now, usualWake),
            bed = bed,
            wake = wake,
            windDown = maxOf(now, bed.minusMinutes(30)),
            sleepOpportunityMinutes = opportunity,
            desiredSleepMinutes = desired,
            feasible = feasible,
            currentS = currentS,
            observedSleepMinutes = observedSleep,
            sleepDebtMinutes = debt,
            sleepDebtApplied = debt != null && evidenceAdequate,
            extensionMinutes = extension,
            napApplied = validNap != null,
            dataPreferenceApplied = dataPreferredBed != null && selectedCandidate != null &&
                enoughWindow && bed == selectedCandidate,
            dataPreferredBed = dataPreferredBed
        )
    }

    private fun advance(s: Double, asleep: Boolean, hours: Double): Double =
        if (asleep) s * exp(-hours / TAU_SLEEP_HOURS)
        else 1.0 - (1.0 - s) * exp(-hours / TAU_WAKE_HOURS)

    private fun clockMinutes(value: ZonedDateTime): Double =
        value.hour * 60.0 + value.minute + value.second / 60.0

    private fun clockMinutes(value: LocalTime): Double =
        value.hour * 60.0 + value.minute + value.second / 60.0

    private fun clockDelta(value: Double, reference: Double): Double =
        positiveModulo(value - reference + 720.0, 1440.0) - 720.0

    private fun positiveModulo(value: Double, modulus: Double): Double = ((value % modulus) + modulus) % modulus
}
