package app.sleepwell.user

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class UserSecureStore(context: Context) {
    private val prefs = context.getSharedPreferences("sleepwell_user_secure_v1", Context.MODE_PRIVATE)

    fun load(): UserState = synchronized(LOCK) { decrypt(KEY_STATE)?.let { userStateFromJson(JSONObject(it)) }
        ?: UserState(participantKey = newParticipantKey()).also(::save) }

    fun save(value: UserState) = synchronized(LOCK) { encrypt(KEY_STATE, value.toJson().toString()) }

    fun mutateState(transform: (UserState) -> UserState): UserState = synchronized(LOCK) {
        val current = decrypt(KEY_STATE)?.let { userStateFromJson(JSONObject(it)) }
            ?: UserState(participantKey = newParticipantKey())
        transform(current).also { encrypt(KEY_STATE, it.toJson().toString()) }
    }

    fun pending(): List<PendingBackup> = synchronized(LOCK) { decrypt(KEY_QUEUE)?.let { raw ->
        val a = JSONArray(raw)
        (0 until a.length()).map { i -> a.getJSONObject(i).let { PendingBackup(it.getString("day"), it.getInt("revision"), it.getString("payload")) } }
    } ?: emptyList() }

    fun replacePending(values: List<PendingBackup>) = synchronized(LOCK) {
        val a = JSONArray().also { out -> values.forEach { out.put(JSONObject().put("day", it.day).put("revision", it.revision).put("payload", it.payload)) } }
        encrypt(KEY_QUEUE, a.toString())
    }

    fun createPending(day: String, payload: (Int) -> String): PendingBackup = synchronized(LOCK) {
        val revisions = JSONObject(decrypt(KEY_REVISIONS) ?: "{}")
        val next = revisions.optInt(day, 0) + 1
        revisions.put(day, next)
        encrypt(KEY_REVISIONS, revisions.toString())
        val item = PendingBackup(day, next, payload(next))
        replacePending(pending().filterNot { it.day == day } + item)
        item
    }

    fun acknowledge(day: String, revision: Int): Boolean = synchronized(LOCK) {
        val current = pending()
        val next = current.filterNot { UserContractRules.isAcknowledged(it.day, it.revision, day, revision) }
        if (next.size == current.size) false else { replacePending(next); true }
    }

    private fun encrypt(key: String, value: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, secretKey()) }
        val payload = JSONObject().put("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .put("data", Base64.encodeToString(cipher.doFinal(value.toByteArray()), Base64.NO_WRAP))
        check(prefs.edit().putString(key, payload.toString()).commit()) { "로컬 보안 저장소 기록에 실패했습니다." }
    }

    private fun decrypt(key: String): String? {
        val stored = prefs.getString(key, null) ?: return null
        return runCatching {
        val payload = JSONObject(stored)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, Base64.decode(payload.getString("iv"), Base64.NO_WRAP)))
        String(cipher.doFinal(Base64.decode(payload.getString("data"), Base64.NO_WRAP)))
        }.getOrElse { throw IllegalStateException("암호화된 로컬 자료를 읽지 못했습니다. 앱 데이터를 임의로 초기화하지 않았습니다.", it) }
    }

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true).build())
            generateKey()
        }
    }

    private fun newParticipantKey(): String = ByteArray(16).also(SecureRandom()::nextBytes)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    companion object {
        private const val ALIAS = "sleepwell_user_data_v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val KEY_STATE = "user_state"
        private const val KEY_QUEUE = "backup_queue"
        private const val KEY_REVISIONS = "backup_revisions"
        private val LOCK = Any()
    }
}
