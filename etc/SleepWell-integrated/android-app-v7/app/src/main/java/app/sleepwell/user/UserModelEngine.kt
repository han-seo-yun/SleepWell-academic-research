package app.sleepwell.user

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.roundToInt
import kotlin.math.roundToLong
import kotlin.math.sin
import kotlin.math.sqrt

data class EngineOutput(
    val recommendation: Recommendation,
    val forecast: Forecast,
    val outcomes: JSONObject
)

/**
 * Offline user-app model entry point.
 *
 * Research forecasts estimate likely behaviour.  The recommendation is a
 * conservative sleep opportunity anchored to the user's wake constraint and
 * target duration.  Wearable timings are supporting observations only.
 */
class UserModelEngine private constructor(
    private val fusionFactory: () -> PortableFusionModel,
    private val populationLoader: () -> ByteArray
) {
    constructor(context: Context) : this(
        fusionFactory = { PortableFusionModel(context) },
        populationLoader = { context.assets.open("models/population_prior.json").use { it.readBytes() } }
    )

    internal constructor(assetLoader: (String) -> ByteArray, testable: Boolean = true) : this(
        fusionFactory = { PortableFusionModel(assetLoader, testable) },
        populationLoader = { assetLoader("models/population_prior.json") }
    )
    companion object {
        private val SEOUL = ZoneId.of("Asia/Seoul")
        private const val POPULATION_HASH = "5cd8cc62078ee0a1b44d029513090e82588b5e71c46e6bd98dac54220ef7d396"
        private const val PERSONALIZATION_POLICY_VERSION = "personalization-v5-measurement-evidence"
        private const val PERSONALIZATION_MINIMUM_HISTORY = 1
        private const val RECOMMENDATION_ENGINE_VERSION = "sleep-opportunity-android-v7-measurement-evidence"
        private const val RECOMMENDATION_POLICY_VERSION = "android_v7_supported_subset_measurement_two_process_v1"
    }

    private val fusion by lazy(fusionFactory)

    fun compute(
        profile: UserProfile,
        records: List<SleepRecord>,
        now: Instant = Instant.now(),
        prospectiveForecasts: List<JSONObject> = emptyList(),
        prospectiveOutcomes: List<JSONObject> = emptyList(),
        historicalEvaluation: Boolean = false,
        inputPolicy: SleepInputPolicy = SleepInputPolicy.DIARY_ONLY
    ): EngineOutput {
        val localNow = now.atZone(SEOUL)
        val decisionToday = localNow.toLocalDate().atTime(20, 0).atZone(SEOUL)
        val preview = localNow.isBefore(decisionToday)
        val cutoff = decisionToday
        val forecastBoundary = if (preview) now else cutoff.toInstant()
        fun availableAtOrBefore(boundary: Instant, record: SleepRecord): Boolean =
            runCatching {
                !Instant.parse(record.wakeTime).isAfter(boundary) &&
                    !Instant.parse(record.modifiedAt).isAfter(boundary)
            }.getOrDefault(false)
        fun availableBefore(boundary: Instant, record: SleepRecord): Boolean =
            runCatching {
                Instant.parse(record.wakeTime).isBefore(boundary) &&
                    Instant.parse(record.modifiedAt).isBefore(boundary)
            }.getOrDefault(false)
        // The frozen fusion contract is strictly causal at the 20:00 cutoff.
        // A live preview, by contrast, may use observations known exactly now.
        val liveServiceObservation = inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1 && !historicalEvaluation
        val forecastRecords = records.filter { record ->
            if (liveServiceObservation) {
                runCatching { Instant.parse(record.wakeTime).isBefore(cutoff.toInstant()) }.getOrDefault(false) &&
                    availableAtOrBefore(now, record)
            } else if (preview) availableAtOrBefore(forecastBoundary, record)
            else availableBefore(forecastBoundary, record)
        }
        val personalizationBoundary = if (historicalEvaluation) cutoff.toInstant() else now
        val liveRecords = records.filter {
            if (historicalEvaluation) availableBefore(personalizationBoundary, it)
            else availableAtOrBefore(personalizationBoundary, it)
        }
        val forecastInputRows = policyRows(forecastRecords, localNow, profile.createdAt, inputPolicy)
        val personalizationNow = personalizationBoundary.atZone(SEOUL)
        val liveInputRows = policyRows(liveRecords, personalizationNow, profile.createdAt, inputPolicy)
        val forecastInputRecords = forecastInputRows.map { it.record }
        val liveInputRecords = liveInputRows.map { it.record }
        val result = fusion.predict(
            profile, forecastInputRecords, cutoff, inputPolicy = inputPolicy,
            observedThrough = now.takeIf { liveServiceObservation }
        )
        val appPersonalization = personalize(
            profile, liveInputRecords, personalizationNow,
            questionnaireBoundaryInclusive = !historicalEvaluation
        )
        val usualWake = LocalTime.parse(profile.usualWake)
        val recommendationNightId = MeasurementRecommendationPolicy.nightId(localNow, usualWake)
        val recommendationCutoff = recommendationNightId.atTime(20, 0).atZone(SEOUL)
        val activeContext = profile.dailyContext?.takeIf { context ->
            context.nightId == recommendationNightId.toString() && runCatching {
                !Instant.parse(context.observedAt).isAfter(now)
            }.getOrDefault(false)
        }
        val hybridCmi = result.predictions.getJSONObject("hybrid_cmi")
        val hybridMesa = result.predictions.getJSONObject("hybrid_mesa")
        val cmiBed = hybridCmi.getDouble("onset_offset_min")
        val mesaBed = hybridMesa.getDouble("onset_offset_min")
        val behaviorBed = (cmiBed + mesaBed) / 2.0

        val wakeDate = recommendationNightId.plusDays(1)
        val habitualWake = ZonedDateTime.of(wakeDate, appPersonalization.wakeTime, SEOUL)
        val scheduleWake = activeContext?.resolveScheduleWake(recommendationCutoff, localNow)
            ?.takeIf { it.isBefore(habitualWake) }
        val constrainedWake = scheduleWake ?: habitualWake
        val targetDuration = appPersonalization.durationMinutes
        val completeDiaries = liveInputRows.mapNotNull { row ->
            if (isWearableEntry(row.record) &&
                (!hasTrustedQuestionnaire(row, personalizationNow, !historicalEvaluation) ||
                    row.record.rested == null || row.record.fatigue == null)
            ) return@mapNotNull null
            val latency = row.record.sleepLatencyMinutes ?: return@mapNotNull null
            val awake = row.record.awakeMinutes ?: return@mapNotNull null
            val sleep = (row.timeInBedMinutes - latency - awake).takeIf { it > 0 } ?: return@mapNotNull null
            MeasurementRecommendationPolicy.CompleteDiary(row.bed, row.wake, latency, sleep)
        }
        val completedNap = activeContext?.let { context ->
            val start = context.napStart?.let { runCatching { Instant.parse(it).atZone(SEOUL) }.getOrNull() }
            val end = context.napEnd?.let { runCatching { Instant.parse(it).atZone(SEOUL) }.getOrNull() }
            if (start != null && end != null) MeasurementRecommendationPolicy.SleepInterval(start, end) else null
        }
        val policy = MeasurementRecommendationPolicy.plan(
            now = localNow,
            wake = constrainedWake,
            targetSleepMinutes = targetDuration,
            latencyMinutes = appPersonalization.latencyMinutes,
            phaseMinutes = appPersonalization.phaseMinutes,
            personalizedMode = appPersonalization.personalized,
            evidenceAdequate = !appPersonalization.conservative,
            usualBed = LocalTime.parse(profile.usualBed),
            usualWake = usualWake,
            recentCompleteDiaries = completeDiaries,
            completedNap = completedNap
        )
        val desiredBedtime = constrainedWake.minus(
            Duration.ofMillis(((policy.desiredSleepMinutes + appPersonalization.latencyMinutes) * 60_000.0).roundToLong())
        )
        val wake = policy.wake
        val bedtime = policy.bed
        val availableMinutes = policy.sleepOpportunityMinutes
        val planFeasible = policy.feasible
        val windDown = policy.windDown
        val generated = now.toString()
        val enrolledByCutoff = runCatching {
            !Instant.parse(profile.createdAt).isAfter(cutoff.toInstant())
        }.getOrDefault(false)
        val contextKnownByCutoff = activeContext == null || runCatching {
            !Instant.parse(activeContext.observedAt).isAfter(cutoff.toInstant())
        }.getOrDefault(false)

        val forecastStatus = when {
            historicalEvaluation -> "historical_reconstruction_not_prospective"
            preview -> "preview_before_20"
            !enrolledByCutoff -> "live_after_enrollment_not_prospective"
            !contextKnownByCutoff -> "live_after_cutoff_context_not_prospective"
            !localNow.isAfter(decisionToday.plusMinutes(15)) -> "prospective_20_window"
            else -> "late_reconstruction_not_prospective"
        }
        val notePrefix = when (forecastStatus) {
            "historical_reconstruction_not_prospective" -> "과거 파일을 실제 기준 시각으로 재구성한 값이므로 전향 예측 또는 보정 표본으로 간주하지 않습니다."
            "preview_before_20" -> "20시 이전 미리보기입니다. 20시 저장 예측은 그때까지 들어온 기록으로 다시 계산됩니다."
            "prospective_20_window" -> "오늘 20시 기준 입력으로 생성한 다음 주수면 예측입니다."
            "live_after_enrollment_not_prospective" -> "20시 이후 등록한 첫 권장이므로 사전 저장 예측으로 간주하지 않습니다."
            "live_after_cutoff_context_not_prospective" -> "20시 이후 입력한 생활 맥락을 반영한 실시간 권장이므로 20시 사전 저장 예측으로 간주하지 않습니다."
            else -> "20시 15분 이후 처음 계산한 재구성값이므로 사전 저장 예측으로 간주하지 않습니다."
        }
        val forecast = Forecast(
            generatedAt = generated,
            cmiBedOffset = cmiBed,
            mesaBedOffset = mesaBed,
            ensembleBedOffset = behaviorBed,
            note = "$notePrefix CMI·MESA 평균은 검증된 새 앙상블이 아니라 행동 지연 안내용 참고값입니다."
        )
        val recommendation = Recommendation(
            generatedAt = generated,
            wakeTime = wake.toOffsetDateTime().toString(),
            bedtime = bedtime.toOffsetDateTime().toString(),
            durationMinutes = availableMinutes,
            regularityMinutes = appPersonalization.regularityMinutes,
            subjectiveQuality = appPersonalization.restedMean,
            processS = policy.currentS,
            planFeasible = planFeasible,
            explanation = buildString {
                if (!planFeasible) {
                    append("현재 일정으로 목표 수면시간과 입면 여유를 모두 확보하기 어려워 일정 조정이 필요합니다. 확보 가능한 수면 기회는 ${availableMinutes}분입니다. ")
                } else {
                    append("기상 목표, ${targetDuration}분 수면 목표와 입면 여유 ${appPersonalization.latencyMinutes.roundToInt()}분을 우선한 권장입니다. ")
                }
                append(when {
                    !appPersonalization.personalized -> "아직 유효한 수면 기록이 없어 사용자가 정한 목표 수면시간과 평소 일정을 유지합니다. "
                    appPersonalization.conservative -> "최근성 가중 수면 기록 통계는 갱신했지만 근거가 부족·오래됨·불규칙한 경우라 사용자가 정한 수면 목표를 보수적으로 유지합니다. "
                    else -> "14일 반감기 수면 기록 근거와 사용자 목표를 수축 결합해 개인화했습니다. "
                })
                if (scheduleWake != null && constrainedWake == scheduleWake) append("다음 일정과 준비 시간을 반영해 기상 시각을 앞당겼습니다. ")
                if (activeContext?.napStart != null) append("낮잠 기록은 생활 맥락으로 함께 저장했습니다. ")
                if (activeContext?.caffeineAt != null) append("카페인 기록은 생활 맥락으로 함께 저장했습니다. ")
                if (policy.extensionMinutes > 0) append("측정 정보가 갖춰진 최근 수면 기록의 관측 부족분을 반영해 수면 기회를 ${policy.extensionMinutes.roundToInt()}분 늘렸습니다. ")
                if (policy.napApplied) append("완료된 낮잠을 고정 Two-Process 수면압력 계산에 반영했습니다. ")
                append("확보 가능한 범위에서 고정 Two-Process 수면압력·일주기 점수로 5분 간격 후보를 비교했고 ${formatTime(windDown)}부터 준비하도록 안내합니다. ")
                append(if (inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1)
                    "착용 수면 시각은 서비스 예측·행동 위상 입력이며, S는 측정값이 아닌 Two-Process 계산값입니다."
                else "착용 기록은 보조 지표이며 S는 측정값이 아닌 Two-Process 계산값입니다.")
            }
        )
        val adaptivePredictions = adaptiveResearchPredictions(result, prospectiveOutcomes, cutoff.toInstant())
        val researchBaselines = ResearchBaselines.compute(profile, records, cutoff.toInstant(), activeContext)
        return EngineOutput(
            recommendation,
            forecast,
            outcomesJson(profile, liveRecords, forecastInputRows.count { isManualEntry(it.record) },
                liveInputRows.count { isManualEntry(it.record) }, forecastInputRows.size, liveInputRows.size,
                inputPolicy, localNow, result, appPersonalization, policy, forecastStatus, windDown,
                activeContext, scheduleWake, constrainedWake, desiredBedtime, availableMinutes, planFeasible,
                adaptivePredictions, researchBaselines)
        )
    }

    private data class WeektypePattern(
        val status: String,
        val weekdayCount: Int,
        val weekendCount: Int,
        val weekdayDecayedMass: Double,
        val weekendDecayedMass: Double,
        val weekdayResultantLength: Double,
        val weekendResultantLength: Double,
        val weekdayMidpointMin: Double?,
        val weekendMidpointMin: Double?,
        val weekendMinusWeekdayMin: Double?
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("status", status)
            .put("used_for_recommendation", false)
            .put("minimum_each_type", 2)
            .put("weekday_count", weekdayCount)
            .put("weekend_count", weekendCount)
            .put("weekday_decayed_mass", weekdayDecayedMass)
            .put("weekend_decayed_mass", weekendDecayedMass)
            .put("weekday_resultant_length", weekdayResultantLength)
            .put("weekend_resultant_length", weekendResultantLength)
            .put("weekday_midpoint_min", weekdayMidpointMin ?: JSONObject.NULL)
            .put("weekend_midpoint_min", weekendMidpointMin ?: JSONObject.NULL)
            .put("weekend_minus_weekday_min", weekendMinusWeekdayMin ?: JSONObject.NULL)
            .put("interpretation", "기상일 기준 평일·주말 수면 중간시각의 기술 통계이며 권장에는 사용하지 않음")
    }

    private data class Personalization(
        val durationMinutes: Int,
        val wakeTime: LocalTime,
        val regularityMinutes: Int?,
        val restedMean: Int?,
        val personalized: Boolean,
        val effectiveWeight: Double,
        val eligibleCount: Int,
        val decayedMass: Double,
        val kishEffectiveN: Double,
        val goodNightCount: Int,
        val goodNightMass: Double,
        val goodNightWeight: Double,
        val baselineDurationMinutes: Int,
        val priorSource: JSONObject,
        val weektypePattern: WeektypePattern,
        val conservative: Boolean,
        val phaseMinutes: Double,
        val phaseResultantLength: Double?,
        val phaseStatus: String,
        val evidenceFlags: List<MeasurementPersonalization.Flag>,
        val lastRecordGapDays: Double?,
        val latestObservationAgeDays: Double?,
        val latencyEffectiveRecords: Double,
        val activityEffectiveRecords: Double,
        val exerciseEffectiveRecords: Double,
        val latencyMinutes: Double,
        val averageSteps: Double,
        val exerciseMinutes: Double
    )

    private fun personalize(
        profile: UserProfile,
        records: List<SleepRecord>,
        now: ZonedDateTime,
        questionnaireBoundaryInclusive: Boolean = true
    ): Personalization {
        val valid = personalRows(records.filter { isManualEntry(it) || isWearableEntry(it) }, now, profile.createdAt)
        val prior = populationDuration(profile)
        // The user-declared adult target is the baseline. NHANES remains descriptive metadata only.
        val baselineDuration = UserContractRules.boundAdultSleepTarget(profile.desiredSleepMinutes)
        val evidence = valid.map { row ->
            val trustedQuestionnaire = hasTrustedQuestionnaire(row, now, questionnaireBoundaryInclusive)
            val supplementalMeasurementKnown = row.record.subjectiveSource == null || trustedQuestionnaire
            MeasurementPersonalization.DiaryEvidence(
                midpointMinutes = positiveModulo(minutes(row.bed.toLocalTime()) + row.timeInBedMinutes / 2.0, 1440.0),
                wakeAgeDays = Duration.between(row.wake, now).seconds.coerceAtLeast(0) / 86400.0,
                observationAgeDays = Duration.between(row.observed, now).seconds.coerceAtLeast(0) / 86400.0,
                timeInBedMinutes = row.timeInBedMinutes,
                latencyMinutes = row.record.sleepLatencyMinutes.takeIf { supplementalMeasurementKnown },
                awakeMinutes = row.record.awakeMinutes.takeIf { supplementalMeasurementKnown },
                rested = row.record.rested.takeIf { trustedQuestionnaire },
                fatigue = row.record.fatigue.takeIf { trustedQuestionnaire },
                steps = row.record.steps.takeIf { trustedQuestionnaire },
                exerciseMinutes = row.record.exerciseMinutes.takeIf { trustedQuestionnaire }
            )
        }
        val profileWake = LocalTime.parse(profile.usualWake)
        val policy = MeasurementPersonalization.compute(
            declaredTargetMinutes = baselineDuration,
            usualBedMinutes = minutes(LocalTime.parse(profile.usualBed)),
            usualWakeMinutes = minutes(profileWake),
            baselineLatencyMinutes = profile.sleepLatencyMinutes,
            baselineSteps = profile.averageSteps,
            baselineExerciseMinutes = profile.exerciseMinutes,
            evidence = evidence
        )
        // v7 keeps the explicit wake constraint; behavioral phase is diagnostic/scoring evidence, not a replacement alarm.
        val wakeTime = profileWake
        val recent = valid.takeLast(14)
        val bedCenter = circularMean(recent.map { minutes(it.bed.toLocalTime()) }, recent.map { recencyWeight(it.wake, now) })
        val regularity = if (recent.size < 2) null else median(recent.map { abs(shortestClockDifference(minutes(it.bed.toLocalTime()), bedCenter)) }).roundToInt()
        val reported = recent.filter { hasTrustedQuestionnaire(it, now, questionnaireBoundaryInclusive) }
            .mapNotNull { it.record.rested }.filter { it in 1..5 }
        val restedMean = if (reported.isEmpty()) null else reported.average().roundToInt()
        val weektypePattern = weektypePattern(valid, policy.weights)
        return Personalization(
            policy.targetSleepMinutes, wakeTime, regularity, restedMean, policy.active,
            policy.personalizationWeight, valid.size, policy.effectiveRecords, policy.kishEffectiveN,
            policy.goodNightCount, policy.goodNightEffectiveRecords, policy.sleepTargetWeight,
            baselineDuration, prior, weektypePattern, policy.conservative, policy.phaseMinutes,
            policy.phaseResultantLength, policy.phaseStatus, policy.flags, policy.lastRecordGapDays,
            policy.latestObservationAgeDays, policy.latencyEffectiveRecords,
            policy.activityEffectiveRecords, policy.exerciseEffectiveRecords, policy.latencyMinutes,
            policy.averageSteps, policy.exerciseMinutes
        )
    }

    private fun weektypePattern(rows: List<PersonalRow>, weights: List<Double>): WeektypePattern {
        data class Center(val midpoint: Double?, val mass: Double, val resultantLength: Double)
        val indexed = rows.zip(weights)
        val weekday = indexed.filter { (row, _) -> row.wake.dayOfWeek.value < 6 }
        val weekend = indexed.filter { (row, _) -> row.wake.dayOfWeek.value >= 6 }
        fun center(group: List<Pair<PersonalRow, Double>>): Center {
            val mass = group.sumOf { it.second }
            val values = group.map { (row, _) ->
                positiveModulo(minutes(row.bed.toLocalTime()) + row.timeInBedMinutes / 2.0, 1440.0)
            }
            if (mass <= 0.0) return Center(null, mass, 0.0)
            val x = values.zip(group.map { it.second }).sumOf { (value, weight) ->
                cos(2 * PI * value / 1440.0) * weight
            }
            val y = values.zip(group.map { it.second }).sumOf { (value, weight) ->
                sin(2 * PI * value / 1440.0) * weight
            }
            val resultant = sqrt(x * x + y * y) / mass
            val midpoint = if (group.size >= 2 && resultant > 1e-12)
                positiveModulo(atan2(y, x) * 1440.0 / (2 * PI), 1440.0) else null
            return Center(midpoint, mass, resultant)
        }
        val weekdayCenter = center(weekday)
        val weekendCenter = center(weekend)
        val weekdayMidpoint = weekdayCenter.midpoint
        val weekendMidpoint = weekendCenter.midpoint
        val sufficient = weekdayMidpoint != null && weekendMidpoint != null
        return WeektypePattern(
            status = if (sufficient) "exploratory_sufficient" else "insufficient_history",
            weekdayCount = weekday.size,
            weekendCount = weekend.size,
            weekdayDecayedMass = weekdayCenter.mass,
            weekendDecayedMass = weekendCenter.mass,
            weekdayResultantLength = weekdayCenter.resultantLength,
            weekendResultantLength = weekendCenter.resultantLength,
            weekdayMidpointMin = weekdayMidpoint,
            weekendMidpointMin = weekendMidpoint,
            weekendMinusWeekdayMin = if (weekendMidpoint != null && weekdayMidpoint != null)
                shortestClockDifference(weekendMidpoint, weekdayMidpoint) else null
        )
    }

    private data class PersonalRow(
        val record: SleepRecord,
        val bed: ZonedDateTime,
        val wake: ZonedDateTime,
        val observed: ZonedDateTime,
        val duration: Int,
        val timeInBedMinutes: Double
    )

    private fun populationDuration(profile: UserProfile): JSONObject {
        val bytes = populationLoader()
        val hash = java.security.MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        require(hash == POPULATION_HASH) { "집단 사전값 파일 해시가 맞지 않습니다." }
        val raw = JSONObject(bytes.toString(Charsets.UTF_8))
        require(raw.optString("artifact_type") == "sleepwell_population_prior") { "집단 사전값 형식이 맞지 않습니다." }
        val age = normalizeAgeBand(profile.ageBand)
        val sex = normalizeSex(profile.sex)
        val activity = when { profile.activityMinutes < 30 -> "low"; profile.activityMinutes < 90 -> "medium"; else -> "high" }
        val keys = if (age == null) emptyList() else
            listOf(Triple(age, sex, activity), Triple(age, sex, "any"), Triple(age, "any", "any")).distinct()
        val groups = raw.getJSONArray("groups")
        for ((ageKey, sexKey, activityKey) in keys) {
            for (index in 0 until groups.length()) {
                val group = groups.getJSONObject(index)
                if (group.getString("age_group") == ageKey && group.getString("sex") == sexKey &&
                    group.getString("activity_band") == activityKey && group.getInt("n_people") >= 30) {
                    return JSONObject().put("available", true).put("mean_sleep_min", group.getDouble("mean_sleep_min"))
                        .put("n_people", group.getInt("n_people")).put("level", "$ageKey/$sexKey/$activityKey")
                        .put("version", raw.getString("version"))
                        .put("interpretation", raw.optString("interpretation", "관측 평균이며 최적 수면량이 아님"))
                }
            }
        }
        val global = raw.getJSONObject("global")
        return JSONObject().put("available", true).put("mean_sleep_min", global.getDouble("mean_sleep_min"))
            .put("n_people", global.getInt("n_people")).put("level", "global").put("version", raw.getString("version"))
            .put("interpretation", raw.optString("interpretation", "관측 평균이며 최적 수면량이 아님"))
    }

    private fun outcomesJson(
        profile: UserProfile,
        records: List<SleepRecord>,
        researchForecastDiaryCount: Int,
        livePersonalizationDiaryCount: Int,
        forecastModelInputCount: Int,
        livePersonalizationInputCount: Int,
        inputPolicy: SleepInputPolicy,
        now: ZonedDateTime,
        result: PortableFusionModel.Result,
        personalization: Personalization,
        recommendationPolicy: MeasurementRecommendationPolicy.Result,
        forecastStatus: String,
        windDown: ZonedDateTime,
        dailyContext: DailyContext?,
        scheduleWake: ZonedDateTime?,
        constrainedWake: ZonedDateTime,
        desiredBedtime: ZonedDateTime,
        availableMinutes: Int,
        planFeasible: Boolean,
        adaptivePredictions: JSONObject,
        researchBaselines: ResearchBaselines.Snapshot
    ): JSONObject {
        val valid = personalRows(records.filter { isManualEntry(it) || isWearableEntry(it) }, now, profile.createdAt)
        val primaryRows = valid.filter { isManualEntry(it.record) }
        val wearableRows = valid.filter { !isManualEntry(it.record) }
        val recent = primaryRows.takeLast(14)
        val earlier = if (primaryRows.size >= 21) primaryRows.dropLast(14).takeLast(7) else emptyList()
        fun metrics(rows: List<PersonalRow>): JSONObject {
            val primary = rows.filter { isManualEntry(it.record) }
            val subjective = primary.filter { it.record.rested != null || it.record.fatigue != null }
            val rested = subjective.mapNotNull { it.record.rested }.filter { it in 1..5 }
            val fatigue = subjective.mapNotNull { it.record.fatigue }.filter { it in 1..5 }
            val bedTimes = primary.map { minutes(it.bed.toLocalTime()) }
            val regularity = if (bedTimes.size < 2) null else {
                val center = circularMean(bedTimes, List(bedTimes.size) { 1.0 })
                median(bedTimes.map { abs(shortestClockDifference(it, center)) })
            }
            return JSONObject()
                .put("n_records", primary.size)
                .put("n_subjective_responses", subjective.size)
                .put("n_rested_responses", rested.size)
                .put("n_fatigue_responses", fatigue.size)
                .put("self_reported_rested_mean", rested.takeIf { it.isNotEmpty() }?.average() ?: JSONObject.NULL)
                .put("self_reported_fatigue_mean", fatigue.takeIf { it.isNotEmpty() }?.average() ?: JSONObject.NULL)
                .put("bedtime_regularity_mad_min", regularity ?: JSONObject.NULL)
        }
        val outcomes = JSONObject()
            .put("schema_version", 3)
            .put("generated_at", now.toInstant().toString())
            .put("night_id", recommendationPolicy.nightId.toString())
            .put("recommendation_night_id", recommendationPolicy.nightId.toString())
            .put("research_forecast_night_id", result.cutoff.toLocalDate().toString())
            .put("forecast_status", forecastStatus)
            .put("cutoff", result.cutoff.toOffsetDateTime().toString())
            .put("recommendation_engine_version", RECOMMENDATION_ENGINE_VERSION)
            .put("recommendation_policy", RECOMMENDATION_POLICY_VERSION)
            .put("recommendation_feasibility", JSONObject()
                .put("plan_feasible", planFeasible)
                .put("target_duration_min", recommendationPolicy.desiredSleepMinutes)
                .put("personalized_sleep_target_min", personalization.durationMinutes)
                .put("latency_allowance_min", personalization.latencyMinutes)
                .put("available_opportunity_min", availableMinutes)
                .put("shortfall_min", (recommendationPolicy.desiredSleepMinutes + personalization.latencyMinutes - availableMinutes).coerceAtLeast(0.0))
                .put("constrained_wake", constrainedWake.toOffsetDateTime().toString())
                .put("desired_bedtime", desiredBedtime.toOffsetDateTime().toString()))
            .put("measurement_recommendation", JSONObject()
                .put("supported_subset", "complete_diary_debt_latency_fixed_two_process_candidate_search_completed_nap")
                .put("observed_sleep_min", recommendationPolicy.observedSleepMinutes ?: JSONObject.NULL)
                .put("sleep_debt_min", recommendationPolicy.sleepDebtMinutes ?: JSONObject.NULL)
                .put("sleep_debt_status", if (recommendationPolicy.observedSleepMinutes == null)
                    "unavailable_no_complete_recent_diary" else "available")
                .put("sleep_debt_applied", recommendationPolicy.sleepDebtApplied)
                .put("extension_min", recommendationPolicy.extensionMinutes)
                .put("current_s", recommendationPolicy.currentS)
                .put("completed_nap_applied", recommendationPolicy.napApplied)
                .put("occupied_schedule_support", "not_available_android_input")
                .put("weekend_specific_wake_support", "not_available_android_input"))
            .put("physical_provider", PortableFusionModel.PHYSICAL_PROVIDER)
            .put("physical_interpretation", JSONObject()
                .put("process_s_role", "고정 상수 Two-Process 계산값; 측정된 수면압이 아님")
                .put("tau_wake_h", 18.2)
                .put("tau_sleep_h", 4.2)
                .put("constants_personally_estimated", false))
            .put("future_physical_provider", "PCR planned; not implemented")
            .put("model_version", result.runtimeModelVersion)
            .put("artifact_model_version", PortableFusionModel.MODEL_VERSION)
            .put("runtime_personalization_policy", result.personalizationPolicy)
            .put("research_metrics_scope", result.researchMetricsScope)
            .put("sleep_input_policy", result.inputPolicy)
            .put("service_input_policy", JSONObject()
                .put("policy", inputPolicy.wireName)
                .put("wearable_sources", JSONArray(listOf("samsung_health", "health_connect", "samsung_health_csv")))
                .put("diary_precedence_on_same_wake_date", true)
                .put("frozen_research_metrics_apply", inputPolicy == SleepInputPolicy.DIARY_ONLY)
                .put("sleep_wake_cutoff", result.cutoff.toInstant().toString())
                .put("observation_boundary", result.observationBoundary.toString())
                .put("observation_boundary_inclusive", result.observationBoundaryInclusive)
                .put("observation_boundary_role", if (result.observationBoundaryInclusive)
                    "live_service_known_by_actual_now" else "strict_research_cutoff"))
            .put("model_feature_contract", PortableFusionModel.FEATURE_CONTRACT)
            .put("profile_contract", JSONObject()
                .put("usual_wake", profile.usualWake).put("usual_bed", profile.usualBed)
                .put("desired_sleep_minutes", UserContractRules.boundAdultSleepTarget(profile.desiredSleepMinutes))
                .put("baseline_latency_minutes", profile.sleepLatencyMinutes)
                .put("baseline_average_steps", profile.averageSteps)
                .put("baseline_exercise_minutes", profile.exerciseMinutes)
                .put("occupation", profile.occupation)
                .put("occupation_model_coefficient", JSONObject.NULL))
            .put("daily_context", dailyContext?.toJson() ?: JSONObject.NULL)
            .put("daily_context_policy", JSONObject()
                .put("schedule_wake_constraint", scheduleWake?.toOffsetDateTime()?.toString() ?: JSONObject.NULL)
                .put("nap_model_coefficient", JSONObject.NULL)
                .put("completed_nap_process_s_applied", recommendationPolicy.napApplied)
                .put("caffeine_model_coefficient", JSONObject.NULL)
                .put("note", "첫 일정은 기상 제약, 완료된 낮잠은 고정 Two-Process S 재구성에 사용합니다. 카페인은 검증된 개인 계수가 없어 기록만 보존합니다."))
            .put("self_report_scales", JSONObject()
                .put("rested", "SleepWell Android 1-5; higher is more rested")
                .put("fatigue", "SleepWell Android 1-5; higher is more fatigued")
                .put("pooling_warning", "Windows의 기존 0-10 피로도와 직접 합산하지 않음"))
            .put("input_coverage", JSONObject()
                .put("diary_primary_records", primaryRows.size)
                .put("live_personalization_diary_records", livePersonalizationDiaryCount)
                .put("research_forecast_diary_records", researchForecastDiaryCount)
                .put("forecast_model_input_records", forecastModelInputCount)
                .put("live_personalization_input_records", livePersonalizationInputCount)
                .put("wearable_records", wearableRows.size)
                .put("wearable_auxiliary_records", wearableRows.size)
                .put("wearable_role", if (inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1)
                    "service_prediction_and_phase_input" else "auxiliary_only"))
            .put("personalization", JSONObject()
                .put("mode", if (personalization.personalized) "daily_recency_shrinkage" else "initial_user_goal")
                .put("parameter_version", PERSONALIZATION_POLICY_VERSION)
                .put("input_policy", inputPolicy.wireName)
                .put("phase_timing_sources", if (inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1)
                    "diary_and_recognized_wearable" else "diary_only")
                .put("minimum_history_nights", PERSONALIZATION_MINIMUM_HISTORY)
                .put("required_history_nights", PERSONALIZATION_MINIMUM_HISTORY)
                .put("activation_rule", if (inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1)
                    "first eligible diary or recognized wearable timing record" else "first eligible primary diary record")
                .put("half_life_days", 14.0)
                .put("shrinkage_lambda", 14.0)
                .put("adequate_effective_records_threshold", 5.0)
                .put("weight", personalization.effectiveWeight)
                .put("eligible_primary_nights", livePersonalizationDiaryCount)
                .put("eligible_input_nights", personalization.eligibleCount)
                .put("decayed_mass", personalization.decayedMass)
                .put("kish_effective_n", personalization.kishEffectiveN)
                .put("good_night_definition", if (inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1)
                    "complete measured duration plus causally observed questionnaire rested>=4 and fatigue<=3"
                    else "complete diary duration (latency and awake both present) with rested>=4 and fatigue<=3")
                .put("good_night_count", personalization.goodNightCount)
                .put("good_night_decayed_mass", personalization.goodNightMass)
                .put("sleep_target_weight", personalization.goodNightWeight)
                .put("baseline_duration_min", personalization.baselineDurationMinutes)
                .put("baseline_role", "사용자가 정한 희망 수면시간을 성인 420-540분 범위로 제한한 기준값")
                .put("population_prior", JSONObject(personalization.priorSource.toString())
                    .put("role", "descriptive_observed_mean_not_target")
                    .put("used_for_target", false))
                .put("phase_min", personalization.phaseMinutes)
                .put("phase_resultant_length", personalization.phaseResultantLength ?: JSONObject.NULL)
                .put("phase_status", personalization.phaseStatus)
                .put("last_record_gap_days", personalization.lastRecordGapDays ?: JSONObject.NULL)
                .put("latest_observation_age_days", personalization.latestObservationAgeDays ?: JSONObject.NULL)
                .put("evidence_quality", JSONObject()
                    .put("status", if (personalization.conservative) "conservative" else "adequate")
                    .put("heuristic_not_calibrated_probability", true)
                    .put("flags", JSONArray().also { flags -> personalization.evidenceFlags.forEach { flag ->
                        flags.put(JSONObject().put("code", flag.code).put("active", flag.active)
                            .put("threshold", flag.threshold).put("observed", flag.observed ?: JSONObject.NULL))
                    } }))
                .put("field_effective_records", JSONObject()
                    .put("phase", personalization.decayedMass)
                    .put("sleep_target", personalization.goodNightMass)
                    .put("latency", personalization.latencyEffectiveRecords)
                    .put("activity", personalization.activityEffectiveRecords)
                    .put("exercise", personalization.exerciseEffectiveRecords))
                .put("latency_min", personalization.latencyMinutes)
                .put("average_steps", personalization.averageSteps)
                .put("exercise_min", personalization.exerciseMinutes)
                .put("parameter_semantics", JSONObject()
                    .put("physiological_constants", "fixed_not_identified")
                    .put("phase", "behavioral_midpoint_not_DLMO")
                    .put("target_sleep", "heuristic_adjustment_around_user_goal_not_optimal_need"))
                .put("weektype_pattern", personalization.weektypePattern.toJson()))
            .put("primary_observed_outcomes", metrics(recent))
            .put("baseline_observed_outcomes", metrics(earlier))
            .put("baseline_comparable", earlier.isNotEmpty())
            .put("wearable_auxiliary", wearableMetrics(wearableRows.takeLast(14))
                .put("service_input_policy", inputPolicy.wireName)
                .put("used_for_service_prediction", inputPolicy == SleepInputPolicy.DIARY_AND_WEARABLE_V1))
            .put("research_predictions", result.predictions)
            .put("adaptive_research_predictions", adaptivePredictions)
            .put("research_baselines", researchBaselines.toJson())
            .put("research_artifacts", result.artifacts)
            .put("research_feature_snapshot", result.snapshots)
            .put("research_personalization", result.personalization)
            .put("wind_down_time", windDown.toOffsetDateTime().toString())
            .put("limitations", JSONArray()
                .put("착용 기록은 누락·기기·착용 습관 영향을 받는 보조 지표입니다.")
                .put("수면의 질 점수를 추정하지 않으며 개운함·피로는 사용자 응답만 집계합니다.")
                .put("Process S는 고정 상수의 Two-Process 계산값이며 측정된 생리값이 아닙니다.")
                .put("Android 입력에는 근무·수업 점유 구간과 주말별 별도 기상 목표가 없어 빈 제약으로 계산합니다.")
                .put("완전 일지의 밤중 각성 총량은 실제 발생 시각이 없어 수면압력 구간 내부 위치를 재구성하지 않습니다.")
                .put("관측 수면부족분의 25%·최대 30분 확장은 보정된 임상 규칙이 아닌 탐색적 정책입니다.")
                .put("권장 정책의 건강 개선 효과는 전향적으로 검증되지 않았습니다."))
        return outcomes
    }

    private fun adaptiveResearchPredictions(
        result: PortableFusionModel.Result,
        outcomes: List<JSONObject>,
        cutoff: Instant
    ): JSONObject = JSONObject().also { output ->
        result.predictions.keys().forEach { arm ->
            val current = result.predictions.getJSONObject(arm)
            val cohort = arm.substringAfterLast('_')
            val expectedArtifact = result.artifacts.optJSONObject(cohort)?.optString("sha256").orEmpty()
            val history = outcomes.mapNotNull { observed ->
                if (observed.optString("source_model_version") != result.runtimeModelVersion ||
                    observed.optString("source_personalization_policy") != result.personalizationPolicy ||
                    observed.optString("source_research_metrics_scope") != result.researchMetricsScope
                ) return@mapNotNull null
                val artifact = observed.optJSONObject("source_research_artifacts")
                    ?.optJSONObject(cohort)?.optString("sha256").orEmpty()
                if (expectedArtifact.isBlank() || artifact != expectedArtifact) return@mapNotNull null
                val residual = observed.optJSONObject("residuals")?.optJSONObject(arm) ?: return@mapNotNull null
                runCatching {
                    AdaptiveResidualPersonalization.HistoryResidual(
                        observed.optString("night_id").takeIf(String::isNotBlank),
                        residual.getDouble("onset_residual_min"), residual.getDouble("wake_residual_min"),
                        Instant.parse(observed.getString("available_at")),
                        Instant.parse(observed.optString("outcome_at", observed.getString("available_at")))
                    )
                }.getOrNull()
            }
            val adjustment = AdaptiveResidualPersonalization.computeAdjustment(history, cutoff)
            val projected = AdaptiveResidualPersonalization.project(
                current.getDouble("onset_offset_min") + adjustment.onsetCorrectionMin,
                current.getDouble("wake_offset_min") + adjustment.wakeCorrectionMin
            )
            output.put(arm, JSONObject()
                .put("role", "동결 연구예측의 전향적 과거 오차 보정 sidecar; 원본 예측을 대체하지 않음")
                .put("base_onset_offset_min", current.getDouble("onset_offset_min"))
                .put("base_wake_offset_min", current.getDouble("wake_offset_min"))
                .put("adjusted_onset_offset_min", projected.first)
                .put("adjusted_wake_offset_min", projected.second)
                .put("adjustment", adjustment.toJson()))
        }
    }

    private fun normalizeAgeBand(value: String): String? =
        value.trim().takeIf { it in setOf("18-29", "30-44", "45-59", "60-80") }

    private fun normalizeSex(value: String): String = when (value.lowercase()) {
        "female", "f", "여성", "여" -> "female"
        "male", "m", "남성", "남" -> "male"
        else -> "any"
    }

    private fun isManualEntry(record: SleepRecord): Boolean =
        record.source.lowercase() in setOf("manual", "diary")

    private fun isWearableEntry(record: SleepRecord): Boolean =
        record.source.lowercase() in setOf("samsung_health", "health_connect", "samsung_health_csv")

    private fun policyRows(
        records: List<SleepRecord>,
        now: ZonedDateTime,
        createdAt: String,
        policy: SleepInputPolicy
    ): List<PersonalRow> {
        val allowed = records.filter { record ->
            isManualEntry(record) || (policy == SleepInputPolicy.DIARY_AND_WEARABLE_V1 && isWearableEntry(record))
        }
        return personalRows(allowed, now, createdAt)
            .groupBy { it.wake.toLocalDate() }
            .values.map { sameDay ->
                sameDay.filter { isManualEntry(it.record) }.maxWithOrNull(
                    compareBy<PersonalRow>({ it.observed }, { it.record.id })
                ) ?: sameDay.maxWith(compareBy<PersonalRow>({ it.observed }, { it.record.id }))
            }
            .sortedBy { it.wake.toInstant() }
    }

    private fun hasTrustedQuestionnaire(
        row: PersonalRow,
        boundary: ZonedDateTime,
        boundaryInclusive: Boolean = true
    ): Boolean {
        if (isManualEntry(row.record)) return true
        if (!isWearableEntry(row.record) || row.record.subjectiveSource?.lowercase() != "questionnaire") return false
        val observed = row.record.subjectiveObservedAt?.let {
            runCatching { Instant.parse(it).atZone(SEOUL) }.getOrNull()
        } ?: return false
        val known = if (boundaryInclusive) !observed.isAfter(boundary) else observed.isBefore(boundary)
        return !observed.isBefore(row.wake) && known
    }

    private fun personalRows(records: List<SleepRecord>, now: ZonedDateTime, createdAt: String): List<PersonalRow> {
        val enrollment = runCatching { Instant.parse(createdAt).atZone(SEOUL) }.getOrElse { now }
        val bySourceAndDay = mutableMapOf<String, Pair<ZonedDateTime, PersonalRow>>()
        records.forEach { record ->
            runCatching {
                val bed = Instant.parse(record.bedtime).atZone(SEOUL)
                val wake = Instant.parse(record.wakeTime).atZone(SEOUL)
                val observed = Instant.parse(record.modifiedAt).atZone(SEOUL)
                val timeInBedMinutes = Duration.between(bed, wake).seconds / 60.0
                val duration = timeInBedMinutes.toInt()
                val supplementalKnown = record.subjectiveSource?.lowercase() != "questionnaire" ||
                    record.subjectiveObservedAt?.let {
                        runCatching { !Instant.parse(it).atZone(SEOUL).isAfter(now) }.getOrDefault(false)
                    } == true
                val latency = record.sleepLatencyMinutes.takeIf { supplementalKnown }
                val awake = record.awakeMinutes.takeIf { supplementalKnown }
                val knownLoss = (latency ?: 0) + (awake ?: 0)
                if (!bed.isBefore(wake) || wake.isAfter(now) || observed.isBefore(wake) || observed.isAfter(now) ||
                    wake.isBefore(enrollment) || timeInBedMinutes !in 30.0..960.0 ||
                    (latency != null && latency !in 0..180) ||
                    (awake != null && awake !in 0..600) ||
                    (supplementalKnown && record.steps != null && record.steps !in 0..100000) ||
                    (supplementalKnown && record.exerciseMinutes != null && record.exerciseMinutes !in 0.0..600.0) ||
                    knownLoss >= timeInBedMinutes) return@runCatching
                val stratum = if (isManualEntry(record)) "primary" else "wearable"
                val key = "$stratum:${wake.toLocalDate()}"
                val row = PersonalRow(record, bed, wake, observed, duration, timeInBedMinutes)
                val old = bySourceAndDay[key]
                if (old == null || observed.isAfter(old.first) ||
                    (observed == old.first && record.id > old.second.record.id)) {
                    bySourceAndDay[key] = observed to row
                }
            }
        }
        return bySourceAndDay.values.map { it.second }.sortedBy { it.wake.toInstant() }
    }

    private fun wearableMetrics(rows: List<PersonalRow>): JSONObject {
        val durations = rows.map { it.duration.toDouble() }
        val bedTimes = rows.map { minutes(it.bed.toLocalTime()) }
        val regularity = if (bedTimes.size < 2) null else {
            val center = circularMean(bedTimes, List(bedTimes.size) { 1.0 })
            median(bedTimes.map { abs(shortestClockDifference(it, center)) })
        }
        return JSONObject()
            .put("n_records", rows.size)
            .put("duration_mean_min", durations.takeIf { it.isNotEmpty() }?.average() ?: JSONObject.NULL)
            .put("bedtime_regularity_mad_min", regularity ?: JSONObject.NULL)
            .put("role", "착용·기기 영향을 받는 보조 지표")
    }

    private fun recencyWeight(wake: ZonedDateTime, now: ZonedDateTime): Double =
        exp(-ln(2.0) * Duration.between(wake, now).seconds.coerceAtLeast(0) / 86400.0 / 14.0)

    private fun weightedMedian(values: List<Double>, weights: List<Double>): Double {
        val pairs = values.zip(weights).sortedBy { it.first }
        val threshold = weights.sum() / 2.0
        var sum = 0.0
        pairs.forEach { (value, weight) -> sum += weight; if (sum >= threshold) return value }
        return values.last()
    }

    private fun circularMean(values: List<Double>, weights: List<Double>): Double {
        if (values.isEmpty()) return 0.0
        val x = values.zip(weights).sumOf { (value, weight) -> cos(2 * PI * value / 1440.0) * weight }
        val y = values.zip(weights).sumOf { (value, weight) -> sin(2 * PI * value / 1440.0) * weight }
        return positiveModulo(atan2(y, x) * 1440.0 / (2 * PI), 1440.0)
    }

    private fun shortestClockDifference(value: Double, reference: Double): Double =
        positiveModulo(value - reference + 720.0, 1440.0) - 720.0

    private fun median(values: List<Double>): Double {
        val sorted = values.sorted()
        return if (sorted.size % 2 == 1) sorted[sorted.size / 2] else (sorted[sorted.size / 2 - 1] + sorted[sorted.size / 2]) / 2.0
    }

    private fun positiveModulo(value: Double, modulus: Double): Double = ((value % modulus) + modulus) % modulus
    private fun minutes(time: LocalTime): Double = time.hour * 60.0 + time.minute + time.second / 60.0
    private fun formatTime(value: ZonedDateTime): String = "%02d:%02d".format(value.hour, value.minute)
}
