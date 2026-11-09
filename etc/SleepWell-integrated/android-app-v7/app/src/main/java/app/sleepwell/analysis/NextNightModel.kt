package app.sleepwell.analysis

import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.LimitCode
import app.sleepwell.health.Recommendation
import app.sleepwell.user.DailyContext
import app.sleepwell.user.MeasurementRecommendationPolicy
import app.sleepwell.user.PortableFusionModel
import app.sleepwell.user.SleepInputPolicy
import app.sleepwell.user.UserModelEngine
import app.sleepwell.user.UserProfile
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import app.sleepwell.health.SleepRecord as WatchRecord
import app.sleepwell.user.SleepRecord as NightRecord

/*
 * 예상·권장 시각 = 서윤 새 모델(robust-next-night-research-v2) 연결 (소빈 · 2026-10-31, 통합 v6)
 *
 * 앱이 하는 일 두 가지 중
 *   1. 수면 분석(수면 압력·일주기 그래프) → 지금 그대로 (채윤 파이프라인, 1분 칸)
 *   2. 예상·권장 시각                    → 이 파일이 서윤 엔진(app.sleepwell.user, repo 파일 그대로)을 부른다
 *
 * 흐름: 파이프라인 결과(1분 칸 분석 + 세션 목록) + 사용자가 저장한 일기 → 엔진 → Recommendation 칸만 바꿔 끼움
 * 그래서 화면(AnalysisDisplay·ScheduleCard)은 지금처럼 result.recommendation 을 읽으면 된다.
 *
 * 기준 시각: 고른 날짜(그날 아침 기상 기준, A안)의 **저녁 8시** — 엔진 규칙("20시에 그날 밤 예측").
 *   엔진은 8시까지 끝난 기록만 쓰므로 지난 날짜를 골라도 그 뒤 기록이 섞이지 않는다.
 *
 * 최신 measurement-evidence 계약에서는 워치 기록을 일기로 재분류하지 않는다. 워치는 samsung_health 보조 관측,
 * 사용자가 직접 저장한 수면일지만 manual/diary primary 이다. 합성 시연은 demoMode 안에서만 명시적으로 demo: 일기를 만든다.
 */
object NextNightModel {

    /** 같은 밤이 여러 조각으로 기록되면 사이 간격이 이 안일 때 한 밤으로 합친다 (서윤 Samsung 전처리 "2시간 이내 세션 병합") */
    const val MERGE_GAP_MIN = 120L

    /** 주 수면으로 볼 최소 길이 (A안 기상일 규칙과 같음: 3시간 이상) — 낮잠이 "밤"으로 들어가지 않게 */
    const val MAIN_SLEEP_MIN = 180L

    /** 프로필 입력 화면을 만들기 전 임시값 (화면 수정 목록 #3). 나이대 18-29 / 30-44 / 45-59 / 60-80 */
    data class ModelProfile(
        val ageBand: String = "18-29",
        val sex: String = "any",
        val activityMinutes: Int = 60,
        val desiredSleepMinutes: Int = 480,
        val usualBed: String = "23:30",
        val usualWake: String = "07:30",
        val occupation: String = "",
        val onboardingCompleted: Boolean = false
    )

    private val SEOUL: ZoneId = ZoneId.of("Asia/Seoul")

    /**
     * 그동안 쌓인 설문 (지금 데이터의 참여자 것만: 실제 = "me", 합성 = "Test"). 화면이 계산 전에 넣어 준다.
     * 그날 설문의 피로도·개운함을 그날 아침에 끝난 잠(일기)에 붙인다 — 개운함 = 그날 아침 일어났을 때, 피로도 = 그 잠을 자고 보낸 하루의 피로.
     */
    @Volatile var surveyDays: List<SurveyDay> = emptyList()
    /** 사용자가 직접 저장한 일기. UI가 [DiaryStore]에서 불러와 설정한다. */
    @Volatile var diaryRecords: List<NightRecord> = emptyList()
    /** 실제 import 시각과 출처를 보존한 웨어러블 밤. [ImportedSleepStore]에서 불러온다. */
    @Volatile var importedRecords: List<NightRecord> = emptyList()
    /** 명시적 서비스 정책 opt-in. false면 기존 diary-only 엔진 계약을 그대로 유지한다. */
    @Volatile var wearablePersonalizationEnabled: Boolean = false
    /** 합성 asset만 워치를 일기처럼 시연하기 위한 격리 스위치. 실제 기록과 동시에 쓰지 않는다. */
    @Volatile var demoMode: Boolean = false

    /**
     * 피로도 1~7(앱·혜지 설문, 1 = 매우 맑음 · 7 = 매우 피곤함) → 엔진 1~5 (10/31 소빈 결정).
     * 양 끝을 맞춰 늘인 값 1 + (x−1)×4/6 을 반올림 → 엔진 "잘 잔 밤" 기준(1~5에서 3 이하) = 1~7에서 4(보통) 이하.
     * 기준값 자체는 정답이 있는 값이 아니라 정한 규칙 (서윤 문서도 척도별 기준이 서로 보정되지 않았다고 명시) → 회의에서 공유
     */
    fun fatigueForEngine(x: Int?): Int? = when (x) { 1 -> 1; 2 -> 2; 3 -> 2; 4 -> 3; 5 -> 4; 6 -> 4; 7 -> 5; else -> null }
    private var engine: UserModelEngine? = null
    var profile: ModelProfile = ModelProfile()
    /** 마지막 계산에서 엔진이 실패한 이유 (디버그용) */
    var lastError: String? = null
        private set

    /** 앱 시작 때 한 번: assets/models/ 를 읽는 함수만 넘긴다 (MainActivity). 테스트에서는 파일 읽기로 넘긴다 */
    fun install(assetLoader: (String) -> ByteArray) {
        engine = UserModelEngine(assetLoader)
    }

    val isInstalled: Boolean get() = engine != null

    /** 파이프라인 결과의 예상·권장(옛 HybridRecommendationEngine 값)을 새 엔진 값으로 바꾼다 */
    fun applyTo(
        result: AnalysisResult,
        direct: DirectInput?,
        features: Map<String, Double?>,
        nowMs: Long? = null,
        contextObservedAtMs: Long? = null,
        historicalReconstruction: Boolean = false,
        includeImportedRecords: Boolean = wearablePersonalizationEnabled
    ): AnalysisResult {
        val eng = engine ?: return result
        val refMs = result.inputPeriodEndMs.takeIf { it > 0 } ?: return result
        val decisionTime = nowMs?.let { Instant.ofEpochMilli(it).atZone(SEOUL) }
            ?: Instant.ofEpochMilli(refMs).atZone(SEOUL).toLocalDate().atTime(20, 0).atZone(SEOUL)
        val cutoff = decisionTime.toLocalDate().atTime(20, 0).atZone(SEOUL)
        return try {
            val nights = nightlyRecords(
                result.sleepRecords, direct, observedAt = decisionTime.toInstant(),
                includeImportedRecords = includeImportedRecords
            )
            // Transient pipeline watch rows use wake time as availability and therefore stay auxiliary.
            // Service personalization may consume only persisted imports with an actual import/update timestamp.
            val engineNights = if (includeImportedRecords) nights.filterNot { it.id.startsWith("watch:") } else nights
            val availableAt = if (decisionTime.isBefore(cutoff)) decisionTime else cutoff
            val contextObservedAt = contextObservedAtMs?.let { Instant.ofEpochMilli(it).atZone(SEOUL) } ?: decisionTime
            val up = userProfile(engineNights, availableAt).copy(dailyContext = dailyContext(direct, features, contextObservedAt))
            val out = eng.compute(
                up, engineNights, decisionTime.toInstant(), historicalEvaluation = historicalReconstruction,
                inputPolicy = if (includeImportedRecords) SleepInputPolicy.DIARY_AND_WEARABLE_V1 else SleepInputPolicy.DIARY_ONLY
            )
            lastError = null
            val p = out.outcomes.getJSONObject("research_predictions")
            val personalization = out.outcomes.optJSONObject("personalization")
            val evidenceStatus = personalization?.optJSONObject("evidence_quality")?.optString("status")
            val runtimeModelVersion = out.outcomes.optString("model_version", PortableFusionModel.MODEL_VERSION)
            val runtimePolicy = out.outcomes.optString("runtime_personalization_policy").takeIf { it.isNotBlank() }
            val r = out.recommendation
            val rec = Recommendation(
                recommendedBedTimeMs = ms(r.bedtime),
                recommendedWakeTimeMs = ms(r.wakeTime),
                recommendedSleepMin = r.durationMinutes.toDouble(),
                physicalBedTimeMs = avg(p, "physical", "bed"),
                physicalWakeTimeMs = avg(p, "physical", "wake"),
                predictedBedTimeMs = avg(p, "hybrid", "bed"),
                predictedWakeTimeMs = avg(p, "hybrid", "wake"),
                residualModelVersion = runtimeModelVersion,
                modelStatus = "fusion_cmi_mesa",
                usedFallback = evidenceStatus != "adequate",
                explanation = r.explanation,
                planFeasible = r.planFeasible,
                generatedAtMs = decisionTime.toInstant().toEpochMilli(),
                personalizationStatus = personalization?.optString("parameter_version")?.takeIf { it.isNotBlank() },
                personalizationWeight = personalization?.takeIf { it.has("weight") && !it.isNull("weight") }?.optDouble("weight"),
                evidenceStatus = evidenceStatus,
                runtimePolicy = runtimePolicy
            )
            result.copy(
                recommendation = rec,
                modelVersion = "분석: Two-Process · 예측·권장: $runtimeModelVersion" +
                    (runtimePolicy?.let { " · 개인화: $it" } ?: ""),
                // 옛 엔진 기준 제한 코드는 새 엔진에 해당하지 않아 뺀다 (Ridge 학습 전 · 14일 단계 · 목표 기상 없으면 권장 없음).
                // 새 엔진 상황에 맞는 안내 문구는 화면 수정 때 (화면 수정 목록)
                predictionLimitations = result.predictionLimitations.filter {
                    it != LimitCode.RIDGE_UNTRAINED && it != LimitCode.FEW_RECORDS && it != LimitCode.NO_TARGET_WAKE
                }
            )
        } catch (e: Exception) {
            lastError = e.message ?: e::class.java.simpleName
            // 옛 엔진 값(새벽 5시 쏠림)을 보여 주지 않도록 비운다 → 화면은 "계산하지 못했어요"
            result.copy(recommendation = null)
        }
    }

    /**
     * 워치 세션과 명시적 일기 → 밤 단위 엔진 입력. 테스트용으로 공개.
     *  1. 간격 MERGE_GAP_MIN 안의 조각을 한 밤으로 합침
     *  2. 기상 날짜(한국 시간)마다 가장 긴 것 하나, MAIN_SLEEP_MIN 이상만 (낮잠 제외)
     *  3. 실제 모드의 워치는 samsung_health 보조 관측으로 유지한다.
     *  4. 저장된 manual/diary는 실제 modifiedAt을 보존하고 같은 기상일의 워치보다 우선한다.
     *  5. 직접 입력 fallback은 그 입력을 처리한 실제 시각 [observedAt]을 modifiedAt으로 사용한다.
     */
    fun nightlyRecords(
        records: List<WatchRecord>,
        direct: DirectInput? = null,
        surveys: List<SurveyDay> = surveyDays,
        observedAt: Instant = Instant.now(),
        includeImportedRecords: Boolean = false
    ): List<NightRecord> {
        val fatigueByDate = surveys.mapNotNull { sv -> fatigueForEngine(sv.fatigue)?.let { runCatching { LocalDate.parse(sv.date) }.getOrNull()?.to(it) } }.toMap()
        val restedByDate = surveys.mapNotNull { sv -> sv.rested?.takeIf { it in 1..5 }?.let { runCatching { LocalDate.parse(sv.date) }.getOrNull()?.to(it) } }.toMap()
        val merged = mutableListOf<LongArray>()
        records.sortedBy { it.startMs }.forEach { r ->
            val last = merged.lastOrNull()
            if (last != null && r.startMs - last[1] <= MERGE_GAP_MIN * 60_000L) last[1] = maxOf(last[1], r.endMs)
            else merged += longArrayOf(r.startMs, r.endMs)
        }
        val source = if (demoMode) "manual" else "samsung_health"
        val idPrefix = if (demoMode) "demo" else "watch"
        // Service personalization must use only persisted imports with a real receipt timestamp.
        // The raw analysis rows have no such timestamp and must remain auxiliary graph input.
        val byWake: MutableMap<LocalDate, NightRecord> = if (!demoMode && includeImportedRecords) {
            mutableMapOf()
        } else {
            merged
                .filter { (it[1] - it[0]) >= MAIN_SLEEP_MIN * 60_000L }
                .groupBy { wakeDate(it[1]) }
                .mapValues { (_, v) -> v.maxBy { it[1] - it[0] } }
                .mapValues { (day, v) -> night(
                    "$idPrefix:$day", v[0], v[1], source,
                    if (demoMode) fatigueByDate[day] else null,
                    if (demoMode) restedByDate[day] else null,
                    Instant.ofEpochMilli(v[1])
                ) }
                .toMutableMap()
        }
        if (!demoMode) {
            if (includeImportedRecords) {
                importedRecords.asSequence()
                    .filter { it.source in setOf("samsung_health", "health_connect", "samsung_health_csv") }
                    .filter { recordAvailableAt(it, observedAt) }
                    .mapNotNull { record ->
                        runCatching { Instant.parse(record.wakeTime).atZone(SEOUL).toLocalDate() to record }.getOrNull()
                    }
                    .groupBy({ it.first }, { it.second })
                    .mapValues { (_, values) -> values.maxWithOrNull(compareBy<NightRecord>({ it.durationMinutes }, { Instant.parse(it.modifiedAt) }))!! }
                    .forEach { (day, record) -> byWake[day] = record }
            }
            diaryRecords.asSequence()
                .filter { it.source == "manual" || it.source == "diary" }
                .filter { recordAvailableAt(it, observedAt) }
                .mapNotNull { record ->
                    runCatching { Instant.parse(record.wakeTime).atZone(SEOUL).toLocalDate() to record }.getOrNull()
                }
                .groupBy({ it.first }, { it.second })
                .mapValues { (_, values) -> values.maxWithOrNull(compareBy<NightRecord>({ Instant.parse(it.modifiedAt) }, { it.id }))!! }
                .forEach { (day, record) -> byWake[day] = record }
        }
        val ds = direct?.sleepStartMs
        val de = direct?.sleepEndMs
        if (ds != null && de != null && de > ds) {
            val day = wakeDate(de)
            val existing = byWake[day]
            if (demoMode || existing == null || existing.source !in setOf("manual", "diary")) {
                byWake[day] = night(
                    if (demoMode) "demo:input:$day" else "diary:fallback:$day",
                    ds, de, "manual", fatigueByDate[day], restedByDate[day], observedAt
                )
            }
        }
        return byWake.values.sortedBy { it.wakeTime }
    }

    private fun night(
        id: String, startMs: Long, endMs: Long, source: String,
        fatigue: Int? = null, rested: Int? = null, observedAt: Instant
    ) = NightRecord(
        id = id,
        day = Instant.ofEpochMilli(startMs).atZone(SEOUL).toLocalDate().toString(),
        bedtime = Instant.ofEpochMilli(startMs).toString(),
        wakeTime = Instant.ofEpochMilli(endMs).toString(),
        durationMinutes = ((endMs - startMs) / 60_000L).toInt(),
        rested = rested,   // 그날 설문의 아침 개운함 1~5. 개운함 4 이상 + 피로 3 이하(1~5) = "잘 잔 밤" → 5번 넘게 쌓이면 목표 수면시간 조정
        fatigue = fatigue,
        source = source,
        modifiedAt = observedAt.toString()
    )

    private fun wakeDate(ms: Long): LocalDate = Instant.ofEpochMilli(ms).atZone(SEOUL).toLocalDate()

    private fun recordAvailableAt(record: NightRecord, boundary: Instant): Boolean = runCatching {
        !Instant.parse(record.modifiedAt).isAfter(boundary) && !Instant.parse(record.wakeTime).isAfter(boundary)
    }.getOrDefault(false)

    /**
     * 엔진 프로필. 평소 취침·기상과 목표 수면시간은 사용자가 선언한 프로필을 유지한다.
     * 워치 관측으로 선언값을 조용히 덮어쓰지 않는다.
     * 가입일 = 첫 기록 하루 전 → 엔진이 가입일 이전 기록을 버리므로, 불러온 과거 기록도 모두 쓰이게.
     */
    fun userProfile(nights: List<NightRecord>, cutoff: ZonedDateTime): UserProfile {
        val first = nights.minOfOrNull { Instant.parse(it.bedtime) } ?: cutoff.toInstant()
        return UserProfile(
            ageBand = profile.ageBand, sex = profile.sex, activityMinutes = profile.activityMinutes,
            usualBed = profile.usualBed,
            usualWake = profile.usualWake,
            desiredSleepMinutes = profile.desiredSleepMinutes,
            createdAt = first.minusSeconds(86_400).toString(),
            occupation = profile.occupation
        )
    }

    /** Canonical live-v7 night association. The frozen research forecast keeps its separate 20:00 cutoff. */
    internal fun liveNightId(now: ZonedDateTime, usualWake: String = profile.usualWake): LocalDate =
        MeasurementRecommendationPolicy.nightId(now.withZoneSameInstant(SEOUL), java.time.LocalTime.parse(usualWake))

    /**
     * 설문 → 엔진 생활 맥락. 첫 일정과 완료된 낮잠을 live-v7의 밤 ID에 연결한다.
     * 실시간 입력은 실제 관측 시각을 보존하며 research 20:00 cutoff를 대신하지 않는다.
     */
    internal fun dailyContext(direct: DirectInput?, f: Map<String, Double?>, observedAt: ZonedDateTime): DailyContext? {
        val alarm = direct?.targetWakeMs
        val napEnd = f["napEndMs"]?.toLong()
        val napMin = f["napMinutes"]
        val caf = f["lastCaffeineMs"]?.toLong()
        val cafMg = f["caffeineMg"]
        if (alarm == null && napEnd == null && caf == null) return null
        return DailyContext(
            nightId = liveNightId(observedAt).toString(),
            kind = "first",
            firstStart = alarm?.let { Instant.ofEpochMilli(it).toString() },
            preparationMinutes = 0,
            napStart = if (napEnd != null && napMin != null && napMin > 0) Instant.ofEpochMilli(napEnd - (napMin * 60_000).toLong()).toString() else null,
            napEnd = if (napEnd != null && napMin != null && napMin > 0) Instant.ofEpochMilli(napEnd).toString() else null,
            caffeineAt = caf?.let { Instant.ofEpochMilli(it).toString() },
            caffeineMg = if (caf != null) cafMg?.toInt() else null,
            observedAt = observedAt.toInstant().toString()
        )
    }

    private fun ms(iso: String): Long = OffsetDateTime.parse(iso).toInstant().toEpochMilli()

    /** CMI·MESA 두 모델 평균 (엔진의 예상 취침 = 두 융합 모델 평균과 같은 방식) */
    private fun avg(p: JSONObject, arm: String, key: String): Long? = try {
        (ms(p.getJSONObject("${arm}_cmi").getString(key)) + ms(p.getJSONObject("${arm}_mesa").getString(key))) / 2
    } catch (e: Exception) { null }
}
