package app.sleepwell.analysis

import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Read-only projection of saved survey rows back into the editable form. */
object SurveyFormRestore {
    private val clock = DateTimeFormatter.ofPattern("HH:mm", Locale.US)
    private val dateTime = DateTimeFormatter.ofPattern(DirectInputParser.PATTERN, Locale.US)

    data class Restored(
        val form: DirectInputForm,
        val context: SurveyDay?,
        val sleepSupplement: SurveyDay?,
        val contextObservedAtMs: Long?
    )

    fun restore(
        days: List<SurveyDay>,
        participant: String,
        now: Instant,
        displayZone: ZoneId,
        sleepWakeDate: String? = null,
        usualWake: String = NextNightModel.profile.usualWake
    ): Restored {
        val observed = SurveyObservationPolicy.currentNight(days, participant, now, usualWake, displayZone)
        val context = observed?.survey
        val supplement = sleepWakeDate?.let { wakeDate ->
            days.asSequence()
                .filter { it.participant == participant && it.sleepWakeDate == wakeDate }
                .mapNotNull { day -> observedAt(day)?.let { day to it } }
                .filter { !it.second.isAfter(now) }
                .maxByOrNull { it.second }
                ?.first
        }
        // Once a sleep is selected, its morning answers must never be borrowed from another night.
        val response = if (sleepWakeDate != null) supplement else context

        fun number(value: Double?): String = value?.let {
            if (it % 1.0 == 0.0) it.toLong().toString() else it.toString()
        }.orEmpty()
        fun shown(recordedMs: Long?, fallback: Instant?): String =
            (recordedMs?.let(Instant::ofEpochMilli) ?: fallback)
                ?.atZone(displayZone)?.format(clock).orEmpty()
        val targetText = when {
            context?.targetWakeMs != null && context.targetWakeMs > now.toEpochMilli() ->
                Instant.ofEpochMilli(context.targetWakeMs).atZone(displayZone).format(dateTime)
            context?.targetWakeMs != null -> ""
            else -> context?.nextAlarmAfter(now)?.atZone(displayZone)?.format(clock).orEmpty()
        }

        return Restored(
            form = DirectInputForm(
                targetWakeText = targetText,
                sleepLatencyText = response?.sleepLatencyMinutes?.toString().orEmpty(),
                awakeMinutesText = response?.awakeMinutes?.toString().orEmpty(),
                condition = response?.fatigue,
                rested = response?.rested,
                caffeineMgText = number(context?.caffeineMg),
                lastCaffeineText = shown(context?.lastCaffeineMs, context?.lastCaffeineInstant()),
                napMinText = number(context?.napMinutes),
                napEndText = shown(context?.napEndMs, context?.napEndInstant()),
                exerciseMinText = number(response?.exerciseMinutes),
                exerciseIntensity = response?.exerciseIntensity,
                sleepWakeDate = sleepWakeDate,
                fatigueScaleMax = response?.fatigueScaleMax ?: FatigueScale.CURRENT_MAX
            ),
            context = context,
            sleepSupplement = supplement,
            contextObservedAtMs = observed?.observedAt?.toEpochMilli()
        )
    }

    private fun observedAt(day: SurveyDay): Instant? = day.savedAt?.let {
        runCatching { ZonedDateTime.parse(it).toInstant() }.getOrNull()
    }
}
