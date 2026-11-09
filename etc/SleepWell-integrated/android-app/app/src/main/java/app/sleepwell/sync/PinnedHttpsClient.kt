package app.sleepwell.sync

import android.annotation.SuppressLint
import org.json.JSONObject
import java.net.URL
import java.security.MessageDigest
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

class PinnedHttpsClient(private val config: PairingConfig) {
    fun post(path: String, body: JSONObject, bearer: String? = null): JSONObject {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= MAX_BODY_BYTES) { "전송 데이터가 1MB를 넘습니다." }
        val connection = (URL(config.baseUrl + path).openConnection() as HttpsURLConnection).apply {
            connectTimeout = 10_000
            readTimeout = 20_000
            requestMethod = "POST"
            doOutput = true
            instanceFollowRedirects = false
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
            setRequestProperty("Accept", "application/json")
            bearer?.let { setRequestProperty("Authorization", "Bearer $it") }
            sslSocketFactory = pinnedContext().socketFactory
            hostnameVerifier = HostnameVerifier { hostname, _ -> hostname == config.host }
        }
        connection.outputStream.use { it.write(bytes) }
        val code = connection.responseCode
        val stream = if (code in 200..299) connection.inputStream else connection.errorStream
        val response = stream?.use { input ->
            val buffer = ByteArray(MAX_BODY_BYTES + 1)
            var size = 0
            while (true) {
                val n = input.read(buffer, size, buffer.size - size)
                if (n < 0) break
                size += n
                require(size <= MAX_BODY_BYTES) { "PC 응답이 1MB를 넘습니다." }
                if (size == buffer.size) error("PC 응답이 1MB를 넘습니다.")
            }
            String(buffer, 0, size, Charsets.UTF_8)
        }.orEmpty()
        connection.disconnect()
        if (code !in 200..299) throw HttpStatusException(code)
        return if (response.isBlank()) JSONObject() else JSONObject(response)
    }

    @SuppressLint("CustomX509TrustManager")
    private fun pinnedContext(): SSLContext {
        val expected = config.certificateSha256
        val trustManager = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) =
                error("클라이언트 인증서는 지원하지 않습니다.")

            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                val leaf = chain?.firstOrNull() ?: error("PC 인증서가 없습니다.")
                leaf.checkValidity()
                val actual = MessageDigest.getInstance("SHA-256")
                    .digest(leaf.encoded)
                    .joinToString("") { "%02x".format(it) }
                require(MessageDigest.isEqual(actual.toByteArray(), expected.toByteArray())) {
                    "PC 인증서 지문이 일치하지 않습니다."
                }
            }
        }
        return SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustManager), null) }
    }

    companion object { const val MAX_BODY_BYTES = 1_000_000 }
}

class HttpStatusException(val statusCode: Int) : Exception("PC가 요청을 거부했습니다. (HTTP $statusCode)")
