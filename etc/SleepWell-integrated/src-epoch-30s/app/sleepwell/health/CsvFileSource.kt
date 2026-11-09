package app.sleepwell.health

import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.io.InputStream
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/**
 * CSV 파일을 읽어서 공통 타입으로 변환하는 데이터 소스.
 *
 * 팀장 방향: "최종 앱은 Samsung Health와 직접 연동하지 않는다.
 * 우리가 정리한 데이터를 업로드하는 방식을 기본으로 한다."
 *
 * SamsungHealthSource(SDK 실시간 조회)를 대체한다.
 * 인터페이스(HealthDataSource)가 같으므로 기존 화면/그래프/export 코드는
 * 한 줄도 수정할 필요가 없다.
 *
 * 현재는 혜지가 올린 삼성헬스 앱 내보내기 CSV 형식을 읽는다.
 * 서윤이 공통 schema(8번 작업)를 확정하면 이 파일만 수정하면 된다.
 */
class CsvFileSource(
    /** 수면 단계 CSV 파일 (sleep_stage_cleaned.csv 형식) */
    private val sleepStageFile: File? = null,
    /** 수면 요약 CSV 파일 (sleep_summary_cleaned.csv 형식) — 선택 */
    private val sleepSummaryFile: File? = null,
    /** assets 등에서 InputStream으로 직접 넘기는 경우 */
    private val sleepStageStream: InputStream? = null,
    // TODO: 심박, 걸음 CSV가 확보되면 여기에 추가
) : HealthDataSource {

    override val name: String = "CSV 파일"
    override val sourceTag: String = "csv"

    override suspend fun isAvailable(): Boolean =
        sleepStageFile?.exists() == true || sleepStageStream != null

    override suspend fun hasPermissions(): Boolean = true

    override suspend fun requestPermissions(): HealthResult<Unit> =
        HealthResult.Success(Unit)

    // ────────────────────────────────────────────────
    // 심박 — 현재 혜지 CSV에 심박 데이터가 없음
    // ────────────────────────────────────────────────
    override suspend fun readHeartRate(q: HealthQuery): HealthResult<List<HeartRateSample>> {
        // 혜지 CSV에는 심박이 없어서 빈 목록 반환.
        // 심박 CSV가 확보되면 여기에 파싱 로직 추가.
        return HealthResult.Success(emptyList())
    }

    // ────────────────────────────────────────────────
    // 걸음 — 현재 혜지 CSV에 걸음 데이터가 없음
    // ────────────────────────────────────────────────
    override suspend fun readSteps(q: HealthQuery): HealthResult<List<StepSample>> {
        return HealthResult.Success(emptyList())
    }

    // ────────────────────────────────────────────────
    // 수면 세션 — sleep_stage_cleaned.csv 파싱
    // ────────────────────────────────────────────────
    override suspend fun readSleepSessions(q: HealthQuery): HealthResult<List<SleepSession>> {
        return try {
            val stages = when {
                sleepStageStream != null -> parseSleepStageCsv(sleepStageStream)
                sleepStageFile?.exists() == true -> parseSleepStageCsv(sleepStageFile!!.inputStream())
                else -> return HealthResult.Unavailable("수면 단계 CSV 파일이 지정되지 않았습니다.")
            }
            val filtered = stages.filter { it.startMs in q.startMs until q.endMs }
            val sessions = groupIntoSessions(filtered)
            HealthResult.Success(sessions)
        } catch (e: Exception) {
            HealthResult.Failure(e)
        }
    }

    // ────────────────────────────────────────────────
    // CSV 파싱 — 혜지의 sleep_stage_cleaned.csv 형식
    //
    // 열: start_time, end_time, duration_min, stage, stage_name,
    //     sleep_id, datauuid, time_offset
    //
    // stage 코드:
    //   40001 = 깨어있음 (Wake)
    //   40002 = 얕은 수면 (Light) → LIGHT
    //   40003 = 깊은 수면 (Deep)  → DEEP
    //   40004 = 렘수면 (REM)
    //
    // 서윤 공통 schema가 나오면 이 함수만 교체하면 됨.
    // ────────────────────────────────────────────────
    private fun parseSleepStageCsv(input: InputStream): List<SleepStageRow> {
        val rows = mutableListOf<SleepStageRow>()

        BufferedReader(InputStreamReader(input, Charsets.UTF_8)).use { reader ->
            val header = reader.readLine() ?: return rows
            // BOM 제거
            val cleanHeader = header.trimStart('\uFEFF')
            val cols = cleanHeader.split(",").map { it.trim() }

            val iStart = cols.indexOf("start_time")
            val iEnd = cols.indexOf("end_time")
            val iStage = cols.indexOf("stage")
            val iSleepId = cols.indexOf("sleep_id")
            val iOffset = cols.indexOf("time_offset")

            if (iStart < 0 || iEnd < 0 || iStage < 0) {
                throw IllegalArgumentException(
                    "CSV에 필수 열이 없습니다. 필요: start_time, end_time, stage. " +
                    "실제 헤더: $cols"
                )
            }

            reader.forEachLine { line ->
                if (line.isBlank()) return@forEachLine
                val values = line.split(",").map { it.trim() }
                if (values.size <= maxOf(iStart, iEnd, iStage)) return@forEachLine

                val offset = if (iOffset >= 0 && values.size > iOffset)
                    values[iOffset] else "UTC+0900"

                val startMs = parseTimestamp(values[iStart], offset)
                val endMs = parseTimestamp(values[iEnd], offset)
                val stageCode = values[iStage].toIntOrNull() ?: return@forEachLine
                val sleepId = if (iSleepId >= 0 && values.size > iSleepId)
                    values[iSleepId] else "unknown"

                if (startMs != null && endMs != null) {
                    rows.add(SleepStageRow(
                        startMs = startMs,
                        endMs = endMs,
                        stage = samsungCodeToSleepStage(stageCode),
                        sleepId = sleepId
                    ))
                }
            }
        }

        return rows.sortedBy { it.startMs }
    }

    /**
     * 삼성헬스 수면 단계 코드 → 팀 공통 SleepStage.
     *
     * Samsung Health는 4단계만 제공 (N1/N2 구분 없음).
     * 합의(소빈): Light → LIGHT로 표기. md 기준 워치 단계는 AWAKE/LIGHT/DEEP/REM 4단계 통일.
     * PSG와 비교할 때만 매핑 사용.
     */
    private fun samsungCodeToSleepStage(code: Int): SleepStage = when (code) {
        40001 -> SleepStage.WAKE
        40002 -> SleepStage.LIGHT   // Light → LIGHT (수정)
        40003 -> SleepStage.DEEP    // Deep → DEEP (수정)
        40004 -> SleepStage.REM
        else  -> SleepStage.UNKNOWN
    }

    /**
     * sleep_id가 같은 행들을 하나의 SleepSession으로 묶는다.
     * 한 sleep_id = 하룻밤(또는 낮잠) 하나.
     */
    private fun groupIntoSessions(rows: List<SleepStageRow>): List<SleepSession> {
        return rows.groupBy { it.sleepId }
            .map { (sleepId, segments) ->
                val sorted = segments.sortedBy { it.startMs }
                SleepSession(
                    sessionId = sleepId,
                    startMs = sorted.first().startMs,
                    endMs = sorted.last().endMs,
                    stages = sorted.map { row ->
                        SleepStageSegment(
                            startMs = row.startMs,
                            endMs = row.endMs,
                            stage = row.stage
                        )
                    },
                    device = DeviceInfo(
                        sourceName = "Samsung Health CSV",
                        deviceType = DeviceType.WATCH
                    )
                )
            }
            .sortedBy { it.startMs }
    }

    // ────────────────────────────────────────────────
    // 시각 파싱
    // ────────────────────────────────────────────────
    private val dateFormat = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).apply {
        // 기본은 KST. CSV의 time_offset에 따라 동적 조정.
        timeZone = TimeZone.getTimeZone("Asia/Seoul")
    }

    private val dateFormatWithMs = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("Asia/Seoul")
    }

    /**
     * "2026-09-13 18:24:00" 또는 "2026-09-13 18:24:00.000" 형식을 epoch ms로 변환.
     *
     * [통합 수정 2026-09-25 소빈 — 합의안 4절 "시간대 오류: 소빈"]
     * 삼성헬스 내보내기 CSV 의 start_time/end_time 은 **UTC** 이고, 현지 시간과의 차이는
     * time_offset 열에 따로 적힌다. 기존 코드는 이 문자열을 KST 로 읽어서 모든 시각이 9시간 이르게 잡혔다.
     * 근거: (1) 혜지 데이터에서 5시간 이상 수면 대부분이 원본 기준 18~21시 시작·2~3시 종료
     *           → UTC 로 읽어야 KST 03~06시 취침·11~12시 기상으로 자연스러움
     *       (2) 삼성헬스 export 분석 자료: "time based fields are UTC time, the time offset is in a separate column"
     * epoch ms(= 절대 시각)를 만드는 데는 UTC 만 알면 되므로 time_offset 은 여기서 쓰지 않는다.
     * 화면 표시는 앱 기준 시간대(APP_ZONE)가 맡는다.
     *
     * ★ 아직 확정되지 않은 값 (2026-10-02 기준)
     *   SAMSUNG_TIMES_ARE_UTC = true 는 **소빈의 가설**이다. 혜지 확인이 아직 안 끝났다.
     *   혜지 확인 결과 원본이 KST 로 밝혀지면 이 상수만 false 로 바꾸면 이전 동작으로 돌아간다.
     *
     *   이 값이 C_sleep 와 phaseRef 를 바꾼다는 점이 중요하다.
     *   Process S 는 수면/각성 순서와 지속 시간에만 의존하므로 시각 해석과 무관하다.
     *   C_sleep 와 phaseRef 는 시계 시각을 직접 쓰므로 9시간 밀린다.
     *   → 30초/1분 **비교**는 양쪽이 같은 해석을 쓰므로 영향이 없다.
     *     절대 수치를 보고서에 쓸 때는 이 상수가 무엇인지 반드시 명시할 것.
     */
    private fun parseTimestamp(text: String, offset: String = "UTC+0900"): Long? {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return null

        val tz = if (SAMSUNG_TIMES_ARE_UTC) TimeZone.getTimeZone("UTC") else when {
            offset.contains("+0900") || offset.contains("+09:00") ->
                TimeZone.getTimeZone("Asia/Seoul")
            offset.contains("+0000") || offset.contains("+00:00") || offset == "UTC" ->
                TimeZone.getTimeZone("UTC")
            else -> TimeZone.getTimeZone("Asia/Seoul") // 기본값
        }

        return try {
            if (trimmed.contains(".")) {
                dateFormatWithMs.timeZone = tz
                dateFormatWithMs.parse(trimmed)?.time
            } else {
                dateFormat.timeZone = tz
                dateFormat.parse(trimmed)?.time
            }
        } catch (e: Exception) {
            null
        }
    }

    private companion object {
        /** 삼성헬스 export 시각이 UTC 인가 (parseTimestamp 주석 참고). 혜지 확인 후 확정. */
        const val SAMSUNG_TIMES_ARE_UTC = true
    }

    /** 파싱 중간 결과를 담는 내부 클래스 */
    internal data class SleepStageRow(
        val startMs: Long,
        val endMs: Long,
        val stage: SleepStage,
        val sleepId: String
    )

    // ────────────────────────────────────────────────
    // 테스트·파이프라인용 노출 함수 (SleepAnalysisPipeline에서 사용)
    // ────────────────────────────────────────────────

    /**
     * CSV 파싱 결과를 외부로 노출 (파이프라인에서 epoch 변환 전 단계 사용).
     * 내부 private 함수 parseSleepStageCsv를 래핑.
     */
    internal fun parseSleepStageCsvForPipeline(input: InputStream): List<SleepStageRow> {
        return parseSleepStageCsv(input)
    }

    /**
     * SleepStageRow 목록을 SleepSession으로 그룹화 결과 노출.
     * 내부 private 함수 groupIntoSessions를 래핑.
     */
    internal fun groupIntoSessionsForPipeline(rows: List<SleepStageRow>): List<SleepSession> {
        return groupIntoSessions(rows)
    }
}
