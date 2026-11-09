package app.sleepwell.export

import android.content.Context
import app.sleepwell.health.EpochAggregator
import app.sleepwell.health.EPOCH_MS
import app.sleepwell.health.EpochRow
import app.sleepwell.health.HealthSnapshot
import app.sleepwell.health.SleepStage
import app.sleepwell.health.TimeFmt
import java.io.File

/*
 * 조회 결과를 연구용 파일로 내보낸다.
 *
 * 이 단계가 앱과 연구를 잇는 유일한 접점이다. 그래서 형식을 팀 공통 규격에 맞추는 것이
 * 가장 중요하다 — 특히 epoch_1m.csv 는 (subject_id, night, epoch_idx) 매칭 키
 * subject_id / night / epoch_idx 를 그대로 쓴다.
 * (2026-09-25) 수면 단계는 워치 4단계(AWAKE/LIGHT/DEEP/REM)로 바뀌어 PSG 용 stage_label(0~4) 열을
 * 없앴다. 워치 단계는 정답(y_true)이 아니며, PSG 와 비교할 때는 평가 쪽에서 4단계 매핑을 쓴다.
 *
 * 저장 위치는 앱 전용 외부 저장소(getExternalFilesDir)다. 저장 권한이 필요 없어서
 * 권한 문제로 막히지 않는다.
 */

data class ExportResult(
    val folder: File,
    val files: List<File>,
    /** EpochAggregator.selfCheck 결과. 비어 있으면 규격 정상. */
    val problems: List<String>
)

object Exporters {

    /** 가짜 데이터 경고문. meta.json 과 export.json 이 같은 문장을 쓴다. */
    private const val NOTE_FAKE =
        "source 가 fake 이면 이 파일은 실제 측정값이 아닙니다. 학습에 사용하지 마세요."

    /** CSV 4개 + meta.json + 통합 JSON 을 한 폴더에 쓴다. */
    fun exportAll(context: Context, snapshot: HealthSnapshot): ExportResult {
        val rows = EpochAggregator.aggregate(snapshot)
        val problems = EpochAggregator.selfCheck(rows).toMutableList()

        // 가상 데이터가 실제 데이터로 오인되는 것을 막기 위해 파일명에 흔적을 남긴다.
        // (2026-09-25) CSV 업로드("csv")도 실제 측정값이므로 "fake" 일 때만 표시한다.
        val fakeMark = if (snapshot.sourceTag == "fake") "_FAKE" else ""
        val root = context.getExternalFilesDir(null) ?: context.filesDir
        val folder = File(root, "exports/sleepwell_${snapshot.subjectId}_" +
                "${TimeFmt.stamp(System.currentTimeMillis())}$fakeMark")
        folder.mkdirs()

        val written = mutableListOf<File>()
        written += write(folder, "meta.json", metaJson(snapshot, rows))
        written += write(folder, "hr.csv", hrCsv(snapshot))
        written += write(folder, "steps.csv", stepsCsv(snapshot))
        written += write(folder, "sleep_stage.csv", sleepStageCsv(snapshot))
        written += write(folder, "epoch_1m.csv", epochCsv(rows))
        written += write(folder, "export.json", combinedJson(snapshot, rows))

        // JSON 을 라이브러리 없이 손으로 조립하고 있어서, 따옴표나 쉼표 한 글자만 틀려도
        // 파일 전체가 파싱 불가가 된다. 그런데 사람 눈으로는 거의 구별이 안 된다.
        // (실제로 note 줄의 따옴표가 두 개가 되어 두 파일이 모두 깨진 채 나간 적이 있다.)
        // org.json 은 안드로이드 플랫폼에 기본 포함이라 의존성을 늘리지 않고 검사할 수 있다.
        written.filter { it.extension == "json" }.forEach { f ->
            try {
                org.json.JSONObject(f.readText())
            } catch (e: Exception) {
                problems += "${f.name} 이 올바른 JSON 이 아닙니다 (${e.message})"
            }
        }

        return ExportResult(folder, written, problems)
    }

    private fun write(folder: File, name: String, content: String): File =
        File(folder, name).apply { writeText(content, Charsets.UTF_8) }

    // ── CSV ────────────────────────────────────────────────────
    private fun hrCsv(s: HealthSnapshot) = buildString {
        appendLine("subject_id,time_iso,time_ms,bpm,device_type,source_name")
        s.heartRate.forEach {
            appendLine(
                listOf(
                    s.subjectId, TimeFmt.iso(it.timeMs), it.timeMs.toString(),
                    fmt1(it.bpm),
                    it.device?.deviceType?.name ?: "", it.device?.sourceName ?: ""
                ).joinToString(",") { f -> csv(f) }
            )
        }
    }

    private fun stepsCsv(s: HealthSnapshot) = buildString {
        appendLine("subject_id,start_iso,end_iso,start_ms,end_ms,steps,device_type,source_name")
        s.steps.forEach {
            appendLine(
                listOf(
                    s.subjectId, TimeFmt.iso(it.startMs), TimeFmt.iso(it.endMs),
                    it.startMs.toString(), it.endMs.toString(), it.count.toString(),
                    it.device?.deviceType?.name ?: "", it.device?.sourceName ?: ""
                ).joinToString(",") { f -> csv(f) }
            )
        }
    }

    private fun sleepStageCsv(s: HealthSnapshot) = buildString {
        appendLine("subject_id,session_id,start_iso,end_iso,start_ms,end_ms,stage_code,stage_label")
        s.sleepSessions.forEach { sess ->
            sess.stages.forEach { seg ->
                appendLine(
                    listOf(
                        s.subjectId, sess.sessionId,
                        TimeFmt.iso(seg.startMs), TimeFmt.iso(seg.endMs),
                        seg.startMs.toString(), seg.endMs.toString(),
                        seg.stage.code.toString(), seg.stage.label
                    ).joinToString(",") { f -> csv(f) }
                )
            }
        }
    }

    /**
     * ★ 팀 파이프라인 직결 파일.
     * 매칭 키(subject_id / night / epoch_idx)는 기존 평가코드 규격과 동일하다.
     * 수면 단계 열 (2026-09-25):
     *   sleep_stage      = AWAKE / LIGHT / DEEP / REM (모르면 빈칸)  ← 서윤 #8 공통 schema 열 이름
     *   sleep_wake       = 1 수면 / 0 각성 (모르면 빈칸)              ← 서윤 #8 공통 schema 열 이름
     *   watch_stage_code = 0~3 (모르면 -1). PSG stage_label(0=W,1=N1…)과 다른 체계라 이름을 구분했다.
     * hr_mean 이 없으면 빈칸으로 둔다 — 0으로 채우면 "심박 0" 이라는 거짓 정보가 된다.
     *
     * night / epoch_idx 는 **행 번호가 아니라 시각 좌표**다 (규칙은 EpochAggregator 주석 참고).
     *   night     = 그 밤이 속한 날짜 yyyyMMdd (정오~다음날 정오)
     *   epoch_idx = 그 날짜 정오부터 1분 칸 번호. `epoch_idx × 60 = 정오 기준 경과 초`
     * session_id 는 매칭 키가 아니라 같은 밤의 낮잠/밤잠을 구분하는 참고 열이다.
     * 평가코드는 (subject_id, night, epoch_idx) 만 쓰므로 이 열이 있어도 영향이 없다.
     */
    private fun epochCsv(rows: List<EpochRow>) = buildString {
        appendLine("subject_id,night,session_id,epoch_idx,sleep_stage,sleep_wake,watch_stage_code,time_iso,hr_mean,steps")
        rows.forEach {
            val st = SleepStage.fromCode(it.stageLabel)
            appendLine(
                listOf(
                    it.subjectId, it.night.toString(), it.sessionId, it.epochIdx.toString(),
                    stageText(st), sleepWakeText(st), it.stageLabel.toString(), TimeFmt.iso(it.timeMs),
                    it.hrMean?.let { v -> fmt1(v) } ?: "",
                    it.steps.toString()
                ).joinToString(",") { f -> csv(f) }
            )
        }
    }

    // ── JSON ───────────────────────────────────────────────────
    private fun metaJson(s: HealthSnapshot, rows: List<EpochRow>): String {
        val sum = EpochAggregator.summarize(rows)
        val dev = s.sleepSessions.firstNotNullOfOrNull { it.device }
        return buildString {
            appendLine("{")
            appendLine("""  "subject_id": ${q(s.subjectId)},""")
            appendLine("""  "source": ${q(s.sourceTag)},""")
            appendLine("""  "exported_at": ${q(TimeFmt.iso(System.currentTimeMillis()))},""")
            appendLine("""  "timezone": ${q(TimeFmt.zoneId())},""")
            appendLine("""  "epoch_seconds": ${EPOCH_MS / 1000},""")
            // night / epoch_idx 의 뜻을 파일 안에 적어둔다. 이 규칙을 모르면 받는 쪽에서
            // "0부터 다시 번호를 매기면 되겠지" 하고 재색인해서 전체를 밀어버릴 수 있다.
            appendLine("""  "night_def": ${q("그 밤이 속한 날짜 yyyyMMdd. 정오~다음날 정오를 한 밤으로 본다. 조회 범위를 바꿔도 값이 변하지 않는다.")},""")
            appendLine("""  "epoch_idx_def": ${q("night 날짜의 12:00(로컬)부터 센 1분 칸 번호. epoch_idx x 60 = 정오 기준 경과 초. 행 번호가 아니므로 다시 번호를 매기지 말 것.")},""")
            appendLine("""  "stage_scheme": "watch4",""")
            appendLine("""  "watch_stage_code_map": {"0":"AWAKE","1":"LIGHT","2":"DEEP","3":"REM","-1":"UNKNOWN"},""")
            appendLine("""  "stage_note": ${q("삼성헬스는 Light 를 N1/N2 로 나누지 않는다. PSG(W/N1/N2/N3/REM)와 비교할 때만 N1+N2→LIGHT, N3→DEEP 매핑을 쓴다. 워치 단계는 정답(y_true)으로 쓰지 않는다.")},""")
            appendLine("""  "range": {"start": ${q(TimeFmt.iso(s.queriedFrom))}, "end": ${q(TimeFmt.iso(s.queriedTo))}},""")
            appendLine("""  "device": {"source_name": ${q(dev?.sourceName ?: "")}, "device_type": ${q(dev?.deviceType?.name ?: "")}, "raw_source_id": ${q(dev?.rawSourceId ?: "")}},""")
            appendLine("""  "counts": {"hr": ${s.heartRate.size}, "steps": ${s.steps.size}, "sleep_sessions": ${s.sleepSessions.size}, "epochs": ${rows.size}},""")
            appendLine("""  "summary": {"nights": ${sum.nights}, "sessions": ${sum.sessions}, "sleep_minutes": ${sum.sleepDurationMs / 60000}, "unknown_epochs": ${sum.unknownEpochs}, "hr_coverage": ${fmt3(sum.hrCoverage)}},""")
            appendLine("""  "warnings": [${s.warnings.joinToString(", ") { q(it) }}],""")
            // 문자열은 반드시 q() 를 거친다. 예전에 여기서 raw string 끝의 따옴표를 손으로
            // 붙였다가 따옴표가 두 개(마세요."") 가 되어 meta.json / export.json 이 둘 다
            // JSON 파싱 불가 상태로 나갔다. 눈으로는 거의 안 보이는 종류의 오류다.
            appendLine("""  "note": ${q(NOTE_FAKE)}""")
            append("}")
        }
    }

    private fun combinedJson(s: HealthSnapshot, rows: List<EpochRow>): String = buildString {
        appendLine("{")
        appendLine(""""meta": ${metaJson(s, rows)},""")
        appendLine(""""hr": [""")
        appendLine(s.heartRate.joinToString(",\n") {
            """  {"time_iso": ${q(TimeFmt.iso(it.timeMs))}, "time_ms": ${it.timeMs}, "bpm": ${fmt1(it.bpm)}, "device_type": ${q(it.device?.deviceType?.name ?: "")}}"""
        })
        appendLine("],")
        appendLine(""""steps": [""")
        appendLine(s.steps.joinToString(",\n") {
            """  {"start_iso": ${q(TimeFmt.iso(it.startMs))}, "end_iso": ${q(TimeFmt.iso(it.endMs))}, "steps": ${it.count}}"""
        })
        appendLine("],")
        appendLine(""""sleep_stage": [""")
        appendLine(s.sleepSessions.flatMap { sess ->
            sess.stages.map { seg ->
                """  {"session_id": ${q(sess.sessionId)}, "start_iso": ${q(TimeFmt.iso(seg.startMs))}, "end_iso": ${q(TimeFmt.iso(seg.endMs))}, "stage_code": ${seg.stage.code}, "stage_label": ${q(seg.stage.label)}}"""
            }
        }.joinToString(",\n"))
        appendLine("],")
        appendLine(""""epoch_1m": [""")
        appendLine(rows.joinToString(",\n") {
            """  {"subject_id": ${q(it.subjectId)}, "night": ${it.night}, "session_id": ${q(it.sessionId)}, "epoch_idx": ${it.epochIdx}, "sleep_stage": ${SleepStage.fromCode(it.stageLabel).let { st -> if (st == SleepStage.UNKNOWN) "null" else q(st.label) }}, "sleep_wake": ${sleepWakeJson(SleepStage.fromCode(it.stageLabel))}, "watch_stage_code": ${it.stageLabel}, "time_iso": ${q(TimeFmt.iso(it.timeMs))}, "hr_mean": ${it.hrMean?.let { v -> fmt1(v) } ?: "null"}, "steps": ${it.steps}}"""
        })
        appendLine("]")
        append("}")
    }

    // ── 포맷 유틸 ───────────────────────────────────────────────
    /** 값에 쉼표·따옴표·줄바꿈이 있으면 CSV 규칙대로 감싼다. */
    private fun csv(v: String): String =
        if (v.any { it == ',' || it == '"' || it == '\n' }) "\"${v.replace("\"", "\"\"")}\"" else v

    private fun q(v: String): String =
        "\"" + v.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n") + "\""

    private fun stageText(st: SleepStage): String = if (st == SleepStage.UNKNOWN) "" else st.label
    private fun sleepWakeText(st: SleepStage): String =
        if (st == SleepStage.UNKNOWN) "" else if (st.isSleep) "1" else "0"
    private fun sleepWakeJson(st: SleepStage): String =
        if (st == SleepStage.UNKNOWN) "null" else if (st.isSleep) "1" else "0"

    private fun fmt1(v: Float): String = String.format("%.1f", v)
    private fun fmt3(v: Float): String = String.format("%.3f", v)
}

/** 단계 비율 표시에 쓰는 순서 (화면·요약 공용) */
val STAGE_ORDER = listOf(SleepStage.WAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM)
