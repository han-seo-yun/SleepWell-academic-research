package app.sleepwell.user

import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.pow
import kotlin.math.sin

/**
 * Frozen research comparators evaluated beside, but never used by, the recommendation.
 * Every history value must have ended and been reported strictly before the 20:00 cutoff.
 */
object ResearchBaselines {
    const val VERSION = "android-research-baselines-v1"
    const val MODEL_IDENTIFIER = "research_comparator_only_no_recommendation"
    const val PROFILE_ASSUMPTION = "user_profile_assumption_not_cohort_train_population_prior"
    private val ZONE: ZoneId = ZoneId.of("Asia/Seoul")
    private val MANUAL = setOf("manual", "diary", "user_entry")

    data class Prediction(
        val available: Boolean,
        val onsetOffsetMin: Double? = null,
        val wakeOffsetMin: Double? = null,
        val method: String,
        val fallback: String? = null,
        val contributingRecords: Int = 0,
        val reason: String? = null
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("available", available)
            .put("onset_offset_min", onsetOffsetMin ?: JSONObject.NULL)
            .put("wake_offset_min", wakeOffsetMin ?: JSONObject.NULL)
            .put("interval_min", if (onsetOffsetMin != null && wakeOffsetMin != null) wakeOffsetMin - onsetOffsetMin else JSONObject.NULL)
            .put("method", method)
            .put("fallback", fallback ?: JSONObject.NULL)
            .put("contributing_records", contributingRecords)
            .put("reason", reason ?: JSONObject.NULL)
    }

    data class Snapshot(
        val cutoff: Instant,
        val historyCount: Int,
        val predictions: Map<String, Prediction>
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("schema_version", 1)
            .put("version", VERSION)
            .put("cutoff", cutoff.toString())
            .put("model_identifier", MODEL_IDENTIFIER)
            .put("history_count", historyCount)
            .put("history_policy", "manual only; wake and modified_at strictly before cutoff; latest version per KST wake date")
            .put("profile_fallback_role", PROFILE_ASSUMPTION)
            .put("baselines", JSONObject().also { output ->
                predictions.forEach { (name, prediction) -> output.put(name, prediction.toJson()) }
            })
    }

    private data class Row(
        val record: SleepRecord,
        val bed: ZonedDateTime,
        val wake: ZonedDateTime,
        val observed: ZonedDateTime,
        val onsetOffset: Double,
        val wakeOffset: Double
    )

    fun compute(
        profile: UserProfile,
        records: List<SleepRecord>,
        cutoff: Instant,
        context: DailyContext?
    ): Snapshot {
        val cutoffLocal = cutoff.atZone(ZONE)
        val profilePrediction = profilePrediction(profile)
        val rows = history(records, profile.createdAt, cutoffLocal)
        val windows = listOf(7L, 14L, 28L).associateWith { days ->
            rows.filter { !it.wake.isBefore(cutoffLocal.minusDays(days)) }
        }
        val recent28 = windows.getValue(28L)
        val targetWeekend = cutoffLocal.plusDays(1).dayOfWeek.value >= 6
        val daytype = recent28.filter { (it.wake.dayOfWeek.value >= 6) == targetWeekend }

        fun fallback(name: String): Prediction = Prediction(
            available = true,
            onsetOffsetMin = profilePrediction.first,
            wakeOffsetMin = profilePrediction.second,
            method = name,
            fallback = PROFILE_ASSUMPTION,
            contributingRecords = 0
        )

        fun arithmetic(name: String, selected: List<Row>): Prediction {
            if (selected.isEmpty()) return fallback(name)
            return projected(selected.map { it.onsetOffset }.average(), selected.map { it.wakeOffset }.average(), name, selected.size)
        }

        val last = recent28.lastOrNull()?.let {
            projected(it.onsetOffset, it.wakeOffset, "last_observation_within_28_calendar_days", 1)
        } ?: fallback("last_observation_within_28_calendar_days")
        val mean7 = arithmetic("arithmetic_mean_within_7_calendar_days", windows.getValue(7L))
        val mean14 = arithmetic("arithmetic_mean_within_14_calendar_days", windows.getValue(14L))
        val mean28 = arithmetic("arithmetic_mean_within_28_calendar_days", recent28)
        val circular7 = if (windows.getValue(7L).isEmpty()) {
            fallback("circular_clock_mean_within_7_calendar_days")
        } else {
            val selected = windows.getValue(7L)
            val onset = clockOffset(circularMean(selected.map { clockMinutes(it.bed) }))
            var wake = clockOffset(circularMean(selected.map { clockMinutes(it.wake) }))
            if (wake <= onset) wake += 1440.0
            projected(onset, wake, "circular_clock_mean_within_7_calendar_days", selected.size)
        }
        val weighted28 = if (recent28.isEmpty()) {
            fallback("recency_weighted_28_calendar_days_half_life_14_days")
        } else {
            val weights = recent28.map { row ->
                val days = Duration.between(row.wake, cutoffLocal).seconds.coerceAtLeast(0) / 86400.0
                2.0.pow(-days / 14.0)
            }
            projected(weighted(recent28.map { it.onsetOffset }, weights),
                weighted(recent28.map { it.wakeOffset }, weights),
                "recency_weighted_28_calendar_days_half_life_14_days", recent28.size)
        }
        val daytypePrediction = if (daytype.isEmpty()) {
            fallback("matching_target_weekday_or_weekend_within_28_calendar_days")
        } else arithmetic("matching_target_weekday_or_weekend_within_28_calendar_days", daytype)
        val schedule = schedulePrediction(profile, cutoffLocal, context)

        val predictions = linkedMapOf(
            "profile_clock_assumption" to projected(profilePrediction.first, profilePrediction.second,
                "usual_profile_clock_assumption", 0).copy(fallback = PROFILE_ASSUMPTION),
            "last_28_calendar_days" to last,
            "arithmetic_mean_7_calendar_days" to mean7,
            "arithmetic_mean_14_calendar_days" to mean14,
            "arithmetic_mean_28_calendar_days" to mean28,
            "circular_mean_7_calendar_days" to circular7,
            "recency_weighted_28_calendar_days_half_life_14_days" to weighted28,
            "target_daytype_28_calendar_days" to daytypePrediction,
            "next_day_first_schedule_only_profile_duration" to schedule
        )
        return Snapshot(cutoff, rows.size, predictions)
    }

    private fun history(records: List<SleepRecord>, createdAt: String, cutoff: ZonedDateTime): List<Row> {
        val enrollment = runCatching { Instant.parse(createdAt).atZone(ZONE) }.getOrNull()
            ?: return emptyList()
        val latest = mutableMapOf<String, Row>()
        records.forEach { record ->
            if (record.source.lowercase() !in MANUAL) return@forEach
            runCatching {
                val bed = Instant.parse(record.bedtime).atZone(ZONE)
                val wake = Instant.parse(record.wakeTime).atZone(ZONE)
                val observed = Instant.parse(record.modifiedAt).atZone(ZONE)
                val duration = Duration.between(bed, wake).toMinutes()
                if (!bed.isBefore(wake) || !wake.isBefore(cutoff) || !observed.isBefore(cutoff) ||
                    wake.isBefore(enrollment) || duration !in 30..960) return@runCatching
                val onsetOffset = clockOffset(clockMinutes(bed))
                var wakeOffset = clockOffset(clockMinutes(wake))
                if (wakeOffset <= onsetOffset) wakeOffset += 1440.0
                val row = Row(record, bed, wake, observed, onsetOffset, wakeOffset)
                val key = wake.toLocalDate().toString()
                val old = latest[key]
                if (old == null || observed.isAfter(old.observed) ||
                    (observed == old.observed && record.id > old.record.id)) latest[key] = row
            }
        }
        return latest.values.sortedBy { it.wake.toInstant() }
    }

    private fun profilePrediction(profile: UserProfile): Pair<Double, Double> {
        val onset = clockOffset(clockMinutes(LocalTime.parse(profile.usualBed)))
        var wake = clockOffset(clockMinutes(LocalTime.parse(profile.usualWake)))
        if (wake <= onset) wake += 1440.0
        return project(onset, wake)
    }

    private fun schedulePrediction(profile: UserProfile, cutoff: ZonedDateTime, context: DailyContext?): Prediction {
        if (context == null) return Prediction(false, method = "next_day_first_schedule_minus_preparation_with_profile_duration",
            reason = "no_schedule_context")
        val observed = runCatching { Instant.parse(context.observedAt).atZone(ZONE) }.getOrNull()
            ?: return Prediction(false, method = "next_day_first_schedule_minus_preparation_with_profile_duration",
                reason = "invalid_context_observed_at")
        if (context.nightId != cutoff.toLocalDate().toString() || observed.isAfter(cutoff)) {
            return Prediction(false, method = "next_day_first_schedule_minus_preparation_with_profile_duration",
                reason = "context_not_available_by_cutoff")
        }
        val first = context.firstStart?.let { runCatching { Instant.parse(it).atZone(ZONE) }.getOrNull() }
            ?: return Prediction(false, method = "next_day_first_schedule_minus_preparation_with_profile_duration",
                reason = "no_next_day_first_schedule")
        if (first.toLocalDate() != cutoff.plusDays(1).toLocalDate() || !first.isAfter(cutoff) || first.isAfter(cutoff.plusHours(36))) {
            return Prediction(false, method = "next_day_first_schedule_minus_preparation_with_profile_duration",
                reason = "first_schedule_outside_next_day_window")
        }
        val wake = Duration.between(cutoff, first.minusMinutes(context.preparationMinutes.toLong())).seconds / 60.0
        val onset = wake - profile.desiredSleepMinutes
        if (onset !in 0.0..960.0 || wake !in 30.0..1440.0) {
            return Prediction(false, method = "next_day_first_schedule_minus_preparation_with_profile_duration",
                fallback = "fixed_user_profile_desired_duration_not_population_trained",
                reason = "fixed_profile_duration_outside_prospective_prediction_window")
        }
        return projected(onset, wake, "next_day_first_schedule_minus_preparation_with_profile_duration", 0)
            .copy(fallback = "fixed_user_profile_desired_duration_not_population_trained")
    }

    private fun projected(onset: Double, wake: Double, method: String, count: Int): Prediction {
        val value = project(onset, wake)
        return Prediction(true, value.first, value.second, method, contributingRecords = count)
    }

    private fun project(rawOnset: Double, rawWake: Double): Pair<Double, Double> {
        val onset = rawOnset.coerceIn(0.0, 960.0)
        val wake = rawWake.coerceIn(onset + 30.0, minOf(1440.0, onset + 960.0))
        return onset to wake
    }

    private fun weighted(values: List<Double>, weights: List<Double>): Double =
        values.zip(weights).sumOf { (value, weight) -> value * weight } / weights.sum()

    private fun circularMean(values: List<Double>): Double {
        val x = values.sumOf { cos(2 * PI * it / 1440.0) }
        val y = values.sumOf { sin(2 * PI * it / 1440.0) }
        return positiveModulo(atan2(y, x) * 1440.0 / (2 * PI), 1440.0)
    }

    private fun clockMinutes(value: ZonedDateTime): Double = clockMinutes(value.toLocalTime())
    private fun clockMinutes(value: LocalTime): Double = value.hour * 60.0 + value.minute + value.second / 60.0
    private fun clockOffset(clockMinute: Double): Double = positiveModulo(clockMinute - 1200.0, 1440.0)
    private fun positiveModulo(value: Double, modulus: Double): Double = ((value % modulus) + modulus) % modulus
}
