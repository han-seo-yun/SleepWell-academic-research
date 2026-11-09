package app.sleepwell.sync

import android.net.Uri

data class PairingConfig(
    val host: String,
    val port: Int,
    val certificateSha256: String,
    val secret: String
) {
    val baseUrl: String get() = "https://$host:$port"
}

object PairingUri {
    private val pinPattern = Regex("^[0-9a-fA-F]{64}$")
    private val secretPattern = Regex("^[A-Za-z0-9_-]{43}$")

    fun parse(text: String): PairingConfig {
        val uri = Uri.parse(text.trim())
        require(uri.scheme == "sleepwell" && uri.host == "pair") { "SleepWell 연결 주소가 아닙니다." }
        require(uri.getQueryParameter("v") == "1") { "지원하지 않는 연결 주소 버전입니다." }
        val host = uri.getQueryParameter("host") ?: error("PC 주소가 없습니다.")
        require(isPrivateIpv4(host)) { "PC 주소는 같은 사설망의 IPv4 주소여야 합니다." }
        val port = uri.getQueryParameter("port")?.toIntOrNull()
            ?: error("포트 번호가 올바르지 않습니다.")
        require(port in 1..65535) { "포트 번호가 범위를 벗어났습니다." }
        val pin = uri.getQueryParameter("pin") ?: error("인증서 지문이 없습니다.")
        require(pinPattern.matches(pin)) { "인증서 지문 형식이 올바르지 않습니다." }
        val secret = uri.getQueryParameter("secret") ?: error("일회용 연결 암호가 없습니다.")
        require(secretPattern.matches(secret)) { "일회용 연결 암호 형식이 올바르지 않습니다." }
        return PairingConfig(host, port, pin.lowercase(), secret)
    }

    internal fun isPrivateIpv4(value: String): Boolean {
        val parts = value.split('.')
        if (parts.size != 4) return false
        val octets = parts.map {
            if (it.isEmpty() || (it.length > 1 && it.startsWith('0')) || !it.all(Char::isDigit)) return false
            it.toIntOrNull()?.takeIf { n -> n in 0..255 } ?: return false
        }
        return octets[0] == 10 ||
            (octets[0] == 172 && octets[1] in 16..31) ||
            (octets[0] == 192 && octets[1] == 168)
    }
}
