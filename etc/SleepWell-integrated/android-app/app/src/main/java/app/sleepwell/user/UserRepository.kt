package app.sleepwell.user

import android.annotation.SuppressLint
import android.content.Context
import app.sleepwell.sync.HealthConnectSleepReader
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.security.MessageDigest
import kotlin.math.roundToInt

class UserRepository(private val context: Context) {
    private val store = UserSecureStore(context)

    fun load(): UserState = store.load()

    fun refresh(): UserState = store.mutateState { compute(it, awake = true) }

    fun runScheduledRecommendationSlot(awake: Boolean = true): UserState =
        store.mutateState { compute(it, awake) }

    fun saveProfile(ageBand: String, sex: String, occupation: String, activity: Int, usualBed: String, usualWake: String, desired: Int): UserState {
        parseTime(usualBed); parseTime(usualWake)
        require(ageBand.trim() in setOf("18-29", "30-44", "45-59", "60-80")) { "연령대는 18-29, 30-44, 45-59, 60-80 중 하나를 입력하세요." }
        require(sex.trim() in setOf("여성", "남성", "응답 안 함")) { "성별은 여성, 남성, 응답 안 함 중 하나를 입력하세요." }
        require(activity in 0..1440) { "하루 활동시간은 0~1440분으로 입력하세요." }
        require(desired in 240..720) { "희망 수면시간은 240~720분으로 입력하세요." }
        return store.mutateState { previous ->
            val profile = UserProfile(ageBand.trim().take(40), sex.trim().take(20), activity, usualBed, usualWake, desired,
                previous.profile?.createdAt ?: nowIso(), occupation.trim().take(80), previous.profile?.dailyContext)
            compute(previous.copy(profile = profile, status = "프로필을 저장했습니다."))
        }
    }

    fun addDiary(day: String, bedtime: String, wake: String, rested: Int?, fatigue: Int?): UserState {
        require(rested == null || rested in 1..5) { "회복감은 비워 두거나 1~5 범위로 입력하세요." }
        require(fatigue == null || fatigue in 1..5) { "피로도는 비워 두거나 1~5 범위로 입력하세요." }
        val wakeDay = LocalDate.parse(day)
        val end = ZonedDateTime.of(wakeDay, parseTime(wake), ZONE)
        var start = ZonedDateTime.of(wakeDay, parseTime(bedtime), ZONE)
        if (!start.isBefore(end)) start = start.minusDays(1)
        val duration = Duration.between(start, end).toMinutes().toInt()
        require(duration in 30..960) { "수면 구간은 30분~16시간이어야 합니다." }
        val record = SleepRecord("manual:$day", day, start.toInstant().toString(), end.toInstant().toString(), duration,
            rested, fatigue, "manual", nowIso())
        return store.mutateState { current ->
            val frozenOutcomes = freezeFirstOutcome(current, record)
            compute(current.copy(records = merge(current.records, listOf(record)), prospectiveOutcomes = frozenOutcomes,
                status = "수면 일기를 저장했습니다."))
        }
    }

    @SuppressLint("NewApi")
    suspend fun importSamsung(): Pair<UserState, Int> {
        val reader = HealthConnectSleepReader(context)
        check(reader.isSupported()) { "Android 14 이상에서만 Samsung Health 기록을 불러올 수 있습니다." }
        check(reader.hasPermission()) { "Health Connect 수면 읽기 권한이 필요합니다." }
        val end = Instant.now()
        val read = reader.readCompleteSamsungSessions(end.minus(Duration.ofDays(90)), end)
        val imported = read.sessions.map { session ->
            val wake = session.end.atZone(ZONE)
            SleepRecord("health:${session.id}", wake.toLocalDate().toString(), session.start.toString(), session.end.toString(),
                Duration.between(session.start, session.end).toMinutes().toInt(), null, null, "samsung_health", session.modifiedAt.toString())
        }
        val fromIso = end.minus(Duration.ofDays(90))
        val importedIds = imported.mapTo(hashSetOf()) { it.id }
        val state = store.mutateState { current ->
            val reconciled = current.records.filterNot { old ->
                old.source == "samsung_health" && !UserContractRules.keepSamsungRecord(
                    runCatching { !Instant.parse(old.wakeTime).isBefore(fromIso) }.getOrDefault(false), old.id in importedIds
                )
            }
            compute(current.copy(records = merge(reconciled, imported),
                status = "Samsung Health 수면 ${imported.size}건을 불러왔습니다. 주관 점수는 일기에서 보완할 수 있습니다."))
        }
        return state to imported.size
    }

    fun setAutoBackup(enabled: Boolean): UserState = store.mutateState { it.copy(autoBackup = enabled) }

    fun saveDailyContext(value: DailyContext): UserState = store.mutateState { current ->
        val profile = current.profile ?: error("먼저 프로필을 저장하세요.")
        val versions = (current.dailyContexts + value)
            .distinctBy { "${it.nightId}:${it.observedAt}" }.sortedBy { it.observedAt }.takeLast(180)
        compute(current.copy(profile = profile.copy(dailyContext = value), dailyContexts = versions,
            status = "다음날 일정·낮잠·카페인 정보를 저장했습니다."))
    }

    fun updateMetadata(transform: (UserState) -> UserState): UserState = store.mutateState(transform)

    /** Record viewing separately from plan generation. The first plan shown for a night is immutable. */
    fun markRecommendationViewed(generatedAt: String): UserState = store.mutateState { current ->
        val recommendation = current.recommendation ?: return@mutateState current
        val outcomes = current.modelOutcomes ?: return@mutateState current
        if (recommendation.generatedAt != generatedAt) return@mutateState current
        val nightId = outcomes.optString("night_id")
        if (nightId.isBlank() || current.recommendationExposures.any { it.optString("night_id") == nightId }) {
            return@mutateState current
        }
        val exposure = JSONObject()
            .put("night_id", nightId)
            .put("viewed_at", nowIso())
            .put("recommendation_generated_at", generatedAt)
            .put("publication_id", current.recommendationPublications.lastOrNull()?.id ?: JSONObject.NULL)
            .put("forecast_status", outcomes.optString("forecast_status"))
            .put("recommendation_policy", outcomes.optString("recommendation_policy"))
            .put("recommendation", recommendation.toJson())
            .put("model_version", outcomes.optString("model_version"))
            .put("research_artifacts", outcomes.optJSONObject("research_artifacts") ?: JSONObject())
            .put("interpretation", "추천을 화면에 표시한 노출 기록이며 추천 효과를 뜻하지 않음")
        current.copy(recommendationExposures = (current.recommendationExposures + exposure).takeLast(90))
    }

    private fun compute(value: UserState, awake: Boolean = true): UserState {
        val profile = value.profile ?: return value
        val now = Instant.now()
        val output = UserModelEngine(context).compute(profile, value.records, now,
            value.prospectiveForecasts, value.prospectiveOutcomes)
        val prospective = UserContractRules.shouldFreezeForecast(output.outcomes.optString("forecast_status"))
        val nightId = output.outcomes.optString("night_id")
        val hadFrozenForecast = value.prospectiveForecasts.any { it.optString("night_id") == nightId }
        val history = if (prospective && nightId.isNotBlank() && !hadFrozenForecast) {
            (value.prospectiveForecasts + compactForecast(output)).takeLast(90)
        } else value.prospectiveForecasts

        val effectiveTiming = value.recommendationTiming
            ?.takeIf { it.nightId == nightId } ?: output.recommendationTiming
        val timing = RecommendationScheduleRules.timing(
            LocalDate.parse(effectiveTiming.nightId),
            ZonedDateTime.parse(effectiveTiming.expectedOnsetAt)
        )
        val evaluatedPrefix = "$nightId:"
        val seen = value.evaluatedRecommendationSlots.filter { it.startsWith(evaluatedPrefix) }
            .map { it.removePrefix(evaluatedPrefix) }.toSet()
        val due = RecommendationScheduleRules.dueSlot(now.atZone(ZONE), timing, seen, awake)
        val fingerprint = recommendationFingerprint(profile, value.records, profile.dailyContext)
        var publications = value.recommendationPublications
        var evaluated = value.evaluatedRecommendationSlots
        var publishedRecommendation = publications.lastOrNull { it.nightId == nightId }?.recommendation
        if (due != null) {
            evaluated = (evaluated + "$nightId:${due.slot()}").distinct().takeLast(450)
            val previous = publications.lastOrNull { it.nightId == nightId }
            if (previous == null || previous.inputFingerprint != fingerprint) {
                val revision = (publications.filter { it.nightId == nightId }.maxOfOrNull { it.revision } ?: -1) + 1
                val publication = RecommendationPublication(
                    id = "$nightId:$revision", nightId = nightId, revision = revision,
                    slot = due.slot(), kind = due.kind(), scheduledAt = due.scheduledAt().toOffsetDateTime().toString(),
                    publishedAt = now.toString(), inputFingerprint = fingerprint,
                    recommendation = output.recommendation
                )
                publications = (publications + publication).takeLast(450)
                publishedRecommendation = publication.recommendation
            }
        }
        return value.copy(
            recommendation = publishedRecommendation,
            forecast = if (hadFrozenForecast) value.forecast ?: output.forecast else output.forecast,
            modelOutcomes = if (hadFrozenForecast) value.modelOutcomes ?: JSONObject(output.outcomes.toString()) else JSONObject(output.outcomes.toString()),
            prospectiveForecasts = history,
            recommendationTiming = effectiveTiming,
            recommendationPublications = publications,
            evaluatedRecommendationSlots = evaluated
        )
    }

    private fun recommendationFingerprint(
        profile: UserProfile,
        records: List<SleepRecord>,
        context: DailyContext?
    ): String {
        val primary = records.filter { it.source == "manual" }
            .sortedWith(compareBy<SleepRecord> { it.wakeTime }.thenBy { it.id })
        val payload = JSONObject()
            .put("schema", 1)
            .put("profile", profile.toJson())
            .put("records", org.json.JSONArray().also { array -> primary.forEach { array.put(it.toJson()) } })
            .put("daily_context", context?.toJson() ?: JSONObject.NULL)
            .put("policy", "wake_constraint_plus_sleep_opportunity_v3_daily_personalization")
            .toString().toByteArray(Charsets.UTF_8)
        return MessageDigest.getInstance("SHA-256").digest(payload).joinToString("") { "%02x".format(it) }
    }

    private fun compactForecast(output: EngineOutput): JSONObject = JSONObject()
        .put("night_id", output.outcomes.getString("night_id"))
        .put("forecast_status", output.outcomes.getString("forecast_status"))
        .put("cutoff", output.outcomes.getString("cutoff"))
        .put("generated_at", output.forecast.generatedAt)
        .put("model_version", output.outcomes.optString("model_version"))
        .put("model_feature_contract", output.outcomes.optString("model_feature_contract"))
        .put("research_predictions", output.outcomes.optJSONObject("research_predictions") ?: JSONObject())
        .put("research_artifacts", output.outcomes.optJSONObject("research_artifacts") ?: JSONObject())
        .put("research_feature_snapshot", output.outcomes.optJSONObject("research_feature_snapshot") ?: JSONObject())
        .put("adaptive_research_predictions", output.outcomes.optJSONObject("adaptive_research_predictions") ?: JSONObject())
        .put("research_baselines", output.outcomes.optJSONObject("research_baselines") ?: JSONObject())
        .put("recommendation_policy", output.outcomes.optString("recommendation_policy"))
        .put("recommendation", output.recommendation.toJson())

    private fun freezeFirstOutcome(current: UserState, record: SleepRecord): List<JSONObject> {
        val forecast = current.prospectiveForecasts.firstOrNull { snapshot ->
            runCatching { LocalDate.parse(snapshot.getString("night_id")).plusDays(1).toString() == record.day }.getOrDefault(false)
        } ?: return current.prospectiveOutcomes
        val nightId = forecast.optString("night_id")
        if (!UserContractRules.shouldFreezeFirstManualReport(
                current.prospectiveOutcomes.any { it.optString("night_id") == nightId })) return current.prospectiveOutcomes
        val cutoff = runCatching { Instant.parse(forecast.getString("cutoff")) }.getOrNull() ?: return current.prospectiveOutcomes
        val generated = runCatching { Instant.parse(forecast.getString("generated_at")) }.getOrNull() ?: return current.prospectiveOutcomes
        val bed = runCatching { Instant.parse(record.bedtime) }.getOrNull() ?: return current.prospectiveOutcomes
        val wake = runCatching { Instant.parse(record.wakeTime) }.getOrNull() ?: return current.prospectiveOutcomes
        val observed = runCatching { Instant.parse(record.modifiedAt) }.getOrNull() ?: return current.prospectiveOutcomes
        val onsetOffset = ChronoUnit.MINUTES.between(cutoff, bed).toDouble()
        val wakeOffset = ChronoUnit.MINUTES.between(cutoff, wake).toDouble()
        val exclusion = when {
            !bed.isAfter(generated) -> "첫 자기보고의 잠든 시각이 예측 생성 이후가 아니므로 제외"
            wake.isAfter(observed) -> "첫 자기보고의 기상 시각이 보고 시각 이후이므로 제외"
            onsetOffset !in 0.0..960.0 -> "첫 자기보고의 잠든 시각이 사전 지정 예측 범위 밖이므로 제외"
            wakeOffset !in 30.0..1440.0 -> "첫 자기보고의 기상 시각이 사전 지정 예측 범위 밖이므로 제외"
            record.durationMinutes !in 30..960 -> "첫 자기보고의 수면 구간이 사전 지정 범위 밖이므로 제외"
            else -> null
        }
        val frozenBase = JSONObject()
            .put("night_id", nightId)
            .put("record_id", record.id)
            .put("actual_source", "manual")
            .put("target", "first_self_reported_sleep_onset_and_final_wake")
            .put("available_at", record.modifiedAt)
            .put("outcome_at", record.wakeTime)
            .put("source_model_version", forecast.optString("model_version"))
            .put("source_model_feature_contract", forecast.optString("model_feature_contract"))
            .put("source_research_artifacts", forecast.optJSONObject("research_artifacts") ?: JSONObject())
            .put("source_research_baselines", forecast.optJSONObject("research_baselines") ?: JSONObject())
            .put("rule", "first manual report after frozen forecast is immutable, including exclusions; later edits cannot substitute")
        if (exclusion != null) {
            return (current.prospectiveOutcomes + frozenBase.put("status", "excluded").put("exclusion_reason", exclusion)).takeLast(90)
        }
        val predictions = forecast.optJSONObject("research_predictions") ?: JSONObject()
        val residuals = JSONObject()
        predictions.keys().forEach { key ->
            val prediction = predictions.optJSONObject(key) ?: return@forEach
            residuals.put(key, JSONObject()
                .put("onset_residual_min", onsetOffset - prediction.getDouble("onset_offset_min"))
                .put("wake_residual_min", wakeOffset - prediction.getDouble("wake_offset_min")))
        }
        val baselineResiduals = JSONObject()
        val baselines = forecast.optJSONObject("research_baselines")?.optJSONObject("baselines") ?: JSONObject()
        baselines.keys().forEach { key ->
            val prediction = baselines.optJSONObject(key) ?: return@forEach
            if (!prediction.optBoolean("available") || prediction.isNull("onset_offset_min") || prediction.isNull("wake_offset_min")) {
                return@forEach
            }
            baselineResiduals.put(key, JSONObject()
                .put("onset_residual_min", onsetOffset - prediction.getDouble("onset_offset_min"))
                .put("wake_residual_min", wakeOffset - prediction.getDouble("wake_offset_min")))
        }
        val frozen = frozenBase
            .put("status", "matched")
            .put("actual_onset_offset_min", onsetOffset)
            .put("actual_wake_offset_min", wakeOffset)
            .put("residuals", residuals)
            .put("research_baseline_residuals", baselineResiduals)
        return (current.prospectiveOutcomes + frozen).takeLast(90)
    }

    private fun merge(old: List<SleepRecord>, incoming: List<SleepRecord>): List<SleepRecord> =
        (old + incoming).associateBy { it.id }.values.sortedByDescending { it.wakeTime }.take(500)

    private fun parseTime(value: String): LocalTime = LocalTime.parse(value, DateTimeFormatter.ofPattern("HH:mm"))

    companion object { val ZONE: ZoneId = ZoneId.of("Asia/Seoul") }
}
