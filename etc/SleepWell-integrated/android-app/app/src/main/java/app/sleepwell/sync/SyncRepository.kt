package app.sleepwell.sync

import android.annotation.SuppressLint
import android.content.Context
import android.os.Build
import app.sleepwell.user.DailyContext
import app.sleepwell.user.UserRepository
import kotlinx.coroutines.CancellationException
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.temporal.ChronoUnit

data class SyncStatus(val message: String, val uploaded: Int, val deleted: Int, val skipped: Int)
data class BackupAck(val day: String, val revision: Int, val storedRevision: Int, val duplicate: Boolean)

class SyncRepository(private val context: Context) {
    private val state = SecureStateStore(context)

    fun isConnected(): Boolean = connectionOrNull() != null

    fun connectionLabel(): String? = connectionOrNull()?.let { "${it.config.host}:${it.config.port}" }

    fun hasPending(): Boolean = state.get(SecureStateStore.PENDING) != null

    fun sendBackup(payload: JSONObject): BackupAck {
        val connection = connectionOrNull() ?: error("먼저 PC와 연결하세요.")
        val response = PinnedHttpsClient(connection.config).post("/v1/backup", payload, connection.token)
        check(response.optBoolean("accepted", false)) { "PC가 백업을 확인하지 않았습니다." }
        val day = response.getString("day")
        val revision = response.getInt("revision")
        require(day == payload.getString("day") && revision == payload.getInt("revision")) {
            "PC의 백업 확인 값이 전송 자료와 다릅니다."
        }
        return BackupAck(day, revision, response.getInt("stored_revision"), response.optBoolean("duplicate", false))
    }

    fun pair(uriText: String): String {
        val supplied = PairingUri.parse(uriText)
        val response = PinnedHttpsClient(supplied).post(
            "/v1/pair",
            JSONObject().put("secret", supplied.secret).put(
                "device_name",
                listOf(Build.MANUFACTURER, Build.MODEL).filter(String::isNotBlank).joinToString(" ").take(80)
            )
        )
        val token = response.optString("device_token")
        require(token.isNotBlank() && token.length <= 4096) { "PC가 올바른 기기 토큰을 보내지 않았습니다." }
        val saved = JSONObject()
            .put("host", supplied.host)
            .put("port", supplied.port)
            .put("pin", supplied.certificateSha256)
            .put("token", token)
        val samePcCertificate = connectionOrNull()?.config?.certificateSha256 == supplied.certificateSha256
        if (!samePcCertificate) {
            state.remove(SecureStateStore.PENDING, SecureStateStore.JOURNAL, SecureStateStore.LAST_RESULT)
        }
        state.put(SecureStateStore.CONNECTION, saved.toString())
        return "${supplied.host}:${supplied.port}"
    }

    @SuppressLint("NewApi") // reader.isSupported()로 API 34 미만을 차단한 뒤 플랫폼 API를 호출한다.
    suspend fun sync(): SyncStatus {
        val connection = connectionOrNull() ?: error("먼저 PC와 연결하세요.")
        var sleep: SyncStatus? = null
        var sleepFailure: Exception? = null
        try {
            sleep = syncSleep(connection)
        } catch (error: Exception) {
            if (error is CancellationException) throw error
            sleepFailure = error
        }
        var contextMessage: String? = null
        var contextFailure: Exception? = null
        try {
            contextMessage = syncContext(connection)
        } catch (error: Exception) {
            if (error is CancellationException) throw error
            contextFailure = error
        }
        if (sleep == null && contextMessage == null) {
            throw IllegalStateException("수면과 생활 컨텍스트 동기화를 모두 완료하지 못했습니다.", contextFailure ?: sleepFailure)
        }
        val base = sleep ?: SyncStatus("수면 동기화 건너뜀", 0, 0, 0)
        val details = buildList {
            add(base.message.removeSuffix(" Windows 화면은 새로고침해 주세요."))
            if (sleepFailure != null) add("수면은 권한 또는 조회 오류로 보류")
            if (contextMessage != null) add(contextMessage)
            else add("생활 컨텍스트는 오류로 보류")
        }
        return base.copy(message = details.joinToString(", ") + ". Windows 화면은 새로고침해 주세요.")
    }

    @SuppressLint("NewApi")
    private suspend fun syncSleep(connection: Connection): SyncStatus {
        state.get(SecureStateStore.PENDING)?.let { return sendPending(connection, JSONObject(it)) }

        val reader = HealthConnectSleepReader(context)
        check(reader.isSupported()) { "Android 14 이상에서만 Health Connect 직접 연동을 지원합니다." }
        check(reader.hasPermission()) { "Health Connect 수면 읽기 권한이 없습니다." }

        val to = Instant.now()
        val from = to.minus(30, ChronoUnit.DAYS)
        // readCompleteSamsungSessions가 모든 페이지를 정상적으로 읽은 경우에만 아래 삭제 비교를 수행한다.
        val read = reader.readCompleteSamsungSessions(from, to)
        val sessions = read.sessions
        val previous = readJournal()
        val eligibleIds = sessions.mapTo(hashSetOf()) { it.id }
        // 원본이 사라졌거나 수정을 거쳐 서버의 유효 범위를 벗어난 경우 모두 기존 서버 사본을 지운다.
        // 전체 페이지 조회가 성공한 뒤에만 실행되므로 부분 조회를 삭제로 오인하지 않는다.
        val deleted = previous.filter { it.end >= from && it.id !in eligibleIds }.map { it.id }.sorted()
        val nextJournal = JSONArray().also { array ->
            sessions.forEach { array.put(JSONObject().put("id", it.id).put("end", it.end.toString())) }
        }
        val batches = buildBatches(sessions, deleted)
        val pending = JSONObject()
            .put("next_index", 0)
            .put("uploaded", 0)
            .put("deleted", deleted.size)
            .put("skipped", read.skippedCount)
            .put("batches", batches)
            .put("next_journal", nextJournal)
        state.put(SecureStateStore.PENDING, pending.toString())
        return sendPending(connection, pending)
    }

    fun disconnect() {
        state.remove(
            SecureStateStore.CONNECTION,
            SecureStateStore.PENDING,
            SecureStateStore.JOURNAL,
            SecureStateStore.LAST_RESULT
        )
    }

    /** 잘못된 4xx 자료가 재시도를 막을 때만 사용한다. 마지막 성공 저널은 보존해 다음 조회가 안전하게 재구성된다. */
    fun discardPending() {
        state.remove(SecureStateStore.PENDING)
    }

    private fun buildBatches(sessions: List<SyncSleepSession>, deleted: List<String>): JSONArray {
        val batches = JSONArray()
        deleted.chunked(100).forEach { ids ->
            val payload = payloadOf(emptyList(), ids)
            require(payload.toString().toByteArray(Charsets.UTF_8).size <= PinnedHttpsClient.MAX_BODY_BYTES) {
                "삭제 목록 한 묶음이 전송 한도(1,000,000바이트)를 넘습니다."
            }
            batches.put(payload)
        }
        var chunk = mutableListOf<SyncSleepSession>()
        fun flush() {
            if (chunk.isEmpty()) return
            val payload = payloadOf(chunk, emptyList())
            require(payload.toString().toByteArray(Charsets.UTF_8).size <= PinnedHttpsClient.MAX_BODY_BYTES) {
                "같은 기상일의 수면 세션 묶음이 전송 한도(1,000,000바이트)를 넘습니다."
            }
            batches.put(payload)
            chunk = mutableListOf()
        }
        // PC의 대표 밤 선택과 같은 기준: 마지막 수면 단계의 종료일(서울 시간)을 기상일로 쓴다.
        // 휴대폰이 해외 시간대로 바뀌어도 한 밤의 세션 묶음이 갈라지지 않는다.
        val dayGroups = sessions.groupBy { session ->
            val wake = session.stages.lastOrNull {
                it.stage == "sleeping" || it.stage == "light" || it.stage == "deep" || it.stage == "rem"
            }?.end ?: session.end
            wake.atZone(SERVER_ZONE).toLocalDate()
        }
            .toSortedMap()
            .values
        dayGroups.forEach { sameWakeDay ->
            require(sameWakeDay.size <= 100) { "하루의 수면 세션이 100건을 넘습니다." }
            val candidate = chunk + sameWakeDay
            val payload = payloadOf(candidate, emptyList())
            if (candidate.size > 100 || payload.toString().toByteArray(Charsets.UTF_8).size > PinnedHttpsClient.MAX_BODY_BYTES) {
                require(chunk.isNotEmpty()) { "같은 기상일의 수면 세션 묶음이 전송 한도(1,000,000바이트)를 넘습니다." }
                flush()
            }
            chunk += sameWakeDay
        }
        flush()
        if (batches.length() == 0) batches.put(payloadOf(emptyList(), emptyList()))
        return batches
    }

    private fun payloadOf(sessions: List<SyncSleepSession>, deleted: List<String>): JSONObject = JSONObject()
        .put("schema_version", 1)
        .put("sessions", JSONArray().also { rows -> sessions.forEach { rows.put(sessionJson(it)) } })
        .put("deleted_ids", JSONArray(deleted))

    private fun sessionJson(session: SyncSleepSession): JSONObject = JSONObject()
        .put("id", session.id)
        .put("modified_at", session.modifiedAt.toString())
        .put("start", session.start.toString())
        .put("end", session.end.toString())
        .put("stages", JSONArray().also { stages ->
            session.stages.forEach {
                stages.put(JSONObject().put("start", it.start.toString()).put("end", it.end.toString()).put("stage", it.stage))
            }
        })

    private fun sendPending(connection: Connection, pending: JSONObject): SyncStatus {
        val client = PinnedHttpsClient(connection.config)
        val batches = pending.getJSONArray("batches")
        var index = pending.getInt("next_index")
        var uploaded = pending.optInt("uploaded", 0)
        val serverCounts = linkedMapOf<String, Int>()
        while (index < batches.length()) {
            val payload = batches.getJSONObject(index)
            val response = client.post("/v1/sync", payload, connection.token)
            SERVER_COUNT_KEYS.forEach { key ->
                if (response.has(key) && !response.isNull(key)) {
                    serverCounts[key] = (serverCounts[key] ?: 0) + response.optInt(key, 0).coerceAtLeast(0)
                }
            }
            uploaded += payload.getJSONArray("sessions").length()
            index += 1
            pending.put("next_index", index).put("uploaded", uploaded)
            state.put(SecureStateStore.PENDING, pending.toString())
        }
        state.put(SecureStateStore.JOURNAL, pending.getJSONArray("next_journal").toString())
        state.remove(SecureStateStore.PENDING)
        val deleted = pending.optInt("deleted", 0)
        val skipped = pending.optInt("skipped", 0)
        val detail = serverCounts.entries.joinToString { "${it.key}=${it.value}" }
        val message = buildString {
            append("수면 ${uploaded}건 전송, 삭제 ${deleted}건 요청")
            if (skipped > 0) append(", 범위 밖 ${skipped}건 제외")
            if (detail.isNotEmpty()) append(" ($detail)")
            append(". Windows 화면은 새로고침해 주세요.")
        }
        state.put(SecureStateStore.LAST_RESULT, JSONObject().put("at", Instant.now().toString()).put("message", message).toString())
        return SyncStatus(message, uploaded, deleted, skipped)
    }

    @SuppressLint("NewApi")
    private suspend fun syncContext(connection: Connection): String {
        val now = Instant.now()
        val from = now.minus(30, ChronoUnit.DAYS)
        val reader = HealthConnectContextReader(context)
        val health = if (reader.isSupported()) reader.readAvailableSamsungContext(from, now)
            else HealthContextReadResult(emptyList(), emptySet())
        val user = UserRepository(context).load()
        val manualEvents = JSONArray()
        val contexts = user.dailyContexts.filter { runCatching { Instant.parse(it.observedAt) >= from }.getOrDefault(false) }
        contexts.forEach { context -> addManualContext(manualEvents, context) }
        val clearedGroups = JSONArray()
        contexts.groupBy { it.nightId }.values.mapNotNull { versions -> versions.maxByOrNull { it.observedAt } }
            .forEach { value ->
                fun clear(kind: String) { clearedGroups.put(JSONObject()
                    .put("group_id", "manual:${value.nightId}:$kind").put("modified_at", value.observedAt)) }
                if (value.firstStart == null) clear("schedule")
                if (value.napStart == null) clear("nap")
                if (value.caffeineAt == null) clear("caffeine")
            }
        require(manualEvents.length() <= 1000) { "직접 입력한 생활 컨텍스트가 1,000건을 넘어 전송하지 않았습니다." }
        val client = PinnedHttpsClient(connection.config)
        var changed = 0
        var unchanged = 0
        fun send(rangeStart: Instant, rangeEnd: Instant, scopes: List<String>, events: JSONArray, clears: JSONArray = JSONArray()) {
            val payload = JSONObject().put("schema_version", 1).put("participant_key", user.participantKey)
                .put("generated_at", now.toString()).put("range_start", rangeStart.toString()).put("range_end", rangeEnd.toString())
                .put("complete_scopes", JSONArray(scopes)).put("events", events).put("cleared_groups", clears)
            require(payload.toString().toByteArray(Charsets.UTF_8).size <= PinnedHttpsClient.MAX_BODY_BYTES) {
                "생활 컨텍스트 전송 묶음이 1MB를 넘습니다."
            }
            val response = client.post("/v1/context", payload, connection.token)
            changed += response.optInt("accepted") + response.optInt("superseded") + response.optInt("deleted")
            unchanged += response.optInt("unchanged")
        }
        // Manual revisions are independent of Health Connect grants and sensor read failures.
        send(from, now, emptyList(), manualEvents, clearedGroups)
        fun sendWindow(scope: String, start: Instant, end: Instant, rows: List<SyncContextEvent>) {
            if (rows.size <= 900) {
                send(start, end, listOf(scope), JSONArray().also { out -> rows.forEach { out.put(contextEventJson(it)) } })
                return
            }
            val nanos = java.time.Duration.between(start, end).toNanos()
            require(nanos > 1) { "$scope 동일 시각의 기록이 전송 한도를 넘습니다." }
            val middle = start.plusNanos(nanos / 2)
            val (left, right) = rows.partition { it.start < middle }
            sendWindow(scope, start, middle, left)
            sendWindow(scope, middle, end, right)
        }
        health.completeScopes.sorted().forEach { scope ->
            val rows = health.events.filter { it.kind == scope && it.start >= from && it.start < now }
            if (rows.size <= 900) {
                send(from, now, listOf(scope), JSONArray().also { out -> rows.forEach { out.put(contextEventJson(it)) } })
            } else {
                var cursor = from
                while (cursor < now) {
                    val boundary = minOf(cursor.plus(1, ChronoUnit.DAYS), now)
                    val window = rows.filter { it.start >= cursor && it.start < boundary }
                    sendWindow(scope, cursor, boundary, window)
                    cursor = boundary
                }
            }
        }
        val failed = health.failedScopes.sorted().joinToString("·")
        return buildString {
            append("생활 컨텍스트 ${changed}건 반영·${unchanged}건 동일")
            if (failed.isNotEmpty()) append(" ($failed 조회 실패, 직접 입력은 전송됨)")
        }
    }

    private fun contextEventJson(event: SyncContextEvent): JSONObject = JSONObject()
        .put("id", event.id).put("group_id", JSONObject.NULL).put("kind", event.kind)
        .put("start", event.start.toString()).put("end", event.end?.toString() ?: JSONObject.NULL)
        .put("value", event.value ?: JSONObject.NULL).put("modified_at", event.modifiedAt.toString())
        .put("observed_at", JSONObject.NULL).put("source", event.source)

    private fun addManualContext(events: JSONArray, value: DailyContext) {
        fun event(kind: String, start: String?, end: String?, amount: Int?) {
            if (start == null) return
            val group = "manual:${value.nightId}:$kind"
            events.put(JSONObject().put("id", "$group:${value.observedAt}").put("group_id", group)
                .put("kind", kind).put("start", start).put("end", end ?: JSONObject.NULL)
                .put("value", amount ?: JSONObject.NULL).put("modified_at", value.observedAt)
                .put("observed_at", value.observedAt).put("source", "manual_android"))
        }
        event("schedule", value.firstStart, null, null)
        event("nap", value.napStart, value.napEnd, null)
        event("caffeine", value.caffeineAt, null, value.caffeineMg)
    }

    private fun connectionOrNull(): Connection? = runCatching {
        val value = JSONObject(state.get(SecureStateStore.CONNECTION) ?: return null)
        Connection(
            PairingConfig(value.getString("host"), value.getInt("port"), value.getString("pin"), ""),
            value.getString("token")
        )
    }.getOrNull()

    private fun readJournal(): List<JournalEntry> = runCatching {
        val array = JSONArray(state.get(SecureStateStore.JOURNAL) ?: "[]")
        (0 until array.length()).map {
            val row = array.getJSONObject(it)
            JournalEntry(row.getString("id"), Instant.parse(row.getString("end")))
        }
    }.getOrDefault(emptyList())

    private data class Connection(val config: PairingConfig, val token: String)
    private data class JournalEntry(val id: String, val end: Instant)

    companion object {
        private val SERVER_COUNT_KEYS = listOf(
            "accepted", "unchanged", "stale", "deleted", "records_upserted",
            "records_deleted", "conflicts_pending", "records_held"
        )
        private val SERVER_ZONE: ZoneId = ZoneId.of("Asia/Seoul")
    }
}
