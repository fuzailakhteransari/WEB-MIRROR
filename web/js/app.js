import { InputCaptureController, getVideoContentRect } from "./input_capture.js?v=4";
import { WebRTCViewerClient } from "./webrtc_client.js?v=4";

const PAIRING_STORAGE_KEY = "webdesk_pairings_v1";
const WORKSPACE_TOKEN_KEY = "webdesk_workspace_token";

const els = {
  portalHeader: document.getElementById("portal-header"),
  portalView: document.getElementById("portal-view"),
  sessionView: document.getElementById("session-view"),
  tabAccess: document.getElementById("tab-access"),
  tabSupport: document.getElementById("tab-support"),
  panelAccess: document.getElementById("panel-access"),
  panelSupport: document.getElementById("panel-support"),
  serverStatusPill: document.getElementById("server-status-pill"),
  btnWorkspaceToken: document.getElementById("btn-workspace-token"),
  btnRefreshHosts: document.getElementById("btn-refresh-hosts"),
  tunnelBanner: document.getElementById("tunnel-banner"),
  tunnelUrlLink: document.getElementById("tunnel-url-link"),
  tunnelQrImg: document.getElementById("tunnel-qr-img"),
  btnCopyTunnel: document.getElementById("btn-copy-tunnel"),
  hostsGrid: document.getElementById("hosts-grid"),
  hostsEmpty: document.getElementById("hosts-empty"),
  supportForm: document.getElementById("support-form"),
  supportCodeInput: document.getElementById("support-code-input"),
  supportError: document.getElementById("support-error"),
  pinModal: document.getElementById("pin-modal"),
  pinModalTitle: document.getElementById("pin-modal-title"),
  pinForm: document.getElementById("pin-form"),
  pinInput: document.getElementById("pin-input"),
  rememberPinCheckbox: document.getElementById("remember-pin-checkbox"),
  pinError: document.getElementById("pin-error"),
  btnPinCancel: document.getElementById("btn-pin-cancel"),
  tokenModal: document.getElementById("token-modal"),
  tokenForm: document.getElementById("token-form"),
  workspaceTokenInput: document.getElementById("workspace-token-input"),
  tokenError: document.getElementById("token-error"),
  btnTokenCancel: document.getElementById("btn-token-cancel"),
  stage: document.getElementById("stage"),
  remoteVideo: document.getElementById("remote-video"),
  fallbackCanvas: document.getElementById("fallback-canvas"),
  trackpadCursor: document.getElementById("trackpad-cursor"),
  mobileKbInput: document.getElementById("mobile-kb-input"),
  sessionOverlay: document.getElementById("session-overlay"),
  sessionStatusText: document.getElementById("session-status-text"),
  btnCancelConnecting: document.getElementById("btn-cancel-connecting"),
  hudHostName: document.getElementById("hud-host-name"),
  hudTransport: document.getElementById("hud-transport"),
  hudMetrics: document.getElementById("hud-metrics"),
  btnToggleSidebar: document.getElementById("btn-toggle-sidebar"),
  sessionSidebar: document.getElementById("session-sidebar"),
  sidebarHostTitle: document.getElementById("sidebar-host-title"),
  btnDisconnect: document.getElementById("btn-disconnect"),
  btnFullscreen: document.getElementById("btn-fullscreen"),
  btnFitMode: document.getElementById("btn-fit-mode"),
  btnToggleInput: document.getElementById("btn-toggle-input"),
  selectMonitor: document.getElementById("select-monitor"),
  selectInputMode: document.getElementById("select-input-mode"),
  selectQuality: document.getElementById("select-quality"),
  clipboardBox: document.getElementById("clipboard-box"),
  btnClipboardSend: document.getElementById("btn-clipboard-send"),
  btnClipboardPull: document.getElementById("btn-clipboard-pull"),
  clipboardStatus: document.getElementById("clipboard-status"),
  pasteTextInput: document.getElementById("paste-text-input"),
  btnPasteText: document.getElementById("btn-paste-text"),
  btnQuickKeyboard: document.getElementById("btn-quick-keyboard"),
  btnQuickMode: document.getElementById("btn-quick-mode"),
  btnQuickFullscreen: document.getElementById("btn-quick-fullscreen"),
  btnQuickFit: document.getElementById("btn-quick-fit"),
  btnQuickHudActions: document.getElementById("btn-quick-hud-actions"),
  btnQuickSidebar: document.getElementById("btn-quick-sidebar"),
  btnQuickDisconnect: document.getElementById("btn-quick-disconnect"),
  fileInput: document.getElementById("file-input"),
  btnUploadFile: document.getElementById("btn-upload-file"),
  fileProgress: document.getElementById("file-progress"),
  quickActionsView: document.getElementById("quick-actions-view"),
  qaHostTitle: document.getElementById("qa-host-title"),
  qaModeSub: document.getElementById("qa-mode-sub"),
  qaBtnRefresh: document.getElementById("qa-btn-refresh"),
  qaBtnMirror: document.getElementById("qa-btn-mirror"),
  qaBtnDisconnect: document.getElementById("qa-btn-disconnect"),
  qaFeedback: document.getElementById("qa-feedback"),
  qaStatCpu: document.getElementById("qa-stat-cpu"),
  qaStatRam: document.getElementById("qa-stat-ram"),
  qaStatBattery: document.getElementById("qa-stat-battery"),
  qaStatLock: document.getElementById("qa-stat-lock"),
  qaStatAwake: document.getElementById("qa-stat-awake"),
  qaUrlInput: document.getElementById("qa-url-input"),
  qaBtnOpenUrl: document.getElementById("qa-btn-open-url"),
  qaBtnSnapshot: document.getElementById("qa-btn-snapshot"),
  qaSnapshotImg: document.getElementById("qa-snapshot-img"),
  qaClipInput: document.getElementById("qa-clip-input"),
  qaBtnClipSend: document.getElementById("qa-btn-clip-send"),
  qaBtnClipPull: document.getElementById("qa-btn-clip-pull"),
  qaFileInput: document.getElementById("qa-file-input"),
  qaBtnUpload: document.getElementById("qa-btn-upload"),
  qaFileProgress: document.getElementById("qa-file-progress"),
};

let activeClient = null;
let inputController = null;
let pendingHost = null;
let pendingControlOnly = false;
let activeSessionOpts = null;
let activeControlOnly = false;
let workspaceTokenRequired = false;

function getWorkspaceToken() {
  return localStorage.getItem(WORKSPACE_TOKEN_KEY) || "";
}

function getPairings() {
  try {
    return JSON.parse(localStorage.getItem(PAIRING_STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function savePairing(hostId, pairingData) {
  const map = getPairings();
  map[hostId] = pairingData;
  localStorage.setItem(PAIRING_STORAGE_KEY, JSON.stringify(map));
}

function clearPairing(hostId) {
  const map = getPairings();
  delete map[hostId];
  localStorage.setItem(PAIRING_STORAGE_KEY, JSON.stringify(map));
}

async function apiFetch(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const token = getWorkspaceToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...opts, headers });
  if (resp.status === 401) {
    els.tokenModal.classList.remove("hidden");
    throw new Error("Workspace access token required");
  }
  return resp;
}

function formatLastSeen(ts) {
  if (!ts) return "Never";
  const diff = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (diff < 10) return "Just now";
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return new Date(ts * 1000).toLocaleTimeString();
}

async function refreshHealthAndHosts() {
  try {
    const authResp = await fetch("/api/auth/status");
    const authJson = await authResp.json();
    workspaceTokenRequired = Boolean(authJson.workspace_token_required);
    els.btnWorkspaceToken.classList.toggle("hidden", !workspaceTokenRequired);

    const healthResp = await fetch("/api/health");
    const health = await healthResp.json();
    els.serverStatusPill.textContent = `Server OK • ${health.online_hosts} online`;
    els.serverStatusPill.className = "pill pill-online";

    const hostsResp = await apiFetch("/api/hosts");
    const data = await hostsResp.json();
    renderHosts(data.hosts || []);

    try {
      const tunResp = await apiFetch("/api/tunnel-url");
      const tun = await tunResp.json();
      if (tun.url) {
        els.tunnelUrlLink.href = tun.url;
        els.tunnelUrlLink.textContent = tun.url;
        const token = getWorkspaceToken();
        const qrPath = `/api/qr?url=${encodeURIComponent(tun.url)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
        if (els.tunnelQrImg.getAttribute("src") !== qrPath) {
          els.tunnelQrImg.setAttribute("src", qrPath);
        }
        els.tunnelBanner.classList.remove("hidden");
      } else {
        els.tunnelBanner.classList.add("hidden");
      }
    } catch {
      els.tunnelBanner.classList.add("hidden");
    }
  } catch (err) {
    els.serverStatusPill.textContent = "Server error / Auth needed";
    els.serverStatusPill.className = "pill pill-error";
  }
}

function renderHosts(hosts) {
  els.hostsGrid.innerHTML = "";
  els.hostsEmpty.classList.toggle("hidden", hosts.length > 0);
  const pairings = getPairings();

  for (const h of hosts) {
    const card = document.createElement("div");
    card.className = "host-card";
    card.dataset.hostId = h.host_id;

    const paired = Boolean(pairings[h.host_id]);
    const monSummary =
      h.monitors && h.monitors.length
        ? `${h.monitors.length} display${h.monitors.length > 1 ? "s" : ""} (${h.monitors[0].width}x${h.monitors[0].height})`
        : h.platform;

    card.innerHTML = `
      <div>
        <div class="host-top">
          <div>
            <div class="host-name"></div>
            <div class="muted small">${monSummary} • Last seen ${formatLastSeen(h.last_seen)}</div>
          </div>
          <span class="status-badge status-${h.status}">
            <span class="status-dot"></span>${h.status}
          </span>
        </div>
      </div>
      <div class="host-actions">
        <button class="btn btn-primary btn-connect" ${h.status !== "online" ? "disabled" : ""}>
          ${paired ? "Connect (Paired)" : "Connect"}
        </button>
        <button class="btn btn-secondary btn-quick-actions" ${h.status !== "online" ? "disabled" : ""}>
          Quick Actions
        </button>
        ${paired ? '<button class="btn btn-ghost btn-sm btn-forget">Forget PIN</button>' : ""}
        ${h.status === "offline" ? '<button class="btn btn-ghost btn-sm btn-remove">Remove</button>' : ""}
      </div>
    `;
    card.querySelector(".host-name").textContent = h.name;

    card.querySelector(".btn-connect").addEventListener("click", () => {
      onHostClicked(h, false);
    });
    card.querySelector(".btn-quick-actions").addEventListener("click", () => {
      onHostClicked(h, true);
    });
    const forgetBtn = card.querySelector(".btn-forget");
    if (forgetBtn) {
      forgetBtn.addEventListener("click", () => {
        clearPairing(h.host_id);
        refreshHealthAndHosts();
      });
    }
    const removeBtn = card.querySelector(".btn-remove");
    if (removeBtn) {
      removeBtn.addEventListener("click", async () => {
        await apiFetch(`/api/hosts/${encodeURIComponent(h.host_id)}`, { method: "DELETE" });
        refreshHealthAndHosts();
      });
    }

    els.hostsGrid.appendChild(card);
  }
}

function onHostClicked(host, controlOnly = false) {
  pendingHost = host;
  pendingControlOnly = Boolean(controlOnly);
  const pairing = getPairings()[host.host_id];
  if (pairing && pairing.pairing_id && pairing.pairing_secret) {
    startSession({
      mode: "me2me",
      hostId: host.host_id,
      hostName: host.name,
      pairing,
      controlOnly: pendingControlOnly,
    });
    return;
  }
  els.pinModalTitle.textContent = pendingControlOnly
    ? `Quick Actions: ${host.name}`
    : `Connect to ${host.name}`;
  els.pinError.classList.add("hidden");
  els.pinInput.value = "";
  els.pinModal.classList.remove("hidden");
  els.pinInput.focus();
}

function buildWsClientUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const token = getWorkspaceToken();
  const base = `${proto}//${location.host}/ws/client`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

function renderSystemStatus(st) {
  if (!st || typeof st !== "object") return;
  if (typeof st.cpu_percent === "number") {
    els.qaStatCpu.textContent = `CPU: ${st.cpu_percent}%`;
  }
  if (typeof st.memory_used_gb === "number") {
    els.qaStatRam.textContent = `RAM: ${st.memory_used_gb}/${st.memory_total_gb} GB (${st.memory_percent}%)`;
  }
  if (typeof st.battery_percent === "number") {
    const chg = st.power_plugged ? "⚡" : "";
    els.qaStatBattery.textContent = `Battery: ${st.battery_percent}% ${chg}`.trim();
  } else {
    els.qaStatBattery.textContent = "Power: AC / Desktop";
  }
  if (st.windows_locked) {
    els.qaStatLock.textContent = "Lock: Windows Locked";
    els.qaStatLock.className = "pill pill-error";
  } else if (st.privacy_lock_active) {
    els.qaStatLock.textContent = "Lock: Privacy Shield";
    els.qaStatLock.className = "pill pill-error";
  } else {
    els.qaStatLock.textContent = "Lock: Unlocked";
    els.qaStatLock.className = "pill pill-online";
  }
  els.qaStatAwake.textContent = st.keep_awake ? "Keep Awake: ON" : "Keep Awake: Off";
  els.qaStatAwake.className = st.keep_awake ? "pill pill-online" : "pill";
}

function showQaFeedback(message, ok = true) {
  els.qaFeedback.textContent = message;
  els.qaFeedback.classList.remove("hidden", "qa-ok", "qa-err");
  els.qaFeedback.classList.add(ok ? "qa-ok" : "qa-err");
}

function openQuickActionsPanel({ hostName, controlOnly }) {
  activeControlOnly = Boolean(controlOnly);
  els.qaHostTitle.textContent = `${hostName || "Remote PC"} — Quick Actions`;
  els.qaModeSub.textContent = activeControlOnly
    ? "Direct encrypted PC commands (zero video bandwidth)"
    : "Live mirroring active in background";
  els.qaBtnMirror.textContent = activeControlOnly ? "Screen Mirror" : "Back to Mirror";
  els.quickActionsView.classList.remove("hidden");
  if (activeClient) activeClient.requestSystemStatus();
}

async function startSession({ mode, hostId, hostName, supportCode, pin, pairing, rememberDevice, controlOnly = false }) {
  if (activeClient) activeClient.close();
  activeSessionOpts = { mode, hostId, hostName, supportCode, pin, pairing, rememberDevice };
  activeControlOnly = Boolean(controlOnly);

  els.portalHeader.classList.add("hidden");
  els.portalView.classList.add("hidden");
  els.quickActionsView.classList.add("hidden");
  els.sessionView.classList.remove("hidden");
  els.sessionOverlay.classList.remove("hidden");
  els.sessionStatusText.textContent = "Fetching ICE config...";
  els.hudHostName.textContent = hostName || "Remote PC";
  els.sidebarHostTitle.textContent = hostName || "Remote Session";
  els.hudTransport.textContent = controlOnly ? "Quick Actions" : "Negotiating";
  els.hudMetrics.textContent = "-- ms | -- fps";
  els.sessionSidebar.classList.remove("open");
  els.btnToggleSidebar.classList.remove("open");
  els.btnToggleSidebar.textContent = "‹";

  els.remoteVideo.classList.remove("hidden");
  els.fallbackCanvas.classList.add("hidden");

  let iceServers = [];
  if (!controlOnly) {
    try {
      const iceResp = await apiFetch("/api/ice-config");
      const iceJson = await iceResp.json();
      iceServers = iceJson.iceServers || [];
    } catch (err) {
      showPortalWithError(err.message);
      return;
    }
  }

  activeClient = new WebRTCViewerClient({
    wsUrl: buildWsClientUrl(),
    iceServers,
    videoEl: els.remoteVideo,
    canvasEl: els.fallbackCanvas,
    onStatus: (text) => {
      els.sessionStatusText.textContent = text;
    },
    onReady: (info) => {
      els.sessionOverlay.classList.add("hidden");
      if (info.control_only) {
        els.sessionView.classList.add("hidden");
        renderSystemStatus(info.system_status);
        openQuickActionsPanel({ hostName, controlOnly: true });
        return;
      }

      const isFallback = Boolean(info.wsFallback);
      els.remoteVideo.classList.toggle("hidden", isFallback);
      els.fallbackCanvas.classList.toggle("hidden", !isFallback);
      const activeMediaEl = isFallback ? els.fallbackCanvas : els.remoteVideo;
      if (isFallback) {
        els.hudTransport.textContent = "WSS Relay";
      }

      populateMonitors(info.monitors || [], info.active_monitor);
      if (inputController) inputController.detach();
      inputController = new InputCaptureController({
        stageEl: els.stage,
        videoEl: activeMediaEl,
        sendInput: (ev) => activeClient && activeClient.sendInput(ev),
        onCursorMove: (nx, ny, mode) => {
          if (mode === "trackpad") {
            const box = getVideoContentRect(activeMediaEl);
            els.trackpadCursor.style.left = `${box.left + nx * box.width}px`;
            els.trackpadCursor.style.top = `${box.top + ny * box.height}px`;
            els.trackpadCursor.classList.remove("hidden");
          } else {
            els.trackpadCursor.classList.add("hidden");
          }
        },
      });
      inputController.attach();
      syncInputModeUi();
      els.stage.focus();
    },
    onSystemStatus: (st) => {
      renderSystemStatus(st);
    },
    onQuickActionResult: (res) => {
      if (res.status) renderSystemStatus(res.status);
      showQaFeedback(res.message || `Action ${res.action} completed`, res.ok !== false);
    },
    onSnapshot: (snap) => {
      if (snap && snap.image_b64) {
        els.qaSnapshotImg.src = `data:image/jpeg;base64,${snap.image_b64}`;
        els.qaSnapshotImg.classList.remove("hidden");
        showQaFeedback(`Snapshot captured (${snap.width}x${snap.height})`, true);
      }
    },
    onError: (message) => {
      if (!els.sessionOverlay.classList.contains("hidden")) {
        showPortalWithError(message);
      } else if (!els.quickActionsView.classList.contains("hidden")) {
        showQaFeedback(message, false);
      } else {
        els.hudTransport.textContent = `Error: ${message}`;
      }
    },
    onPairingGranted: ({ hostId: hid, pairing_id, pairing_secret }) => {
      if (hid && pairing_id && pairing_secret) {
        savePairing(hid, { pairing_id, pairing_secret });
        if (activeSessionOpts && activeSessionOpts.hostId === hid) {
          activeSessionOpts.pairing = { pairing_id, pairing_secret };
        }
      }
    },
    onPairingInvalid: (hid) => {
      if (hid) clearPairing(hid);
    },
    onClipboardSync: (text) => {
      els.clipboardBox.value = text;
      els.qaClipInput.value = text;
      els.clipboardStatus.textContent = "Pulled from host clipboard.";
      if (!els.quickActionsView.classList.contains("hidden")) {
        showQaFeedback("Pulled text from PC clipboard.", true);
      }
    },
    onFileProgress: (msg) => {
      els.fileProgress.textContent = msg;
      els.qaFileProgress.textContent = msg;
    },
    onStats: ({ fps, rttMs, candidateType, bitrateKbps }) => {
      const modeMap = {
        host: "P2P Local",
        srflx: "P2P STUN",
        prflx: "P2P STUN",
        relay: "TURN Relay",
        ws_relay: "WSS Relay",
      };
      els.hudTransport.textContent = modeMap[candidateType] || "Connected";
      const rttStr = rttMs !== null ? `${rttMs} ms` : "-- ms";
      const rateStr =
        bitrateKbps >= 1000
          ? `${(bitrateKbps / 1000).toFixed(1)} Mbps`
          : `${bitrateKbps} kbps`;
      els.hudMetrics.textContent = `${rttStr} | ${fps} fps | ${rateStr}`;
    },
    onClosed: () => {
      teardownSessionUi();
    },
  });

  try {
    await activeClient.connect({ mode, hostId, supportCode, pin, pairing, rememberDevice, controlOnly });
  } catch (err) {
    showPortalWithError(err.message || "Failed to connect");
  }
}

function populateMonitors(monitors, activeIndex) {
  els.selectMonitor.innerHTML = "";
  for (const m of monitors) {
    const opt = document.createElement("option");
    opt.value = String(m.index);
    opt.textContent = `${m.name}`;
    if (m.index === activeIndex) opt.selected = true;
    els.selectMonitor.appendChild(opt);
  }
}

function teardownSessionUi() {
  if (inputController) {
    inputController.detach();
    inputController = null;
  }
  activeClient = null;
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
  if (navigator.keyboard && navigator.keyboard.unlock) {
    navigator.keyboard.unlock();
  }
  els.remoteVideo.classList.remove("fit-original");
  els.fallbackCanvas.classList.remove("fit-original");
  els.remoteVideo.classList.remove("hidden");
  els.fallbackCanvas.classList.add("hidden");
  els.stage.classList.remove("fit-original-stage");
  els.trackpadCursor.classList.add("hidden");
  els.btnFitMode.textContent = "Fit: Contain";
  els.btnQuickFit.textContent = "1:1";
  els.quickActionsView.classList.add("hidden");
  els.qaFeedback.classList.add("hidden");
  els.qaSnapshotImg.classList.add("hidden");
  els.qaSnapshotImg.removeAttribute("src");
  activeControlOnly = false;
  els.sessionView.classList.add("hidden");
  els.portalHeader.classList.remove("hidden");
  els.portalView.classList.remove("hidden");
  refreshHealthAndHosts();
}

function showPortalWithError(message) {
  if (activeClient) activeClient.close();
  teardownSessionUi();
  if (els.panelSupport.classList.contains("hidden")) {
    els.pinError.textContent = message;
    els.pinError.classList.remove("hidden");
    if (pendingHost) {
      els.pinModal.classList.remove("hidden");
    }
  } else {
    els.supportError.textContent = message;
    els.supportError.classList.remove("hidden");
  }
}

// Navigation tabs
els.tabAccess.addEventListener("click", () => {
  els.tabAccess.classList.add("active");
  els.tabSupport.classList.remove("active");
  els.panelAccess.classList.remove("hidden");
  els.panelSupport.classList.add("hidden");
});

els.tabSupport.addEventListener("click", () => {
  els.tabSupport.classList.add("active");
  els.tabAccess.classList.remove("active");
  els.panelSupport.classList.remove("hidden");
  els.panelAccess.classList.add("hidden");
});

els.btnRefreshHosts.addEventListener("click", refreshHealthAndHosts);

// Auto-format 12-digit support code as XXXX-XXXX-XXXX
els.supportCodeInput.addEventListener("input", () => {
  const digits = els.supportCodeInput.value.replace(/\D/g, "").slice(0, 12);
  const parts = digits.match(/.{1,4}/g) || [];
  els.supportCodeInput.value = parts.join("-");
});

els.supportForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const digits = els.supportCodeInput.value.replace(/\D/g, "");
  if (digits.length !== 12) {
    els.supportError.textContent = "Please enter all 12 digits of the support code.";
    els.supportError.classList.remove("hidden");
    return;
  }
  els.supportError.classList.add("hidden");
  startSession({
    mode: "it2me",
    hostName: "Remote Support PC",
    supportCode: digits,
    pin: digits,
    rememberDevice: false,
  });
});

// PIN modal handlers
els.btnPinCancel.addEventListener("click", () => {
  pendingHost = null;
  els.pinModal.classList.add("hidden");
});

els.pinForm.addEventListener("submit", (e) => {
  e.preventDefault();
  if (!pendingHost) return;
  const host = pendingHost;
  const controlOnly = pendingControlOnly;
  const pin = els.pinInput.value;
  const remember = els.rememberPinCheckbox.checked;
  els.pinModal.classList.add("hidden");
  startSession({
    mode: "me2me",
    hostId: host.host_id,
    hostName: host.name,
    pin,
    rememberDevice: remember,
    controlOnly,
  });
});

// Workspace token modal
els.btnWorkspaceToken.addEventListener("click", () => {
  els.workspaceTokenInput.value = getWorkspaceToken();
  els.tokenModal.classList.remove("hidden");
});

els.btnTokenCancel.addEventListener("click", () => {
  els.tokenModal.classList.add("hidden");
});

els.tokenForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const val = els.workspaceTokenInput.value.trim();
  const resp = await fetch("/api/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: val }),
  });
  if (!resp.ok) {
    els.tokenError.textContent = "Invalid workspace token.";
    els.tokenError.classList.remove("hidden");
    return;
  }
  localStorage.setItem(WORKSPACE_TOKEN_KEY, val);
  els.tokenError.classList.add("hidden");
  els.tokenModal.classList.add("hidden");
  refreshHealthAndHosts();
});

// Active session controls
els.btnCancelConnecting.addEventListener("click", () => {
  if (activeClient) activeClient.close("Cancelled by user");
});

els.btnDisconnect.addEventListener("click", () => {
  if (activeClient) activeClient.close("Disconnected by user");
});

function toggleSidebarDrawer() {
  const open = els.sessionSidebar.classList.toggle("open");
  els.btnToggleSidebar.classList.toggle("open", open);
  els.btnToggleSidebar.textContent = open ? "›" : "‹";
  if (!open) els.stage.focus({ preventScroll: true });
}

async function toggleFullscreenMode() {
  try {
    if (!document.fullscreenElement) {
      await els.stage.requestFullscreen();
      if (navigator.keyboard && navigator.keyboard.lock) {
        await navigator.keyboard.lock([
          "AltLeft",
          "AltRight",
          "Tab",
          "Escape",
          "MetaLeft",
          "MetaRight",
        ]);
      }
    } else {
      await document.exitFullscreen();
    }
  } catch {
    // Keyboard lock is optional across browsers
  }
  els.stage.focus({ preventScroll: true });
}

function toggleFitMode() {
  const orig = els.remoteVideo.classList.toggle("fit-original");
  els.fallbackCanvas.classList.toggle("fit-original", orig);
  els.stage.classList.toggle("fit-original-stage", orig);
  els.btnFitMode.textContent = orig ? "Fit: 1:1 Original" : "Fit: Contain";
  els.btnQuickFit.textContent = orig ? "Contain" : "1:1";
  els.stage.focus({ preventScroll: true });
}

function syncInputModeUi() {
  if (!inputController) return;
  const labels = {
    desktop: "Mouse",
    trackpad: "Trackpad",
    touch: "Touch",
  };
  els.selectInputMode.value = inputController.inputMode;
  els.btnQuickMode.textContent = labels[inputController.inputMode] || "Mode";
}

els.btnToggleSidebar.addEventListener("click", toggleSidebarDrawer);
els.btnQuickHudActions.addEventListener("click", () => {
  openQuickActionsPanel({
    hostName: (activeSessionOpts && activeSessionOpts.hostName) || els.hudHostName.textContent,
    controlOnly: false,
  });
});
els.btnQuickSidebar.addEventListener("click", toggleSidebarDrawer);
els.btnQuickDisconnect.addEventListener("click", () => {
  if (activeClient) activeClient.close("Disconnected by user");
});

// Quick Actions Deck handlers
els.qaBtnDisconnect.addEventListener("click", () => {
  if (activeClient) activeClient.close("Disconnected by user");
});

els.qaBtnRefresh.addEventListener("click", () => {
  if (activeClient) activeClient.requestSystemStatus();
});

els.qaBtnMirror.addEventListener("click", () => {
  if (!activeControlOnly) {
    els.quickActionsView.classList.add("hidden");
    els.stage.focus({ preventScroll: true });
    return;
  }
  const opts = activeSessionOpts;
  if (!opts) return;
  const pairing = getPairings()[opts.hostId] || opts.pairing;
  startSession({ ...opts, pairing, controlOnly: false });
});

for (const btn of document.querySelectorAll(".qa-action-btn")) {
  btn.addEventListener("click", () => {
    if (!activeClient) return;
    const msg = btn.dataset.confirm;
    if (msg && !window.confirm(msg)) return;
    activeClient.sendQuickAction(btn.dataset.action);
  });
}

for (const btn of document.querySelectorAll(".qa-preset-btn")) {
  btn.addEventListener("click", () => {
    if (!activeClient) return;
    activeClient.sendQuickAction("launch_preset", { preset: btn.dataset.preset });
  });
}

els.qaBtnOpenUrl.addEventListener("click", () => {
  if (!activeClient) return;
  const url = els.qaUrlInput.value.trim();
  if (!url) {
    showQaFeedback("Enter an http:// or https:// URL first.", false);
    return;
  }
  activeClient.sendQuickAction("open_url", { url });
});

els.qaBtnSnapshot.addEventListener("click", () => {
  if (!activeClient) return;
  showQaFeedback("Capturing screen snapshot...", true);
  activeClient.requestSnapshot();
});

els.qaBtnClipSend.addEventListener("click", () => {
  if (!activeClient) return;
  activeClient.sendClipboard(els.qaClipInput.value);
  showQaFeedback("Sent text to PC clipboard.", true);
});

els.qaBtnClipPull.addEventListener("click", () => {
  if (activeClient) activeClient.pullClipboard();
});

els.qaBtnUpload.addEventListener("click", async () => {
  if (!activeClient) return;
  const file = els.qaFileInput.files && els.qaFileInput.files[0];
  if (!file) {
    els.qaFileProgress.textContent = "Select a file first.";
    return;
  }
  try {
    const res = await activeClient.uploadFile(file);
    els.qaFileProgress.textContent = `Saved on host: ${res.name} (${res.size} bytes)`;
    showQaFeedback(`Uploaded ${res.name} to host Downloads!`, true);
  } catch (err) {
    els.qaFileProgress.textContent = `Error: ${err.message}`;
    showQaFeedback(err.message, false);
  }
});

els.btnQuickMode.addEventListener("click", () => {
  if (!inputController) return;
  const order = ["trackpad", "touch", "desktop"];
  const next = order[(order.indexOf(inputController.inputMode) + 1) % order.length];
  inputController.setMode(next);
  syncInputModeUi();
  els.btnQuickMode.blur();
  els.stage.focus({ preventScroll: true });
});

els.selectInputMode.addEventListener("change", () => {
  if (inputController) {
    inputController.setMode(els.selectInputMode.value);
    syncInputModeUi();
  }
  els.selectInputMode.blur();
  els.stage.focus({ preventScroll: true });
});

els.btnQuickKeyboard.addEventListener("click", () => {
  els.mobileKbInput.focus();
});

els.mobileKbInput.addEventListener("keydown", (e) => {
  if (!activeClient) return;
  if (["Backspace", "Enter", "Tab", "Escape"].includes(e.key)) {
    e.preventDefault();
    activeClient.sendInput({ type: "key_event", code: e.key, key: e.key, down: true });
    activeClient.sendInput({ type: "key_event", code: e.key, key: e.key, down: false });
  }
});

els.mobileKbInput.addEventListener("input", () => {
  if (!activeClient) return;
  const val = els.mobileKbInput.value;
  if (val) {
    activeClient.sendText(val);
    els.mobileKbInput.value = "";
  }
});

els.btnCopyTunnel.addEventListener("click", async () => {
  const url = els.tunnelUrlLink.textContent;
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    els.btnCopyTunnel.textContent = "Copied!";
    setTimeout(() => {
      els.btnCopyTunnel.textContent = "Copy URL";
    }, 1500);
  } catch {
    // Clipboard blocked on non-HTTPS non-localhost contexts
  }
});

els.btnFullscreen.addEventListener("click", toggleFullscreenMode);
els.btnQuickFullscreen.addEventListener("click", toggleFullscreenMode);

els.btnFitMode.addEventListener("click", toggleFitMode);
els.btnQuickFit.addEventListener("click", toggleFitMode);

els.btnToggleInput.addEventListener("click", () => {
  if (!inputController) return;
  inputController.enabled = !inputController.enabled;
  els.btnToggleInput.textContent = inputController.enabled ? "Control: ON" : "Control: View Only";
  els.btnToggleInput.blur();
  els.stage.focus({ preventScroll: true });
});

els.selectMonitor.addEventListener("change", () => {
  if (activeClient) {
    activeClient.sendControl({
      type: "select_monitor",
      index: Number(els.selectMonitor.value),
    });
  }
  els.selectMonitor.blur();
  els.stage.focus({ preventScroll: true });
});

els.selectQuality.addEventListener("change", () => {
  if (activeClient) {
    activeClient.sendControl({
      type: "set_quality",
      preset: els.selectQuality.value,
    });
  }
  els.selectQuality.blur();
  els.stage.focus({ preventScroll: true });
});

for (const btn of document.querySelectorAll(".combo-btn")) {
  btn.addEventListener("click", () => {
    if (activeClient) {
      activeClient.sendInput({ type: "special_combo", combo: btn.dataset.combo });
    }
    btn.blur();
    els.stage.focus({ preventScroll: true });
  });
}

els.btnPasteText.addEventListener("click", () => {
  if (!activeClient) return;
  const text = els.pasteTextInput.value;
  if (!text) return;
  activeClient.sendText(text);
  els.pasteTextInput.value = "";
  els.btnPasteText.blur();
  els.stage.focus({ preventScroll: true });
});

els.btnClipboardSend.addEventListener("click", () => {
  if (activeClient) {
    activeClient.sendClipboard(els.clipboardBox.value);
    els.clipboardStatus.textContent = "Sent to host clipboard.";
  }
});

els.btnClipboardPull.addEventListener("click", () => {
  if (activeClient) activeClient.pullClipboard();
});

els.btnUploadFile.addEventListener("click", async () => {
  if (!activeClient) return;
  const file = els.fileInput.files && els.fileInput.files[0];
  if (!file) {
    els.fileProgress.textContent = "Select a file first.";
    return;
  }
  try {
    const res = await activeClient.uploadFile(file);
    els.fileProgress.textContent = `Saved on host: ${res.name} (${res.size} bytes)`;
  } catch (err) {
    els.fileProgress.textContent = `Error: ${err.message}`;
  }
});

// Initial load & periodic host directory refresh
if (location.hash === "#support") {
  els.tabSupport.click();
}
refreshHealthAndHosts();
setInterval(() => {
  if (els.sessionView.classList.contains("hidden") && els.quickActionsView.classList.contains("hidden")) {
    refreshHealthAndHosts();
  }
}, 3000);
