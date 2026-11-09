package app.sleepwell.health

/**
 * 수면 분석 결과 전체 데이터 구조.
 *
 * 채윤(백엔드) → 소빈(프론트) 직접 전달용 (같은 Kotlin 앱 내, 객체 직접 전달).
 *
 * futureS · recommendation은 현재 모델에 없으므로 null 허용 ("준비 중" 표시).
 */
data class AnalysisResult(
    /** 현재 Process S (수면 압력, 0~1). 0=완전히 rested, 1=극도로 졸림 */
    val currentS: Double,

    /** 현재 C_sleep (일주기 리듬, 시계 시각 기준). 수면 중간 시각에서 +최대(수면 쪽), 반대편에서 -최대 */
    val currentC: Double,

    /** 현재 수면 성향 (S + C_sleep). 클수록 잠들기 쉬움 */
    val currentPropensity: Double,

    /** 최근 수면 기록 요약. 모델 기준 최근 7일, 기록 화면은 전체 표시. */
    val sleepRecords: List<SleepRecord>,

    /** 과거~현재 S·C·propensity 시계열. 그래프 해상도: 계산 30초, 전달 10~15분 간격으로 필터 가능. */
    val scHistory: List<PropensityPoint>,

    /** 미래 24시간 S 예측 시계열. 현재는 null ("준비 중" — 서윤 언니 담당). */
    val futureS: List<PredictedPoint>?,

    /** 권장 취침·기상·수면시간. 현재는 null ("준비 중" — 서윤 언니 담당). */
    val recommendation: Recommendation?,

    /** 계산에 사용한 모델 버전 표시 ("임시 Two-Process Kotlin 모델" 등) */
    val modelVersion: String,

    /** 계산 수행 시각 (KST epoch ms) */
    val calculationTimeMs: Long
)

// ────────────────────────────────────────────────
// 수면 기록 (세션 요약)
// ────────────────────────────────────────────────

data class SleepRecord(
    /** 세션 고유 ID */
    val sessionId: String,

    /** 세션 시작 시각 (KST epoch ms) */
    val startMs: Long,

    /** 세션 종료 시각 (KST epoch ms) */
    val endMs: Long,

    /** 총 수면 시간 (분). WAKE 구간 제외한 수면 단계 구간 합계 */
    val totalSleepMin: Double,

    /** 단계별 지속 시간 (분). 키: LIGHT / DEEP / REM / WAKE */
    val stageSummary: Map<String, Double>
)

// ────────────────────────────────────────────────
// S·C·propensity 시계열
// ────────────────────────────────────────────────

data class PropensityPoint(
    /** 해당 시점 시각 (KST epoch ms) */
    val timestampMs: Long,

    /** Process S 값 */
    val S: Double,

    /** Process C 값 */
    val C: Double,

    /** 수면 성향 (S + C) */
    val propensity: Double
)

// ────────────────────────────────────────────────
// 미래 S 예측
// ────────────────────────────────────────────────

data class PredictedPoint(
    /** 예측 시점 시각 (KST epoch ms) */
    val timestampMs: Long,

    /** 예측 Process S 값 */
    val predictedS: Double,

    /** 예측 Process C 값 (참고) */
    val predictedC: Double,

    /** 예측 수면 성향 (S + C) */
    val predictedPropensity: Double
)

// ────────────────────────────────────────────────
// 권장 사항
// ────────────────────────────────────────────────

data class Recommendation(
    /** 권장 취침 시각 (KST epoch ms). 현재 모델에는 없어 null 가능. */
    val recommendedBedTimeMs: Long?,

    /** 권장 기상 시각 (KST epoch ms). 현재 모델에는 없어 null 가능. */
    val recommendedWakeTimeMs: Long?,

    /** 권장 수면시간 (분). 현재 모델에는 없어 null 가능. */
    val recommendedSleepMin: Double?
)

// ────────────────────────────────────────────────
// 사용자 직접 입력 (선택)
// ────────────────────────────────────────────────

data class DirectInput(
    /** 취침 시각 (KST epoch ms). CSV 없을 때 또는 보완 입력. */
    val sleepStartMs: Long?,

    /** 기상 시각 (KST epoch ms). CSV 없을 때 또는 보완 입력. */
    val sleepEndMs: Long?,

    /** 목표 기상 시각 (선택, KST epoch ms). 입력 시 조건부 계산 트리거. */
    val targetWakeMs: Long?,

    /** 예정 취침 시각 (선택, KST epoch ms). 입력 시 조건부 계산 트리거. */
    val plannedBedMs: Long?
)
