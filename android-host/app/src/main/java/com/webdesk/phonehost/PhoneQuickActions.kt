package com.webdesk.phonehost

import android.app.ActivityManager
import android.app.KeyguardManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.MediaStore
import android.view.KeyEvent
import org.json.JSONObject
import java.io.File
import java.io.OutputStream

class PhoneQuickActions(
    private val context: Context,
    private val hasScreenCast: () -> Boolean,
) {
    private val mainHandler = Handler(Looper.getMainLooper())
    private var activeRingtone: Ringtone? = null
    private var torchEnabled = false
    private val stopRingRunnable = Runnable { stopRinging() }

    @Suppress("DEPRECATION")
    fun wakeScreen(): Boolean {
        return try {
            val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            val wl = pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "WebDeskPhoneHost:Wake"
            )
            wl.acquire(3500L)
            true
        } catch (_: Exception) {
            false
        }
    }

    fun getSystemStatus(): JSONObject {
        val status = JSONObject()
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val plugged = intent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val pct = if (level >= 0 && scale > 0) (level * 100) / scale else bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)

        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val memInfo = ActivityManager.MemoryInfo()
        am.getMemoryInfo(memInfo)
        val totalMb = (memInfo.totalMem / (1024L * 1024L)).toInt()
        val availMb = (memInfo.availMem / (1024L * 1024L)).toInt()

        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager

        status.put("battery_percent", pct.coerceIn(0, 100))
        status.put("power_plugged", plugged != 0)
        status.put("ram_used_mb", (totalMb - availMb).coerceAtLeast(0))
        status.put("ram_total_mb", totalMb)
        status.put("screen_on", pm.isInteractive)
        status.put("device_locked", km.isKeyguardLocked)
        status.put("accessibility_enabled", WebDeskAccessibilityService.isEnabled())
        status.put("screen_cast_ready", hasScreenCast())
        status.put("torch_on", torchEnabled)
        status.put("network_type", readNetworkType())
        return status
    }

    private fun readNetworkType(): String {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val net = cm.activeNetwork ?: return "Offline"
        val caps = cm.getNetworkCapabilities(net) ?: return "Offline"
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "Wi-Fi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "Cellular"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "Ethernet"
            else -> "Online"
        }
    }

    fun execute(action: String, params: JSONObject?): Pair<Boolean, String> {
        return when (action) {
            "wake_screen" -> {
                val ok = wakeScreen()
                ok to if (ok) "Phone screen woken up" else "Failed to wake screen"
            }
            "lock_screen", "android_lock_screen" -> {
                val svc = WebDeskAccessibilityService.instance
                    ?: return false to "Enable Accessibility control in the app on the phone first"
                val ok = svc.performCombo("android_lock_screen")
                ok to if (ok) "Locked phone screen" else "Lock screen requires Android 9+"
            }
            "android_back",
            "android_home",
            "android_recents",
            "android_notifications",
            "android_quick_settings",
            "android_power_dialog",
            "android_screenshot" -> {
                val svc = WebDeskAccessibilityService.instance
                    ?: return false to "Enable Accessibility control on the target phone first"
                val ok = svc.performCombo(action)
                ok to if (ok) "Executed ${action.removePrefix("android_")}" else "Action unavailable"
            }
            "vol_up" -> adjustVolume(AudioManager.ADJUST_RAISE, "Volume increased")
            "vol_down" -> adjustVolume(AudioManager.ADJUST_LOWER, "Volume decreased")
            "vol_mute" -> adjustVolume(AudioManager.ADJUST_TOGGLE_MUTE, "Toggled mute")
            "media_play_pause" -> dispatchMediaKey(KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, "Toggled Play/Pause")
            "media_next" -> dispatchMediaKey(KeyEvent.KEYCODE_MEDIA_NEXT, "Skipped to next track")
            "media_prev" -> dispatchMediaKey(KeyEvent.KEYCODE_MEDIA_PREVIOUS, "Previous track")
            "find_phone_ring" -> startRinging()
            "stop_ring" -> {
                stopRinging()
                true to "Stopped phone ring"
            }
            "torch_on" -> setTorch(true)
            "torch_off" -> setTorch(false)
            "open_url" -> openUrl(params?.optString("url") ?: "")
            else -> false to "Unknown mobile action: $action"
        }
    }

    private fun adjustVolume(direction: Int, label: String): Pair<Boolean, String> {
        return try {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            am.adjustStreamVolume(AudioManager.STREAM_MUSIC, direction, AudioManager.FLAG_SHOW_UI)
            true to label
        } catch (e: Exception) {
            false to "Volume error: ${e.message}"
        }
    }

    private fun dispatchMediaKey(keyCode: Int, label: String): Pair<Boolean, String> {
        return try {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            am.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, keyCode))
            am.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, keyCode))
            true to label
        } catch (e: Exception) {
            false to "Media control failed: ${e.message}"
        }
    }

    private fun startRinging(): Pair<Boolean, String> {
        return try {
            stopRinging()
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            val ringtone = RingtoneManager.getRingtone(context, uri)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                ringtone.isLooping = true
            }
            ringtone.audioAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            ringtone.play()
            activeRingtone = ringtone
            vibrateBurst()
            mainHandler.postDelayed(stopRingRunnable, 25_000L)
            true to "Ringing phone alarm (25s or until stopped)"
        } catch (e: Exception) {
            false to "Unable to ring phone: ${e.message}"
        }
    }

    @Suppress("DEPRECATION")
    private fun vibrateBurst() {
        try {
            val effect = VibrationEffect.createWaveform(longArrayOf(0, 400, 250, 400, 250, 400), -1)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
                vm.defaultVibrator.vibrate(effect)
            } else {
                val vib = context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
                vib.vibrate(effect)
            }
        } catch (_: Exception) {
        }
    }

    fun stopRinging() {
        mainHandler.removeCallbacks(stopRingRunnable)
        try {
            activeRingtone?.stop()
        } catch (_: Exception) {
        }
        activeRingtone = null
    }

    private fun setTorch(enabled: Boolean): Pair<Boolean, String> {
        return try {
            val cm = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
            val cameraId = cm.cameraIdList.firstOrNull { id ->
                val chars = cm.getCameraCharacteristics(id)
                val facing = chars.get(CameraCharacteristics.LENS_FACING)
                val hasFlash = chars.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
                hasFlash && facing == CameraCharacteristics.LENS_FACING_BACK
            } ?: cm.cameraIdList.firstOrNull { id ->
                cm.getCameraCharacteristics(id).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            } ?: return false to "No camera flash found on this device"

            cm.setTorchMode(cameraId, enabled)
            torchEnabled = enabled
            true to if (enabled) "Flashlight turned ON" else "Flashlight turned OFF"
        } catch (e: Exception) {
            false to "Flashlight error: ${e.message}"
        }
    }

    private fun openUrl(rawUrl: String): Pair<Boolean, String> {
        val cleaned = rawUrl.trim()
        val parsed = try {
            Uri.parse(cleaned)
        } catch (_: Exception) {
            return false to "Invalid URL"
        }
        val scheme = parsed.scheme?.lowercase()
        if (scheme !in setOf("http", "https") || parsed.host.isNullOrBlank()) {
            return false to "Only http:// and https:// URLs are allowed"
        }
        return try {
            wakeScreen()
            val intent = Intent(Intent.ACTION_VIEW, parsed).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            true to "Opened $cleaned on phone"
        } catch (e: Exception) {
            false to "Failed to open URL: ${e.message}"
        }
    }

    fun setClipboard(text: String) {
        mainHandler.post {
            try {
                val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("WebDesk Remote", text))
            } catch (_: Exception) {
            }
        }
    }

    fun getClipboard(): String {
        var result = ""
        val done = Object()
        if (Looper.myLooper() == Looper.getMainLooper()) {
            val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            return cm.primaryClip?.getItemAt(0)?.coerceToText(context)?.toString() ?: ""
        }
        mainHandler.post {
            try {
                val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                result = cm.primaryClip?.getItemAt(0)?.coerceToText(context)?.toString() ?: ""
            } catch (_: Exception) {
            } finally {
                synchronized(done) { done.notifyAll() }
            }
        }
        synchronized(done) {
            try {
                done.wait(400)
            } catch (_: InterruptedException) {
            }
        }
        return result
    }

    fun saveUploadedFile(safeName: String, bytes: ByteArray): Pair<String, Long> {
        val cleanName = safeName.substringAfterLast('/').substringAfterLast('\\').ifBlank { "upload.bin" }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val resolver = context.contentResolver
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, cleanName)
                put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
                put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/WebDeskRemote")
            }
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("Could not create Downloads file")
            val out: OutputStream = resolver.openOutputStream(uri)
                ?: throw IllegalStateException("Could not open Downloads stream")
            out.use { it.write(bytes) }
            return "Downloads/WebDeskRemote/$cleanName" to bytes.size.toLong()
        }

        val dir = File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "WebDeskRemote")
        dir.mkdirs()
        var dest = File(dir, cleanName)
        if (dest.exists()) {
            val dot = cleanName.lastIndexOf('.')
            val base = if (dot > 0) cleanName.substring(0, dot) else cleanName
            val ext = if (dot > 0) cleanName.substring(dot) else ""
            dest = File(dir, "${base}_${System.currentTimeMillis() % 10000}$ext")
        }
        dest.writeBytes(bytes)
        return dest.absolutePath to bytes.size.toLong()
    }

    fun close() {
        stopRinging()
        if (torchEnabled) {
            setTorch(false)
        }
    }
}
