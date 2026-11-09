package app.sleepwell.ui

import app.sleepwell.analysis.SavedResult
import app.sleepwell.analysis.SleepHistoryNight
import app.sleepwell.analysis.SurveyDay
import java.time.Instant
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZoneId

/** Pure, read-only projection shared by the history calendar and regression tests. */
data class HistoryCalendarDay(
    val date: LocalDate,
    val sleepRecords: List<SleepHistoryNight>,
    val savedResults: List<SavedResult>,
    val surveys: List<SurveyDay> = emptyList()
) {
    val hasSleep: Boolean get() = sleepRecords.isNotEmpty()
    val hasSavedResult: Boolean get() = savedResults.isNotEmpty()
    val hasSurvey: Boolean get() = surveys.isNotEmpty()
}

object HistoryCalendarProjection {
    fun resultDate(result: SavedResult, zone: ZoneId): LocalDate? {
        // New rows retain an absolute basis instant and can follow the device zone just like live history.
        // dayDate remains the compatibility key for received/legacy rows without time provenance.
        if (result.sourceZoneId != null && result.basisMs > 0L)
            return Instant.ofEpochMilli(result.basisMs).atZone(zone).toLocalDate()
        return result.dayDate.takeIf { it.isNotBlank() }
            ?.let { text -> runCatching { LocalDate.parse(text) }.getOrNull() }
            ?: result.basisMs.takeIf { it > 0L }?.let { Instant.ofEpochMilli(it).atZone(zone).toLocalDate() }
    }

    fun sleepDate(night: SleepHistoryNight, zone: ZoneId): LocalDate =
        Instant.ofEpochMilli(night.wakeMs).atZone(zone).toLocalDate()

    fun surveyDate(survey: SurveyDay, zone: ZoneId): LocalDate? {
        if (survey.sleepWakeDate != null) {
            return survey.sleepWakeMs?.takeIf { it > 0L }
                ?.let { Instant.ofEpochMilli(it).atZone(zone).toLocalDate() }
                ?: runCatching { LocalDate.parse(survey.sleepWakeDate) }.getOrNull()
        }
        // Context dates are semantic night dates in their declared source zone. They must not
        // drift to the previous/next date merely because the device display zone changes.
        if (runCatching { ZoneId.of(survey.sourceZoneId) }.isFailure) return null
        return runCatching { LocalDate.parse(survey.sourceDate ?: survey.date) }.getOrNull()
    }

    fun days(
        nights: List<SleepHistoryNight>,
        results: List<SavedResult>,
        zone: ZoneId,
        surveys: List<SurveyDay> = emptyList(),
        includeSyntheticResults: Boolean = false
    ): Map<LocalDate, HistoryCalendarDay> {
        val visibleResults = if (includeSyntheticResults) results else results.filterNot { it.source.isSyntheticSource() }
        val visibleSurveys = surveys.filter { it.participant == "me" }
        val dates = (nights.map { sleepDate(it, zone) } +
            visibleResults.mapNotNull { resultDate(it, zone) } + visibleSurveys.mapNotNull { surveyDate(it, zone) }).toSortedSet()
        return dates.associateWith { date ->
            HistoryCalendarDay(
                date,
                nights.filter { sleepDate(it, zone) == date }.sortedBy { it.bedtimeMs },
                visibleResults.filter { resultDate(it, zone) == date }.sortedBy { it.savedAtMs },
                visibleSurveys.filter { surveyDate(it, zone) == date }.sortedBy { it.savedAt.orEmpty() }
            )
        }
    }

    fun monthCells(month: YearMonth): List<LocalDate?> {
        val leadingSundayCells = month.atDay(1).dayOfWeek.value % 7
        val values = MutableList<LocalDate?>(leadingSundayCells) { null }
        values += (1..month.lengthOfMonth()).map(month::atDay)
        while (values.size % 7 != 0) values += null
        return values
    }
}

internal fun String.isSyntheticSource(): Boolean {
    val normalized = lowercase()
    return normalized.contains("test") || normalized.contains("demo") ||
        normalized.contains("합성") || normalized.contains("시연")
}
