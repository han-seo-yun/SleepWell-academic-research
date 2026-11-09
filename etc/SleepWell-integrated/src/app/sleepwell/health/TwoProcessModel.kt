package app.sleepwell.health

import kotlin.math.*
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.math.absoluteValue

/**
 * Two-Process 모델 (Daan et al., 1984) — 기준 Python 코드(two_process_model_demo_corrected.py)와 일치.
 *
 * Process S (수면 압력): 각성 시 증가, 수면 시 감소
 *   - 수면: S = sLower + (S_prev - sLower) * exp(-dt_h / tauSleepH)
 *   - 각성: S = sUpper - (sUpper - S_prev) * exp(-dt_h / tauWakeH)
 *   - i번째 S는 (i-1)번째 epoch의 수면 여부로 [t(i-1), t(i)] 구간 계산
 *
 * Process C_sleep (일주기 리듬, 시계 시각 기준):
 *   - clockH = epoch 시각(Asia/Seoul)의 시 + 분/60 + 초/3600
 *   - phaseRef = 연속 수면 구간(bout) 중간 시각(시계 시각)의 원형 평균
 *   - C_sleep = amplitude * cos(2π * (clockH - phaseRef) / 24)
 *   - 수면 중간 시각에서 +최대(수면 쪽), 반대편에서 -최대
 *
 * Sleep propensity = S + C_sleep (클수록 잠들기 쉬움)
 */
object TwoProcessModel {

    // ────────────────────────────────────────────────
    // 기준 코드 파라미터
    // ────────────────────────────────────────────────
    private const val TAU_WAKE_H: Double = 18.2    // 각성 시간 상수 (시간)
    private const val TAU_SLEEP_H: Double = 4.2    // 수면 시간 상수 (시간)
    private const val S_UPPER: Double = 1.0
    private const val S_LOWER: Double = 0.0
    private const val INITIAL_S: Double = 0.5
    private const val AMPLITUDE: Double = 0.15
    private const val TAU_C_HOURS: Double = 24.0   // 일주기 주기 (시간)

    // ────────────────────────────────────────────────
    // 1. 과거~현재 S·C_sleep·propensity 계산
    // ────────────────────────────────────────────────

    /**
     * epoch 타임라인으로부터 S, C_sleep, sleepPropensity 시계열과 현재 값을 계산.
     *
     * - i번째 S는 (i-1)번째 epoch의 수면 여부로 [t(i-1), t(i)] 구간 계산
     * - C는 시계 시각(Asia/Seoul) 기준, phaseRef는 수면 bout 중간 시각의 원형 평균
     * - epoch 0: S = INITIAL_S, C는 epoch 0의 clockH 기준 (S 업데이트 전 기록)
     */
    fun compute(
        timestampsMs: List<Long>,
        isSleep: List<Boolean>
    ): ProcessSCOutput {
        if (timestampsMs.isEmpty()) {
            return ProcessSCOutput(
                history = emptyList(),
                currentS = INITIAL_S,
                currentC = 0.0,
                currentPropensity = INITIAL_S,
                lastTimestampMs = 0L
            )
        }

        // phaseRef 계산: 수면 bout 중간 시각(clockH)의 원형 평균
        val phaseRef = calculatePhaseRef(timestampsMs, isSleep)

        val history = mutableListOf<SCPoint>()
        var s = INITIAL_S
        val firstTs = timestampsMs.first()

        for (i in timestampsMs.indices) {
            val tsMs = timestampsMs[i]
            val sleeping = isSleep[i]
            val clockH = clockHours(tsMs)

            // epoch 간격 (시간) — i=0이면 0, i≥1이면 [t(i-1), t(i)]
            val dtH = if (i > 0) {
                (timestampsMs[i] - timestampsMs[i - 1]) / 3_600_000.0
            } else {
                0.0
            }

            // S 업데이트: (i-1)번째 epoch의 수면 여부로 [t(i-1), t(i)] 구간 (기준 코드와 동일)
            if (dtH > 0.0) {
                s = if (isSleep[i - 1]) {
                    // 수면: S 감소 (앞서 epoch(i-1)에서 잠들었음)
                    S_LOWER + (s - S_LOWER) * exp(-dtH / TAU_SLEEP_H)
                } else {
                    // 각성: S 증가
                    S_UPPER - (S_UPPER - s) * exp(-dtH / TAU_WAKE_H)
                }
            }

            // C는 clockH 기준 (S 업데이트 후, epoch 시작 시점 기록)
            val cSleep = AMPLITUDE * cos(2.0 * PI * (clockH - phaseRef) / TAU_C_HOURS)

            // epoch 시작 시점 S값 기록 (S 업데이트 후)
            history.add(SCPoint(
                timestampMs = tsMs,
                S = s,
                C = cSleep,
                propensity = s + cSleep
            ))
        }

        val lastTimestampMs = timestampsMs.last()
        val lastClockH = clockHours(lastTimestampMs)
        val finalCSleep = AMPLITUDE * cos(2.0 * PI * (lastClockH - phaseRef) / TAU_C_HOURS)
        val finalS = s
        val finalPropensity = finalS + finalCSleep

        return ProcessSCOutput(
            history = history,
            currentS = finalS,
            currentC = finalCSleep,
            currentPropensity = finalPropensity,
            lastTimestampMs = lastTimestampMs,
            phaseRef = phaseRef
        )
    }

    /**
     * 현재 시점(epoch 마지막 시각)부터 미래 24시간 S 변화 예측.
     *
     * - S: 미래는 각성 가정 (S 증가)
     * - C_sleep: clockH 기준, compute()와 동일한 phaseRef 사용
     */
    fun predictFutureS(
        lastS: Double,
        lastTimestampMs: Long,
        phaseRef: Double,
        futureIntervalMin: Int = 15
    ): List<FutureSPoint> {
        val result = mutableListOf<FutureSPoint>()
        val intervalMs = futureIntervalMin * 60_000L
        var s = lastS
        val thresholdMs = 24 * 60 * 60_000L // 24시간 (ms)

        var cursor = lastTimestampMs
        while (cursor < lastTimestampMs + thresholdMs) {
            val clockH = clockHours(cursor)
            val cSleep = AMPLITUDE * cos(2.0 * PI * (clockH - phaseRef) / TAU_C_HOURS)

            result.add(FutureSPoint(
                timestampMs = cursor,
                predictedS = s,
                predictedC = cSleep,
                predictedPropensity = s + cSleep
            ))

            // 미래 각성 가정: S 증가
            s = S_UPPER - (S_UPPER - s) * exp(-(futureIntervalMin.toDouble() / 60.0) / TAU_WAKE_H)
            cursor += intervalMs
        }

        return result
    }

    // ────────────────────────────────────────────────
    // 내부 계산 함수
    // ────────────────────────────────────────────────

    /**
     * epoch timestamp(ms)의 Asia/Seoul 시계 시각(hour).
     * clockH = 시 + 분/60 + 초/3600
     *
     * [통합 수정 2026-10-02 소빈 반영 — 원래 2026-09-25 지적]
     * 아래에서 `+` 를 **줄 맨 앞**에 두면 Kotlin 이 그 줄을 새 문장(단항 +)으로 읽는다.
     * 결과적으로 `return` 은 시(hour)만 돌려주고 분/60 · 초/3600 은 조용히 버려진다.
     *   → C_sleep 가 매시간 계단처럼 바뀌고(분 단위 변화가 사라짐),
     *     calculatePhaseRef 의 bout 중간 시각도 정시로 뭉개진다.
     * 실측(5/5 불일치): 12:24 → 저장소형 12.000000 / 정상 12.400000, 12:24:30 → 12.408333
     * 연산자를 줄 **끝**에 두어야 이어진 식이 된다. 아래 형태가 그걸 한다.
     */
    private fun clockHours(tsMs: Long): Double {
        val instant = Instant.ofEpochMilli(tsMs)
        val zone = ZoneId.of("Asia/Seoul")
        val local = instant.atZone(zone)
        return local.hour.toDouble() +
            local.minute.toDouble() / 60.0 +
            local.second.toDouble() / 3600.0
    }

    /**
     * 연속 수면 bout(잠든 구간)들의 중간 시각(clockH)의 원형 평균.
     *
     * - isSleep=true인 연속된 구간을 하나의 bout으로 묶음
     * - 각 bout의 중간 시각 = (boutStartMs + boutEndMs) / 2 의 clockH
     * - 원형 평균: mean_sin = Σ sin(h*2π/24) / n, mean_cos = Σ cos(h*2π/24) / n
     *   phaseRef = atan2(mean_sin, mean_cos) * 24 / (2π)
     */
    private fun calculatePhaseRef(timestampsMs: List<Long>, isSleep: List<Boolean>): Double {
        // 수면 bout 찾기 (연속된 isSleep=true 구간)
        val boutMidpointsH = mutableListOf<Double>()
        var inBout = false
        var boutStartTs: Long? = null

        for (i in timestampsMs.indices) {
            if (isSleep[i]) {
                if (!inBout) {
                    boutStartTs = timestampsMs[i]
                    inBout = true
                }
            } else {
                if (inBout) {
                    val boutEndTs = timestampsMs[i - 1]   // 마지막 수면 epoch (기준 코드와 동일)
                    val midTs = (boutStartTs!! + boutEndTs) / 2
                    boutMidpointsH.add(clockHours(midTs))
                    boutStartTs = null
                    inBout = false
                }
            }
        }
        // bout이 진행 중이면 마지막 bout 처리
        if (inBout && boutStartTs != null) {
            val lastTs = timestampsMs.last()
            val midTs = (boutStartTs + lastTs) / 2
            boutMidpointsH.add(clockHours(midTs))
        }

        if (boutMidpointsH.isEmpty()) {
            return 0.0
        }

        // 원형 평균 (24시간 기준)
        var sumSin = 0.0
        var sumCos = 0.0
        val n = boutMidpointsH.size.toDouble()
        for (h in boutMidpointsH) {
            val rad = h * 2.0 * PI / TAU_C_HOURS
            sumSin += sin(rad)
            sumCos += cos(rad)
        }
        val meanSin = sumSin / n
        val meanCos = sumCos / n
        // atan2 결과로 나온 값 (-π ~ π)을 0~24 범위로 변환
        var phaseRad = atan2(meanSin, meanCos)
        if (phaseRad < 0) phaseRad += 2.0 * PI
        return phaseRad * TAU_C_HOURS / (2.0 * PI)
    }

    // ────────────────────────────────────────────────
    // 데이터 클래스
    // ────────────────────────────────────────────────

    /** S·C_sleep 시계열 한 지점 */
    data class SCPoint(
        val timestampMs: Long,
        val S: Double,
        val C: Double,
        val propensity: Double   // S + C_sleep
    )

    /** 모델 계산 결과 */
    data class ProcessSCOutput(
        val history: List<SCPoint>,       // 과거~현재 S·C_sleep·propensity 시계열
        val currentS: Double,              // 현재 Process S (0~1)
        val currentC: Double,              // 현재 C_sleep (시계 시각 기준)
        val currentPropensity: Double,     // 현재 S + C_sleep
        val lastTimestampMs: Long,         // 마지막 epoch 시각
        val phaseRef: Double = 0.0        // 수면 bout 중간 시각의 원형 평균 (clockH)
    )

    /** 미래 S 예측 한 지점 */
    data class FutureSPoint(
        val timestampMs: Long,
        val predictedS: Double,
        val predictedC: Double,
        val predictedPropensity: Double
    )
}
