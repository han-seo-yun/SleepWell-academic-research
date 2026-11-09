package app.sleepwell.analysis

/**
 * 피로도 원점수와 모델 입력 척도를 분리한다.
 *
 * 7점→5점 변환은 과거 기록을 현재 엔진에 넣기 위한 호환 규칙일 뿐,
 * 두 척도의 심리측정적 동등성이나 예측 정확도 개선을 뜻하지 않는다.
 */
object FatigueScale {
    const val LEGACY_MAX = 7
    const val CURRENT_MAX = 5

    fun toEngine(raw: Int?, scaleMax: Int): Int? = when (scaleMax) {
        LEGACY_MAX -> when (raw) {
            1 -> 1; 2 -> 2; 3 -> 2; 4 -> 3; 5 -> 4; 6 -> 4; 7 -> 5; else -> null
        }
        CURRENT_MAX -> raw?.takeIf { it in 1..CURRENT_MAX }
        else -> null
    }

    fun validRaw(raw: Int?, scaleMax: Int): Boolean =
        raw == null || when (scaleMax) {
            LEGACY_MAX, CURRENT_MAX -> raw in 1..scaleMax
            else -> false
        }

    /** 내부 sentinel이나 미지원 분모를 사용자 화면에 노출하지 않는다. */
    fun displayText(raw: Int, scaleMax: Int?): String = when (scaleMax) {
        LEGACY_MAX -> "피로도 $raw/7 (이전 척도)"
        CURRENT_MAX -> "피로도 $raw/5"
        else -> "피로도 $raw (척도 확인 필요)"
    }
}
