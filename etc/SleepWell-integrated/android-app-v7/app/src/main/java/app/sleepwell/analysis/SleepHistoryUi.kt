package app.sleepwell.analysis

import android.content.Context
import app.sleepwell.health.CsvFileSource
import app.sleepwell.health.SleepStage
import app.sleepwell.user.SleepRecord
import java.io.File
import java.time.Instant

/** Read-only projection for the v7 history UI. Model/storage contracts remain unchanged. */
data class SleepHistoryNight(
    val day: String,
    val bedtimeMs: Long,
    val wakeMs: Long,
    val intervalMinutes: Int,
    val source: String,
    val sourceLabel: String,
    val isManual: Boolean,
    val sleepLatencyMinutes: Int?,
    val awakeMinutes: Int?,
    val rested: Int?,
    val fatigue: Int?,
    /** Raw linked answer is retained so event clocks can be rendered in the current device zone. */
    val survey: SurveyDay?,
    val observedAtMs: Long,
    /** Only explicitly observed stage rows; UNKNOWN gaps are never inferred as sleep. */
    val stageMinutes: Map<String, Int> = emptyMap(),
    val stageObservedMinutes: Int? = null,
    val fatigueScaleMax: Int? = null
)

object SleepHistoryUi {
    fun load(context: Context, nowMs: Long = System.currentTimeMillis()): List<SleepHistoryNight> {
        val surveys = SurveyStore.load(File(context.filesDir, "survey_log.csv"))
            .filter { it.participant == "me" }
        val wearable = ImportedSleepStore.loadModelNights(File(context.filesDir, "sleep_data"), nowMs, surveys)
        val diaries = runCatching { DiaryStore.load(File(context.filesDir, DiaryStore.FILE_NAME)) }
            .getOrDefault(emptyList())
        return merge(wearable, diaries, surveys, nowMs, loadStages(File(context.filesDir, "sleep_data")))
    }

    internal fun merge(
        wearable: List<SleepRecord>,
        diaries: List<SleepRecord>,
        surveys: List<SurveyDay>,
        nowMs: Long,
        stagesByRecordId: Map<String, Map<String, Int>> = emptyMap()
    ): List<SleepHistoryNight> {
        val now = Instant.ofEpochMilli(nowMs)
        val validWearable = wearable.filter { validAt(it, now) }
        val validDiary = diaries.filter { validAt(it, now) && it.source in setOf("manual", "diary") }
        val selected = (validWearable + validDiary)
            .groupBy { it.day }
            .mapNotNull { (_, records) ->
                records.filter { it.source in setOf("manual", "diary") }.maxByOrNull { it.modifiedAt }
                    ?: records.maxWithOrNull(compareBy<SleepRecord>({ it.durationMinutes }, { it.modifiedAt }))
            }
        val latestSurveys = surveys.asSequence()
            .filter { it.participant == "me" && it.sleepWakeDate != null }
            .mapNotNull { survey ->
                val saved = survey.savedAt?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: return@mapNotNull null
                if (saved.isAfter(now)) null else survey
            }
            .groupBy { it.sleepWakeDate!! }
            .mapValues { (_, rows) -> rows.maxByOrNull { it.savedAt.orEmpty() }!! }
        return selected.mapNotNull { record ->
            val bed = runCatching { Instant.parse(record.bedtime) }.getOrNull() ?: return@mapNotNull null
            val wake = runCatching { Instant.parse(record.wakeTime) }.getOrNull() ?: return@mapNotNull null
            val observed = runCatching { Instant.parse(record.modifiedAt) }.getOrNull() ?: return@mapNotNull null
            val survey = latestSurveys[record.day]?.takeIf { linked ->
                linked.savedAt?.let { runCatching { !Instant.parse(it).isBefore(wake) }.getOrDefault(false) } == true
            }
            val manual = record.source in setOf("manual", "diary")
            val stages = (stagesByRecordId[record.id]
                ?: stagesByRecordId["timing:${bed.toEpochMilli()}:${wake.toEpochMilli()}"]).orEmpty()
            SleepHistoryNight(
                day = record.day,
                bedtimeMs = bed.toEpochMilli(), wakeMs = wake.toEpochMilli(),
                intervalMinutes = record.durationMinutes,
                source = record.source,
                sourceLabel = when (record.source) {
                    "samsung_health" -> "삼성 헬스"
                    "samsung_health_csv" -> "삼성 헬스 가져오기"
                    "health_connect" -> "헬스 커넥트"
                    "manual", "diary" -> "직접 기록"
                    else -> "수면 기록"
                },
                isManual = manual,
                sleepLatencyMinutes = record.sleepLatencyMinutes ?: survey?.sleepLatencyMinutes,
                awakeMinutes = record.awakeMinutes ?: survey?.awakeMinutes,
                rested = survey?.rested ?: record.rested,
                fatigue = survey?.fatigue ?: record.fatigue,
                survey = survey,
                observedAtMs = observed.toEpochMilli(),
                stageMinutes = stages,
                stageObservedMinutes = stages.values.sum().takeIf { stages.isNotEmpty() },
                fatigueScaleMax = when {
                    survey?.fatigue != null -> 7
                    record.fatigue != null -> 5
                    else -> null
                }
            )
        }.sortedByDescending { it.wakeMs }
    }

    private fun validAt(record: SleepRecord, now: Instant): Boolean = runCatching {
        val bed = Instant.parse(record.bedtime)
        val wake = Instant.parse(record.wakeTime)
        val observed = Instant.parse(record.modifiedAt)
        wake.isAfter(bed) && !wake.isAfter(now) && !observed.isAfter(now) && record.durationMinutes >= 180
    }.getOrDefault(false)

    private fun loadStages(dir: File): Map<String, Map<String, Int>> {
        val result = linkedMapOf<String, Map<String, Int>>()
        fun add(file: File, sourceFor: (String) -> String) {
            if (!file.isFile) return
            val sessions = runCatching {
                file.inputStream().use { input ->
                    val parser = CsvFileSource()
                    parser.groupIntoSessionsForPipeline(parser.parseSleepStageCsvForPipeline(input))
                }
            }.getOrDefault(emptyList())
            sessions.forEach { session ->
                val minutes = session.stages.filter { it.stage != SleepStage.UNKNOWN }
                    .groupBy { it.stage }
                    .mapValues { (_, rows) -> (rows.sumOf { it.endMs - it.startMs } / 60_000L).toInt() }
                    .mapKeys { (stage, _) -> stage.name.lowercase() }
                    .filterValues { it > 0 }
                if (minutes.isNotEmpty()) {
                    result["wearable:${sourceFor(session.sessionId)}:${session.sessionId}"] = minutes
                    result.putIfAbsent("timing:${session.startMs}:${session.endMs}", minutes)
                }
            }
        }
        add(File(dir, SleepDataStore.ARCHIVE)) { "samsung_health_csv" }
        val healthConnect = File(dir, SleepDataStore.HEALTH_CONNECT)
        val metadata = runCatching {
            if (healthConnect.isFile) HealthConnectCsv.modelMetadataBySession(healthConnect.readText(Charsets.UTF_8)) else emptyMap()
        }.getOrDefault(emptyMap())
        add(healthConnect) { sessionId -> metadata[sessionId]?.source ?: "health_connect" }
        return result
    }
}
