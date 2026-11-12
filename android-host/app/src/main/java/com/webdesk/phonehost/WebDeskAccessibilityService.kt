package com.webdesk.phonehost

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Path
import android.graphics.Point
import android.graphics.PointF
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

class WebDeskAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Events not needed continuously; we query focus on demand.
    }

    override fun onInterrupt() {
        // No-op
    }

    override fun onDestroy() {
        if (instance === this) {
            instance = null
        }
        super.onDestroy()
    }

    fun getScreenSize(): Pair<Int, Int> = getRealDisplaySize(this)

    fun performTap(nx: Float, ny: Float): Boolean {
        val (w, h) = getScreenSize()
        val x = (nx.coerceIn(0f, 1f) * w).coerceIn(1f, (w - 1).coerceAtLeast(1).toFloat())
        val y = (ny.coerceIn(0f, 1f) * h).coerceIn(1f, (h - 1).coerceAtLeast(1).toFloat())
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 45L)
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun performLongPress(nx: Float, ny: Float): Boolean {
        val (w, h) = getScreenSize()
        val x = (nx.coerceIn(0f, 1f) * w).coerceIn(1f, (w - 1).coerceAtLeast(1).toFloat())
        val y = (ny.coerceIn(0f, 1f) * h).coerceIn(1f, (h - 1).coerceAtLeast(1).toFloat())
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 650L)
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun performSwipe(normPoints: List<PointF>, durationMs: Long): Boolean {
        if (normPoints.size < 2) return false
        val (w, h) = getScreenSize()
        val maxX = (w - 1).coerceAtLeast(1).toFloat()
        val maxY = (h - 1).coerceAtLeast(1).toFloat()

        val path = Path()
        val first = normPoints.first()
        path.moveTo(
            (first.x.coerceIn(0f, 1f) * w).coerceIn(1f, maxX),
            (first.y.coerceIn(0f, 1f) * h).coerceIn(1f, maxY)
        )
        for (i in 1 until normPoints.size) {
            val p = normPoints[i]
            path.lineTo(
                (p.x.coerceIn(0f, 1f) * w).coerceIn(1f, maxX),
                (p.y.coerceIn(0f, 1f) * h).coerceIn(1f, maxY)
            )
        }
        val clampedDuration = durationMs.coerceIn(80L, 1200L)
        val stroke = GestureDescription.StrokeDescription(path, 0L, clampedDuration)
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun performWheelScroll(nx: Float, ny: Float, dx: Float, dy: Float): Boolean {
        val startX = nx.coerceIn(0.1f, 0.9f)
        val startY = ny.coerceIn(0.15f, 0.85f)
        // Wheel dy > 0 scrolls down -> swipe finger upward
        val shiftY = (-dy * 0.12f).coerceIn(-0.32f, 0.32f)
        val shiftX = (-dx * 0.12f).coerceIn(-0.32f, 0.32f)
        val endX = (startX + shiftX).coerceIn(0.05f, 0.95f)
        val endY = (startY + shiftY).coerceIn(0.05f, 0.95f)
        if (kotlin.math.abs(endX - startX) < 0.01f && kotlin.math.abs(endY - startY) < 0.01f) {
            return false
        }
        return performSwipe(listOf(PointF(startX, startY), PointF(endX, endY)), 180L)
    }

    fun performCombo(combo: String): Boolean {
        return when (combo) {
            "android_back" -> performGlobalAction(GLOBAL_ACTION_BACK)
            "android_home" -> performGlobalAction(GLOBAL_ACTION_HOME)
            "android_recents" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
            "android_notifications" -> performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)
            "android_quick_settings" -> performGlobalAction(GLOBAL_ACTION_QUICK_SETTINGS)
            "android_power_dialog" -> performGlobalAction(GLOBAL_ACTION_POWER_DIALOG)
            "android_lock_screen", "lock_screen" -> {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN)
                } else {
                    false
                }
            }
            "android_screenshot" -> {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    performGlobalAction(GLOBAL_ACTION_TAKE_SCREENSHOT)
                } else {
                    false
                }
            }
            else -> false
        }
    }

    fun inputText(text: String): Boolean {
        if (text.isEmpty()) return true
        val node = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        if (node != null) {
            try {
                if (node.isEditable) {
                    val isHint = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && node.isShowingHintText
                    val existing = if (isHint) "" else (node.text?.toString() ?: "")
                    val args = Bundle().apply {
                        putCharSequence(
                            AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            existing + text
                        )
                    }
                    if (node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
                        return true
                    }
                }
                val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("WebDesk", text))
                if (node.performAction(AccessibilityNodeInfo.ACTION_PASTE)) {
                    return true
                }
            } finally {
                @Suppress("DEPRECATION")
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                    node.recycle()
                }
            }
        }
        return false
    }

    fun handleKeyEvent(code: String, key: String, down: Boolean) {
        if (!down) return
        when {
            code == "Escape" || key == "Escape" -> performGlobalAction(GLOBAL_ACTION_BACK)
            code == "Backspace" || key == "Backspace" -> deleteLastChar()
            code == "Enter" || key == "Enter" -> submitOrNewline()
            key.length == 1 -> inputText(key)
        }
    }

    private fun deleteLastChar() {
        val node = findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return
        try {
            if (!node.isEditable) return
            val isHint = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && node.isShowingHintText
            val existing = if (isHint) "" else (node.text?.toString() ?: "")
            if (existing.isEmpty()) return
            val args = Bundle().apply {
                putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                    existing.dropLast(1)
                )
            }
            node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        } finally {
            @Suppress("DEPRECATION")
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                node.recycle()
            }
        }
    }

    private fun submitOrNewline() {
        val node = findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                if (node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) {
                    return
                }
            }
            if (node.isEditable) {
                val isHint = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && node.isShowingHintText
                val existing = if (isHint) "" else (node.text?.toString() ?: "")
                val args = Bundle().apply {
                    putCharSequence(
                        AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                        existing + "\n"
                    )
                }
                node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            }
        } finally {
            @Suppress("DEPRECATION")
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                node.recycle()
            }
        }
    }

    companion object {
        @Volatile
        var instance: WebDeskAccessibilityService? = null
            private set

        fun isEnabled(): Boolean = instance != null

        @Suppress("DEPRECATION")
        fun getRealDisplaySize(context: Context): Pair<Int, Int> {
            val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
            return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val bounds = wm.maximumWindowMetrics.bounds
                bounds.width().coerceAtLeast(1) to bounds.height().coerceAtLeast(1)
            } else {
                val pt = Point()
                wm.defaultDisplay.getRealSize(pt)
                pt.x.coerceAtLeast(1) to pt.y.coerceAtLeast(1)
            }
        }
    }
}
