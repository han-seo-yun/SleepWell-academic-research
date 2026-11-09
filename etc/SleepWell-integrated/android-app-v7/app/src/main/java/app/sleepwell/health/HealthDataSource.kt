package app.sleepwell.health

/**
 * 데이터를 주는 "창구".
 *
 * 화면·그래프·export 는 이 인터페이스만 보고 일한다. 뒤에 실제 Samsung Health SDK가
 * 있는지 가상 데이터 생성기가 있는지 신경 쓰지 않는다. 그래서
 *   - 소빈은 기기 없이 화면/export 를 완성할 수 있고
 *   - 채윤은 화면 걱정 없이 SDK 연동만 파면 되고
 *   - 통합할 때는 주입하는 구현체 한 줄만 바꾸면 실제 데이터로 돌아간다.
 *
 * 구현체
 *   - SamsungHealthSource (채윤): 실제 SDK
 *   - FakeDataSource      (소빈): 가상 데이터
 */
interface HealthDataSource {

    /** 화면에 "지금 어느 소스를 보고 있는지" 표시용. "SamsungHealth" 또는 "Fake". */
    val name: String

    /** export 시 meta.json 의 source 필드에 들어갈 값. "samsung" 또는 "fake". */
    val sourceTag: String

    /** 이 소스를 쓸 수 있는 환경인가. (Samsung Health 앱 설치·버전 등) Fake 는 항상 true. */
    suspend fun isAvailable(): Boolean

    suspend fun hasPermissions(): Boolean

    /** 권한 요청. 이미 허용된 경우에도 Success 를 돌려준다. */
    suspend fun requestPermissions(): HealthResult<Unit>

    suspend fun readHeartRate(q: HealthQuery): HealthResult<List<HeartRateSample>>

    suspend fun readSteps(q: HealthQuery): HealthResult<List<StepSample>>

    suspend fun readSleepSessions(q: HealthQuery): HealthResult<List<SleepSession>>
}

/**
 * 한 번 조회한 결과를 묶어 두는 그릇. 화면과 export 가 같은 객체를 본다.
 * 부분 실패를 허용한다 — 예를 들어 심박은 읽히는데 수면 단계만 권한이 없는 경우가 실제로 있다.
 */
data class HealthSnapshot(
    val subjectId: String,
    val queriedFrom: Long,
    val queriedTo: Long,
    val sourceTag: String,
    val heartRate: List<HeartRateSample> = emptyList(),
    val steps: List<StepSample> = emptyList(),
    val sleepSessions: List<SleepSession> = emptyList(),
    /** 항목별 실패 사유. 비어 있으면 전부 성공. */
    val warnings: List<String> = emptyList()
) {
    val isEmpty: Boolean
        get() = heartRate.isEmpty() && steps.isEmpty() && sleepSessions.isEmpty()
}

/**
 * 3종을 한 번에 읽어 HealthSnapshot 으로 묶는다.
 * 한 항목이 실패해도 나머지는 그대로 살려서 warnings 에만 남긴다.
 */
suspend fun HealthDataSource.readSnapshot(
    subjectId: String,
    q: HealthQuery
): HealthSnapshot {
    val warnings = mutableListOf<String>()

    val hr = readHeartRate(q).also { r ->
        r.errorMessageOrNull()?.let { warnings += "심박(HR) — $it" }
    }.dataOrNull().orEmpty()

    val st = readSteps(q).also { r ->
        r.errorMessageOrNull()?.let { warnings += "걸음(Steps) — $it" }
    }.dataOrNull().orEmpty()

    val sl = readSleepSessions(q).also { r ->
        r.errorMessageOrNull()?.let { warnings += "수면(Sleep) — $it" }
    }.dataOrNull().orEmpty()

    return HealthSnapshot(
        subjectId = subjectId,
        queriedFrom = q.startMs,
        queriedTo = q.endMs,
        sourceTag = sourceTag,
        heartRate = hr.sortedBy { it.timeMs },
        steps = st.sortedBy { it.startMs },
        sleepSessions = sl.sortedBy { it.startMs },
        warnings = warnings
    )
}
