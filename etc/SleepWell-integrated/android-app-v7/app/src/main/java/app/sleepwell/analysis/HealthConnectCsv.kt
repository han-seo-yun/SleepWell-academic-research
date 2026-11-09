package app.sleepwell.analysis

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/*
 * 헬스 커넥트 수면 기록 → 지금 파이프라인이 읽는 수면 단계 CSV (소빈 · 2026-10-27)
 *
 * 왜 CSV 로 바꾸나
 *  - 분석 파이프라인(health/SleepAnalysisPipeline)은 삼성헬스 수면 단계 CSV 를 입력으로 받는다.
 *  - 헬스 커넥트에서 읽은 수면을 **같은 형식**으로 바꿔 넘기면, 계산·화면 코드는 하나도 안 바꿔도 된다.
 *    (CSV 업로드 · 합성 데이터 · 헬스 커넥트 세 가지가 모두 같은 길로 분석됨)
 *
 * 형식 = 삼성헬스 정리본과 같음
 *   start_time,end_time,duration_min,stage,stage_name,sleep_id,datauuid,time_offset
 *   - 시각: **UTC** "yyyy-MM-dd HH:mm:ss" (CsvFileSource 가 UTC 로 읽음, SAMSUNG_TIMES_ARE_UTC)
 *   - stage: 삼성 코드 40001 AWAKE · 40002 LIGHT · 40003 DEEP · 40004 REM
 *   - sleep_id: 세션 하나(하룻밤·낮잠) = 한 값. 파이프라인이 이걸로 세션을 묶는다
 *
 * Android 없이 시험할 수 있게, 헬스 커넥트 객체 대신 아래 단순한 값만 받는다 (변환은 HealthConnectSleep.kt).
 */

/** 헬스 커넥트 수면 세션 하나 (SleepSessionRecord 에서 필요한 값만) */
data class HcSleepSession(
    val id: String,
    val startMs: Long,
    val endMs: Long,
    /** 기록된 곳의 UTC 차이(초). 모르면 null → 한국 시간(+9) */
    val offsetSeconds: Int?,
    val stages: List<HcSleepStage>,
    /** Canonical model source; generated Health Connect CSV preserves it per record. */
    val source: String = "health_connect",
    /** Health Connect metadata.lastModifiedTime, or null for legacy/generated data. */
    val observedAtMs: Long? = null
)

/** 단계 하나. code = 삼성 코드(40001~40004) */
data class HcSleepStage(val startMs: Long, val endMs: Long, val code: Int)

object HealthConnectCsv {

    const val AWAKE = 40001
    const val LIGHT = 40002
    const val DEEP = 40003
    const val REM = 40004
    const val UNKNOWN = -1

    private fun name(code: Int) = when (code) {
        AWAKE -> "AWAKE"; LIGHT -> "LIGHT"; DEEP -> "DEEP"; REM -> "REM"; else -> "UNKNOWN"
    }

    private fun utc() = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }

    private fun offsetText(sec: Int?): String {
        val s = sec ?: 9 * 3600
        val sign = if (s < 0) "-" else "+"
        val a = kotlin.math.abs(s)
        return String.format(Locale.US, "UTC%s%02d%02d", sign, a / 3600, (a % 3600) / 60)
    }

    /**
     * 세션 목록 → CSV 글자.
     *  - 단계가 있으면 단계마다 한 줄 (세션 범위 밖은 잘라냄, 길이 0 이하는 버림)
     *  - 단계가 없거나 세션 경계·단계 사이가 비면 UNKNOWN 행으로 보존한다. 결측을 수면으로 추정하지 않는다.
     *  - (v6) idPrefix: 합친 파일(SleepDataStore)은 출처별 접두어(SH·HC)를 id 에 이미 붙여서 "" 로 부른다
     */
    fun toCsv(sessions: List<HcSleepSession>, idPrefix: String = "HC"): String {
        val f = utc()
        val sb = StringBuilder("start_time,end_time,duration_min,stage,stage_name,sleep_id,datauuid,time_offset,stage_observed,model_source,observed_at_ms\n")
        sessions.sortedBy { it.startMs }.forEach { s ->
            if (s.endMs <= s.startMs) return@forEach
            // Health Connect ids are CSV-safe UUID-like strings. Keep punctuation so provider ids do not
            // collide (for example, `a-b` and `ab`) and stable updates remain identifiable.
            val sid = idPrefix + s.id.ifBlank { s.startMs.toString() }
            val measured = s.stages
                .map { HcSleepStage(maxOf(it.startMs, s.startMs), minOf(it.endMs, s.endMs), it.code) }
                .filter { it.endMs > it.startMs }
                .sortedBy { it.startMs }
            val rows = if (measured.isEmpty()) {
                listOf(HcSleepStage(s.startMs, s.endMs, UNKNOWN))
            } else {
                buildList {
                    var coveredUntil = s.startMs
                    measured.forEach { stage ->
                        if (stage.startMs > coveredUntil) add(HcSleepStage(coveredUntil, stage.startMs, UNKNOWN))
                        add(stage)
                        coveredUntil = maxOf(coveredUntil, stage.endMs)
                    }
                    if (coveredUntil < s.endMs) add(HcSleepStage(coveredUntil, s.endMs, UNKNOWN))
                }
            }
            rows.forEachIndexed { i, r ->
                val dur = ((r.endMs - r.startMs) / 60_000L).coerceAtLeast(0)
                sb.append(f.format(Date(r.startMs))).append(',')
                    .append(f.format(Date(r.endMs))).append(',')
                    .append(dur).append(',')
                    .append(r.code).append(',')
                    .append(name(r.code)).append(',')
                    .append(sid).append(',')
                    .append(sid).append('-').append(i + 1).append(',')
                    .append(offsetText(s.offsetSeconds)).append(',')
                    .append(s.stages.isNotEmpty()).append(',')
                    .append(s.source).append(',')
                    .append(s.observedAtMs ?: "").append('\n')
            }
        }
        return sb.toString()
    }

    /** Generated Health Connect CSV only: preserve stage provenance, source, and provider modification time. */
    internal data class ModelMetadata(val stageObserved: Boolean, val source: String?, val observedAtMs: Long?)

    internal fun modelMetadataBySession(csv: String): Map<String, ModelMetadata> {
        val lines = csv.lineSequence().filter { it.isNotBlank() }.toList()
        if (lines.isEmpty()) return emptyMap()
        val header = lines.first().split(',')
        val idIndex = header.indexOf("sleep_id")
        val observedIndex = header.indexOf("stage_observed")
        val sourceIndex = header.indexOf("model_source")
        val observedAtIndex = header.indexOf("observed_at_ms")
        if (idIndex < 0) return emptyMap()
        return lines.drop(1).mapNotNull { line ->
            val columns = line.split(',')
            val id = columns.getOrNull(idIndex)?.takeIf { it.isNotBlank() } ?: return@mapNotNull null
            id to ModelMetadata(
                stageObserved = columns.getOrNull(observedIndex)?.equals("true", ignoreCase = true) == true,
                source = columns.getOrNull(sourceIndex)?.takeIf { it.isNotBlank() },
                observedAtMs = columns.getOrNull(observedAtIndex)?.toLongOrNull()
            )
        }.toMap()
    }
}
