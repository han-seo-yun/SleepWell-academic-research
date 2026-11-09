package app.sleepwell.user

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToLong
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Exact Android inference port of web-app/night_core.py, robust_features.py and
 * fusion_forecast.py.  The artifacts remain research comparisons; this class
 * does not turn their predicted behaviour into a clinical recommendation.
 */
internal class PortableFusionModel private constructor(
    private val assetLoader: (String) -> ByteArray
) {
    constructor(context: Context) : this({ path -> context.assets.open(path).use { it.readBytes() } })
    internal constructor(loader: (String) -> ByteArray, testable: Boolean = true) : this(loader)
    companion object {
        const val MODEL_VERSION = "robust-next-night-research-v2"
        const val FEATURE_CONTRACT = "sleepwell_next_night_robust_v2"
        const val PHYSICAL_PROVIDER = "two_process_fixed_v1"
        private const val ARTIFACT_TYPE = "sleepwell_robust_next_night_portable"
        private const val TAU_WAKE = 18.2
        private const val TAU_SLEEP = 4.2
        private const val C_AMPLITUDE = 0.15
        private val SEOUL: ZoneId = ZoneId.of("Asia/Seoul")
        private val EXPECTED_HASHES = mapOf(
            "cmi" to "add8ba4e23f9c4c9337b821406386664a054580d86c6e53c9b5e6f189bdfa2c7",
            "mesa" to "757b939d756c9174eab9304e0d8e872f4ed9eeae34e99a3d921f29284207c620"
        )
        private val DATA_FEATURES = listOf(
            "population_bed", "population_wake", "habit_bed", "habit_wake",
            "recent_bed", "recent_wake", "previous_bed", "previous_wake",
            "recent_duration", "bed_variability", "wake_variability",
            "history_count_7", "history_age_days", "personal_weight", "weekend"
        )
        private val EXTRA_FEATURES = listOf(
            "next_day_sin", "next_day_cos", "next_day_weekend",
            "median14_bed", "median14_wake", "ew_bed", "ew_wake",
            "weekday_bed", "weekday_wake", "weekday_count",
            "bed_mad", "wake_mad", "duration_mad", "previous_duration",
            "bed_trend", "wake_trend", "gap_days_mean", "recent_count14"
        )
        private val PHYSICAL_FEATURES = listOf(
            "current_s", "current_c", "sleep_threshold", "wake_threshold",
            "physical_bed", "physical_wake"
        )
    }

    private val artifacts: Map<String, Artifact> = listOf("cmi", "mesa").associateWith { cohort ->
        val bytes = assetLoader("models/robust_${cohort}_v2.json")
        require(bytes.size <= 16 * 1024 * 1024) { "모델 파일이 너무 큽니다." }
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        require(digest == EXPECTED_HASHES.getValue(cohort)) { "모델 파일 해시가 맞지 않습니다: $cohort" }
        Artifact(JSONObject(bytes.toString(Charsets.UTF_8)), cohort, digest)
    }

    init {
        // Exported by the Python reference implementation.  This makes the
        // deployed provider fail closed if arithmetic or JSON traversal drifts.
        val reference = predict(
            UserProfile("30-44", "any", 0, "23:00", "07:00", 480, "2026-10-01T00:00:00Z"),
            emptyList(),
            ZonedDateTime.of(2026, 10, 20, 20, 0, 0, 0, SEOUL)
        ).predictions
        val expected = mapOf(
            "physical_cmi" to (180.0 to 690.0),
            "data_cmi" to (161.1637500487975 to 665.6572048847543),
            "hybrid_cmi" to (142.04257940157947 to 657.8397106761714),
            "physical_mesa" to (235.0 to 655.0),
            "data_mesa" to (186.31527801819936 to 639.8916827710668),
            "hybrid_mesa" to (186.4741883478474 to 640.3517720155231)
        )
        expected.forEach { (name, pair) ->
            val actual = reference.getJSONObject(name)
            require(kotlin.math.abs(actual.getDouble("onset_offset_min") - pair.first) <= 1e-7 &&
                kotlin.math.abs(actual.getDouble("wake_offset_min") - pair.second) <= 1e-7) {
                "Python 기준 예측과 Android 계산이 일치하지 않습니다: $name " +
                    "actual=${actual.getDouble("onset_offset_min")},${actual.getDouble("wake_offset_min")} expected=$pair"
            }
        }
        val history = (0 until 16).map { index ->
            val day = ZonedDateTime.of(2026, 10, 20, 20, 0, 0, 0, SEOUL).minusDays((17 - index).toLong())
            val bed = day.withHour(22 + index % 2).withMinute((index * 7) % 50)
            val wake = day.plusDays(1).withHour(6 + index % 3).withMinute((index * 11) % 50)
            SleepRecord(
                id = "reference:$index", day = wake.toLocalDate().toString(),
                bedtime = bed.toInstant().toString(), wakeTime = wake.toInstant().toString(),
                durationMinutes = Duration.between(bed, wake).toMinutes().toInt(),
                rested = 3, fatigue = 3, source = "diary",
                modifiedAt = wake.plusMinutes(30).toInstant().toString()
            )
        }
        val historyReference = predict(
            UserProfile("30-44", "any", 0, "23:00", "07:00", 480, "2026-09-19T15:00:00Z"),
            history,
            ZonedDateTime.of(2026, 10, 20, 20, 0, 0, 0, SEOUL)
        ).predictions
        val historyExpected = mapOf(
            "physical_cmi" to (175.0 to 685.0),
            "data_cmi" to (158.49808237468034 to 669.1552490375618),
            "hybrid_cmi" to (169.2404151431544 to 668.1009950350582),
            "physical_mesa" to (200.0 to 670.0),
            "data_mesa" to (182.47331222730517 to 676.2566882412182),
            "hybrid_mesa" to (181.20284809608705 to 678.8183100370821)
        )
        historyExpected.forEach { (name, pair) ->
            val actual = historyReference.getJSONObject(name)
            require(kotlin.math.abs(actual.getDouble("onset_offset_min") - pair.first) <= 1e-7 &&
                kotlin.math.abs(actual.getDouble("wake_offset_min") - pair.second) <= 1e-7) {
                "개인화 Python 기준 예측과 Android 계산이 일치하지 않습니다: $name " +
                    "actual=${actual.getDouble("onset_offset_min")},${actual.getDouble("wake_offset_min")} expected=$pair"
            }
        }
    }

    internal data class Result(
        val cutoff: ZonedDateTime,
        val predictions: JSONObject,
        val snapshots: JSONObject,
        val personalization: JSONObject,
        val artifacts: JSONObject
    )

    fun predict(profile: UserProfile, records: List<SleepRecord>, cutoff: ZonedDateTime): Result {
        require(cutoff.zone == SEOUL || cutoff.withZoneSameInstant(SEOUL).zone == SEOUL)
        val localCutoff = cutoff.withZoneSameInstant(SEOUL)
        require(localCutoff.toLocalTime() == LocalTime.of(20, 0)) { "예측 기준은 20:00이어야 합니다." }
        val predictions = JSONObject()
        val snapshots = JSONObject()
        val personalization = JSONObject()
        val metadata = JSONObject()
        artifacts.forEach { (cohort, artifact) ->
            val extended = extendedFeatures(records, localCutoff, artifact.prior, profile.createdAt)
            val physical = constrain(
                extended.values.getValue("physical_bed"),
                extended.values.getValue("physical_wake")
            )
            val data = artifact.predict("data", extended.values)
            val hybrid = artifact.predict("hybrid", extended.values)
            mapOf("physical" to physical, "data" to data, "hybrid" to hybrid).forEach { (arm, pair) ->
                predictions.put("${arm}_$cohort", predictionJson(localCutoff, pair))
            }
            snapshots.put(cohort, JSONObject(extended.values))
            personalization.put(cohort, extended.state)
            metadata.put(cohort, JSONObject()
                .put("cohort", cohort)
                .put("sha256", artifact.sha256)
                .put("model_version", MODEL_VERSION)
                .put("feature_contract", FEATURE_CONTRACT)
                .put("physical_provider", PHYSICAL_PROVIDER)
                .put("source_model_sha256", artifact.raw.getJSONObject("source_model_sha256")))
        }
        return Result(localCutoff, predictions, snapshots, personalization, metadata)
    }

    private fun predictionJson(cutoff: ZonedDateTime, pair: Pair<Double, Double>): JSONObject = JSONObject()
        .put("bed", plusMinutesExact(cutoff, pair.first).toOffsetDateTime().toString())
        .put("wake", plusMinutesExact(cutoff, pair.second).toOffsetDateTime().toString())
        .put("interval_min", pair.second - pair.first)
        .put("onset_offset_min", pair.first)
        .put("wake_offset_min", pair.second)

    private inner class Artifact(val raw: JSONObject, expectedCohort: String, val sha256: String) {
        val prior: Prior
        init {
            require(raw.getInt("schema_version") == 2)
            require(raw.getString("artifact_type") == ARTIFACT_TYPE)
            require(raw.getString("feature_contract") == FEATURE_CONTRACT)
            require(raw.getString("model_version") == MODEL_VERSION)
            require(raw.getString("status") == "research_comparison_only")
            require(raw.getString("cohort") == expectedCohort)
            require(raw.getString("decision_time") == "20:00 Asia/Seoul")
            require(raw.getString("physical_provider") == PHYSICAL_PROVIDER)
            val p = raw.getJSONObject("prior")
            prior = Prior(p.getDouble("bed_offset"), p.getDouble("wake_offset"))
            validateNight(prior.bed, prior.wake)
            validatePipeline(raw.getJSONObject("models").getJSONObject("data"), DATA_FEATURES + EXTRA_FEATURES)
            validatePipeline(raw.getJSONObject("models").getJSONObject("hybrid"), DATA_FEATURES + EXTRA_FEATURES + PHYSICAL_FEATURES)
        }

        fun predict(arm: String, values: Map<String, Double>): Pair<Double, Double> {
            val model = raw.getJSONObject("models").getJSONObject(arm)
            val names = model.getJSONArray("feature_order").strings()
            val preprocessing = model.getJSONObject("preprocessing")
            val medians = preprocessing.getJSONArray("imputer_median").doubles()
            val missing = BooleanArray(names.size)
            val source = names.mapIndexed { index, name ->
                val value = values[name] ?: Double.NaN
                missing[index] = value.isNaN()
                if (value.isNaN()) medians[index] else value
            }.toMutableList()
            preprocessing.getJSONArray("missing_indicator_features").ints().forEach { source += if (missing[it]) 1.0 else 0.0 }
            val low = preprocessing.getJSONArray("clip_low").doubles()
            val high = preprocessing.getJSONArray("clip_high").doubles()
            val clipped = source.mapIndexed { index, value -> value.coerceIn(low[index], high[index]) }
            val keep = preprocessing.getJSONArray("variance_keep").ints()
            val mean = preprocessing.getJSONArray("scale_mean").doubles()
            val scale = preprocessing.getJSONArray("scale").doubles()
            val row = keep.mapIndexed { index, sourceIndex -> (clipped[sourceIndex] - mean[index]) / scale[index] }
            val estimators = model.getJSONArray("estimators")
            val output = (0 until estimators.length()).map { estimatorPredict(estimators.getJSONObject(it), row) }
            return constrain(output[0], output[1])
        }

        private fun estimatorPredict(estimator: JSONObject, row: List<Double>): Double = when (estimator.getString("kind")) {
            "linear" -> estimator.getDouble("intercept") + estimator.getJSONArray("coefficients").doubles()
                .zip(row).sumOf { (coefficient, value) -> coefficient * value }
            "hist_gradient_boosting" -> estimator.getDouble("baseline") +
                (0 until estimator.getJSONArray("trees").length()).sumOf { treeIndex ->
                    treePredict(estimator.getJSONArray("trees").getJSONArray(treeIndex), row)
                }
            else -> error("지원하지 않는 추정기입니다.")
        }

        private fun treePredict(tree: JSONArray, row: List<Double>): Double {
            var index = 0
            while (true) {
                val node = tree.getJSONObject(index)
                if (node.has("leaf")) return node.getDouble("leaf")
                val value = row[node.getInt("feature")]
                val goLeft = if (value.isNaN()) node.getBoolean("missing_left") else value <= node.getDouble("threshold")
                index = node.getInt(if (goLeft) "left" else "right")
            }
        }

        private fun validatePipeline(model: JSONObject, expected: List<String>) {
            require(model.getJSONArray("feature_order").strings() == expected) { "모델 특징 순서가 맞지 않습니다." }
            val preprocessing = model.getJSONObject("preprocessing")
            val rawCount = expected.size
            require(preprocessing.getJSONArray("imputer_median").length() == rawCount)
            val expanded = rawCount + preprocessing.getJSONArray("missing_indicator_features").length()
            require(preprocessing.getJSONArray("clip_low").length() == expanded)
            require(preprocessing.getJSONArray("clip_high").length() == expanded)
            val kept = preprocessing.getJSONArray("variance_keep").length()
            require(preprocessing.getJSONArray("scale_mean").length() == kept)
            require(preprocessing.getJSONArray("scale").length() == kept)
            require(model.getJSONArray("estimators").length() == 2)
        }
    }

    private data class Prior(val bed: Double, val wake: Double)
    private data class HistoryRow(
        val bed: ZonedDateTime,
        val wake: ZonedDateTime,
        val observed: ZonedDateTime,
        val bedOffset: Double,
        val wakeOffset: Double,
        val duration: Double,
        val tieKey: String
    )
    private data class Extended(val values: LinkedHashMap<String, Double>, val state: JSONObject)

    private fun extendedFeatures(records: List<SleepRecord>, cutoff: ZonedDateTime, prior: Prior, enrolledAt: String): Extended {
        val base = baseFeatures(records, cutoff, prior, enrolledAt)
        val output = base.values
        val history = normalizeHistory(records, cutoff, enrolledAt)
        val recent = history.filter { !it.wake.isBefore(cutoff.minusDays(56)) }.takeLast(14)
        val nextDay = cutoff.plusDays(1)
        val nextIndex = nextDay.dayOfWeek.value - 1
        output["next_day_sin"] = sin(2 * PI * nextIndex / 7.0)
        output["next_day_cos"] = cos(2 * PI * nextIndex / 7.0)
        output["next_day_weekend"] = if (nextDay.dayOfWeek.value >= 6) 1.0 else 0.0
        output["recent_count14"] = recent.size.toDouble()
        val sameWeektype = recent.filter { (it.wake.dayOfWeek.value >= 6) == (nextDay.dayOfWeek.value >= 6) }
        output["weekday_count"] = sameWeektype.size.toDouble()
        listOf(
            Triple("bed", prior.bed, { row: HistoryRow -> row.bedOffset }),
            Triple("wake", prior.wake, { row: HistoryRow -> row.wakeOffset })
        ).forEach { (name, initial, getter) ->
            val values = recent.map(getter)
            val center = if (values.isEmpty()) initial else median(values)
            val weights = recent.map { exp(-ln(2.0) * secondsBetween(it.wake, cutoff) / 86400.0 / 7.0) }
            output["median14_$name"] = center
            output["ew_$name"] = if (values.isEmpty()) initial else weightedMedian(values, weights)
            val matched = sameWeektype.map(getter)
            val weight = matched.size / (matched.size + 4.0)
            output["weekday_$name"] = if (matched.isEmpty()) center else weight * median(matched) + (1 - weight) * center
            output["${name}_mad"] = if (values.isEmpty()) 0.0 else median(values.map { kotlin.math.abs(it - center) })
            output["${name}_trend"] = if (values.size >= 6) median(values.takeLast(3)) - median(values.dropLast(3)) else 0.0
        }
        val durations = recent.map { it.duration }
        output["duration_mad"] = if (durations.isEmpty()) 0.0 else {
            val center = median(durations); median(durations.map { kotlin.math.abs(it - center) })
        }
        output["previous_duration"] = durations.lastOrNull() ?: (prior.wake - prior.bed)
        val gaps = recent.zipWithNext { left, right -> secondsBetween(left.wake, right.wake) / 86400.0 }
        output["gap_days_mean"] = gaps.average().takeUnless { it.isNaN() } ?: 0.0
        return Extended(output, base.state)
    }

    private fun baseFeatures(records: List<SleepRecord>, cutoff: ZonedDateTime, prior: Prior, enrolledAt: String): Extended {
        validateNight(prior.bed, prior.wake)
        val history = normalizeHistory(records, cutoff, enrolledAt)
        val enrollment = parseTime(enrolledAt)
        val elapsed = max(0, cutoff.toLocalDate().toEpochDay() - enrollment.toLocalDate().toEpochDay()).toInt()
        val active = history.size >= 14 && elapsed >= 14
        val weight = if (active) history.size / (history.size + 14.0) else 0.0
        val habitBed = if (history.isEmpty()) prior.bed else (1 - weight) * prior.bed + weight * history.map { it.bedOffset }.average()
        val habitWake = if (history.isEmpty()) prior.wake else (1 - weight) * prior.wake + weight * history.map { it.wakeOffset }.average()
        val recent = history.filter { !it.wake.isBefore(cutoff.minusDays(28)) }.takeLast(7)
        val beds = recent.map { it.bedOffset }
        val wakes = recent.map { it.wakeOffset }
        val thresholds = stationaryThresholds(habitBed, habitWake)
        val currentS = reconstructS(history, cutoff, habitBed, habitWake)
        val physical = physicalPrediction(currentS, cutoff, thresholds.first, thresholds.second.first, thresholds.second.second, habitBed to habitWake)
        val values = linkedMapOf(
            "population_bed" to prior.bed, "population_wake" to prior.wake,
            "habit_bed" to habitBed, "habit_wake" to habitWake,
            "recent_bed" to (beds.takeIf { it.isNotEmpty() }?.let(::median) ?: prior.bed),
            "recent_wake" to (wakes.takeIf { it.isNotEmpty() }?.let(::median) ?: prior.wake),
            "previous_bed" to (beds.lastOrNull() ?: prior.bed), "previous_wake" to (wakes.lastOrNull() ?: prior.wake),
            "recent_duration" to (recent.map { it.duration }.takeIf { it.isNotEmpty() }?.let(::median) ?: prior.wake - prior.bed),
            "bed_variability" to pstdev(beds), "wake_variability" to pstdev(wakes),
            "history_count_7" to recent.size.toDouble(),
            "history_age_days" to (recent.lastOrNull()?.let { min(28.0, secondsBetween(it.wake, cutoff) / 86400.0) } ?: 28.0),
            "personal_weight" to weight, "weekend" to if (cutoff.dayOfWeek.value >= 6) 1.0 else 0.0,
            "current_s" to currentS, "current_c" to circadian(cutoff, thresholds.first),
            "sleep_threshold" to thresholds.second.first, "wake_threshold" to thresholds.second.second,
            "physical_bed" to physical.first.first, "physical_wake" to physical.first.second
        )
        val state = JSONObject()
            .put("valid_days", history.size).put("elapsed_days", elapsed).put("personal_weight", weight)
            .put("mode", if (active) "personalized" else "population")
            .put("habit_bed_offset", habitBed).put("habit_wake_offset", habitWake)
            .put("phase_min", thresholds.first).put("tau_wake_h", TAU_WAKE).put("tau_sleep_h", TAU_SLEEP)
            .put("sleep_threshold", thresholds.second.first).put("wake_threshold", thresholds.second.second)
            .put("physical_fallback", physical.second)
            .put("note", "행동 기록으로 보정한 위상·전환 기준. 생리 시간상수는 고정이며 측정된 개인 생리값이 아닙니다.")
        return Extended(values, state)
    }

    private fun normalizeHistory(records: List<SleepRecord>, cutoff: ZonedDateTime, enrolledAt: String): List<HistoryRow> {
        val enrollment = parseTime(enrolledAt)
        val byDate = mutableMapOf<LocalDate, HistoryRow>()
        records.forEach { record ->
            if (record.source.lowercase() !in setOf("manual", "diary", "user_entry")) return@forEach
            try {
                val bed = parseTime(record.bedtime)
                val wake = parseTime(record.wakeTime)
                val observed = parseTime(record.modifiedAt)
                val duration = secondsBetween(bed, wake) / 60.0
                if (!(bed.isBefore(wake) && wake.isBefore(cutoff) && observed.isBefore(cutoff) && duration in 30.0..960.0)) return@forEach
                if (wake.isBefore(enrollment)) return@forEach
                val bedOffset = offsetAfter20(bed)
                val wakeOffset = bedOffset + duration
                if (bedOffset > 960.0 || wakeOffset > 1440.0) return@forEach
                val tie = listOf(record.id, record.day, record.bedtime, record.wakeTime, record.modifiedAt, record.source).joinToString("\u0000")
                val row = HistoryRow(bed, wake, observed, bedOffset, wakeOffset, duration, tie)
                val old = byDate[wake.toLocalDate()]
                if (old == null || observed.isAfter(old.observed) || (observed == old.observed && tie > old.tieKey)) byDate[wake.toLocalDate()] = row
            } catch (_: RuntimeException) {
                // Invalid device rows are excluded, matching the Python causal normalizer.
            }
        }
        return byDate.values.sortedBy { it.wake.toInstant() }
    }

    private fun reconstructS(history: List<HistoryRow>, cutoff: ZonedDateTime, bedOffset: Double, wakeOffset: Double): Double {
        val start = cutoff.minusDays(7)
        val actual = history.filter { it.wake.isAfter(start) }.map { it.bed to it.wake }
        val known = history.map { cutoffForOnset(it.bed).toLocalDate() }.toSet()
        val intervals = actual.toMutableList()
        for (day in -8..-1) {
            val anchor = cutoff.plusDays(day.toLong())
            if (anchor.toLocalDate() !in known) intervals += plusMinutesExact(anchor, bedOffset) to plusMinutesExact(anchor, wakeOffset)
        }
        val edges = mutableSetOf(start, cutoff)
        intervals.forEach { (bed, wake) ->
            if (bed.isAfter(start) && bed.isBefore(cutoff)) edges += bed
            if (wake.isAfter(start) && wake.isBefore(cutoff)) edges += wake
        }
        val ordered = edges.sortedBy { it.toInstant() }
        var s = 0.5
        ordered.zipWithNext().forEach { (left, right) ->
            val asleep = intervals.any { (bed, wake) -> !left.isBefore(bed) && left.isBefore(wake) }
            s = advance(s, asleep, secondsBetween(left, right) / 3600.0)
        }
        return s
    }

    private fun stationaryThresholds(bed: Double, wake: Double): Pair<Double, Pair<Double, Double>> {
        val duration = (wake - bed).coerceIn(30.0, 960.0)
        val asleepFactor = exp(-duration / 60.0 / TAU_SLEEP)
        val awakeFactor = exp(-(24.0 - duration / 60.0) / TAU_WAKE)
        val sWake = asleepFactor * (1 - awakeFactor) / (1 - asleepFactor * awakeFactor)
        val sBed = 1 - (1 - sWake) * awakeFactor
        val phase = positiveModulo(1200.0 + (bed + wake) / 2.0, 1440.0)
        val cEdge = C_AMPLITUDE * cos(2 * PI * duration / 2.0 / 1440.0)
        return phase to (sBed + cEdge to sWake + cEdge)
    }

    private fun physicalPrediction(
        initialS: Double, cutoff: ZonedDateTime, phase: Double, high: Double, low: Double,
        fallback: Pair<Double, Double>
    ): Pair<Pair<Double, Double>, Boolean> {
        var s = initialS
        var onset: Double? = null
        var wake: Double? = null
        for (offset in 0..1440 step 5) {
            val time = cutoff.plusMinutes(offset.toLong())
            if (offset != 0) s = advance(s, onset != null, 5.0 / 60.0)
            val drive = s + circadian(time, phase)
            if (onset == null && offset <= 960 && drive >= high) onset = offset.toDouble()
            else if (onset != null && offset >= onset + 30 && drive <= low) { wake = offset.toDouble(); break }
        }
        return if (onset == null || wake == null || wake - onset !in 30.0..960.0) fallback to true else (onset to wake) to false
    }

    private fun advance(s: Double, asleep: Boolean, hours: Double): Double =
        if (asleep) s * exp(-hours / TAU_SLEEP) else 1 - (1 - s) * exp(-hours / TAU_WAKE)

    private fun circadian(time: ZonedDateTime, phase: Double): Double =
        C_AMPLITUDE * cos(2 * PI * (minute(time) - phase) / 1440.0)

    private fun cutoffForOnset(onset: ZonedDateTime): ZonedDateTime {
        val sameDay = onset.toLocalDate().atTime(20, 0).atZone(SEOUL)
        return if (!onset.isBefore(sameDay)) sameDay else sameDay.minusDays(1)
    }

    private fun parseTime(value: String): ZonedDateTime = Instant.parse(value).atZone(SEOUL)
    private fun secondsBetween(start: ZonedDateTime, end: ZonedDateTime): Double {
        val duration = Duration.between(start, end)
        return duration.seconds + duration.nano / 1_000_000_000.0
    }
    private fun plusMinutesExact(value: ZonedDateTime, minutes: Double): ZonedDateTime =
        value.plusNanos((minutes * 60_000_000.0).roundToLong() * 1_000L)
    private fun minute(value: ZonedDateTime): Double = value.hour * 60.0 + value.minute + value.second / 60.0
    private fun offsetAfter20(value: ZonedDateTime): Double = positiveModulo(minute(value) - 1200.0, 1440.0)
    private fun positiveModulo(value: Double, modulus: Double): Double = ((value % modulus) + modulus) % modulus

    private fun median(values: List<Double>): Double {
        require(values.isNotEmpty())
        val sorted = values.sorted()
        return if (sorted.size % 2 == 1) sorted[sorted.size / 2] else (sorted[sorted.size / 2 - 1] + sorted[sorted.size / 2]) / 2.0
    }

    private fun weightedMedian(values: List<Double>, weights: List<Double>): Double {
        val pairs = values.zip(weights).sortedBy { it.first }
        val threshold = weights.sum() / 2.0
        var cumulative = 0.0
        pairs.forEach { (value, weight) -> cumulative += weight; if (cumulative >= threshold) return value }
        error("가중 중앙값을 계산할 수 없습니다.")
    }

    private fun pstdev(values: List<Double>): Double {
        if (values.size <= 1) return 0.0
        val mean = values.average()
        return sqrt(values.sumOf { (it - mean).pow(2) } / values.size)
    }

    private fun validateNight(bed: Double, wake: Double) {
        require(bed.isFinite() && wake.isFinite() && bed in 0.0..960.0 && wake >= bed + 30.0 && wake <= 1440.0 && wake - bed <= 960.0)
    }

    private fun constrain(bed: Double, wake: Double): Pair<Double, Double> {
        val boundedBed = bed.coerceIn(0.0, 960.0)
        val boundedWake = wake.coerceIn(boundedBed + 30.0, min(boundedBed + 960.0, 1440.0))
        return boundedBed to boundedWake
    }

    private fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
    private fun JSONArray.doubles(): List<Double> = (0 until length()).map { getDouble(it) }
    private fun JSONArray.ints(): List<Int> = (0 until length()).map { getInt(it) }
}
