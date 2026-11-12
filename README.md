# WebDesk Remote

Access and control a Windows computer from a browser. Open the WebDesk portal on your phone, tablet, or another computer to view the remote screen, use mouse and keyboard controls, scroll, zoom, and run Quick Actions.

This repository contains the Windows host and the signaling service. Each person should connect their phone and computer through the same WebDesk server URL; use a server you control for an isolated setup.

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

Use the URL belonging to your own WebDesk server. The public `web-mirror.onrender.com` instance belongs to the project owner; it is a shared service and is not the private server for a new install.

- **Same Wi-Fi/LAN:** Open the LAN URL shown by the Windows launcher on the phone.
- **Remote access without deploying a server:** In the Windows Control Center, start Quick Tunnel and open its generated HTTPS URL on the phone. The temporary URL changes when the tunnel restarts.
- **Your own always-on cloud server:** Deploy this repository's Render Blueprint (below), set that service URL in **Fixed Cloud WS** on the Windows computer, and open that same service's HTTPS URL in phone Chrome.

Then select the computer registered on that server, enter the PIN configured on that Windows computer, and choose screen mirroring or Quick Actions. A server maintains its own host list: using another person's server means using their shared instance, even when you choose your own computer name and PIN.

## Use your own cloud deployment

The repository includes a Render Blueprint at `render.yaml` and a Dockerfile. For a private setup, your friend should create a Render account/project and deploy their own Blueprint from this repository. Enter their new service URL in the Windows Control Center under **Fixed Cloud WS** and choose **Apply & Run in Background**. On the phone, open that same service URL in Chrome. This keeps the host registry separate from the project owner's server.

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
