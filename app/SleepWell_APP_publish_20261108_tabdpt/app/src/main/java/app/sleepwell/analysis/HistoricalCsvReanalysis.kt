package app.sleepwell.analysis

import app.sleepwell.user.SleepRecord
import java.time.Duration
import java.time.Instant
import java.time.ZoneId

/**
 * Explicit retrospective adapter for the frozen model's observation-time contract.
 * Only a model-call copy uses sleep completion as availability. The persisted receipt timestamp,
 * NextNightModel.importedRecords and snapshot input records are NEVER rewritten by this adapter.
 * This is a reanalysis of today's CSV and profile, not a forecast the app made in the past.
 */
internal object HistoricalCsvReanalysis {
    /** Keep the evaluation zone's 20:00 decision time, but never borrow sleep from the next displayed date. */
    fun dataCutoffMs(referenceMs: Long, displayZone: ZoneId, modelCutoffMs: Long): Long {
        val nextDate = Instant.ofEpochMilli(referenceMs).atZone(displayZone).toLocalDate().plusDays(1)
            .atStartOfDay(displayZone).toInstant().toEpochMilli()
        return minOf(modelCutoffMs, nextDate)
    }

    /** Match the archive merge rule: eligible CSV wins over overlapping Health Connect sessions. */
    fun candidates(records: List<SleepRecord>, cutoff: Instant): List<SleepRecord> {
        val csv = records.mapNotNull { prepare(it, cutoff) }
        return records.filter { record ->
            record.source == "samsung_health_csv" || csv.none { archived ->
                runCatching {
                    Instant.parse(record.bedtime).isBefore(Instant.parse(archived.wakeTime)) &&
                        Instant.parse(archived.bedtime).isBefore(Instant.parse(record.wakeTime))
                }.getOrDefault(false)
            }
        }
    }

    fun prepare(record: SleepRecord, cutoff: Instant): SleepRecord? = runCatching {
        if (record.source != "samsung_health_csv") return null
        val bed = Instant.parse(record.bedtime)
        val wake = Instant.parse(record.wakeTime)
        Instant.parse(record.modifiedAt) // Invalid receipt metadata must not become valid through projection.
        if (!bed.isBefore(wake) || !wake.isBefore(cutoff) ||
            Duration.between(bed, wake).toMinutes() !in NextNightModel.MAIN_SLEEP_MIN..960L) return null
        // Subjective provenance stays unchanged: the engine still rejects answers saved after cutoff.
        record.copy(modifiedAt = record.wakeTime)
    }.getOrNull()
}
