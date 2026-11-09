package app.sleepwell.user

import android.test.InstrumentationTestCase
import app.sleepwell.analysis.NextNightModel
import app.sleepwell.health.AnalysisResult
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class TabDptIntegrationTest : InstrumentationTestCase() {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun testInjectedPredictionChangesForecastAndBoundedRecommendation() {
        val now = at(2026, 11, 3, 20, 0)
        val profile = profile(usualBed = "22:00", sleepLatency = 60.0)
        val history = (1..7).map { daysAgo ->
            val wake = at(2026, 11, 3, 4, 0).minusDays(daysAgo.toLong())
            diary("phase-$daysAgo", wake.minusHours(8), wake, wake.plusMinutes(1).toInstant(), latency = 60)
        }
        val early = compute(profile, success(onset = 120.0, wake = 690.0), now, history)
        val late = compute(profile, success(onset = 240.0, wake = 810.0), now, history)

        assertEquals(120.0, early.forecast.ensembleBedOffset ?: -1.0, 0.0)
        assertEquals(240.0, late.forecast.ensembleBedOffset ?: -1.0, 0.0)
        assertFalse("A materially different DATA preference must be able to change a feasible plan",
            early.recommendation.bedtime == late.recommendation.bedtime)
        listOf(early, late).forEach { output ->
            val feasibility = output.outcomes.getJSONObject("recommendation_feasibility")
            val runtime = output.outcomes.getJSONObject("tabdpt_runtime")
            assertEquals("available_applied", runtime.getString("status"))
            assertEquals(TABDPT_MODEL_VERSION, runtime.getString("model_version"))
            assertTrue(output.recommendation.planFeasible)
            assertEquals("2026-11-04T07:00+09:00", output.recommendation.wakeTime)
            val actualOpportunityMs = Duration.between(
                OffsetDateTime.parse(output.recommendation.bedtime),
                OffsetDateTime.parse(output.recommendation.wakeTime)
            ).toMillis()
            val requiredOpportunityMs = 60_000.0 *
                (feasibility.getDouble("target_duration_min") + feasibility.getDouble("latency_allowance_min"))
            assertTrue(actualOpportunityMs + 1.0 >= requiredOpportunityMs)
            assertTrue(feasibility.getDouble("shortfall_min") < 1e-9)
        }
    }

    fun testPredictedOnsetIsConvertedToBedtimeByPersonalizedLatency() {
        val now = at(2026, 11, 3, 20, 0)
        val output = compute(profile(sleepLatency = 45.0), success(onset = 180.0, wake = 690.0), now)
        val measurement = output.outcomes.getJSONObject("measurement_recommendation")

        assertEquals(
            "2026-11-03T22:15+09:00",
            measurement.getString("tabdpt_hybrid_preferred_bed")
        )
        val prediction = output.outcomes.getJSONObject("research_predictions")
            .getJSONObject("tabdpt_hybrid_cmi")
        assertEquals("2026-11-03T23:00+09:00", prediction.getString("onset"))
        assertEquals("2026-11-03T22:15+09:00", prediction.getString("bed"))
        assertEquals(180.0, prediction.getDouble("onset_offset_min"), 0.0)
        assertEquals(135.0, prediction.getDouble("bed_offset_min"), 0.0)
        assertEquals(45.0, prediction.getDouble("latency_allowance_min"), 0.0)
        assertTrue(measurement.getBoolean("tabdpt_hybrid_preference_applied"))
        assertFalse(prediction.has("tabdpt_data_cmi"))
        assertFalse(prediction.has("tabdpt_data_mesa"))
    }

    fun testNextNightDisplayUsesBedtimeNotRawOnset() {
        val assets = instrumentation.targetContext.assets
        val oldProfile = NextNightModel.profile
        val oldSurvey = NextNightModel.surveyDays
        val oldDiary = NextNightModel.diaryRecords
        val oldImported = NextNightModel.importedRecords
        val oldWearable = NextNightModel.wearablePersonalizationEnabled
        val oldDemo = NextNightModel.demoMode
        try {
            NextNightModel.profile = NextNightModel.ModelProfile(
                ageBand = "18-29", sex = "female", activityMinutes = 60,
                desiredSleepMinutes = 480, usualBed = "23:00", usualWake = "07:00",
                onboardingCompleted = true
            )
            NextNightModel.surveyDays = emptyList()
            NextNightModel.diaryRecords = emptyList()
            NextNightModel.importedRecords = emptyList()
            NextNightModel.wearablePersonalizationEnabled = false
            NextNightModel.demoMode = false
            NextNightModel.install(
                assetLoader = { path -> assets.open(path).use { it.readBytes() } },
                tabDptProvider = success(onset = 180.0, wake = 690.0),
                zoneId = seoul
            )
            val now = at(2026, 11, 3, 20, 0).toInstant().toEpochMilli()
            val base = AnalysisResult(
                currentS = 0.5, currentC = 0.0, currentPropensity = 0.5,
                sleepRecords = emptyList(), scHistory = emptyList(), futureS = null,
                recommendation = null, modelVersion = "test", calculationTimeMs = now,
                inputPeriodEndMs = now
            )

            val applied = NextNightModel.applyTo(base, null, emptyMap(), nowMs = now)
            val displayed = checkNotNull(applied.recommendation)

            // Default latency is 15 min: raw onset 23:00 must display as bedtime 22:45.
            assertEquals(at(2026, 11, 3, 22, 45).toInstant().toEpochMilli(), displayed.predictedBedTimeMs)
            assertEquals(at(2026, 11, 4, 7, 30).toInstant().toEpochMilli(), displayed.predictedWakeTimeMs)
            assertEquals("tabdpt_hybrid39_personalized_two_process", displayed.modelStatus)
        } finally {
            NextNightModel.profile = oldProfile
            NextNightModel.surveyDays = oldSurvey
            NextNightModel.diaryRecords = oldDiary
            NextNightModel.importedRecords = oldImported
            NextNightModel.wearablePersonalizationEnabled = oldWearable
            NextNightModel.demoMode = oldDemo
            NextNightModel.install(
                assetLoader = { path -> assets.open(path).use { it.readBytes() } },
                zoneId = seoul
            )
        }
    }

    fun testSamePredictionStillRespondsToCompletedNapPhysicalState() {
        val now = at(2026, 11, 3, 20, 0)
        val plain = compute(profile(), success(onset = 180.0, wake = 690.0), now)
        val napContext = DailyContext(
            nightId = "2026-11-03", kind = "none", firstStart = null,
            preparationMinutes = 0,
            napStart = at(2026, 11, 3, 15, 0).toInstant().toString(),
            napEnd = at(2026, 11, 3, 16, 0).toInstant().toString(),
            caffeineAt = null, caffeineMg = null, observedAt = now.toInstant().toString()
        )
        val withNap = compute(profile(context = napContext), success(onset = 180.0, wake = 690.0), now)
        val plainMeasurement = plain.outcomes.getJSONObject("measurement_recommendation")
        val napMeasurement = withNap.outcomes.getJSONObject("measurement_recommendation")

        assertFalse(plainMeasurement.getBoolean("completed_nap_applied"))
        assertTrue(napMeasurement.getBoolean("completed_nap_applied"))
        assertTrue(napMeasurement.getDouble("current_s") < plainMeasurement.getDouble("current_s"))
        assertEquals(plain.forecast.ensembleBedOffset, withNap.forecast.ensembleBedOffset)
        assertEquals("available_applied", withNap.outcomes.getJSONObject("tabdpt_runtime").getString("status"))
    }

    fun testFutureRecordIsExcludedBeforeProviderRequest() {
        val now = at(2026, 11, 3, 20, 0)
        var withoutFuture: JSONObject? = null
        var withFuture: JSONObject? = null
        val providerA = captureProvider { withoutFuture = JSONObject(it.toString()) }
        val providerB = captureProvider { withFuture = JSONObject(it.toString()) }
        compute(profile(), providerA, now, emptyList())
        val future = diary(
            id = "future", bed = at(2026, 11, 4, 23, 0), wake = at(2026, 11, 5, 7, 0),
            modified = at(2026, 11, 5, 7, 1).toInstant()
        )
        compute(profile(), providerB, now, listOf(future))

        assertNotNull(withoutFuture)
        assertEquals(withoutFuture.toString(), withFuture.toString())
    }

    fun testAfterMidnightPredictionIsNotAppliedAcrossNightBoundary() {
        val now = at(2026, 11, 4, 1, 0)
        val output = compute(profile(), success(onset = 180.0, wake = 690.0), now)
        val runtime = output.outcomes.getJSONObject("tabdpt_runtime")
        val measurement = output.outcomes.getJSONObject("measurement_recommendation")

        assertEquals("2026-11-03", output.outcomes.getString("recommendation_night_id"))
        assertEquals("2026-11-04", output.outcomes.getString("research_forecast_night_id"))
        assertEquals("available_night_mismatch_not_applied", runtime.getString("status"))
        assertFalse(runtime.getBoolean("recommendation_night_matches_forecast"))
        assertFalse(measurement.getBoolean("tabdpt_hybrid_preference_applied"))
        assertTrue(measurement.isNull("tabdpt_preferred_bed"))
    }

    fun testPredictionIsNotClaimedAppliedWhenNoFeasibleSleepWindowRemains() {
        val now = at(2026, 11, 3, 23, 30)
        val output = compute(profile(), success(onset = 180.0, wake = 690.0), now)
        val runtime = output.outcomes.getJSONObject("tabdpt_runtime")
        val measurement = output.outcomes.getJSONObject("measurement_recommendation")

        assertFalse(output.recommendation.planFeasible)
        assertEquals("available_infeasible_not_applied", runtime.getString("status"))
        assertFalse(measurement.getBoolean("tabdpt_hybrid_preference_applied"))
        assertTrue(output.recommendation.explanation.contains("일정에는 반영하지 않았습니다"))
    }

    fun testUnavailableServerShowsNoFakeTabDptForecastAndUsesPhysicalPlan() {
        val now = at(2026, 11, 3, 20, 0)
        val unavailable = compute(
            profile(),
            TabDptPredictionProvider { _, _ -> TabDptResult.Unavailable("offline", "test outage") },
            now
        )
        val physical = UserModelEngine(instrumentation.targetContext)
            .compute(profile(), emptyList(), now.toInstant())
        val runtime = unavailable.outcomes.getJSONObject("tabdpt_runtime")

        assertEquals("unavailable_physical_only", runtime.getString("status"))
        assertEquals("offline", runtime.getString("reason_code"))
        assertFalse(runtime.has("model_version"))
        assertNull(unavailable.forecast.cmiBedOffset)
        assertNull(unavailable.forecast.mesaBedOffset)
        assertNull(unavailable.forecast.ensembleBedOffset)
        assertEquals(physical.recommendation.bedtime, unavailable.recommendation.bedtime)
        assertEquals(physical.recommendation.wakeTime, unavailable.recommendation.wakeTime)
        assertFalse(unavailable.outcomes.getJSONObject("measurement_recommendation")
            .getBoolean("tabdpt_hybrid_preference_applied"))
    }

    fun testHistoricalEvaluationNeverCallsLiveProvider() {
        var calls = 0
        val provider = TabDptPredictionProvider { _, _ ->
            calls++
            successPrediction(180.0, 690.0)
        }
        UserModelEngine(instrumentation.targetContext, provider).compute(
            profile(), emptyList(), at(2026, 11, 3, 20, 0).toInstant(), historicalEvaluation = true
        )
        assertEquals(0, calls)
    }

    fun testRequestContainsExactHybrid39AndResponseIsStrictlyValidated() {
        val snapshots = JSONObject()
        listOf("cmi", "mesa").forEach { cohort ->
            val values = JSONObject()
            PortableFusionModel.HYBRID_FEATURE_ORDER.forEachIndexed { index, name ->
                values.put(name, index.toDouble())
            }
            values.put("raw_diary", "must-not-leak")
            values.put("participant_id", "must-not-leak")
            snapshots.put(cohort, values)
        }
        val request = HttpTabDptPredictionClient.buildRequest("request-1", snapshots)
        assertEquals(TABDPT_REQUEST_SCHEMA, request.getString("schema"))
        listOf("cmi", "mesa").forEach { cohort ->
            val values = request.getJSONObject("features").getJSONObject(cohort)
            assertEquals(39, values.length())
            assertFalse(values.has("raw_diary"))
            assertFalse(values.has("participant_id"))
            PortableFusionModel.HYBRID_FEATURE_ORDER.forEach { assertTrue(values.has(it)) }
            PortableFusionModel.PHYSICAL_FEATURES.forEach { assertTrue(values.has(it)) }
        }

        val valid = response("request-1", 180.0, 690.0)
        assertEquals(180.0, HttpTabDptPredictionClient.parseResponse(valid, "request-1").meanOnsetOffset, 0.0)
        expectIllegalArgument { HttpTabDptPredictionClient.parseResponse(response("wrong", 180.0, 690.0), "request-1") }
        expectIllegalArgument {
            val wrongModel = response("request-1", 180.0, 690.0).put("model_version", "unknown")
            HttpTabDptPredictionClient.parseResponse(wrongModel, "request-1")
        }
        expectIllegalArgument {
            val oldContract = response("request-1", 180.0, 690.0)
                .put("feature_contract", PortableFusionModel.FEATURE_CONTRACT)
            HttpTabDptPredictionClient.parseResponse(oldContract, "request-1")
        }
        expectIllegalArgument {
            HttpTabDptPredictionClient.parseResponse(response("request-1", 180.0, 190.0), "request-1")
        }
    }

    fun testEndpointPolicyAllowsLocalHttpAndRejectsPublicHttpOrWrongPath() {
        TabDptSettingsStore.requireValidEndpoint("http://127.0.0.1:8766/v1/predict")
        TabDptSettingsStore.requireValidEndpoint("http://192.168.1.8:8766/v1/predict")
        TabDptSettingsStore.requireValidEndpoint("https://example.test/v1/predict")
        expectIllegalArgument { TabDptSettingsStore.requireValidEndpoint("http://8.8.8.8/v1/predict") }
        expectIllegalArgument { TabDptSettingsStore.requireValidEndpoint("http://127.0.0.1:8766/redirect") }
        expectIllegalArgument { TabDptSettingsStore.requireValidEndpoint("http://127.0.0.1:8766/v1/predict?q=x") }
    }

    private fun compute(
        profile: UserProfile,
        provider: TabDptPredictionProvider,
        now: ZonedDateTime,
        records: List<SleepRecord> = emptyList()
    ): EngineOutput = UserModelEngine(instrumentation.targetContext, provider)
        .compute(profile, records, now.toInstant())

    private fun success(onset: Double, wake: Double) = TabDptPredictionProvider { _, _ ->
        successPrediction(onset, wake)
    }

    private fun captureProvider(capture: (JSONObject) -> Unit) = TabDptPredictionProvider { _, snapshots ->
        capture(snapshots)
        successPrediction(180.0, 690.0)
    }

    private fun successPrediction(onset: Double, wake: Double): TabDptResult.Success =
        TabDptResult.Success(
            TabDptPrediction(onset, wake, onset, wake, TABDPT_MODEL_VERSION,
                JSONObject().put("provider", "instrumentation-fake"))
        )

    private fun response(requestId: String, onset: Double, wake: Double) = JSONObject()
        .put("schema", TABDPT_RESPONSE_SCHEMA)
        .put("request_id", requestId)
        .put("feature_contract", TABDPT_FEATURE_CONTRACT)
        .put("model_version", TABDPT_MODEL_VERSION)
        .put("predictions", JSONObject()
            .put("cmi", JSONObject().put("onset_offset_min", onset).put("wake_offset_min", wake))
            .put("mesa", JSONObject().put("onset_offset_min", onset).put("wake_offset_min", wake)))
        .put("provenance", JSONObject().put("source", "test"))

    private fun profile(
        usualBed: String = "23:00",
        sleepLatency: Double = 15.0,
        context: DailyContext? = null
    ) = UserProfile(
        ageBand = "18-29", sex = "female", activityMinutes = 60,
        usualBed = usualBed, usualWake = "07:00", desiredSleepMinutes = 480,
        createdAt = "2026-10-01T00:00:00Z", dailyContext = context,
        sleepLatencyMinutes = sleepLatency, averageSteps = 6_000.0, exerciseMinutes = 30.0
    )

    private fun diary(
        id: String,
        bed: ZonedDateTime,
        wake: ZonedDateTime,
        modified: Instant,
        latency: Int = 15
    ) = SleepRecord(
        id = id, day = wake.toLocalDate().toString(), bedtime = bed.toInstant().toString(),
        wakeTime = wake.toInstant().toString(), durationMinutes = Duration.between(bed, wake).toMinutes().toInt(),
        rested = 5, fatigue = 2, source = "diary", modifiedAt = modified.toString(),
        sleepLatencyMinutes = latency, awakeMinutes = 0
    )

    private fun expectIllegalArgument(block: () -> Unit) {
        try {
            block()
            fail("Expected IllegalArgumentException")
        } catch (_: IllegalArgumentException) {
            // expected
        }
    }

    private fun at(year: Int, month: Int, day: Int, hour: Int, minute: Int): ZonedDateTime =
        ZonedDateTime.of(year, month, day, hour, minute, 0, 0, seoul)
}
