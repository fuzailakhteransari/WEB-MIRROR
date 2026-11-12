package com.webdesk.phonehost

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

data class HostRuntimeState(
    val running: Boolean = false,
    val connected: Boolean = false,
    val castReady: Boolean = false,
    val activeViewer: String? = null,
    val statusText: String = "Stopped",
)

class PhoneHostService : Service() {

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var prefs: HostPrefs
    private lateinit var quickActions: PhoneQuickActions
    private val rateLimiter = PinRateLimiter()

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .pingInterval(20, TimeUnit.SECONDS)
            .connectTimeout(12, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    private var captureEngine: ScreenCaptureEngine? = null

    @Volatile
    private var activeWs: WebSocket? = null

    @Volatile
    private var activeSession: PhonePeerSession? = null

    private var connectLoopJob: Job? = null
    private var heartbeatJob: Job? = null
    private val stopping = AtomicBoolean(false)

    override fun onCreate() {
        super.onCreate()
        prefs = HostPrefs(this)
        quickActions = PhoneQuickActions(this) { captureEngine?.isActive() == true }
        createNotificationChannel()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopHostCleanly()
                return START_NOT_STICKY
            }
            ACTION_START_CAST -> {
                val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
                val projData: Intent? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(EXTRA_PROJECTION_DATA, Intent::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(EXTRA_PROJECTION_DATA)
                }
                if (resultCode != Activity.RESULT_OK || projData == null) {
                    updateState(statusText = "Screen cast permission cancelled")
                    stopHostCleanly()
                    return START_NOT_STICKY
                }
                startForegroundTyped(useMediaProjection = true)
                setupMediaProjection(resultCode, projData)
                ensureSocketLoopRunning()
                return START_STICKY
            }
            ACTION_START_CONTROL_ONLY, null -> {
                val hasCast = captureEngine?.isActive() == true
                startForegroundTyped(useMediaProjection = hasCast)
                ensureSocketLoopRunning()
                return START_STICKY
            }
        }
        return START_STICKY
    }

    private fun startForegroundTyped(useMediaProjection: Boolean) {
        val notification = buildNotification(
            if (useMediaProjection) "Ready to mirror & control from anywhere"
            else "Control-Only Standby active"
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val fgsType = if (useMediaProjection) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            } else {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            }
            startForeground(NOTIF_ID, notification, fgsType)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    private fun setupMediaProjection(resultCode: Int, data: Intent) {
        captureEngine?.close()
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val projection = mpm.getMediaProjection(resultCode, data)
        if (projection == null) {
            updateState(castReady = false, statusText = "Failed to acquire MediaProjection")
            return
        }
        captureEngine = ScreenCaptureEngine(
            context = this,
            projection = projection,
            onProjectionStopped = {
                captureEngine = null
                updateState(
                    castReady = false,
                    statusText = if (_state.value.connected) "Online (Screen cast stopped — Control-Only)" else "Cast stopped"
                )
                refreshNotification()
            },
            onDimensionsChanged = { _, _ ->
                sendHeartbeat()
            }
        )
        updateState(running = true, castReady = true, statusText = "Starting host connection...")
    }

    private fun ensureSocketLoopRunning() {
        stopping.set(false)
        if (!prefs.hasPinConfigured() || prefs.getNormalizedWsUrl().isBlank()) {
            updateState(
                running = false,
                connected = false,
                statusText = "Configure Server URL & Access PIN first"
            )
            stopHostCleanly()
            return
        }

        updateState(running = true, castReady = captureEngine?.isActive() == true)
        if (connectLoopJob?.isActive == true) {
            // Re-register if cast capability just became ready
            if (activeWs != null) {
                sendRegisterHost(activeWs!!)
            }
            return
        }

        connectLoopJob = serviceScope.launch {
            var backoffMs = 1500L
            while (isActive && !stopping.get()) {
                val wsUrl = prefs.getNormalizedWsUrl()
                if (wsUrl.isBlank()) {
                    delay(3000L)
                    continue
                }
                updateState(connected = false, statusText = "Connecting to server...")
                refreshNotification()

                val closedSignal = kotlinx.coroutines.CompletableDeferred<Unit>()
                val req = Request.Builder().url(wsUrl).build()
                val ws = client.newWebSocket(req, object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        activeWs = webSocket
                        backoffMs = 1500L
                        sendRegisterHost(webSocket)
                        startHeartbeatLoop()
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        handleSignalingText(webSocket, text)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        cleanupSocket(webSocket, if (reason.isNotBlank()) reason else "Disconnected ($code)")
                        closedSignal.complete(Unit)
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        val msg = t.message ?: "Network error"
                        cleanupSocket(webSocket, "Reconnecting: $msg")
                        closedSignal.complete(Unit)
                    }
                })
                activeWs = ws
                closedSignal.await()
                if (stopping.get()) break
                delay(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(15_000L)
            }
        }
    }

    private fun cleanupSocket(ws: WebSocket, reason: String) {
        if (activeWs === ws) {
            activeWs = null
        }
        heartbeatJob?.cancel()
        heartbeatJob = null
        activeSession?.close()
        activeSession = null
        if (!stopping.get()) {
            updateState(connected = false, activeViewer = null, statusText = reason)
            refreshNotification()
        }
    }

    private fun startHeartbeatLoop() {
        heartbeatJob?.cancel()
        heartbeatJob = serviceScope.launch {
            while (isActive && !stopping.get()) {
                delay(15_000L)
                sendHeartbeat()
            }
        }
    }

    private fun sendRegisterHost(ws: WebSocket) {
        val (w, h) = getDisplayDimensions()
        val caps = mutableListOf("input", "clipboard", "files", "mobile", "ws_direct")
        if (captureEngine?.isActive() == true) {
            caps.add(0, "video")
        }
        val msg = JSONObject()
            .put("type", "register_host")
            .put(
                "payload",
                JSONObject()
                    .put("host_id", prefs.getOrCreateHostId())
                    .put("name", prefs.hostName)
                    .put("platform", "android")
                    .put("mode", "me2me")
                    .put(
                        "monitors",
                        JSONArray().put(
                            JSONObject()
                                .put("index", 1)
                                .put("name", "Phone Screen (${w}x${h})")
                                .put("left", 0)
                                .put("top", 0)
                                .put("width", w)
                                .put("height", h)
                                .put("is_primary", true)
                        )
                    )
                    .put("capabilities", JSONArray(caps))
            )
        ws.send(msg.toString())
    }

    private fun sendHeartbeat() {
        val ws = activeWs ?: return
        val (w, h) = getDisplayDimensions()
        val msg = JSONObject()
            .put("type", "heartbeat")
            .put(
                "payload",
                JSONObject().put(
                    "monitors",
                    JSONArray().put(
                        JSONObject()
                            .put("index", 1)
                            .put("name", "Phone Screen (${w}x${h})")
                            .put("left", 0)
                            .put("top", 0)
                            .put("width", w)
                            .put("height", h)
                            .put("is_primary", true)
                    )
                )
            )
        ws.send(msg.toString())
    }

    private fun handleSignalingText(ws: WebSocket, raw: String) {
        val msg = try {
            JSONObject(raw)
        } catch (_: Exception) {
            return
        }
        val type = msg.optString("type")
        val payload = msg.optJSONObject("payload") ?: JSONObject()

        when (type) {
            "host_registered" -> {
                val cast = captureEngine?.isActive() == true
                updateState(
                    running = true,
                    connected = true,
                    castReady = cast,
                    statusText = if (cast) "Online • Mirror + Control Ready" else "Online • Control-Only Standby"
                )
                refreshNotification()
            }

            "connect_request" -> {
                val sessionId = msg.optString("session_id")
                if (sessionId.isBlank()) return
                activeSession?.close()

                val mode = payload.optString("mode", "me2me")
                val sdpOffer = payload.optString("sdp_offer", "")
                val pairingId = payload.optString("pairing_id").takeIf { it.isNotBlank() && it != "null" }
                val controlOnly = payload.optBoolean("control_only", false)
                val viewerLabel = payload.optString("viewer_label", "Remote Viewer")

                val session = PhonePeerSession(
                    sessionId = sessionId,
                    mode = mode,
                    viewerLabel = viewerLabel,
                    prefs = prefs,
                    rateLimiter = rateLimiter,
                    quickActions = quickActions,
                    captureEngineProvider = { captureEngine },
                    displaySizeProvider = { getDisplayDimensions() },
                    sendJson = { obj -> activeWs?.send(obj.toString()) },
                    sendBinaryFrame = { frameBytes ->
                        activeWs?.send(frameBytes.toByteString(0, frameBytes.size)) ?: false
                    },
                    onClosed = { closedSid ->
                        if (activeSession?.sessionId == closedSid) {
                            activeSession = null
                            val cast = captureEngine?.isActive() == true
                            updateState(
                                activeViewer = null,
                                castReady = cast,
                                statusText = if (_state.value.connected) {
                                    if (cast) "Online • Mirror + Control Ready" else "Online • Control-Only Standby"
                                } else {
                                    "Disconnected"
                                }
                            )
                            refreshNotification()
                        }
                    },
                    scope = serviceScope,
                )
                activeSession = session
                updateState(
                    activeViewer = viewerLabel,
                    statusText = if (controlOnly) "Quick Actions active ($viewerLabel)" else "Mirroring to $viewerLabel"
                )
                refreshNotification()
                session.startFromOffer(sdpOffer, pairingId, controlOnly)
            }

            "pake_client_proof",
            "pairing_client_proof",
            "enable_ws_fallback",
            "ws_frame_ack",
            "ws_channel",
            "session_end" -> {
                val sid = msg.optString("session_id")
                val session = activeSession
                if (session != null && (sid.isBlank() || sid == session.sessionId)) {
                    session.handleSignalMessage(msg)
                }
            }
        }
    }

    private fun getDisplayDimensions(): Pair<Int, Int> {
        return captureEngine?.getRealSize() ?: WebDeskAccessibilityService.getRealDisplaySize(this)
    }

    private fun stopHostCleanly() {
        stopping.set(true)
        heartbeatJob?.cancel()
        connectLoopJob?.cancel()
        activeSession?.close()
        activeSession = null
        activeWs?.close(1000, "Host stopped")
        activeWs = null
        captureEngine?.close()
        captureEngine = null
        quickActions.close()
        updateState(
            running = false,
            connected = false,
            castReady = false,
            activeViewer = null,
            statusText = "Stopped"
        )
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        stopping.set(true)
        heartbeatJob?.cancel()
        connectLoopJob?.cancel()
        activeSession?.close()
        activeWs?.close(1000, "Service destroyed")
        captureEngine?.close()
        quickActions.close()
        serviceScope.cancel()
        updateState(running = false, connected = false, castReady = false, activeViewer = null, statusText = "Stopped")
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            "WebDesk Phone Host",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Keeps your phone reachable for remote mirroring and control"
        }
        nm.createNotificationChannel(channel)
    }

    private fun refreshNotification() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIF_ID, buildNotification(_state.value.statusText))
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stopIntent = PendingIntent.getService(
            this,
            1,
            Intent(this, PhoneHostService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(prefs.hostName)
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openIntent)
            .addAction(0, "Stop Host", stopIntent)
            .build()
    }

    private fun updateState(
        running: Boolean = _state.value.running,
        connected: Boolean = _state.value.connected,
        castReady: Boolean = _state.value.castReady,
        activeViewer: String? = _state.value.activeViewer,
        statusText: String = _state.value.statusText,
    ) {
        _state.value = HostRuntimeState(
            running = running,
            connected = connected,
            castReady = castReady,
            activeViewer = activeViewer,
            statusText = statusText,
        )
    }

    companion object {
        const val ACTION_START_CAST = "com.webdesk.phonehost.START_CAST"
        const val ACTION_START_CONTROL_ONLY = "com.webdesk.phonehost.START_CONTROL_ONLY"
        const val ACTION_STOP = "com.webdesk.phonehost.STOP"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_PROJECTION_DATA = "projection_data"

        private const val CHANNEL_ID = "webdesk_phone_host"
        private const val NOTIF_ID = 4201

        private val _state = MutableStateFlow(HostRuntimeState())
        val state: StateFlow<HostRuntimeState> = _state.asStateFlow()
    }
}
