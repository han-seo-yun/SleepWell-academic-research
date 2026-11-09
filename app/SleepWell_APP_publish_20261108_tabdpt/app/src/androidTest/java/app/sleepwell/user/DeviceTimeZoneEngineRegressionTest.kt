package app.sleepwell.user

import android.test.InstrumentationTestCase
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

@Suppress("DEPRECATION")
class DeviceTimeZoneEngineRegressionTest : InstrumentationTestCase() {
    private val profile = UserProfile(
        ageBand = "30-44", sex = "any", activityMinutes = 60,
        usualBed = "23:00", usualWake = "07:30", desiredSleepMinutes = 480,
        createdAt = "2026-01-01T00:00:00Z"
    )

    fun testDefaultZonePreservesExplicitKstParity() {
        val now = ZonedDateTime.of(2026, 11, 6, 20, 0, 0, 0, ZoneId.of("Asia/Seoul")).toInstant()
        val implicit = UserModelEngine(instrumentation.targetContext).compute(profile, emptyList(), now)
        val explicit = UserModelEngine(instrumentation.targetContext, ZoneId.of("Asia/Seoul"))
            .compute(profile, emptyList(), now)

        assertEquals(implicit.recommendation, explicit.recommendation)
        assertEquals(
            implicit.outcomes.getJSONObject("research_feature_snapshot").toString(),
            explicit.outcomes.getJSONObject("research_feature_snapshot").toString()
        )
    }

    fun testUtcRecommendationUsesProfileClockInUtc() {
        val zone = ZoneId.of("UTC")
        val now = ZonedDateTime.of(2026, 11, 6, 20, 0, 0, 0, zone).toInstant()
        val output = UserModelEngine(instrumentation.targetContext, zone).compute(profile, emptyList(), now)
        val wake = ZonedDateTime.parse(output.recommendation.wakeTime)

        assertEquals(0, wake.offset.totalSeconds)
        assertEquals("07:30", wake.toLocalTime().toString())
        assertEquals("2026-11-07", wake.toLocalDate().toString())
        assertEquals("2026-11-06", output.outcomes.getString("recommendation_night_id"))
        assertEquals("UTC", output.outcomes.getString("calculation_zone"))
    }

    fun testNewYorkDstFallbackKeepsHabitualWakeAt0730Local() {
        val zone = ZoneId.of("America/New_York")
        val now = ZonedDateTime.of(2026, 10, 31, 20, 0, 0, 0, zone).toInstant()
        val output = UserModelEngine(instrumentation.targetContext, zone).compute(profile, emptyList(), now)
        val wake = ZonedDateTime.parse(output.recommendation.wakeTime)

        assertEquals("2026-11-01", wake.toLocalDate().toString())
        assertEquals("07:30", wake.toLocalTime().toString())
        assertEquals("-05:00", wake.offset.toString())
        assertEquals("2026-10-31", output.outcomes.getString("recommendation_night_id"))
        assertEquals("America/New_York", output.outcomes.getString("calculation_zone"))

        val context = buildDailyContext(
            nightId = "2026-10-31", kind = "first", firstClock = "07:30", preparation = "0",
            napStartClock = "", napEndClock = "", caffeineClock = "", caffeineAmount = "",
            now = ZonedDateTime.of(2026, 10, 31, 18, 0, 0, 0, zone).toInstant(),
            calculationZone = zone
        )
        val firstStart = ZonedDateTime.ofInstant(Instant.parse(context.firstStart!!), zone)
        assertEquals("2026-11-01T07:30-05:00[America/New_York]", firstStart.toString())
    }

    fun testSydneyDstStartupKeepsFrozenValidationIndependentOfDeviceZone() {
        val zone = ZoneId.of("Australia/Sydney")
        val now = ZonedDateTime.of(2026, 10, 3, 20, 0, 0, 0, zone).toInstant()
        val output = UserModelEngine(instrumentation.targetContext, zone).compute(profile, emptyList(), now)
        val wake = ZonedDateTime.parse(output.recommendation.wakeTime)

        assertEquals("2026-10-04", wake.toLocalDate().toString())
        assertEquals("07:30", wake.toLocalTime().toString())
        assertEquals("+11:00", wake.offset.toString())
        assertEquals("Australia/Sydney", output.outcomes.getString("calculation_zone"))
    }

    fun testData33UsesEvaluationZoneWithoutRewritingRecordInstants() {
        val record = SleepRecord(
            id = "zone-boundary", day = "legacy-day",
            bedtime = "2026-11-06T00:30:00Z", wakeTime = "2026-11-06T08:30:00Z",
            durationMinutes = 480, rested = 4, fatigue = 2, source = "diary",
            modifiedAt = "2026-11-06T08:31:00Z"
        )
        val original = record.copy()
        val now = Instant.parse("2026-11-07T04:00:00Z")
        val utc = UserModelEngine(instrumentation.targetContext, ZoneId.of("UTC"))
            .compute(profile, listOf(record), now)
        val newYork = UserModelEngine(instrumentation.targetContext, ZoneId.of("America/New_York"))
            .compute(profile, listOf(record), now)

        val utcFeature = utc.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
        val nyFeature = newYork.outcomes.getJSONObject("research_feature_snapshot").getJSONObject("cmi")
        assertTrue(utcFeature.getDouble("previous_bed") != nyFeature.getDouble("previous_bed"))
        assertEquals(original, record)
        assertEquals("legacy-day", record.day)
        assertEquals("2026-11-06T00:30:00Z", record.bedtime)
    }
}
