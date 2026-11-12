package com.webdesk.phonehost

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.HandlerThread
import android.view.Display
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean

data class JpegFrame(val bytes: ByteArray, val width: Int, val height: Int)

data class QualityProfile(val maxDim: Int, val fps: Int, val jpegQuality: Int)

/**
 * Manages a single MediaProjection + VirtualDisplay across rotations and multiple viewer
 * sessions, capturing 0 frames while idle so standby battery overhead stays minimal.
 */
class ScreenCaptureEngine(
    private val context: Context,
    private val projection: MediaProjection,
    private val onProjectionStopped: () -> Unit,
    private val onDimensionsChanged: (Int, Int) -> Unit,
) {
    private val handlerThread = HandlerThread("WebDeskScreenCapture").apply { start() }
    private val handler = Handler(handlerThread.looper)
    private val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager

    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var reusableBitmap: Bitmap? = null

    private var realWidth = 1080
    private var realHeight = 2400
    private var densityDpi = 420
    private var capWidth = 540
    private var capHeight = 1200

    @Volatile
    private var streamingEnabled = false

    @Volatile
    private var qualityProfile = PROFILE_AUTO

    @Volatile
    private var frameListener: ((JpegFrame) -> Unit)? = null

    @Volatile
    private var pendingSnapshot: CompletableDeferred<JpegFrame?>? = null

    @Volatile
    private var lastFrameBytes: JpegFrame? = null

    private var lastEncodedAtMs = 0L
    private val closed = AtomicBoolean(false)

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            close()
            onProjectionStopped()
        }
    }

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) = Unit
        override fun onDisplayRemoved(displayId: Int) = Unit
        override fun onDisplayChanged(displayId: Int) {
            if (displayId == Display.DEFAULT_DISPLAY && !closed.get()) {
                checkRotationAndResize()
            }
        }
    }

    init {
        refreshDisplayMetrics()
        val (cw, ch) = computeScaledSize(realWidth, realHeight, qualityProfile.maxDim)
        capWidth = cw
        capHeight = ch

        projection.registerCallback(projectionCallback, handler)
        displayManager.registerDisplayListener(displayListener, handler)

        val reader = createReader(capWidth, capHeight)
        imageReader = reader
        virtualDisplay = projection.createVirtualDisplay(
            "WebDeskPhoneCast",
            capWidth,
            capHeight,
            densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface,
            null,
            handler
        )
    }

    fun isActive(): Boolean = !closed.get()

    fun getRealSize(): Pair<Int, Int> = realWidth to realHeight

    fun setQualityPreset(preset: String) {
        val next = when (preset) {
            "ultra" -> PROFILE_ULTRA
            "balanced" -> PROFILE_BALANCED
            "low" -> PROFILE_LOW
            else -> PROFILE_AUTO
        }
        val dimChanged = next.maxDim != qualityProfile.maxDim
        qualityProfile = next
        if (dimChanged && !closed.get()) {
            handler.post { recreateReaderForCurrentMetrics() }
        }
    }

    fun startStreaming(listener: (JpegFrame) -> Unit) {
        frameListener = listener
        streamingEnabled = true
        lastEncodedAtMs = 0L
        // Emit cached frame immediately if we have one so viewer renders right away
        lastFrameBytes?.let { cached ->
            handler.post { listener(cached) }
        }
    }

    fun stopStreaming() {
        streamingEnabled = false
        frameListener = null
    }

    suspend fun captureSingleSnapshot(timeoutMs: Long = 1500L): JpegFrame? {
        if (closed.get()) return null
        val deferred = CompletableDeferred<JpegFrame?>()
        pendingSnapshot = deferred
        val fresh = withTimeoutOrNull(timeoutMs) { deferred.await() }
        if (pendingSnapshot === deferred) {
            pendingSnapshot = null
        }
        return fresh ?: lastFrameBytes
    }

    private fun refreshDisplayMetrics() {
        val (rw, rh) = WebDeskAccessibilityService.getRealDisplaySize(context)
        realWidth = rw
        realHeight = rh
        densityDpi = context.resources.displayMetrics.densityDpi.coerceAtLeast(160)
    }

    private fun checkRotationAndResize() {
        val prevW = realWidth
        val prevH = realHeight
        refreshDisplayMetrics()
        if (realWidth != prevW || realHeight != prevH) {
            recreateReaderForCurrentMetrics()
            onDimensionsChanged(realWidth, realHeight)
        }
    }

    private fun recreateReaderForCurrentMetrics() {
        if (closed.get()) return
        val (cw, ch) = computeScaledSize(realWidth, realHeight, qualityProfile.maxDim)
        if (cw == capWidth && ch == capHeight && imageReader != null) return
        capWidth = cw
        capHeight = ch

        val oldReader = imageReader
        val newReader = createReader(capWidth, capHeight)
        imageReader = newReader
        virtualDisplay?.resize(capWidth, capHeight, densityDpi)
        virtualDisplay?.setSurface(newReader.surface)
        oldReader?.setOnImageAvailableListener(null, null)
        oldReader?.close()
        reusableBitmap?.recycle()
        reusableBitmap = null
    }

    private fun createReader(w: Int, h: Int): ImageReader {
        val reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        reader.setOnImageAvailableListener({ r -> onImageAvailable(r) }, handler)
        return reader
    }

    private fun onImageAvailable(reader: ImageReader) {
        if (closed.get()) return
        val image = try {
            reader.acquireLatestImage()
        } catch (_: Exception) {
            null
        } ?: return

        try {
            val snap = pendingSnapshot
            val wantsFrame = streamingEnabled || snap != null
            if (!wantsFrame) {
                return
            }

            val now = System.currentTimeMillis()
            val minIntervalMs = (1000L / qualityProfile.fps.coerceAtLeast(1))
            if (snap == null && (now - lastEncodedAtMs) < minIntervalMs) {
                return
            }

            val plane = image.planes[0]
            val buffer = plane.buffer
            val pixelStride = plane.pixelStride
            val rowStride = plane.rowStride
            val width = image.width
            val height = image.height
            val rowPadding = rowStride - pixelStride * width
            val bmpWidth = width + (rowPadding / pixelStride.coerceAtLeast(1))

            var bmp = reusableBitmap
            if (bmp == null || bmp.width != bmpWidth || bmp.height != height) {
                bmp?.recycle()
                bmp = Bitmap.createBitmap(bmpWidth, height, Bitmap.Config.ARGB_8888)
                reusableBitmap = bmp
            }
            bmp.copyPixelsFromBuffer(buffer)

            val cropped = if (rowPadding > 0) {
                Bitmap.createBitmap(bmp, 0, 0, width, height)
            } else {
                bmp
            }

            val baos = ByteArrayOutputStream(48 * 1024)
            cropped.compress(Bitmap.CompressFormat.JPEG, qualityProfile.jpegQuality, baos)
            if (cropped !== bmp) {
                cropped.recycle()
            }

            val frame = JpegFrame(baos.toByteArray(), width, height)
            lastFrameBytes = frame
            lastEncodedAtMs = now

            if (snap != null) {
                pendingSnapshot = null
                snap.complete(frame)
            }
            if (streamingEnabled) {
                frameListener?.invoke(frame)
            }
        } catch (_: Exception) {
            // Ignore transient frame failures during resize/shutdown
        } finally {
            image.close()
        }
    }

    fun close() {
        if (!closed.compareAndSet(false, true)) return
        streamingEnabled = false
        frameListener = null
        pendingSnapshot?.complete(null)
        pendingSnapshot = null

        handler.post {
            try {
                displayManager.unregisterDisplayListener(displayListener)
            } catch (_: Exception) {}
            try {
                projection.unregisterCallback(projectionCallback)
            } catch (_: Exception) {}
            try {
                virtualDisplay?.release()
            } catch (_: Exception) {}
            virtualDisplay = null
            try {
                imageReader?.setOnImageAvailableListener(null, null)
                imageReader?.close()
            } catch (_: Exception) {}
            imageReader = null
            reusableBitmap?.recycle()
            reusableBitmap = null
            try {
                projection.stop()
            } catch (_: Exception) {}
            handlerThread.quitSafely()
        }
    }

    companion object {
        val PROFILE_ULTRA = QualityProfile(maxDim = 1440, fps = 20, jpegQuality = 82)
        val PROFILE_AUTO = QualityProfile(maxDim = 1120, fps = 18, jpegQuality = 74)
        val PROFILE_BALANCED = QualityProfile(maxDim = 860, fps = 15, jpegQuality = 66)
        val PROFILE_LOW = QualityProfile(maxDim = 640, fps = 12, jpegQuality = 56)

        fun computeScaledSize(realW: Int, realH: Int, maxDim: Int): Pair<Int, Int> {
            val longest = maxOf(realW, realH).coerceAtLeast(1)
            if (longest <= maxDim) {
                return even(realW) to even(realH)
            }
            val scale = maxDim.toFloat() / longest.toFloat()
            val w = even((realW * scale).toInt().coerceAtLeast(240))
            val h = even((realH * scale).toInt().coerceAtLeast(240))
            return w to h
        }

        private fun even(v: Int): Int = if (v % 2 == 0) v else v - 1
    }
}
