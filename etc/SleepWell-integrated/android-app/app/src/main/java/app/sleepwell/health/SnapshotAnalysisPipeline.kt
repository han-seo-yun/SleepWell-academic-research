package app.sleepwell.health

import java.time.Instant
import java.time.ZoneId
import kotlin.math.roundToInt

/** 수집 UI의 HealthSnapshot을 1분 물리모델과 Ridge 추천으로 연결하는 앱 통합 진입점. */
object SnapshotAnalysisPipeline {
    private const val MINUTE_MS = 60_000L
    private const val MAX_WINDOW_MS = 7L * 24 * 60 * 60 * 1000L

    fun analyze(
        snapshot: HealthSnapshot,
        directInput: DirectInput? = null,
        survey: DailySurveyFeatures? = null,
        residualModel: RidgeResidualModel? = null,
        referenceTimeMs: Long = snapshot.queriedTo,
        targetSleepMin: Double? = null
    ): HealthResult<AnalysisResult> {
        return try {
        if (snapshot.sleepSessions.isEmpty()) {
            return HealthResult.Unavailable("수면 기록이 없습니다")
        }
        val end = referenceTimeMs
        val start = maxOf(snapshot.queriedFrom, end - MAX_WINDOW_MS)
        if (end <= start) return HealthResult.Unavailable("조회 기간이 올바르지 않습니다")

        val timestamps = mutableListOf<Long>()
        val sleepFlags = mutableListOf<Boolean>()
        var cursor = Math.floorDiv(start, MINUTE_MS) * MINUTE_MS
        while (cursor < end) {
            timestamps += cursor
            val epochEnd = cursor + MINUTE_MS
            var sleeping = snapshot.sleepSessions.any { session ->
                if (session.stages.isEmpty()) {
                    session.startMs < epochEnd && session.endMs > cursor
                } else {
                    session.stages.any { segment ->
                        segment.stage.isSleep && segment.startMs < epochEnd && segment.endMs > cursor
                    }
                }
            }
            if (directInput?.sleepStartMs != null && directInput.sleepEndMs != null) {
                val overlapsDirect = directInput.sleepStartMs < epochEnd && directInput.sleepEndMs > cursor
                val overlapsMeasured = snapshot.sleepSessions.any { it.startMs < epochEnd && it.endMs > cursor }
                if (overlapsDirect && !overlapsMeasured) sleeping = true
            }
            sleepFlags += sleeping
            cursor += MINUTE_MS
        }
        if (sleepFlags.none { it } || sleepFlags.all { it }) {
            return HealthResult.Unavailable("수면·각성 기록이 모두 필요합니다")
        }

        val process = TwoProcessModel.compute(timestamps, sleepFlags)
        val future = TwoProcessModel.predictFutureS(
            process.currentS, process.lastTimestampMs, process.phaseRef, futureIntervalMin = 15
        ).map { PredictedPoint(it.timestampMs, it.predictedS, it.predictedC, it.predictedPropensity) }
        val records = snapshot.sleepSessions.sortedBy { it.startMs }.map { session ->
            val sleepSegments = session.stages.filter { it.stage.isSleep }
            val sleepMs = if (session.stages.isEmpty()) session.durationMs
            else sleepSegments.sumOf { it.endMs - it.startMs }
            SleepRecord(
                sessionId = session.sessionId,
                startMs = session.startMs,
                endMs = session.endMs,
                totalSleepMin = sleepMs / 60_000.0,
                stageSummary = sleepSegments.groupBy { it.stage.label }
                    .mapValues { (_, segments) -> segments.sumOf { it.endMs - it.startMs } / 60_000.0 }
            )
        }
        val sleepTarget = targetSleepMin ?: median(records.map { it.totalSleepMin }).coerceIn(360.0, 600.0)
        val features = PredictionFeatureBuilder.build(snapshot, process, survey, directInput, end)
        val recommendation = HybridRecommendationEngine.recommend(
            future, directInput, residualModel, features, sleepTarget
        )

        HealthResult.Success(
            AnalysisResult(
                currentS = process.currentS,
                currentC = process.currentC,
                currentPropensity = process.currentPropensity,
                sleepRecords = records,
                scHistory = process.history.map { PropensityPoint(it.timestampMs, it.S, it.C, it.propensity) },
                futureS = future,
                recommendation = recommendation,
                modelVersion = "Two-Process + Ridge residual · epoch 1분",
                calculationTimeMs = System.currentTimeMillis(),
                epochIntervalMs = MINUTE_MS,
                epochIntervalLabel = "1분",
                inputPeriodStartMs = start,
                inputPeriodEndMs = end
            )
        )
        } catch (e: Exception) {
            HealthResult.Failure(e)
        }
    }

    private fun median(values: List<Double>): Double {
        val sorted = values.filter { it > 0.0 }.sorted()
        if (sorted.isEmpty()) return 480.0
        return if (sorted.size % 2 == 1) sorted[sorted.size / 2]
        else (sorted[sorted.size / 2 - 1] + sorted[sorted.size / 2]) / 2.0
    }
}

/** 설문 CSV를 앱에 연결할 때 그대로 채울 수 있는 결정 시점 입력. */
data class DailySurveyFeatures(
    val caffeineMg: Double? = null,
    val lastCaffeineMs: Long? = null,
    val napMinutes: Double? = null,
    val napEndMs: Long? = null,
    val exerciseMinutes: Double? = null,
    val exerciseIntensity: Double? = null,
    val fatigueScore: Double? = null
)

object PredictionFeatureBuilder {
    private val seoul = ZoneId.of("Asia/Seoul")

    fun build(
        snapshot: HealthSnapshot,
        process: TwoProcessModel.ProcessSCOutput,
        survey: DailySurveyFeatures?,
        directInput: DirectInput?,
        decisionTimeMs: Long
    ): Map<String, Double?> {
        val dayStart = decisionTimeMs - 24L * 60 * 60 * 1000
        val hr = snapshot.heartRate.filter { it.timeMs in dayStart..decisionTimeMs }.map { it.bpm.toDouble() }
        val sessions = snapshot.sleepSessions.filter { it.endMs <= decisionTimeMs }.sortedBy { it.endMs }
        val durations = sessions.map { it.durationMs / 3_600_000.0 }
        val bedtime = sessions.map { minuteOfDay(it.startMs).toDouble() }
        val wake = sessions.map { minuteOfDay(it.endMs).toDouble() }
        val stepCount = snapshot.steps.filter { it.startMs <= decisionTimeMs && it.endMs >= dayStart }
            .sumOf { it.count }.toDouble()
        val activeMinutes = snapshot.steps.filter { it.count > 0 && it.startMs <= decisionTimeMs && it.endMs >= dayStart }
            .sumOf { (it.endMs - it.startMs).coerceAtLeast(0L) } / 60_000.0

        return linkedMapOf(
            "current_s" to process.currentS,
            "current_c" to process.currentC,
            "current_propensity" to process.currentPropensity,
            "day_hr_mean_pre20" to hr.takeIf { it.isNotEmpty() }?.average(),
            "day_hr_std_pre20" to hr.takeIf { it.size > 1 }?.let { values ->
                val m = values.average(); kotlin.math.sqrt(values.sumOf { (it - m) * (it - m) } / values.size)
            },
            "late_hr_mean_pre20" to snapshot.heartRate
                .filter { it.timeMs in (decisionTimeMs - 3 * 3_600_000L)..decisionTimeMs }
                .map { it.bpm.toDouble() }.takeIf { it.isNotEmpty() }?.average(),
            "steps_pre20" to stepCount,
            "active_minutes_pre20" to activeMinutes,
            "previous_sleep_duration_h" to durations.lastOrNull(),
            "prior_bedtime_median_7" to medianClock(bedtime, 20 * 60),
            "prior_wake_median_7" to medianClock(wake, 0),
            "prior_duration_median_7" to median(durations.takeLast(7)),
            "caffeine_mg" to survey?.caffeineMg,
            "caffeine_gap_min" to survey?.lastCaffeineMs?.let { (decisionTimeMs - it) / 60_000.0 },
            "nap_minutes" to survey?.napMinutes,
            "nap_end_gap_min" to survey?.napEndMs?.let { (decisionTimeMs - it) / 60_000.0 },
            "exercise_minutes" to survey?.exerciseMinutes,
            "exercise_intensity" to survey?.exerciseIntensity,
            "fatigue_score" to survey?.fatigueScore,
            "target_wake_minute" to directInput?.targetWakeMs?.let { minuteOfDay(it).toDouble() },
            "weekend" to Instant.ofEpochMilli(decisionTimeMs).atZone(seoul).dayOfWeek.value
                .let { if (it >= 6) 1.0 else 0.0 }
        )
    }

    private fun minuteOfDay(ms: Long): Int {
        val local = Instant.ofEpochMilli(ms).atZone(seoul)
        return local.hour * 60 + local.minute
    }

    private fun median(values: List<Double>): Double? {
        val s = values.sorted()
        if (s.isEmpty()) return null
        return if (s.size % 2 == 1) s[s.size / 2] else (s[s.size / 2 - 1] + s[s.size / 2]) / 2.0
    }

    private fun medianClock(values: List<Double>, anchor: Int): Double? {
        val shifted = values.takeLast(7).map { ((it - anchor + 1440.0) % 1440.0) }
        return median(shifted)?.let { (it + anchor) % 1440.0 }
    }
}
