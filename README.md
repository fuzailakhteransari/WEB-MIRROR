# WebDesk Remote

WebDesk Remote lets you view and control a Windows PC from a browser. Use screen mirroring for live control or Quick Actions for common commands.

## How it works

The Windows app runs on the computer you want to access. A WebDesk server connects that computer to the browser on your phone. For a separate setup, deploy your own server and use its URL on both devices. Each server keeps its own host list.

## Features

- Live screen mirroring and remote input
- Mobile controls for touch, scrolling, and pinch-to-zoom
- Quick Actions for common Windows commands
- Clipboard and file transfer
- PIN-based host authentication
- Local network, temporary HTTPS tunnel, or self-managed Render deployment

## Requirements

- Windows 10 or 11 (64-bit)
- Python 3.12 or newer
- Git for Windows
- Google Chrome (required for Bizpeople attendance actions)
- Internet access for the initial dependency setup

## 1. Install the Windows host

Clone the repository on the Windows PC you want to access, then run the launcher:

```powershell
git clone https://github.com/fuzailakhteransari/WEB-MIRROR.git
cd WEB-MIRROR
.\WebDesk_Remote.bat
```

On first launch, the setup creates a local Python environment and installs the required packages. In the Control Center, choose a computer name and a private PIN, then select **Save PIN & Apply**.

## 2. Deploy your own WebDesk server on Render

These steps create a separate WebDesk service under your own Render account. Your computer and phone will use this service rather than another person's server.

1. Sign in to your Render account.
2. Create a new **Blueprint** and connect the `WEB-MIRROR` GitHub repository. The included `render.yaml` defines the service.
3. Deploy the Blueprint and wait for the service to become available.
4. Copy the service URL, such as `https://your-service.onrender.com`. Confirm that it is responding by opening `https://your-service.onrender.com/api/health`.
5. On the Windows PC, open the WebDesk Control Center. Paste the service URL into **Fixed Cloud WS**, then select **Apply & Run in Background**.

The Render service URL is publicly reachable. For an additional shared access secret, set `WEBDESK_WORKSPACE_TOKEN` in the Render service's environment and enter the same value in the Control Center. Keep the token private and do not commit it to Git.

## 3. Connect from your phone

1. Open the same Render service URL in Chrome on your phone.
2. Select the Windows computer registered on that service.
3. Enter the private PIN configured for that computer.
4. Choose screen mirroring for live control or Quick Actions for commands without video.

## Bizpeople attendance actions

The Windows Quick Actions panel includes **Check In to Bizpeople** and **Check Out to Bizpeople**. Each action asks for confirmation, then opens the Bizpeople dashboard in a dedicated Chrome profile on the Windows PC. On first use, sign in when prompted. The browser stays open so you can review the result; close it before starting another attendance action. The sign-in session is stored in that Windows user's `%LOCALAPPDATA%` Chrome profile, separate from their regular Chrome profile.

## Other connection options

- **Same Wi-Fi network:** Open the LAN URL shown by the Windows launcher on your phone.
- **Temporary remote access:** Start **Quick Tunnel** in the Control Center and open its HTTPS URL on your phone. The URL changes when the tunnel restarts.

## Security

- Use a unique, private PIN for each computer.
- The host stores a PIN verifier rather than the plaintext PIN.
- Keep workspace tokens and local configuration private. Host settings and trusted-device pairing data are stored in `%USERPROFILE%\.webdesk-remote\`.
- Do not commit `.env` files, tokens, signing keys, logs, or local device data.

## Development

Install the development dependencies and run the test suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v tests/
```

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `WEBDESK_WORKSPACE_TOKEN` | Optional shared secret for portal and signaling access |
| `WEBDESK_ICE_SERVERS_JSON` | Custom STUN/TURN server configuration |
| `WEBDESK_REGISTRY_PATH` | Persistent server-side host registry location |
| `WEBDESK_SUPPORT_TTL` | Lifetime of one-time support codes in seconds (default: `300`) |
