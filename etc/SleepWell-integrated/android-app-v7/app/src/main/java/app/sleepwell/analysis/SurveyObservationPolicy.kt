package app.sleepwell.analysis

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime

/** Selects only survey revisions that were actually available at an evaluation boundary. */
object SurveyObservationPolicy {
    private val seoul = ZoneId.of("Asia/Seoul")

    data class ObservedSurvey(val survey: SurveyDay, val observedAt: Instant)

    fun currentNight(
        days: List<SurveyDay>,
        participant: String,
        now: Instant,
        usualWake: String = NextNightModel.profile.usualWake
    ): ObservedSurvey? {
        val nightId = NextNightModel.liveNightId(now.atZone(seoul), usualWake).toString()
        return latestKnown(days, participant, nightId, now)
    }

    fun historicalNight(
        days: List<SurveyDay>,
        participant: String,
        nightId: String
    ): ObservedSurvey? {
        val cutoff = runCatching {
            LocalDate.parse(nightId).atTime(20, 0).atZone(seoul).toInstant()
        }.getOrNull() ?: return null
        return latestKnown(days, participant, nightId, cutoff)
    }

    fun historicalCutoffMs(nightId: String): Long =
        LocalDate.parse(nightId).atTime(20, 0).atZone(seoul).toInstant().toEpochMilli()

    private fun latestKnown(
        days: List<SurveyDay>,
        participant: String,
        nightId: String,
        boundary: Instant
    ): ObservedSurvey? = days.asSequence()
        .filter { it.participant == participant && it.date == nightId && it.sleepWakeDate == null }
        .mapNotNull { survey -> parseObservedAt(survey.savedAt)?.let { ObservedSurvey(survey, it) } }
        .filter { !it.observedAt.isAfter(boundary) }
        .maxWithOrNull(compareBy<ObservedSurvey>({ it.observedAt }, { it.survey.savedAt.orEmpty() }))

    private fun parseObservedAt(value: String?): Instant? =
        value?.let { runCatching { ZonedDateTime.parse(it).toInstant() }.getOrNull() }
}
