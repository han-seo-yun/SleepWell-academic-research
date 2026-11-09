package app.sleepwell.user

import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime

/** Recorded user context; not fitted coefficients in the frozen research models. */
data class DailyContext(
    val nightId: String,
    val kind: String,
    val firstStart: String?,
    val preparationMinutes: Int,
    val napStart: String?,
    val napEnd: String?,
    val caffeineAt: String?,
    val caffeineMg: Int?,
    val observedAt: String
)

internal fun DailyContext.toJson(): JSONObject = JSONObject()
    .put("night_id", nightId).put("kind", kind)
    .put("first_start", firstStart ?: JSONObject.NULL)
    .put("preparation_minutes", preparationMinutes)
    .put("nap_start", napStart ?: JSONObject.NULL).put("nap_end", napEnd ?: JSONObject.NULL)
    .put("caffeine_at", caffeineAt ?: JSONObject.NULL).put("caffeine_mg", caffeineMg ?: JSONObject.NULL)
    .put("observed_at", observedAt)

internal fun dailyContextFromJson(v: JSONObject) = DailyContext(
    v.getString("night_id"), v.getString("kind"), v.optionalText("first_start"),
    v.getInt("preparation_minutes"), v.optionalText("nap_start"), v.optionalText("nap_end"),
    v.optionalText("caffeine_at"), if (v.isNull("caffeine_mg")) null else v.getInt("caffeine_mg"),
    v.getString("observed_at")
)
private fun JSONObject.optionalText(key: String): String? = if (!has(key) || isNull(key)) null else getString(key)

/**
 * Resolves an explicit next-day commitment into a usable wake constraint.
 *
 * The date carried by [firstStart] is part of the commitment: stale dates are
 * rejected instead of being silently shifted.  The resulting preparation
 * deadline must also still be ahead of both the fixed 20:00 decision cutoff
 * and the current evaluation time.
 */
internal fun DailyContext.resolveScheduleWake(
    cutoff: ZonedDateTime,
    now: ZonedDateTime
): ZonedDateTime? {
    if (preparationMinutes !in 0..240) return null
    val first = firstStart?.let { value ->
        runCatching { Instant.parse(value).atZone(cutoff.zone) }.getOrNull()
    } ?: return null
    if (first.toLocalDate() != cutoff.plusDays(1).toLocalDate()) return null

    val wakeConstraint = first.minusMinutes(preparationMinutes.toLong())
    val localNow = now.withZoneSameInstant(cutoff.zone)
    return wakeConstraint.takeIf { it.isAfter(cutoff) && it.isAfter(localNow) }
}

internal fun buildDailyContext(
    nightId: String, kind: String, firstClock: String, preparation: String,
    napStartClock: String, napEndClock: String, caffeineClock: String, caffeineAmount: String,
    now: Instant = Instant.now()
): DailyContext {
    val zone = ZoneId.of("Asia/Seoul")
    val date = LocalDate.parse(nightId)
    require(date == now.atZone(zone).toLocalDate()) { "오늘 날짜의 생활 정보를 입력해주세요." }
    require(kind in setOf("work", "class", "first")) { "일정 종류를 선택해주세요." }
    val prep = preparation.trim().toIntOrNull()
    require(prep != null && prep in 0..240) { "준비·이동 시간은 0~240분으로 입력해주세요." }
    fun at(day: LocalDate, clock: String): Instant? = if (clock.isBlank()) null else
        runCatching { day.atTime(LocalTime.parse(clock.trim())).atZone(zone).toInstant() }
            .getOrElse { throw IllegalArgumentException("시각은 07:30처럼 입력해주세요.") }
    val first = at(date.plusDays(1), firstClock)
    val napStart = at(date, napStartClock)
    val napEnd = at(date, napEndClock)
    require((napStart == null) == (napEnd == null)) { "낮잠 시작과 종료를 함께 입력해주세요." }
    if (napStart != null && napEnd != null) {
        require(napEnd > napStart && Duration.between(napStart, napEnd).toMinutes() in 1..360) { "낮잠은 종료가 시작 이후이며 6시간 이내여야 합니다." }
        require(napEnd <= now) { "이미 끝난 낮잠만 기록해주세요." }
    }
    val caffeine = at(date, caffeineClock)
    val mg = if (caffeineAmount.isBlank()) null else caffeineAmount.trim().toIntOrNull()
        ?: throw IllegalArgumentException("카페인 양은 숫자로 입력해주세요.")
    require((caffeine == null) == (mg == null)) { "카페인 시각과 양을 함께 입력해주세요." }
    if (caffeine != null) require(caffeine <= now) { "이미 섭취한 카페인만 기록해주세요." }
    if (mg != null) require(mg in 0..1000) { "카페인 양은 0~1000mg으로 입력해주세요." }
    return DailyContext(nightId, kind, first?.toString(), prep, napStart?.toString(),
        napEnd?.toString(), caffeine?.toString(), mg, now.toString())
}
