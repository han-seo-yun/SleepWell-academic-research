package app.sleepwell.user

import android.test.InstrumentationTestCase
import org.json.JSONArray
import org.json.JSONObject
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class PortableFusionGradualParityTest : InstrumentationTestCase() {
    private val tolerance = 1e-7

    fun testCanonicalPythonGradualGoldenFixtures() {
        val fixture = instrumentation.context.assets
            .open("portable_fusion_gradual_v1_golden.json")
            .bufferedReader()
            .use { JSONObject(it.readText()) }
        assertEquals(1, fixture.getInt("schema_version"))
        assertEquals(PortableFusionModel.MODEL_VERSION, fixture.getString("model_version"))
        assertEquals(PortableFusionModel.RUNTIME_MODEL_VERSION, fixture.getString("runtime_model_version"))
        assertEquals(PortableFusionModel.RUNTIME_PERSONALIZATION_POLICY, fixture.getString("personalization_policy"))
        assertEquals(PortableFusionModel.FEATURE_CONTRACT, fixture.getString("feature_contract"))
        assertEquals(PortableFusionModel.PHYSICAL_PROVIDER, fixture.getString("physical_provider"))
        assertEquals(PortableFusionModel.RUNTIME_METRICS_SCOPE, fixture.getString("research_metrics_scope"))

        val model = model()
        val cases = fixture.getJSONArray("cases")
        assertEquals(9, cases.length())
        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            val result = model.predict(
                profile(case.getJSONObject("profile")),
                records(case.getJSONArray("records")),
                ZonedDateTime.parse(case.getString("cutoff"))
            )
            val path = case.getString("name")
            val expected = case.getJSONObject("expected")
            assertJsonEquals(expected.getJSONObject("snapshots"), result.snapshots, "$path.snapshots")
            assertJsonEquals(expected.getJSONObject("personalization"), result.personalization, "$path.personalization")
            assertJsonContains(expected.getJSONObject("predictions"), result.predictions, "$path.predictions")
            assertRuntimeMetadata(result, path)
        }
    }

    fun testDefaultRuntimeIsGradualAndLegacyComparisonRemainsExplicit() {
        val case = fixtureCase("history_13")
        val profile = profile(case.getJSONObject("profile"))
        val records = records(case.getJSONArray("records"))
        val cutoff = ZonedDateTime.parse(case.getString("cutoff"))
        val model = model()

        val defaultRuntime = model.predict(profile, records, cutoff)
        val explicitGradual = model.predict(
            profile, records, cutoff, PortableFusionModel.PersonalizationPolicy.GRADUAL_V1
        )
        val legacy = model.predict(
            profile, records, cutoff, PortableFusionModel.PersonalizationPolicy.LEGACY14
        )

        assertJsonEquals(defaultRuntime.snapshots, explicitGradual.snapshots, "default-gradual.snapshots")
        assertJsonEquals(defaultRuntime.predictions, explicitGradual.predictions, "default-gradual.predictions")
        assertEquals(13.0 / 27.0,
            defaultRuntime.personalization.getJSONObject("cmi").getDouble("personal_weight"), tolerance)
        assertEquals(0.0,
            legacy.personalization.getJSONObject("cmi").getDouble("personal_weight"), tolerance)
        assertEquals(PortableFusionModel.RUNTIME_MODEL_VERSION, defaultRuntime.runtimeModelVersion)
        assertEquals(PortableFusionModel.RUNTIME_PERSONALIZATION_POLICY, defaultRuntime.personalizationPolicy)
        assertEquals(PortableFusionModel.RUNTIME_METRICS_SCOPE, defaultRuntime.researchMetricsScope)
        assertEquals(PortableFusionModel.MODEL_VERSION, legacy.runtimeModelVersion)
        assertEquals(PortableFusionModel.LEGACY_PERSONALIZATION_POLICY, legacy.personalizationPolicy)
        assertEquals(PortableFusionModel.FROZEN_METRICS_SCOPE, legacy.researchMetricsScope)
        assertFalse(legacy.personalization.getJSONObject("cmi").has("personalization_policy"))
    }

    private fun model(): PortableFusionModel = PortableFusionModel(
        { path -> instrumentation.targetContext.assets.open(path).use { it.readBytes() } },
        testable = true
    )

    private fun fixtureCase(name: String): JSONObject {
        val fixture = instrumentation.context.assets
            .open("portable_fusion_gradual_v1_golden.json")
            .bufferedReader()
            .use { JSONObject(it.readText()) }
        val cases = fixture.getJSONArray("cases")
        for (index in 0 until cases.length()) {
            val case = cases.getJSONObject(index)
            if (case.getString("name") == name) return case
        }
        error("fixture case not found: $name")
    }

    private fun profile(value: JSONObject): UserProfile = UserProfile(
        ageBand = value.getString("age_band"),
        sex = value.getString("sex"),
        activityMinutes = value.getInt("activity_minutes"),
        usualBed = value.getString("usual_bed"),
        usualWake = value.getString("usual_wake"),
        desiredSleepMinutes = value.getInt("desired_sleep_minutes"),
        createdAt = value.getString("created_at")
    )

    private fun records(values: JSONArray): List<SleepRecord> = (0 until values.length()).map { index ->
        val value = values.getJSONObject(index)
        SleepRecord(
            id = value.getString("id"),
            day = value.getString("day"),
            bedtime = nullableString(value, "bedtime"),
            wakeTime = nullableString(value, "wake_time"),
            durationMinutes = value.getInt("duration_minutes"),
            rested = nullableInt(value, "rested"),
            fatigue = nullableInt(value, "fatigue"),
            source = value.getString("source"),
            modifiedAt = nullableString(value, "modified_at")
        )
    }

    private fun nullableString(value: JSONObject, key: String): String =
        if (value.isNull(key)) "" else value.getString(key)

    private fun nullableInt(value: JSONObject, key: String): Int? =
        if (value.isNull(key)) null else value.getInt(key)

    private fun assertRuntimeMetadata(result: PortableFusionModel.Result, path: String) {
        assertEquals("$path.runtimeModelVersion", PortableFusionModel.RUNTIME_MODEL_VERSION, result.runtimeModelVersion)
        assertEquals("$path.personalizationPolicy", PortableFusionModel.RUNTIME_PERSONALIZATION_POLICY, result.personalizationPolicy)
        assertEquals("$path.metricsScope", PortableFusionModel.RUNTIME_METRICS_SCOPE, result.researchMetricsScope)
        listOf("cmi", "mesa").forEach { cohort ->
            val artifact = result.artifacts.getJSONObject(cohort)
            assertEquals(PortableFusionModel.MODEL_VERSION, artifact.getString("artifact_model_version"))
            assertEquals(PortableFusionModel.RUNTIME_MODEL_VERSION, artifact.getString("runtime_model_version"))
            assertEquals(PortableFusionModel.RUNTIME_PERSONALIZATION_POLICY, artifact.getString("personalization_policy"))
            assertEquals(PortableFusionModel.FEATURE_CONTRACT, artifact.getString("model_feature_contract"))
            assertEquals(PortableFusionModel.RUNTIME_METRICS_SCOPE, artifact.getString("research_metrics_scope"))
        }
    }

    private fun assertJsonEquals(expected: Any?, actual: Any?, path: String) {
        when {
            expected === JSONObject.NULL -> assertTrue("$path expected null", actual === JSONObject.NULL || actual == null)
            expected is JSONObject -> {
                assertTrue("$path actual is not JSONObject", actual is JSONObject)
                val actualObject = actual as JSONObject
                assertEquals("$path key count", expected.length(), actualObject.length())
                val keys = expected.keys()
                while (keys.hasNext()) {
                    val key = keys.next()
                    assertTrue("$path missing key $key", actualObject.has(key))
                    assertJsonEquals(expected.get(key), actualObject.get(key), "$path.$key")
                }
            }
            expected is JSONArray -> {
                assertTrue("$path actual is not JSONArray", actual is JSONArray)
                val actualArray = actual as JSONArray
                assertEquals("$path length", expected.length(), actualArray.length())
                for (index in 0 until expected.length()) {
                    assertJsonEquals(expected.get(index), actualArray.get(index), "$path[$index]")
                }
            }
            expected is Number -> {
                assertTrue("$path actual is not numeric: $actual", actual is Number)
                assertEquals(path, expected.toDouble(), (actual as Number).toDouble(), tolerance)
            }
            else -> assertEquals(path, expected, actual)
        }
    }

    private fun assertJsonContains(expected: JSONObject, actual: JSONObject, path: String) {
        val keys = expected.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            assertTrue("$path missing key $key", actual.has(key))
            val expectedValue = expected.get(key)
            val actualValue = actual.get(key)
            if (expectedValue is JSONObject && actualValue is JSONObject) {
                assertJsonContains(expectedValue, actualValue, "$path.$key")
            } else {
                assertJsonEquals(expectedValue, actualValue, "$path.$key")
            }
        }
    }
}
