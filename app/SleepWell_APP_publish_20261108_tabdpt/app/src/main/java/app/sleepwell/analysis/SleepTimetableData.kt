package app.sleepwell.analysis

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.temporal.ChronoUnit
import kotlin.math.ceil
import kotlin.math.floor

/** A single wake-date row in the read-only history timetable. */
data class SleepTimetableRow(
    val date: LocalDate,
    val nights: List<SleepHistoryNight>
)

/** Display-only projection. It never changes the stored record or personalization selection. */
data class SleepTimetablePeriod(
    val startDate: LocalDate,
    val endDate: LocalDate,
    val rows: List<SleepTimetableRow>,
    val recordedDays: Int,
    val recordCount: Int,
    val averageIntervalMinutes: Int?,
    val axisStartMinute: Int,
    val axisEndMinute: Int
)

object SleepTimetableData {
    private const val DEFAULT_AXIS_START = -6 * 60 // previous day 18:00
    private const val DEFAULT_AXIS_END = 12 * 60 // wake date 12:00
    private const val AXIS_STEP_MINUTES = 3 * 60
    private const val MAX_PERIOD_DAYS = 366

    fun period(
        nights: List<SleepHistoryNight>,
        zone: ZoneId,
        endDate: LocalDate,
        days: Int
    ): SleepTimetablePeriod {
        require(days in 1..MAX_PERIOD_DAYS) { "days must be between 1 and $MAX_PERIOD_DAYS" }
        val startDate = endDate.minusDays(days.toLong() - 1L)
        val inPeriod = nights.asSequence()
            .map { night -> Instant.ofEpochMilli(night.wakeMs).atZone(zone).toLocalDate() to night }
            .filter { (date, _) -> date >= startDate && date <= endDate }
            .toList()
        val byWakeDate = inPeriod.groupBy({ it.first }, { it.second })
        val rows = (0 until days).map { offset ->
            val date = startDate.plusDays(offset.toLong())
            SleepTimetableRow(date, byWakeDate[date].orEmpty())
        }
        val shownNights = rows.flatMap { it.nights }
        val axisValues = rows.flatMap { row ->
            row.nights.flatMap { night ->
                listOf(
                    wallMinute(night.bedtimeMs, row.date, zone),
                    wallMinute(night.wakeMs, row.date, zone)
                )
            }
        }
        val axisStart = axisValues.minOrNull()
            ?.let { floor(minOf(it, DEFAULT_AXIS_START.toDouble()) / AXIS_STEP_MINUTES).toInt() * AXIS_STEP_MINUTES }
            ?: DEFAULT_AXIS_START
        val axisEnd = axisValues.maxOrNull()
            ?.let { ceil(maxOf(it, DEFAULT_AXIS_END.toDouble()) / AXIS_STEP_MINUTES).toInt() * AXIS_STEP_MINUTES }
            ?: DEFAULT_AXIS_END

        return SleepTimetablePeriod(
            startDate = startDate,
            endDate = endDate,
            rows = rows,
            recordedDays = rows.count { it.nights.isNotEmpty() },
            recordCount = shownNights.size,
            averageIntervalMinutes = shownNights.takeIf { it.isNotEmpty() }
                ?.map { it.intervalMinutes }
                ?.average()
                ?.toInt(),
            axisStartMinute = axisStart,
            axisEndMinute = axisEnd
        )
    }

    /**
     * Local civil-clock minute relative to midnight of [wakeDate]. This intentionally ignores
     * offset changes at DST boundaries; elapsed duration remains the record's intervalMinutes.
     */
    fun wallMinute(epochMs: Long, wakeDate: LocalDate, zone: ZoneId): Double {
        val local = Instant.ofEpochMilli(epochMs).atZone(zone).toLocalDateTime()
        val dayOffset = ChronoUnit.DAYS.between(wakeDate, local.toLocalDate())
        return dayOffset * 1_440.0 + local.hour * 60.0 + local.minute +
            local.second / 60.0 + local.nano / 60_000_000_000.0
    }
}
