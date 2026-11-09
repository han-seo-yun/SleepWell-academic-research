package app.sleepwell.user

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant

data class UserProfile(
    val ageBand: String,
    val sex: String,
    val activityMinutes: Int,
    val usualBed: String,
    val usualWake: String,
    val desiredSleepMinutes: Int,
    val createdAt: String,
    val occupation: String = "",
    val dailyContext: DailyContext? = null,
    val sleepLatencyMinutes: Double = 15.0,
    val averageSteps: Double = 0.0,
    val exerciseMinutes: Double = 0.0
)

data class SleepRecord(
    val id: String,
    val day: String,
    val bedtime: String,
    val wakeTime: String,
    val durationMinutes: Int,
    val rested: Int?,
    val fatigue: Int?,
    val source: String,
    val modifiedAt: String,
    val sleepLatencyMinutes: Int? = null,
    val awakeMinutes: Int? = null,
    val steps: Int? = null,
    val exerciseMinutes: Double? = null,
    /** Wearable subjective/activity values are usable only when explicitly questionnaire-derived. */
    val subjectiveSource: String? = null,
    val subjectiveObservedAt: String? = null
)

enum class SleepInputPolicy(val wireName: String) {
    DIARY_ONLY("diary_only"),
    DIARY_AND_WEARABLE_V1("diary_and_wearable_v1")
}

data class Forecast(
    val generatedAt: String,
    val cmiBedOffset: Double?,
    val mesaBedOffset: Double?,
    val ensembleBedOffset: Double?,
    val note: String
)

data class Recommendation(
    val generatedAt: String,
    val wakeTime: String,
    val bedtime: String,
    val durationMinutes: Int,
    val regularityMinutes: Int?,
    val subjectiveQuality: Int?,
    val processS: Double?,
    val explanation: String,
    val planFeasible: Boolean = true
)

data class UserState(
    val participantKey: String,
    val profile: UserProfile? = null,
    val records: List<SleepRecord> = emptyList(),
    val recommendation: Recommendation? = null,
    val forecast: Forecast? = null,
    val modelOutcomes: JSONObject? = null,
    val prospectiveForecasts: List<JSONObject> = emptyList(),
    val recommendationExposures: List<JSONObject> = emptyList(),
    val prospectiveOutcomes: List<JSONObject> = emptyList(),
    val autoBackup: Boolean = false,
    val lastLocalBackup: String? = null,
    val lastPcDelivery: String? = null,
    val status: String? = null
)

data class PendingBackup(val day: String, val revision: Int, val payload: String)

internal fun UserProfile.toJson() = JSONObject()
    .put("age_band", ageBand).put("sex", sex).put("activity_minutes", activityMinutes)
    .put("usual_bed", usualBed).put("usual_wake", usualWake)
    .put("desired_sleep_minutes", desiredSleepMinutes).put("created_at", createdAt)
    .put("occupation", occupation).put("daily_context", dailyContext?.toJson() ?: JSONObject.NULL)
    .put("sleep_latency_minutes", sleepLatencyMinutes).put("average_steps", averageSteps)
    .put("exercise_minutes", exerciseMinutes)

internal fun profileFromJson(v: JSONObject) = UserProfile(
    v.getString("age_band"), v.getString("sex"), v.getInt("activity_minutes"),
    v.getString("usual_bed"), v.getString("usual_wake"), v.getInt("desired_sleep_minutes"),
    v.getString("created_at"), v.optString("occupation", ""), v.optJSONObject("daily_context")?.let(::dailyContextFromJson),
    v.optDouble("sleep_latency_minutes", 15.0), v.optDouble("average_steps", 0.0),
    v.optDouble("exercise_minutes", 0.0)
)

internal fun SleepRecord.toJson() = JSONObject()
    .put("id", id).put("day", day).put("bedtime", bedtime).put("wake_time", wakeTime)
    .put("duration_minutes", durationMinutes).put("rested", rested ?: JSONObject.NULL).put("fatigue", fatigue ?: JSONObject.NULL)
    .put("source", source).put("modified_at", modifiedAt)
    .put("sleep_latency_minutes", sleepLatencyMinutes ?: JSONObject.NULL)
    .put("awake_minutes", awakeMinutes ?: JSONObject.NULL)
    .put("steps", steps ?: JSONObject.NULL).put("exercise_minutes", exerciseMinutes ?: JSONObject.NULL)
    .put("subjective_source", subjectiveSource ?: JSONObject.NULL)
    .put("subjective_observed_at", subjectiveObservedAt ?: JSONObject.NULL)

internal fun recordFromJson(v: JSONObject) = SleepRecord(
    v.getString("id"), v.getString("day"), v.getString("bedtime"), v.getString("wake_time"),
    v.getInt("duration_minutes"), v.intOrNull("rested"), v.intOrNull("fatigue"),
    v.getString("source"), v.getString("modified_at"),
    v.intOrNull("sleep_latency_minutes"), v.intOrNull("awake_minutes"),
    v.intOrNull("steps"), v.doubleOrNull("exercise_minutes"),
    v.optionalString("subjective_source"), v.optionalString("subjective_observed_at")
)

private fun JSONObject.optionalString(key: String): String? =
    if (!has(key) || isNull(key)) null else optString(key).takeIf { it.isNotBlank() }

internal fun Forecast.toJson() = JSONObject().put("generated_at", generatedAt)
    .putNullable("cmi_bed_offset", cmiBedOffset).putNullable("mesa_bed_offset", mesaBedOffset)
    .putNullable("ensemble_bed_offset", ensembleBedOffset).put("note", note)

internal fun forecastFromJson(v: JSONObject) = Forecast(
    v.getString("generated_at"), v.doubleOrNull("cmi_bed_offset"),
    v.doubleOrNull("mesa_bed_offset"), v.doubleOrNull("ensemble_bed_offset"), v.getString("note")
)

internal fun Recommendation.toJson() = JSONObject().put("generated_at", generatedAt)
    .put("wake_time", wakeTime).put("bedtime", bedtime).put("duration_minutes", durationMinutes)
    .putNullable("regularity_minutes", regularityMinutes).putNullable("subjective_quality", subjectiveQuality)
    .putNullable("process_s", processS).put("explanation", explanation)
    .put("plan_feasible", planFeasible)

internal fun recommendationFromJson(v: JSONObject) = Recommendation(
    v.getString("generated_at"), v.getString("wake_time"), v.getString("bedtime"),
    v.getInt("duration_minutes"), v.intOrNull("regularity_minutes"), v.intOrNull("subjective_quality"),
    v.doubleOrNull("process_s"), v.getString("explanation"), v.optBoolean("plan_feasible", true)
)

internal fun UserState.toJson() = JSONObject().put("participant_key", participantKey)
    .put("profile", profile?.toJson() ?: JSONObject.NULL)
    .put("records", JSONArray().also { a -> records.forEach { a.put(it.toJson()) } })
    .put("recommendation", recommendation?.toJson() ?: JSONObject.NULL)
    .put("forecast", forecast?.toJson() ?: JSONObject.NULL)
    .put("model_outcomes", modelOutcomes ?: JSONObject.NULL)
    .put("prospective_forecasts", JSONArray().also { a -> prospectiveForecasts.forEach { a.put(it) } })
    .put("recommendation_exposures", JSONArray().also { a -> recommendationExposures.forEach { a.put(it) } })
    .put("prospective_outcomes", JSONArray().also { a -> prospectiveOutcomes.forEach { a.put(it) } })
    .put("auto_backup", autoBackup).put("last_local_backup", lastLocalBackup ?: JSONObject.NULL)
    .put("last_pc_delivery", lastPcDelivery ?: JSONObject.NULL).put("status", status ?: JSONObject.NULL)

internal fun userStateFromJson(v: JSONObject) = UserState(
    participantKey = v.getString("participant_key"),
    profile = v.optJSONObject("profile")?.let(::profileFromJson),
    records = v.optJSONArray("records")?.let { a -> (0 until a.length()).map { recordFromJson(a.getJSONObject(it)) } } ?: emptyList(),
    recommendation = v.optJSONObject("recommendation")?.let(::recommendationFromJson),
    forecast = v.optJSONObject("forecast")?.let(::forecastFromJson),
    modelOutcomes = v.optJSONObject("model_outcomes"),
    prospectiveForecasts = v.optJSONArray("prospective_forecasts")?.let { a ->
        (0 until a.length()).map { JSONObject(a.getJSONObject(it).toString()) }
    } ?: emptyList(),
    recommendationExposures = v.optJSONArray("recommendation_exposures")?.let { a ->
        (0 until a.length()).map { JSONObject(a.getJSONObject(it).toString()) }
    } ?: emptyList(),
    prospectiveOutcomes = v.optJSONArray("prospective_outcomes")?.let { a ->
        (0 until a.length()).map { JSONObject(a.getJSONObject(it).toString()) }
    } ?: emptyList(),
    autoBackup = v.optBoolean("auto_backup"),
    lastLocalBackup = v.stringOrNull("last_local_backup"),
    lastPcDelivery = v.stringOrNull("last_pc_delivery"),
    status = v.stringOrNull("status")
)

private fun JSONObject.putNullable(key: String, value: Any?): JSONObject = put(key, value ?: JSONObject.NULL)
private fun JSONObject.stringOrNull(key: String) = if (!has(key) || isNull(key)) null else getString(key)
private fun JSONObject.doubleOrNull(key: String) = if (!has(key) || isNull(key)) null else getDouble(key)
private fun JSONObject.intOrNull(key: String) = if (!has(key) || isNull(key)) null else getInt(key)

internal fun nowIso(): String = Instant.now().toString()
