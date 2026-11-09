package app.sleepwell.user

import android.test.InstrumentationTestCase
import org.json.JSONObject
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.pow

@Suppress("DEPRECATION")
class MeasurementPersonalizationRegressionTest : InstrumentationTestCase() {
    fun testIncompleteDiaryDoesNotFabricateSleepDuration() {
        val incomplete = evidence(latency = 10, awake = null)
        val complete = evidence(latency = 10, awake = 20)

        assertNull(incomplete.sleepMinutes)
        assertEquals(450.0, complete.sleepMinutes ?: -1.0, 0.0)
        val enoughButIncomplete = policy((0 until 7).map { evidence(age = it.toDouble(), awake = null) })
        assertFalse(enoughButIncomplete.conservative)
        assertEquals(0, enoughButIncomplete.goodNightCount)
        assertEquals(0.0, enoughButIncomplete.goodNightEffectiveRecords, 0.0)
        assertEquals(480, enoughButIncomplete.targetSleepMinutes)
    }

    fun testDeclaredAdultTargetIsTheBoundedBaseline() {
        val high = MeasurementPersonalization.compute(
            declaredTargetMinutes = 600,
            usualBedMinutes = 23 * 60.0,
            usualWakeMinutes = 7 * 60.0,
            baselineLatencyMinutes = 15.0,
            baselineSteps = 0.0,
            baselineExerciseMinutes = 0.0,
            evidence = emptyList()
        )
        assertEquals(540, high.targetSleepMinutes)
        assertEquals("profile_no_history", high.phaseStatus)
    }

    fun testCanonicalRecentGoldenActivatesOnlyWithAdequateEvidence() {
        val sparse = policy((0 until 4).map { evidence(age = it.toDouble(), sleep = 450.0) })
        assertTrue(sparse.conservative)
        assertEquals("profile_insufficient_evidence", sparse.phaseStatus)
        assertEquals(480, sparse.targetSleepMinutes)

        val adequate = policy((0 until 7).map { evidence(age = it.toDouble(), sleep = 450.0) })
        assertFalse(adequate.conservative)
        assertEquals("personalized", adequate.phaseStatus)
        assertEquals(6.063433321253326, adequate.effectiveRecords, 1e-12)
        assertEquals(0.3022131468810122, adequate.sleepTargetWeight, 1e-12)
        assertEquals(471, adequate.targetSleepMinutes)
    }

    fun testStaleAndIrregularHistoriesKeepDeclaredTargetAndPhase() {
        val stale = policy((15 until 45).map { evidence(age = it.toDouble()) })
        assertTrue(stale.effectiveRecords > 5.0)
        assertTrue(stale.conservative)
        assertEquals("profile_stale_history", stale.phaseStatus)
        assertEquals(480, stale.targetSleepMinutes)
        assertEquals(180.0, stale.phaseMinutes, 1e-9)

        val irregular = policy((0 until 8).map { index ->
            evidence(age = 0.0, midpoint = if (index % 2 == 0) 0.0 else 720.0)
        })
        assertTrue(irregular.conservative)
        assertEquals("profile_undefined_resultant", irregular.phaseStatus)
        assertTrue((irregular.phaseResultantLength ?: 1.0) < 1e-12)
        assertEquals(180.0, irregular.phaseMinutes, 1e-9)
    }

    fun testMissingFieldsHaveIndependentEffectiveMass() {
        val rows = (0 until 7).map { index ->
            evidence(
                age = index.toDouble(),
                latency = if (index == 0 || index == 3) 10 else null,
                awake = if (index == 0 || index == 3) 20 else null,
                steps = if (index == 0) 10_000 else null,
                exercise = if (index == 6) 60.0 else null
            )
        }
        val result = policy(rows)

        assertEquals(1.8619728212469777, result.latencyEffectiveRecords, 1e-12)
        assertEquals(1.0, result.activityEffectiveRecords, 1e-12)
        assertEquals(2.0.pow(-6.0 / 14.0), result.exerciseEffectiveRecords, 1e-12)
        assertTrue(result.latencyEffectiveRecords < result.effectiveRecords)
        assertTrue(result.activityEffectiveRecords < result.effectiveRecords)
    }

    fun testMeasurementFieldsRoundTripWithoutTurningMissingIntoZero() {
        val record = SleepRecord(
            id = "diary:1", day = "2026-11-03",
            bedtime = "2026-11-02T14:00:00Z", wakeTime = "2026-11-02T22:00:00Z",
            durationMinutes = 480, rested = 5, fatigue = 2, source = "diary",
            modifiedAt = "2026-11-02T22:01:00Z", sleepLatencyMinutes = 10,
            awakeMinutes = null, steps = 9_000, exerciseMinutes = null
        )

        val json = record.toJson()
        val restored = recordFromJson(JSONObject(json.toString()))
        assertEquals(10, restored.sleepLatencyMinutes)
        assertNull(restored.awakeMinutes)
        assertEquals(9_000, restored.steps)
        assertNull(restored.exerciseMinutes)
    }

    fun testPostCutoffDiaryUpdatesLivePersonalizationButNotFixedResearchForecast() {
        val seoul = ZoneId.of("Asia/Seoul")
        val now = ZonedDateTime.of(2026, 11, 3, 20, 1, 0, 0, seoul)
        val wake = now.withHour(19).withMinute(50)
        val bed = wake.minusHours(8)
        val record = SleepRecord(
            id = "diary:late", day = wake.toLocalDate().toString(),
            bedtime = bed.toInstant().toString(), wakeTime = wake.toInstant().toString(),
            durationMinutes = 480, rested = 5, fatigue = 2, source = "diary",
            modifiedAt = now.toInstant().toString(), sleepLatencyMinutes = 0, awakeMinutes = 0
        )
        val profile = UserProfile(
            ageBand = "20-29", sex = "female", activityMinutes = 60,
            usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z"
        )
        val output = UserModelEngine(instrumentation.targetContext).compute(
            profile = profile, records = listOf(record), now = now.toInstant()
        )

        val coverage = output.outcomes.getJSONObject("input_coverage")
        assertEquals(1, coverage.getInt("live_personalization_diary_records"))
        assertEquals(0, coverage.getInt("research_forecast_diary_records"))
        assertEquals(1, output.outcomes.getJSONObject("personalization").getInt("eligible_primary_nights"))
        assertEquals(
            0,
            output.outcomes.getJSONObject("research_personalization")
                .getJSONObject("cmi").getInt("valid_days")
        )
    }

    fun testObservationExactlyAtCutoffIsLiveButExcludedFromStrictResearchSnapshot() {
        val seoul = ZoneId.of("Asia/Seoul")
        val cutoff = ZonedDateTime.of(2026, 11, 3, 20, 0, 0, 0, seoul)
        val now = cutoff.plusMinutes(1)
        val wake = cutoff.minusMinutes(10)
        val record = SleepRecord(
            id = "diary:at-cutoff", day = wake.toLocalDate().toString(),
            bedtime = wake.minusHours(8).toInstant().toString(), wakeTime = wake.toInstant().toString(),
            durationMinutes = 480, rested = 5, fatigue = 2, source = "diary",
            modifiedAt = cutoff.toInstant().toString(), sleepLatencyMinutes = 0, awakeMinutes = 0
        )
        val profile = UserProfile(
            ageBand = "20-29", sex = "female", activityMinutes = 60,
            usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z"
        )

        val output = UserModelEngine(instrumentation.targetContext).compute(
            profile = profile, records = listOf(record), now = now.toInstant()
        )

        val coverage = output.outcomes.getJSONObject("input_coverage")
        assertEquals(1, coverage.getInt("live_personalization_diary_records"))
        assertEquals(0, coverage.getInt("research_forecast_diary_records"))
        assertEquals(
            0,
            output.outcomes.getJSONObject("research_personalization")
                .getJSONObject("cmi").getInt("valid_days")
        )
    }

    fun testHistoricalEvaluationIsExplicitlyNonProspectiveWithoutChangingClock() {
        val seoul = ZoneId.of("Asia/Seoul")
        val now = ZonedDateTime.of(2026, 10, 24, 20, 0, 0, 0, seoul)
        val profile = UserProfile(
            ageBand = "20-29", sex = "female", activityMinutes = 60,
            usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z"
        )

        val output = UserModelEngine(instrumentation.targetContext).compute(
            profile = profile, records = emptyList(), now = now.toInstant(), historicalEvaluation = true
        )

        assertEquals("2026-10-24T11:00:00Z", output.outcomes.getString("generated_at"))
        assertEquals(
            "historical_reconstruction_not_prospective",
            output.outcomes.getString("forecast_status")
        )
        assertTrue(output.forecast.note.contains("전향 예측 또는 보정 표본으로 간주하지 않습니다"))
    }

    fun testAdaptiveResidualSidecarRequiresExactRuntimePolicyAndMetricsIdentity() {
        val seoul = ZoneId.of("Asia/Seoul")
        val now = ZonedDateTime.of(2026, 11, 3, 20, 0, 0, 0, seoul)
        val profile = UserProfile(
            ageBand = "20-29", sex = "female", activityMinutes = 60,
            usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
            createdAt = "2026-10-01T00:00:00Z"
        )
        val engine = UserModelEngine(instrumentation.targetContext)
        val identity = engine.compute(profile, emptyList(), now.toInstant()).outcomes
        fun observed(version: String, onsetResidual: Double) = JSONObject()
            .put("night_id", "2026-11-01")
            .put("source_model_version", version)
            .put("source_personalization_policy", identity.getString("runtime_personalization_policy"))
            .put("source_research_metrics_scope", identity.getString("research_metrics_scope"))
            .put("source_research_artifacts", identity.getJSONObject("research_artifacts"))
            .put("available_at", "2026-11-02T11:00:00Z")
            .put("outcome_at", "2026-11-02T10:00:00Z")
            .put("residuals", JSONObject().put("hybrid_cmi", JSONObject()
                .put("onset_residual_min", onsetResidual)
                .put("wake_residual_min", 20.0)))
        val exactRuntime = observed(identity.getString("model_version"), 10.0)
        val legacyArtifact = observed(identity.getString("artifact_model_version"), 100.0)

        val output = engine.compute(
            profile = profile,
            records = emptyList(),
            now = now.toInstant(),
            prospectiveOutcomes = listOf(exactRuntime, legacyArtifact)
        )
        val adjustment = output.outcomes.getJSONObject("adaptive_research_predictions")
            .getJSONObject("hybrid_cmi").getJSONObject("adjustment")

        assertEquals(1, adjustment.getInt("eligible_count"))
        assertEquals(10.0, adjustment.getDouble("raw_onset_residual_mean_min"), 0.0)
    }

    private fun policy(rows: List<MeasurementPersonalization.DiaryEvidence>) =
        MeasurementPersonalization.compute(
            declaredTargetMinutes = 480,
            usualBedMinutes = 23 * 60.0,
            usualWakeMinutes = 7 * 60.0,
            baselineLatencyMinutes = 15.0,
            baselineSteps = 6_000.0,
            baselineExerciseMinutes = 30.0,
            evidence = rows
        )

    private fun evidence(
        age: Double = 0.0,
        midpoint: Double = 180.0,
        sleep: Double = 450.0,
        latency: Int? = 10,
        awake: Int? = 20,
        steps: Int? = null,
        exercise: Double? = null
    ): MeasurementPersonalization.DiaryEvidence {
        val timeInBed = if (latency != null && awake != null) sleep + latency + awake else 480.0
        return MeasurementPersonalization.DiaryEvidence(
            midpointMinutes = midpoint,
            wakeAgeDays = age,
            observationAgeDays = age,
            timeInBedMinutes = timeInBed,
            latencyMinutes = latency,
            awakeMinutes = awake,
            rested = 5,
            fatigue = 2,
            steps = steps,
            exerciseMinutes = exercise
        )
    }
}
