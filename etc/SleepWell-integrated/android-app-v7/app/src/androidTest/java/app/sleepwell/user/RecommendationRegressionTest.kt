package app.sleepwell.user

import android.test.InstrumentationTestCase
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId

@Suppress("DEPRECATION")
class RecommendationRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun testStaleExplicitScheduleFallsBackInsteadOfReturningZeroMinutes() {
        val now = Instant.parse("2026-11-03T03:00:00Z") // 12:00 KST preview
        val output = compute(
            now = now,
            firstStart = "2026-10-24T22:30:00Z",
            preparationMinutes = 0
        )

        assertTrue(output.recommendation.durationMinutes > 0)
        assertOrdered(output)
        assertTrue(output.recommendation.wakeTime.startsWith("2026-11-04T07:30"))
        assertTrue(
            output.outcomes.getJSONObject("daily_context_policy")
                .isNull("schedule_wake_constraint")
        )
    }

    fun testMidnightScheduleUsesPostCutoffPreparationDeadline() {
        val now = Instant.parse("2026-11-03T11:00:00Z") // 20:00 KST cutoff
        val output = compute(
            now = now,
            firstStart = "2026-11-03T15:30:00Z", // 00:30 KST next day
            preparationMinutes = 90
        )

        assertEquals(180, output.recommendation.durationMinutes)
        assertFalse(output.recommendation.planFeasible)
        assertTrue(output.recommendation.wakeTime.startsWith("2026-11-03T23:00"))
        assertOrdered(output)
    }

    fun testLateEvaluationRejectsElapsedMidnightConstraint() {
        val now = Instant.parse("2026-11-03T14:30:00Z") // 23:30 KST
        val output = compute(
            now = now,
            firstStart = "2026-11-03T15:30:00Z", // prep deadline was 23:00 KST
            preparationMinutes = 90
        )

        assertTrue(output.recommendation.durationMinutes > 0)
        assertOrdered(output)
        assertTrue(output.recommendation.wakeTime.startsWith("2026-11-04T07:30"))
        assertTrue(
            output.outcomes.getJSONObject("daily_context_policy")
                .isNull("schedule_wake_constraint")
        )
    }

    private fun compute(
        now: Instant,
        firstStart: String,
        preparationMinutes: Int
    ): EngineOutput {
        val observedAt = now.minusSeconds(60).toString()
        val context = DailyContext(
            nightId = now.atZone(seoul).toLocalDate().toString(),
            kind = "first",
            firstStart = firstStart,
            preparationMinutes = preparationMinutes,
            napStart = null,
            napEnd = null,
            caffeineAt = null,
            caffeineMg = null,
            observedAt = observedAt
        )
        val profile = UserProfile(
            ageBand = "20-29",
            sex = "female",
            activityMinutes = 60,
            usualBed = "23:30",
            usualWake = "07:30",
            desiredSleepMinutes = 480,
            createdAt = "2026-01-01T00:00:00Z",
            dailyContext = context
        )
        return UserModelEngine(instrumentation.targetContext).compute(profile, emptyList(), now)
    }

    private fun assertOrdered(output: EngineOutput) {
        val bedtime = OffsetDateTime.parse(output.recommendation.bedtime)
        val wake = OffsetDateTime.parse(output.recommendation.wakeTime)
        assertTrue(wake.isAfter(bedtime))
        assertEquals(
            output.recommendation.durationMinutes.toLong(),
            Duration.between(bedtime, wake).toMinutes()
        )
    }
}
