package app.sleepwell.user

import org.json.JSONObject
import java.nio.file.Files
import java.nio.file.Path
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.abs

private val seoul = ZoneId.of("Asia/Seoul")
private val now = Instant.parse("2026-10-25T11:05:00Z") // 20:05 Asia/Seoul

private data class Snapshot(
    val engineVersion: String,
    val recommendationPolicy: String,
    val personalizationVersion: String,
    val mode: String,
    val eligible: Int,
    val mass: Double,
    val weight: Double,
    val pattern: JSONObject
)

private fun requireNear(actual: Double, expected: Double, tolerance: Double, label: String) {
    check(abs(actual - expected) <= tolerance) { "$label: expected=$expected actual=$actual" }
}

private fun profile(createdAt: String = "2025-01-01T00:00:00Z") = UserProfile(
    ageBand = "30-44", sex = "any", activityMinutes = 60,
    usualBed = "23:00", usualWake = "07:00", desiredSleepMinutes = 480,
    createdAt = createdAt, occupation = "research verification"
)

private fun record(
    id: String,
    wake: Instant,
    midpointMinute: Int = 180,
    duration: Int = 480
): SleepRecord {
    val wakeZoned = wake.atZone(seoul)
    val midpoint = wakeZoned.toLocalDate().atStartOfDay(seoul).plusMinutes(midpointMinute.toLong())
    val bed = midpoint.minusMinutes((duration / 2).toLong())
    val end = bed.plusMinutes(duration.toLong())
    return SleepRecord(
        id = id,
        day = bed.toLocalDate().toString(),
        bedtime = bed.toInstant().toString(),
        wakeTime = end.toInstant().toString(),
        durationMinutes = duration,
        rested = 4,
        fatigue = 2,
        source = "manual",
        modifiedAt = end.plusMinutes(1).toInstant().toString()
    )
}

private fun snapshot(engine: UserModelEngine, records: List<SleepRecord>, p: UserProfile = profile()): Snapshot {
    val outcomes = engine.compute(p, records, now).outcomes
    val personal = outcomes.getJSONObject("personalization")
    return Snapshot(
        engineVersion = outcomes.getString("recommendation_engine_version"),
        recommendationPolicy = outcomes.getString("recommendation_policy"),
        personalizationVersion = personal.getString("parameter_version"),
        mode = personal.getString("mode"),
        eligible = personal.getInt("eligible_primary_nights"),
        mass = personal.getDouble("decayed_mass"),
        weight = personal.getDouble("weight"),
        pattern = personal.getJSONObject("weektype_pattern")
    )
}

private fun verificationJson(results: Map<String, Any?>): String = JSONObject(results).toString()

fun main(args: Array<String>) {
    require(args.size == 2) { "usage: <android-assets-root> <verification-json>" }
    val assets = Path.of(args[0])
    val engine = UserModelEngine(assetLoader = { relative -> Files.readAllBytes(assets.resolve(relative)) })

    val zero = snapshot(engine, emptyList())
    check(zero.engineVersion == "sleep-opportunity-v6-daily-personalization")
    check(zero.recommendationPolicy == "wake_constraint_plus_sleep_opportunity_v3_daily_personalization")
    check(zero.personalizationVersion == "personalization-v4-daily-recency")
    check(zero.mode == "population_warm_start" && zero.eligible == 0)
    requireNear(zero.mass, 0.0, 0.0, "zero mass")
    requireNear(zero.weight, 0.0, 0.0, "zero weight")

    val recentWake = Instant.parse("2026-10-24T22:00:00Z")
    val one = snapshot(engine, listOf(record("one", recentWake)))
    check(one.mode == "daily_recency_shrinkage" && one.eligible == 1 && one.mass > 0.0 && one.weight > 0.0)

    val eightRows = (0 until 8).map { index ->
        record("eight-$index", recentWake.minusSeconds(index * 86_400L), midpointMinute = 180 + index * 3)
    }
    val eight = snapshot(engine, eightRows)
    check(eight.mode == "daily_recency_shrinkage" && eight.eligible == 8)
    check(eight.mass > one.mass && eight.weight > one.weight)

    val gapRecent = snapshot(engine, listOf(record("gap-recent", recentWake)))
    val gapOld = snapshot(engine, listOf(record("gap-old", Instant.parse("2026-07-01T22:00:00Z"))))
    check(gapOld.mass < gapRecent.mass && gapOld.weight < gapRecent.weight)

    val underflow = snapshot(
        engine,
        listOf(record("underflow", Instant.parse("1000-01-06T22:00:00Z"))),
        profile("0900-01-01T00:00:00Z")
    )
    check(underflow.eligible == 1)
    requireNear(underflow.mass, 0.0, 0.0, "underflow mass")
    requireNear(underflow.weight, 0.0, 0.0, "underflow weight")
    check(underflow.mode == "population_warm_start")

    fun wakeOn(date: LocalDate) = ZonedDateTime.of(date, java.time.LocalTime.NOON, seoul).toInstant()
    val oldDates = generateSequence(LocalDate.parse("1000-01-01")) { it.plusDays(1) }.take(14).toList()
    val oldWeekdays = oldDates.filter { it.dayOfWeek.value < 6 }.take(2)
    val oldWeekends = oldDates.filter { it.dayOfWeek.value >= 6 }.take(2)
    val antipodalRows = listOf(
        record("weekday-zero", wakeOn(oldWeekdays[0]), midpointMinute = 0),
        record("weekday-half", wakeOn(oldWeekdays[1]), midpointMinute = 720),
        record("weekend-zero", wakeOn(oldWeekends[0]), midpointMinute = 0),
        record("weekend-half", wakeOn(oldWeekends[1]), midpointMinute = 720)
    )
    val antipodal = snapshot(engine, antipodalRows, profile("0900-01-01T00:00:00Z")).pattern
    check(antipodal.getInt("weekday_count") == 2 && antipodal.getInt("weekend_count") == 2)
    check(antipodal.getDouble("weekday_resultant_length") <= 1e-12)
    check(antipodal.getDouble("weekend_resultant_length") <= 1e-12)
    check(antipodal.isNull("weekday_midpoint_min") && antipodal.isNull("weekend_midpoint_min"))
    check(antipodal.isNull("weekend_minus_weekday_min"))
    check(antipodal.getString("status") == "insufficient_history")

    val publicResult = linkedMapOf<String, Any?>(
        "schema_version" to 1,
        "subject" to "actual Kotlin UserModelEngine daily-personalization verification",
        "engine_version" to zero.engineVersion,
        "recommendation_policy" to zero.recommendationPolicy,
        "personalization_policy_version" to zero.personalizationVersion,
        "all_passed" to true,
        "cases" to linkedMapOf(
            "zero_records" to mapOf("passed" to true, "mode" to zero.mode, "eligible" to zero.eligible, "decayed_mass" to zero.mass, "personal_weight" to zero.weight),
            "one_record" to mapOf("passed" to true, "mode" to one.mode, "eligible" to one.eligible, "decayed_mass" to one.mass, "personal_weight" to one.weight),
            "eight_records" to mapOf("passed" to true, "mode" to eight.mode, "eligible" to eight.eligible, "decayed_mass" to eight.mass, "personal_weight" to eight.weight),
            "gap_decay" to mapOf("passed" to true, "recent_mass" to gapRecent.mass, "old_mass" to gapOld.mass, "recent_weight" to gapRecent.weight, "old_weight" to gapOld.weight),
            "zero_mass_underflow" to mapOf("passed" to true, "mode" to underflow.mode, "eligible" to underflow.eligible, "decayed_mass" to underflow.mass, "personal_weight" to underflow.weight),
            "antipodal_pattern_guard" to mapOf(
                "passed" to true,
                "status" to antipodal.getString("status"),
                "weekday_count" to antipodal.getInt("weekday_count"),
                "weekend_count" to antipodal.getInt("weekend_count"),
                "weekday_resultant_length" to antipodal.getDouble("weekday_resultant_length"),
                "weekend_resultant_length" to antipodal.getDouble("weekend_resultant_length"),
                "midpoints_defined" to false,
                "used_for_recommendation" to false
            )
        ),
        "scope" to mapOf(
            "production_sources_executed" to listOf("UserModelEngine.kt", "PortableFusionModel.kt", "UserModels.kt", "DailyContext.kt", "UserContractRules.java"),
            "frozen_model_assets_loaded" to true,
            "android_device_tested" to false,
            "note" to "JVM harness executes the production Kotlin engine with frozen packaged assets; no Android device was connected."
        )
    )
    val output = Path.of(args[1])
    Files.createDirectories(output.parent)
    Files.writeString(output, verificationJson(publicResult))
    println(verificationJson(publicResult))
}
