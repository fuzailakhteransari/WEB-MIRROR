package com.webdesk.phonehost

import android.annotation.SuppressLint
import android.net.Uri
import android.os.Bundle
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton

class ViewerActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var fileUploadCallback: ValueCallback<Array<Uri>>? = null

    private val pickFileLauncher =
        registerForActivityResult(ActivityResultContracts.GetMultipleContents()) { uris ->
            fileUploadCallback?.onReceiveValue(uris.toTypedArray())
            fileUploadCallback = null
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_viewer)

        val targetUrl = intent.getStringExtra(EXTRA_URL).orEmpty().ifBlank { "about:blank" }
        findViewById<TextView>(R.id.tvViewerTitle).text = targetUrl
        webView = findViewById(R.id.webView)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            useWideViewPort = true
            loadWithOverviewMode = true
        }
        webView.webViewClient = WebViewClient()
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                fileUploadCallback?.onReceiveValue(null)
                fileUploadCallback = callback
                pickFileLauncher.launch("*/*")
                return true
            }
        }
        webView.loadUrl(targetUrl)

        findViewById<MaterialButton>(R.id.btnViewerReload).setOnClickListener {
            webView.reload()
        }
        findViewById<MaterialButton>(R.id.btnViewerClose).setOnClickListener {
            finish()
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    finish()
                }
            }
        })
    }

    override fun onDestroy() {
        fileUploadCallback?.onReceiveValue(null)
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_URL = "portal_url"
    }
}
