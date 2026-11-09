package app.sleepwell.analysis

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Calendar
import java.util.TimeZone

/*
 * 취침 전 설문 CSV 읽기 (소빈 · 2026-10-24) — 작업 지시서 Ⅲ-2 "기존 취침 전 설문과 웨어러블 수면·활동 데이터를 하나의 사용자 기록으로"
 *
 * (2026-10-24 (13)) 앱에서 설문 CSV 올리기는 뺐다. SurveyDay 는 「실제 컨디션 입력」(DirectInputParser) 결과로 쓰고,
 *   parse() 는 합성 데이터(assets/demo_survey_*.csv)와 앱 누적 파일(SurveyStore, 같은 형식) 읽기에 쓴다.
 *   지난 2주치 웹 설문 CSV 는 서윤 언니 개인 보정 학습용.
 * 형식: 설문 페이지(han-seo-yun.github.io/sleep-timing-survey)의 "CSV 내보내기" 파일 그대로 (sleep-study-checkins.csv)
 *   열: participant, date, caffeineMg, lastCaffeine, napMinutes, napEnd, exerciseMinutes, exerciseEnd, fatigue,
 *       steps, heartRate, alarmTime, firstCommitment, examTomorrow, alcohol, illness, caffeineUsed, exerciseIntensity, savedAt
 *   (열 순서는 이름으로 찾는다. 공용 시트 내보내기처럼 recordId 등이 더 있어도 된다)
 *
 * 역할 나눔 (2026-10-24 합의 예정)
 *  - 소빈: 이 파일 = 설문 CSV 읽기 + 화면 표시 + 알람 시각 → 목표 기상
 *  - 채윤: 설문 값 → 모델 입력(Ridge 21개 항목) 변환. 서윤 언니 repo 의 DailySurveyFeatures 형식으로 넘긴다
 *          → featureValues() 가 그 형식의 이름·단위 그대로 값을 만든다 (채윤 쪽에서 DailySurveyFeatures(...) 로 옮기기만 하면 됨)
 *  - 지금 모델(Ridge)은 학습 전(가중치 0)이라 설문 값이 숫자를 바꾸지 않는다. 화면에 "모델 반영 예정"으로 표시.
 *    알람 시각만 지금부터 목표 기상으로 쓰여 권장 취침 계산에 들어간다.
 *
 * 시각("15:00")은 그 설문 날짜(date)의 시각으로 해석한다. 설문은 20:00 기준이라 하루를 넘지 않는다.
 * 알람 시각은 기준 시각 다음에 처음 오는 그 시각 (DirectInputParser.nextOccurrence 와 같은 규칙).
 */

data class SurveyDay(
    val participant: String,
    /** 예측 기준 날짜 (그날 저녁 → 그날 밤 수면) yyyy-MM-dd */
    val date: String,
    val caffeineUsed: Boolean?,
    val caffeineMg: Double?,
    val lastCaffeine: String?,
    val napMinutes: Double?,
    val napEnd: String?,
    val exerciseMinutes: Double?,
    val exerciseEnd: String?,
    /** none / light / moderate / vigorous */
    val exerciseIntensity: String?,
    /** 원점수. [fatigueScaleMax]와 함께 해석하며 값을 덮어쓰지 않는다. */
    val fatigue: Int?,
    val steps: Int?,
    val heartRate: Double?,
    val alarmTime: String?,
    val firstCommitment: String?,
    val examTomorrow: Boolean,
    val alcohol: Boolean,
    val illness: Boolean,
    /** 저장한 때 (ISO, 웹 설문 savedAt 과 같은 형식). 앱 입력 누적(SurveyStore)에서 채운다 */
    val savedAt: String? = null,
    /** (v6 2026-10-31) 아침 개운함 1 = 전혀 개운하지 않음 ~ 5 = 매우 개운함 (서윤 엔진과 같은 척도). 웹 설문에는 없는 열 */
    val rested: Int? = null,
    /** 이 행의 벽시계 입력을 해석할 IANA timezone. 기존 웹/앱 행은 한국 설문 계약으로 마이그레이션한다. */
    val sourceZoneId: String = SurveyCsv.LEGACY_SOURCE_ZONE,
    /** 카페인·낮잠이 속한 원본 지역의 밤 날짜. null인 legacy 행은 [date]를 사용한다. */
    val sourceDate: String? = null,
    /** 파서가 확정한 목표 기상의 절대 시각. 명시 날짜와 새벽 당일 알람을 sourceDate+1로 왜곡하지 않는다. */
    val targetWakeMs: Long? = null,
    /** 신규 앱 입력의 실제 카페인 시각. null인 legacy 행만 sourceDate/date + HH:mm 계약을 사용한다. */
    val lastCaffeineMs: Long? = null,
    /** 신규 앱 입력의 실제 낮잠 종료 시각. null인 legacy 행만 sourceDate/date + HH:mm 계약을 사용한다. */
    val napEndMs: Long? = null,
    /** Explicit canonical wake date of the completed sleep being supplemented; separate from the upcoming night. */
    val sleepWakeDate: String? = null,
    val sleepLatencyMinutes: Int? = null,
    val awakeMinutes: Int? = null,
    val sleepWakeMs: Long? = null,
    /** 열이 없던 기존 CSV와 기존 생성자는 이전 7점 척도로 해석한다. */
    val fatigueScaleMax: Int = FatigueScale.LEGACY_MAX
) {
    internal val storageKey get() = participant to (sleepWakeDate?.let { "sleep:$it" } ?: "context:$date")

    /** The upcoming night's constraints must not be shadowed by a completed sleep's questionnaire. */
    fun dailyContextPart(): SurveyDay? {
        if (sleepWakeDate == null) return this
        val hasContext = listOf(caffeineUsed, caffeineMg, lastCaffeine, napMinutes, napEnd,
            alarmTime, targetWakeMs, firstCommitment).any { it != null } || examTomorrow || alcohol || illness
        if (!hasContext) return null
        return copy(sleepWakeDate = null, sleepWakeMs = null, sleepLatencyMinutes = null, awakeMinutes = null,
            rested = null, fatigue = null, steps = null, exerciseMinutes = null, exerciseEnd = null, exerciseIntensity = null)
    }

    fun sleepSupplementPart(): SurveyDay? {
        if (sleepWakeDate == null || listOf(rested, fatigue, sleepLatencyMinutes, awakeMinutes,
                exerciseMinutes, exerciseIntensity, steps).all { it == null }) return null
        return copy(caffeineUsed = null, caffeineMg = null, lastCaffeine = null, lastCaffeineMs = null,
            napMinutes = null, napEnd = null, napEndMs = null, alarmTime = null, targetWakeMs = null,
            firstCommitment = null, examTomorrow = false, alcohol = false, illness = false)
    }
    /** 화면 한 줄 요약. 입력 안 한 항목은 뺀다. includeAlarm=false: 목표 기상을 따로 보여 줄 때 */
    fun summary(includeAlarm: Boolean = true): String {
        val parts = mutableListOf<String>()
        sleepWakeDate?.let { parts += "${sleepWakeMs?.let { ms -> DISPLAY_STAMP.format(Instant.ofEpochMilli(ms).atZone(sourceZone())) } ?: it} 기상 수면 보완" }
        sleepLatencyMinutes?.let { parts += "입면까지 ${it}분" }
        awakeMinutes?.let { parts += "중간각성 ${it}분" }
        when {
            caffeineUsed == false || caffeineMg == 0.0 -> parts += "카페인 없음"
            caffeineMg != null -> parts += "카페인 ${caffeineMg.toInt()}mg" + (lastCaffeine?.let { " (마지막 $it)" } ?: "")
            caffeineUsed == true -> parts += "카페인 섭취" + (lastCaffeine?.let { " (마지막 $it)" } ?: "")
        }
        napMinutes?.takeIf { it > 0 }?.let { parts += "낮잠 ${it.toInt()}분" + (napEnd?.let { e -> " ($e 종료)" } ?: "") }
        val ex = exerciseMinutes?.takeIf { it > 0 }
        if (ex != null) parts += "운동 ${ex.toInt()}분" + (intensityWord()?.let { " · $it" } ?: "")
        else if (exerciseIntensity == "none") parts += "운동 안 함"
        else intensityWord()?.let { parts += "운동 $it" }
        rested?.let { parts += "개운함 $it/5" }
        fatigue?.let { parts += FatigueScale.displayText(it, fatigueScaleMax) }
        if (includeAlarm) alarmTime?.let { parts += "알람 $it" }
        firstCommitment?.let { parts += "첫 일정 $it" }
        listOfNotNull(
            "시험·마감".takeIf { examTomorrow }, "음주".takeIf { alcohol }, "아픔·컨디션 이상".takeIf { illness }
        ).takeIf { it.isNotEmpty() }?.let { parts += it.joinToString("·") }
        return if (parts.isEmpty()) "입력한 항목이 없어요" else parts.joinToString(" · ")
    }

    /** 알람(목표 기상) 말고 설문 항목이 하나라도 있는가 */
    fun hasItems(): Boolean = listOf(caffeineUsed, caffeineMg, lastCaffeine, napMinutes, napEnd, exerciseMinutes, exerciseIntensity, fatigue,
        firstCommitment, rested, sleepLatencyMinutes, awakeMinutes).any { it != null } || examTomorrow || alcohol || illness

    fun intensityWord(): String? = when (exerciseIntensity) {
        "none" -> null; "light" -> "가벼움"; "moderate" -> "보통"; "vigorous" -> "격함"; else -> null
    }

    /** 운동 강도 숫자 (안 함 0 · 가벼움 1 · 보통 2 · 격함 3). 모델 쪽 기준이 정해지면 채윤과 맞춘다 (추정 매핑) */
    fun intensityScore(): Double? = when (exerciseIntensity) {
        "none" -> 0.0; "light" -> 1.0; "moderate" -> 2.0; "vigorous" -> 3.0; else -> null
    }

    /** 설문 날짜의 "HH:mm" → epoch ms (표시 시간대가 아니라 설문을 쓴 곳의 시간대 = 한국 시간) */
    fun timeOnDate(hhmm: String?, tz: TimeZone): Long? {
        val m = CLOCK.matchEntire(hhmm?.trim() ?: return null) ?: return null
        val d = DATE.matchEntire(date) ?: return null
        return Calendar.getInstance(tz).apply {
            clear()
            set(d.groupValues[1].toInt(), d.groupValues[2].toInt() - 1, d.groupValues[3].toInt(),
                m.groupValues[1].toInt(), m.groupValues[2].toInt(), 0)
        }.timeInMillis
    }

    /**
     * 서윤 언니 repo DailySurveyFeatures 와 같은 이름·단위 (채윤 변환용).
     *   caffeineMg, lastCaffeineMs, napMinutes, napEndMs, exerciseMinutes, exerciseIntensity, fatigueScore
     */
    fun featureValues(@Suppress("UNUSED_PARAMETER") tz: TimeZone): Map<String, Double?> = mapOf(
        "caffeineMg" to (if (caffeineUsed == false) 0.0 else caffeineMg),
        "lastCaffeineMs" to lastCaffeineInstant()?.toEpochMilli()?.toDouble(),
        "napMinutes" to napMinutes,
        "napEndMs" to napEndInstant()?.toEpochMilli()?.toDouble(),
        "exerciseMinutes" to exerciseMinutes,
        "exerciseIntensity" to intensityScore(),
        // 옛 Ridge feature 계약은 7점 원점수다. 새 5점 점수를 근거 없이 역변환하지 않는다.
        "fatigueScore" to fatigue?.takeIf {
            fatigueScaleMax == FatigueScale.LEGACY_MAX && FatigueScale.validRaw(it, fatigueScaleMax)
        }?.toDouble(),
        // 제한 안내가 5점 피로도 응답을 '설문 없음'으로 오인하지 않게 하는 내부 표식이다.
        "_surveyResponsePresent" to 1.0.takeIf { hasItems() }
    )

    /** Recorded source-zone conversion; caller timezone is intentionally ignored for source compatibility. */
    fun featureValues(): Map<String, Double?> = featureValues(TimeZone.getTimeZone(sourceZoneId))

    fun eventInstant(clock: String?): Instant? = clockInstant(clock, dayOffset = 0)
    fun lastCaffeineInstant(): Instant? = lastCaffeineMs?.let(Instant::ofEpochMilli) ?: eventInstant(lastCaffeine)
    fun napEndInstant(): Instant? = napEndMs?.let(Instant::ofEpochMilli) ?: eventInstant(napEnd)
    fun alarmInstant(): Instant? = targetWakeMs?.let(Instant::ofEpochMilli) ?: clockInstant(alarmTime, dayOffset = 1)

    /** Persisted alarm first, then source-local daily recurrence if that occurrence has passed. */
    fun nextAlarmAfter(after: Instant): Instant? {
        alarmInstant()?.takeIf { it.isAfter(after) }?.let { return it }
        val clock = parseClock(alarmTime) ?: return null
        val zone = sourceZone()
        var day = after.atZone(zone).toLocalDate()
        repeat(4) {
            val local = day.atTime(clock)
            val candidate = runCatching { DirectInputParser.resolveLocal(local, zone) }.getOrNull()
            if (candidate != null && candidate.isAfter(after)) return candidate
            day = day.plusDays(1)
        }
        return null
    }

    /** Saved-results display helper. Logical [date] remains the research night ID; event clocks become device time. */
    fun summaryIn(displayZoneId: ZoneId, includeAlarm: Boolean = true): String {
        fun shown(clock: String?, recordedMs: Long? = null, dayOffset: Long): String? =
            (recordedMs?.let(Instant::ofEpochMilli) ?: clockInstant(clock, dayOffset))?.atZone(displayZoneId)?.let {
            DISPLAY_STAMP.format(it)
        }
        val parts = mutableListOf<String>()
        sleepWakeDate?.let { parts += "${sleepWakeMs?.let { ms -> DISPLAY_STAMP.format(Instant.ofEpochMilli(ms).atZone(displayZoneId)) } ?: it} 기상 수면 보완" }
        sleepLatencyMinutes?.let { parts += "입면까지 ${it}분" }
        awakeMinutes?.let { parts += "중간각성 ${it}분" }
        when {
            caffeineUsed == false || caffeineMg == 0.0 -> parts += "카페인 없음"
            caffeineMg != null -> parts += "카페인 ${caffeineMg.toInt()}mg" + (shown(lastCaffeine, lastCaffeineMs, 0)?.let { " (마지막 $it)" } ?: "")
            caffeineUsed == true -> parts += "카페인 섭취" + (shown(lastCaffeine, lastCaffeineMs, 0)?.let { " (마지막 $it)" } ?: "")
        }
        napMinutes?.takeIf { it > 0 }?.let { parts += "낮잠 ${it.toInt()}분" + (shown(napEnd, napEndMs, 0)?.let { e -> " ($e 종료)" } ?: "") }
        val ex = exerciseMinutes?.takeIf { it > 0 }
        if (ex != null) parts += "운동 ${ex.toInt()}분" + (intensityWord()?.let { " · $it" } ?: "")
        else if (exerciseIntensity == "none") parts += "운동 안 함"
        else intensityWord()?.let { parts += "운동 $it" }
        rested?.let { parts += "개운함 $it/5" }
        fatigue?.let { parts += FatigueScale.displayText(it, fatigueScaleMax) }
        if (includeAlarm) alarmInstant()?.atZone(displayZoneId)?.let { DISPLAY_STAMP.format(it) }?.let { parts += "알람 $it" }
        shown(firstCommitment, dayOffset = 1)?.let { parts += "첫 일정 $it" }
        listOfNotNull(
            "시험·마감".takeIf { examTomorrow }, "음주".takeIf { alcohol }, "아픔·컨디션 이상".takeIf { illness }
        ).takeIf { it.isNotEmpty() }?.let { parts += it.joinToString("·") }
        return if (parts.isEmpty()) "입력한 항목이 없어요" else parts.joinToString(" · ")
    }

    fun alarmTextIn(displayZoneId: ZoneId): String? = alarmInstant()?.atZone(displayZoneId)?.let { DISPLAY_CLOCK.format(it) }
    fun eventTextIn(clock: String?, displayZoneId: ZoneId): String? =
        eventInstant(clock)?.atZone(displayZoneId)?.let { DISPLAY_STAMP.format(it) }

    private fun clockInstant(clock: String?, dayOffset: Long): Instant? {
        val time = parseClock(clock) ?: return null
        val date = runCatching { LocalDate.parse(sourceDate ?: this.date) }.getOrNull() ?: return null
        return runCatching { DirectInputParser.resolveLocal(date.plusDays(dayOffset).atTime(time), sourceZone()) }.getOrNull()
    }

    private fun sourceZone(): ZoneId = runCatching { ZoneId.of(sourceZoneId) }.getOrDefault(ZoneId.of(SurveyCsv.LEGACY_SOURCE_ZONE))
    private fun parseClock(value: String?): LocalTime? = CLOCK.matchEntire(value?.trim() ?: return null)?.let { match ->
        runCatching { LocalTime.of(match.groupValues[1].toInt(), match.groupValues[2].toInt()) }.getOrNull()
    }

    companion object {
        private val CLOCK = Regex("""^(\d{1,2}):(\d{2})$""")
        private val DATE = Regex("""^(\d{4})-(\d{2})-(\d{2})$""")
        private val DISPLAY_STAMP = DateTimeFormatter.ofPattern("M/d HH:mm")
        private val DISPLAY_CLOCK = DateTimeFormatter.ofPattern("HH:mm")
    }
}

data class SurveyParse(val days: List<SurveyDay>, val participants: List<String>, val errors: List<String>)

object SurveyCsv {

    const val LEGACY_SOURCE_ZONE = "Asia/Seoul"
    /** 설문 시각은 한국에서 쓴 것 (설문 페이지가 기기 시각을 그대로 저장) */
    val SURVEY_ZONE: TimeZone = TimeZone.getTimeZone("Asia/Seoul")

    fun parse(text: String): SurveyParse {
        val lines = splitCsv(text.trimStart('\uFEFF'))
        if (lines.isEmpty()) return SurveyParse(emptyList(), emptyList(), listOf("빈 파일이에요."))
        val header = lines.first().map { it.trim() }
        val need = listOf("participant", "date")
        val missing = need.filter { it !in header }
        if (missing.isNotEmpty()) return SurveyParse(
            emptyList(), emptyList(),
            listOf("설문 CSV 가 아니에요. 필요한 열: ${missing.joinToString()} · 실제 첫 줄: ${header.take(6).joinToString(",")}")
        )
        fun col(row: List<String>, name: String): String? =
            header.indexOf(name).takeIf { it >= 0 }?.let { row.getOrNull(it)?.trim() }?.takeIf { it.isNotEmpty() }
        fun num(row: List<String>, name: String) = col(row, name)?.toDoubleOrNull()
        fun bool(row: List<String>, name: String) = col(row, name)?.lowercase() == "true"

        val errors = mutableListOf<String>()
        val days = lines.drop(1).filter { r -> r.any { it.isNotBlank() } }.mapNotNull { r ->
            val p = col(r, "participant"); val d = col(r, "date")
            if (p == null || d == null || !Regex("""^\d{4}-\d{2}-\d{2}$""").matches(d)) { errors += "참여자·날짜가 없는 줄을 건너뛰었어요."; return@mapNotNull null }
            val sourceZone = col(r, "sourceZoneId")?.takeIf { candidate ->
                runCatching { ZoneId.of(candidate) }.isSuccess.also { valid ->
                    if (!valid) errors += "$p · $d: sourceZoneId '$candidate'를 읽을 수 없어 한국 시간으로 해석했어요."
                }
            } ?: LEGACY_SOURCE_ZONE
            val sourceDate = col(r, "sourceDate")?.takeIf { candidate ->
                runCatching { LocalDate.parse(candidate) }.isSuccess.also { valid ->
                    if (!valid) errors += "$p · $d: sourceDate '$candidate'를 읽을 수 없어 설문 날짜를 사용했어요."
                }
            }
            val fatigueText = col(r, "fatigue")
            val fatigueNumber = fatigueText?.toDoubleOrNull()
            val fatigue = fatigueNumber?.takeIf {
                it.isFinite() && it % 1.0 == 0.0 && it >= Int.MIN_VALUE.toDouble() && it <= Int.MAX_VALUE.toDouble()
            }?.toInt()
            val fatigueScaleDeclared = "fatigueScaleMax" in header
            val fatigueScaleText = col(r, "fatigueScaleMax")
            val fatigueScale = fatigueScaleText?.toIntOrNull() ?: if (!fatigueScaleDeclared) {
                FatigueScale.LEGACY_MAX
            } else {
                Int.MIN_VALUE
            }
            if (fatigueText != null && fatigue == null) {
                errors += "$p · $d: 피로도 원점수 '$fatigueText'는 정수로 읽을 수 없어 모델 입력에서 제외했어요."
            } else if (!FatigueScale.validRaw(fatigue, fatigueScale)) {
                errors += "$p · $d: 피로도 원점수 '$fatigue'와 척도 '$fatigueScale'를 확인해 주세요. 원본 값은 보존하고 모델 입력에서는 제외했어요."
            }
            SurveyDay(
                participant = p, date = d,
                caffeineUsed = col(r, "caffeineUsed")?.let { it == "yes" },
                caffeineMg = num(r, "caffeineMg"), lastCaffeine = col(r, "lastCaffeine"),
                napMinutes = num(r, "napMinutes"), napEnd = col(r, "napEnd"),
                exerciseMinutes = num(r, "exerciseMinutes"), exerciseEnd = col(r, "exerciseEnd"),
                exerciseIntensity = col(r, "exerciseIntensity"),
                fatigue = fatigue,
                steps = num(r, "steps")?.toInt(), heartRate = num(r, "heartRate"),
                alarmTime = col(r, "alarmTime"), firstCommitment = col(r, "firstCommitment"),
                examTomorrow = bool(r, "examTomorrow"), alcohol = bool(r, "alcohol"), illness = bool(r, "illness"),
                savedAt = col(r, "savedAt"),
                rested = num(r, "rested")?.toInt()?.takeIf { it in 1..5 },
                sourceZoneId = sourceZone,
                sourceDate = sourceDate,
                targetWakeMs = col(r, "targetWakeMs")?.toLongOrNull(),
                lastCaffeineMs = col(r, "lastCaffeineMs")?.toLongOrNull(),
                napEndMs = col(r, "napEndMs")?.toLongOrNull(),
                sleepWakeDate = col(r, "sleepWakeDate")?.takeIf { runCatching { LocalDate.parse(it) }.isSuccess },
                sleepLatencyMinutes = col(r, "sleepLatencyMinutes")?.toIntOrNull()?.takeIf { it in 0..180 },
                awakeMinutes = col(r, "awakeMinutes")?.toIntOrNull()?.takeIf { it in 0..600 },
                sleepWakeMs = col(r, "sleepWakeMs")?.toLongOrNull(),
                fatigueScaleMax = fatigueScale
            )
        }
        // 같은 사람·같은 날짜가 여러 번이면 마지막 줄 (설문 페이지도 같은 날 다시 저장하면 덮어쓴다)
        val dedup = days.associateBy { it.storageKey }.values.sortedWith(compareBy({ it.participant }, { it.date }))
        return SurveyParse(dedup, dedup.map { it.participant }.distinct(), errors.distinct())
    }

    /** 따옴표·쉼표·줄바꿈을 지키는 간단한 CSV 분해 */
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

    /** 이 기준 시각에 쓸 설문 날짜: (기준 시각 − 6시간)의 날짜. 아침 8시 → 그날 저녁 설문, 새벽 1시 → 전날 저녁 설문 */
    fun surveyDateFor(refMs: Long, tz: TimeZone = SURVEY_ZONE): String = dateText(refMs - 6 * 3_600_000L, tz)

    fun dateText(ms: Long, tz: TimeZone = SURVEY_ZONE): String {
        val c = Calendar.getInstance(tz).apply { timeInMillis = ms }
        return String.format(java.util.Locale.US, "%04d-%02d-%02d", c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH))
    }
}
