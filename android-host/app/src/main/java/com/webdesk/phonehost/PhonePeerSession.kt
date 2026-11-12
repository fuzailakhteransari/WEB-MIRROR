package com.webdesk.phonehost

import android.graphics.PointF
import android.os.Handler
import android.os.Looper
import android.util.Base64
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Handles one authenticated remote viewer session over WebDesk signaling + WSS binary frame relay.
 */
class PhonePeerSession(
    val sessionId: String,
    val mode: String,
    val viewerLabel: String,
    private val prefs: HostPrefs,
    private val rateLimiter: PinRateLimiter,
    private val quickActions: PhoneQuickActions,
    private val captureEngineProvider: () -> ScreenCaptureEngine?,
    private val displaySizeProvider: () -> Pair<Int, Int>,
    private val sendJson: (JSONObject) -> Unit,
    private val sendBinaryFrame: (ByteArray) -> Boolean,
    private val onClosed: (String) -> Unit,
    private val scope: CoroutineScope,
) {
    private val mainHandler = Handler(Looper.getMainLooper())
    private val closed = AtomicBoolean(false)

    @Volatile
    private var authenticated = false

    @Volatile
    private var controlOnly = false

    @Volatile
    private var streamingStarted = false

    private var srpSession: MobileSrp.SrpServerSession? = null
    private var pairingIdPending: String? = null
    private var pairingNonce: String? = null
    private var dtlsContext: String = ""
    private var pendingPairingGrant: Pair<String, String>? = null

    private val sentSeq = AtomicInteger(0)
    private val ackedSeq = AtomicInteger(0)

    private class IncomingFile(
        val name: String,
        val expectedSize: Int,
        val stream: ByteArrayOutputStream = ByteArrayOutputStream(),
        val digest: MessageDigest = MessageDigest.getInstance("SHA-256"),
    )

    private val incomingFiles = ConcurrentHashMap<String, IncomingFile>()

    fun startFromOffer(sdpOffer: String, pairingId: String?, isControlOnly: Boolean) {
        controlOnly = isControlOnly

        val (locked, waitSec) = rateLimiter.isLocked()
        if (locked) {
            reject("Too many wrong PIN attempts. Retry in ${waitSec}s.")
            return
        }

        if (!prefs.hasPinConfigured()) {
            reject("Access PIN is not configured on this phone yet.")
            return
        }

        if (!controlOnly && captureEngineProvider()?.isActive() != true) {
            reject("Screen Cast is paused on target phone. Use Quick Actions or tap Start Mirror on the phone.")
            return
        }

        val hostFp = "ws:$sessionId"
        val extractedViewerFp = MobileSrp.extractDtlsFingerprint(sdpOffer)
        val viewerFp = if (extractedViewerFp.isNotBlank()) extractedViewerFp else "ws"
        dtlsContext = MobileSrp.buildDtlsContext(hostFp, viewerFp)

        if (mode == "me2me" && !pairingId.isNullOrBlank() && prefs.getPairingAuthKey(pairingId) != null) {
            pairingIdPending = pairingId
            pairingNonce = MobileSrp.bytesToHex(MobileSrp.randomBytes(24))
            sendJson(
                JSONObject()
                    .put("type", "pairing_challenge")
                    .put("session_id", sessionId)
                    .put(
                        "payload",
                        JSONObject()
                            .put("pairing_id", pairingId)
                            .put("nonce", pairingNonce)
                            .put("host_fp", hostFp)
                            .put("viewer_fp", viewerFp)
                            .put("ws_direct", true)
                    )
            )
            return
        }

        sendSrpChallenge(hostFp, viewerFp)
    }

    private fun sendSrpChallenge(hostFp: String, viewerFp: String) {
        val identity = prefs.getOrCreateHostId()
        val session = MobileSrp.SrpServerSession.start(
            identity = identity,
            saltHex = prefs.srpSalt,
            verifierHex = prefs.srpVerifier,
            dtlsContext = dtlsContext,
        )
        srpSession = session
        sendJson(
            JSONObject()
                .put("type", "pake_challenge")
                .put("session_id", sessionId)
                .put(
                    "payload",
                    JSONObject()
                        .put("identity", identity)
                        .put("salt", session.saltHex)
                        .put("B", session.bHex)
                        .put("host_fp", hostFp)
                        .put("viewer_fp", viewerFp)
                        .put("ws_direct", true)
                )
        )
    }

    fun handleSignalMessage(msg: JSONObject) {
        if (closed.get()) return
        val type = msg.optString("type")
        val payload = msg.optJSONObject("payload") ?: JSONObject()

        when (type) {
            "pake_client_proof" -> {
                val srp = srpSession ?: run {
                    reject("Unexpected PAKE proof")
                    return
                }
                val aHex = payload.optString("A")
                val m1Hex = payload.optString("M1")
                val rememberDevice = payload.optBoolean("remember_device", false)
                val label = payload.optString("viewer_label", viewerLabel).ifBlank { "Viewer" }

                val m2Hex = try {
                    srp.verifyClientProof(aHex, m1Hex)
                } catch (_: SecurityException) {
                    rateLimiter.recordFailure()
                    reject("Invalid PIN or verification context")
                    return
                }

                rateLimiter.recordSuccess()
                if (rememberDevice && mode == "me2me") {
                    pendingPairingGrant = prefs.createPairing(label)
                }
                sendJson(
                    JSONObject()
                        .put("type", "pake_server_proof")
                        .put("session_id", sessionId)
                        .put(
                            "payload",
                            JSONObject()
                                .put("M2", m2Hex)
                                .put("ws_direct", true)
                        )
                )
                unlockSession()
            }

            "pairing_client_proof" -> {
                if (payload.optBoolean("fallback_to_pin", false)) {
                    val parts = dtlsContext.split("|")
                    val hostFp = parts.getOrNull(0)?.removePrefix("host:") ?: "ws:$sessionId"
                    val viewerFp = parts.getOrNull(1)?.removePrefix("viewer:") ?: "ws"
                    sendSrpChallenge(hostFp, viewerFp)
                    return
                }
                val authKey = prefs.getPairingAuthKey(pairingIdPending)
                val nonce = pairingNonce
                val proof = payload.optString("proof")
                if (authKey.isNullOrBlank() || nonce.isNullOrBlank()) {
                    reject("Pairing credential invalid or revoked", code = "pairing_invalid")
                    return
                }
                if (!MobileSrp.verifyPairingHmac(authKey, proof, nonce, dtlsContext)) {
                    rateLimiter.recordFailure()
                    reject("Pairing proof rejected", code = "pairing_invalid")
                    return
                }
                rateLimiter.recordSuccess()
                sendJson(
                    JSONObject()
                        .put("type", "pairing_accepted")
                        .put("session_id", sessionId)
                        .put(
                            "payload",
                            JSONObject()
                                .put("pairing_id", pairingIdPending)
                                .put("ws_direct", true)
                        )
                )
                unlockSession()
            }

            "enable_ws_fallback" -> {
                if (!authenticated) {
                    reject("WebSocket relay requires authentication")
                    return
                }
                activateStreaming()
            }

            "ws_frame_ack" -> {
                if (!authenticated) return
                val ack = payload.optInt("seq", 0)
                if (ack > ackedSeq.get()) {
                    ackedSeq.set(ack)
                }
            }

            "ws_channel" -> {
                if (!authenticated) return
                val channel = payload.optString("channel")
                val data = payload.optJSONObject("data") ?: return
                dispatchChannelMessage(channel, data)
            }

            "session_end" -> close()
        }
    }

    private fun unlockSession() {
        authenticated = true
        if (controlOnly) {
            val ready = JSONObject()
                .put("type", "session_ready")
                .put("control_only", true)
                .put("platform", "android")
                .put("capabilities", JSONArray(listOf("video", "input", "clipboard", "files", "mobile", "ws_direct")))
                .put("monitors", buildMonitorsJson())
                .put("active_monitor", 1)
                .put("system_status", quickActions.getSystemStatus())
            sendChannel("control", ready)
            sendPendingPairingGrant()
        }
    }

    private fun activateStreaming() {
        if (streamingStarted || closed.get()) return
        val engine = captureEngineProvider()
        if (engine == null || !engine.isActive()) {
            reject("Screen Cast is not active on this phone")
            return
        }
        streamingStarted = true

        if (prefs.autoWakeOnConnect) {
            quickActions.wakeScreen()
        }

        sendJson(
            JSONObject()
                .put("type", "ws_fallback_ready")
                .put("session_id", sessionId)
                .put(
                    "payload",
                    JSONObject()
                        .put("platform", "android")
                        .put("capabilities", JSONArray(listOf("video", "input", "clipboard", "files", "mobile", "ws_direct")))
                        .put("monitors", buildMonitorsJson())
                        .put("active_monitor", 1)
                )
        )
        sendPendingPairingGrant()

        val sidBytes = sessionId.toByteArray(StandardCharsets.UTF_8)
        val header = ByteBuffer.allocate(2 + sidBytes.size)
            .putShort(sidBytes.size.toShort())
            .put(sidBytes)
            .array()

        engine.startStreaming { frame ->
            if (closed.get()) return@startStreaming
            val inFlight = sentSeq.get() - ackedSeq.get()
            if (inFlight >= 2) {
                return@startStreaming
            }
            val packet = ByteArray(header.size + frame.bytes.size)
            System.arraycopy(header, 0, packet, 0, header.size)
            System.arraycopy(frame.bytes, 0, packet, header.size, frame.bytes.size)
            if (sendBinaryFrame(packet)) {
                sentSeq.incrementAndGet()
            }
        }
    }

    private fun sendPendingPairingGrant() {
        val grant = pendingPairingGrant ?: return
        pendingPairingGrant = null
        sendChannel(
            "control",
            JSONObject()
                .put("type", "pairing_grant")
                .put("pairing_id", grant.first)
                .put("pairing_secret", grant.second)
        )
    }

    private fun dispatchChannelMessage(label: String, data: JSONObject) {
        val msgType = data.optString("type")
        when (label) {
            "input" -> handleInputMessage(msgType, data)
            "control" -> handleControlMessage(msgType, data)
            "clipboard" -> handleClipboardMessage(msgType, data)
            "files" -> handleFileMessage(msgType, data)
        }
    }

    private fun handleInputMessage(msgType: String, data: JSONObject) {
        val acc = WebDeskAccessibilityService.instance ?: return
        mainHandler.post {
            when (msgType) {
                "touch_tap" -> {
                    acc.performTap(
                        data.optDouble("x", 0.5).toFloat(),
                        data.optDouble("y", 0.5).toFloat()
                    )
                }
                "touch_long_press" -> {
                    acc.performLongPress(
                        data.optDouble("x", 0.5).toFloat(),
                        data.optDouble("y", 0.5).toFloat()
                    )
                }
                "touch_swipe" -> {
                    val arr = data.optJSONArray("points") ?: return@post
                    val pts = ArrayList<PointF>(arr.length())
                    for (i in 0 until arr.length()) {
                        val pt = arr.optJSONObject(i) ?: continue
                        pts.add(
                            PointF(
                                pt.optDouble("x", 0.5).toFloat(),
                                pt.optDouble("y", 0.5).toFloat()
                            )
                        )
                    }
                    val durationMs = data.optLong("duration_ms", 220L)
                    acc.performSwipe(pts, durationMs)
                }
                "mouse_button" -> {
                    // Fallback if viewer sends standard click down=false
                    if (!data.optBoolean("down", true) && data.optInt("button", 0) == 0) {
                        acc.performTap(
                            data.optDouble("x", 0.5).toFloat(),
                            data.optDouble("y", 0.5).toFloat()
                        )
                    } else if (!data.optBoolean("down", true) && data.optInt("button", 0) == 2) {
                        acc.performCombo("android_back")
                    }
                }
                "mouse_wheel" -> {
                    acc.performWheelScroll(
                        data.optDouble("x", 0.5).toFloat(),
                        data.optDouble("y", 0.5).toFloat(),
                        data.optDouble("dx", 0.0).toFloat(),
                        data.optDouble("dy", 0.0).toFloat()
                    )
                }
                "special_combo" -> {
                    acc.performCombo(data.optString("combo"))
                }
                "paste_text" -> {
                    acc.inputText(data.optString("text"))
                }
                "key_event" -> {
                    acc.handleKeyEvent(
                        data.optString("code"),
                        data.optString("key"),
                        data.optBoolean("down", true)
                    )
                }
            }
        }
    }

    private fun handleControlMessage(msgType: String, data: JSONObject) {
        when (msgType) {
            "set_quality" -> {
                val preset = data.optString("preset", "auto")
                captureEngineProvider()?.setQualityPreset(preset)
                sendChannel(
                    "control",
                    JSONObject().put("type", "quality_changed").put("preset", preset)
                )
            }
            "system_status" -> {
                sendChannel(
                    "control",
                    JSONObject()
                        .put("type", "system_status")
                        .put("status", quickActions.getSystemStatus())
                )
            }
            "quick_snapshot" -> {
                val engine = captureEngineProvider()
                if (engine == null || !engine.isActive()) {
                    sendChannel(
                        "control",
                        JSONObject()
                            .put("type", "quick_action_result")
                            .put("action", "quick_snapshot")
                            .put("ok", false)
                            .put("message", "Screen Cast is paused on target phone")
                    )
                    return
                }
                scope.launch(Dispatchers.IO) {
                    val frame = engine.captureSingleSnapshot()
                    if (frame != null) {
                        sendChannel(
                            "control",
                            JSONObject()
                                .put("type", "quick_snapshot")
                                .put("image_b64", Base64.encodeToString(frame.bytes, Base64.NO_WRAP))
                                .put("width", frame.width)
                                .put("height", frame.height)
                        )
                    } else {
                        sendChannel(
                            "control",
                            JSONObject()
                                .put("type", "quick_action_result")
                                .put("action", "quick_snapshot")
                                .put("ok", false)
                                .put("message", "Snapshot timed out (wake screen and try again)")
                        )
                    }
                }
            }
            "quick_action" -> {
                val action = data.optString("action")
                val params = data.optJSONObject("params")
                val (ok, message) = quickActions.execute(action, params)
                sendChannel(
                    "control",
                    JSONObject()
                        .put("type", "quick_action_result")
                        .put("action_id", data.opt("action_id"))
                        .put("action", action)
                        .put("ok", ok)
                        .put("message", message)
                        .put("status", quickActions.getSystemStatus())
                )
            }
        }
    }

    private fun handleClipboardMessage(msgType: String, data: JSONObject) {
        when (msgType) {
            "clipboard_set" -> {
                val text = data.optString("text")
                quickActions.setClipboard(text)
                sendChannel(
                    "clipboard",
                    JSONObject().put("type", "clipboard_ack").put("length", text.length)
                )
            }
            "clipboard_pull" -> {
                sendChannel(
                    "clipboard",
                    JSONObject().put("type", "clipboard_sync").put("text", quickActions.getClipboard())
                )
            }
        }
    }

    private fun handleFileMessage(msgType: String, data: JSONObject) {
        val fileId = data.optString("file_id")
        if (fileId.isBlank()) return

        when (msgType) {
            "file_offer" -> {
                val name = data.optString("name", "upload.bin")
                val size = data.optInt("size", 0)
                if (size < 0 || size > 35 * 1024 * 1024) {
                    sendChannel(
                        "files",
                        JSONObject()
                            .put("type", "file_error")
                            .put("file_id", fileId)
                            .put("reason", "Mobile upload limit is 35MB")
                    )
                    return
                }
                incomingFiles[fileId] = IncomingFile(name = name, expectedSize = size)
                sendChannel(
                    "files",
                    JSONObject()
                        .put("type", "file_accept")
                        .put("file_id", fileId)
                        .put("saved_name", name)
                )
            }
            "file_chunk" -> {
                val entry = incomingFiles[fileId] ?: return
                val b64 = data.optString("data")
                val chunk = Base64.decode(b64, Base64.DEFAULT)
                entry.stream.write(chunk)
                entry.digest.update(chunk)
            }
            "file_complete" -> {
                val entry = incomingFiles.remove(fileId) ?: return
                val actualSha = MobileSrp.bytesToHex(entry.digest.digest())
                val expectedSha = data.optString("sha256")
                if (expectedSha.isNotBlank() && !expectedSha.equals(actualSha, ignoreCase = true)) {
                    sendChannel(
                        "files",
                        JSONObject()
                            .put("type", "file_error")
                            .put("file_id", fileId)
                            .put("reason", "SHA-256 mismatch")
                    )
                    return
                }
                scope.launch(Dispatchers.IO) {
                    try {
                        val rawBytes = entry.stream.toByteArray()
                        val (savedPath, savedSize) = quickActions.saveUploadedFile(entry.name, rawBytes)
                        sendChannel(
                            "files",
                            JSONObject()
                                .put("type", "file_saved")
                                .put("file_id", fileId)
                                .put("path", savedPath)
                                .put("name", entry.name)
                                .put("size", savedSize)
                                .put("sha256", actualSha)
                        )
                    } catch (e: Exception) {
                        sendChannel(
                            "files",
                            JSONObject()
                                .put("type", "file_error")
                                .put("file_id", fileId)
                                .put("reason", e.message ?: "Save failed")
                        )
                    }
                }
            }
        }
    }

    private fun buildMonitorsJson(): JSONArray {
        val (w, h) = captureEngineProvider()?.getRealSize() ?: displaySizeProvider()
        return JSONArray().put(
            JSONObject()
                .put("index", 1)
                .put("name", "Phone Screen (${w}x${h})")
                .put("left", 0)
                .put("top", 0)
                .put("width", w)
                .put("height", h)
                .put("is_primary", true)
        )
    }

    private fun sendChannel(channel: String, data: JSONObject) {
        if (closed.get()) return
        sendJson(
            JSONObject()
                .put("type", "ws_channel")
                .put("session_id", sessionId)
                .put(
                    "payload",
                    JSONObject()
                        .put("channel", channel)
                        .put("data", data)
                )
        )
    }

    private fun reject(reason: String, code: String = "auth_failed") {
        sendJson(
            JSONObject()
                .put("type", "session_reject")
                .put("session_id", sessionId)
                .put(
                    "payload",
                    JSONObject()
                        .put("reason", reason)
                        .put("code", code)
                )
        )
        close()
    }

    fun close() {
        if (!closed.compareAndSet(false, true)) return
        authenticated = false
        if (streamingStarted) {
            streamingStarted = false
            captureEngineProvider()?.stopStreaming()
        }
        incomingFiles.clear()
        onClosed(sessionId)
    }
}
