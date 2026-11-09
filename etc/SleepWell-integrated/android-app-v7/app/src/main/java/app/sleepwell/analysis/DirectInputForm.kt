package app.sleepwell.analysis

import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DirectInput
import app.sleepwell.user.SleepRecord
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.time.format.ResolverStyle
import java.util.Locale

/*
 * 4단계 직접 입력: 화면의 글자 입력 → 채윤 파이프라인의 DirectInput 으로 바꾸는 곳. (소빈 · 2026-09-25)
 *
 * 근거 (1순위 md #5)
 *  - "필요한 경우 최근 취침·기상 시각이나 예상 취침·기상 시각 등 기본 정보를 직접 입력"
 *  - cf. "사용자가 입력한 개인 정보(컨디션 상태, 전날 취침 시간 및 기상 시간, 갤럭시 워치 건강정보)를 받아 분석"
 *  - 가능하다면 "내일 몇 시에 일어나야 하는지 / 오늘 몇 시쯤 잘 예정인지" 입력 → 취침·수면시간 계산
 *
 * 계산에 실제로 쓰이는 것 (2026-10-24 갱신)
 *  - 최근 취침~기상(기록 보완): 수면 압력 계산에 반영
 *  - 목표 기상: 권장 엔진이 연결되면 권장 취침 계산에 반영 (지금 폴더에는 엔진 없음 → 화면에 "준비 중")
 *  - 피로도·카페인·낮잠·운동(설문 항목): 계산에 반영 안 됨 — SurveyDay 로 담아 두기만 (개인 보정 학습 후 반영 예정)
 *  - 예정 취침: 화면 칸은 뺐다 (계산에 안 쓰임). 형식 처리는 남겨 둠
 *
 * 시각 형식은 "yyyy-MM-dd HH:mm". 기기 시간대로 읽고 제출 시 하나의 시간대 스냅샷을 사용한다.
 * 작성 도중 기기 시간대가 바뀐 미완성 입력은 작성 시작 시간대와 함께 유지한다.
 */
data class DirectInputForm(
    val bedText: String = "",
    val wakeText: String = "",
    val plannedBedText: String = "",
    val targetWakeText: String = "",
    /** 일지의 입면잠복기·중간각성. 둘 다 있으면 실제 수면시간을 완전 관측으로 계산한다. */
    val sleepLatencyText: String = "",
    val awakeMinutesText: String = "",
    /** (2026-10-24) 피로도 1~7 — 취침 전 웹 설문과 같은 척도 (이전: 컨디션 1~5, 1 = 피곤) */
    val condition: Int? = null,
    /** (v6) 아침 개운함 1~5 (서윤 엔진 척도) — 피로도와 같이 "잘 잔 밤" 판단에 쓰인다 */
    val rested: Int? = null,
    // ── (2026-10-24) 취침 전 설문 항목 (마지막 기록 다음 수면 기준) — 웹 설문·모델 입력(DailySurveyFeatures)과 같은 항목 ──
    val caffeineMgText: String = "",
    val lastCaffeineText: String = "",
    val napMinText: String = "",
    val napEndText: String = "",
    val exerciseMinText: String = "",
    /** none / light / moderate / vigorous (웹 설문 값 그대로). null = 선택 안 함 */
    val exerciseIntensity: String? = null,
    /** Selected imported sleep's canonical wake date; no manual interval is required for its questionnaire. */
    val sleepWakeDate: String? = null
)

data class ParsedDirectInput(
    /** 입력이 하나도 없으면 null */
    val input: DirectInput?,
    val errors: List<String>,
    /** (2026-10-24) 설문 항목 (설문 항목을 하나도 안 넣으면 null). featureValues() 로 모델 입력 이름이 된다 */
    val survey: SurveyDay? = null,
    /** 취침·기상 입력을 실제 입력 시각과 함께 보존한 명시적 일지. */
    val diary: SleepRecord? = null
) {
    val ok: Boolean get() = errors.isEmpty()
}

object DirectInputParser {

    const val PATTERN = "yyyy-MM-dd HH:mm"
    private val DATE_TIME = DateTimeFormatter.ofPattern("uuuu-MM-dd HH:mm", Locale.US)
        .withResolverStyle(ResolverStyle.STRICT)
    private const val MAX_SLEEP_MS = 16 * 3_600_000L
    /** 파이프라인 계산 범위(기준 시각 이전 7일). 채윤-소빈 합의값 */
    const val WINDOW_MS = 7 * 24 * 3_600_000L

    /**
     * 이 파일로 직접 입력을 반영할 수 있는 마지막 기상 시각(이 시각 **전**까지).
     * 기상 시각이 기준이 되면 그 이전 7일을 계산하는데, 그 안에 업로드 기록이 하나도 없으면
     * 기록 없는 날을 전부 '깨어 있음'으로 계산하게 되어(S 가 1.0 에 붙음) 파이프라인이 거부한다.
     */
    fun latestWakeAllowed(lastRecordEndMs: Long): Long = lastRecordEndMs + WINDOW_MS

    /**
     * (2026-09-25 추가) 이 시각 **이후**에 깬 수면만 반영된다.
     * 기상이 마지막 기록보다 앞이면 기준 시각은 마지막 기록 그대로이고, 계산 범위는 그 이전 7일뿐이라
     * 더 오래된 입력은 파이프라인에서 조용히 무시된다(결과는 그대로인데 화면엔 "반영됨"으로 나오던 문제).
     */
    fun earliestWakeAllowed(lastRecordEndMs: Long): Long = lastRecordEndMs - WINDOW_MS

    private fun displayZoneSnapshot(): ZoneId = ZoneId.of(DisplaySettings.zone.zoneId)

    fun toText(ms: Long, zoneId: ZoneId = displayZoneSnapshot()): String =
        DATE_TIME.format(Instant.ofEpochMilli(ms).atZone(zoneId))

    /** "7:30" / "07:30" */
    private val CLOCK = Regex("""^(\d{1,2}):(\d{2})$""")

    /** 목표 기상은 기준 시각에서 이만큼은 떨어져야 한다 (아침 7:07 에 깬 기록 + 알람 07:30 → 23분 뒤가 아니라 다음 날 07:30) */
    const val MIN_WAKE_GAP_MS = 3 * 3_600_000L

    /**
     * [zoneId]의 base(+minGapMs) 다음 h:mm. DST overlap은 첫 번째(더 이른 instant) offset으로 고정한다.
     * DST gap의 존재하지 않는 벽시계 시각은 다음 날로 조용히 이동하지 않고 명시적으로 거부한다.
     */
    fun nextOccurrence(base: Long, h: Int, mi: Int, minGapMs: Long = 0L, zoneId: ZoneId = displayZoneSnapshot()): Long {
        require(h in 0..23 && mi in 0..59) { "없는 시각이에요." }
        val threshold = Instant.ofEpochMilli(base + minGapMs)
        val localThreshold = threshold.atZone(zoneId).toLocalDateTime()
        var date = Instant.ofEpochMilli(base).atZone(zoneId).toLocalDate()
        repeat(3) {
            val local = date.atTime(h, mi)
            if (!local.isAfter(localThreshold)) {
                date = date.plusDays(1)
                return@repeat
            }
            val candidate = resolveLocal(local, zoneId)
            if (candidate.isAfter(threshold)) return candidate.toEpochMilli()
            date = date.plusDays(1)
        }
        error("다음 시각을 계산할 수 없어요.")
    }

    fun parse(
        form: DirectInputForm,
        lastRecordEndMs: Long? = null,
        nowMs: Long? = null,
        zoneId: ZoneId = displayZoneSnapshot(),
        linkedSleep: SleepRecord? = null
    ): ParsedDirectInput {
        val errors = mutableListOf<String>()

        fun read(label: String, text: String): Long? {
            val t = text.trim()
            if (t.isEmpty()) return null
            return try {
                resolveLocal(LocalDateTime.parse(t, DATE_TIME), zoneId).toEpochMilli()
            } catch (e: DateTimeParseException) {
                errors += "$label: \"$t\" 형식이 맞지 않아요 (예: 2026-09-15 23:30" + (if (label == "목표 기상" || label == "예정 취침") ", 또는 시각만 07:30" else "") + ")"
                null
            } catch (e: IllegalArgumentException) {
                errors += "$label: ${e.message}"
                null
            }
        }

        val bed = read("최근 취침", form.bedText)
        val wake = read("최근 기상", form.wakeText)
        val linked = linkedSleep?.takeIf { record ->
            record.day == form.sleepWakeDate && runCatching {
                val end = Instant.parse(record.wakeTime)
                Instant.parse(record.bedtime).isBefore(end) &&
                    !end.isAfter(Instant.ofEpochMilli(nowMs ?: System.currentTimeMillis()))
            }.getOrDefault(false)
        }
        if (form.sleepWakeDate != null && linked == null) {
            errors += "보완할 수면 기록을 다시 선택해 주세요."
        }
        if (linked != null && (form.bedText.isNotBlank() || form.wakeText.isNotBlank())) {
            errors += "워치 수면 보완과 직접 일지는 따로 저장해 주세요. 직접 일지는 수면 기록 선택을 해제한 뒤 입력할 수 있어요."
        }

        // (2026-10-24) 목표 기상·예정 취침은 "07:30"처럼 시각만 써도 된다.
        // 현재 입력은 nowMs, 과거 기록 재현은 마지막 기록/직접 입력 기상 시각을 기준으로 한다.
        // 그 기준 시각 **다음에 처음 오는** 그 시각으로 이해한다. 날짜까지 쓰면 그대로 쓴다.
        val base = nowMs ?: (listOfNotNull(lastRecordEndMs, wake).maxOrNull() ?: System.currentTimeMillis())
        fun readPlan(label: String, text: String, minGapMs: Long): Long? {
            val t = text.trim()
            CLOCK.matchEntire(t)?.let { m ->
                val h = m.groupValues[1].toInt(); val mi = m.groupValues[2].toInt()
                if (h > 23 || mi > 59) { errors += "$label: \"$t\" 는 없는 시각이에요"; return null }
                return try { nextOccurrence(base, h, mi, minGapMs, zoneId) }
                catch (e: IllegalArgumentException) { errors += "$label: ${e.message}"; null }
            }
            return read(label, t)
        }
        val plannedBed = readPlan("예정 취침", form.plannedBedText, 0L)
        val targetWake = readPlan("목표 기상", form.targetWakeText, if (nowMs != null) 0L else MIN_WAKE_GAP_MS)
        if (nowMs != null && targetWake != null && targetWake <= nowMs) errors += "목표 기상은 현재 시각 이후로 입력해 주세요."

        if ((bed == null) != (wake == null) && errors.isEmpty()) {
            errors += "최근 취침과 기상 시각은 둘 다 입력해야 수면으로 반영돼요."
        }
        if (bed != null && wake != null) {
            if (wake <= bed) errors += "최근 기상 시각이 취침 시각보다 늦어야 해요."
            else if (wake - bed > MAX_SLEEP_MS) errors += "수면이 16시간을 넘어요. 날짜를 확인해 주세요."
            else if (nowMs != null && wake > nowMs) errors += "최근 기상 시각은 현재 시각 이후일 수 없어요."
        }
        // 실시간 일지 저장은 워치 업로드 범위와 독립적이다. nowMs가 없는 과거 graph 재현만
        // 기존 ±7일 범위를 유지해 장기간을 전부 각성으로 계산하는 오류를 막는다.
        if (bed != null && wake != null && wake > bed && lastRecordEndMs != null && nowMs == null &&
            wake >= latestWakeAllowed(lastRecordEndMs)) {
            errors += "업로드한 기록의 마지막이 ${toText(lastRecordEndMs, zoneId)} 이라서, 기상 시각은 " +
                    "${toText(latestWakeAllowed(lastRecordEndMs), zoneId)} 이전이어야 반영돼요. " +
                    "(그 사이 기록이 없으면 전부 '깨어 있음'으로 계산돼 결과가 틀어져요)"
        }
        if (bed != null && wake != null && wake > bed && lastRecordEndMs != null && nowMs == null &&
            wake <= earliestWakeAllowed(lastRecordEndMs)) {
            errors += "계산 범위는 업로드 기록의 마지막(${toText(lastRecordEndMs, zoneId)}) 이전 7일이라서, 기상 시각은 " +
                    "${toText(earliestWakeAllowed(lastRecordEndMs), zoneId)} 이후여야 반영돼요."
        }
        if (form.condition != null && form.condition !in 1..7) errors += "피로도는 1~7 중에서 골라 주세요."

        // ── 설문 항목 (2026-10-24) — 숫자·시각 형식만 확인. 모델 반영은 개인 보정 학습 후 ──
        fun num(label: String, text: String, max: Double): Double? {
            val t = text.trim(); if (t.isEmpty()) return null
            val v = t.toDoubleOrNull()
            if (v == null || v < 0 || v > max) { errors += "$label: \"$t\" 는 0~${max.toInt()} 사이 숫자로 써 주세요."; return null }
            return v
        }
        fun wholeMinutes(label: String, text: String, max: Int): Int? {
            val t = text.trim()
            if (t.isEmpty()) return null
            val v = t.toIntOrNull()
            if (v == null || v !in 0..max) {
                errors += "$label: \"$t\" 는 0~${max}분 사이 정수로 써 주세요."
                return null
            }
            return v
        }
        val sleepLatency = wholeMinutes("잠드는 데 걸린 시간", form.sleepLatencyText, 180)
        val awakeMinutes = wholeMinutes("잠든 뒤 깨어 있던 시간", form.awakeMinutesText, 600)
        if ((form.sleepLatencyText.isNotBlank() || form.awakeMinutesText.isNotBlank()) && (bed == null || wake == null) && linked == null) {
            errors += "입면잠복기와 중간각성은 불러온 수면을 선택하거나 최근 취침·기상 시각과 함께 입력해 주세요."
        }
        val measuredBed = bed ?: linked?.bedtime?.let { Instant.parse(it).toEpochMilli() }
        val measuredWake = wake ?: linked?.wakeTime?.let { Instant.parse(it).toEpochMilli() }
        if (measuredBed != null && measuredWake != null && measuredWake > measuredBed) {
            val timeInBed = ((measuredWake - measuredBed) / 60_000L).toInt()
            if (sleepLatency != null && sleepLatency > timeInBed) errors += "잠드는 데 걸린 시간이 침대에 있던 시간보다 길어요."
            if (awakeMinutes != null && awakeMinutes > timeInBed) errors += "잠든 뒤 깨어 있던 시간이 침대에 있던 시간보다 길어요."
            val effectiveLatency = linked?.sleepLatencyMinutes ?: sleepLatency
            val effectiveAwake = linked?.awakeMinutes ?: awakeMinutes
            if ((effectiveLatency != null || effectiveAwake != null) &&
                (effectiveLatency ?: 0) + (effectiveAwake ?: 0) >= timeInBed) {
                errors += "입면잠복기와 중간각성 시간의 합은 침대에 있던 시간보다 짧아야 해요."
            }
        }
        fun clock(label: String, text: String): String? {
            val t = text.trim(); if (t.isEmpty()) return null
            val m = CLOCK.matchEntire(t)
            if (m == null || m.groupValues[1].toInt() > 23 || m.groupValues[2].toInt() > 59) { errors += "$label: \"$t\" 는 07:30 처럼 써 주세요."; return null }
            return String.format(Locale.US, "%02d:%02d", m.groupValues[1].toInt(), m.groupValues[2].toInt())
        }
        val caf = num("카페인(mg)", form.caffeineMgText, 2000.0)
        val lastCaf = clock("마지막 카페인", form.lastCaffeineText)
        val nap = num("낮잠(분)", form.napMinText, 600.0)
        val napEnd = clock("낮잠 끝난 시각", form.napEndText)
        val ex = num("운동(분)", form.exerciseMinText, 600.0)
        if (form.rested != null && form.rested !in 1..5) errors += "개운함은 1~5 중에서 골라 주세요."
        val anySurvey = listOf(caf, lastCaf, nap, napEnd, ex, form.exerciseIntensity, form.condition, form.rested,
            sleepLatency, awakeMinutes).any { it != null } ||
                form.targetWakeText.isNotBlank()
        val sourceNow = Instant.ofEpochMilli(base).atZone(zoneId)
        val sourceLocalDate = sourceNightDate(sourceNow, NextNightModel.profile.usualWake)
        fun latestEventOccurrence(label: String, clock: String?): Long? {
            val time = clock?.let { LocalTime.parse(it) } ?: return null
            var date = sourceNow.toLocalDate()
            if (time.isAfter(sourceNow.toLocalTime())) date = date.minusDays(1)
            return try { resolveLocal(date.atTime(time), zoneId).toEpochMilli() }
            catch (e: IllegalArgumentException) { errors += "$label: ${e.message}"; null }
        }
        val lastCaffeineMs = latestEventOccurrence("마지막 카페인", lastCaf)
        val napEndMs = latestEventOccurrence("낮잠 끝난 시각", napEnd)
        // 설문 날짜 = 기준 시각 − 6시간의 날짜 (웹 설문의 "예측 기준 날짜"와 같은 뜻)
        val survey = if (!anySurvey) null else SurveyDay(
            participant = "me", date = if (nowMs == null) SurveyCsv.surveyDateFor(base) else
                NextNightModel.liveNightId(java.time.Instant.ofEpochMilli(base).atZone(java.time.ZoneId.of("Asia/Seoul"))).toString(),
            caffeineUsed = caf?.let { it > 0 }, caffeineMg = caf, lastCaffeine = lastCaf,
            napMinutes = nap, napEnd = napEnd, exerciseMinutes = ex, exerciseEnd = null,
            exerciseIntensity = form.exerciseIntensity, fatigue = form.condition,
            steps = null, heartRate = null,
            alarmTime = CLOCK.matchEntire(form.targetWakeText.trim())?.let { m ->       // 웹 설문처럼 "07:30" 두 자리로
                String.format(Locale.US, "%02d:%02d", m.groupValues[1].toInt(), m.groupValues[2].toInt())
            },
            firstCommitment = null, examTomorrow = false, alcohol = false, illness = false,
            rested = form.rested?.takeIf { it in 1..5 },
            sourceZoneId = zoneId.id,
            sourceDate = sourceLocalDate.toString(),
            targetWakeMs = targetWake,
            lastCaffeineMs = lastCaffeineMs,
            napEndMs = napEndMs,
            sleepWakeDate = linked?.day ?: wake?.let { Instant.ofEpochMilli(it).atZone(ZoneId.of("Asia/Seoul")).toLocalDate().toString() },
            sleepLatencyMinutes = sleepLatency,
            awakeMinutes = awakeMinutes,
            sleepWakeMs = measuredWake
        )

        val any = listOf(bed, wake, plannedBed, targetWake).any { it != null }
        val input = if (!any) null else DirectInput(
            sleepStartMs = bed,
            sleepEndMs = wake,
            targetWakeMs = targetWake,
            plannedBedMs = plannedBed
        )
        val diary = if (errors.isEmpty() && bed != null && wake != null && nowMs != null) {
            val wakeDate = Instant.ofEpochMilli(wake).atZone(ZoneId.of("Asia/Seoul")).toLocalDate().toString()
            SleepRecord(
                id = "diary:$wakeDate",
                day = wakeDate,
                bedtime = Instant.ofEpochMilli(bed).toString(),
                wakeTime = Instant.ofEpochMilli(wake).toString(),
                durationMinutes = ((wake - bed) / 60_000L).toInt(),
                rested = form.rested?.takeIf { it in 1..5 },
                fatigue = NextNightModel.fatigueForEngine(form.condition),
                source = "manual",
                modifiedAt = Instant.ofEpochMilli(nowMs).toString(),
                sleepLatencyMinutes = sleepLatency,
                awakeMinutes = awakeMinutes,
                exerciseMinutes = ex
            )
        } else null
        return ParsedDirectInput(input, errors, survey, diary)
    }

    /** Strict wall-clock resolution. Overlap policy is the earlier instant, matching java.time's default. */
    internal fun resolveLocal(local: LocalDateTime, zoneId: ZoneId): Instant {
        val offsets = zoneId.rules.getValidOffsets(local)
        require(offsets.isNotEmpty()) { "${DATE_TIME.format(local)}은(는) ${zoneId.id}에서 존재하지 않는 시각이에요. 일광절약시간 전환을 확인해 주세요." }
        return ZonedDateTime.ofLocal(local, zoneId, offsets.first()).toInstant()
    }

    private fun sourceNightDate(now: ZonedDateTime, usualWakeText: String): LocalDate {
        val usualWake = runCatching { LocalTime.parse(usualWakeText) }.getOrDefault(LocalTime.of(7, 30))
        val boundary = minOf(12 * 60, usualWake.hour * 60 + usualWake.minute)
        val minute = now.hour * 60 + now.minute
        return if (minute < boundary) now.toLocalDate().minusDays(1) else now.toLocalDate()
    }
}
