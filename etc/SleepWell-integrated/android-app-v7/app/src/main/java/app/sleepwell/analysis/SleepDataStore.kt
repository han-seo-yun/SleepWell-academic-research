package app.sleepwell.analysis

import app.sleepwell.health.CsvFileSource
import app.sleepwell.health.SleepSession
import app.sleepwell.health.SleepStage
import java.io.File

/*
 * 수면 기록 보관 + 합치기 (소빈 · 2026-10-31, 통합 v6)
 *
 * 10/27 서윤 합의: **과거 기록 = 삼성 헬스 내보내기 CSV, 앞으로 = 헬스 커넥트 연동**.
 * 전에는 불러올 때마다 파일 하나로 통째로 바뀌어서, 앱을 다시 켜면 헬스 커넥트(최근 30일)로 덮여 과거 기록이 사라졌다.
 *
 *  - 내보내기 CSV(폴더 선택·파일 고르기) → archive 로 보관 (다시 고르면 바꿔 끼움)
 *  - 헬스 커넥트에서 읽은 기록          → healthconnect 로 보관 (읽을 때마다 바꿔 끼움)
 *  - 분석에는 둘을 합친 merged 를 쓴다. 겹치는 밤(같은 잠이 양쪽에 있음)은 내보내기 CSV 쪽만 남긴다
 *    (둘 다 삼성 워치에서 온 같은 기록이라, 원본 쪽을 우선)
 *  - 파일은 앱 전용 폴더(filesDir/sleep_data) 안에만. 서버로 보내지 않는다.
 *  - 합성 데이터 버튼은 여기를 거치지 않는다 (실제 기록과 섞이지 않게)
 */
object SleepDataStore {

    /** Serializes foreground and JobService storage transactions in this app process. */
    internal val STORAGE_LOCK = Any()

    const val ARCHIVE = "sleep_archive.csv"
    const val HEALTH_CONNECT = "sleep_healthconnect.csv"
    const val MERGED = "sleep_merged.csv"

    data class MergeInfo(
        val archiveSessions: Int,
        val healthConnectSessions: Int,
        /** 헬스 커넥트 기록 중 내보내기와 겹치지 않아 새로 더해진 수 */
        val healthConnectAdded: Int,
        val total: Int,
        val firstMs: Long?,
        val lastMs: Long?
    )

    fun hasArchive(dir: File) = synchronized(STORAGE_LOCK) { File(dir, ARCHIVE).isFile }
    fun hasAny(dir: File) = synchronized(STORAGE_LOCK) {
        File(dir, ARCHIVE).isFile || File(dir, HEALTH_CONNECT).isFile
    }

    /**
     * 내보내기 CSV 를 보관 (원본 그대로 복사).
     * (v6 검토 #3) 먼저 읽어 보고 수면 기록이 하나도 없으면(수면 단계 파일이 아님) **저장하지 않고** 오류를 낸다
     *  → 잘못 고른 파일이 보관돼서 이후 계속 오류가 나거나 헬스 커넥트 기록까지 못 보는 일을 막는다. 기존 보관 기록은 그대로.
     */
    fun saveArchive(dir: File, src: File, observedAtMs: Long = System.currentTimeMillis()): Unit = synchronized(STORAGE_LOCK) {
        val sessions = try { read(src) } catch (e: Exception) { emptyList() }
        require(sessions.isNotEmpty()) {
            "이 파일에서 수면 기록을 찾지 못했어요. 삼성 헬스 내보내기 중 com.samsung.health.sleep_stage…csv 파일(또는 그 파일이 있는 폴더)을 골라 주세요. 이전에 불러온 기록은 그대로 있어요."
        }
        dir.mkdirs()
        atomicWrite(File(dir, ARCHIVE)) { tmp -> src.copyTo(tmp, overwrite = true) }
        ImportedSleepStore.ingest(dir, sessions, "samsung_health_csv", observedAtMs)
    }

    /**
     * 헬스 커넥트에서 읽은 CSV 글자(HealthConnectCsv 형식)를 보관.
     * (v6 검토 #4) 바꿔 끼우지 않고 **더한다**: 헬스 커넥트는 매번 최근 30일만 읽으므로, 바꿔 끼우면 연결 30일 뒤부터
     *  그 전 연동 기록이 사라진다. 이전에 보관한 연동 기록에 새로 읽은 것을 더하고, 겹치는 밤은 새로 읽은 쪽으로 바꾼다.
     */
    fun saveHealthConnect(
        dir: File,
        csv: String,
        source: String = "health_connect",
        observedAtMs: Long = System.currentTimeMillis()
    ): Unit = synchronized(STORAGE_LOCK) {
        dir.mkdirs()
        val target = File(dir, HEALTH_CONNECT)
        val incomingFile = File(dir, "$HEALTH_CONNECT.incoming")
        incomingFile.writeText(csv, Charsets.UTF_8)
        val incoming = try { read(incomingFile) } finally { incomingFile.delete() }
        // A malformed existing archive must fail visibly; treating it as empty would overwrite history.
        val previous = readStoredHealthConnect(target)
        val combined = mergeHealthConnect(previous, incoming)
        atomicWrite(target) { tmp -> tmp.writeText(HealthConnectCsv.toCsv(combined.map(::toHc), idPrefix = ""), Charsets.UTF_8) }
        ImportedSleepStore.ingest(
            dir, incoming, source, observedAtMs,
            metadataBySession = HealthConnectCsv.modelMetadataBySession(csv)
        )
    }

    /** 새로 읽은 연동 기록은 모두 남기고, 이전 연동 기록은 새것과 겹치지 않는 것만 남긴다 (중복 = 새것으로) */
    fun mergeHealthConnect(previous: List<SleepSession>, incoming: List<SleepSession>): List<SleepSession> =
        (incoming + previous.filter { p -> incoming.none { n -> p.startMs < n.endMs && n.startMs < p.endMs } }).sortedBy { it.startMs }

    /** (v6 검토 #8) 보관한 수면 기록(내보내기 · 헬스 커넥트 · 합친 파일)을 모두 지운다. 설문·저장 결과·내 정보는 그대로 */
    fun clear(dir: File) = synchronized(STORAGE_LOCK) {
        listOf(ARCHIVE, HEALTH_CONNECT, MERGED, ImportedSleepStore.FILE_NAME).forEach { File(dir, it).delete() }
    }

    /** 보관한 둘을 합쳐 merged 파일을 다시 만들고 돌려준다. 둘 다 없으면 null */
    fun buildMerged(dir: File): Pair<File, MergeInfo>? = synchronized(STORAGE_LOCK) {
        if (!hasAny(dir)) return@synchronized null
        val archive = read(File(dir, ARCHIVE))
        val hc = read(File(dir, HEALTH_CONNECT))
        val merged = merge(archive, hc)
        val out = File(dir, MERGED)
        atomicWrite(out) { tmp -> tmp.writeText(HealthConnectCsv.toCsv(merged.map(::toHc), idPrefix = ""), Charsets.UTF_8) }
        out to MergeInfo(
            archiveSessions = archive.size,
            healthConnectSessions = hc.size,
            healthConnectAdded = merged.size - archive.size,
            total = merged.size,
            firstMs = merged.minOfOrNull { it.startMs },
            lastMs = merged.maxOfOrNull { it.endMs }
        )
    }

    /** 내보내기 세션은 모두 남기고, 헬스 커넥트 세션은 내보내기 세션과 1분이라도 겹치지 않을 때만 더한다 */
    fun merge(archive: List<SleepSession>, healthConnect: List<SleepSession>): List<SleepSession> {
        val added = healthConnect.filter { h -> archive.none { a -> h.startMs < a.endMs && a.startMs < h.endMs } }
        return (archive + added).sortedBy { it.startMs }
    }

    private fun read(file: File): List<SleepSession> {
        if (!file.isFile) return emptyList()
        val src = CsvFileSource()
        val rows = file.inputStream().use { src.parseSleepStageCsvForPipeline(it) }
        return src.groupIntoSessionsForPipeline(rows)
    }

    private fun readStoredHealthConnect(file: File): List<SleepSession> {
        if (!file.isFile) return emptyList()
        val header = file.useLines { lines -> lines.firstOrNull().orEmpty() }.split(',')
        require(header.containsAll(listOf("start_time", "end_time", "sleep_id"))) {
            "기존 헬스 커넥트 수면 보관 파일이 손상되어 새 기록으로 덮어쓰지 않았어요."
        }
        return read(file)
    }

    private fun toHc(s: SleepSession) = HcSleepSession(
        id = (if (s.sessionId.startsWith("HC")) "" else "SH") + s.sessionId,
        startMs = s.startMs,
        endMs = s.endMs,
        offsetSeconds = null,
        stages = s.stages.map { st ->
            HcSleepStage(st.startMs, st.endMs, when (st.stage) {
                SleepStage.WAKE -> HealthConnectCsv.AWAKE
                SleepStage.DEEP -> HealthConnectCsv.DEEP
                SleepStage.REM -> HealthConnectCsv.REM
                SleepStage.LIGHT -> HealthConnectCsv.LIGHT
                else -> HealthConnectCsv.UNKNOWN
            })
        }
    )

    /** 쓰다가 앱이 꺼져도 기존 파일이 깨지지 않게: 임시 파일에 쓴 뒤 바꿔 끼움 */
    private fun atomicWrite(target: File, write: (File) -> Unit) {
        val tmp = File(target.parentFile, target.name + ".tmp")
        write(tmp)
        if (!tmp.renameTo(target)) { tmp.copyTo(target, overwrite = true); tmp.delete() }
    }
}
