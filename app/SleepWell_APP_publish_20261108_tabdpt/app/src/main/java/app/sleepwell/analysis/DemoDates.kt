package app.sleepwell.analysis

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** Only synthetic fixtures are rebased; imported records keep their real timestamps. */
object DemoDates {
    private val stamp = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")
    private val dates = Regex("\\d{4}-\\d{2}-\\d{2}")

    fun rebase(sleepCsv: String, surveyCsv: String, now: Instant): Pair<String, String> {
        val end = sleepCsv.lineSequence().drop(1).filter { it.isNotBlank() }
            .map { LocalDateTime.parse(it.split(',')[1], stamp).toInstant(ZoneOffset.UTC) }.maxOrNull()
            ?: return sleepCsv to surveyCsv
        val seoul = ZoneId.of("Asia/Seoul")
        var shift = ChronoUnit.DAYS.between(end.atZone(seoul).toLocalDate(), now.atZone(seoul).toLocalDate())
        // Before this morning's synthetic wake time, use yesterday's completed night.
        if (end.plusSeconds(shift * 86_400).isAfter(now)) shift--
        fun move(text: String) = dates.replace(text) { LocalDate.parse(it.value).plusDays(shift).toString() }
        return move(sleepCsv) to move(surveyCsv)
    }
}
