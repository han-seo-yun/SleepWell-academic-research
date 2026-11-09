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
import kotlin.math.sin
import kotlin.math.sqrt

data class EngineOutput(
    val recommendation: Recommendation,
    val forecast: Forecast,
    val outcomes: JSONObject,
    val recommendationTiming: RecommendationTiming
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
        private const val PERSONALIZATION_POLICY_VERSION = "personalization-v4-daily-recency"
        private const val PERSONALIZATION_MINIMUM_HISTORY = 1
        private const val RECOMMENDATION_ENGINE_VERSION = "sleep-opportunity-v6-daily-personalization"
        private const val RECOMMENDATION_POLICY_VERSION = "wake_constraint_plus_sleep_opportunity_v3_daily_personalization"
    }

    private val fusion by lazy(fusionFactory)

    fun compute(
        profile: UserProfile,
        records: List<SleepRecord>,
        now: Instant = Instant.now(),
        prospectiveForecasts: List<JSONObject> = emptyList(),
        prospectiveOutcomes: List<JSONObject> = emptyList()
    ): EngineOutput {
        val localNow = now.atZone(SEOUL)
        val decisionToday = localNow.toLocalDate().atTime(20, 0).atZone(SEOUL)
        val preview = localNow.isBefore(decisionToday)
        val cutoff = decisionToday
        val availabilityBoundary = if (preview) now else cutoff.toInstant()
        val causalRecords = records.filter { record ->
            runCatching {
                Instant.parse(record.wakeTime).isBefore(availabilityBoundary) &&
                    Instant.parse(record.modifiedAt).isBefore(availabilityBoundary)
            }.getOrDefault(false)
        }
        // Wearable timing is intentionally auxiliary: it cannot displace a
        // diary night in the frozen research feature contract or tune the
        // user's target duration/subjective outcome.
        val primaryRecords = causalRecords.filter(::isManualEntry)
        val result = fusion.predict(profile, primaryRecords, cutoff)
        val appPersonalization = personalize(profile, primaryRecords, localNow)
        val activeContext = profile.dailyContext?.takeIf { context ->
            context.nightId == cutoff.toLocalDate().toString() && runCatching {
                !Instant.parse(context.observedAt).isAfter(now)
            }.getOrDefault(false)
        }
        val hybridCmi = result.predictions.getJSONObject("hybrid_cmi")
        val hybridMesa = result.predictions.getJSONObject("hybrid_mesa")
        val cmiBed = hybridCmi.getDouble("onset_offset_min")
        val mesaBed = hybridMesa.getDouble("onset_offset_min")
        val behaviorBed = (cmiBed + mesaBed) / 2.0
        val expectedOnset = cutoff.plusSeconds((behaviorBed * 60.0).roundToInt().toLong())
        val scheduleTiming = RecommendationScheduleRules.timing(cutoff.toLocalDate(), expectedOnset)
        val recommendationTiming = RecommendationTiming(
            nightId = cutoff.toLocalDate().toString(),
            modelCutoffAt = cutoff.toOffsetDateTime().toString(),
            expectedOnsetAt = expectedOnset.toOffsetDateTime().toString(),
            initialDecisionAt = scheduleTiming.initialDecisionAt().toOffsetDateTime().toString()
        )

        val wakeDate = cutoff.plusDays(1).toLocalDate()
        val habitualWake = ZonedDateTime.of(wakeDate, appPersonalization.wakeTime, SEOUL)
        val scheduleWake = activeContext?.firstStart?.let { firstStart ->
            runCatching {
                Instant.parse(firstStart).atZone(SEOUL)
                    .minusMinutes(activeContext.preparationMinutes.toLong())
            }.getOrNull()
        }
        val constrainedWake = scheduleWake?.takeIf { it.isBefore(habitualWake) } ?: habitualWake
        val targetDuration = appPersonalization.durationMinutes
        val desiredBedtime = constrainedWake.minusMinutes(targetDuration.toLong())
        val wake = if (constrainedWake.isAfter(localNow)) constrainedWake else localNow
        val bedtime = if (desiredBedtime.isAfter(localNow)) desiredBedtime else localNow
        val availableMinutes = Duration.between(bedtime, wake).toMinutes().coerceAtLeast(0).toInt()
        val planFeasible = availableMinutes >= targetDuration
        val opportunityOffset = Duration.between(cutoff, bedtime).seconds / 60.0
        val predictedDelay = (behaviorBed - opportunityOffset).coerceIn(0.0, 30.0).roundToInt()
        val proposedWindDown = bedtime.minusMinutes(predictedDelay.toLong())
        val windDown = if (proposedWindDown.isAfter(localNow)) proposedWindDown else localNow
        val generated = now.toString()
        val enrolledByCutoff = runCatching {
            !Instant.parse(profile.createdAt).isAfter(cutoff.toInstant())
        }.getOrDefault(false)
        val contextKnownByCutoff = activeContext == null || runCatching {
            !Instant.parse(activeContext.observedAt).isAfter(cutoff.toInstant())
        }.getOrDefault(false)

        val forecastStatus = when {
            preview -> "preview_before_20"
            !enrolledByCutoff -> "live_after_enrollment_not_prospective"
            !contextKnownByCutoff -> "live_after_cutoff_context_not_prospective"
            !localNow.isAfter(decisionToday.plusMinutes(15)) -> "prospective_20_window"
            else -> "late_reconstruction_not_prospective"
        }
        val notePrefix = when (forecastStatus) {
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
            processS = result.snapshots.getJSONObject("cmi").getDouble("current_s"),
            planFeasible = planFeasible,
            explanation = buildString {
                if (!planFeasible) {
                    append("현재 일정으로 목표 수면시간 확보가 불가해 일정 조정이 필요합니다. 확보 가능한 수면 기회는 ${availableMinutes}분이며 목표보다 ${targetDuration - availableMinutes}분 부족합니다. ")
                } else {
                    append("기상 목표와 ${targetDuration}분 수면 기회를 우선한 권장입니다. ")
                }
                append(if (appPersonalization.personalized) "첫 기록부터 최근 기록의 공백·최근성을 반영합니다. " else "아직 개인 기록이 없어 NHANES 자기보고 관측 평균을 성인 7~9시간 범위로 제한한 초기값을 사용하며, 이는 개인의 최적 수면량이 아닙니다. ")
                if (scheduleWake != null && constrainedWake == scheduleWake) append("다음 일정과 준비 시간을 반영해 기상 시각을 앞당겼습니다. ")
                if (activeContext?.napStart != null) append("낮잠 기록은 생활 맥락으로 함께 저장했습니다. ")
                if (activeContext?.caffeineAt != null) append("카페인 기록은 생활 맥락으로 함께 저장했습니다. ")
                if (predictedDelay > 0) append("융합 모델이 늦은 입면 가능성을 보여 ${formatTime(windDown)}부터 준비하도록 안내합니다. ")
                append("착용 기록은 보조 지표이며 S는 측정값이 아닌 Two-Process 계산값입니다.")
            }
        )
        val adaptivePredictions = adaptiveResearchPredictions(result, prospectiveOutcomes, cutoff.toInstant())
        val researchBaselines = ResearchBaselines.compute(profile, records, cutoff.toInstant(), activeContext)
        return EngineOutput(
            recommendation,
            forecast,
            outcomesJson(profile, causalRecords, localNow, result, appPersonalization, forecastStatus, windDown,
                activeContext, scheduleWake, constrainedWake, desiredBedtime, availableMinutes, planFeasible,
                adaptivePredictions, researchBaselines)
                .put("recommendation_schedule", recommendationTiming.toJson())
                .put("recommendation_schedule_role", "display_schedule_only_research_cutoff_unchanged"),
            recommendationTiming
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
        val weektypePattern: WeektypePattern
    )

    private fun personalize(profile: UserProfile, records: List<SleepRecord>, now: ZonedDateTime): Personalization {
        val valid = personalRows(records, now, profile.createdAt)
        val prior = populationDuration(profile)
        // NHANES is descriptive; the bounded user preference anchors the opportunity target.
        val baselineDuration = UserContractRules.boundAdultSleepTarget(profile.desiredSleepMinutes).toDouble()
        val weights = valid.map { recencyWeight(it.wake, now) }
        val effective = weights.sum()
        val effectiveSquares = weights.sumOf { it * it }
        val kishEffective = if (effectiveSquares > 0) effective * effective / effectiveSquares else 0.0
        val personalized = valid.isNotEmpty() && effective > 0.0
        val alpha = if (personalized) effective / (effective + 14.0) else 0.0
        val good = valid.zip(weights).filter { (row, _) ->
            isManualEntry(row.record) && UserContractRules.isCompleteGoodNight(row.record.rested, row.record.fatigue)
        }
        val goodMass = good.sumOf { it.second }
        val goodWeight = if (personalized && good.size >= 5 && goodMass > 0) goodMass / (goodMass + 14.0) else 0.0
        val personalDuration = if (goodWeight > 0) weightedMedian(good.map { it.first.duration.toDouble() }, good.map { it.second }) else baselineDuration
        val duration = ((1 - goodWeight) * baselineDuration + goodWeight * personalDuration)
            .coerceIn(baselineDuration - 30.0, baselineDuration + 30.0).roundToInt()

        val profileWake = LocalTime.parse(profile.usualWake)
        val observedWake = if (valid.isEmpty()) minutes(profileWake) else circularMean(valid.map { minutes(it.wake.toLocalTime()) }, weights)
        val wakeShift = shortestClockDifference(observedWake, minutes(profileWake)).coerceIn(-30.0, 30.0)
        val wakeMinute = positiveModulo(minutes(profileWake) + alpha * wakeShift, 1440.0).roundToInt() % 1440
        val wakeTime = LocalTime.of(wakeMinute / 60, wakeMinute % 60)
        val recent = valid.takeLast(14)
        val bedCenter = circularMean(recent.map { minutes(it.bed.toLocalTime()) }, recent.map { recencyWeight(it.wake, now) })
        val regularity = if (recent.size < 2) null else median(recent.map { abs(shortestClockDifference(minutes(it.bed.toLocalTime()), bedCenter)) }).roundToInt()
        val reported = recent.filter { isManualSubjective(it.record) }.mapNotNull { it.record.rested }.filter { it in 1..5 }
        val restedMean = if (reported.isEmpty()) null else reported.average().roundToInt()
        val weektypePattern = weektypePattern(valid, weights)
        return Personalization(duration, wakeTime, regularity, restedMean, personalized, alpha,
            valid.size, effective, kishEffective, good.size, goodMass, goodWeight,
            baselineDuration.roundToInt(), prior, weektypePattern)
    }

    private fun weektypePattern(rows: List<PersonalRow>, weights: List<Double>): WeektypePattern {
        data class Center(val midpoint: Double?, val mass: Double, val resultantLength: Double)
        val indexed = rows.zip(weights)
        val weekday = indexed.filter { (row, _) -> row.wake.dayOfWeek.value < 6 }
        val weekend = indexed.filter { (row, _) -> row.wake.dayOfWeek.value >= 6 }
        fun center(group: List<Pair<PersonalRow, Double>>): Center {
            val mass = group.sumOf { it.second }
            val values = group.map { (row, _) ->
                positiveModulo(minutes(row.bed.toLocalTime()) + row.duration / 2.0, 1440.0)
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

    private data class PersonalRow(val record: SleepRecord, val bed: ZonedDateTime, val wake: ZonedDateTime, val duration: Int)

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
        now: ZonedDateTime,
        result: PortableFusionModel.Result,
        personalization: Personalization,
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
        val valid = personalRows(records, now, profile.createdAt)
        val primaryRows = valid.filter { isManualEntry(it.record) }
        val wearableRows = valid.filter { !isManualEntry(it.record) }
        val recent = primaryRows.takeLast(14)
        val earlier = if (primaryRows.size >= 21) primaryRows.dropLast(14).takeLast(7) else emptyList()
        fun metrics(rows: List<PersonalRow>): JSONObject {
            val primary = rows.filter { isManualEntry(it.record) }
            val subjective = primary.filter { isManualSubjective(it.record) }
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
            .put("schema_version", 2)
            .put("generated_at", now.toInstant().toString())
            .put("night_id", result.cutoff.toLocalDate().toString())
            .put("forecast_status", forecastStatus)
            .put("cutoff", result.cutoff.toOffsetDateTime().toString())
            .put("recommendation_engine_version", RECOMMENDATION_ENGINE_VERSION)
            .put("recommendation_policy", RECOMMENDATION_POLICY_VERSION)
            .put("recommendation_feasibility", JSONObject()
                .put("plan_feasible", planFeasible)
                .put("target_duration_min", personalization.durationMinutes)
                .put("available_opportunity_min", availableMinutes)
                .put("shortfall_min", (personalization.durationMinutes - availableMinutes).coerceAtLeast(0))
                .put("constrained_wake", constrainedWake.toOffsetDateTime().toString())
                .put("desired_bedtime", desiredBedtime.toOffsetDateTime().toString()))
            .put("physical_provider", PortableFusionModel.PHYSICAL_PROVIDER)
            .put("physical_interpretation", JSONObject()
                .put("process_s_role", "고정 상수 Two-Process 계산값; 측정된 수면압이 아님")
                .put("tau_wake_h", 18.2)
                .put("tau_sleep_h", 4.2)
                .put("constants_personally_estimated", false))
            .put("future_physical_provider", "PCR planned; not implemented")
            .put("model_version", PortableFusionModel.MODEL_VERSION)
            .put("model_feature_contract", PortableFusionModel.FEATURE_CONTRACT)
            .put("profile_contract", JSONObject()
                .put("usual_wake", profile.usualWake).put("usual_bed", profile.usualBed)
                .put("occupation", profile.occupation)
                .put("occupation_model_coefficient", JSONObject.NULL))
            .put("daily_context", dailyContext?.toJson() ?: JSONObject.NULL)
            .put("daily_context_policy", JSONObject()
                .put("schedule_wake_constraint", scheduleWake?.toOffsetDateTime()?.toString() ?: JSONObject.NULL)
                .put("nap_model_coefficient", JSONObject.NULL)
                .put("caffeine_model_coefficient", JSONObject.NULL)
                .put("note", "일정은 기상 제약에만 사용하며 낮잠·카페인은 기록 맥락과 안내에만 사용합니다."))
            .put("self_report_scales", JSONObject()
                .put("rested", "SleepWell Android 1-5; higher is more rested")
                .put("fatigue", "SleepWell Android 1-5; higher is more fatigued")
                .put("pooling_warning", "Windows의 기존 0-10 피로도와 직접 합산하지 않음"))
            .put("input_coverage", JSONObject()
                .put("diary_primary_records", primaryRows.size)
                .put("wearable_auxiliary_records", wearableRows.size))
            .put("personalization", JSONObject()
                .put("mode", if (personalization.personalized) "daily_recency_shrinkage" else "population_warm_start")
                .put("parameter_version", PERSONALIZATION_POLICY_VERSION)
                .put("minimum_history_nights", PERSONALIZATION_MINIMUM_HISTORY)
                .put("required_history_nights", PERSONALIZATION_MINIMUM_HISTORY)
                .put("activation_rule", "first eligible primary diary record")
                .put("half_life_days", 14.0)
                .put("shrinkage_lambda", 14.0)
                .put("weight", personalization.effectiveWeight)
                .put("eligible_primary_nights", personalization.eligibleCount)
                .put("decayed_mass", personalization.decayedMass)
                .put("kish_effective_n", personalization.kishEffectiveN)
                .put("good_night_definition", "manual record with both rested>=4 and fatigue<=3")
                .put("good_night_count", personalization.goodNightCount)
                .put("good_night_decayed_mass", personalization.goodNightMass)
                .put("sleep_target_weight", personalization.goodNightWeight)
                .put("baseline_duration_min", personalization.baselineDurationMinutes)
                .put("baseline_role", "NHANES 자기보고 관측 평균을 성인 420-540분 범위로 제한한 초기값; 최적 필요량 아님")
                .put("population_prior", personalization.priorSource)
                .put("weektype_pattern", personalization.weektypePattern.toJson()))
            .put("primary_observed_outcomes", metrics(recent))
            .put("baseline_observed_outcomes", metrics(earlier))
            .put("baseline_comparable", earlier.isNotEmpty())
            .put("wearable_auxiliary", wearableMetrics(wearableRows.takeLast(14)))
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
                if (observed.optString("source_model_version") != PortableFusionModel.MODEL_VERSION) return@mapNotNull null
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

    private fun isManualSubjective(record: SleepRecord): Boolean =
        isManualEntry(record) && (record.rested != null || record.fatigue != null)

    private fun isManualEntry(record: SleepRecord): Boolean =
        record.source.lowercase() in setOf("manual", "diary", "user_entry")

    private fun personalRows(records: List<SleepRecord>, now: ZonedDateTime, createdAt: String): List<PersonalRow> {
        val enrollment = runCatching { Instant.parse(createdAt).atZone(SEOUL) }.getOrElse { now }
        val bySourceAndDay = mutableMapOf<String, Pair<ZonedDateTime, PersonalRow>>()
        records.forEach { record ->
            runCatching {
                val bed = Instant.parse(record.bedtime).atZone(SEOUL)
                val wake = Instant.parse(record.wakeTime).atZone(SEOUL)
                val observed = Instant.parse(record.modifiedAt).atZone(SEOUL)
                val duration = Duration.between(bed, wake).toMinutes().toInt()
                if (!bed.isBefore(wake) || !wake.isBefore(now) || !observed.isBefore(now) ||
                    wake.isBefore(enrollment) || duration !in 30..960) return@runCatching
                val stratum = if (isManualEntry(record)) "primary" else "wearable"
                val key = "$stratum:${wake.toLocalDate()}"
                val row = PersonalRow(record, bed, wake, duration)
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
