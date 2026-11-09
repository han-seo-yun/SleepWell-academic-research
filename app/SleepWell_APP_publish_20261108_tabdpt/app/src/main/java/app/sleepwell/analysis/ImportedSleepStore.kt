package app.sleepwell.analysis

import app.sleepwell.health.SleepSession
import app.sleepwell.health.SleepStage
import app.sleepwell.user.SleepRecord
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Duration
import java.time.Instant
import java.time.ZoneId

/** Persistent normalized wearable nights used only by the explicit wearable personalization policy. */
object ImportedSleepStore {
    const val FILE_NAME = "sleep_imported_model.json"
    private const val SCHEMA_VERSION = 1
    private const val MAIN_SLEEP_MINUTES = 180L
    private const val COVERAGE_TOLERANCE_MS = 60_000L
    private val seoul = ZoneId.of("Asia/Seoul")
    private val wearableSources = setOf("samsung_health", "health_connect", "samsung_health_csv")

    /** Merge an import without dropping older dates. Unchanged reimports retain their first observation time. */
    internal fun ingest(
        dir: File,
        sessions: List<SleepSession>,
        source: String,
        observedAtMs: Long = System.currentTimeMillis(),
        metadataBySession: Map<String, HealthConnectCsv.ModelMetadata> = emptyMap()
    ): List<SleepRecord> = synchronized(SleepDataStore.STORAGE_LOCK) {
        require(source in wearableSources) { "지원하지 않는 웨어러블 출처예요: $source" }
        val incoming = sessions.asSequence()
            .filter { it.endMs > it.startMs && it.endMs - it.startMs >= MAIN_SLEEP_MINUTES * 60_000L }
            .map { session ->
                val metadata = metadataBySession[session.sessionId]
                val recordSource = metadata?.source?.takeIf { it in wearableSources } ?: source
                // A provider modification can predate this app's first receipt. Model availability starts
                // only once both have happened, so historical provider timestamps cannot backdate evidence.
                val recordObservedAt = Instant.ofEpochMilli(maxOf(observedAtMs, metadata?.observedAtMs ?: Long.MIN_VALUE))
                val explicitlyObserved = if (recordSource == "samsung_health_csv") session.stages.isNotEmpty()
                    else metadata?.stageObserved == true
                normalize(session, recordSource, recordObservedAt, explicitlyObserved)
            }
            .groupBy { it.day }
            .mapValues { (_, values) -> values.maxWithOrNull(compareBy<SleepRecord>({ it.durationMinutes }, { it.wakeTime }))!! }
            .values
        val file = File(dir, FILE_NAME)
        val current = loadFile(file).associateBy { it.id }.toMutableMap()
        incoming.forEach { candidate ->
            val sameSourceDay = current.values.filter { it.source == candidate.source && it.day == candidate.day }
            val longest = sameSourceDay.maxByOrNull { it.durationMinutes }
            val previous = current[candidate.id]
            // A stable provider id is an update, even when its corrected duration becomes shorter.
            if (previous != null || longest == null || candidate.durationMinutes >= longest.durationMinutes) {
                current.entries.removeAll { (_, existing) ->
                    existing.source == candidate.source && existing.day == candidate.day
                }
                current[candidate.id] = if (previous != null && sameTimingAndMeasurements(previous, candidate)) previous else candidate
            }
        }
        val all = current.values.sortedBy { it.wakeTime }
        write(file, all)
        all
    }

    /**
     * Load one longest imported main sleep per wake date. Legacy raw files are migrated once using their
     * filesystem modification time, never the current app-start time. A matching questionnaire may add
     * subjective values without changing the wearable timing observation timestamp.
     */
    fun loadModelNights(
        dir: File,
        nowMs: Long = System.currentTimeMillis(),
        surveys: List<SurveyDay> = emptyList()
    ): List<SleepRecord> = synchronized(SleepDataStore.STORAGE_LOCK) {
        val now = Instant.ofEpochMilli(nowMs)
        loadReanalysisCandidates(dir, nowMs)
            .groupBy { it.day }
            .mapValues { (_, values) -> values.maxWithOrNull(compareBy<SleepRecord>({ it.durationMinutes }, { it.modifiedAt }))!! }
            .values
            .map { record -> attachQuestionnaire(record, surveys, now) }
            .sortedBy { it.wakeTime }
    }

    /** Preserve both CSV and Health Connect candidates until historical availability is evaluated. */
    internal fun loadReanalysisCandidates(dir: File, nowMs: Long = System.currentTimeMillis()): List<SleepRecord> =
        synchronized(SleepDataStore.STORAGE_LOCK) {
            val file = File(dir, FILE_NAME)
            if (!file.isFile) migrateRawImports(dir)
            val now = Instant.ofEpochMilli(nowMs)
            loadFile(file)
                .filter { runCatching { !Instant.parse(it.modifiedAt).isAfter(now) }.getOrDefault(false) }
                .filter { runCatching { !Instant.parse(it.wakeTime).isAfter(now) }.getOrDefault(false) }
                .sortedBy { it.wakeTime }
        }

    private fun migrateRawImports(dir: File) {
        val archive = File(dir, SleepDataStore.ARCHIVE)
        if (archive.isFile) {
            ingest(dir, read(archive), "samsung_health_csv", archive.lastModified().takeIf { it > 0 } ?: 0L)
        }
        val healthConnect = File(dir, SleepDataStore.HEALTH_CONNECT)
        if (healthConnect.isFile) {
            // Old generated CSV has no stage-observed flag, so measurement fields deliberately remain null.
            ingest(dir, read(healthConnect), "health_connect", healthConnect.lastModified().takeIf { it > 0 } ?: 0L)
        }
    }

    /** Join a historical answer from raw stored nights, avoiding later answers overwriting earlier ones. */
    internal fun withQuestionnairesBefore(records: List<SleepRecord>, surveys: List<SurveyDay>, cutoff: Instant): List<SleepRecord> =
        records.map { attachQuestionnaire(it, surveys, cutoff.minusNanos(1)) }

    private fun attachQuestionnaire(record: SleepRecord, surveys: List<SurveyDay>, now: Instant): SleepRecord {
        val wake = runCatching { Instant.parse(record.wakeTime) }.getOrNull() ?: return record
        val observed = surveys.asSequence()
            .filter { it.participant == "me" && it.sleepWakeDate == record.day }
            .mapNotNull { survey ->
                val at = survey.savedAt?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: return@mapNotNull null
                if (at.isBefore(wake) || at.isAfter(now)) null else at to survey
            }
            .maxByOrNull { it.first } ?: return record
        val (at, survey) = observed
        val hasSupplement = listOf(
            survey.rested, survey.fatigue, survey.exerciseMinutes, survey.steps,
            survey.sleepLatencyMinutes, survey.awakeMinutes
        ).any { it != null }
        if (!hasSupplement) return record
        val candidateLatency = record.sleepLatencyMinutes ?: survey.sleepLatencyMinutes
        val candidateAwake = record.awakeMinutes ?: survey.awakeMinutes
        val validMeasurements = (candidateLatency == null || candidateLatency in 0..180) &&
            (candidateAwake == null || candidateAwake in 0..600) &&
            (candidateLatency ?: 0) + (candidateAwake ?: 0) < record.durationMinutes
        return record.copy(
            rested = survey.rested?.takeIf { it in 1..5 },
            fatigue = NextNightModel.fatigueForEngine(survey.fatigue, survey.fatigueScaleMax),
            sleepLatencyMinutes = if (validMeasurements) candidateLatency else record.sleepLatencyMinutes,
            awakeMinutes = if (validMeasurements) candidateAwake else record.awakeMinutes,
            steps = survey.steps,
            exerciseMinutes = survey.exerciseMinutes,
            subjectiveSource = "questionnaire",
            subjectiveObservedAt = at.toString()
        )
    }

    private fun normalize(session: SleepSession, source: String, observedAt: Instant, stageObserved: Boolean): SleepRecord {
        val start = Instant.ofEpochMilli(session.startMs)
        val end = Instant.ofEpochMilli(session.endMs)
        val measurements = stageMeasurements(session, stageObserved)
        val day = end.atZone(seoul).toLocalDate().toString()
        return SleepRecord(
            id = "wearable:$source:${session.sessionId}", day = day, bedtime = start.toString(), wakeTime = end.toString(),
            durationMinutes = Duration.between(start, end).toMinutes().toInt(), rested = null, fatigue = null,
            source = source, modifiedAt = observedAt.toString(),
            sleepLatencyMinutes = measurements?.first, awakeMinutes = measurements?.second
        )
    }

    /** Full, non-overlapping observed coverage is required; latency also needs explicit leading wake. */
    private fun stageMeasurements(session: SleepSession, explicitlyObserved: Boolean): Pair<Int?, Int?>? {
        if (!explicitlyObserved) return null
        val stages = session.stages
            .map { it.copy(startMs = maxOf(it.startMs, session.startMs), endMs = minOf(it.endMs, session.endMs)) }
            .filter { it.endMs > it.startMs }
            .sortedBy { it.startMs }
        if (stages.isEmpty() || stages.any { it.stage == SleepStage.UNKNOWN }) return null
        if (stages.first().startMs - session.startMs > COVERAGE_TOLERANCE_MS ||
            session.endMs - stages.last().endMs > COVERAGE_TOLERANCE_MS) return null
        if (stages.zipWithNext().any { (a, b) ->
                b.startMs < a.endMs || b.startMs - a.endMs > COVERAGE_TOLERANCE_MS
            }) return null
        val firstSleep = stages.indexOfFirst { it.stage.isSleep }
        if (firstSleep < 0) return null
        val leading = stages.take(firstSleep)
        val latency = leading.takeIf { it.isNotEmpty() && it.all { stage -> stage.stage == SleepStage.WAKE } }
            ?.sumOf { it.endMs - it.startMs }
        val awake = stages.drop(firstSleep + 1).filter { it.stage == SleepStage.WAKE }.sumOf { it.endMs - it.startMs }
        return latency?.let { (it / 60_000L).toInt() } to (awake / 60_000L).toInt()
    }

    private fun sameTimingAndMeasurements(a: SleepRecord, b: SleepRecord): Boolean =
        a.bedtime == b.bedtime && a.wakeTime == b.wakeTime && a.durationMinutes == b.durationMinutes &&
            a.sleepLatencyMinutes == b.sleepLatencyMinutes && a.awakeMinutes == b.awakeMinutes && a.source == b.source

    private fun read(file: File): List<SleepSession> {
        if (!file.isFile) return emptyList()
        val parser = app.sleepwell.health.CsvFileSource()
        val rows = file.inputStream().use { parser.parseSleepStageCsvForPipeline(it) }
        return parser.groupIntoSessionsForPipeline(rows)
    }

    private fun loadFile(file: File): List<SleepRecord> {
        if (!file.isFile) return emptyList()
        val root = JSONObject(file.readText(Charsets.UTF_8))
        require(root.optInt("schema_version", -1) == SCHEMA_VERSION) { "지원하지 않는 가져온 수면 형식이에요." }
        val array = root.getJSONArray("records")
        return (0 until array.length()).map { index -> fromJson(array.getJSONObject(index)) }
    }

    private fun write(file: File, records: List<SleepRecord>) {
        file.parentFile?.mkdirs()
        val payload = JSONObject().put("schema_version", SCHEMA_VERSION).put("records", JSONArray().also { array ->
            records.forEach { array.put(toJson(it)) }
        }).toString(2)
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(payload, Charsets.UTF_8)
        try {
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (_: AtomicMoveNotSupportedException) {
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }

    private fun toJson(record: SleepRecord) = JSONObject()
        .put("id", record.id).put("day", record.day).put("bedtime", record.bedtime).put("wake_time", record.wakeTime)
        .put("duration_minutes", record.durationMinutes).put("source", record.source).put("modified_at", record.modifiedAt)
        .put("sleep_latency_minutes", record.sleepLatencyMinutes ?: JSONObject.NULL)
        .put("awake_minutes", record.awakeMinutes ?: JSONObject.NULL)

    private fun fromJson(value: JSONObject) = SleepRecord(
        id = value.getString("id"), day = value.getString("day"), bedtime = value.getString("bedtime"),
        wakeTime = value.getString("wake_time"), durationMinutes = value.getInt("duration_minutes"),
        rested = null, fatigue = null, source = value.getString("source"), modifiedAt = value.getString("modified_at"),
        sleepLatencyMinutes = value.intOrNull("sleep_latency_minutes"), awakeMinutes = value.intOrNull("awake_minutes")
    )

    private fun JSONObject.intOrNull(key: String): Int? = if (!has(key) || isNull(key)) null else getInt(key)
}
