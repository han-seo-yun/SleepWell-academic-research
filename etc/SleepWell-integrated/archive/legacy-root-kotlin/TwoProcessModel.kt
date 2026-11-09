package app.sleepwell.health

import kotlin.math.*

/**
 * Two-Process 모델 (Daan et al., 1984) — Kotlin 이식 (임시 모델)
 *
 * Process S (수면 압력): 각성 시 증가, 수면 시 감소
 *   - 각성: dS/dt = (1 - S) / tau_w
 *   - 수면:  dS/dt = -S / tau_s
 *
 * Process C (일주기 리듬): 24시간 코사인 파형
 *   - C(t) = A * cos(2π * (t - φ) / T_c)
 *
 * Sleep propensity = S + C
 *
 * 주의:
 *   - 파라미터는 코드 기본값 사용. 확정값이 아니므로 "임시 모델"로 표시.
 *   - pandas 3.x 마이크로초 문제(팀 공유)와 유사하게, epoch ms 기준 계산 시
 *     단위 일관성에 주의. (Python 코드 가정: 나노초 → Kotlin: 밀리초 직접 사용)
 *   - futureS(미래 예측)와 권장 시각은 이 모델에 없음 → "준비 중" 처리.
 */
object TwoProcessModel {

    // ────────────────────────────────────────────────
    // 기본 파라미터 (임시 모델용 코드 기본값)
    // ────────────────────────────────────────────────
    private const val S0: Double = 0.3       // 초기 수면 압력 (0~1)
    private const val TAU_W: Double = 166.67  // 각성 시 시간 상수 (분) ≈ 2.78시간
    private const val TAU_S: Double = 40.0    // 수면 시 시간 상수 (분) ≈ 0.67시간
    private const val C_AMPLITUDE: Double = 0.2   // 일주기 진폭
    private const val C_PHASE_SHIFT: Double = 0.0 // 위상각 보정 (시간 단위, 자정 기준)
    private const val T_C: Double = 1440.0    // 일주기 주기 (분) = 24시간

    // ────────────────────────────────────────────────
    // 1. 과거~현재 S·C·propensity 계산
    // ────────────────────────────────────────────────

    /**
     * epoch 타임라인으로부터 S, C 시계열과 현재 값을 계산.
     */
    fun compute(
        timestampsMs: List<Long>,
        isSleep: List<Boolean>
    ): ProcessSCOutput {
        if (timestampsMs.isEmpty()) {
            return ProcessSCOutput(
                history = emptyList(),
                currentS = S0,
                currentC = 0.0,
                currentPropensity = S0,
                lastTimestampMs = 0L
            )
        }

        val history = mutableListOf<SCPoint>()
        var s = S0
        val firstTs = timestampsMs.first()

        for (i in timestampsMs.indices) {
            val tsMs = timestampsMs[i]
            val sleeping = isSleep[i]

            // epoch 간격 (분) 계산
            val dtMin = if (i > 0) {
                (timestampsMs[i] - timestampsMs[i - 1]) / 60000.0
            } else {
                0.0
            }

            // epoch 중간 시각 (분 단위, epoch 시작 기준 상대 시간)
            val tMin = (tsMs - firstTs) / 60000.0

            // Process C 계산 (epoch 중간 시각 기준)
            val c = computeC(tMin)

            // epoch 시작 시점 S값 기록 (isSleep 적용 전)
            history.add(SCPoint(
                timestampMs = tsMs,
                S = s,
                C = c,
                propensity = s + c
            ))

            // isSleep 상태 반영하여 S 업데이트 (epoch 구간 평균)
            if (dtMin > 0) {
                s = if (sleeping) {
                    // 수면: S 감소
                    s * exp(-dtMin / TAU_S)
                } else {
                    // 각성: S 증가
                    1.0 - (1.0 - s) * exp(-dtMin / TAU_W)
                }
            }
        }

        val lastTimestampMs = timestampsMs.last()
        val lastTMin = (lastTimestampMs - firstTs) / 60000.0
        val finalC = computeC(lastTMin)
        val finalS = s
        val finalPropensity = finalS + finalC

        return ProcessSCOutput(
            history = history,
            currentS = finalS,
            currentC = finalC,
            currentPropensity = finalPropensity,
            lastTimestampMs = lastTimestampMs
        )
    }

    /**
     * 현재 시점(epoch 마지막 시각)부터 미래 24시간 S 변화 예측.
     *
     * 주의: 권장 시각 계산은 이 모델에 없음. "준비 중" 처리.
     * 여기서는 S 변화 추이만 예측. 미래 입력이 없으면 각성 상태(S 증가)로 가정.
     */
    fun predictFutureS(
        lastS: Double,
        lastTimestampMs: Long,
        futureIntervalMin: Int = 15 // 15분 간격 (그래프 해상도 결정)
    ): List<FutureSPoint> {
        val result = mutableListOf<FutureSPoint>()
        val intervalMs = futureIntervalMin * 60000L
        var s = lastS
        val thresholdMs = 24 * 60 * 60000L // 24시간 (ms)

        var cursor = lastTimestampMs
        while (cursor < lastTimestampMs + thresholdMs) {
            val tMin = (cursor - lastTimestampMs) / 60000.0
            val c = computeCFromMs(cursor - lastTimestampMs)

            result.add(FutureSPoint(
                timestampMs = cursor,
                predictedS = s,
                predictedC = c,
                predictedPropensity = s + c
            ))

            // 미래는 각성 상태(S 증가)로 가정 (기본 시나리오)
            s = 1.0 - (1.0 - s) * exp(-(futureIntervalMin.toDouble()) / TAU_W)
            cursor += intervalMs
        }

        return result
    }

    // ────────────────────────────────────────────────
    // 내부 계산 함수
    // ────────────────────────────────────────────────

    /** Process C 계산 (상대 분 단위 시각 기준) */
    private fun computeC(tMin: Double): Double {
        val phaseRad = 2.0 * PI * (tMin - C_PHASE_SHIFT * 60.0) / T_C
        return C_AMPLITUDE * cos(phaseRad)
    }

    /** Process C 계산 (절대 ms 기준, 현재 시각 기준 상대) */
    private fun computeCFromMs(elapsedMs: Long): Double {
        val tMin = elapsedMs / 60000.0
        return computeC(tMin)
    }

    // ────────────────────────────────────────────────
    // 데이터 클래스
    // ────────────────────────────────────────────────

    /** S·C 시계열 한 지점 */
    data class SCPoint(
        val timestampMs: Long,
        val S: Double,
        val C: Double,
        val propensity: Double   // S + C
    )

    /** 모델 계산 결과 */
    data class ProcessSCOutput(
        val history: List<SCPoint>,       // 과거~현재 S·C·propensity 시계열
        val currentS: Double,              // 현재 Process S (0~1)
        val currentC: Double,              // 현재 Process C
        val currentPropensity: Double,     // 현재 S + C
        val lastTimestampMs: Long          // 마지막 epoch 시각
    )

    /** 미래 S 예측 한 지점 */
    data class FutureSPoint(
        val timestampMs: Long,
        val predictedS: Double,
        val predictedC: Double,
        val predictedPropensity: Double
    )
}
