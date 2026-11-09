package app.sleepwell.user

import android.test.InstrumentationTestCase
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class MeasurementRecommendationPolicyRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")
    private val usualBed = LocalTime.of(23, 0)
    private val usualWake = LocalTime.of(7, 0)

    fun testSupportedSubsetMatchesPythonV7NoHistoryGolden() {
        val now = at(2026, 11, 3, 20, 0)
        val result = plan(now, wake = at(2026, 11, 4, 7, 0))

        assertEquals("2026-11-03", result.nightId.toString())
        assertEquals(at(2026, 11, 3, 22, 45), result.bed)
        assertEquals(at(2026, 11, 4, 7, 0), result.wake)
        assertEquals(495, result.sleepOpportunityMinutes)
        assertEquals(480.0, result.desiredSleepMinutes, 0.0)
        assertTrue(result.feasible)
        assertEquals(0.5558851198190611, result.currentS, 1e-12)
        assertNull(result.observedSleepMinutes)
        assertNull(result.sleepDebtMinutes)
    }

    fun testAfterMidnightPlansRemainderOfPreviousNight() {
        val now = at(2026, 11, 4, 1, 0)
        val result = plan(now, wake = at(2026, 11, 4, 7, 0))

        assertEquals("2026-11-03", result.nightId.toString())
        assertEquals(now, result.bed)
        assertEquals(360, result.sleepOpportunityMinutes)
        assertFalse(result.feasible)
        assertEquals(0.3872068062385517, result.currentS, 1e-12)
    }

    fun testEngineKeepsLiveRecommendationNightSeparateFromForecastCutoffAfterMidnight() {
        val now = at(2026, 11, 4, 1, 0)
        val profile = UserProfile(
            ageBand = "20-29", sex = "female", activityMinutes = 60,
            usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z"
        )

        val output = UserModelEngine(instrumentation.targetContext).compute(profile, emptyList(), now.toInstant())

        assertEquals("2026-11-03", output.outcomes.getString("recommendation_night_id"))
        assertEquals("2026-11-04", output.outcomes.getString("research_forecast_night_id"))
        assertTrue(output.recommendation.wakeTime.startsWith("2026-11-04T07:00"))
        assertEquals(360, output.recommendation.durationMinutes)
        assertFalse(output.recommendation.planFeasible)
    }

    fun testAdequateCompleteDiaryAppliesBoundedDebtExtension() {
        val now = at(2026, 11, 3, 20, 0)
        val recent = (1..7).map { daysAgo ->
            val wake = at(2026, 11, 3, 8, 0).minusDays(daysAgo.toLong())
            MeasurementRecommendationPolicy.CompleteDiary(
                bed = wake.minusMinutes(450), wake = wake,
                latencyMinutes = 15, sleepMinutes = 420.0
            )
        }
        val adequate = plan(now, at(2026, 11, 4, 7, 0), adequate = true, diaries = recent)
        val conservative = plan(now, at(2026, 11, 4, 7, 0), adequate = false, diaries = recent)

        assertEquals(420.0, adequate.observedSleepMinutes ?: -1.0, 0.0)
        assertEquals(60.0, adequate.sleepDebtMinutes ?: -1.0, 0.0)
        assertEquals(15.0, adequate.extensionMinutes, 0.0)
        assertEquals(495.0, adequate.desiredSleepMinutes, 0.0)
        assertTrue(adequate.sleepDebtApplied)
        assertEquals(0.0, conservative.extensionMinutes, 0.0)
        assertFalse(conservative.sleepDebtApplied)
    }

    fun testCompletedNapLowersCurrentSleepPressureWithoutInventingMissingDiary() {
        val now = at(2026, 11, 3, 20, 0)
        val withoutNap = plan(now, at(2026, 11, 4, 7, 0))
        val withNap = MeasurementRecommendationPolicy.plan(
            now = now,
            wake = at(2026, 11, 4, 7, 0),
            targetSleepMinutes = 480,
            latencyMinutes = 15.0,
            phaseMinutes = 180.0,
            personalizedMode = false,
            evidenceAdequate = false,
            usualBed = usualBed,
            usualWake = usualWake,
            recentCompleteDiaries = emptyList(),
            completedNap = MeasurementRecommendationPolicy.SleepInterval(
                at(2026, 11, 3, 15, 0), at(2026, 11, 3, 16, 0)
            )
        )

        assertTrue(withNap.napApplied)
        assertTrue(withNap.currentS < withoutNap.currentS)
        assertNull(withNap.observedSleepMinutes)
        assertNull(withNap.sleepDebtMinutes)
    }

    fun testEngineMatchesPythonV7CompleteDiaryPersonalizedPhaseDebtAndNapGolden() {
        val now = at(2026, 11, 3, 20, 0)
        val records = (1..7).map { daysAgo ->
            val wake = at(2026, 11, 3, 8, 0).minusDays(daysAgo.toLong())
            diary(
                id = "complete:$daysAgo", wake = wake, latency = 0,
                awake = if (daysAgo <= 4) 0 else 60
            )
        }
        val context = DailyContext(
            nightId = "2026-11-03", kind = "none", firstStart = null,
            preparationMinutes = 0,
            napStart = at(2026, 11, 3, 15, 0).toInstant().toString(),
            napEnd = at(2026, 11, 3, 16, 0).toInstant().toString(),
            caffeineAt = null, caffeineMg = null, observedAt = now.toInstant().toString()
        )

        val output = UserModelEngine(instrumentation.targetContext).compute(
            goldenProfile(context), records, now.toInstant()
        )
        val personalized = output.outcomes.getJSONObject("personalization")
        val measurement = output.outcomes.getJSONObject("measurement_recommendation")

        // Golden generated directly by web-app/engine.py v7 recommend() for these inputs.
        assertEquals("2026-11-03T22:44:18.107+09:00", output.recommendation.bedtime)
        assertEquals("2026-11-04T07:00+09:00", output.recommendation.wakeTime)
        assertEquals(495, output.recommendation.durationMinutes)
        assertEquals(197.20713935730663, personalized.getDouble("phase_min"), 1e-12)
        assertEquals(10.698215160673342, personalized.getDouble("latency_min"), 1e-12)
        assertEquals(460.0, measurement.getDouble("observed_sleep_min"), 0.0)
        assertEquals(20.0, measurement.getDouble("sleep_debt_min"), 0.0)
        assertEquals(0.458851120844193, measurement.getDouble("current_s"), 1e-12)
        assertTrue(measurement.getBoolean("completed_nap_applied"))
    }

    fun testEngineMatchesPythonV7StaleIncompleteDiaryGoldenWithoutInventedDebt() {
        val now = at(2026, 11, 3, 20, 0)
        val records = (15 until 45).map { daysAgo ->
            diary(
                id = "stale:$daysAgo",
                wake = at(2026, 11, 3, 8, 0).minusDays(daysAgo.toLong()),
                latency = 10,
                awake = null
            )
        }

        val output = UserModelEngine(instrumentation.targetContext).compute(
            goldenProfile(), records, now.toInstant()
        )
        val personalized = output.outcomes.getJSONObject("personalization")
        val measurement = output.outcomes.getJSONObject("measurement_recommendation")

        // Separate Python v7 golden: stale phase/target remain declared, latency still uses its own evidence mass.
        assertEquals("2026-11-03T22:46:44.050+09:00", output.recommendation.bedtime)
        assertEquals("2026-11-04T07:00+09:00", output.recommendation.wakeTime)
        assertEquals(493, output.recommendation.durationMinutes)
        assertEquals("conservative", personalized.getJSONObject("evidence_quality").getString("status"))
        assertEquals(180.0, personalized.getDouble("phase_min"), 0.0)
        assertEquals(13.265832467538136, personalized.getDouble("latency_min"), 1e-12)
        assertTrue(measurement.isNull("observed_sleep_min"))
        assertTrue(measurement.isNull("sleep_debt_min"))
        assertEquals(0.5558851198190611, measurement.getDouble("current_s"), 1e-12)
        assertFalse(measurement.getBoolean("completed_nap_applied"))
    }

    private fun plan(
        now: ZonedDateTime,
        wake: ZonedDateTime,
        adequate: Boolean = false,
        diaries: List<MeasurementRecommendationPolicy.CompleteDiary> = emptyList()
    ) = MeasurementRecommendationPolicy.plan(
        now = now,
        wake = wake,
        targetSleepMinutes = 480,
        latencyMinutes = 15.0,
        phaseMinutes = 180.0,
        personalizedMode = diaries.isNotEmpty(),
        evidenceAdequate = adequate,
        usualBed = usualBed,
        usualWake = usualWake,
        recentCompleteDiaries = diaries,
        completedNap = null
    )

    private fun goldenProfile(context: DailyContext? = null) = UserProfile(
        ageBand = "20-29", sex = "female", activityMinutes = 60,
        usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
        createdAt = "2026-08-31T15:00:00Z", dailyContext = context,
        sleepLatencyMinutes = 15.0, averageSteps = 6_000.0, exerciseMinutes = 30.0
    )

    private fun diary(
        id: String,
        wake: ZonedDateTime,
        latency: Int?,
        awake: Int?
    ) = SleepRecord(
        id = id,
        day = wake.toLocalDate().toString(),
        bedtime = wake.minusHours(8).toInstant().toString(),
        wakeTime = wake.toInstant().toString(),
        durationMinutes = 480,
        rested = 5,
        fatigue = 2,
        source = "diary",
        modifiedAt = wake.plusMinutes(1).toInstant().toString(),
        sleepLatencyMinutes = latency,
        awakeMinutes = awake
    )

    private fun at(year: Int, month: Int, day: Int, hour: Int, minute: Int): ZonedDateTime =
        ZonedDateTime.of(year, month, day, hour, minute, 0, 0, seoul)
}
