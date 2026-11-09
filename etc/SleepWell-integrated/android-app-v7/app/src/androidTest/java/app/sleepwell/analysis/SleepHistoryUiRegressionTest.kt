package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.SleepRecord
import java.time.Instant
import java.time.ZoneId

@Suppress("DEPRECATION")
class SleepHistoryUiRegressionTest : InstrumentationTestCase() {
    private val now = Instant.parse("2026-11-05T12:00:00Z")

    fun testManualNightWinsSameCanonicalDayAndSupplementJoinsOnlyMyRecord() {
        val wearable = record(
            id = "wearable:health_connect:watch", day = "2026-11-04",
            bed = "2026-11-03T14:00:00Z", wake = "2026-11-03T22:00:00Z",
            source = "samsung_health", observed = "2026-11-03T23:00:00Z"
        )
        val manual = wearable.copy(
            id = "diary:2026-11-04", source = "manual",
            bedtime = "2026-11-03T14:30:00Z", durationMinutes = 450,
            modifiedAt = "2026-11-04T00:00:00Z", sleepLatencyMinutes = 20
        )
        val myAnswer = survey("me", "2026-11-04", "2026-11-04T01:00:00Z", fatigue = 6)
        val testAnswer = survey("Test", "2026-11-04", "2026-11-04T02:00:00Z", fatigue = 1)

        val merged = SleepHistoryUi.merge(listOf(wearable), listOf(manual), listOf(myAnswer, testAnswer), now.toEpochMilli())

        assertEquals(1, merged.size)
        assertTrue(merged.single().isManual)
        assertEquals("manual", merged.single().source)
        assertEquals("직접 기록", merged.single().sourceLabel)
        assertEquals(20, merged.single().sleepLatencyMinutes)
        assertEquals(6, merged.single().fatigue)
        assertEquals(7, merged.single().fatigueScaleMax)
        assertEquals("me", merged.single().survey?.participant)
    }

    fun testFutureOrUnobservedRowsNeverAppearAndPreWakeAnswerDoesNotJoin() {
        val completed = record(
            id = "wearable:health_connect:complete", day = "2026-11-04",
            bed = "2026-11-03T14:00:00Z", wake = "2026-11-03T22:00:00Z",
            source = "health_connect", observed = "2026-11-03T23:00:00Z"
        )
        val futureWake = record(
            id = "wearable:health_connect:future-wake", day = "2026-11-06",
            bed = "2026-11-05T14:00:00Z", wake = "2026-11-05T22:00:00Z",
            source = "health_connect", observed = "2026-11-05T23:00:00Z"
        )
        val futureReceipt = completed.copy(id = "diary:future", source = "manual", modifiedAt = "2026-11-06T00:00:00Z")
        val preWake = survey("me", "2026-11-04", "2026-11-03T21:59:00Z", fatigue = 7)

        val merged = SleepHistoryUi.merge(
            listOf(completed, futureWake), listOf(futureReceipt), listOf(preWake), now.toEpochMilli()
        )

        assertEquals(listOf("wearable:health_connect:complete"), merged.map { completed.id })
        assertNull(merged.single().survey)
        assertNull(merged.single().fatigue)
    }

    fun testLinkedSurveyKeepsAbsoluteEventsForCurrentDisplayTimezone() {
        val completed = record(
            id = "wearable:samsung_health:zone", day = "2026-11-04",
            bed = "2026-11-03T14:00:00Z", wake = "2026-11-03T22:00:00Z",
            source = "samsung_health", observed = "2026-11-03T23:00:00Z"
        )
        val napEnd = Instant.parse("2026-11-03T07:00:00Z").toEpochMilli()
        val answer = survey("me", "2026-11-04", "2026-11-04T01:00:00Z", fatigue = 4)
            .copy(napMinutes = 30.0, napEndMs = napEnd, sourceZoneId = "Asia/Seoul")

        val night = SleepHistoryUi.merge(
            listOf(completed), emptyList(), listOf(answer), now.toEpochMilli(),
            stagesByRecordId = mapOf(completed.id to mapOf("light" to 260, "deep" to 80, "rem" to 70))
        ).single()

        assertEquals(Instant.parse("2026-11-03T14:00:00Z").toEpochMilli(), night.bedtimeMs)
        assertEquals("삼성 헬스", night.sourceLabel)
        assertEquals(410, night.stageObservedMinutes)
        assertTrue(night.survey!!.summaryIn(ZoneId.of("America/Los_Angeles"), includeAlarm = false)
            .contains("11/2 23:00 종료"))
    }

    private fun record(
        id: String, day: String, bed: String, wake: String, source: String, observed: String
    ): SleepRecord {
        val minutes = java.time.Duration.between(Instant.parse(bed), Instant.parse(wake)).toMinutes().toInt()
        return SleepRecord(
            id = id, day = day, bedtime = bed, wakeTime = wake, durationMinutes = minutes,
            rested = null, fatigue = null, source = source, modifiedAt = observed
        )
    }

    private fun survey(participant: String, wakeDate: String, savedAt: String, fatigue: Int) = SurveyDay(
        participant = participant, date = wakeDate,
        caffeineUsed = null, caffeineMg = null, lastCaffeine = null,
        napMinutes = null, napEnd = null, exerciseMinutes = null, exerciseEnd = null,
        exerciseIntensity = null, fatigue = fatigue, steps = null, heartRate = null,
        alarmTime = null, firstCommitment = null, examTomorrow = false, alcohol = false, illness = false,
        savedAt = savedAt, sleepWakeDate = wakeDate
    )
}
