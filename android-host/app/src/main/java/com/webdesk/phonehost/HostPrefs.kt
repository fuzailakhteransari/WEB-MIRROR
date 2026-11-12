package com.webdesk.phonehost

import android.content.Context
import android.net.Uri
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/**
 * Persists phone host identity, normalized signaling URL, SRP-6a verifier,
 * and remembered viewer pairing HMAC keys without ever storing plaintext PIN.
 */
class HostPrefs(context: Context) {

    private val prefs = context.getSharedPreferences("webdesk_phone_host", Context.MODE_PRIVATE)

    @Synchronized
    fun getOrCreateHostId(): String {
        val existing = prefs.getString("host_id", null)
        if (!existing.isNullOrBlank()) return existing
        val created = "android-" + UUID.randomUUID().toString().replace("-", "").take(12)
        prefs.edit().putString("host_id", created).apply()
        return created
    }

    var hostName: String
        get() {
            val saved = prefs.getString("host_name", null)
            if (!saved.isNullOrBlank()) return saved
            val brand = Build.MANUFACTURER.orEmpty().trim().replaceFirstChar { it.uppercase() }
            val model = Build.MODEL.orEmpty().trim()
            return listOf(brand, model).filter { it.isNotBlank() }.distinct().joinToString(" ")
                .ifBlank { "Android Phone" }
        }
        set(value) {
            prefs.edit().putString("host_name", value.trim()).apply()
        }

    var rawServerUrl: String
        get() = prefs.getString("server_url", "") ?: ""
        set(value) {
            prefs.edit().putString("server_url", value.trim()).apply()
        }

    var workspaceToken: String
        get() = prefs.getString("workspace_token", "") ?: ""
        set(value) {
            prefs.edit().putString("workspace_token", value.trim()).apply()
        }

    var autoWakeOnConnect: Boolean
        get() = prefs.getBoolean("auto_wake", true)
        set(value) {
            prefs.edit().putBoolean("auto_wake", value).apply()
        }

    var bootControlStandby: Boolean
        get() = prefs.getBoolean("boot_standby", false)
        set(value) {
            prefs.edit().putBoolean("boot_standby", value).apply()
        }

    val srpSalt: String
        get() = prefs.getString("srp_salt", "") ?: ""

    val srpVerifier: String
        get() = prefs.getString("srp_verifier", "") ?: ""

    fun hasPinConfigured(): Boolean = srpSalt.isNotBlank() && srpVerifier.isNotBlank()

    fun setPin(pin: String) {
        val hostId = getOrCreateHostId()
        val (saltHex, verifierHex) = MobileSrp.createVerifier(hostId, pin)
        prefs.edit()
            .putString("srp_salt", saltHex)
            .putString("srp_verifier", verifierHex)
            .apply()
    }

    @Synchronized
    fun createPairing(label: String): Pair<String, String> {
        val pairingId = "pair-" + MobileSrp.bytesToHex(MobileSrp.randomBytes(6))
        val rawSecret = MobileSrp.bytesToHex(MobileSrp.randomBytes(32))
        val authKey = MobileSrp.hashPairingSecret(rawSecret)

        val arr = readPairingsArray()
        val entry = JSONObject()
            .put("pairing_id", pairingId)
            .put("label", label.take(60))
            .put("auth_key", authKey)
            .put("created_at", System.currentTimeMillis() / 1000L)
        arr.put(entry)
        // Keep last 20 pairings
        val trimmed = JSONArray()
        val start = maxOf(0, arr.length() - 20)
        for (i in start until arr.length()) {
            trimmed.put(arr.get(i))
        }
        prefs.edit().putString("pairings_json", trimmed.toString()).apply()
        return pairingId to rawSecret
    }

    @Synchronized
    fun getPairingAuthKey(pairingId: String?): String? {
        if (pairingId.isNullOrBlank()) return null
        val arr = readPairingsArray()
        for (i in 0 until arr.length()) {
            val obj = arr.optJSONObject(i) ?: continue
            if (obj.optString("pairing_id") == pairingId) {
                return obj.optString("auth_key").takeIf { it.isNotBlank() }
            }
        }
        return null
    }

    @Synchronized
    fun pairedCount(): Int = readPairingsArray().length()

    @Synchronized
    fun clearPairings() {
        prefs.edit().remove("pairings_json").apply()
    }

    private fun readPairingsArray(): JSONArray {
        val raw = prefs.getString("pairings_json", null) ?: return JSONArray()
        return try {
            JSONArray(raw)
        } catch (_: Exception) {
            JSONArray()
        }
    }

    fun getNormalizedWsUrl(): String {
        val base = normalizeServerWsUrl(rawServerUrl)
        if (base.isBlank()) return ""
        val tok = workspaceToken
        if (tok.isBlank()) return base
        val sep = if ("?" in base) "&" else "?"
        return "$base${sep}token=${Uri.encode(tok)}"
    }

    fun getPortalHttpUrl(): String = wsUrlToHttpBase(normalizeServerWsUrl(rawServerUrl))

    companion object {
        fun normalizeServerWsUrl(raw: String): String {
            val cleaned = raw.trim()
            if (cleaned.isBlank()) return ""
            val withScheme = if ("://" in cleaned) cleaned else "https://$cleaned"
            val uri = Uri.parse(withScheme)
            val scheme = when (uri.scheme?.lowercase()) {
                "https", "wss" -> "wss"
                "http", "ws" -> "ws"
                else -> return ""
            }
            val hostPort = uri.encodedAuthority ?: return ""
            var path = (uri.encodedPath ?: "").trimEnd('/')
            if (path.isBlank()) {
                path = "/ws/host"
            } else if (!path.endsWith("/ws/host")) {
                path = "$path/ws/host"
            }
            return "$scheme://$hostPort$path"
        }

        fun wsUrlToHttpBase(wsUrl: String): String {
            if (wsUrl.isBlank()) return ""
            val uri = Uri.parse(wsUrl)
            val httpScheme = if (uri.scheme == "wss") "https" else "http"
            val auth = uri.encodedAuthority ?: return ""
            return "$httpScheme://$auth"
        }
    }
}

class PinRateLimiter(private val maxAttempts: Int = 5) {
    private var failures = 0
    private var lockedUntilMs = 0L

    @Synchronized
    fun isLocked(): Pair<Boolean, Int> {
        val now = System.currentTimeMillis()
        if (now < lockedUntilMs) {
            val secs = ((lockedUntilMs - now + 999) / 1000).toInt().coerceAtLeast(1)
            return true to secs
        }
        return false to 0
    }

    @Synchronized
    fun recordFailure() {
        failures += 1
        if (failures >= maxAttempts) {
            val shift = (failures - maxAttempts).coerceAtMost(5)
            val cooldownSec = 15L shl shift
            lockedUntilMs = System.currentTimeMillis() + cooldownSec * 1000L
        }
    }

    @Synchronized
    fun recordSuccess() {
        failures = 0
        lockedUntilMs = 0L
    }
}
