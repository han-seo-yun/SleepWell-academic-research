package app.sleepwell.analysis

import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.EpochInterval
import app.sleepwell.health.HealthResult
import app.sleepwell.health.ReasonCode
import app.sleepwell.health.RidgeResidualModel
import app.sleepwell.health.SleepAnalysisPipeline
import app.sleepwell.health.SleepRecord
import java.io.File

/*
 * 채윤 파이프라인을 부르는 **유일한 곳**. (소빈 · 2026-09-25)
 *
 * - 화면은 이 함수만 부른다. 채윤이 호출 방식(작업지시서 1번: InputStream 입력 + HealthResult 반환)을
 *   바꾸면 이 파일 안쪽만 고치면 되고 화면 코드는 그대로 둔다.
 * - (2026-09-25) 채윤 최종본은 InputStream 을 받고 HealthResult 를 돌려준다. 화면은 계속 runFile 만 부른다.
 */
object AnalysisRunner {

    /**
     * (2026-10) 모델 입력 epoch 간격 = 1분. 2026-09-26 회의 "데이터셋과 앱을 모두 1분 기준으로 통일".
     * 채윤 파이프라인의 기본값(EpochInterval.DEFAULT)은 30초라서, 여기서 명시해서 넘긴다.
     * 시작 시각은 1분 경계로 맞춘다(alignToEpochBoundary = true, 채윤 기본값) → 화면·export 의 1분 격자와 같은 시각.
     */
    val EPOCH_INTERVAL: EpochInterval = EpochInterval.MIN1

    /** Diary-only users can receive a plan without inventing wearable stages or a sensor graph. */
    fun runDiary(
        direct: DirectInput?,
        features: Map<String, Double?>,
        nowMs: Long,
        contextObservedAtMs: Long? = null,
        historicalReconstruction: Boolean = false,
        historicalCsvReanalysis: Boolean = false,
        historicalImports: List<app.sleepwell.user.SleepRecord>? = null,
        historicalDataCutoffMs: Long? = null
    ): AnalysisResult =
        NextNightModel.applyTo(AnalysisResult(
            currentS = 0.5, currentC = 0.0, currentPropensity = 0.5,
            sleepRecords = emptyList(), scHistory = emptyList(), futureS = null,
            recommendation = null, modelVersion = "직접 수면일지",
            calculationTimeMs = nowMs, inputPeriodStartMs = nowMs, inputPeriodEndMs = nowMs
        ), direct, features, nowMs, contextObservedAtMs, historicalReconstruction,
            historicalCsvReanalysis = historicalCsvReanalysis, historicalImports = historicalImports,
            historicalDataCutoffMs = historicalDataCutoffMs)

    /**
     * 채윤 최종본(219987a) 호출 방식: analyze(InputStream, DirectInput?, referenceTimeMs?) → HealthResult.
     * 파이프라인이 예외를 스스로 HealthResult 로 바꿔 주므로, 여기서는 파일 열기 실패와
     * 메모리 부족(OutOfMemoryError 는 Exception 이 아니라 파이프라인이 못 잡음)만 받는다.
     *
     * (4단계, 2026-09-25) 직접 입력한 기상 시각이 **업로드 기록보다 뒤**이면 기준 시각을 그 기상 시각으로 옮긴다.
     *  - 파이프라인은 기준 시각(기본 = 마지막 기록) 이전 7일만 계산하므로, 그대로 두면 "어젯밤" 입력이 범위 밖이라 무시된다.
     *  - 1순위 #5 cf "전날 취침·기상 시간을 받아 분석" → 마지막 기록 이후 빈칸을 직접 입력으로 이어 "지금"에 가깝게 계산.
     *  - 마지막 기록과 입력한 취침 사이는 파이프라인 규칙대로 '깨어 있음'으로 계산된다.
     */
    fun runFile(
        file: File,
        directInput: DirectInput? = null,
        predictionFeatures: Map<String, Double?> = emptyMap(),   // (채윤 2026-10-24) 설문 → 모델 입력. SurveyDay.featureValues() 그대로
        residualModel: RidgeResidualModel? = null                // 학습된 Ridge (없으면 물리모델만)
    ): HealthResult<AnalysisResult> {
        val first = runOnce(file, directInput, null, predictionFeatures, residualModel)
        val wake = directInput?.sleepEndMs ?: return first
        if (first !is HealthResult.Success) return first
        val lastRecordEnd = first.data.sleepRecords.maxOfOrNull { it.endMs } ?: return first
        if (wake <= lastRecordEnd) return first
        // Keep the diary for recommendations without filling a long sensor gap as awake.
        if (wake >= DirectInputParser.latestWakeAllowed(lastRecordEnd)) return first

        val second = runOnce(file, directInput, wake, predictionFeatures, residualModel)
        return if (second is HealthResult.Unavailable)
            HealthResult.Unavailable(
                "업로드한 기록의 마지막이 ${DirectInputParser.toText(lastRecordEnd)} 이라서, 기상 시각은 " +
                        "${DirectInputParser.toText(DirectInputParser.latestWakeAllowed(lastRecordEnd))} 이전이어야 반영돼요. " +
                        "최근 기록이 담긴 파일을 올리거나 날짜를 확인해 주세요.",
                ReasonCode.WAKE_OUT_OF_RANGE
            )
        else second
    }

    /**
     * (2026-10) 지난 밤 조회용: 기준 시각을 직접 정해서 계산한다 (그 앞 7일이 계산 구간).
     * 그 7일 안에 수면 기록이 하나도 없으면 파이프라인이 "기록이 더 필요합니다"로 거부한다.
     */
    fun runFileAt(
        file: File, directInput: DirectInput?, referenceTimeMs: Long,
        targetSleepMin: Double? = null,
        predictionFeatures: Map<String, Double?> = emptyMap(),   // (소빈 2026-10-24) 지난 날: 그날 쌓인 설문 값
        recommendationTimeMs: Long? = null,
        contextObservedAtMs: Long? = null,
        historicalReconstruction: Boolean = false,
        historicalCsvReanalysis: Boolean = false,
        historicalImports: List<app.sleepwell.user.SleepRecord>? = null,
        historicalDataCutoffMs: Long? = null
    ): HealthResult<AnalysisResult> =
        runOnce(
            file, directInput, referenceTimeMs, predictionFeatures, targetSleepMin = targetSleepMin,
            recommendationTimeMs = recommendationTimeMs, contextObservedAtMs = contextObservedAtMs,
            historicalReconstruction = historicalReconstruction,
            historicalCsvReanalysis = historicalCsvReanalysis, historicalImports = historicalImports,
            historicalDataCutoffMs = historicalDataCutoffMs
        )

    /** Refresh the live plan, but do not turn a long gap in recordings into days of assumed wakefulness. */
    fun runCurrent(
        file: File,
        baseline: AnalysisResult,
        direct: DirectInput?,
        features: Map<String, Double?>,
        nowMs: Long,
        contextObservedAtMs: Long? = null
    ): AnalysisResult {
        val lastWake = baseline.sleepRecords.maxOfOrNull { it.endMs }
        val recent = lastWake != null && nowMs - lastWake in 0..(36L * 3_600_000)
        val analysis = if (recent) {
            val refreshed = runFileAt(file, direct, nowMs, predictionFeatures = features)
            (refreshed as? HealthResult.Success)?.data ?: baseline
        } else baseline
        return NextNightModel.applyTo(analysis, direct, features, nowMs, contextObservedAtMs)
    }

    /*
     * (소빈 2026-10-24, 임시 — 채윤에게 엔진 쪽 수정 요청함)
     * 지난 날짜를 계산할 때 엔진(SleepAnalysisPipeline)은 수면시간 목표(중간값)와 기록 일수를 **파일 전체** 기록으로 센다.
     * → 10/20 을 봐도 10/21~ 기록이 섞인다. 화면 쪽에서 "기준 시각까지 끝난 기록"만으로 같은 규칙을 계산해 넘긴다.
     * 규칙은 엔진과 똑같이 맞춤: 수면시간 = totalSleepMin(>0) 중간값을 6~10시간으로 제한 / 기록 일수 = 기상 날짜(한국 시간) 수.
     * 엔진이 기준 시각까지만 쓰도록 고쳐지면 이 두 함수와 runFileAt 의 targetSleepMin 은 빼도 된다.
     */
    fun sleepTargetUntil(records: List<SleepRecord>, referenceTimeMs: Long): Double? {
        val v = records.filter { it.endMs <= referenceTimeMs + 60_000L }.map { it.totalSleepMin }.filter { it > 0.0 }.sorted()
        if (v.isEmpty()) return null
        val n = v.size
        val median = if (n % 2 == 1) v[n / 2] else (v[n / 2 - 1] + v[n / 2]) / 2.0
        return median.coerceIn(360.0, 600.0)
    }

    fun recordDaysUntil(records: List<SleepRecord>, referenceTimeMs: Long): Int =
        records.filter { it.endMs <= referenceTimeMs + 60_000L }
            .map { java.time.Instant.ofEpochMilli(it.endMs).atZone(NextNightModel.currentCalculationZone()).toLocalDate() }
            .distinct().size

    private fun runOnce(
        file: File,
        directInput: DirectInput?,
        referenceTimeMs: Long?,
        predictionFeatures: Map<String, Double?> = emptyMap(),
        residualModel: RidgeResidualModel? = null,
        targetSleepMin: Double? = null,
        recommendationTimeMs: Long? = null,
        contextObservedAtMs: Long? = null,
        historicalReconstruction: Boolean = false,
        historicalCsvReanalysis: Boolean = false,
        historicalImports: List<app.sleepwell.user.SleepRecord>? = null,
        historicalDataCutoffMs: Long? = null
    ): HealthResult<AnalysisResult> = try {
        file.inputStream().use { input ->
            SleepAnalysisPipeline.analyze(
                input, directInput, referenceTimeMs,
                epochInterval = EPOCH_INTERVAL, alignToEpochBoundary = true,
                residualModel = residualModel, predictionFeatures = predictionFeatures,
                targetSleepMin = targetSleepMin
            )
        }.let { res ->
            // (v6 소빈 2026-10-31) 수면 분석은 파이프라인 그대로, 예상·권장만 서윤 새 모델로 바꿔 끼운다 (NextNightModel)
            if (res is HealthResult.Success) HealthResult.Success(
                NextNightModel.applyTo(
                    res.data, directInput, predictionFeatures, recommendationTimeMs,
                    contextObservedAtMs, historicalReconstruction,
                    historicalCsvReanalysis = historicalCsvReanalysis, historicalImports = historicalImports,
                    historicalDataCutoffMs = historicalDataCutoffMs
                )
            ) else res
        }
    } catch (e: OutOfMemoryError) {
        HealthResult.Unavailable("파일이 너무 커서 메모리가 부족합니다.", ReasonCode.OUT_OF_MEMORY)
    } catch (e: Exception) {
        HealthResult.Failure(e)
    }
}
