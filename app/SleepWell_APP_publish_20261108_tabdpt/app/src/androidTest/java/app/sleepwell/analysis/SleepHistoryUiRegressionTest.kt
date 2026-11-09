package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.SleepRecord
import app.sleepwell.health.SleepStage
import app.sleepwell.health.SleepStageSegment
import java.time.Instant
import java.time.ZoneId
import java.time.LocalDate

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

    fun testNewAndLegacyQuestionnaireScoresKeepTheirOwnDisplayDenominator() {
        val completed = record(
            id = "wearable:health_connect:scale", day = "2026-11-04",
            bed = "2026-11-03T14:00:00Z", wake = "2026-11-03T22:00:00Z",
            source = "health_connect", observed = "2026-11-03T23:00:00Z"
        )
        val legacy = survey("me", "2026-11-04", "2026-11-04T01:00:00Z", fatigue = 4)
        val current = legacy.copy(fatigueScaleMax = 5)
        for (answer in listOf(legacy, current)) {
            val night = SleepHistoryUi.merge(listOf(completed), emptyList(), listOf(answer), now.toEpochMilli()).single()
            assertEquals(4, night.fatigue)
            assertEquals(answer.fatigueScaleMax, night.fatigueScaleMax)
            assertTrue(night.survey!!.summaryIn(ZoneId.of("Asia/Seoul"))
                .contains("피로도 4/${answer.fatigueScaleMax}"))
        }
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

    fun testStageTimelineUsesExactWearableIdentityAndNeverLeaksIntoManualOverride() {
        val wearable = record(
            id = "wearable:health_connect:exact", day = "2026-11-04",
            bed = "2026-11-03T14:00:00Z", wake = "2026-11-03T22:00:00Z",
            source = "health_connect", observed = "2026-11-03T23:00:00Z"
        )
        val deep = SleepStageSegment(
            Instant.parse("2026-11-03T15:00:00Z").toEpochMilli(),
            Instant.parse("2026-11-03T16:00:00Z").toEpochMilli(),
            SleepStage.DEEP
        )
        val exact = SleepHistoryUi.merge(
            listOf(wearable), emptyList(), emptyList(), now.toEpochMilli(),
            stageSegmentsByRecordId = mapOf(wearable.id to listOf(deep))
        ).single()
        assertEquals(listOf(deep), exact.stageSegments)
        assertEquals(mapOf("deep" to 60), exact.stageMinutes)

        val manual = wearable.copy(id = "diary:2026-11-04", source = "manual", modifiedAt = "2026-11-04T00:00:00Z")
        val overridden = SleepHistoryUi.merge(
            listOf(wearable), listOf(manual), emptyList(), now.toEpochMilli(),
            stageSegmentsByRecordId = mapOf(wearable.id to listOf(deep))
        ).single()
        assertTrue(overridden.isManual)
        assertTrue(overridden.stageSegments.isEmpty())
        assertTrue(overridden.stageMinutes.isEmpty())
    }

    fun testTrendProjectionUsesCurrentZoneAndOmitsMissingDays() {
        val nights = listOf(
            historyNight("2026-11-05", "2026-11-04T15:00:00Z", "2026-11-04T23:00:00Z"),
            historyNight("2026-11-03", "2026-11-02T14:30:00Z", "2026-11-02T22:30:00Z")
        )
        val points = SleepChartData.trend(nights, ZoneId.of("Asia/Seoul"), now.toEpochMilli(), 7)

        assertEquals(2, points.size)
        assertEquals(listOf("2026-11-03", "2026-11-05"), points.map { it.day.toString() })
        assertEquals(listOf(480, 480), points.map { it.durationMinutes })
        assertEquals(690, points.first().bedtimeAxisMinute) // 23:30, measured from the previous noon
        assertEquals(1170, points.first().wakeAxisMinute) // 07:30 next morning
    }

    fun testStaleLastRecordKeepsBlankTrailingDaysBeforeTodayAxisEnd() {
        val today = LocalDate.parse("2026-11-05")
        val staleLastRecord = LocalDate.parse("2026-11-03")

        val staleFraction = SleepChartData.axisFraction(staleLastRecord, today, 7)

        assertEquals(4f / 6f, staleFraction, 0.0001f)
        assertEquals(1f, SleepChartData.axisFraction(today, today, 7), 0f)
        assertTrue(staleFraction < 1f)
    }

    fun testFractionalStageDurationsUseRawUnionCoverageForCompleteVsPartial() {
        val completed = record(
            id = "wearable:health_connect:fractional", day = "2026-11-04",
            bed = "2026-11-03T14:00:00Z", wake = "2026-11-03T21:30:00Z",
            source = "health_connect", observed = "2026-11-03T22:00:00Z"
        )
        val bed = Instant.parse(completed.bedtime).toEpochMilli()
        val wake = Instant.parse(completed.wakeTime).toEpochMilli()
        val split = bed + 10 * 60_000L + 30_000L
        val full = listOf(
            SleepStageSegment(bed, split, SleepStage.REM),
            SleepStageSegment(split, wake, SleepStage.LIGHT)
        )
        val fullNight = SleepHistoryUi.merge(
            listOf(completed), emptyList(), emptyList(), now.toEpochMilli(),
            stageSegmentsByRecordId = mapOf(completed.id to full)
        ).single()

        assertEquals(449, fullNight.stageMinutes.values.sum()) // per-stage display rounding remains conservative
        assertEquals(450, fullNight.stageObservedMinutes)
        assertEquals(wake - bed, fullNight.stageObservedCoverageMs)

        val withGap = full.toMutableList().apply { this[1] = this[1].copy(startMs = split + 60_000L) }
        val partialNight = SleepHistoryUi.merge(
            listOf(completed), emptyList(), emptyList(), now.toEpochMilli(),
            stageSegmentsByRecordId = mapOf(completed.id to withGap)
        ).single()
        assertEquals(wake - bed - 60_000L, partialNight.stageObservedCoverageMs)
        assertTrue(partialNight.stageObservedCoverageMs!! < wake - bed)
    }

    fun testClockTrendUsesWallTimeAcrossDaylightSavingTransition() {
        val dstNight = historyNight(
            "2026-03-08", "2026-03-08T04:30:00Z", "2026-03-08T11:30:00Z"
        )
        val zone = ZoneId.of("America/New_York")
        val points = SleepChartData.trend(
            listOf(dstNight), zone, Instant.parse("2026-03-08T12:00:00Z").toEpochMilli(), 7
        )

        assertEquals(1, points.size)
        assertEquals(690, points.single().bedtimeAxisMinute) // 23:30 wall clock
        assertEquals(1170, points.single().wakeAxisMinute) // 07:30 wall clock despite spring-forward
        assertEquals(420, points.single().durationMinutes) // real elapsed duration remains seven hours
    }

    private fun historyNight(day: String, bed: String, wake: String): SleepHistoryNight {
        val bedMs = Instant.parse(bed).toEpochMilli()
        val wakeMs = Instant.parse(wake).toEpochMilli()
        return SleepHistoryNight(
            day, bedMs, wakeMs, ((wakeMs - bedMs) / 60_000L).toInt(), "health_connect", "헬스 커넥트",
            false, null, null, null, null, null, wakeMs, emptyMap(), null, null
        )
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
