package app.sleepwell.analysis

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

/** Local explicit sleep-diary storage. Wearable sessions never enter this file. */
object DiaryStore {
    const val FILE_NAME = "sleep_diary.json"
    private const val SCHEMA_VERSION = 1
    private val seoul = ZoneId.of("Asia/Seoul")

    fun load(file: File): List<SleepRecord> {
        if (!file.isFile) return emptyList()
        val root = try { JSONObject(file.readText()) } catch (e: Exception) {
            throw IllegalArgumentException("수면일지 파일을 읽을 수 없어요: ${e.message}", e)
        }
        require(root.optInt("schema_version", -1) == SCHEMA_VERSION) { "지원하지 않는 수면일지 형식이에요." }
        val values = root.optJSONArray("records") ?: throw IllegalArgumentException("수면일지 records 배열이 없어요.")
        val records = (0 until values.length()).map { index ->
            try { fromJson(values.getJSONObject(index)).also(::validate) } catch (e: Exception) {
                throw IllegalArgumentException("수면일지 ${index + 1}번째 기록이 잘못됐어요: ${e.message}", e)
            }
        }
        require(records.map { it.id }.distinct().size == records.size) { "수면일지 ID가 중복됐어요." }
        return records.sortedBy { it.wakeTime }
    }

    /** Merge one correction without replacing unrelated diary rows. Older corrections fail closed. */
    fun save(file: File, record: SleepRecord): List<SleepRecord> {
        validate(record)
        val current = load(file)
        current.firstOrNull { it.id == record.id }?.let { previous ->
            require(!Instant.parse(record.modifiedAt).isBefore(Instant.parse(previous.modifiedAt))) {
                "더 오래된 입력으로 기존 수면일지를 덮어쓸 수 없어요."
            }
        }
        val merged = (current.filterNot { it.id == record.id } + record).sortedBy { it.wakeTime }
        writeAtomic(file, merged)
        return merged
    }

    private fun writeAtomic(file: File, records: List<SleepRecord>) {
        file.parentFile?.mkdirs()
        val payload = JSONObject().put("schema_version", SCHEMA_VERSION)
            .put("records", JSONArray().also { array -> records.forEach { array.put(toJson(it)) } }).toString(2)
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(payload)
        try {
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (_: AtomicMoveNotSupportedException) {
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }

    private fun validate(record: SleepRecord) {
        require(record.id.startsWith("diary:")) { "명시적 일지 ID가 아니에요." }
        require(record.source == "manual" || record.source == "diary") { "수면일지는 manual/diary 출처여야 해요." }
        val bed = runCatching { Instant.parse(record.bedtime) }.getOrElse { throw IllegalArgumentException("취침 시각이 잘못됐어요.") }
        val wake = runCatching { Instant.parse(record.wakeTime) }.getOrElse { throw IllegalArgumentException("기상 시각이 잘못됐어요.") }
        val observed = runCatching { Instant.parse(record.modifiedAt) }.getOrElse { throw IllegalArgumentException("입력 시각이 잘못됐어요.") }
        require(wake.isAfter(bed)) { "기상 시각은 취침 시각 이후여야 해요." }
        require(!observed.isBefore(wake)) { "입력 시각은 기상 시각 이전일 수 없어요." }
        require(record.day == wake.atZone(seoul).toLocalDate().toString()) { "일지 날짜와 기상 날짜가 다릅니다." }
        val timeInBed = Duration.between(bed, wake).toMinutes().toInt()
        require(record.durationMinutes == timeInBed) { "침대 체류시간이 취침·기상 구간과 다릅니다." }
        val latency = record.sleepLatencyMinutes
        val awake = record.awakeMinutes
        require(latency == null || (latency in 0..180 && latency <= timeInBed)) { "입면잠복기는 0~180분이며 침대 체류시간 이하여야 해요." }
        require(awake == null || (awake in 0..600 && awake <= timeInBed)) { "중간각성은 0~600분이며 침대 체류시간 이하여야 해요." }
        require((latency ?: 0) + (awake ?: 0) < timeInBed) { "입면잠복기와 중간각성 합이 침대 체류시간 이상이에요." }
        require(record.rested == null || record.rested in 1..5) { "개운함 범위가 잘못됐어요." }
        require(record.fatigue == null || record.fatigue in 1..5) { "피로도 범위가 잘못됐어요." }
    }

    private fun toJson(record: SleepRecord) = JSONObject()
        .put("id", record.id).put("day", record.day).put("bedtime", record.bedtime)
        .put("wake_time", record.wakeTime).put("duration_minutes", record.durationMinutes)
        .put("sleep_latency_minutes", record.sleepLatencyMinutes ?: JSONObject.NULL)
        .put("awake_minutes", record.awakeMinutes ?: JSONObject.NULL)
        .put("steps", record.steps ?: JSONObject.NULL)
        .put("exercise_minutes", record.exerciseMinutes ?: JSONObject.NULL)
        .put("rested", record.rested ?: JSONObject.NULL).put("fatigue", record.fatigue ?: JSONObject.NULL)
        .put("source", record.source).put("modified_at", record.modifiedAt)

    /** Missing measurement fields are a supported incomplete-diary migration. */
    private fun fromJson(value: JSONObject) = SleepRecord(
        id = value.getString("id"), day = value.getString("day"), bedtime = value.getString("bedtime"),
        wakeTime = value.getString("wake_time"), durationMinutes = value.getInt("duration_minutes"),
        rested = value.intOrNull("rested"), fatigue = value.intOrNull("fatigue"),
        source = value.getString("source"), modifiedAt = value.getString("modified_at"),
        sleepLatencyMinutes = value.intOrNull("sleep_latency_minutes"), awakeMinutes = value.intOrNull("awake_minutes"),
        steps = value.intOrNull("steps"), exerciseMinutes = value.doubleOrNull("exercise_minutes")
    )

    private fun JSONObject.intOrNull(key: String): Int? = if (!has(key) || isNull(key)) null else getInt(key)
    private fun JSONObject.doubleOrNull(key: String): Double? = if (!has(key) || isNull(key)) null else getDouble(key)
}
