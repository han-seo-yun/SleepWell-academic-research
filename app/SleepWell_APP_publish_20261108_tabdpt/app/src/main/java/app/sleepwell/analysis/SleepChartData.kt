package app.sleepwell.analysis

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.temporal.ChronoUnit

data class SleepTrendPoint(
    val day: LocalDate,
    val durationMinutes: Int,
    /** Minutes since noon on the day before the displayed wake date. */
    val bedtimeAxisMinute: Int,
    val wakeAxisMinute: Int,
    val sourceLabel: String
)

/** Pure, read-only chart projections. Missing dates are deliberately not synthesized as zeroes. */
object SleepChartData {
    /** Position inside a selected day window. Today/period end is 1; missing trailing days stay blank. */
    fun axisFraction(day: LocalDate, periodEnd: LocalDate, days: Int): Float {
        val span = (days.coerceAtLeast(2) - 1).toLong()
        val daysAgo = ChronoUnit.DAYS.between(day, periodEnd)
        return ((span - daysAgo).toFloat() / span).coerceIn(0f, 1f)
    }

    fun trend(
        nights: List<SleepHistoryNight>,
        zone: ZoneId,
        nowMs: Long,
        days: Int
    ): List<SleepTrendPoint> {
        val today = Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate()
        val firstDay = today.minusDays((days.coerceAtLeast(1) - 1).toLong())
        return nights.mapNotNull { night ->
            val bed = Instant.ofEpochMilli(night.bedtimeMs).atZone(zone)
            val wake = Instant.ofEpochMilli(night.wakeMs).atZone(zone)
            val wakeDay = wake.toLocalDate()
            if (wakeDay.isBefore(firstDay) || wakeDay.isAfter(today)) return@mapNotNull null
            val axisStart = wakeDay.minusDays(1).atTime(12, 0).atZone(zone)
            SleepTrendPoint(
                day = wakeDay,
                durationMinutes = night.intervalMinutes,
                // Clock trend represents displayed wall time. Zoned elapsed minutes would shift the
                // plotted clock by an hour across DST even though the label still says the same HH:mm.
                bedtimeAxisMinute = ChronoUnit.MINUTES.between(axisStart.toLocalDateTime(), bed.toLocalDateTime()).toInt(),
                wakeAxisMinute = ChronoUnit.MINUTES.between(axisStart.toLocalDateTime(), wake.toLocalDateTime()).toInt(),
                sourceLabel = night.sourceLabel
            )
        }.sortedBy { it.day }
    }
}
