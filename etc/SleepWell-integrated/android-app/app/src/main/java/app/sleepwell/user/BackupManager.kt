package app.sleepwell.user

import android.content.Context
import app.sleepwell.sync.HttpStatusException
import app.sleepwell.sync.PinnedHttpsClient
import app.sleepwell.sync.SyncRepository
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.temporal.ChronoUnit

class BackupManager(private val context: Context) {
    private val store = UserSecureStore(context)
    private val users = UserRepository(context)

    fun createTodaySnapshot(): PendingBackup {
        val state = users.refresh()
        val profile = state.profile ?: error("먼저 프로필을 저장하세요.")
        val now = Instant.now()
        val day = now.atZone(UserRepository.ZONE).toLocalDate().toString()
        val cutoff = LocalDate.parse(day).minusDays(89)
        val records = state.records.filter { runCatching { !LocalDate.parse(it.day).isBefore(cutoff) }.getOrDefault(false) }.take(500)
        val item = store.createPending(day) { revision -> JSONObject().put("schema_version", 1).put("day", day).put("revision", revision)
            .put("generated_at", now.toString()).put("participant_key", state.participantKey)
            .put("data", JSONObject().put("profile", profile.toJson())
                .put("records", JSONArray().also { a -> records.forEach { a.put(it.toJson()) } })
                .put("recommendation", state.recommendation?.toJson() ?: JSONObject.NULL)
                .put("forecast", state.forecast?.toJson() ?: JSONObject.NULL)
                .put("outcomes", outcomes(state, records, now))).also { payload ->
                    require(payload.toString().toByteArray().size <= PinnedHttpsClient.MAX_BODY_BYTES) { "백업 자료가 1MB를 넘습니다." }
                }.toString() }
        users.updateMetadata { it.copy(lastLocalBackup = now.toString(), status = "오늘의 암호화 백업을 휴대폰에 저장했습니다.") }
        return item
    }

    fun sendPending(): Int {
        val repository = SyncRepository(context)
        val queue = store.pending()
        var sent = 0
        for (item in queue.toList()) {
            try {
                val ack = repository.sendBackup(JSONObject(item.payload))
                store.acknowledge(ack.day, ack.revision)
                sent++
            } catch (e: HttpStatusException) {
                // 409는 다른 내용의 같은 revision이므로 자동 폐기하거나 덮어쓰지 않는다.
                if (e.statusCode == 409) throw IllegalStateException("PC에 같은 날짜·revision의 다른 백업이 있어 대기 자료를 보존했습니다.", e)
                throw e
            }
        }
        if (sent > 0) {
            users.updateMetadata { it.copy(lastPcDelivery = nowIso(), status = "PC에 백업 ${sent}건을 전달했습니다. Windows 화면을 새로고침하세요.") }
        }
        return sent
    }

    fun pendingCount(): Int = store.pending().size

    private fun outcomes(state: UserState, records: List<SleepRecord>, now: Instant): JSONObject {
        fun summary(rows: List<SleepRecord>, subjective: Boolean) = JSONObject()
            .put("record_count", rows.size)
            .put("mean_duration_minutes", rows.takeIf { it.isNotEmpty() }?.map { it.durationMinutes }?.average() ?: JSONObject.NULL)
            .put("mean_rested", if (subjective) rows.mapNotNull { it.rested }.takeIf { it.isNotEmpty() }?.average() ?: JSONObject.NULL else JSONObject.NULL)
            .put("mean_fatigue", if (subjective) rows.mapNotNull { it.fatigue }.takeIf { it.isNotEmpty() }?.average() ?: JSONObject.NULL else JSONObject.NULL)
        val enrolled = state.profile?.createdAt?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: Instant.EPOCH
        val eligible = records.filter { record -> runCatching {
            val wake = Instant.parse(record.wakeTime)
            !wake.isBefore(enrolled) && !wake.isAfter(now)
        }.getOrDefault(false) }
        val diary = eligible.filter { it.source == "manual" }.take(14)
        val wearable = eligible.filter { it.source == "samsung_health" }.take(14)
        return JSONObject().put("record_count", records.size)
            .put("summary_window", "most_recent_14_records_since_enrollment_through_snapshot")
            .put("measurement_scales", JSONObject().put("rested", "1=전혀 회복되지 않음, 5=매우 회복됨")
                .put("fatigue", "1=피로 없음, 5=매우 피로함"))
            .put("primary_diary", summary(diary, true))
            .put("wearable_auxiliary", summary(wearable, false))
            .put("current_model", state.modelOutcomes ?: JSONObject.NULL)
            .put("prospective_forecasts", JSONArray().also { a -> state.prospectiveForecasts.forEach { a.put(it) } })
            .put("recommendation_exposures", JSONArray().also { a -> state.recommendationExposures.forEach { a.put(it) } })
            .put("prospective_outcomes", JSONArray().also { a -> state.prospectiveOutcomes.forEach { a.put(it) } })
            .put("daily_context_versions", JSONArray().also { a -> state.dailyContexts.forEach { a.put(it.toJson()) } })
            .put("recommendation_timing", state.recommendationTiming?.toJson() ?: JSONObject.NULL)
            .put("recommendation_publications", JSONArray().also { a -> state.recommendationPublications.forEach { a.put(it.toJson()) } })
            .put("evaluated_recommendation_slots", JSONArray().also { a -> state.evaluatedRecommendationSlots.forEach { a.put(it) } })
            .put("realized_comparisons", realizedComparisons(state, records, now))
    }

    private fun realizedComparisons(state: UserState, records: List<SleepRecord>, observedAt: Instant): JSONArray {
        val byDay = records.groupBy { it.day }
        val outcomes = state.prospectiveOutcomes.associateBy { it.optString("night_id") }
        val exposures = state.recommendationExposures.associateBy { it.optString("night_id") }
        return JSONArray().also { out -> state.prospectiveForecasts.forEach { snapshot ->
            val night = runCatching { LocalDate.parse(snapshot.getString("night_id")) }.getOrNull() ?: return@forEach
            val sameDay = byDay[night.plusDays(1).toString()].orEmpty()
            val cutoff = night.atTime(20, 0).atZone(UserRepository.ZONE).toInstant()
            fun eligible(record: SleepRecord): Boolean = runCatching {
                val bed = Instant.parse(record.bedtime)
                val wake = Instant.parse(record.wakeTime)
                val onsetOffset = ChronoUnit.MINUTES.between(cutoff, bed)
                val wakeOffset = ChronoUnit.MINUTES.between(cutoff, wake)
                !wake.isAfter(observedAt) &&
                    onsetOffset in 0..960 && wakeOffset in 30..1440 && record.durationMinutes in 30..960
            }.getOrDefault(false)
            val actual = outcomes[night.toString()]
            if (actual == null) {
                out.put(JSONObject().put("night_id", night.toString()).put("status", "excluded")
                    .put("reason", "예측 저장 뒤 동결된 첫 유효 자기보고 결과가 없습니다."))
                return@forEach
            }
            if (actual.optString("status") != "matched") {
                out.put(JSONObject().put("night_id", night.toString()).put("status", "excluded")
                    .put("record_id", actual.optString("record_id"))
                    .put("actual_available_at", actual.optString("available_at"))
                    .put("reason", actual.optString("exclusion_reason", "첫 자기보고 결과가 사전 지정 기준을 충족하지 않음"))
                    .put("later_edits_may_substitute", false))
                return@forEach
            }
            val wearable = sameDay.filter { it.source == "samsung_health" && eligible(it) }.maxByOrNull { it.modifiedAt }
            val actualBed = actual.getDouble("actual_onset_offset_min")
            val actualWake = actual.getDouble("actual_wake_offset_min")
            val exposure = exposures[night.toString()]
            val adherence = exposure?.optJSONObject("recommendation")?.let { recommendation ->
                runCatching {
                    val recommendedBed = Instant.parse(recommendation.getString("bedtime"))
                    val recommendedWake = Instant.parse(recommendation.getString("wake_time"))
                    JSONObject()
                        .put("bedtime_absolute_difference_min", kotlin.math.abs(actualBed - ChronoUnit.MINUTES.between(cutoff, recommendedBed)))
                        .put("wake_absolute_difference_min", kotlin.math.abs(actualWake - ChronoUnit.MINUTES.between(cutoff, recommendedWake)))
                        .put("interpretation", "화면에 표시된 계획과 첫 자기보고 결과의 기술적 차이이며 효과나 순응의 인과 추정이 아님")
                }.getOrNull()
            }
            out.put(JSONObject().put("night_id", night.toString()).put("status", "matched").put("record_id", actual.optString("record_id"))
                .put("actual_source", "manual")
                .put("target", "first_self_reported_sleep_onset_and_final_wake")
                .put("actual_available_at", actual.optString("available_at"))
                .put("actual_onset_offset_min", actualBed).put("actual_wake_offset_min", actualWake)
                .put("recommendation_exposure", exposure ?: JSONObject.NULL)
                .put("recommendation_adherence_descriptive", adherence ?: JSONObject.NULL)
                .put("wearable_auxiliary", wearable?.let { w -> JSONObject()
                    .put("record_id", w.id)
                    .put("actual_onset_offset_min", ChronoUnit.MINUTES.between(cutoff, Instant.parse(w.bedtime)).toDouble())
                    .put("actual_wake_offset_min", ChronoUnit.MINUTES.between(cutoff, Instant.parse(w.wakeTime)).toDouble())
                } ?: JSONObject.NULL)
                .put("research_predictions", snapshot.optJSONObject("research_predictions") ?: JSONObject.NULL)
                .put("adaptive_research_predictions", snapshot.optJSONObject("adaptive_research_predictions") ?: JSONObject.NULL)
                .put("research_baselines", snapshot.optJSONObject("research_baselines") ?: JSONObject.NULL)
                .put("research_baseline_residuals", actual.optJSONObject("research_baseline_residuals") ?: JSONObject.NULL))
        } }
    }

    companion object {
    }
}
