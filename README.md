# WebDesk Remote

A self-hosted remote desktop system inspired by Google Chrome Remote Desktop (`remotedesktop.google.com`), built on **FastAPI + WebSocket signaling**, a **zero-install browser WebRTC viewer**, and an **`aiortc` + `mss` + Win32 `SendInput` native Windows host**.

## Architecture

1. **`server/` (Control & Signaling Plane)**
   - Serves the browser portal (`web/`), REST host directory (`/api/hosts`), ICE config (`/api/ice-config`), and WebSocket signaling (`/ws/host`, `/ws/client`).
   - Supports optional workspace token authentication (`WEBDESK_WORKSPACE_TOKEN`) and custom STUN/TURN configuration (`WEBDESK_ICE_SERVERS_JSON`).
2. **`shared/` (Protocol & Cryptography)**
   - RFC 5054 2048-bit **SRP-6a** password-authenticated key exchange (`shared/srp.py` matched byte-for-byte by `web/js/srp.js`).
   - Binds both peers' WebRTC **DTLS certificate fingerprints** into the PAKE proof (`M1`/`M2`) so even a compromised signaling relay cannot MitM the WebRTC session.
3. **`host/` (Native Windows Host Agent)**
   - Captures displays via `mss` on a GDI-safe worker thread and encodes VP8/H264 via `aiortc` + `PyAV`.
   - Video capture and DataChannels (`control`, `input`, `clipboard`, `files`) stay locked until SRP-6a or trusted device pairing succeeds.
   - Injects mouse, scroll, and keyboard events via Windows `user32.SendInput` with DPI awareness and multi-monitor virtual desktop mapping.
   - Includes Chromium **Native Messaging** (`stdin`/`stdout` 32-bit length-prefixed JSON) + optional MV3 extension (`extension/`).
4. **`web/` (Browser Viewer Portal)**
   - Pure HTML/CSS/ES-Module WebRTC client requiring zero extensions to view/control a remote PC.

---

## Quickstart

### One-Click Windows Launchers (Recommended)
Double-click **`WebDesk_Remote.bat`** for the full **Desktop Control Center GUI** (manage PIN with Show/Hide toggle, start local/Wi-Fi or free HTTPS Phone Access with scannable QR code, share 12-digit support codes, configure 4G/5G TURN, and stop services).

Or use any direct 1-click `.bat` script in the project folder:
- **`WebDesk_Remote.bat`**: Opens the full desktop GUI Control Center (no console window).
- **`Enable_Background_Mode.bat`**: Registers silent auto-start on Windows login (like Chrome Remote Desktop) AND starts server + host + phone tunnel in the background immediately.
- **`Disable_Background_Mode.bat`**: Stops background services and removes Windows auto-start.
- **`Start_WebDesk_Remote.bat`**: Starts background signaling server & Me2Me host + opens `http://127.0.0.1:8080` automatically.
- **`Start_Phone_Access.bat`**: Starts everything + launches a **free zero-signup Cloudflare HTTPS Tunnel** (`https://....trycloudflare.com`) + scannable QR code on `http://127.0.0.1:8080` for mobile access anywhere.
- **`Share_Support_Code.bat`**: Generates a one-time 12-digit Remote Support code and opens the portal.
- **`Reset_PIN.bat`**: Lets you change/reset your Access PIN (with double confirmation) and restarts the background host.
- **`Stop_WebDesk_Remote.bat`**: Cleanly stops the background supervisor, server, host, and tunnel processes.

### Quick Actions Mode (No Video Streaming Needed)
Click **Quick Actions** on any device card to open the lightweight remote command deck without starting video capture:
- **Lock, Privacy Shield & Display:** Engage **Privacy Lock** (dark overlay + screen off) and **Open Lock** (wake display & clear shield), **Screen Off / Wake**, **Windows Lock (`Win+L`)**, and **Keep Awake** toggle.
- **Power & Recovery:** `Sleep`, `Restart (5s)`, `Shutdown (5s)`, and 1-tap `Abort Shutdown`.
- **Media, Apps & Snapshots:** Volume/Media controls, shortcuts (`Show Desktop`, `Alt+Tab`, `Alt+F4`), launch presets (`Downloads`, `Explorer`, `Task Manager`, `Terminal`, `Calc`, `Notepad`), open any `https://` URL in your PC browser, take instant still screen snapshots, and inspect live CPU / RAM / Battery / Lock telemetry.

### Free Permanent URL Setup (Render Free Tier — Never Scan QR Again)
1. Push this folder to a GitHub repository.
2. On [Render](https://render.com), click **New + -> Blueprint** (or **Web Service -> Docker**), select your repo, and choose the **Free** plan (`render.yaml` + `Dockerfile` are preconfigured).
3. Double-click **`Setup_Cloud_URL.bat`** (or paste your Render URL in **`WebDesk_Remote.bat`** -> **3. Always-On Background Service** -> **Apply & Run in Background**) and paste `https://your-app.onrender.com`.
4. Bookmark `https://your-app.onrender.com` on your phone once—your PC connects automatically after every Windows reboot with zero daily setup or QR re-scanning.

For strict mobile 4G/5G carrier CGNAT where direct UDP P2P is blocked, WebDesk automatically falls back to encrypted **WSS TLS Relay** (or you can add a custom STUN/TURN list in `%USERPROFILE%\.webdesk-remote\ice_servers.json` / `WEBDESK_ICE_SERVERS_JSON`).

### Manual CLI Setup

### 1. Install Dependencies
```powershell
python -m pip install -r requirements.txt
```

### 2. Start the Signaling Server
```powershell
python -m uvicorn server.app:app --host 0.0.0.0 --port 8080
```

### 3a. Unattended Remote Access (Me2Me)
```powershell
# Configure once (stores salted SRP-6a verifier locally, never plaintext PIN)
python -m host.cli setup --name "My-Windows-PC" --pin 123456 --server ws://127.0.0.1:8080/ws/host

# Start persistent host daemon
python -m host.cli start
```

### 3b. On-Demand Remote Support (IT2Me)
```powershell
# Generates a single-use 12-digit code and asks local consent when helper connects
python -m host.cli share --server ws://127.0.0.1:8080/ws/host
```

### 4. Connect from Any Browser
Open `http://127.0.0.1:8080` (or `http://<LAN-IP>:8080`), click your device (or switch to **Remote Support** to enter the 12-digit code), and enter the PIN.

---

## Environment Variables (Optional)

* `WEBDESK_WORKSPACE_TOKEN`: Require a bearer/session token to view hosts or connect via signaling.
* `WEBDESK_ICE_SERVERS_JSON`: JSON array of RTCIceServer objects (e.g., adding a self-hosted `coturn` TURN server for symmetric NATs/firewalls).
* `WEBDESK_REGISTRY_PATH`: Path to persist registered host metadata JSON across server restarts.
* `WEBDESK_SUPPORT_TTL`: IT2Me 12-digit code TTL in seconds (default `300`).

---

## Running Tests

```powershell
python -m pytest -v tests/
```
