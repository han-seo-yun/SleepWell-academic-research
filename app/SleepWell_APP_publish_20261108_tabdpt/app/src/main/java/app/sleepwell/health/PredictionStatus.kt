package app.sleepwell.health

import java.time.Instant
import java.time.ZoneId

/*
 * 결과 상자에 들어가는 "기록 일수 · 개인화 단계 · 예측 불가/제한 이유 코드" (채윤 · 2026-10-24)
 *
 * 화면(소빈)은 이 코드를 문장으로 바꿔 보여 주고, 웹앱도 같은 코드를 쓰면 두 환경의 안내가 맞는다.
 * 코드는 영문 대문자 상수라 화면 문구가 바뀌어도 테스트·로그·저장 기록이 흔들리지 않는다.
 */

/** HealthResult.Unavailable 의 code — 결과를 아예 못 낸 이유 */
object ReasonCode {
    const val NO_SOURCE = "NO_SOURCE"                 // 파일이 지정되지 않음
    const val NO_SLEEP_RECORDS = "NO_SLEEP_RECORDS"   // CSV 에 수면 기록이 없음
    const val NEED_MORE_RECORDS = "NEED_MORE_RECORDS" // 수면/각성이 모두 있는 구간이 부족 (모델 입력 불가)
    const val OUT_OF_MEMORY = "OUT_OF_MEMORY"
    const val WAKE_OUT_OF_RANGE = "WAKE_OUT_OF_RANGE" // 직접 입력 기상 시각이 허용 범위 밖
    const val UNAVAILABLE = "UNAVAILABLE"             // 기본값 (분류 안 된 경우)
}

/** AnalysisResult.predictionLimitations — 결과는 나왔지만 예측·권장이 제한되는 이유 */
object LimitCode {
    const val FEW_RECORDS = "FEW_RECORDS"                 // 기록 일수가 1단계(14일) 미만 → 초기 단계
    const val NO_TARGET_WAKE = "NO_TARGET_WAKE"           // 목표 기상 없음 → 권장 = 예상 (물리 기준)
    const val RIDGE_UNTRAINED = "RIDGE_UNTRAINED"         // 개인 보정(Ridge) 미학습 → 설문이 숫자를 안 바꿈
    const val NO_SURVEY_FEATURES = "NO_SURVEY_FEATURES"   // 설문 입력 없음
}

/** 개인화 단계. 기준 일수는 서윤 미래예측모델 PERSONALIZATION_PROTOCOL 후보값(확정 전) — 소빈 화면과 같은 14/30일 */
object PersonalizationStage {
    const val INITIAL = "initial"   // 기록 14일 미만
    const val STAGE1 = "stage1"     // 14일 이상
    const val STAGE2 = "stage2"     // 30일 이상
    const val STAGE1_DAYS = 14
    const val STAGE2_DAYS = 30

    fun of(recordDays: Int): String = when {
        recordDays >= STAGE2_DAYS -> STAGE2
        recordDays >= STAGE1_DAYS -> STAGE1
        else -> INITIAL
    }
}

internal object PredictionStatus {
    /** 서로 다른 기기 시간대의 기상 날짜 수. 같은 날의 여러 기록은 하루로 센다. */
    fun recordDays(records: List<SleepRecord>, calculationZone: ZoneId = ZoneId.of("Asia/Seoul")): Int =
        records.map { Instant.ofEpochMilli(it.endMs).atZone(calculationZone).toLocalDate() }.distinct().size

    fun limitations(
        recordDays: Int,
        directInput: DirectInput?,
        residualModel: RidgeResidualModel?,
        features: Map<String, Double?>
    ): List<String> = buildList {
        if (recordDays < PersonalizationStage.STAGE1_DAYS) add(LimitCode.FEW_RECORDS)
        if (directInput?.targetWakeMs == null) add(LimitCode.NO_TARGET_WAKE)
        if (residualModel?.status != "trained") add(LimitCode.RIDGE_UNTRAINED)
        if (features.values.all { it == null }) add(LimitCode.NO_SURVEY_FEATURES)
    }
}
