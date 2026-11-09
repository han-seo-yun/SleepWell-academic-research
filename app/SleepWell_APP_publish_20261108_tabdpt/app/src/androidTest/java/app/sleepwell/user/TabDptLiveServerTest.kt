package app.sleepwell.user

import android.test.InstrumentationTestCase
import org.json.JSONObject
import java.io.File
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * End-to-end research-server verification. The host provisions the config and
 * `adb reverse tcp:8766 tcp:8766`; this test never persists participant data.
 */
@Suppress("DEPRECATION")
class TabDptLiveServerTest : InstrumentationTestCase() {
    fun testActualServerPredictionFlowsThroughEngineWithProvenance() {
        val context = instrumentation.targetContext
        val configFile = File(context.filesDir, "tabdpt_verification_config.json")
        if (!configFile.isFile) {
            System.err.println("SKIP TabDptLiveServerTest: missing ${configFile.absolutePath}; this is not verification evidence")
            return
        }
        val config = JSONObject(configFile.readText(Charsets.UTF_8))
        val baseUrl = config.getString("base_url").trimEnd('/')
        val endpoint = if (baseUrl.endsWith("/v1/predict")) baseUrl else "$baseUrl/v1/predict"
        val settings = TabDptServerSettings(
            enabled = true,
            endpoint = endpoint,
            bearerToken = config.getString("token")
        )
        TabDptSettingsStore.requireValidEndpoint(endpoint)
        val provider = HttpTabDptPredictionClient(settings)
        val profile = profile()
        val now = now()

        val output = UserModelEngine(context, provider).compute(profile, emptyList(), now.toInstant())
        val runtime = output.outcomes.getJSONObject("tabdpt_runtime")
        val measurement = output.outcomes.getJSONObject("measurement_recommendation")

        assertEquals("available_applied", runtime.getString("status"))
        assertEquals(TABDPT_MODEL_VERSION, runtime.getString("model_version"))
        assertEquals(TABDPT_FEATURE_CONTRACT, runtime.getString("feature_contract"))
        val provenance = runtime.getJSONObject("provenance")
        assertEquals(4, provenance.getInt("ensembles"))
        assertEquals(20261107, provenance.getInt("seed"))
        assertFalse(provenance.getBoolean("online_residual_correction"))
        assertTrue(provenance.getString("checkpoint_sha256").matches(Regex("[0-9a-f]{64}")))
        assertFinite(output.forecast.cmiBedOffset)
        assertFinite(output.forecast.mesaBedOffset)
        assertFinite(output.forecast.ensembleBedOffset)
        assertEquals(
            (output.forecast.cmiBedOffset!! + output.forecast.mesaBedOffset!!) / 2.0,
            output.forecast.ensembleBedOffset!!,
            1e-9
        )
        assertTrue(measurement.getBoolean("tabdpt_hybrid_preference_applied"))
        assertTrue(output.recommendation.planFeasible)
        assertEquals("2026-11-04T07:00+09:00", output.recommendation.wakeTime)
        assertTrue(output.recommendation.durationMinutes >= 500)
        assertTrue(output.forecast.note.contains("TabDPT 하이브리드"))
    }

    fun testRealServerRejectsInvalidBearerWithoutFakeForecastOrScheduleLoss() {
        val context = instrumentation.targetContext
        val configFile = File(context.filesDir, "tabdpt_verification_config.json")
        if (!configFile.isFile) {
            System.err.println("SKIP TabDptLiveServerTest invalid-bearer: missing config; this is not verification evidence")
            return
        }
        val baseUrl = JSONObject(configFile.readText(Charsets.UTF_8)).getString("base_url").trimEnd('/')
        val endpoint = if (baseUrl.endsWith("/v1/predict")) baseUrl else "$baseUrl/v1/predict"
        assertPhysicalFallback(HttpTabDptPredictionClient(TabDptServerSettings(
            enabled = true, endpoint = endpoint, bearerToken = "invalid-token-must-return-401"
        )), "request_failed")
    }

    fun testConnectionRefusedKeepsPhysicalRecommendationAndNoFakeForecast() {
        assertPhysicalFallback(HttpTabDptPredictionClient(TabDptServerSettings(
            enabled = true,
            endpoint = "http://127.0.0.1:1/v1/predict",
            bearerToken = "unused-but-long-enough-research-token"
        )), "request_failed")
    }

    private fun assertPhysicalFallback(provider: TabDptPredictionProvider, reason: String) {
        val context = instrumentation.targetContext
        val physical = UserModelEngine(context).compute(profile(), emptyList(), now().toInstant())
        val output = UserModelEngine(context, provider).compute(profile(), emptyList(), now().toInstant())
        val runtime = output.outcomes.getJSONObject("tabdpt_runtime")

        assertEquals("unavailable_physical_only", runtime.getString("status"))
        assertEquals(reason, runtime.getString("reason_code"))
        assertFalse(runtime.has("model_version"))
        assertNull(output.forecast.cmiBedOffset)
        assertNull(output.forecast.mesaBedOffset)
        assertNull(output.forecast.ensembleBedOffset)
        assertEquals(physical.recommendation.bedtime, output.recommendation.bedtime)
        assertEquals(physical.recommendation.wakeTime, output.recommendation.wakeTime)
        assertFalse(output.outcomes.getJSONObject("measurement_recommendation")
            .getBoolean("tabdpt_hybrid_preference_applied"))
    }

    private fun profile() = UserProfile(
        ageBand = "18-29", sex = "female", activityMinutes = 45,
        usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
        createdAt = "2026-10-01T00:00:00Z", sleepLatencyMinutes = 20.0,
        averageSteps = 6_500.0, exerciseMinutes = 25.0
    )

    private fun now() = ZonedDateTime.of(
        2026, 11, 3, 20, 0, 0, 0, ZoneId.of("Asia/Seoul")
    )

    private fun assertFinite(value: Double?) {
        assertNotNull(value)
        assertTrue(value!!.isFinite())
    }
}
