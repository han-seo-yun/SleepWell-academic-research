package app.sleepwell.analysis

import java.io.File
import java.io.FileOutputStream
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
        "sleepWakeDate", "sleepLatencyMinutes", "awakeMinutes", "sleepWakeMs",
        "fatigueScaleMax"
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

    /**
     * Saves an edit that was restored from existing rows. Fields absent from the app form are
     * retained, unchanged rows keep their original observation time, and clearing every visible
     * field removes only the visible response rather than inventing a new measurement.
     */
    fun saveRestoredEdit(
        file: File,
        edited: SurveyDay?,
        originalContext: SurveyDay?,
        originalSleepSupplement: SurveyDay?,
        nowMs: Long = System.currentTimeMillis(),
        /** Identity of an all-empty edit; prevents a draft crossing midnight from deleting yesterday. */
        emptyEditContextDate: String? = null,
        emptyEditSleepWakeDate: String? = null
    ): List<SurveyDay> {
        val before = load(file)
        val oldRows = listOfNotNull(originalContext, originalSleepSupplement)
        val editedParts = if (edited == null) emptyList() else if (edited.sleepWakeDate == null) listOf(edited)
            else listOfNotNull(edited.dailyContextPart(), edited.sleepSupplementPart())
        fun SurveyDay.isSleepPart() = sleepWakeDate != null
        fun emptyEditTargets(old: SurveyDay): Boolean = if (old.isSleepPart())
            emptyEditSleepWakeDate == null || emptyEditSleepWakeDate == old.sleepWakeDate
        else emptyEditContextDate == null || emptyEditContextDate == old.date
        val removedKeys = editedParts.map { it.storageKey }.toMutableSet()
        oldRows.forEach { old ->
            val sameType = editedParts.filter { it.isSleepPart() == old.isSleepPart() }
            if ((sameType.isEmpty() && emptyEditTargets(old)) || sameType.any { it.storageKey == old.storageKey })
                removedKeys += old.storageKey
        }

        fun hiddenOnly(old: SurveyDay, sleep: Boolean): SurveyDay? {
            val cleared = old.copy(
                caffeineUsed = null, caffeineMg = null, lastCaffeine = null, lastCaffeineMs = null,
                napMinutes = null, napEnd = null, napEndMs = null,
                exerciseMinutes = null, exerciseIntensity = null,
                fatigue = null, rested = null, sleepLatencyMinutes = null, awakeMinutes = null,
                alarmTime = null, targetWakeMs = null
            )
            val hasHidden = cleared.heartRate != null || cleared.steps != null || cleared.exerciseEnd != null ||
                cleared.firstCommitment != null || cleared.examTomorrow || cleared.alcohol || cleared.illness
            return cleared.takeIf { hasHidden }?.let {
                if (sleep) it.copy(caffeineUsed = null, caffeineMg = null, lastCaffeine = null, lastCaffeineMs = null,
                    napMinutes = null, napEnd = null, napEndMs = null, alarmTime = null, targetWakeMs = null,
                    firstCommitment = null, examTomorrow = false, alcohol = false, illness = false)
                else it.copy(sleepWakeDate = null, sleepWakeMs = null)
            }
        }

        fun mergeVisible(fresh: SurveyDay, old: SurveyDay?): SurveyDay {
            if (old == null) return fresh
            fun keepInstantWhenClockIsUnchanged(oldMs: Long?, freshClock: String?, freshMs: Long?): Long? {
                if (oldMs == null || freshClock == null) return freshMs
                val zone = runCatching { java.time.ZoneId.of(fresh.sourceZoneId) }.getOrNull() ?: return freshMs
                val oldClock = java.time.Instant.ofEpochMilli(oldMs).atZone(zone)
                    .format(java.time.format.DateTimeFormatter.ofPattern("HH:mm", Locale.US))
                return if (oldClock == freshClock) oldMs else freshMs
            }
            return if (fresh.sleepWakeDate != null) old.copy(
                exerciseMinutes = fresh.exerciseMinutes,
                exerciseIntensity = fresh.exerciseIntensity,
                fatigue = fresh.fatigue,
                fatigueScaleMax = if (fresh.fatigue != null) fresh.fatigueScaleMax else old.fatigueScaleMax,
                rested = fresh.rested,
                sleepLatencyMinutes = fresh.sleepLatencyMinutes,
                awakeMinutes = fresh.awakeMinutes
            ) else {
                val sameAbsoluteTarget = old.targetWakeMs != null && old.targetWakeMs == fresh.targetWakeMs
                val hiddenExpiredTarget = old.targetWakeMs != null && old.targetWakeMs <= nowMs &&
                    fresh.targetWakeMs == null && fresh.alarmTime == null
                old.copy(
                date = fresh.date,
                caffeineUsed = fresh.caffeineUsed,
                caffeineMg = fresh.caffeineMg,
                lastCaffeine = fresh.lastCaffeine,
                lastCaffeineMs = keepInstantWhenClockIsUnchanged(old.lastCaffeineMs, fresh.lastCaffeine, fresh.lastCaffeineMs),
                napMinutes = fresh.napMinutes,
                napEnd = fresh.napEnd,
                napEndMs = keepInstantWhenClockIsUnchanged(old.napEndMs, fresh.napEnd, fresh.napEndMs),
                exerciseMinutes = fresh.exerciseMinutes,
                exerciseIntensity = fresh.exerciseIntensity,
                fatigue = fresh.fatigue,
                fatigueScaleMax = if (fresh.fatigue != null) fresh.fatigueScaleMax else old.fatigueScaleMax,
                rested = fresh.rested,
                sleepLatencyMinutes = fresh.sleepLatencyMinutes,
                awakeMinutes = fresh.awakeMinutes,
                alarmTime = if (sameAbsoluteTarget || hiddenExpiredTarget) old.alarmTime else fresh.alarmTime,
                targetWakeMs = if (hiddenExpiredTarget) old.targetWakeMs else fresh.targetWakeMs,
                sourceZoneId = fresh.sourceZoneId,
                sourceDate = fresh.sourceDate
                )
            }
        }

        val editedCandidates = editedParts.map { fresh ->
            mergeVisible(fresh, oldRows.firstOrNull { it.storageKey == fresh.storageKey }
                ?: before.firstOrNull { it.storageKey == fresh.storageKey })
        }
        val editedKeys = editedCandidates.map { it.storageKey }.toSet()
        val clearedCandidates = oldRows.filter { old ->
            old.storageKey !in editedKeys && editedParts.none { it.isSleepPart() == old.isSleepPart() } &&
                emptyEditTargets(old)
        }.mapNotNull { old ->
            hiddenOnly(old, sleep = old.sleepWakeDate != null)
        }
        val candidates = editedCandidates + clearedCandidates

        val finalRows = candidates.map { candidate ->
            val old = oldRows.firstOrNull { it.storageKey == candidate.storageKey }
                ?: before.firstOrNull { it.storageKey == candidate.storageKey }
            if (old != null && old.copy(savedAt = null) == candidate.copy(savedAt = null)) old
            else candidate.copy(savedAt = iso(nowMs))
        }
        val all = before.filterNot { it.storageKey in removedKeys } + finalRows
        if (toCsv(before) == toCsv(all)) return before
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
                d.sleepWakeDate ?: "", d.sleepLatencyMinutes?.toString() ?: "", d.awakeMinutes?.toString() ?: "", d.sleepWakeMs?.toString() ?: "",
                d.fatigueScaleMax.toString()
            ).joinToString(",") { esc(it) }).append('\n')
        }
        return sb.toString()
    }

    private fun write(file: File, all: List<SurveyDay>) {
        val text = toCsv(all)
        backupLegacySchemaOnce(file)
        // 쓰다가 앱이 꺼져도 기존 파일이 깨지지 않게: 임시 파일에 다 쓴 뒤 바꿔 끼운다
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(text, Charsets.UTF_8)
        if (!tmp.renameTo(file)) { file.writeText(text, Charsets.UTF_8); tmp.delete() }
    }

    private fun backupLegacySchemaOnce(file: File) {
        if (!file.isFile) return
        val header = file.bufferedReader(Charsets.UTF_8).use { it.readLine().orEmpty().trimStart('\uFEFF') }
        if (header.split(',').any { it.trim() == "fatigueScaleMax" }) return
        val backup = File(file.parentFile, file.nameWithoutExtension + ".before-fatigue-scale-v2.csv")
        if (backup.exists()) {
            check(backup.isFile) { "기존 피로도 척도 백업 경로가 파일이 아니어서 저장을 중단했어요: ${backup.name}" }
            return
        }
        val temporary = File(file.parentFile, backup.name + ".tmp-${System.nanoTime()}")
        try {
            file.inputStream().use { input ->
                FileOutputStream(temporary).use { output ->
                    input.copyTo(output)
                    output.fd.sync()
                }
            }
            // 다른 저장 작업이 먼저 완성했다면 그 백업을 보존한다.
            if (backup.exists()) {
                check(backup.isFile) { "기존 피로도 척도 백업 경로가 파일이 아니어서 저장을 중단했어요: ${backup.name}" }
            } else {
                check(temporary.renameTo(backup)) {
                    "기존 피로도 기록 백업을 완성하지 못해 저장을 중단했어요: ${backup.name}"
                }
            }
        } finally {
            if (temporary.exists()) temporary.delete()
        }
    }

    private fun esc(s: String) = if (s.any { it == ',' || it == '"' || it == '\n' || it == '\r' }) "\"" + s.replace("\"", "\"\"") + "\"" else s

    private fun iso(ms: Long) = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(ms))
}
