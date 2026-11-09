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
 *  - 수면 단계 코드는 팀 공통 기준(0=W,1=N1,2=N2,3=N3,4=REM)을 그대로 쓴다.
 *    그래야 export 결과가 기존 평가코드(평가코드.py)에 바로 들어간다.
 */

const val EPOCH_MS = 30_000L   // 팀 공통 격자: 30초

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
    WAKE(0, "W"),
    N1(1, "N1"),
    N2(2, "N2"),
    N3(3, "N3"),
    REM(4, "REM"),

    /**
     * 매핑 실패. code = -1 이라 평가코드의 "0~4 밖 값은 채점 제외" 규칙에 자동으로 걸린다.
     * 없는 정보를 그럴듯한 값으로 채우지 않기 위해 일부러 남겨 둔 상태값이다.
     */
    UNKNOWN(-1, "UNKNOWN");

    val isSleep: Boolean get() = this != WAKE && this != UNKNOWN

    companion object {
        fun fromCode(code: Int): SleepStage = entries.firstOrNull { it.code == code } ?: UNKNOWN
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
    data class Unavailable(val reason: String) : HealthResult<Nothing>
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
object TimeFmt {
    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US)
    private val stamp = SimpleDateFormat("yyyyMMdd_HHmm", Locale.US)
    private val dayOnly = SimpleDateFormat("yyyy-MM-dd", Locale.US)
    private val timeOnly = SimpleDateFormat("HH:mm", Locale.US)

    fun iso(ms: Long): String = iso.format(Date(ms))
    fun stamp(ms: Long): String = stamp.format(Date(ms))
    fun day(ms: Long): String = dayOnly.format(Date(ms))
    fun hhmm(ms: Long): String = timeOnly.format(Date(ms))

    /** meta.json 에 기록할 타임존 ID (예: Asia/Seoul) */
    fun zoneId(): String = TimeZone.getDefault().id

    /** 밀리초 → "7시간 24분" */
    fun durationText(ms: Long): String {
        val totalMin = ms / 60_000L
        return "${totalMin / 60}시간 ${totalMin % 60}분"
    }
}
