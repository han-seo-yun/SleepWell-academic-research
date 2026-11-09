package app.sleepwell.health

/*
 * ★ 검증용 스텁 (앱 빌드에 포함하지 않음)
 *
 * 왜 있는가:
 *   저장소의 src/app/sleepwell/health/에는 채윤 담당 파일 5개만 있고, 앱 전체가 공유하는
 *   타입(HealthModels.kt, HealthDataSource.kt)은 소빈의 Android 프로젝트에 있다.
 *   그 파일이 이 폴더에는 없으므로, epoch 버전을 컴파일해 숫자를 확인하려면 타입이 필요하다.
 *
 *   여기서는 그 앱 파일에서 실제로 쓰이는 정의만 그대로 옮겼다. SleepStage는 앱에 이미
 *   LIGHT/DEEP/REM/WAKE로 반영된 버전을 따른다(저장소 CsvFileSource.kt가 LIGHT/DEEP를 쓴다).
 *
 * 주의:
 *   이 파일은 컴파일·실행 검증용이다. 앱에 붙일 때는 지우고 앱의 HealthModels.kt /
 *   HealthDataSource.kt를 쓰면 된다. 특히 EPOCH_MS(앱 export 격자)는 여기 값을 쓰지 않는다.
 */

enum class DeviceType { WATCH, PHONE, MANUAL_INPUT, UNKNOWN }

data class DeviceInfo(
    val sourceName: String,
    val deviceType: DeviceType,
    val rawSourceId: String? = null
)

data class HeartRateSample(
    val timeMs: Long,
    val bpm: Float,
    val device: DeviceInfo? = null
)

data class StepSample(
    val startMs: Long,
    val endMs: Long,
    val count: Int,
    val device: DeviceInfo? = null
)

enum class SleepStage(val code: Int, val label: String) {
    WAKE(0, "W"),
    LIGHT(1, "LIGHT"),
    DEEP(2, "DEEP"),
    REM(3, "REM"),
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

data class SleepSession(
    val sessionId: String,
    val startMs: Long,
    val endMs: Long,
    val stages: List<SleepStageSegment> = emptyList(),
    val device: DeviceInfo? = null
) {
    val durationMs: Long get() = endMs - startMs
}

data class HealthQuery(
    val startMs: Long,
    val endMs: Long,
    val watchOnly: Boolean = true
)

sealed interface HealthResult<out T> {
    data class Success<T>(val data: T) : HealthResult<T>
    data object PermissionDenied : HealthResult<Nothing>
    data class Unavailable(val reason: String) : HealthResult<Nothing>
    data class Failure(val error: Throwable) : HealthResult<Nothing>
}

fun HealthResult<*>.errorMessageOrNull(): String? = when (this) {
    is HealthResult.Success -> null
    is HealthResult.PermissionDenied -> "건강 데이터 접근 권한이 거부되었습니다."
    is HealthResult.Unavailable -> "사용할 수 없습니다: $reason"
    is HealthResult.Failure -> "조회 실패: ${error.message ?: error::class.java.simpleName}"
}

fun <T> HealthResult<T>.dataOrNull(): T? = (this as? HealthResult.Success)?.data

interface HealthDataSource {
    val name: String
    val sourceTag: String
    suspend fun isAvailable(): Boolean
    suspend fun hasPermissions(): Boolean
    suspend fun requestPermissions(): HealthResult<Unit>
    suspend fun readHeartRate(q: HealthQuery): HealthResult<List<HeartRateSample>>
    suspend fun readSteps(q: HealthQuery): HealthResult<List<StepSample>>
    suspend fun readSleepSessions(q: HealthQuery): HealthResult<List<SleepSession>>
}

object TimeFmt {
    private val hhmm = java.text.SimpleDateFormat("HH:mm", java.util.Locale.US)
        .apply { timeZone = java.util.TimeZone.getTimeZone("Asia/Seoul") }
    fun hhmm(ms: Long): String = hhmm.format(java.util.Date(ms))

    private val full = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.US)
        .apply { timeZone = java.util.TimeZone.getTimeZone("Asia/Seoul") }
    fun full(ms: Long): String = full.format(java.util.Date(ms))
}