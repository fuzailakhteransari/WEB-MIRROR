# WebDesk Remote

Access and control a Windows computer from a browser. Open the WebDesk portal on your phone, tablet, or another computer to view the remote screen, use mouse and keyboard controls, scroll, zoom, and run Quick Actions.

The portal is available at [web-mirror.onrender.com](https://web-mirror.onrender.com/). This repository contains the Windows host and the signaling service used by the portal.

## Features

- Live Windows screen mirroring and remote input
- Mobile-friendly controls, including touch scrolling and pinch-to-zoom
- Quick Actions for lock, display, media, power, and common Windows commands
- Clipboard and file transfer
- PIN-based SRP-6a authentication and optional workspace access token
- Local network, temporary HTTPS tunnel, or Render deployment

## Run the Windows host

### Requirements

- Windows 10 or 11, 64-bit
- Python 3.12 or newer
- Git for Windows
- Internet connection for the first-time dependency install

Clone the repository and run its single launcher:

```powershell
git clone https://github.com/fuzailakhteransari/WEB-MIRROR.git
cd WEB-MIRROR
.\WebDesk_Remote.bat
```

On first run, the launcher creates a local `.venv` and installs the required packages. In the Control Center, set a computer name and private PIN, then select **Save PIN & Apply**. The host runs in the background and starts automatically when you sign in to Windows.

## Connect from phone Chrome

1. Open [web-mirror.onrender.com](https://web-mirror.onrender.com/) in Chrome on your phone.
2. Select the Windows computer and enter the PIN you set in its Control Center.
3. Choose screen mirroring for live control or Quick Actions for commands without video.

The Windows Control Center can also show a temporary HTTPS tunnel URL and QR code for the local portal. The tunnel address changes when the tunnel restarts.

## Use your own cloud deployment

The repository includes a Render Blueprint at `render.yaml` and a Dockerfile. In Render, create a **Blueprint** from this repository and deploy the signaling service. Then enter that service's URL in the Windows Control Center under **Fixed Cloud WS** and choose **Apply & Run in Background**.

To require an additional workspace secret, set `WEBDESK_WORKSPACE_TOKEN` in Render's Environment settings and enter the same value in the Control Center. Never commit secret values to Git.

## Android phone host (separate project)

The Android APK is **not required** to control a Windows computer from phone Chrome. It is only for people who want to make an Android phone itself a remote host, so another browser can view and control that phone. The Android app is maintained separately from this Windows project.

## Security

- Choose a unique private PIN for each Windows host.
- The host stores a PIN verifier, not the plaintext PIN.
- Local host configuration and trusted-device pairing data live in `%USERPROFILE%\.webdesk-remote\` and must not be shared or committed.
- Keep workspace tokens, `.env` files, and signing keys out of Git.

## Development

Install the test dependencies in the launcher's virtual environment and run the test suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v tests/
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `WEBDESK_WORKSPACE_TOKEN` | Optional shared secret for portal and signaling access |
| `WEBDESK_ICE_SERVERS_JSON` | Custom STUN/TURN server configuration |
| `WEBDESK_REGISTRY_PATH` | Persistent host registry location |
| `WEBDESK_SUPPORT_TTL` | Lifetime of one-time support codes in seconds; default `300` |
