package app.sleepwell.health

/**
 * 수면 분석 결과 전체 데이터 구조. — epoch 1분 버전
 *
 * 채윤(백엔드) → 소빈(프론트) 직접 전달용 (같은 Kotlin 앱 내, 객체 직접 전달).
 *
 * futureS · recommendation은 현재 모델에 없으므로 null 허용 ("준비 중" 표시).
 *
 * 이번 주 변경: 아래 4개 필드 추가 (epochIntervalMs / epochIntervalLabel /
 * inputPeriodStartMs / inputPeriodEndMs).
 *   모두 기본값이 있어서 소빈 화면 코드 수정이 필요 없다.
 *   같은 화면에서 30초·1분 결과가 함께 나올 수 있게 된 만큼,
 *   "이 값이 어느 간격·어느 구간에서 계산된 것인지" 결과 객체가 스스로 들고 있어야 한다.
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

    /**
     * 과거~현재 S·C·propensity 시계열.
     * 그래프 해상도: 계산은 epoch 간격(30초 또는 1분), 전달·그래프는 10~15분 간격으로 필터 가능.
     * → 1분 버전에서 전달 점 개수가 절반이 된다. 화면에서 필터 기준(분 단위)은 그대로 두면 된다.
     */
    val scHistory: List<PropensityPoint>,

    /** 미래 24시간 S 예측 시계열. 현재는 null ("준비 중" — 서윤 언니 담당). */
    val futureS: List<PredictedPoint>?,

    /** 권장 취침·기상·수면시간. 현재는 null ("준비 중" — 서윤 언니 담당). */
    val recommendation: Recommendation?,

    /** 계산에 사용한 모델 버전 표시 ("Two-Process Kotlin 모델 · epoch 1분" 등) */
    val modelVersion: String,

    /** 계산 수행 시각 (KST epoch ms) */
    val calculationTimeMs: Long,

    // ── 이번 주 추가 (epoch 30초 → 1분) ──────────────────────────────

    /**
     * 이 결과를 계산할 때 쓴 epoch 간격 (ms). 30000 또는 60000.
     *
     * 왜 결과 객체에 넣는가:
     *   30초와 1분이 같은 화면에 함께 표시될 수 있다. 간격이 결과에 없으면
     *   그래프 x축 눈금·점 개수를 화면이 추측해야 해서 서로 다른 계산이 같은 그래프로
     *   그려진다. 소요시간 계산(epoch 수 × 간격)과 점 개수 표시에 바로 쓰인다.
     */
    val epochIntervalMs: Long = 60_000L,

    /** epoch 간격 표시 라벨 ("30초" / "1분"). 화면 표기용. */
    val epochIntervalLabel: String = "1분",

    /** 계산에 사용한 타임라인 시작 시각 (KST epoch ms). 모델 입력 구간의 앞쪽 끝. */
    val inputPeriodStartMs: Long = 0L,

    /** 계산에 사용한 타임라인 종료 시각 (KST epoch ms). 모델 입력 구간의 뒤쪽 끝. */
    val inputPeriodEndMs: Long = 0L
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
    val recommendedSleepMin: Double?,

    /** Two-Process 모델이 고른 기준 취침·기상 시각. */
    val physicalBedTimeMs: Long? = null,
    val physicalWakeTimeMs: Long? = null,

    /** 물리 기준에 Ridge 잔차를 더한 개인화 예측 시각. */
    val predictedBedTimeMs: Long? = null,
    val predictedWakeTimeMs: Long? = null,
    val bedtimeResidualMin: Double? = null,
    val wakeResidualMin: Double? = null,

    /** 화면에서 미학습 fallback과 학습 완료 모델을 혼동하지 않게 하는 표시값. */
    val residualModelVersion: String? = null,
    val modelStatus: String = "physics_only_untrained",
    val usedFallback: Boolean = true
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
