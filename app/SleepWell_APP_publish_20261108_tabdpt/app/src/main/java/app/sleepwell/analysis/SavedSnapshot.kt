package app.sleepwell.analysis

import app.sleepwell.health.AnalysisResult
import app.sleepwell.health.DirectInput
import app.sleepwell.health.PredictedPoint
import app.sleepwell.health.PredictionOrigin
import app.sleepwell.health.PropensityPoint
import app.sleepwell.health.Recommendation
import app.sleepwell.health.SleepRecord
import app.sleepwell.user.SleepRecord as PersonalizationRecord
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.Properties
import java.util.UUID

/**
 * Immutable inputs and output behind a saved result.
 *
 * A replay never invokes [NextNightModel] and never reads its mutable live state. The calculated
 * [AnalysisResult] is frozen here and only passed through [AnalysisPresenter] for the current
 * display timezone. This keeps a historical graph stable as new wearable records arrive.
 */
object SavedSnapshot {
    const val DIR = "saved_snapshots"
    private const val SCHEMA = 1
    private val leaseLock = Any()
    private val leaseSessionId = UUID.randomUUID().toString()
    private val initializedLeaseRoots = mutableSetOf<String>()

    data class Snapshot(
        val savedAtMs: Long,
        val result: AnalysisResult,
        val direct: DirectInput?,
        val condition: Int?,
        val survey: SurveyDay?,
        val input: File?,
        val dayMs: Long?,
        val basisMs: Long?,
        val latestRefMs: Long?,
        val dataStartMs: Long?,
        val recommendationReferenceMs: Long?
    )

    data class Replay(val display: AnalysisDisplay?, val result: AnalysisResult?, val error: Notice?)
    data class CapturedInput(val file: File, val sha256: String, internal val lease: File)

    fun dir(base: File, savedAtMs: Long): File = File(base, "$DIR/$savedAtMs")

    fun exists(base: File, savedAtMs: Long): Boolean =
        File(dir(base, savedAtMs), "meta.properties").isFile &&
            File(dir(base, savedAtMs), "result.json").isFile

    fun inputSha256(file: File): String = sha256(file)

    /** Freeze the exact calculation CSV before analysis so background sync cannot replace it. */
    fun captureInput(base: File, input: File): CapturedInput {
        require(input.isFile) { "계산용 CSV가 없어요." }
        return synchronized(leaseLock) {
            val root = File(base, DIR).apply { mkdirs() }
            ensureLeaseSession(root)
            val (destination, sha) = installInput(root, input)
            val lease = createLease(root, sha)
            CapturedInput(destination, sha, lease)
        }
    }

    /** Retain an installed immutable input while another display or save operation uses it. */
    fun retainCapturedInput(base: File, captured: CapturedInput): CapturedInput = synchronized(leaseLock) {
        val root = File(base, DIR).apply { mkdirs() }
        ensureLeaseSession(root)
        check(captured.file.isFile && sha256(captured.file) == captured.sha256) {
            "보관된 계산용 CSV를 찾지 못했거나 내용이 바뀌었어요."
        }
        CapturedInput(captured.file, captured.sha256, createLease(root, captured.sha256))
    }

    /** Release an abandoned calculation capture; saved snapshots keep their referenced copy. */
    fun releaseCapturedInput(base: File, captured: CapturedInput?) {
        if (captured == null) return
        synchronized(leaseLock) {
            val root = File(base, DIR)
            captured.lease.delete()
            if (!isInputReferenced(root, captured.sha256) && !isInputLeased(root, captured.sha256)) captured.file.delete()
        }
    }

    /**
     * Saves a snapshot atomically. If [expectedInputSha256] is supplied, saving fails when the
     * working calculation CSV changed after analysis instead of associating the result with the
     * wrong input file.
     */
    fun save(
        base: File,
        savedAtMs: Long,
        result: AnalysisResult,
        direct: DirectInput?,
        condition: Int?,
        survey: SurveyDay?,
        input: File?,
        expectedInputSha256: String? = null,
        surveys: List<SurveyDay> = emptyList(),
        profile: NextNightModel.ModelProfile = NextNightModel.ModelProfile(),
        diaryRecords: List<PersonalizationRecord> = emptyList(),
        importedRecords: List<PersonalizationRecord> = emptyList(),
        wearablePersonalizationEnabled: Boolean = false,
        dayMs: Long? = null,
        basisMs: Long? = null,
        latestRefMs: Long? = null,
        dataStartMs: Long? = null,
        recommendationReferenceMs: Long? = null
    ) {
        require(savedAtMs > 0L) { "저장 시각이 올바르지 않아요." }
        val root = File(base, DIR).apply { mkdirs() }
        val finalDir = dir(base, savedAtMs)
        check(!finalDir.exists()) { "같은 저장 시각의 결과가 이미 있어요." }

        val installedInput = input?.takeIf { it.isFile }?.let { installInput(root, it) }
        val inputSha = installedInput?.second
        if (expectedInputSha256 != null) {
            check(inputSha == expectedInputSha256) { "분석 뒤 계산용 CSV가 바뀌었어요. 새 결과로 다시 분석한 뒤 저장해 주세요." }
        }

        val temporary = File(root, ".tmp-$savedAtMs-${System.nanoTime()}")
        try {
            check(temporary.mkdirs()) { "결과 스냅샷 임시 폴더를 만들지 못했어요." }
            val meta = Properties().apply {
                setProperty("schema", SCHEMA.toString())
                setProperty("savedAtMs", savedAtMs.toString())
                inputSha?.let { setProperty("inputSha", it) }
                putLong("sleepStartMs", direct?.sleepStartMs)
                putLong("sleepEndMs", direct?.sleepEndMs)
                putLong("targetWakeMs", direct?.targetWakeMs)
                putLong("plannedBedMs", direct?.plannedBedMs)
                condition?.let { setProperty("condition", it.toString()) }
                putLong("dayMs", dayMs)
                putLong("basisMs", basisMs)
                putLong("latestRefMs", latestRefMs)
                putLong("dataStartMs", dataStartMs)
                putLong("recommendationReferenceMs", recommendationReferenceMs)
                setProperty("wearablePersonalizationEnabled", wearablePersonalizationEnabled.toString())
                setProperty("ageBand", profile.ageBand)
                setProperty("sex", profile.sex)
                setProperty("activityMinutes", profile.activityMinutes.toString())
                setProperty("desiredSleepMinutes", profile.desiredSleepMinutes.toString())
                setProperty("usualBed", profile.usualBed)
                setProperty("usualWake", profile.usualWake)
                setProperty("onboardingCompleted", profile.onboardingCompleted.toString())
            }
            writeProperties(File(temporary, "meta.properties"), meta)
            writeSynced(File(temporary, "result.json"), resultToJson(result).toString().toByteArray(Charsets.UTF_8))
            if (survey != null) writeSynced(File(temporary, "survey.csv"), SurveyStore.toCsv(listOf(survey)).toByteArray(Charsets.UTF_8))
            writeSynced(File(temporary, "surveys.csv"), SurveyStore.toCsv(surveys).toByteArray(Charsets.UTF_8))
            writeSynced(
                File(temporary, "personalization_records.json"),
                JSONObject()
                    .put("diary", recordsToJson(diaryRecords))
                    .put("imported", recordsToJson(importedRecords))
                    .toString().toByteArray(Charsets.UTF_8)
            )
            check(temporary.renameTo(finalDir)) { "결과 스냅샷 저장을 완료하지 못했어요." }
        } catch (e: Exception) {
            temporary.deleteRecursively()
            throw e
        }
    }

    fun load(base: File, savedAtMs: Long): Snapshot? {
        val folder = dir(base, savedAtMs)
        val metaFile = File(folder, "meta.properties")
        val resultFile = File(folder, "result.json")
        if (!metaFile.isFile || !resultFile.isFile) return null
        return runCatching {
            val p = Properties().apply { metaFile.inputStream().use(::load) }
            val direct = DirectInput(p.long("sleepStartMs"), p.long("sleepEndMs"), p.long("targetWakeMs"), p.long("plannedBedMs"))
                .takeIf { listOf(it.sleepStartMs, it.sleepEndMs, it.targetWakeMs, it.plannedBedMs).any { value -> value != null } }
            val survey = File(folder, "survey.csv").takeIf { it.isFile }
                ?.let { SurveyCsv.parse(it.readText(Charsets.UTF_8)).days.firstOrNull() }
            val input = p.getProperty("inputSha")?.let { File(File(base, DIR), "inputs/$it.csv") }?.takeIf { it.isFile }
            Snapshot(
                savedAtMs = p.long("savedAtMs") ?: savedAtMs,
                result = resultFromJson(JSONObject(resultFile.readText(Charsets.UTF_8))),
                direct = direct,
                condition = p.getProperty("condition")?.toIntOrNull(),
                survey = survey,
                input = input,
                dayMs = p.long("dayMs"),
                basisMs = p.long("basisMs"),
                latestRefMs = p.long("latestRefMs"),
                dataStartMs = p.long("dataStartMs"),
                recommendationReferenceMs = p.long("recommendationReferenceMs")
            )
        }.getOrNull()
    }

    fun replay(snapshot: Snapshot): Replay = runCatching {
        val selectedDay = snapshot.basisMs?.let { basis ->
            val latest = snapshot.latestRefMs ?: snapshot.result.scHistory.lastOrNull()?.timestampMs
                ?: snapshot.result.calculationTimeMs
            AnalysisPresenter.days(snapshot.result.sleepRecords, snapshot.direct, latest, snapshot.dataStartMs)
                .firstOrNull { it.basisMs == basis }?.startMs
        } ?: snapshot.dayMs
        val display = AnalysisPresenter.build(
            snapshot.result,
            dayMs = selectedDay,
            direct = snapshot.direct,
            condition = snapshot.condition,
            latestRefMs = snapshot.latestRefMs,
            dataStartMs = snapshot.dataStartMs,
            survey = snapshot.survey,
            recommendationReferenceMs = snapshot.recommendationReferenceMs
        )
        Replay(display, snapshot.result, null)
    }.getOrElse { Replay(null, snapshot.result, Notice("저장 결과를 열지 못했어요", listOf(it.message ?: it::class.java.simpleName))) }

    fun delete(base: File, savedAtMs: Long) {
        val root = File(base, DIR)
        val sha = runCatching {
            Properties().apply { File(dir(base, savedAtMs), "meta.properties").inputStream().use(::load) }
                .getProperty("inputSha")
        }.getOrNull()
        dir(base, savedAtMs).deleteRecursively()
        if (sha != null && !isInputReferenced(root, sha) && !isInputLeased(root, sha)) File(root, "inputs/$sha.csv").delete()
    }

    private fun isInputReferenced(root: File, sha: String): Boolean = root.listFiles().orEmpty().any { folder ->
        folder.isDirectory && folder.name != "inputs" && !folder.name.startsWith(".tmp-") && runCatching {
            Properties().apply { File(folder, "meta.properties").inputStream().use(::load) }.getProperty("inputSha") == sha
        }.getOrDefault(false)
    }

    private fun isInputLeased(root: File, sha: String): Boolean =
        File(root, "leases").listFiles().orEmpty().any { lease ->
            lease.isFile && runCatching { lease.readText(Charsets.UTF_8) == sha }.getOrDefault(false)
        }

    /**
     * A process death cannot run Compose disposal. On the first capture in a new process, remove
     * leases owned by earlier processes and then delete only inputs that no saved snapshot or
     * current-process capture still owns.
     */
    private fun ensureLeaseSession(root: File) {
        val key = runCatching { root.canonicalPath }.getOrElse { root.absolutePath }
        if (initializedLeaseRoots.add(key)) cleanupStaleLeases(root, leaseSessionId)
    }

    private fun createLease(root: File, sha: String): File {
        val lease = File(File(root, "leases").apply { mkdirs() }, "$leaseSessionId.$sha.${System.nanoTime()}.lease")
        writeSynced(lease, sha.toByteArray(Charsets.UTF_8))
        return lease
    }

    internal fun cleanupStaleLeases(root: File, activeSessionId: String) = synchronized(leaseLock) {
        val leases = File(root, "leases")
        leases.listFiles().orEmpty().filter { it.isFile }.forEach { lease ->
            if (!lease.name.startsWith("$activeSessionId.")) lease.delete()
        }
        val inputs = File(root, "inputs")
        inputs.listFiles().orEmpty().filter { it.isFile && it.extension == "csv" }.forEach { input ->
            val sha = input.nameWithoutExtension
            if (!isInputReferenced(root, sha) && !isInputLeased(root, sha)) input.delete()
        }
    }

    private fun Properties.putLong(name: String, value: Long?) { value?.let { setProperty(name, it.toString()) } }
    private fun Properties.long(name: String): Long? = getProperty(name)?.toLongOrNull()

    private fun writeProperties(file: File, properties: Properties) {
        FileOutputStream(file).use { output -> properties.store(output, "SleepWell immutable saved result"); output.fd.sync() }
    }

    private fun writeSynced(file: File, bytes: ByteArray) {
        FileOutputStream(file).use { output -> output.write(bytes); output.fd.sync() }
    }

    /** Copy once, then hash that immutable copy before installing it under the digest name. */
    private fun installInput(root: File, source: File): Pair<File, String> {
        val inputs = File(root, "inputs").apply { mkdirs() }
        val temporary = File(inputs, ".capture-${System.nanoTime()}.tmp")
        try {
            source.inputStream().use { input -> FileOutputStream(temporary).use { output -> input.copyTo(output); output.fd.sync() } }
            val sha = sha256(temporary)
            val destination = File(inputs, "$sha.csv")
            if (destination.isFile) {
                check(sha256(destination) == sha) { "보관된 계산용 CSV의 내용 해시가 맞지 않아요." }
            } else if (!temporary.renameTo(destination)) {
                check(destination.isFile && sha256(destination) == sha) { "계산용 CSV 사본을 저장하지 못했어요." }
            }
            return destination to sha
        } finally {
            temporary.delete()
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun resultToJson(result: AnalysisResult) = JSONObject()
        .put("schema", SCHEMA)
        .put("currentS", result.currentS)
        .put("currentC", result.currentC)
        .put("currentPropensity", result.currentPropensity)
        .put("sleepRecords", JSONArray().apply { result.sleepRecords.forEach { put(sleepRecordToJson(it)) } })
        .put("scHistory", JSONArray().apply { result.scHistory.forEach { put(pointToJson(it)) } })
        .put("futureS", result.futureS?.let { points -> JSONArray().apply { points.forEach { put(predictedToJson(it)) } } } ?: JSONObject.NULL)
        .put("recommendation", result.recommendation?.let(::recommendationToJson) ?: JSONObject.NULL)
        .put("modelVersion", result.modelVersion)
        .put("calculationTimeMs", result.calculationTimeMs)
        .put("epochIntervalMs", result.epochIntervalMs)
        .put("epochIntervalLabel", result.epochIntervalLabel)
        .put("inputPeriodStartMs", result.inputPeriodStartMs)
        .put("inputPeriodEndMs", result.inputPeriodEndMs)
        .put("recordDays", result.recordDays)
        .put("personalizationStage", result.personalizationStage)
        .put("predictionLimitations", JSONArray(result.predictionLimitations))

    private fun resultFromJson(j: JSONObject) = AnalysisResult(
        currentS = j.getDouble("currentS"), currentC = j.getDouble("currentC"),
        currentPropensity = j.getDouble("currentPropensity"),
        sleepRecords = j.getJSONArray("sleepRecords").objects(::sleepRecordFromJson),
        scHistory = j.getJSONArray("scHistory").objects(::pointFromJson),
        futureS = j.optJSONArray("futureS")?.objects(::predictedFromJson),
        recommendation = j.optJSONObject("recommendation")?.let(::recommendationFromJson),
        modelVersion = j.getString("modelVersion"), calculationTimeMs = j.getLong("calculationTimeMs"),
        epochIntervalMs = j.optLong("epochIntervalMs", 60_000L),
        epochIntervalLabel = j.optString("epochIntervalLabel", "1분"),
        inputPeriodStartMs = j.optLong("inputPeriodStartMs", 0L),
        inputPeriodEndMs = j.optLong("inputPeriodEndMs", 0L),
        recordDays = j.optInt("recordDays", 0),
        personalizationStage = j.optString("personalizationStage", "initial"),
        predictionLimitations = j.optJSONArray("predictionLimitations")?.strings().orEmpty()
    )

    private fun sleepRecordToJson(r: SleepRecord) = JSONObject().put("sessionId", r.sessionId).put("startMs", r.startMs)
        .put("endMs", r.endMs).put("totalSleepMin", r.totalSleepMin)
        .put("stageSummary", JSONObject(r.stageSummary))
    private fun sleepRecordFromJson(j: JSONObject) = SleepRecord(j.getString("sessionId"), j.getLong("startMs"), j.getLong("endMs"),
        j.getDouble("totalSleepMin"), j.getJSONObject("stageSummary").keys().asSequence().associateWith { j.getJSONObject("stageSummary").getDouble(it) })
    private fun pointToJson(p: PropensityPoint) = JSONObject().put("timestampMs", p.timestampMs).put("S", p.S).put("C", p.C).put("propensity", p.propensity)
    private fun pointFromJson(j: JSONObject) = PropensityPoint(j.getLong("timestampMs"), j.getDouble("S"), j.getDouble("C"), j.getDouble("propensity"))
    private fun predictedToJson(p: PredictedPoint) = JSONObject().put("timestampMs", p.timestampMs).put("predictedS", p.predictedS)
        .put("predictedC", p.predictedC).put("predictedPropensity", p.predictedPropensity)
    private fun predictedFromJson(j: JSONObject) = PredictedPoint(j.getLong("timestampMs"), j.getDouble("predictedS"), j.getDouble("predictedC"), j.getDouble("predictedPropensity"))

    private fun recommendationToJson(r: Recommendation) = JSONObject().apply {
        putNullable("recommendedBedTimeMs", r.recommendedBedTimeMs); putNullable("recommendedWakeTimeMs", r.recommendedWakeTimeMs)
        putNullable("recommendedSleepMin", r.recommendedSleepMin); putNullable("physicalBedTimeMs", r.physicalBedTimeMs)
        putNullable("physicalWakeTimeMs", r.physicalWakeTimeMs); putNullable("predictedBedTimeMs", r.predictedBedTimeMs)
        putNullable("predictedWakeTimeMs", r.predictedWakeTimeMs); putNullable("bedtimeResidualMin", r.bedtimeResidualMin)
        putNullable("wakeResidualMin", r.wakeResidualMin); putNullable("residualModelVersion", r.residualModelVersion)
        put("modelStatus", r.modelStatus); put("usedFallback", r.usedFallback); putNullable("explanation", r.explanation)
        putNullable("planFeasible", r.planFeasible); putNullable("generatedAtMs", r.generatedAtMs)
        putNullable("personalizationStatus", r.personalizationStatus); putNullable("personalizationWeight", r.personalizationWeight)
        putNullable("evidenceStatus", r.evidenceStatus); putNullable("runtimePolicy", r.runtimePolicy)
        put("predictionOrigin", r.predictionOrigin.name)
        putNullable("historyCutoffMs", r.historyCutoffMs)
        putNullable("historyRecordCount", r.historyRecordCount)
        putNullable("csvHistoryRecordCount", r.csvHistoryRecordCount)
    }

    private fun recommendationFromJson(j: JSONObject) = Recommendation(
        recommendedBedTimeMs = j.nullableLong("recommendedBedTimeMs"), recommendedWakeTimeMs = j.nullableLong("recommendedWakeTimeMs"),
        recommendedSleepMin = j.nullableDouble("recommendedSleepMin"), physicalBedTimeMs = j.nullableLong("physicalBedTimeMs"),
        physicalWakeTimeMs = j.nullableLong("physicalWakeTimeMs"), predictedBedTimeMs = j.nullableLong("predictedBedTimeMs"),
        predictedWakeTimeMs = j.nullableLong("predictedWakeTimeMs"), bedtimeResidualMin = j.nullableDouble("bedtimeResidualMin"),
        wakeResidualMin = j.nullableDouble("wakeResidualMin"), residualModelVersion = j.nullableString("residualModelVersion"),
        modelStatus = j.optString("modelStatus", "physics_only_untrained"), usedFallback = j.optBoolean("usedFallback", true),
        explanation = j.nullableString("explanation"), planFeasible = j.nullableBoolean("planFeasible"),
        generatedAtMs = j.nullableLong("generatedAtMs"), personalizationStatus = j.nullableString("personalizationStatus"),
        personalizationWeight = j.nullableDouble("personalizationWeight"), evidenceStatus = j.nullableString("evidenceStatus"),
        runtimePolicy = j.nullableString("runtimePolicy"),
        predictionOrigin = runCatching {
            PredictionOrigin.valueOf(j.optString("predictionOrigin"))
        }.getOrDefault(PredictionOrigin.LEGACY_UNSPECIFIED),
        historyCutoffMs = j.nullableLong("historyCutoffMs"),
        historyRecordCount = j.nullableInt("historyRecordCount"),
        csvHistoryRecordCount = j.nullableInt("csvHistoryRecordCount")
    )

    private fun recordsToJson(records: List<PersonalizationRecord>) = JSONArray().apply { records.forEach { r ->
        put(JSONObject().put("id", r.id).put("day", r.day).put("bedtime", r.bedtime).put("wakeTime", r.wakeTime)
            .put("durationMinutes", r.durationMinutes).putNullable("rested", r.rested).putNullable("fatigue", r.fatigue)
            .put("source", r.source).put("modifiedAt", r.modifiedAt).putNullable("sleepLatencyMinutes", r.sleepLatencyMinutes)
            .putNullable("awakeMinutes", r.awakeMinutes).putNullable("steps", r.steps).putNullable("exerciseMinutes", r.exerciseMinutes)
            .putNullable("subjectiveSource", r.subjectiveSource).putNullable("subjectiveObservedAt", r.subjectiveObservedAt))
    } }

    private fun JSONObject.putNullable(key: String, value: Any?): JSONObject = put(key, value ?: JSONObject.NULL)
    private fun JSONObject.nullableInt(key: String): Int? = if (present(key)) getInt(key) else null
    private fun JSONObject.present(key: String): Boolean = has(key) && !isNull(key)
    private fun JSONObject.nullableLong(key: String): Long? = if (present(key)) getLong(key) else null
    private fun JSONObject.nullableDouble(key: String): Double? = if (present(key)) getDouble(key) else null
    private fun JSONObject.nullableBoolean(key: String): Boolean? = if (present(key)) getBoolean(key) else null
    private fun JSONObject.nullableString(key: String): String? = if (present(key)) getString(key) else null
    private fun <T> JSONArray.objects(mapper: (JSONObject) -> T): List<T> = (0 until length()).map { mapper(getJSONObject(it)) }
    private fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
}
