package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.health.TwoProcessModel
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class DeviceTimezoneConsistencyTest : InstrumentationTestCase() {

    fun testDirectInputUsesEntryZoneForInstantSurveyAndDiaryDate() {
        val utc = ZoneId.of("UTC")
        val now = Instant.parse("2026-11-06T10:38:00Z")
        val parsed = DirectInputParser.parse(
            DirectInputForm(
                bedText = "2026-11-05 23:00",
                wakeText = "2026-11-06 07:30",
                targetWakeText = "07:30",
                condition = 3
            ),
            nowMs = now.toEpochMilli(),
            zoneId = utc
        )

        assertTrue(parsed.errors.toString(), parsed.errors.isEmpty())
        assertEquals(Instant.parse("2026-11-05T23:00:00Z").toEpochMilli(), parsed.input!!.sleepStartMs)
        assertEquals(Instant.parse("2026-11-07T07:30:00Z").toEpochMilli(), parsed.input.targetWakeMs)
        assertEquals("UTC", parsed.survey!!.sourceZoneId)
        assertEquals("2026-11-06", parsed.survey.sleepWakeDate)
        assertEquals("2026-11-06", parsed.diary!!.day)
    }

    fun testSurveyContextDoesNotBorrowAnotherDeviceZone() {
        val now = Instant.parse("2026-11-06T18:00:00Z")
        val rows = listOf(
            survey("Asia/Seoul", "2026-11-06T16:00:00Z", "06:30"),
            survey("UTC", "2026-11-06T17:00:00Z", "07:30")
        )

        val selected = SurveyObservationPolicy.currentNight(
            rows, "me", now, usualWake = "07:30", zoneId = ZoneId.of("UTC")
        )

        assertNotNull(selected)
        assertEquals("UTC", selected!!.survey.sourceZoneId)
        assertEquals("07:30", selected.survey.alarmTime)
    }

    fun testExistingCrossZoneSurveyIsMappedByItsObservedInstant() {
        val utc = ZoneId.of("UTC")
        val observed = Instant.parse("2026-11-06T17:00:00Z")
        val selected = SurveyObservationPolicy.currentNight(
            listOf(survey("Asia/Seoul", observed.toString(), "07:30")),
            "me", observed.plusSeconds(60), usualWake = "07:30", zoneId = utc
        )

        assertNotNull(selected)
        assertEquals(observed, selected!!.observedAt)
        assertEquals("Asia/Seoul", selected.survey.sourceZoneId)
    }

    fun testTwoProcessClockIsInvariantForTheSameLocalTimeline() {
        val kst = ZoneId.of("Asia/Seoul")
        val utc = ZoneId.of("UTC")
        val kstStart = ZonedDateTime.of(2026, 11, 5, 23, 0, 0, 0, kst).toInstant().toEpochMilli()
        val utcStart = ZonedDateTime.of(2026, 11, 5, 23, 0, 0, 0, utc).toInstant().toEpochMilli()
        val offsets = listOf(0L, 60L, 120L, 180L).map { it * 60_000L }
        val sleeping = listOf(false, true, true, false)

        val inKst = TwoProcessModel.compute(offsets.map { kstStart + it }, sleeping, kst)
        val inUtc = TwoProcessModel.compute(offsets.map { utcStart + it }, sleeping, utc)

        assertEquals(inKst.currentC, inUtc.currentC, 1e-12)
        assertEquals(inKst.currentPropensity, inUtc.currentPropensity, 1e-12)
        assertEquals(inKst.phaseRef, inUtc.phaseRef, 1e-12)
    }

    fun testDiaryKeepsLegacyAndCurrentZoneDayMetadataReadable() {
        val root = File(instrumentation.targetContext.cacheDir, "timezone-diary-${System.nanoTime()}")
        val file = File(root, DiaryStore.FILE_NAME)
        try {
            val current = record("2026-11-06", "2026-11-05T23:00:00Z", "2026-11-06T07:30:00Z")
            DiaryStore.save(file, current)
            assertEquals(current, DiaryStore.load(file).single())
        } finally {
            root.deleteRecursively()
        }
    }

    private fun survey(zone: String, savedAt: String, alarm: String) = SurveyDay(
        participant = "me", date = "2026-11-06",
        caffeineUsed = null, caffeineMg = null, lastCaffeine = null,
        napMinutes = null, napEnd = null, exerciseMinutes = null, exerciseEnd = null,
        exerciseIntensity = null, fatigue = null, steps = null, heartRate = null,
        alarmTime = alarm, firstCommitment = null,
        examTomorrow = false, alcohol = false, illness = false,
        savedAt = savedAt, sourceZoneId = zone, sourceDate = "2026-11-06"
    )

    private fun record(day: String, bed: String, wake: String) = app.sleepwell.user.SleepRecord(
        id = "diary:$day", day = day, bedtime = bed, wakeTime = wake,
        durationMinutes = 510, rested = null, fatigue = null, source = "manual",
        modifiedAt = "2026-11-06T10:38:00Z"
    )
}
