package app.sleepwell.ui

import android.test.InstrumentationTestCase
import app.sleepwell.analysis.SavedResult
import app.sleepwell.analysis.SleepHistoryNight
import app.sleepwell.analysis.SurveyDay
import java.time.Instant
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZoneId

@Suppress("DEPRECATION")
class HistoryCalendarProjectionTest : InstrumentationTestCase() {
    fun testWakeDateUsesCurrentDeviceZone() {
        val night = night("2026-11-05T23:30:00Z")
        val seoul = HistoryCalendarProjection.sleepDate(night, ZoneId.of("Asia/Seoul"))
        val utc = HistoryCalendarProjection.sleepDate(night, ZoneId.of("UTC"))
        assertEquals(LocalDate.of(2026, 11, 6), seoul)
        assertEquals(LocalDate.of(2026, 11, 5), utc)
    }

    fun testSavedDayDateWinsAndLegacyBasisFallsBackToDisplayZone() {
        val basis = Instant.parse("2026-11-05T23:30:00Z").toEpochMilli()
        assertEquals(LocalDate.of(2026, 10, 31),
            HistoryCalendarProjection.resultDate(result(basis, "2026-10-31"), ZoneId.of("Asia/Seoul")))
        assertEquals(LocalDate.of(2026, 11, 6),
            HistoryCalendarProjection.resultDate(result(basis, ""), ZoneId.of("Asia/Seoul")))
    }

    fun testNewRawResultMovesWithCurrentZoneWhileLegacyDayDateStaysFixed() {
        val basis = Instant.parse("2026-11-05T23:30:00Z").toEpochMilli()
        val raw = result(basis, "2026-11-05").copy(sourceZoneId = "UTC")
        assertEquals(LocalDate.of(2026, 11, 5),
            HistoryCalendarProjection.resultDate(raw, ZoneId.of("UTC")))
        assertEquals(LocalDate.of(2026, 11, 6),
            HistoryCalendarProjection.resultDate(raw, ZoneId.of("Asia/Seoul")))

        val legacy = result(basis, "2026-11-05").copy(sourceZoneId = null)
        assertEquals(LocalDate.of(2026, 11, 5),
            HistoryCalendarProjection.resultDate(legacy, ZoneId.of("Asia/Seoul")))
    }

    fun testEmptyDateIsNotInventedAndSyntheticResultIsHidden() {
        val real = result(Instant.parse("2026-11-05T02:00:00Z").toEpochMilli(), "2026-11-05")
        val fake = real.copy(savedAtMs = 2, source = "Demo test")
        val days = HistoryCalendarProjection.days(emptyList(), listOf(real, fake), ZoneId.of("Asia/Seoul"))
        assertEquals(1, days.size)
        assertEquals(1, days[LocalDate.of(2026, 11, 5)]!!.savedResults.size)
        assertFalse(days.containsKey(LocalDate.of(2026, 11, 4)))
    }

    fun testSyntheticResultIsVisibleOnlyWhenExplicitlyIncluded() {
        val fake = result(Instant.parse("2026-11-05T02:00:00Z").toEpochMilli(), "2026-11-05")
            .copy(source = "시연 재분석")
        assertTrue(HistoryCalendarProjection.days(emptyList(), listOf(fake), ZoneId.of("Asia/Seoul")).isEmpty())
        val included = HistoryCalendarProjection.days(emptyList(), listOf(fake), ZoneId.of("Asia/Seoul"),
            includeSyntheticResults = true)
        assertEquals(1, included[LocalDate.of(2026, 11, 5)]!!.savedResults.size)
    }

    fun testSurveysProjectToNightAndWakeDatesWithoutDuplicates() {
        val context = survey("2026-11-05")
        val supplement = survey("2026-11-05").copy(
            sleepWakeDate = "2026-11-06",
            sleepWakeMs = Instant.parse("2026-11-05T23:30:00Z").toEpochMilli(),
            rested = 4
        )
        val days = HistoryCalendarProjection.days(emptyList(), emptyList(), ZoneId.of("Asia/Seoul"),
            listOf(context, supplement))
        assertEquals(listOf(LocalDate.of(2026, 11, 5), LocalDate.of(2026, 11, 6)), days.keys.toList())
        assertEquals(1, days[LocalDate.of(2026, 11, 5)]!!.surveys.size)
        assertEquals(1, days[LocalDate.of(2026, 11, 6)]!!.surveys.size)
    }

    fun testSyntheticParticipantSurveyIsExcludedFromPersonalHistory() {
        val days = HistoryCalendarProjection.days(emptyList(), emptyList(), ZoneId.of("Asia/Seoul"),
            listOf(survey("2026-11-05"), survey("2026-11-06").copy(participant = "Test")))
        assertEquals(setOf(LocalDate.of(2026, 11, 5)), days.keys)
        assertEquals(listOf("me"), days.values.single().surveys.map { it.participant })
    }

    fun testCompletedSurveyWakeInstantUsesCurrentDeviceZone() {
        val supplement = survey("2026-11-05").copy(
            sleepWakeDate = "2026-11-06",
            sleepWakeMs = Instant.parse("2026-11-05T23:30:00Z").toEpochMilli(),
            rested = 4
        )
        assertEquals(LocalDate.of(2026, 11, 6), HistoryCalendarProjection.surveyDate(supplement, ZoneId.of("Asia/Seoul")))
        assertEquals(LocalDate.of(2026, 11, 5), HistoryCalendarProjection.surveyDate(supplement, ZoneId.of("UTC")))
    }

    fun testMonthCellsStartsSundayAndPadsWholeWeeks() {
        val cells = HistoryCalendarProjection.monthCells(YearMonth.of(2026, 11))
        assertEquals(LocalDate.of(2026, 11, 1), cells.first())
        assertEquals(0, cells.size % 7)
        assertEquals(LocalDate.of(2026, 11, 30), cells.filterNotNull().last())
    }

    private fun night(wake: String): SleepHistoryNight {
        val end = Instant.parse(wake).toEpochMilli()
        return SleepHistoryNight("2026-11-05", end - 8 * 3_600_000L, end, 480,
            "health_connect", "헬스 커넥트", false, null, null, null, null, null, end)
    }

    private fun result(basis: Long, day: String) = SavedResult(
        savedAtMs = 1, source = "health.csv", basisMs = basis, basisText = "기준",
        modelText = "모델", recText = "권장", habitText = "평소", statusText = "상태",
        surveyText = "", sValue = "0.5", cValue = "0.1", pValue = "0.6", dayDate = day
    )

    private fun survey(day: String) = SurveyDay(
        participant = "me", date = day, caffeineUsed = null, caffeineMg = null,
        lastCaffeine = null, napMinutes = null, napEnd = null, exerciseMinutes = null,
        exerciseEnd = null, exerciseIntensity = null, fatigue = null, steps = null,
        heartRate = null, alarmTime = "07:30", firstCommitment = "09:00",
        examTomorrow = false, alcohol = false, illness = false, sourceZoneId = "Asia/Seoul"
    )
}
