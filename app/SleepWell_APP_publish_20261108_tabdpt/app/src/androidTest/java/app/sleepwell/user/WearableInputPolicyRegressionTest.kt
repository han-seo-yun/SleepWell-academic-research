package app.sleepwell.user

import android.test.InstrumentationTestCase
import org.json.JSONObject
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class WearableInputPolicyRegressionTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")
    private val now = ZonedDateTime.of(2026, 11, 3, 20, 1, 0, 0, seoul)
    private val profile = UserProfile(
        ageBand = "20-29", sex = "female", activityMinutes = 60,
        usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
        createdAt = "2026-09-01T00:00:00Z", averageSteps = 6_000.0
    )

    fun testWatchOnlyFeedsData33AndPhaseOnlyUnderExplicitServicePolicy() {
        val watches = (1..7).map { age ->
            wearable("watch:$age", now.minusDays(age.toLong()).withHour(7).withMinute(0))
        } + wearable("unknown", now.minusDays(8).withHour(7), source = "mystery_watch")
        val engine = UserModelEngine(instrumentation.targetContext)

        val frozen = engine.compute(profile, watches, now.toInstant())
        val service = engine.compute(
            profile, watches, now.toInstant(), inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )

        assertEquals(0, frozen.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
        assertEquals(0, frozen.outcomes.getJSONObject("research_personalization").getJSONObject("cmi").getInt("valid_days"))
        assertEquals(7, service.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
        assertEquals(7, service.outcomes.getJSONObject("input_coverage").getInt("live_personalization_input_records"))
        assertEquals(7, service.outcomes.getJSONObject("research_personalization").getJSONObject("cmi").getInt("valid_days"))
        assertEquals(7.0, service.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
            .getDouble("recent_count14"), 0.0)
        assertEquals(0, service.outcomes.getJSONObject("personalization").getInt("eligible_primary_nights"))
        assertEquals(7, service.outcomes.getJSONObject("personalization").getInt("eligible_input_nights"))
        assertEquals("personalized", service.outcomes.getJSONObject("personalization").getString("phase_status"))
        assertEquals(480.0, service.outcomes.getJSONObject("recommendation_feasibility")
            .getDouble("personalized_sleep_target_min"), 0.0)
        assertEquals(0, service.outcomes.getJSONObject("personalization").getInt("good_night_count"))
    }

    fun testDiaryWinsSameWakeDateWithoutRelabelingWearable() {
        val wake = now.minusDays(1).withHour(7).withMinute(0)
        val watch = wearable("watch", wake, bedHour = 21).copy(
            modifiedAt = wake.plusHours(10).toInstant().toString()
        )
        val diary = SleepRecord(
            id = "diary", day = wake.toLocalDate().toString(),
            bedtime = wake.minusHours(8).toInstant().toString(), wakeTime = wake.toInstant().toString(),
            durationMinutes = 480, rested = null, fatigue = null, source = "diary",
            modifiedAt = wake.plusHours(2).toInstant().toString()
        )

        val output = UserModelEngine(instrumentation.targetContext).compute(
            profile, listOf(watch, diary), now.toInstant(),
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )
        val snapshot = output.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")

        assertEquals(1, output.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
        assertEquals(1, output.outcomes.getJSONObject("input_coverage").getInt("research_forecast_diary_records"))
        assertEquals(180.0, snapshot.getDouble("previous_bed"), 1e-9)
        assertEquals("diary_and_wearable_v1", output.outcomes.getString("sleep_input_policy"))
    }

    fun testWearableSubjectiveValuesWithoutCausalQuestionnaireCannotMoveTarget() {
        val records = (1..7).map { age ->
            wearable("untrusted:$age", now.minusDays(age.toLong()).withHour(7)).copy(
                sleepLatencyMinutes = 0,
                awakeMinutes = 60,
                rested = 5,
                fatigue = 1,
                steps = 30_000,
                subjectiveSource = "questionnaire",
                subjectiveObservedAt = now.plusDays(1).toInstant().toString()
            )
        }

        val output = UserModelEngine(instrumentation.targetContext).compute(
            profile, records, now.toInstant(), inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )
        val personalization = output.outcomes.getJSONObject("personalization")

        assertEquals(480.0, output.outcomes.getJSONObject("recommendation_feasibility")
            .getDouble("personalized_sleep_target_min"), 0.0)
        assertEquals(0, personalization.getInt("good_night_count"))
        assertEquals(6_000.0, personalization.getDouble("average_steps"), 0.0)
        assertTrue(output.outcomes.getJSONObject("measurement_recommendation").isNull("observed_sleep_min"))
    }

    fun testFutureTimingObservationIsExcludedAndServiceIdentityIsDistinct() {
        val futureObserved = wearable("future", now.minusDays(1).withHour(7)).copy(
            modifiedAt = now.plusMinutes(1).toInstant().toString()
        )

        val output = UserModelEngine(instrumentation.targetContext).compute(
            profile, listOf(futureObserved), now.toInstant(),
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )

        assertEquals(0, output.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
        assertEquals(0, output.outcomes.getJSONObject("input_coverage").getInt("live_personalization_input_records"))
        assertEquals(PortableFusionModel.WEARABLE_RUNTIME_MODEL_VERSION, output.outcomes.getString("model_version"))
        assertEquals(PortableFusionModel.WEARABLE_RUNTIME_METRICS_SCOPE, output.outcomes.getString("research_metrics_scope"))
        assertFalse(output.outcomes.getJSONObject("service_input_policy").getBoolean("frozen_research_metrics_apply"))
    }

    fun testOldSleepImportedAfterCutoffFeedsLiveServiceButNotHistoricalSnapshot() {
        val lateNow = ZonedDateTime.of(2026, 11, 3, 21, 0, 0, 0, seoul)
        val imported = wearable("late-import", lateNow.minusDays(1).withHour(7)).copy(
            modifiedAt = lateNow.toInstant().toString()
        )
        val engine = UserModelEngine(instrumentation.targetContext)

        val live = engine.compute(
            profile, listOf(imported), lateNow.toInstant(),
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )
        val historical = engine.compute(
            profile, listOf(imported), lateNow.toInstant(), historicalEvaluation = true,
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )

        assertEquals(1, live.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
        assertEquals(1, live.outcomes.getJSONObject("research_personalization").getJSONObject("cmi").getInt("valid_days"))
        assertEquals(0, historical.outcomes.getJSONObject("input_coverage").getInt("forecast_model_input_records"))
        assertEquals(0, historical.outcomes.getJSONObject("research_personalization").getJSONObject("cmi").getInt("valid_days"))
        assertTrue(live.outcomes.getJSONObject("service_input_policy").getBoolean("observation_boundary_inclusive"))
        assertFalse(historical.outcomes.getJSONObject("service_input_policy").getBoolean("observation_boundary_inclusive"))
        assertEquals(lateNow.toInstant().toString(), live.outcomes.getJSONObject("service_input_policy")
            .getString("observation_boundary"))
    }

    fun testLateQuestionnaireAffectsLivePersonalizationOnly() {
        val lateNow = ZonedDateTime.of(2026, 11, 3, 21, 0, 0, 0, seoul)
        val wake = lateNow.minusDays(1).withHour(7)
        val joined = wearable("late-survey", wake).copy(
            modifiedAt = wake.plusMinutes(5).toInstant().toString(),
            sleepLatencyMinutes = 0,
            awakeMinutes = 30,
            rested = 5,
            fatigue = 1,
            subjectiveSource = "questionnaire",
            subjectiveObservedAt = lateNow.toInstant().toString()
        )
        val engine = UserModelEngine(instrumentation.targetContext)

        val live = engine.compute(
            profile, listOf(joined), lateNow.toInstant(),
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )
        val historical = engine.compute(
            profile, listOf(joined), lateNow.toInstant(), historicalEvaluation = true,
            inputPolicy = SleepInputPolicy.DIARY_AND_WEARABLE_V1
        )

        assertEquals(1, live.outcomes.getJSONObject("personalization").getInt("good_night_count"))
        assertEquals(0, historical.outcomes.getJSONObject("personalization").getInt("good_night_count"))
        assertTrue(live.outcomes.getJSONObject("personalization").getDouble("latency_min") < 15.0)
        assertEquals(15.0, historical.outcomes.getJSONObject("personalization").getDouble("latency_min"), 0.0)
        assertFalse(live.outcomes.getJSONObject("measurement_recommendation").isNull("observed_sleep_min"))
        assertTrue(historical.outcomes.getJSONObject("measurement_recommendation").isNull("observed_sleep_min"))
    }

    fun testQuestionnaireProvenanceRoundTripsWithoutChangingWearableSource() {
        val record = wearable("joined", now.minusDays(1).withHour(7)).copy(
            rested = 4,
            fatigue = 2,
            subjectiveSource = "questionnaire",
            subjectiveObservedAt = now.minusHours(1).toInstant().toString()
        )

        val restored = recordFromJson(JSONObject(record.toJson().toString()))

        assertEquals("samsung_health", restored.source)
        assertEquals("questionnaire", restored.subjectiveSource)
        assertEquals(record.subjectiveObservedAt, restored.subjectiveObservedAt)
    }

    private fun wearable(
        id: String,
        wake: ZonedDateTime,
        bedHour: Int = 23,
        source: String = "samsung_health"
    ): SleepRecord {
        val bed = wake.minusDays(1).withHour(bedHour).withMinute(0)
        return SleepRecord(
            id = id, day = wake.toLocalDate().toString(),
            bedtime = bed.toInstant().toString(), wakeTime = wake.toInstant().toString(),
            durationMinutes = java.time.Duration.between(bed, wake).toMinutes().toInt(),
            rested = null, fatigue = null, source = source,
            modifiedAt = wake.plusMinutes(5).toInstant().toString()
        )
    }
}
