package com.webdesk.phonehost

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_MY_PACKAGE_REPLACED) {
            return
        }
        val prefs = HostPrefs(context)
        if (!prefs.bootControlStandby || !prefs.hasPinConfigured() || prefs.getNormalizedWsUrl().isBlank()) {
            return
        }
        try {
            val svcIntent = Intent(context, PhoneHostService::class.java)
                .setAction(PhoneHostService.ACTION_START_CONTROL_ONLY)
            ContextCompat.startForegroundService(context, svcIntent)
        } catch (_: Exception) {
            // Android 15+ may block dataSync FGS auto-start on boot depending on exemptions
        }
    }
}
