# WebDesk Remote

Browser-based remote access for Windows PCs and Android phones.

WebDesk Remote provides live screen mirroring, remote input, device quick actions, clipboard and file transfer, and a mobile-friendly web portal. Windows hosts run as an always-on background service; Android phones can run as native remote hosts.

## Highlights

- **Windows and Android hosts** — manage PCs and Android phones from one browser portal.
- **Live control** — screen streaming with mouse, keyboard, touch, scroll, trackpad, and pinch-to-zoom controls.
- **Quick Actions** — device commands without starting a video session, including lock, display, media, navigation, and power actions.
- **Secure pairing** — PIN-based SRP-6a authentication with optional trusted-device pairing and workspace access token.
- **Remote-ready** — use a temporary Cloudflare Quick Tunnel or deploy the signaling service to Render.
- **One-click Windows setup** — creates an isolated Python environment, installs runtime dependencies, and launches the Control Center.

## Architecture

```text
Browser / PWA
      │
      ▼
WebDesk signaling service ─── WebRTC or secure WSS relay ─── Windows or Android host
      │
      └── FastAPI portal, device directory, signaling, APK download
```

## Quick start

### Requirements

| Component | Requirement |
| --- | --- |
| Windows host | Windows 10 or 11, 64-bit |
| Runtime | Python 3.12+ |
| Source checkout | Git for Windows |
| Android host | Android 8.0+ (API 26) |

### Install and run

```powershell
git clone https://github.com/fuzailakhteransari/WEB-MIRROR.git
cd WEB-MIRROR
.\WebDesk_Remote.bat
```

On first launch, the installer creates `.venv`, installs the runtime dependencies, and opens the Windows Control Center. Create a host name and a private PIN, then select **Save PIN & Apply**.

The host registers to start automatically when that Windows user signs in. The Control Center also manages the remote URL, workspace token, TURN settings, support sessions, and service shutdown.

## Connect from another device

### Temporary HTTPS access

The Control Center creates a Cloudflare Quick Tunnel by default. Scan the QR code or open its displayed HTTPS URL on the device you will use to connect.

Quick Tunnel URLs are temporary and change after a tunnel restart.

### Persistent cloud access with Render

1. Push this repository to GitHub.
2. In [Render](https://dashboard.render.com/), create a **Blueprint** from the repository. The root `render.yaml` configures the service.
3. Confirm `https://your-service.onrender.com/api/health` returns JSON.
4. In the Windows Control Center, paste the deployed HTTPS URL into **Fixed Cloud WS** and select **Apply & Run in Background**.
5. Bookmark the Render URL on the browser or phone that will connect.

For an additional shared access secret, set `WEBDESK_WORKSPACE_TOKEN` in Render’s **Environment** settings and enter the same value in the Control Center’s **Workspace Token** field. Do not commit token values to Git.

## Android phone host

The ready-to-install APK is included at `dist/webdesk-phone-host.apk` and is served by the portal through **Pair Phone**.

1. Install **WebDesk Phone Host** on the Android phone.
2. Enter the server URL: your Render URL, Quick Tunnel URL, or LAN URL.
3. Set a PIN, enable accessibility control, and accept the screen-capture permission.
4. Set the app battery setting to **Unrestricted**.
5. Select **Start Mirror + Control Host**.

The phone appears in the portal as an Android host. Use live mirroring for direct control, or Quick Actions for commands such as Back, Home, wake, lock, flashlight, media controls, clipboard, and file transfer.

> On Android 13+, accessibility may require **App info → ⋮ → Allow restricted settings** before it can be enabled.

### Rebuild the Android app

Open `android-host` in Android Studio with Android SDK 36 and JDK 17. Build the debug APK, then replace `dist/webdesk-phone-host.apk` with:

```text
android-host\app\build\outputs\apk\debug\app-debug.apk
```

Commit the updated APK and redeploy the cloud service for the portal to serve it.

## Security model

- Hosts authenticate with a private PIN using SRP-6a; the PIN itself is not stored in plaintext.
- An optional workspace token protects the portal and signaling endpoints.
- Device pairings and local host configuration are stored in `%USERPROFILE%\.webdesk-remote\` and must stay on that PC.

When sharing the repository, share the GitHub link only. Do not share or commit local configuration, pairing data, `.env` files, Android signing keys, logs, or build caches.

## Repository layout

| Path | Purpose |
| --- | --- |
| `host/` | Windows host, capture, input, tunnel, Quick Actions, and Control Center |
| `server/` | FastAPI portal, signaling service, and host directory |
| `web/` | Browser and PWA client |
| `android-host/` | Native Android remote-host app |
| `shared/` | Shared signaling and SRP primitives |
| `dist/` | Installable Android host APK |
| `tests/` | Unit, integration, and browser test coverage |

## Development

Install contributor tooling after the one-click setup has created `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v tests/
```

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `WEBDESK_WORKSPACE_TOKEN` | Optional shared token for portal and signaling access |
| `WEBDESK_ICE_SERVERS_JSON` | Custom STUN/TURN server configuration |
| `WEBDESK_REGISTRY_PATH` | Path for persistent server-side host registry data |
| `WEBDESK_SUPPORT_TTL` | One-time support code lifetime in seconds; defaults to `300` |
