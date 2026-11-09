package app.sleepwell.analysis

import app.sleepwell.health.DisplaySettings
import java.io.File
import java.text.SimpleDateFormat
import java.time.ZoneId
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/*
 * 결과 저장·조회 (소빈 · 2026-10-24) — 작업 지시서 Ⅲ-4 "… → 권장시간 산출 → 결과 저장 및 조회"
 *
 * - 「다음 수면 일정」에서 "이 결과 저장"을 누르면 문구와 원시 시각을 함께 저장한다.
 *   새 행은 현재 기기 시간대로 다시 표시하고, 시간대 정보가 없는 기존 행은 저장 당시 문구를 그대로 보존한다.
 * - 저장 위치: 앱 전용 폴더(filesDir)의 saved_results.csv. 앱을 껐다 켜도 남는다. 앱을 지우면 같이 지워진다.
 * - 같은 데이터(파일 이름) + 같은 기준 시각이면 새로 저장한 것으로 바꾼다 (목표 기상을 바꿔 다시 저장한 경우).
 * - 새 라이브러리 없이 CSV 로 둔다. 백업·복원·서버 전송은 아직 없음 (채윤 저장 구조가 정해지면 교체 가능).
 * - Android 없이 시험할 수 있도록 File 만 받는다.
 */
data class SavedResult(
    val savedAtMs: Long,
    val source: String,
    val basisMs: Long,
    val basisText: String,
    val modelText: String,
    val recText: String,
    val habitText: String,
    val statusText: String,
    val surveyText: String,
    val sValue: String,
    val cValue: String,
    val pValue: String,
    /** Null means a legacy row whose display zone was not stored. */
    val sourceZoneId: String? = null,
    val modelBedMs: Long? = null,
    val modelWakeMs: Long? = null,
    val recommendedBedMs: Long? = null,
    val recommendedWakeMs: Long? = null,
    /** Habit is a recurring local clock pattern, not an observed instant. */
    val habitBedMinute: Int? = null,
    val habitWakeMinute: Int? = null,
    val recommendationSleepText: String? = null
)

data class SavedResultText(
    val basisText: String,
    val modelText: String,
    val recommendationText: String,
    val habitText: String,
    val provenanceText: String
)

object ResultStore {
    private val HEADER = listOf(
        "savedAtMs", "source", "basisMs", "basisText", "modelText", "recText",
        "habitText", "statusText", "surveyText", "S", "C", "propensity",
        "sourceZoneId", "modelBedMs", "modelWakeMs", "recommendedBedMs", "recommendedWakeMs",
        "habitBedMinute", "habitWakeMinute", "recommendationSleepText"
    )

    /** 지금 화면(가장 최근 밤)의 결과 → 저장할 한 줄. 일정 카드가 없으면 null */
    fun fromDisplay(d: AnalysisDisplay, source: String, surveyText: String?, nowMs: Long = System.currentTimeMillis()): SavedResult? {
        val s = d.schedule ?: return null
        if (s.isPast) return null
        fun pair(t: ScheduleTimes?) = t?.let { "${it.bedText} 취침 → ${it.wakeText} 기상" } ?: "—"
        fun minute(text: String): Int? = Regex("(\\d{1,2}):(\\d{2})(?!.*\\d{1,2}:\\d{2})").find(text)?.let { match ->
            val hour = match.groupValues[1].toIntOrNull() ?: return@let null
            val value = match.groupValues[2].toIntOrNull() ?: return@let null
            (hour * 60 + value).takeIf { hour in 0..23 && value in 0..59 }
        }
        val rec = s.recommended
        val habitBedMinute = s.habit?.bedText?.let(::minute)
        val habitWakeMinute = s.habit?.wakeText?.let(::minute)
        return SavedResult(
            savedAtMs = nowMs,
            source = source,
            basisMs = s.basisMs ?: d.referenceMs,
            basisText = s.basisText,
            modelText = pair(s.model),
            recText = when {
                rec != null -> "${rec.targetWakeText} 기상 목표 → ${rec.bedText} 전에 취침" + (rec.sleepText?.let { " · $it" } ?: "")
                s.needsTargetWake -> "목표 기상 없음"
                else -> s.unavailableText ?: "—"
            },
            habitText = pair(s.habit),
            statusText = s.status.text,
            surveyText = surveyText ?: "",
            sValue = d.readings.firstOrNull { it.kind == ReadingKind.S }?.value ?: "",
            cValue = d.readings.firstOrNull { it.kind == ReadingKind.C }?.value ?: "",
            pValue = d.readings.firstOrNull { it.kind == ReadingKind.P }?.value ?: "",
            sourceZoneId = DisplaySettings.zone.zoneId,
            modelBedMs = s.model?.bedMs,
            modelWakeMs = s.model?.wakeMs,
            recommendedBedMs = rec?.bedMs,
            recommendedWakeMs = rec?.targetWakeMs,
            habitBedMinute = habitBedMinute,
            habitWakeMinute = habitWakeMinute,
            recommendationSleepText = rec?.sleepText
        )
    }

    /** 최신 저장이 위로 */
    fun load(file: File): List<SavedResult> {
        if (!file.exists()) return emptyList()
        val rows = try { splitCsv(file.readText(Charsets.UTF_8)) } catch (e: Exception) { return emptyList() }
        if (rows.isEmpty()) return emptyList()
        val h = rows.first()
        fun col(r: List<String>, n: String) = h.indexOf(n).takeIf { it >= 0 }?.let { r.getOrNull(it) } ?: ""
        return rows.drop(1).mapNotNull { r ->
            val saved = col(r, "savedAtMs").toLongOrNull() ?: return@mapNotNull null
            SavedResult(
                savedAtMs = saved,
                source = col(r, "source"),
                basisMs = col(r, "basisMs").toLongOrNull() ?: 0L,
                basisText = col(r, "basisText"),
                modelText = col(r, "modelText"),
                recText = col(r, "recText"),
                habitText = col(r, "habitText"),
                statusText = col(r, "statusText"),
                surveyText = col(r, "surveyText"),
                sValue = col(r, "S"),
                cValue = col(r, "C"),
                pValue = col(r, "propensity"),
                sourceZoneId = col(r, "sourceZoneId").takeIf { it.isNotBlank() },
                modelBedMs = col(r, "modelBedMs").toLongOrNull(),
                modelWakeMs = col(r, "modelWakeMs").toLongOrNull(),
                recommendedBedMs = col(r, "recommendedBedMs").toLongOrNull(),
                recommendedWakeMs = col(r, "recommendedWakeMs").toLongOrNull(),
                habitBedMinute = col(r, "habitBedMinute").toIntOrNull(),
                habitWakeMinute = col(r, "habitWakeMinute").toIntOrNull(),
                recommendationSleepText = col(r, "recommendationSleepText").takeIf { it.isNotBlank() }
            )
        }.sortedByDescending { it.savedAtMs }
    }

    fun save(file: File, r: SavedResult): List<SavedResult> {
        val kept = load(file).filterNot { it.source == r.source && it.basisMs == r.basisMs }
        val all = (kept + r).sortedByDescending { it.savedAtMs }
        write(file, all)
        return all
    }

    fun delete(file: File, savedAtMs: Long): List<SavedResult> {
        val all = load(file).filterNot { it.savedAtMs == savedAtMs }
        write(file, all)
        return all
    }

    private fun write(file: File, all: List<SavedResult>) {
        val sb = StringBuilder(HEADER.joinToString(",")).append('\n')
        all.forEach { r ->
            sb.append(listOf(
                r.savedAtMs.toString(), r.source, r.basisMs.toString(), r.basisText, r.modelText, r.recText,
                r.habitText, r.statusText, r.surveyText, r.sValue, r.cValue, r.pValue,
                r.sourceZoneId.orEmpty(), r.modelBedMs?.toString().orEmpty(), r.modelWakeMs?.toString().orEmpty(),
                r.recommendedBedMs?.toString().orEmpty(), r.recommendedWakeMs?.toString().orEmpty(),
                r.habitBedMinute?.toString().orEmpty(), r.habitWakeMinute?.toString().orEmpty(),
                r.recommendationSleepText.orEmpty()
            ).joinToString(",") { esc(it) }).append('\n')
        }
        // 쓰다가 앱이 꺼져도 기존 파일이 깨지지 않게: 임시 파일에 다 쓴 뒤 바꿔 끼운다
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(sb.toString(), Charsets.UTF_8)
        if (!tmp.renameTo(file)) { file.writeText(sb.toString(), Charsets.UTF_8); tmp.delete() }
    }

    private fun esc(s: String) = if (s.any { it == ',' || it == '"' || it == '\n' || it == '\r' }) "\"" + s.replace("\"", "\"\"") + "\"" else s

    /** Render raw times in the current display zone; legacy display-only rows remain verbatim. */
    fun render(r: SavedResult, displayZone: TimeZone): SavedResultText {
        val sourceZone = r.sourceZoneId?.let { runCatching { ZoneId.of(it) }.getOrNull() }
        val hasRawSchedule = sourceZone != null && r.basisMs > 0L
        if (!hasRawSchedule) {
            val allText = listOf(r.basisText, r.modelText, r.recText, r.habitText).joinToString(" ")
            val explicit = when {
                Regex("(?i)(\\bUTC\\b|\\bGMT\\b|(?:^|\\s)Z(?:$|\\s))").containsMatchIn(allText) -> "UTC"
                Regex("(?i)(\\bKST\\b|Asia/Seoul|한국 ?시간)").containsMatchIn(allText) -> "KST"
                else -> null
            }
            return SavedResultText(
                r.basisText,
                r.modelText,
                r.recText,
                r.habitText,
                explicit?.let { "저장 당시 $it 표시 문자열" } ?: "저장 당시 표시 문자열 · 시간대 미기록"
            )
        }
        fun full(ms: Long) = format(ms, "M/d HH:mm", displayZone)
        fun pair(start: Long?, end: Long?, fallback: String): String =
            if (start != null && end != null) "${full(start)} 취침 → ${full(end)} 기상" else fallback
        val rec = if (r.recommendedBedMs != null && r.recommendedWakeMs != null) {
            "${full(r.recommendedWakeMs)} 기상 목표 → ${full(r.recommendedBedMs)} 전에 취침" +
                (r.recommendationSleepText?.let { " · $it" } ?: "")
        } else r.recText
        // Habit is an aggregate local-clock statistic, not an instant. Attaching
        // the basis date would invent an offset/DST transition that was never measured.
        val habit = if (displayZone.toZoneId() == sourceZone) r.habitText else
            "${r.habitText} (${sourceZone.id} 기준 반복 시각 · 자동 변환 안 함)"
        return SavedResultText(
            basisText = "기준 ${format(r.basisMs, "M/d(E) HH:mm", displayZone)}" +
                if (r.basisText.endsWith(" 기상")) " 기상" else "",
            modelText = pair(r.modelBedMs, r.modelWakeMs, r.modelText),
            recommendationText = rec,
            habitText = habit,
            provenanceText = "현재 ${displayZone.id} 표시 · 저장 당시 ${sourceZone.id}"
        )
    }

    private fun format(ms: Long, pattern: String, zone: TimeZone): String =
        SimpleDateFormat(pattern, Locale.KOREA).apply { timeZone = zone }.format(Date(ms))

    private fun splitCsv(text: String): List<List<String>> {
        val rows = mutableListOf<List<String>>()
        var row = mutableListOf<String>(); val cell = StringBuilder(); var q = false; var i = 0
        while (i < text.length) {
            val ch = text[i]
            when {
                q && ch == '"' && i + 1 < text.length && text[i + 1] == '"' -> { cell.append('"'); i++ }
                ch == '"' -> q = !q
                !q && ch == ',' -> { row += cell.toString(); cell.clear() }
                !q && (ch == '\n' || ch == '\r') -> {
                    if (ch == '\r' && i + 1 < text.length && text[i + 1] == '\n') i++
                    row += cell.toString(); cell.clear(); rows += row; row = mutableListOf()
                }
                else -> cell.append(ch)
            }
            i++
        }
        if (cell.isNotEmpty() || row.isNotEmpty()) { row += cell.toString(); rows += row }
        return rows
    }
}
