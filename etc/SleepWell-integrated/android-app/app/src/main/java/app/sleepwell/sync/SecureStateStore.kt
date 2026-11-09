package app.sleepwell.sync

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class SecureStateStore(context: Context) {
    private val prefs = context.getSharedPreferences("pc_sync_secure", Context.MODE_PRIVATE)

    fun put(key: String, value: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val payload = JSONObject()
            .put("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .put("data", Base64.encodeToString(cipher.doFinal(value.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP))
        check(prefs.edit().putString(key, payload.toString()).commit()) { "보안 저장소에 기록하지 못했습니다." }
    }

    fun get(key: String): String? = runCatching {
        val payload = JSONObject(prefs.getString(key, null) ?: return null)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        val iv = Base64.decode(payload.getString("iv"), Base64.NO_WRAP)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, iv))
        String(cipher.doFinal(Base64.decode(payload.getString("data"), Base64.NO_WRAP)), Charsets.UTF_8)
    }.getOrNull()

    fun remove(vararg keys: String) {
        val editor = prefs.edit().also { value -> keys.forEach(value::remove) }
        check(editor.commit()) { "보안 저장소에서 삭제하지 못했습니다." }
    }

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }

    companion object {
        private const val KEY_ALIAS = "sleepwell_pc_sync_v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val CONNECTION = "connection"
        const val PENDING = "pending"
        const val JOURNAL = "journal"
        const val LAST_RESULT = "last_result"
    }
}
