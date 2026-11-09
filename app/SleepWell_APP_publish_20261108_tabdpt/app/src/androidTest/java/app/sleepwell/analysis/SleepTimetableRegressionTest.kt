package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

@Suppress("DEPRECATION")
class SleepTimetableRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun testSevenDayRowsIncludeMissingDatesWithoutTreatingThemAsZero() {
        val nights = listOf(
            night("2026-11-05T14:00:00Z", "2026-11-05T22:00:00Z", 480),
            night("2026-11-03T15:00:00Z", "2026-11-03T22:00:00Z", 420)
        )

        val period = SleepTimetableData.period(nights, seoul, LocalDate.parse("2026-11-06"), 7)

        assertEquals(LocalDate.parse("2026-10-31"), period.startDate)
        assertEquals(7, period.rows.size)
        assertEquals(2, period.recordedDays)
        assertEquals(2, period.recordCount)
        assertEquals(450, period.averageIntervalMinutes)
        assertEquals(5, period.rows.count { it.nights.isEmpty() })
    }

    fun testThirtyDayPeriodKeepsInclusiveBoundariesAndExcludesOutsideRecords() {
        val end = LocalDate.parse("2026-11-30")
        val nights = listOf(
            localNight(LocalDate.parse("2026-11-01"), 23, 0, 7, 0),
            localNight(LocalDate.parse("2026-11-30"), 22, 0, 6, 0),
            localNight(LocalDate.parse("2026-10-31"), 23, 0, 7, 0)
        )

        val period = SleepTimetableData.period(nights, seoul, end, 30)

        assertEquals(30, period.rows.size)
        assertEquals(LocalDate.parse("2026-11-01"), period.startDate)
        assertEquals(2, period.recordCount)
        assertTrue(period.rows.first().nights.isNotEmpty())
        assertTrue(period.rows.last().nights.isNotEmpty())
    }

    fun testMidnightCrossingUsesPreviousEveningToWakeMorningAxis() {
        val wakeDate = LocalDate.parse("2026-11-05")
        val night = localNight(wakeDate, 23, 30, 7, 15)
        val period = SleepTimetableData.period(listOf(night), seoul, wakeDate, 7)

        assertEquals(-30.0, SleepTimetableData.wallMinute(night.bedtimeMs, wakeDate, seoul), 0.001)
        assertEquals(435.0, SleepTimetableData.wallMinute(night.wakeMs, wakeDate, seoul), 0.001)
        assertEquals(-360, period.axisStartMinute)
        assertEquals(720, period.axisEndMinute)
    }

    fun testDaySleepExpandsAxisInsteadOfClipping() {
        val wakeDate = LocalDate.parse("2026-11-05")
        val bedtime = wakeDate.atTime(13, 0).atZone(seoul).toInstant().toEpochMilli()
        val wake = wakeDate.atTime(19, 0).atZone(seoul).toInstant().toEpochMilli()
        val daytime = history(bedtime, wake, 360)

        val period = SleepTimetableData.period(listOf(daytime), seoul, wakeDate, 7)

        assertEquals(-360, period.axisStartMinute)
        assertTrue(period.axisEndMinute >= 19 * 60)
        assertTrue(period.axisEndMinute > SleepTimetableData.wallMinute(wake, wakeDate, seoul) - 1)
    }

    fun testCurrentZoneCollisionPreservesEveryRecord() {
        val wakeDate = LocalDate.parse("2026-11-05")
        val first = night("2026-11-04T12:00:00Z", "2026-11-04T22:00:00Z", 600)
        val second = night("2026-11-04T16:00:00Z", "2026-11-04T23:30:00Z", 450)

        val period = SleepTimetableData.period(listOf(first, second), seoul, wakeDate, 7)

        assertEquals(1, period.recordedDays)
        assertEquals(2, period.recordCount)
        assertEquals(listOf(first, second), period.rows.last().nights)
    }

    fun testDstWallClockDoesNotShiftWhileElapsedIntervalIsPreserved() {
        val zone = ZoneId.of("America/New_York")
        val wakeDate = LocalDate.parse("2026-03-08")
        val springForward = night("2026-03-08T04:30:00Z", "2026-03-08T11:30:00Z", 420)

        val period = SleepTimetableData.period(listOf(springForward), zone, wakeDate, 7)

        assertEquals(-30.0, SleepTimetableData.wallMinute(springForward.bedtimeMs, wakeDate, zone), 0.001)
        assertEquals(450.0, SleepTimetableData.wallMinute(springForward.wakeMs, wakeDate, zone), 0.001)
        assertEquals(420, period.averageIntervalMinutes)
    }

    fun testProjectionDoesNotMutateInputAndRejectsUnsafeDayCounts() {
        val first = night("2026-11-04T14:00:00Z", "2026-11-04T22:00:00Z", 480)
        val second = night("2026-11-03T14:00:00Z", "2026-11-03T22:00:00Z", 480)
        val input = mutableListOf(first, second)
        val snapshot = input.toList()

        SleepTimetableData.period(input, seoul, LocalDate.parse("2026-11-05"), 7)

        assertEquals(snapshot, input)
        assertFails { SleepTimetableData.period(input, seoul, LocalDate.parse("2026-11-05"), 0) }
        assertFails { SleepTimetableData.period(input, seoul, LocalDate.parse("2026-11-05"), 367) }
    }

    private fun localNight(wakeDate: LocalDate, bedHour: Int, bedMinute: Int, wakeHour: Int, wakeMinute: Int): SleepHistoryNight {
        val bedDate = if (bedHour >= 18) wakeDate.minusDays(1) else wakeDate
        val bed = bedDate.atTime(bedHour, bedMinute).atZone(seoul).toInstant().toEpochMilli()
        val wake = wakeDate.atTime(wakeHour, wakeMinute).atZone(seoul).toInstant().toEpochMilli()
        return history(bed, wake, ((wake - bed) / 60_000L).toInt())
    }

    private fun night(bed: String, wake: String, minutes: Int): SleepHistoryNight =
        history(Instant.parse(bed).toEpochMilli(), Instant.parse(wake).toEpochMilli(), minutes)

    private fun history(bed: Long, wake: Long, minutes: Int) = SleepHistoryNight(
        day = Instant.ofEpochMilli(wake).atZone(seoul).toLocalDate().toString(),
        bedtimeMs = bed,
        wakeMs = wake,
        intervalMinutes = minutes,
        source = "health_connect",
        sourceLabel = "헬스 커넥트",
        isManual = false,
        sleepLatencyMinutes = null,
        awakeMinutes = null,
        rested = null,
        fatigue = null,
        survey = null,
        observedAtMs = wake
    )

    private fun assertFails(block: () -> Unit) {
        try {
            block()
            fail("Expected IllegalArgumentException")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }
}
