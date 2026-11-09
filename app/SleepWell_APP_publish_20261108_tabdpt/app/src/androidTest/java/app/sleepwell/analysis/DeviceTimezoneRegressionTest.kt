package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DisplayZone
import app.sleepwell.health.ShowFmt
import app.sleepwell.health.TimeFmt
import app.sleepwell.health.localHourTicks
import app.sleepwell.ui.UiZone
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.util.TimeZone
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.system.measureTimeMillis
import kotlinx.coroutines.runBlocking

@Suppress("DEPRECATION")
class DeviceTimezoneRegressionTest : InstrumentationTestCase() {
    private lateinit var previousDefault: TimeZone
    private lateinit var previousDisplay: DisplayZone

    override fun setUp() {
        super.setUp()
        previousDefault = TimeZone.getDefault()
        previousDisplay = DisplaySettings.zone
    }

    override fun tearDown() {
        TimeZone.setDefault(previousDefault)
        refreshZone()
        DisplaySettings.zone = previousDisplay
        super.tearDown()
    }

    fun testSystemRefreshChangesDateAndClockWithoutChangingInstantOrExport() {
        val ms = Instant.parse("2026-11-05T00:30:00Z").toEpochMilli()
        val exported = TimeFmt.iso(ms)
        TimeZone.setDefault(TimeZone.getTimeZone("Asia/Seoul"))
        refreshZone()
        assertEquals("09:30", ShowFmt.hhmm(ms))
        TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
        refreshZone()
        assertEquals("America/Los_Angeles", UiZone.current.zoneId)
        assertEquals("2026-11-04", ShowFmt.day(ms))
        assertEquals("16:30", ShowFmt.hhmm(ms))
        assertEquals(exported, TimeFmt.iso(ms))
    }

    fun testHalfAndQuarterHourZonesFollowDevice() {
        val ms = Instant.parse("2026-11-05T00:00:00Z").toEpochMilli()
        listOf("Asia/Kolkata" to "05:30", "Asia/Kathmandu" to "05:45").forEach { (id, clock) ->
            TimeZone.setDefault(TimeZone.getTimeZone(id))
            refreshZone()
            assertEquals(clock, ShowFmt.hhmm(ms))
            val ticks = localHourTicks(ms, ms + 6 * 3_600_000L, 1, UiZone.current.tz)
            assertTrue(ticks.isNotEmpty())
            assertTrue(ticks.all { ShowFmt.hhmm(it).endsWith(":00") })
        }
    }

    fun testSpringForwardTicksSkipNonexistentHourAndKeepLocalThreeHourGrid() {
        val zone = TimeZone.getTimeZone("America/New_York")
        val start = LocalDate.parse("2026-03-08").atStartOfDay(zone.toZoneId()).toInstant().toEpochMilli()
        val end = LocalDate.parse("2026-03-09").atStartOfDay(zone.toZoneId()).toInstant().toEpochMilli()
        val hourly = localHourTicks(start, end - 1, 1, zone)
        assertEquals(23, hourly.size)
        assertFalse(hourly.any { ShowFmt.hhmm(it, zone) == "02:00" })
        assertEquals(listOf("00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"),
            localHourTicks(start, end - 1, 3, zone).map { ShowFmt.hhmm(it, zone) })
    }

    fun testFallBackTicksPreserveBothRepeatedHours() {
        val zone = TimeZone.getTimeZone("America/New_York")
        val start = LocalDate.parse("2026-11-01").atStartOfDay(zone.toZoneId()).toInstant().toEpochMilli()
        val end = LocalDate.parse("2026-11-02").atStartOfDay(zone.toZoneId()).toInstant().toEpochMilli()
        val hourly = localHourTicks(start, end - 1, 1, zone)
        assertEquals(25, hourly.size)
        assertEquals(2, hourly.count { ShowFmt.hhmm(it, zone) == "01:00" })
        assertTrue(hourly.zipWithNext().all { (a, b) -> b > a })
    }

    fun testDisplayedDayListUsesLocalDatesAcrossDst() {
        val zone = TimeZone.getTimeZone("America/New_York")
        val end = LocalDate.parse("2026-11-02").atTime(12, 0).atZone(zone.toZoneId()).toInstant().toEpochMilli()
        val start = LocalDate.parse("2026-10-31").atTime(7, 0).atZone(zone.toZoneId()).toInstant().toEpochMilli()
        val direct = app.sleepwell.health.DirectInput(start - 8 * 3_600_000L, start, null, null)
        val days = AnalysisPresenter.days(emptyList(), direct, end, start, zone)
        assertEquals(listOf("2026-10-31", "2026-11-01", "2026-11-02"), days.map { ShowFmt.day(it.startMs, zone) })
        assertEquals(25 * 3_600_000L, days[2].startMs - days[1].startMs)
    }

    fun testInstalledEngineTimezoneRefreshDoesNotWaitForBusyModelOnMainThread() {
        verifyInstalledEngineTimezoneRefreshDoesNotWaitForBusyModelOnMainThread()
    }

    // Keep lambdas out of a JUnit3 method whose name starts with "test". The legacy runner
    // otherwise discovers generated test...$lambda methods as separate, invalid tests.
    private fun verifyInstalledEngineTimezoneRefreshDoesNotWaitForBusyModelOnMainThread() {
        NextNightModel.install({ name ->
            instrumentation.targetContext.assets.open(name).use { it.readBytes() }
        }, ZoneId.of("Asia/Seoul"))
        TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
        val lockHeld = CountDownLatch(1)
        val release = CountDownLatch(1)
        val worker = Executors.newSingleThreadExecutor()
        worker.execute {
            synchronized(NextNightModel) {
                lockHeld.countDown()
                release.await(5, TimeUnit.SECONDS)
            }
        }
        assertTrue(lockHeld.await(2, TimeUnit.SECONDS))

        val elapsed = try {
            measureTimeMillis {
                instrumentation.runOnMainSync { UiZone.refreshFromSystem() }
            }
        } finally {
            release.countDown()
            worker.shutdown()
        }

        assertTrue("main-thread refresh waited ${elapsed}ms for the model lock", elapsed < 1_000)
        assertTrue(worker.awaitTermination(3, TimeUnit.SECONDS))
        runBlocking { UiZone.awaitRefresh() }
        assertEquals("America/Los_Angeles", NextNightModel.currentCalculationZone().id)
        assertEquals("America/Los_Angeles", UiZone.current.zoneId)
    }

    // Legacy JUnit3 also discovers generated SAM methods starting with "test".
    private fun refreshZone() {
        instrumentation.runOnMainSync { UiZone.refreshFromSystem() }
        runBlocking { UiZone.awaitRefresh() }
    }
}
