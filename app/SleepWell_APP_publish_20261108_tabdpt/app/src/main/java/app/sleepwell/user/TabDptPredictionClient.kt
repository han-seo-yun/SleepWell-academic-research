package app.sleepwell.user

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.security.MessageDigest
import java.util.LinkedHashMap
import java.util.Properties

internal const val TABDPT_REQUEST_SCHEMA = "sleepwell.tabdpt.request.v1"
internal const val TABDPT_RESPONSE_SCHEMA = "sleepwell.tabdpt.response.v1"
internal const val TABDPT_MODEL_VERSION = "tabdpt-v1.3-hybrid39-equal-budget-20261107"
internal const val TABDPT_FEATURE_CONTRACT = PortableFusionModel.TABDPT_FEATURE_CONTRACT

data class TabDptServerSettings(
    val enabled: Boolean = false,
    val endpoint: String = "http://127.0.0.1:8766/v1/predict",
    val bearerToken: String = "",
    val configurationError: String? = null
)

object TabDptSettingsStore {
    const val FILE_NAME = "tabdpt_server.properties"

    fun load(context: Context): TabDptServerSettings = load(File(context.filesDir, FILE_NAME))

    internal fun load(file: File): TabDptServerSettings {
        if (!file.isFile) return TabDptServerSettings()
        return runCatching {
            val p = Properties().apply { file.inputStream().use { input -> load(input) } }
            TabDptServerSettings(
                enabled = p.getProperty("enabled", "false").toBooleanStrictOrNull() ?: false,
                endpoint = p.getProperty("endpoint")?.trim().orEmpty()
                    .ifBlank { TabDptServerSettings().endpoint },
                bearerToken = p.getProperty("bearerToken", "")
            ).also(::requireValidSettings)
        }.getOrElse { failure ->
            TabDptServerSettings(configurationError =
                "저장된 TabDPT 설정이 올바르지 않아 서버 연결을 껐습니다: ${failure.message.orEmpty()}")
        }
    }

    fun save(context: Context, settings: TabDptServerSettings) =
        save(File(context.filesDir, FILE_NAME), settings)

    internal fun save(file: File, settings: TabDptServerSettings) {
        requireValidSettings(settings)
        val p = Properties().apply {
            setProperty("enabled", settings.enabled.toString())
            setProperty("endpoint", settings.endpoint.trim())
            setProperty("bearerToken", settings.bearerToken)
        }
        file.parentFile?.mkdirs()
        val temporary = File(file.parentFile, file.name + ".tmp")
        temporary.outputStream().use { p.store(it, "SleepWell TabDPT research server") }
        if (!temporary.renameTo(file)) {
            temporary.copyTo(file, overwrite = true)
            temporary.delete()
        }
    }

    fun requireValidEndpoint(value: String) {
        val uri = runCatching { URI(value.trim()) }.getOrNull()
            ?: throw IllegalArgumentException("서버 주소가 올바르지 않습니다.")
        require(uri.path == "/v1/predict" && uri.query == null && uri.fragment == null) {
            "서버 주소는 /v1/predict 경로여야 합니다."
        }
        require(uri.userInfo == null) { "서버 주소에 사용자 정보를 넣을 수 없습니다." }
        when (uri.scheme?.lowercase()) {
            "https" -> require(!uri.host.isNullOrBlank()) { "HTTPS 서버 호스트가 필요합니다." }
            "http" -> require(isPrivateOrLoopbackHost(uri.host)) {
                "암호화되지 않은 HTTP는 이 PC 또는 사설망 주소만 사용할 수 있습니다."
            }
            else -> throw IllegalArgumentException("서버 주소는 HTTP 또는 HTTPS여야 합니다.")
        }
    }

    fun requireValidSettings(settings: TabDptServerSettings) {
        requireValidEndpoint(settings.endpoint)
        require(settings.bearerToken.length <= 512) { "서버 토큰이 너무 깁니다." }
        if (settings.enabled) require(settings.bearerToken.length >= 24) {
            "TabDPT 서버를 켜려면 24자 이상의 접속 토큰이 필요합니다."
        }
    }

    private fun isPrivateOrLoopbackHost(host: String?): Boolean {
        if (host.isNullOrBlank()) return false
        if (host.equals("localhost", ignoreCase = true)) return true
        if (host == "::1" || host == "0:0:0:0:0:0:0:1") return true
        val parts = host.split('.')
        if (parts.size != 4) return false
        val octets = parts.map { it.toIntOrNull()?.takeIf { value -> value in 0..255 } ?: return false }
        return octets[0] == 127 || octets[0] == 10 ||
            (octets[0] == 172 && octets[1] in 16..31) ||
            (octets[0] == 192 && octets[1] == 168)
    }
}

internal data class TabDptPrediction(
    val cmiOnsetOffset: Double,
    val cmiWakeOffset: Double,
    val mesaOnsetOffset: Double,
    val mesaWakeOffset: Double,
    val modelVersion: String,
    val provenance: JSONObject,
    val backend: PredictionBackend = PredictionBackend.SERVER_TABDPT
) {
    val meanOnsetOffset: Double get() = (cmiOnsetOffset + mesaOnsetOffset) / 2.0
}

internal enum class PredictionBackend(
    val wireName: String,
    val displayName: String,
    val predictionKey: String
) {
    LOCAL_HGB("local_hgb_hybrid39", "기기 내 HGB", "hgb_hybrid"),
    LOCAL_TABDPT("local_tabdpt_hybrid39", "기기 내 경량 TabDPT", "tabdpt_hybrid"),
    SERVER_TABDPT("tabdpt_hybrid39_server", "서버 TabDPT", "tabdpt_hybrid")
}

internal sealed class TabDptResult {
    data class Success(val prediction: TabDptPrediction) : TabDptResult()
    data class Unavailable(
        val code: String,
        val detail: String,
        val backend: PredictionBackend = PredictionBackend.SERVER_TABDPT
    ) : TabDptResult()
}

internal fun interface TabDptPredictionProvider {
    fun predict(requestId: String, snapshots: JSONObject): TabDptResult
}

internal interface PredictionProviderMetadata {
    val backend: PredictionBackend
}

internal class HttpTabDptPredictionClient(
    private val settings: TabDptServerSettings
) : TabDptPredictionProvider, PredictionProviderMetadata {
    override val backend: PredictionBackend = PredictionBackend.SERVER_TABDPT
    init {
        TabDptSettingsStore.requireValidSettings(settings)
    }

    private val cache = object : LinkedHashMap<String, TabDptPrediction>(8, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, TabDptPrediction>?): Boolean = size > 8
    }

    @Synchronized
    override fun predict(requestId: String, snapshots: JSONObject): TabDptResult {
        if (!settings.enabled) return TabDptResult.Unavailable("disabled", "TabDPT 서버가 꺼져 있습니다.")
        return runCatching {
            TabDptSettingsStore.requireValidSettings(settings)
            val request = buildRequest(requestId, snapshots)
            val key = sha256(request.getJSONObject("features").toString() + TABDPT_MODEL_VERSION)
            synchronized(cache) { cache[key] }?.let { return TabDptResult.Success(it) }
            val connection = URL(settings.endpoint).openConnection() as HttpURLConnection
            try {
                connection.requestMethod = "POST"
                connection.instanceFollowRedirects = false
                connection.connectTimeout = 3_000
                connection.readTimeout = 45_000
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.setRequestProperty("Accept", "application/json")
                connection.setRequestProperty("Authorization", "Bearer ${settings.bearerToken}")
                connection.outputStream.use { it.write(request.toString().toByteArray(Charsets.UTF_8)) }
                val status = connection.responseCode
                if (status !in 200..299) throw IllegalStateException("서버 응답 코드 $status")
                val response = connection.inputStream.bufferedReader(Charsets.UTF_8).use { reader ->
                    val out = StringBuilder()
                    val chunk = CharArray(4096)
                    while (true) {
                        val count = reader.read(chunk)
                        if (count < 0) break
                        out.append(chunk, 0, count)
                        require(out.length <= 256 * 1024) { "서버 응답이 너무 큽니다." }
                    }
                    out.toString()
                }
                parseResponse(JSONObject(response), requestId).also { prediction ->
                    synchronized(cache) { cache[key] = prediction }
                }
            } finally {
                connection.disconnect()
            }
        }.fold(
            onSuccess = { TabDptResult.Success(it) },
            onFailure = { TabDptResult.Unavailable("request_failed", it.message ?: it::class.java.simpleName) }
        )
    }

    internal companion object {
        fun buildRequest(requestId: String, snapshots: JSONObject): JSONObject {
            require(requestId.isNotBlank() && requestId.length <= 128) { "request_id가 올바르지 않습니다." }
            val features = JSONObject()
            listOf("cmi", "mesa").forEach { cohort ->
                val snapshot = snapshots.getJSONObject(cohort)
                val values = JSONObject()
                PortableFusionModel.HYBRID_FEATURE_ORDER.forEach { name ->
                    require(snapshot.has(name)) { "$cohort 하이브리드 입력에 $name 항목이 없습니다." }
                    val value = snapshot.opt(name)
                    require(value == JSONObject.NULL || value is Number) { "$cohort 하이브리드의 $name 값 형식이 잘못됐습니다." }
                    if (value is Number) require(value.toDouble().isFinite()) { "$cohort 하이브리드의 $name 값이 유한하지 않습니다." }
                    values.put(name, if (value is Number) value.toDouble() else JSONObject.NULL)
                }
                require(values.length() == PortableFusionModel.HYBRID_FEATURE_ORDER.size) { "하이브리드 입력 수가 맞지 않습니다." }
                features.put(cohort, values)
            }
            return JSONObject()
                .put("schema", TABDPT_REQUEST_SCHEMA)
                .put("request_id", requestId)
                .put("feature_contract", TABDPT_FEATURE_CONTRACT)
                .put("features", features)
        }

        fun parseResponse(response: JSONObject, requestId: String): TabDptPrediction {
            require(response.getString("schema") == TABDPT_RESPONSE_SCHEMA) { "응답 schema가 맞지 않습니다." }
            require(response.getString("request_id") == requestId) { "응답 request_id가 맞지 않습니다." }
            require(response.getString("feature_contract") == TABDPT_FEATURE_CONTRACT) {
                "응답 feature_contract가 맞지 않습니다."
            }
            require(response.getString("model_version") == TABDPT_MODEL_VERSION) { "허용된 TabDPT 모델 버전이 아닙니다." }
            val predictions = response.getJSONObject("predictions")
            fun pair(cohort: String): Pair<Double, Double> {
                val value = predictions.getJSONObject(cohort)
                val onset = value.getDouble("onset_offset_min")
                val wake = value.getDouble("wake_offset_min")
                require(onset.isFinite() && wake.isFinite() && onset in 0.0..960.0 &&
                    wake >= onset + 30.0 && wake <= minOf(1440.0, onset + 960.0)) {
                    "$cohort 예측 범위가 올바르지 않습니다."
                }
                return onset to wake
            }
            val cmi = pair("cmi")
            val mesa = pair("mesa")
            return TabDptPrediction(
                cmi.first, cmi.second, mesa.first, mesa.second,
                TABDPT_MODEL_VERSION, JSONObject(response.optJSONObject("provenance")?.toString() ?: "{}")
            )
        }

        private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }
}
