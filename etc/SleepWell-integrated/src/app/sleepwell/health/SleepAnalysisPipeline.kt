package app.sleepwell.health

import app.sleepwell.health.SleepEpochConverter.EpochInput
import java.io.InputStream
import java.io.PrintWriter
import java.io.StringWriter

/**
 * 수면 분석 파이프라인 (채윤 담당).
 *
 * 흐름:
 *   InputStream(CSV) → CsvFileSource 파싱 → SleepSession 목록
 *     → SleepEpochConverter.toEpochTimeline() (전체 타임라인, WAKE 포함)
 *     → TwoProcessModel.compute() (S, C_sleep, propensity, 시계열)
 *     → TwoProcessModel.predictFutureS() (미래 S 예측)
 *     → AnalysisResult 조립 → HealthResult<AnalysisResult> 반환
 *
 * 주의:
 *   - 입력: InputStream (File 아님). 함수 안에서 use{}로 닫음.
 *   - 타임라인 범위: end = referenceTimeMs ?: 마지막 세션 endMs, start = end - 7일.
 *     이 범위와 겹치는 세션만 epoch 변환에 사용. sleepRecords는 전체 유지.
 *   - DirectInput.sleepStartMs~sleepEndMs 구간이 둘 다 있으면 그 구간을 수면(isSleep=true)으로
 *     타임라인에 반영 (CSV 세션과 겹치면 CSV 우선).
 *   - 예외를 밖으로 던지지 말 것. Unavailable / Failure로 변환.
 */
object SleepAnalysisPipeline {

    // ────────────────────────────────────────────────
    // 공개 진입점
    // ────────────────────────────────────────────────

    /**
     * CSV 파일부터 분석 결과까지 한 번에 실행.
     *
     * @param input          수면 단계 CSV (sleep_stage_cleaned.csv 형식)의 InputStream.
     *                       함수 안에서 use{}로 닫음.
     * @param directInput    사용자 직접 입력 (선택). sleepStartMs~sleepEndMs 구간이 있으면
     *                       수면 구간으로 타임라인에 반영.
     * @param referenceTimeMs 기준 시각 (선택). null이면 마지막 세션의 endMs.
     *                       타임라인 범위: start = referenceTimeMs - 7일, end = referenceTimeMs.
     * @return HealthResult<AnalysisResult>
     *         - Success: 분석 결과
     *         - Unavailable: 수면 기록이 없거나, 수면/각성 중 한쪽만 존재, 기록이 더 필요한 경우
     *         - Failure: 그 외 예외
     */
    fun analyze(
        input: InputStream,
        directInput: DirectInput? = null,
        referenceTimeMs: Long? = null
    ): HealthResult<AnalysisResult> {
        return try {
            // ── 1. CSV 파싱 ──────────────────────────────────────────
            val sessions = parseCsv(input)
            if (sessions.isEmpty()) {
                return HealthResult.Unavailable("수면 기록이 없습니다")
            }

            // ── 2. 타임라인 범위 결정 ────────────────────────────────
            val (timelineStart, timelineEnd) = determineTimelineRange(sessions, referenceTimeMs)

            // ── 3. 타임라인 범위와 겹치는 세션만 epoch 변환 ───────────
            val sessionsInRange = filterSessionsInRange(sessions, timelineStart, timelineEnd)
            if (sessionsInRange.isEmpty()) {
                return HealthResult.Unavailable("기록이 더 필요합니다")
            }

            val epochTimeline = SleepEpochConverter.toEpochTimeline(
                sessions = sessionsInRange,
                startTimeMs = timelineStart,
                endTimeMs = timelineEnd
            )

            // ── 4. DirectInput 반영 (sleepStartMs~sleepEndMs 구간 추가) ──
            val extendedTimeline = applyDirectInput(epochTimeline, sessionsInRange, directInput,
                timelineStart, timelineEnd)

            requireNotEmpty("epoch timeline", extendedTimeline,
                "CSV 파싱 결과가 없어 epoch 타임라인을 만들 수 없습니다.")

            // ── 5. 수면/각성 클래스 확인 ──────────────────────────────
            val sleepClasses = extendedTimeline.map { epoch ->
                if (epoch.isSleep) SleepStage.LIGHT else SleepStage.WAKE
            }.distinct()
            if (sleepClasses.size < 2) {
                return HealthResult.Unavailable("기록이 더 필요합니다")
            }

            // ── 6. Two-Process 모델 실행 (S, C_sleep, propensity, history) ──
            val (timestamps, isSleep) = SleepEpochConverter.toModelInputArrays(extendedTimeline)
            val processOutput = TwoProcessModel.compute(timestamps, isSleep)

            // ── 7. 미래 S 예측 ────────────────────────────────────────
            val futureSPoints = TwoProcessModel.predictFutureS(
                lastS = processOutput.currentS,
                lastTimestampMs = processOutput.lastTimestampMs,
                phaseRef = processOutput.phaseRef,
                futureIntervalMin = 15
            )

            // ── 8. sleepRecords 구성 (전체, 세션별 요약) ───────────────
            val sleepRecords = sessions.map { session ->
                val stageSummary = session.stages.groupBy { it.stage.name }
                    .mapValues { (_, segments) ->
                        segments.sumOf { (it.endMs - it.startMs).toDouble() / 60_000.0 }
                    }

                SleepRecord(
                    sessionId = session.sessionId,
                    startMs = session.startMs,
                    endMs = session.endMs,
                    totalSleepMin = session.stages
                        .filter { it.stage != SleepStage.WAKE }
                        .sumOf { (it.endMs - it.startMs).toDouble() / 60_000.0 },
                    stageSummary = stageSummary
                )
            }

            // ── 9. scHistory 구성 ────────────────────────────────────
            val scHistory = processOutput.history.map { point ->
                PropensityPoint(
                    timestampMs = point.timestampMs,
                    S = point.S,
                    C = point.C,
                    propensity = point.propensity
                )
            }

            // ── 10. AnalysisResult 조립 ──────────────────────────────
            HealthResult.Success(AnalysisResult(
                currentS = processOutput.currentS,
                currentC = processOutput.currentC,
                currentPropensity = processOutput.currentPropensity,
                sleepRecords = sleepRecords,
                scHistory = scHistory,
                futureS = futureSPoints.map { PredictedPoint(it.timestampMs, it.predictedS, it.predictedC, it.predictedPropensity) },
                recommendation = null,       // "준비 중" — 서윤 언니 담당
                modelVersion = "Two-Process Kotlin 모델 (기준 코드 일치)",
                calculationTimeMs = System.currentTimeMillis()
            ))
        } catch (e: Exception) {
            val sw = StringWriter()
            e.printStackTrace(PrintWriter(sw))
            HealthResult.Failure(e)
        }
    }

    // ────────────────────────────────────────────────
    // 내부 헬퍼
    // ────────────────────────────────────────────────

    /**
     * InputStream에서 CSV를 파싱하여 SleepSession 목록 반환.
     * CsvFileSource의 내부 파싱 함수를 사용. InputStream은 호출 측에서 use{}로 닫음.
     */
    private fun parseCsv(input: InputStream): List<SleepSession> {
        val rows = CsvFileSource().parseSleepStageCsvForPipeline(input)
        return CsvFileSource().groupIntoSessionsForPipeline(rows)
    }

    /**
     * 타임라인 범위 결정.
     * - end = referenceTimeMs ?: 마지막 세션 endMs
     * - start = end - 7일 (7 * 24 * 60 * 60 * 1000 ms)
     */
    private fun determineTimelineRange(
        sessions: List<SleepSession>,
        referenceTimeMs: Long?
    ): Pair<Long, Long> {
        val end = referenceTimeMs ?: sessions.maxOfOrNull { it.endMs }
            ?: return Pair(0L, 0L)

        val start = end - 7 * 24 * 60 * 60 * 1000L
        return Pair(start, end)
    }

    /**
     * 타임라인 범위 [startMs, endMs)와 겹치는 세션만 필터링.
     * sleepRecords는 전체 세션을 사용하므로 여기서 필터링하지 않음.
     */
    private fun filterSessionsInRange(
        sessions: List<SleepSession>,
        startMs: Long,
        endMs: Long
    ): List<SleepSession> {
        return sessions.filter { session ->
            session.startMs < endMs && session.endMs > startMs
        }
    }

    /**
     * DirectInput 반영: 새 epoch를 추가하지 않고 기존 30초 칸의 isSleep만 바꾼다.
     * (추가하면 같은 시각 칸이 두 개 생겨 dt=0 칸과 순서 꼬임이 생긴다)
     * CSV 기록(수면·각성 모두)과 겹치지 않는 칸만 isSleep=true로 변경 (CSV 우선).
     */
    private fun applyDirectInput(
        epochTimeline: List<EpochInput>,
        sessions: List<SleepSession>,
        directInput: DirectInput?,
        timelineStart: Long,
        timelineEnd: Long
    ): List<EpochInput> {
        if (directInput == null || directInput.sleepStartMs == null || directInput.sleepEndMs == null) {
            return epochTimeline
        }

        val directStart: Long = directInput.sleepStartMs!!
        val directEnd: Long = directInput.sleepEndMs!!
        if (directEnd <= directStart) return epochTimeline

        // CSV에 기록된 구간(수면·각성 모두) — 이 구간은 CSV 우선
        val csvRegions = sessions.flatMap { session ->
            session.stages.map { region -> Pair(region.startMs, region.endMs) }
        }
        // 새 epoch를 덧붙이지 않고, 이미 있는 30초 칸의 isSleep만 바꾼다
        return epochTimeline.map { e ->
            val eEnd = e.timestampMs + 30_000L
            val inDirect = e.timestampMs < directEnd && eEnd > directStart
            val overlapsCsv = csvRegions.any { (cs, ce) -> e.timestampMs < ce && eEnd > cs }
            if (inDirect && !overlapsCsv) e.copy(isSleep = true) else e
        }
    }

    private fun requireNotEmpty(label: String, list: List<*>, customMessage: String = ""): List<*> {
        if (list.isEmpty()) {
            throw IllegalArgumentException(
                if (customMessage.isNotEmpty()) customMessage
                else "${label}이 비어 있습니다."
            )
        }
        return list
    }
}
