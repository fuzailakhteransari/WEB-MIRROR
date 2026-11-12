# WebDesk Remote

WebDesk Remote lets you view and control a Windows PC or an Android phone from a browser. It includes live screen mirroring, touch and mouse control, Quick Actions, clipboard and file transfer, and a browser portal that can be installed as a PWA.

## Share this project safely

Give your colleague the GitHub repository link and have them clone it on **their own Windows PC**. Do not copy or commit your personal `%USERPROFILE%\.webdesk-remote\` folder. It holds device pairing data, logs, and any saved workspace token.

Each PC should use its own host name and private PIN. Use a private GitHub repository or add your colleague as a repository collaborator if the project is not meant to be public.

## Publish the prepared copy

Before sharing the repository, review the files that will be committed and make sure `dist/webdesk-phone-host.apk` is listed. Then commit and push the release:

```powershell
git status
git add .
git commit -m "Prepare one-click shared setup"
git push origin main
```

Do not force-add ignored files. The repository rules already include the Android APK and exclude local configuration, virtual environments, build caches, Android signing keys, and `.env` files.

## What they need

- Windows 10 or 11, 64-bit, on the PC to control.
- [Git for Windows](https://git-scm.com/download/win).
- Python **3.12 x64** from [python.org](https://www.python.org/downloads/windows/). Select **Add Python to PATH** during installation.
- Internet access for the one-time Python package install. The launcher downloads Cloudflare Tunnel automatically only when the temporary phone URL is used.

The Android phone app is already included as `dist/webdesk-phone-host.apk`; Android Studio is only needed to modify or rebuild that app.

## Fast setup: clone and run

Open PowerShell and run:

```powershell
git clone https://github.com/fuzailakhteransari/WEB-MIRROR.git
cd WEB-MIRROR
.\WebDesk_Remote.bat
```

On the first run, the one launcher creates a private `.venv` environment and installs the runtime packages. Keep the setup window open until the Control Center appears. Later launches are silent.

In the Control Center:

1. Enter a clear PC name and a private PIN of at least six characters, then select **Save PIN & Apply**.
2. The app starts its background supervisor and registers itself to start when that Windows user signs in.
3. If Windows Firewall asks, allow access on the network type you actually use.
4. Use **Open Browser Portal** for the local portal, or use one of the internet access options below.

The single `WebDesk_Remote.bat` file is the only launcher. The Control Center contains the advanced controls: fixed cloud URL, workspace token, temporary phone URL, support code, TURN relay, PIN changes, and Stop All.

## Connect from another device

### Temporary HTTPS phone URL

The default background setup starts a Cloudflare Quick Tunnel. Its current HTTPS address and QR code appear in the Control Center. Open that address on the phone or browser that will control the PC.

Quick Tunnel URLs change when the tunnel restarts. Use the permanent option below if you want one bookmark that keeps working after Windows restarts.

### Permanent Render URL

1. Push this repository, including `dist/webdesk-phone-host.apk`, to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com/), select **New → Blueprint**, connect this GitHub repository, and deploy the root `render.yaml`.
3. Open the deployed URL and confirm that `https://your-service.onrender.com/api/health` returns JSON.
4. On the Windows PC, open `WebDesk_Remote.bat`. In **3. Always-On Background Service**, paste the deployed HTTPS URL into **Fixed Cloud WS**.
5. If you set a workspace token on Render, enter the exact same value in **Workspace Token**, then select **Apply & Run in Background**.
6. Bookmark the Render URL on the controlling device. Select the online PC, enter its PIN, and start mirroring or use Quick Actions.

`render.yaml` is a Render Blueprint in the repository root, so Render uses it during Blueprint deployment. Set a real `WEBDESK_WORKSPACE_TOKEN` only in Render’s **Environment** screen; never commit it to GitHub. Leaving it empty is the simplest setup when the repository and portal access are already private.

## Android phone host

The portal’s **Pair Phone** section offers the included `webdesk-phone-host.apk` after the Render deployment or local server is running.

1. Install the APK on the Android phone to be mirrored. Allow installation from the browser or file manager if Android asks.
2. Open **WebDesk Phone Host**.
3. Enter the same server URL you use in the browser: the Render URL, the temporary Quick Tunnel URL, or a LAN URL.
4. Give the phone its own PIN, save the settings, enable the WebDesk accessibility service, and allow the screen-capture prompt.
5. Set the app battery setting to **Unrestricted** so Android does not stop the host in the background.
6. Select **Start Mirror + Control Host**. The phone will appear in the portal with an Android Phone badge.

On Android 13 or newer, if the accessibility option is disabled, open the app’s info screen, select the three-dot menu, and allow restricted settings.

### Rebuild the Android APK only when needed

1. Install [Android Studio](https://developer.android.com/studio) and accept the requested Android SDK and JDK 17 components.
2. Open the `android-host` folder as the Android Studio project.
3. Choose **Build → Build Bundle(s) / APK(s) → Build APK(s)** with the debug variant.
4. Copy `android-host\app\build\outputs\apk\debug\app-debug.apk` to `dist\webdesk-phone-host.apk`.
5. Commit and push the updated APK, then redeploy Render so the portal serves the new version.

## Daily use

- Double-click `WebDesk_Remote.bat` on the PC you want to control. It starts or confirms the background service.
- Open the portal URL from another device, select the host, and enter that host’s PIN.
- Choose **Screen Mirroring** for live view and control. Choose **Quick Actions** for commands without a video stream.
- Use **Stop All** in the Control Center only when you want to disable remote access on that PC.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Python is not found | Install Python 3.12 x64 from python.org, select **Add Python to PATH**, then run `WebDesk_Remote.bat` again. |
| First setup fails while installing packages | Confirm internet access, delete the project’s `.venv` folder, and run the launcher again. |
| PC is offline in the portal | Open the Control Center on that PC. Check the fixed server URL, workspace token, and Windows Firewall. |
| Quick Tunnel URL stopped working | Open the Control Center and copy the newly shown URL. For a stable address, use Render. |
| Portal says the phone APK is unavailable | Confirm `dist/webdesk-phone-host.apk` is committed to GitHub and that the latest Render deployment completed. |
| Android host stops | Reopen the app, grant screen capture and accessibility permissions, and set the battery mode to Unrestricted. |
| Cloud connection on mobile data is unreliable | Configure a TURN relay in the Control Center’s **4G/5G TURN Setup**. |

## Development and verification

The normal one-click installer uses `requirements.txt`. Contributors can install test tools with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v tests/
```

## Environment variables

- `WEBDESK_WORKSPACE_TOKEN`: Optional extra shared secret for portal and signaling access. Configure it in Render, then enter the same value in the Control Center.
- `WEBDESK_ICE_SERVERS_JSON`: Custom STUN/TURN server JSON for the deployed signaling service.
- `WEBDESK_REGISTRY_PATH`: Server host-registry storage path.
- `WEBDESK_SUPPORT_TTL`: Lifetime of one-time support codes in seconds; default `300`.
