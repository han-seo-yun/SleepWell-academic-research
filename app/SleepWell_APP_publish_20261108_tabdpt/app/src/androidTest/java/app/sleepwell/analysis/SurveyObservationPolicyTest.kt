package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.DailyContext
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class SurveyObservationPolicyTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun testCurrentNightRestoresLatestKnownSavedRevisionAcrossMidnight() {
        val now = atSeoul(2026, 11, 3, 1, 0)
        val rows = listOf(
            survey("2026-11-02", "06:30", "2026-11-02T14:00:00Z"),
            survey("2026-11-02", "07:00", "2026-11-02T15:30:00Z"),
            survey("2026-11-02", "08:00", "2026-11-02T16:30:00Z"), // 01:30 KST: future revision
            survey("2026-11-03", "09:00", "2026-11-02T15:00:00Z")
        )

        val selected = SurveyObservationPolicy.currentNight(rows, "me", now, usualWake = "07:00")

        assertNotNull(selected)
        assertEquals("2026-11-02", selected!!.survey.date)
        assertEquals("07:00", selected.survey.alarmTime)
        assertEquals(Instant.parse("2026-11-02T15:30:00Z"), selected.observedAt)
    }

    fun testHistoricalNightExcludesLateMissingAndMalformedObservations() {
        val rows = listOf(
            survey("2026-11-02", "06:30", "2026-11-02T10:00:00Z"),
            survey("2026-11-02", "07:00", "2026-11-02T10:59:00Z"),
            survey("2026-11-02", "08:00", "2026-11-02T11:01:00Z"), // after 20:00 KST
            survey("2026-11-02", "09:00", null),
            survey("2026-11-02", "10:00", "not-a-time")
        )

        val selected = SurveyObservationPolicy.historicalNight(rows, "me", "2026-11-02")

        assertNotNull(selected)
        assertEquals("07:00", selected!!.survey.alarmTime)
        assertEquals(Instant.parse("2026-11-02T10:59:00Z"), selected.observedAt)
        assertEquals(
            atSeoul(2026, 11, 2, 20, 0).toEpochMilli(),
            SurveyObservationPolicy.historicalCutoffMs("2026-11-02")
        )
    }

    fun testHistoricalEvaluationIsExplicitlyNonProspectiveAtActualCutoff() {
        val assets = instrumentation.targetContext.assets
        val engine = UserModelEngine({ name -> assets.open(name).use { it.readBytes() } })
        val cutoff = atSeoul(2026, 11, 2, 20, 0)
        val profile = UserProfile(
            ageBand = "18-29", sex = "any", activityMinutes = 60,
            usualBed = "23:30", usualWake = "07:00", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z",
            dailyContext = DailyContext(
                nightId = "2026-11-02", kind = "first",
                firstStart = atSeoul(2026, 11, 3, 7, 0).toString(), preparationMinutes = 0,
                napStart = null, napEnd = null, caffeineAt = null, caffeineMg = null,
                observedAt = atSeoul(2026, 11, 2, 19, 0).toString()
            )
        )

        val historical = engine.compute(profile, emptyList(), cutoff, historicalEvaluation = true)
        val live = engine.compute(profile, emptyList(), cutoff)

        assertEquals("historical_reconstruction_not_prospective", historical.outcomes.getString("forecast_status"))
        assertEquals("prospective_20_window", live.outcomes.getString("forecast_status"))
        assertTrue(historical.forecast.note.contains("재구성"))
    }

    private fun survey(date: String, alarm: String, savedAt: String?) = SurveyDay(
        participant = "me", date = date,
        caffeineUsed = null, caffeineMg = null, lastCaffeine = null,
        napMinutes = null, napEnd = null, exerciseMinutes = null, exerciseEnd = null,
        exerciseIntensity = null, fatigue = null, steps = null, heartRate = null,
        alarmTime = alarm, firstCommitment = null,
        examTomorrow = false, alcohol = false, illness = false,
        savedAt = savedAt
    )

    private fun atSeoul(year: Int, month: Int, day: Int, hour: Int, minute: Int): Instant =
        ZonedDateTime.of(year, month, day, hour, minute, 0, 0, seoul).toInstant()
}
