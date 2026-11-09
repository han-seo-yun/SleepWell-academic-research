package app.sleepwell.health

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/*
 * 앱 전체가 공유하는 데이터 모델.
 *
 * 설계 원칙
 *  - Samsung Health SDK 타입을 여기까지 끌고 오지 않는다. SDK 결과는 SamsungHealthSource
 *    안에서만 쓰고, 밖으로는 이 파일의 타입으로만 내보낸다.
 *  - 모든 시각은 Long = epoch milliseconds. java.time 을 쓰면 API 26 미만에서
 *    core library desugaring(Gradle 설정)이 필요해지므로 의도적으로 피했다.
 *  - 수면 단계는 워치(삼성헬스) 4단계 AWAKE/LIGHT/DEEP/REM 을 그대로 쓴다. (2026-09-25 변경)
 *    근거: 1순위 md #4 "Galaxy Watch sleep stage 는 AWAKE/LIGHT/DEEP/REM 으로 통일",
 *          #2 "PSG(W/N1/N2/N3/REM)와 비교할 때만 4-stage mapping 사용".
 *    삼성헬스 원본에는 Light 코드(40002)가 하나뿐이라 N1/N2 를 구분할 정보가 없다.
 *    → PSG 5단계 코드(0~4)와 섞이지 않도록 숫자 코드는 워치 전용(0~3)으로 따로 둔다.
 */

/**
 * 팀 공통 격자: 1분 (2026-09-26 회의 결정 — 삼성 데이터 기본 단위가 1분이므로 데이터셋·앱 모두 1분으로 통일).
 * 30초 버전은 `통합 v2` 폴더에 그대로 보관한다.
 */
const val EPOCH_MS = 60_000L

/** 화면 문구용 칸 길이 표기 */
const val EPOCH_TEXT = "1분"

// ─────────────────────────────────────────────────────────────
// 기기 정보 — "이 데이터가 Watch 5에서 나온 게 맞나" 판단 근거
// ─────────────────────────────────────────────────────────────
enum class DeviceType { WATCH, PHONE, MANUAL_INPUT, UNKNOWN }

data class DeviceInfo(
    val sourceName: String,
    val deviceType: DeviceType,
    /** SDK가 준 원본 식별자. 필터링 근거를 버리지 않기 위해 그대로 보존한다. */
    val rawSourceId: String? = null
)

// ─────────────────────────────────────────────────────────────
// 센서 표본
// ─────────────────────────────────────────────────────────────
data class HeartRateSample(
    val timeMs: Long,
    val bpm: Float,
    val device: DeviceInfo? = null
)

/** 걸음은 시점값이 아니라 구간 합계로 오는 경우가 많아 start/end 를 둘 다 둔다. */
data class StepSample(
    val startMs: Long,
    val endMs: Long,
    val count: Int,
    val device: DeviceInfo? = null
)

// ─────────────────────────────────────────────────────────────
// 수면
// ─────────────────────────────────────────────────────────────
enum class SleepStage(val code: Int, val label: String) {
    // code = 워치 4단계 전용 번호(export 용). PSG 평가코드의 stage_label(0=W,1=N1,…)과 다른 체계이므로
    // 같은 열에 섞지 말 것. (예: LIGHT=1 을 PSG 코드로 읽으면 N1 로 오해된다)
    // enum 이름 WAKE 는 채윤 코드 호환을 위해 유지하고, 사람이 읽는 표기는 "AWAKE".
    WAKE(0, "AWAKE"),
    LIGHT(1, "LIGHT"),
    DEEP(2, "DEEP"),
    REM(3, "REM"),

    /**
     * 매핑 실패. code = -1. 없는 정보를 그럴듯한 값으로 채우지 않기 위해 일부러 남겨 둔 상태값이다.
     */
    UNKNOWN(-1, "UNKNOWN");

    val isSleep: Boolean get() = this != WAKE && this != UNKNOWN

    companion object {
        fun fromCode(code: Int): SleepStage = entries.firstOrNull { it.code == code } ?: UNKNOWN

        /** 화면 표시 순서 (hypnogram 위→아래) */
        val DISPLAY_ORDER = listOf(WAKE, REM, LIGHT, DEEP)
    }
}

data class SleepStageSegment(
    val startMs: Long,
    val endMs: Long,
    val stage: SleepStage
)

/** 하룻밤 = 세션 1개. stages 가 비어 있으면 "잠든 구간만 알고 단계는 모름"이라는 뜻. */
data class SleepSession(
    val sessionId: String,
    val startMs: Long,
    val endMs: Long,
    val stages: List<SleepStageSegment> = emptyList(),
    val device: DeviceInfo? = null
) {
    val durationMs: Long get() = endMs - startMs
}

// ─────────────────────────────────────────────────────────────
// 조회 조건
// ─────────────────────────────────────────────────────────────
data class HealthQuery(
    val startMs: Long,
    val endMs: Long,
    /** Galaxy Watch 소스만 남기기. 폰 자체 기록·수동 입력 제외. */
    val watchOnly: Boolean = true
)

// ─────────────────────────────────────────────────────────────
// 결과 타입
//   건강 데이터는 실패 경로가 많다(권한 거부 / Samsung Health 미설치 / 항목 미지원).
//   예외로 던지면 화면에서 "무엇을 안내해야 하는지" 알 수 없으므로 값으로 표현한다.
// ─────────────────────────────────────────────────────────────
sealed interface HealthResult<out T> {
    data class Success<T>(val data: T) : HealthResult<T>
    data object PermissionDenied : HealthResult<Nothing>
    data class Unavailable(val reason: String, val code: String = ReasonCode.UNAVAILABLE) : HealthResult<Nothing>   // code: health/PredictionStatus.kt (채윤 2026-10-24, 기본값 있어 기존 호출 그대로)
    data class Failure(val error: Throwable) : HealthResult<Nothing>
}

/** 실패 사유를 사람이 읽는 한 줄로. 화면에 그대로 띄우기 위한 것. */
fun HealthResult<*>.errorMessageOrNull(): String? = when (this) {
    is HealthResult.Success -> null
    is HealthResult.PermissionDenied -> "건강 데이터 접근 권한이 거부되었습니다."
    is HealthResult.Unavailable -> "사용할 수 없습니다: $reason"
    is HealthResult.Failure -> "조회 실패: ${error.message ?: error::class.java.simpleName}"
}

fun <T> HealthResult<T>.dataOrNull(): T? = (this as? HealthResult.Success)?.data

// ─────────────────────────────────────────────────────────────
// 시각 포맷
//   SimpleDateFormat 의 'Z' 패턴(+0900)은 모든 API 레벨에서 동작한다.
//   pandas 의 to_datetime 도 이 형식을 그대로 읽는다.
// ─────────────────────────────────────────────────────────────
/**
 * 연구 모델·내보내기의 기준 시간대. 화면과 직접 입력은 기기 시간대를 따른다.
 */
val APP_ZONE: TimeZone = TimeZone.getTimeZone("Asia/Seoul")

// ─────────────────────────────────────────────────────────────
// 기기 설정을 읽은 불변 스냅샷. ID가 바뀌면 Compose도 변경을 감지한다.
// APP_ZONE은 기존 export 형식 호환을 위해 고정한다. 입력·계산·표시는 DisplaySettings.zone을 쓴다.
// ─────────────────────────────────────────────────────────────
data class DisplayZone(val zoneId: String) {
    val tz: TimeZone get() = TimeZone.getTimeZone(zoneId)
    val short: String get() = zoneId

    companion object {
        val KST = DisplayZone("Asia/Seoul")
        val UTC = DisplayZone("UTC")
        fun systemDefault(): DisplayZone = DisplayZone(TimeZone.getDefault().id)
    }
}

/** 현재 기기의 표시 시간대. UiZone이 시스템 변경을 반영한다. */
object DisplaySettings {
    @Volatile var zone: DisplayZone = DisplayZone.systemDefault()
}

/** 화면 표시용 시각 포맷 (표시 시간대를 따른다). export·파일 이름은 TimeFmt(APP_ZONE 고정)를 쓴다. */
object ShowFmt {
    private fun f(pattern: String, tz: TimeZone) = SimpleDateFormat(pattern, Locale.KOREA).apply { timeZone = tz }
    fun day(ms: Long, tz: TimeZone = DisplaySettings.zone.tz): String = f("yyyy-MM-dd", tz).format(Date(ms))
    fun hhmm(ms: Long, tz: TimeZone = DisplaySettings.zone.tz): String = f("HH:mm", tz).format(Date(ms))
}

/** export·파일 이름·세션 ID 용 (APP_ZONE 고정 — 표시 시간대를 바꿔도 연구 데이터는 변하지 않는다) */
object TimeFmt {
    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US).apply { timeZone = APP_ZONE }
    private val stamp = SimpleDateFormat("yyyyMMdd_HHmm", Locale.US).apply { timeZone = APP_ZONE }
    private val dayOnly = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = APP_ZONE }
    private val timeOnly = SimpleDateFormat("HH:mm", Locale.US).apply { timeZone = APP_ZONE }

    fun iso(ms: Long): String = iso.format(Date(ms))
    fun stamp(ms: Long): String = stamp.format(Date(ms))
    fun day(ms: Long): String = dayOnly.format(Date(ms))
    fun hhmm(ms: Long): String = timeOnly.format(Date(ms))

    /** meta.json 에 기록할 타임존 ID (예: Asia/Seoul) */
    fun zoneId(): String = APP_ZONE.id

    /** 밀리초 → "7시간 24분" */
    fun durationText(ms: Long): String {
        val totalMin = ms / 60_000L
        return "${totalMin / 60}시간 ${totalMin % 60}분"
    }
}
