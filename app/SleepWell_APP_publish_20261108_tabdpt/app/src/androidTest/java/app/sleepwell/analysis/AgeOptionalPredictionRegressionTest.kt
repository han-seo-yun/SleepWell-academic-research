package app.sleepwell.analysis

import android.test.InstrumentationTestCase
import app.sleepwell.user.EngineOutput
import app.sleepwell.user.SleepInputPolicy
import app.sleepwell.user.SleepRecord
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import org.json.JSONObject
import java.time.ZoneId
import java.time.ZonedDateTime

/** Age is retained only as legacy metadata and must not alter predictions or personalization. */
@Suppress("DEPRECATION")
class AgeOptionalPredictionRegressionTest : InstrumentationTestCase() {
    private val zone = ZoneId.of("Asia/Seoul")
    private val now = ZonedDateTime.of(2026, 11, 10, 20, 1, 0, 0, zone)
    private val ages = listOf("18-29", "30-44", "45-59", "60-80")

    fun testEmptyAgeAndEveryLegacyAgeProduceSameColdStartPredictionAndPersonalization() {
        assertAgeInvariant(emptyList(), SleepInputPolicy.DIARY_ONLY, expectHistory = false)
    }

    fun testEmptyAgeAndEveryLegacyAgeProduceSameData33AndPersonalizedPredictionWithMixedHistory() {
        val records = (1..21).map { daysAgo ->
            val wake = now.minusDays(daysAgo.toLong()).withHour(7).withMinute((daysAgo * 3) % 45)
            val duration = 420 + (daysAgo % 5) * 15
            val diary = daysAgo % 2 == 0
            SleepRecord(
                id = if (diary) "diary:$daysAgo" else "watch:$daysAgo",
                day = wake.toLocalDate().toString(),
                bedtime = wake.minusMinutes(duration.toLong()).toInstant().toString(),
                wakeTime = wake.toInstant().toString(),
                durationMinutes = duration,
                rested = if (diary) 4 else null,
                fatigue = if (diary) 2 else null,
                source = if (diary) "diary" else "samsung_health",
                modifiedAt = wake.plusMinutes(30).toInstant().toString(),
                sleepLatencyMinutes = if (diary) 15 else null,
                awakeMinutes = if (diary) 15 else null
            )
        }
        assertAgeInvariant(records, SleepInputPolicy.DIARY_AND_WEARABLE_V1, expectHistory = true)
    }

    private fun assertAgeInvariant(
        records: List<SleepRecord>,
        inputPolicy: SleepInputPolicy,
        expectHistory: Boolean
    ) {
        val engine = UserModelEngine(instrumentation.targetContext)
        val baseline = engine.compute(profile(""), records, now.toInstant(), inputPolicy = inputPolicy)
        if (expectHistory) {
            val coverage = baseline.outcomes.getJSONObject("input_coverage")
            assertTrue(coverage.getInt("forecast_model_input_records") > 0)
            assertTrue(coverage.getInt("live_personalization_input_records") > 0)
            val snapshot = baseline.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
            assertTrue(snapshot.getDouble("recent_count14") > 0.0)
            assertTrue(baseline.outcomes.getJSONObject("research_personalization")
                .getJSONObject("cmi").getInt("valid_days") > 0)
            assertTrue(baseline.outcomes.getJSONObject("personalization").getInt("eligible_input_nights") > 0)
        }

        var observedDescriptivePriorDifference = false
        ages.forEach { age ->
            val variant = engine.compute(profile(age), records, now.toInstant(), inputPolicy = inputPolicy)
            assertEquals("forecast must ignore legacy age $age", baseline.forecast, variant.forecast)
            assertEquals("recommendation must ignore legacy age $age", baseline.recommendation, variant.recommendation)
            assertEquals(
                "DATA33, model predictions, and personalization must ignore legacy age $age",
                comparableOutcomes(baseline), comparableOutcomes(variant)
            )
            val prior = variant.outcomes.getJSONObject("personalization").getJSONObject("population_prior")
            assertFalse(prior.getBoolean("used_for_target"))
            val baselinePrior = baseline.outcomes.getJSONObject("personalization").getJSONObject("population_prior")
            observedDescriptivePriorDifference = observedDescriptivePriorDifference ||
                prior.getString("level") != baselinePrior.getString("level") ||
                prior.getDouble("mean_sleep_min") != baselinePrior.getDouble("mean_sleep_min")
        }
        assertTrue("The removed age field may change descriptive metadata only, proving the test is sensitive.",
            observedDescriptivePriorDifference)
    }

    private fun profile(age: String) = UserProfile(
        ageBand = age,
        sex = "any",
        activityMinutes = 60,
        usualBed = "23:30",
        usualWake = "07:30",
        desiredSleepMinutes = 480,
        createdAt = "2026-09-01T00:00:00Z"
    )

    private fun comparableOutcomes(output: EngineOutput): String {
        val copy = JSONObject(output.outcomes.toString())
        copy.getJSONObject("profile_contract").remove("age_band")
        copy.getJSONObject("personalization").remove("population_prior")
        return copy.toString()
    }
}
