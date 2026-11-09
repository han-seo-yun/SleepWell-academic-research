package app.sleepwell.analysis

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime

/** Selects only survey revisions that were actually available at an evaluation boundary. */
object SurveyObservationPolicy {
    data class ObservedSurvey(val survey: SurveyDay, val observedAt: Instant)

    fun currentNight(
        days: List<SurveyDay>,
        participant: String,
        now: Instant,
        usualWake: String = NextNightModel.profile.usualWake,
        zoneId: ZoneId = NextNightModel.currentCalculationZone()
    ): ObservedSurvey? {
        val nightId = NextNightModel.liveNightId(now.atZone(zoneId), usualWake).toString()
        return latestKnown(days, participant, nightId, now, zoneId, usualWake)
    }

    fun historicalNight(
        days: List<SurveyDay>,
        participant: String,
        nightId: String,
        zoneId: ZoneId = NextNightModel.currentCalculationZone(),
        usualWake: String = NextNightModel.profile.usualWake
    ): ObservedSurvey? {
        val cutoff = runCatching {
            LocalDate.parse(nightId).atTime(20, 0).atZone(zoneId).toInstant()
        }.getOrNull() ?: return null
        return latestKnown(days, participant, nightId, cutoff, zoneId, usualWake)
    }

    fun historicalCutoffMs(nightId: String, zoneId: ZoneId = NextNightModel.currentCalculationZone()): Long =
        LocalDate.parse(nightId).atTime(20, 0).atZone(zoneId).toInstant().toEpochMilli()

    private fun latestKnown(
        days: List<SurveyDay>,
        participant: String,
        nightId: String,
        boundary: Instant,
        zoneId: ZoneId,
        usualWake: String
    ): ObservedSurvey? = days.asSequence()
        .mapNotNull { survey -> parseObservedAt(survey.savedAt)?.let { ObservedSurvey(survey, it) } }
        .filter { observed ->
            val survey = observed.survey
            val effectiveNight = if (survey.sourceZoneId == zoneId.id) survey.date else
                NextNightModel.liveNightId(observed.observedAt.atZone(zoneId), usualWake).toString()
            survey.participant == participant && effectiveNight == nightId && survey.sleepWakeDate == null
        }
        .filter { !it.observedAt.isAfter(boundary) }
        .maxWithOrNull(compareBy<ObservedSurvey>({ it.observedAt }, { it.survey.savedAt.orEmpty() }))

    private fun parseObservedAt(value: String?): Instant? =
        value?.let { runCatching { ZonedDateTime.parse(it).toInstant() }.getOrNull() }
}
