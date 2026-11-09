package app.sleepwell.health

import java.io.File

/**
 * 수면 분석 파이프라인 (채윤 담당).
 *
 * 흐름:
 *   CSV 파일 → CsvFileSource 파싱 → SleepSession 목록
 *     → SleepEpochConverter.toEpochTimeline() (전체 타임라인, WAKE 포함)
 *     → TwoProcessModel.compute() (S, C, propensity, 시계열)
 *     → TwoProcessModel.predictFutureS() (미래 S 예측 — 현재는 기본 시나리오)
 *     → AnalysisResult 조립 → 소빈(프론트) 전달
 *
 * 주의:
 *   - futureS · recommendation은 현재도 null ("준비 중" 표시, 서윤 언니 담당).
 *   - 입력 타임라인은 세션 사이 깨어 있던 시간까지 포함해야 함.
 *   - 모델 파라미터는 코드 기본값(임시 모델). 확정된 값이 아님.
 */
object SleepAnalysisPipeline {

    // ────────────────────────────────────────────────
    // 공개 진입점
    // ────────────────────────────────────────────────

    /**
     * CSV 파일부터 분석 결과까지 한 번에 실행.
     *
     * @param csvFile       수면 단계 CSV 파일 (sleep_stage_cleaned.csv 형식)
     * @param directInput   사용자 직접 입력 (선택). CSV가 없으면 이 값으로 타임라인 보간.
     * @param futureIntervalMin 미래 S 예측 간격 (분). 그래프 표시 해상도 기준. 기본값 15분.
     * @return AnalysisResult (futureS·recommendation은 null 가능)
     * @throws IllegalArgumentException CSV 파싱 실패 시
     */
    fun analyze(
        csvFile: File,
        directInput: DirectInput? = null,
        futureIntervalMin: Int = 15
    ): AnalysisResult {
        // ── 1. CSV 파싱 ──────────────────────────────────────────
        val sessions = parseCsv(csvFile)
            .also { requireNotEmpty(csvFile.name, it) }

        // ── 2. 타임라인 범위 결정 ────────────────────────────────
        val (timelineStart, timelineEnd) = determineTimelineRange(sessions, directInput)

        // ── 3. 30초 epoch 전체 타임라인 변환 ─────────────────────
        val epochTimeline = SleepEpochConverter.toEpochTimeline(
            sessions = sessions,
            startTimeMs = timelineStart,
            endTimeMs = timelineEnd
        )

        requireNotEmpty("epoch timeline", epochTimeline,
            "CSV 파싱 결과가 없어 epoch 타임라인을 만들 수 없습니다.")

        // ── 4. Two-Process 모델 실행 (S, C, propensity, history) ──
        val (timestamps, isSleep) = SleepEpochConverter.toModelInputArrays(epochTimeline)
        val processOutput = TwoProcessModel.compute(timestamps, isSleep)

        // ── 5. 미래 S 예측 (기본 시나리오: 각성 가정, 서윤 언니 모델 대체 예정) ──
        val futureSPoints = TwoProcessModel.predictFutureS(
            lastS = processOutput.currentS,
            lastTimestampMs = processOutput.lastTimestampMs,
            futureIntervalMin = futureIntervalMin
        )

        // ── 6. sleepRecords 구성 (전체, 세션별 요약) ───────────────
        val sleepRecords = sessions.map { session ->
            val stageSummary = session.stages.groupBy { it.stage.name }
                .mapValues { (_, segments) ->
                    segments.sumOf { (it.endMs - it.startMs).toDouble() / 60000.0 }
                }

            SleepRecord(
                sessionId = session.sessionId,
                startMs = session.startMs,
                endMs = session.endMs,
                totalSleepMin = session.stages
                    .filter { it.stage != SleepStage.WAKE }
                    .sumOf { (it.endMs - it.startMs).toDouble() / 60000.0 },
                stageSummary = stageSummary
            )
        }

        // ── 7. scHistory 구성 (PropensityPoint 리스트로 변환) ───────
        val scHistory = processOutput.history.map { point ->
            PropensityPoint(
                timestampMs = point.timestampMs,
                S = point.S,
                C = point.C,
                propensity = point.propensity
            )
        }

        // ── 8. AnalysisResult 조립 ────────────────────────────────
        return AnalysisResult(
            currentS = processOutput.currentS,
            currentC = processOutput.currentC,
            currentPropensity = processOutput.currentPropensity,
            sleepRecords = sleepRecords,
            scHistory = scHistory,
            futureS = futureSPoints,     // 임시 모델 예측값 (서윤 언니 확정 전)
            recommendation = null,       // "준비 중" — 서윤 언니 담당
            modelVersion = "임시 Two-Process Kotlin 모델 (기본 파라미터)",
            calculationTimeMs = System.currentTimeMillis()
        )
    }

    // ────────────────────────────────────────────────
    // 내부 헬퍼
    // ────────────────────────────────────────────────

    /** CSV 파일을 파싱하여 SleepSession 목록 반환.
     * CsvFileSource의 내부 파싱 함수를 파이프라인에서 직접 사용.
     * 실제 앱에서는 코루틴 컨텍스트에서 readSleepSessions()를 호출.
     */
    private fun parseCsv(csvFile: File): List<SleepSession> {
        val inputStream = csvFile.inputStream()
        val rows = CsvFileSource().parseSleepStageCsvForPipeline(inputStream)
        return CsvFileSource().groupIntoSessionsForPipeline(rows)
    }

    /**
     * 타임라인 시작·종료 시각 결정.
     *
     * - 시작: 가장 이른 세션 시작, 또는 직접 입력 sleepStartMs(더 이르면 우선)
     * - 종료: 가장 늦은 세션 종료, 또는 직접 입력 sleepEndMs / 현재 시각(더 늦으면 우선)
     *
     * 세션 사이 WAKE 구간을 포함하기 위해 충분한 범위로 설정.
     */
    private fun determineTimelineRange(
        sessions: List<SleepSession>,
        directInput: DirectInput?
    ): Pair<Long, Long> {
        if (sessions.isEmpty() && directInput == null) {
            throw IllegalArgumentException("CSV 세션이 없고 직접 입력도 없습니다.")
        }

        val earliestSessionStart = sessions.minOfOrNull { it.startMs } ?: 0L
        val latestSessionEnd = sessions.maxOfOrNull { it.endMs } ?: 0L

        val directStart = directInput?.sleepStartMs
        val directEnd = directInput?.sleepEndMs

        val startTimeMs = listOfNotNull(earliestSessionStart, directStart).minOrNull() ?: 0L
        val endTimeMs = listOfNotNull(latestSessionEnd, directEnd, System.currentTimeMillis())
            .maxOrNull() ?: 0L

        if (startTimeMs == 0L || endTimeMs == 0L) {
            throw IllegalArgumentException("타임라인 범위를 결정할 수 없습니다.")
        }

        return Pair(startTimeMs, endTimeMs)
    }

    private fun requireNotEmpty(label: String, list: List<*>, customMessage: String = ""): List<*> {
        if (list.isEmpty()) {
            throw IllegalArgumentException(
                if (customMessage.isNotEmpty()) customMessage
                else "$label이 비어 있습니다."
            )
        }
        return list
    }
}
