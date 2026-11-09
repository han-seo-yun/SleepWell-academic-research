package app.sleepwell.analysis

import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/*
 * 설문 입력 누적 (소빈 · 2026-10-24) — 팀장 답변 (10/24 19:23)
 *   "지난 건(2주치) 모델 학습용으로만 사용할거구, 앱에서는 사용자가 직접 입력해서 데이터화 할 수 있음 좋아 !!
 *    그래야 개인화 상수 수식에 넣을 수 있어서. 일단 입력해서 데이터 누적이 되면 괜춘!"
 *
 * - 「실제 컨디션 입력」에서 "입력 반영"을 누를 때마다 그날 설문 한 줄을 앱 전용 폴더 survey_log.csv 에 쌓는다.
 * - 형식 = 웹 설문 "CSV 내보내기"와 **같은 열** → 서윤 언니 학습 코드·SurveyCsv.parse() 가 그대로 읽는다.
 * - 같은 참여자·같은 날짜는 마지막 입력으로 바꾼다 (웹 설문도 같은 날 다시 저장하면 덮어씀).
 * - 참여자: 실제 기록 "me", 시연용 합성 데이터 "Test" (내보낸 뒤 학습에서 걸러낼 수 있게).
 * - 폰(앱) 안에만 저장. 서버 전송 없음. 「저장 기록」 탭에서 보기·삭제·CSV 내보내기.
 * - Android 없이 시험할 수 있게 File 만 받는다.
 */
object SurveyStore {

    /** 웹 설문 CSV 내보내기 열 순서 그대로 */
    val HEADER = listOf(
        "participant", "date", "caffeineMg", "lastCaffeine", "napMinutes", "napEnd", "exerciseMinutes", "exerciseEnd",
        "fatigue", "steps", "heartRate", "alarmTime", "firstCommitment", "examTomorrow", "alcohol", "illness",
        "caffeineUsed", "exerciseIntensity", "savedAt",
        "rested",     // (v6) 앱에서만 받는 아침 개운함 1~5 — 맨 뒤에 붙여서 웹 설문 열 순서는 그대로 (읽을 때는 열 이름으로 찾음)
        "sourceZoneId", "sourceDate", "targetWakeMs", "lastCaffeineMs", "napEndMs",
        "sleepWakeDate", "sleepLatencyMinutes", "awakeMinutes", "sleepWakeMs"
    )

    /** 최근 날짜가 위로 */
    fun load(file: File): List<SurveyDay> {
        if (!file.exists()) return emptyList()
        val text = try { file.readText(Charsets.UTF_8) } catch (e: Exception) { return emptyList() }
        return SurveyCsv.parse(text).days.sortedWith(compareByDescending<SurveyDay> { it.date }.thenBy { it.participant })
    }

    fun save(file: File, day: SurveyDay, nowMs: Long = System.currentTimeMillis()): List<SurveyDay> {
        val stamped = day.copy(savedAt = iso(nowMs))
        val all = load(file).filterNot { it.storageKey == day.storageKey } + stamped
        write(file, all)
        return load(file)
    }

    /** One form can submit a completed-sleep questionnaire and an upcoming-night context independently. */
    fun saveInput(file: File, day: SurveyDay, nowMs: Long = System.currentTimeMillis()): List<SurveyDay> {
        val parts = if (day.sleepWakeDate == null) listOf(day) else
            listOfNotNull(day.dailyContextPart(), day.sleepSupplementPart())
        val keys = parts.map { it.storageKey }.toSet()
        val all = load(file).filterNot { it.storageKey in keys } + parts.map { it.copy(savedAt = iso(nowMs)) }
        write(file, all)
        return load(file)
    }

    /** 여러 날을 한 번에 (시연용 합성 설문을 "그동안 쌓인 기록"으로 넣을 때). 같은 참여자·날짜는 바꾼다 */
    fun saveAll(file: File, days: List<SurveyDay>, nowMs: Long = System.currentTimeMillis()): List<SurveyDay> {
        val keys = days.map { it.storageKey }.toSet()
        val all = load(file).filterNot { it.storageKey in keys } + days.map { it.copy(savedAt = it.savedAt ?: iso(nowMs)) }
        write(file, all)
        return load(file)
    }

    fun delete(file: File, participant: String, date: String, sleepWakeDate: String? = null): List<SurveyDay> {
        write(file, load(file).filterNot { it.participant == participant &&
            if (sleepWakeDate != null) it.sleepWakeDate == sleepWakeDate else it.date == date && it.sleepWakeDate == null })
        return load(file)
    }

    /** 내보내기용 CSV 글자 (오래된 날짜부터) */
    fun toCsv(days: List<SurveyDay>): String {
        val sb = StringBuilder(HEADER.joinToString(",")).append('\n')
        days.sortedWith(compareBy({ it.participant }, { it.date })).forEach { d ->
            fun n(v: Double?) = v?.let { if (it % 1.0 == 0.0) it.toLong().toString() else it.toString() } ?: ""
            sb.append(listOf(
                d.participant, d.date, n(d.caffeineMg), d.lastCaffeine ?: "", n(d.napMinutes), d.napEnd ?: "",
                n(d.exerciseMinutes), d.exerciseEnd ?: "", d.fatigue?.toString() ?: "", d.steps?.toString() ?: "", n(d.heartRate),
                d.alarmTime ?: "", d.firstCommitment ?: "", d.examTomorrow.toString(), d.alcohol.toString(), d.illness.toString(),
                when (d.caffeineUsed) { true -> "yes"; false -> "no"; null -> "" }, d.exerciseIntensity ?: "", d.savedAt ?: "",
                d.rested?.toString() ?: "", d.sourceZoneId, d.sourceDate ?: "", d.targetWakeMs?.toString() ?: "",
                d.lastCaffeineMs?.toString() ?: "", d.napEndMs?.toString() ?: "",
                d.sleepWakeDate ?: "", d.sleepLatencyMinutes?.toString() ?: "", d.awakeMinutes?.toString() ?: "", d.sleepWakeMs?.toString() ?: ""
            ).joinToString(",") { esc(it) }).append('\n')
        }
        return sb.toString()
    }

    private fun write(file: File, all: List<SurveyDay>) {
        val text = toCsv(all)
        // 쓰다가 앱이 꺼져도 기존 파일이 깨지지 않게: 임시 파일에 다 쓴 뒤 바꿔 끼운다
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(text, Charsets.UTF_8)
        if (!tmp.renameTo(file)) { file.writeText(text, Charsets.UTF_8); tmp.delete() }
    }

    private fun esc(s: String) = if (s.any { it == ',' || it == '"' || it == '\n' || it == '\r' }) "\"" + s.replace("\"", "\"\"") + "\"" else s

    private fun iso(ms: Long) = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(ms))
}
