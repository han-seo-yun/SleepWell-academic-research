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
    val dailyContext: DailyContext? = null
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
    val modifiedAt: String
)

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

data class RecommendationTiming(
    val nightId: String,
    val modelCutoffAt: String,
    val expectedOnsetAt: String,
    val initialDecisionAt: String
)

data class RecommendationPublication(
    val id: String,
    val nightId: String,
    val revision: Int,
    val slot: String,
    val kind: String,
    val scheduledAt: String,
    val publishedAt: String,
    val inputFingerprint: String,
    val recommendation: Recommendation
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
    val dailyContexts: List<DailyContext> = emptyList(),
    val recommendationTiming: RecommendationTiming? = null,
    val recommendationPublications: List<RecommendationPublication> = emptyList(),
    val evaluatedRecommendationSlots: List<String> = emptyList(),
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

internal fun profileFromJson(v: JSONObject) = UserProfile(
    v.getString("age_band"), v.getString("sex"), v.getInt("activity_minutes"),
    v.getString("usual_bed"), v.getString("usual_wake"), v.getInt("desired_sleep_minutes"),
    v.getString("created_at"), v.optString("occupation", ""), v.optJSONObject("daily_context")?.let(::dailyContextFromJson)
)

internal fun SleepRecord.toJson() = JSONObject()
    .put("id", id).put("day", day).put("bedtime", bedtime).put("wake_time", wakeTime)
    .put("duration_minutes", durationMinutes).put("rested", rested ?: JSONObject.NULL).put("fatigue", fatigue ?: JSONObject.NULL)
    .put("source", source).put("modified_at", modifiedAt)

internal fun recordFromJson(v: JSONObject) = SleepRecord(
    v.getString("id"), v.getString("day"), v.getString("bedtime"), v.getString("wake_time"),
    v.getInt("duration_minutes"), v.intOrNull("rested"), v.intOrNull("fatigue"),
    v.getString("source"), v.getString("modified_at")
)

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

internal fun RecommendationTiming.toJson() = JSONObject()
    .put("night_id", nightId).put("model_cutoff_at", modelCutoffAt)
    .put("expected_onset_at", expectedOnsetAt).put("initial_decision_at", initialDecisionAt)

internal fun recommendationTimingFromJson(v: JSONObject) = RecommendationTiming(
    v.getString("night_id"), v.getString("model_cutoff_at"),
    v.getString("expected_onset_at"), v.getString("initial_decision_at")
)

internal fun RecommendationPublication.toJson() = JSONObject()
    .put("id", id).put("night_id", nightId).put("revision", revision)
    .put("slot", slot).put("kind", kind).put("scheduled_at", scheduledAt)
    .put("published_at", publishedAt).put("input_fingerprint", inputFingerprint)
    .put("recommendation", recommendation.toJson())

internal fun recommendationPublicationFromJson(v: JSONObject) = RecommendationPublication(
    v.getString("id"), v.getString("night_id"), v.getInt("revision"),
    v.getString("slot"), v.getString("kind"), v.getString("scheduled_at"),
    v.getString("published_at"), v.getString("input_fingerprint"),
    recommendationFromJson(v.getJSONObject("recommendation"))
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
    .put("daily_contexts", JSONArray().also { a -> dailyContexts.forEach { a.put(it.toJson()) } })
    .put("recommendation_timing", recommendationTiming?.toJson() ?: JSONObject.NULL)
    .put("recommendation_publications", JSONArray().also { a -> recommendationPublications.forEach { a.put(it.toJson()) } })
    .put("evaluated_recommendation_slots", JSONArray().also { a -> evaluatedRecommendationSlots.forEach { a.put(it) } })
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
    dailyContexts = v.optJSONArray("daily_contexts")?.let { a ->
        (0 until a.length()).map { dailyContextFromJson(a.getJSONObject(it)) }
    } ?: v.optJSONObject("profile")?.optJSONObject("daily_context")?.let { listOf(dailyContextFromJson(it)) } ?: emptyList(),
    recommendationTiming = v.optJSONObject("recommendation_timing")?.let(::recommendationTimingFromJson),
    recommendationPublications = v.optJSONArray("recommendation_publications")?.let { a ->
        (0 until a.length()).map { recommendationPublicationFromJson(a.getJSONObject(it)) }
    } ?: emptyList(),
    evaluatedRecommendationSlots = v.optJSONArray("evaluated_recommendation_slots")?.let { a ->
        (0 until a.length()).map { a.getString(it) }
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
