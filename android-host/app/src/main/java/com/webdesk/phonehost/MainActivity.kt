package com.webdesk.phonehost

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.button.MaterialButton
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.textfield.TextInputEditText
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var prefs: HostPrefs

    private lateinit var tvStatusPill: TextView
    private lateinit var tvRuntimeDetail: TextView
    private lateinit var tvHostMeta: TextView
    private lateinit var tvAccessibilityStatus: TextView
    private lateinit var tvBatteryStatus: TextView
    private lateinit var tvPinStatus: TextView

    private lateinit var etServerUrl: TextInputEditText
    private lateinit var etDeviceName: TextInputEditText
    private lateinit var etWorkspaceToken: TextInputEditText
    private lateinit var etPin: TextInputEditText
    private lateinit var swAutoWake: MaterialSwitch
    private lateinit var swBootStandby: MaterialSwitch

    private val notifPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            refreshPermissionsUi()
            if (!granted) {
                toast("Notifications permission helps keep the background standby reliable.")
            }
        }

    private val mediaProjectionLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == Activity.RESULT_OK && result.data != null) {
                val intent = Intent(this, PhoneHostService::class.java)
                    .setAction(PhoneHostService.ACTION_START_CAST)
                    .putExtra(PhoneHostService.EXTRA_RESULT_CODE, result.resultCode)
                    .putExtra(PhoneHostService.EXTRA_PROJECTION_DATA, result.data)
                ContextCompat.startForegroundService(this, intent)
            } else {
                toast("Screen Cast permission is needed for live mirroring.")
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = HostPrefs(this)

        tvStatusPill = findViewById(R.id.tvStatusPill)
        tvRuntimeDetail = findViewById(R.id.tvRuntimeDetail)
        tvHostMeta = findViewById(R.id.tvHostMeta)
        tvAccessibilityStatus = findViewById(R.id.tvAccessibilityStatus)
        tvBatteryStatus = findViewById(R.id.tvBatteryStatus)
        tvPinStatus = findViewById(R.id.tvPinStatus)

        etServerUrl = findViewById(R.id.etServerUrl)
        etDeviceName = findViewById(R.id.etDeviceName)
        etWorkspaceToken = findViewById(R.id.etWorkspaceToken)
        etPin = findViewById(R.id.etPin)
        swAutoWake = findViewById(R.id.swAutoWake)
        swBootStandby = findViewById(R.id.swBootStandby)

        loadSettingsIntoViews()
        wireButtons()

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                PhoneHostService.state.collect { st ->
                    renderRuntimeState(st)
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        refreshPermissionsUi()
        refreshMetaUi()
    }

    private fun loadSettingsIntoViews() {
        etServerUrl.setText(prefs.rawServerUrl)
        etDeviceName.setText(prefs.hostName)
        etWorkspaceToken.setText(prefs.workspaceToken)
        swAutoWake.isChecked = prefs.autoWakeOnConnect
        swBootStandby.isChecked = prefs.bootControlStandby
        refreshMetaUi()
    }

    private fun wireButtons() {
        findViewById<MaterialButton>(R.id.btnSaveSettings).setOnClickListener {
            saveSettingsFromViews()
        }

        findViewById<MaterialButton>(R.id.btnClearPairings).setOnClickListener {
            prefs.clearPairings()
            refreshMetaUi()
            toast("Cleared all remembered viewer devices.")
        }

        findViewById<MaterialButton>(R.id.btnStartMirrorHost).setOnClickListener {
            if (!saveSettingsFromViews(quiet = true)) return@setOnClickListener
            ensureNotifPermissionIfNeeded()
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            mediaProjectionLauncher.launch(mpm.createScreenCaptureIntent())
        }

        findViewById<MaterialButton>(R.id.btnStartControlOnly).setOnClickListener {
            if (!saveSettingsFromViews(quiet = true)) return@setOnClickListener
            ensureNotifPermissionIfNeeded()
            val intent = Intent(this, PhoneHostService::class.java)
                .setAction(PhoneHostService.ACTION_START_CONTROL_ONLY)
            ContextCompat.startForegroundService(this, intent)
        }

        findViewById<MaterialButton>(R.id.btnStopHost).setOnClickListener {
            val intent = Intent(this, PhoneHostService::class.java)
                .setAction(PhoneHostService.ACTION_STOP)
            startService(intent)
        }

        findViewById<MaterialButton>(R.id.btnOpenViewer).setOnClickListener {
            val url = resolvePortalUrlForViewer() ?: return@setOnClickListener
            startActivity(
                Intent(this, ViewerActivity::class.java)
                    .putExtra(ViewerActivity.EXTRA_URL, url)
            )
        }

        findViewById<MaterialButton>(R.id.btnOpenBrowser).setOnClickListener {
            val url = resolvePortalUrlForViewer() ?: return@setOnClickListener
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        }

        findViewById<MaterialButton>(R.id.btnEnableAccessibility).setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }

        findViewById<MaterialButton>(R.id.btnAppInfo).setOnClickListener {
            val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.fromParts("package", packageName, null)
            }
            startActivity(intent)
        }

        findViewById<MaterialButton>(R.id.btnBatteryExempt).setOnClickListener {
            requestIgnoreBatteryOptimization()
        }

        findViewById<MaterialButton>(R.id.btnNotifPerm).setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                toast("Notifications already allowed on this Android version.")
            }
        }
    }

    private fun resolvePortalUrlForViewer(): String? {
        val raw = etServerUrl.text?.toString().orEmpty().trim()
        if (raw.isBlank()) {
            toast("Enter your Server URL first (e.g. https://your-app.onrender.com).")
            return null
        }
        val ws = HostPrefs.normalizeServerWsUrl(raw)
        val portal = HostPrefs.wsUrlToHttpBase(ws)
        if (portal.isBlank()) {
            toast("Invalid Server URL.")
            return null
        }
        return portal
    }

    private fun saveSettingsFromViews(quiet: Boolean = false): Boolean {
        val rawUrl = etServerUrl.text?.toString().orEmpty().trim()
        val normalized = HostPrefs.normalizeServerWsUrl(rawUrl)
        if (rawUrl.isBlank() || normalized.isBlank()) {
            toast("Enter a valid Server URL (e.g. https://your-app.onrender.com or http://192.168.1.10:8080).")
            return false
        }

        val pinText = etPin.text?.toString().orEmpty()
        if (pinText.isNotEmpty() && pinText.length < 6) {
            toast("Access PIN must be at least 6 characters or digits.")
            return false
        }
        if (pinText.isEmpty() && !prefs.hasPinConfigured()) {
            toast("Please set an Access PIN (at least 6 digits/characters).")
            return false
        }

        prefs.rawServerUrl = rawUrl
        val name = etDeviceName.text?.toString().orEmpty().trim()
        if (name.isNotBlank()) prefs.hostName = name
        prefs.workspaceToken = etWorkspaceToken.text?.toString().orEmpty().trim()
        prefs.autoWakeOnConnect = swAutoWake.isChecked
        prefs.bootControlStandby = swBootStandby.isChecked

        if (pinText.isNotEmpty()) {
            prefs.setPin(pinText)
            etPin.setText("")
        }
        refreshMetaUi()
        if (!quiet) {
            toast("Saved! Ready for $normalized")
        }
        return true
    }

    private fun ensureNotifPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) {
                notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    @SuppressLint("BatteryLife")
    private fun requestIgnoreBatteryOptimization() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            toast("Unrestricted background battery is already enabled.")
            return
        }
        try {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:$packageName")
            }
            startActivity(intent)
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun refreshPermissionsUi() {
        val accOk = WebDeskAccessibilityService.isEnabled()
        tvAccessibilityStatus.text = if (accOk) {
            "Touch & Navigation Control: Enabled ✓"
        } else {
            "Touch & Navigation Control: Disabled (needed to tap/swipe remotely)"
        }
        tvAccessibilityStatus.setTextColor(
            ContextCompat.getColor(this, if (accOk) R.color.accent_green else R.color.accent_amber)
        )

        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val batOk = pm.isIgnoringBatteryOptimizations(packageName)
        tvBatteryStatus.text = if (batOk) {
            "Background Battery: Unrestricted ✓"
        } else {
            "Background Battery: Optimized (allow unrestricted for 24/7 standby)"
        }
        tvBatteryStatus.setTextColor(
            ContextCompat.getColor(this, if (batOk) R.color.accent_green else R.color.text_muted)
        )
    }

    private fun refreshMetaUi() {
        val id = prefs.getOrCreateHostId()
        val paired = prefs.pairedCount()
        tvHostMeta.text = "Host ID: $id  •  Paired viewers: $paired"
        tvPinStatus.text = if (prefs.hasPinConfigured()) {
            "Access PIN configured ✓ (stored locally as salted SRP-6a verifier)"
        } else {
            "Access PIN not configured yet"
        }
        tvPinStatus.setTextColor(
            ContextCompat.getColor(
                this,
                if (prefs.hasPinConfigured()) R.color.accent_green else R.color.accent_amber
            )
        )
    }

    private fun renderRuntimeState(st: HostRuntimeState) {
        tvRuntimeDetail.text = st.statusText
        when {
            st.activeViewer != null -> {
                tvStatusPill.text = "Mirroring"
                tvStatusPill.setTextColor(ContextCompat.getColor(this, R.color.accent_amber))
            }
            st.connected -> {
                tvStatusPill.text = if (st.castReady) "Online • Cast" else "Online • Control"
                tvStatusPill.setTextColor(ContextCompat.getColor(this, R.color.accent_green))
            }
            st.running -> {
                tvStatusPill.text = "Connecting"
                tvStatusPill.setTextColor(ContextCompat.getColor(this, R.color.accent_blue))
            }
            else -> {
                tvStatusPill.text = "Stopped"
                tvStatusPill.setTextColor(ContextCompat.getColor(this, R.color.text_muted))
            }
        }
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }
}
