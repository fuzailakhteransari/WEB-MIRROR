"""One-click launcher and process manager for WebDesk Remote server and host."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from host.config_store import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    initialize_host_config,
    load_host_config,
    save_host_config,
)
from host.tunnel import _pid_alive, start_cloudflared_tunnel
from shared.srp import create_verifier

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "WebDeskRemote"
DEFAULT_SERVICE_PREFS: dict[str, Any] = {
    "autostart_tunnel": True,
    "keep_awake": False,
    "port": 8080,
    "custom_server_url": "",
}


def load_service_prefs(state_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, Any]:
    path = state_dir / "service_prefs.json"
    prefs = dict(DEFAULT_SERVICE_PREFS)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prefs.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    return prefs


def save_service_prefs(prefs: dict[str, Any], state_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_SERVICE_PREFS)
    merged.update(prefs)
    path = state_dir / "service_prefs.json"
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return path


def normalize_cloud_ws_url(raw_url: str) -> str:
    """Convert any user-pasted https:// or bare domain into a ws(s)://.../ws/host URL."""
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urllib.parse.urlparse(cleaned)
    scheme_map = {
        "https": "wss",
        "wss": "wss",
        "http": "ws",
        "ws": "ws",
    }
    ws_scheme = scheme_map.get(parsed.scheme.lower())
    if not ws_scheme or not parsed.netloc:
        raise ValueError(f"Invalid server URL: {raw_url}")
    path = parsed.path.rstrip("/")
    if not path or path == "":
        path = "/ws/host"
    elif not path.endswith("/ws/host"):
        path = f"{path}/ws/host"
    return urllib.parse.urlunparse((ws_scheme, parsed.netloc, path, "", "", ""))


def ws_url_to_http_base(ws_url: str) -> str:
    """Convert a ws(s)://.../ws/host URL to its browser https:// base URL."""
    cleaned = normalize_cloud_ws_url(ws_url)
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    http_scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{http_scheme}://{parsed.netloc}"


def configure_cloud_server(
    url: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_dir: Path | None = None,
    port: int = 8080,
    workspace_token: str | None = None,
    keep_awake: bool | None = None,
) -> tuple[str, str]:
    """Bind host config & background service prefs cleanly to a fixed cloud server (or reset to local)."""
    s_dir = state_dir or config_path.parent
    ws_url = normalize_cloud_ws_url(url)
    prefs = load_service_prefs(s_dir)
    prefs["port"] = port
    prefs["custom_server_url"] = ws_url
    if keep_awake is not None:
        prefs["keep_awake"] = keep_awake
    save_service_prefs(prefs, s_dir)

    target_ws = ws_url or f"ws://127.0.0.1:{port}/ws/host"
    ensure_host_configured(config_path, target_ws)
    cfg = load_host_config(config_path)
    cfg.server_url = target_ws
    if workspace_token is not None:
        cfg.workspace_token = workspace_token.strip()
    save_host_config(cfg, config_path)
    return ws_url, ws_url_to_http_base(ws_url)


def _preferred_pythonw(python_exe: Path | None = None) -> Path:
    exe = python_exe or Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return pyw
    return exe


def build_autostart_command(
    config_path: Path = DEFAULT_CONFIG_PATH,
    python_exe: Path | None = None,
) -> str:
    py = _preferred_pythonw(python_exe)
    return f'"{py}" -m host.launcher autostart --config "{config_path}"'


def enable_autostart(
    config_path: Path = DEFAULT_CONFIG_PATH,
    reg_path: str = AUTOSTART_REG_KEY,
    value_name: str = AUTOSTART_VALUE_NAME,
    python_exe: Path | None = None,
) -> str:
    """Register WebDesk Remote to auto-start silently on Windows login (no admin needed)."""
    if sys.platform != "win32":
        raise RuntimeError("Windows startup registration is only supported on Windows")
    import winreg

    cmd = build_autostart_command(config_path=config_path, python_exe=python_exe)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, cmd)
    return cmd


def disable_autostart(
    reg_path: str = AUTOSTART_REG_KEY,
    value_name: str = AUTOSTART_VALUE_NAME,
) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
        return True
    except FileNotFoundError:
        return False


def is_autostart_enabled(
    reg_path: str = AUTOSTART_REG_KEY,
    value_name: str = AUTOSTART_VALUE_NAME,
) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, value_name)
            return bool(str(val).strip())
    except FileNotFoundError:
        return False


def discover_lan_ip() -> str:
    """Return the primary local LAN IPv4 address without sending network traffic."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def fetch_health(base_url: str, timeout: float = 0.8) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    return None


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(pid_file: Path) -> int | None:
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _spawn_background(cmd: list[str], log_path: Path, pid_file: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    log_file.close()
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def _prompt_confirmed_pin(prompt_label: str = "Create an Access PIN") -> str:
    print("Tip: Characters are hidden while typing for privacy. You will confirm it twice.")
    while True:
        pin1 = getpass.getpass(f"{prompt_label} (min 6 chars/digits, input hidden): ").strip()
        if len(pin1) < 6:
            print("PIN must be at least 6 characters/digits. Please try again.")
            continue
        pin2 = getpass.getpass("Confirm Access PIN (type it again): ").strip()
        if pin1 != pin2:
            print("PINs did not match. Please try again.")
            continue
        return pin1


def ensure_host_configured(config_path: Path, server_ws_url: str) -> None:
    try:
        load_host_config(config_path)
        return
    except FileNotFoundError:
        pass

    print("=== First-Time WebDesk Remote Host Setup ===")
    default_name = platform.node() or "My-Windows-PC"
    name = input(f"Enter display name for this PC [{default_name}]: ").strip() or default_name
    pin = _prompt_confirmed_pin()
    cfg = initialize_host_config(
        host_name=name,
        pin=pin,
        server_url=server_ws_url,
        path=config_path,
    )
    print(f"Saved host '{cfg.host_name}' to {config_path}\n")


def reset_host_pin(
    new_pin: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host_name: str | None = None,
    port: int = 8080,
) -> None:
    """Update the host's SRP-6a PIN verifier (clearing old pairings) without losing host_id."""
    if len(new_pin.strip()) < 6:
        raise ValueError("PIN must be at least 6 characters/digits.")
    try:
        cfg = load_host_config(config_path)
        if host_name:
            cfg.host_name = host_name.strip()
        salt_hex, verifier_hex = create_verifier(cfg.host_id, new_pin.strip())
        cfg.srp_salt = salt_hex
        cfg.srp_verifier = verifier_hex
        cfg.pairing_registry.clear()
        save_host_config(cfg, config_path)
    except FileNotFoundError:
        initialize_host_config(
            host_name=(host_name or platform.node() or "My-Windows-PC"),
            pin=new_pin.strip(),
            server_url=f"ws://127.0.0.1:{port}/ws/host",
            path=config_path,
        )


def ensure_server_running(port: int, state_dir: Path) -> str:
    base_url = f"http://127.0.0.1:{port}"
    if fetch_health(base_url) is not None:
        return base_url

    server_pid_file = state_dir / "server.pid"
    server_log = state_dir / "server.log"
    _spawn_background(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.app:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
        log_path=server_log,
        pid_file=server_pid_file,
    )
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if fetch_health(base_url) is not None:
            return base_url
        time.sleep(0.15)
    raise RuntimeError(f"Signaling server failed to start on port {port}. Check {server_log}")


def start_all(
    port: int = 8080,
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_dir: Path | None = None,
    open_browser: bool = True,
    mock_desktop: bool = False,
    keep_awake: bool = False,
    custom_server_url: str = "",
) -> dict[str, Any]:
    s_dir = state_dir or config_path.parent
    s_dir.mkdir(parents=True, exist_ok=True)

    custom_ws = normalize_cloud_ws_url(custom_server_url)
    server_ws_url = custom_ws or f"ws://127.0.0.1:{port}/ws/host"
    ensure_host_configured(config_path, server_ws_url)

    if custom_ws:
        base_url = ws_url_to_http_base(custom_ws)
        health: dict[str, Any] = {}
    else:
        base_url = ensure_server_running(port, s_dir)
        health = fetch_health(base_url) or {}

    host_pid_file = s_dir / "host.pid"
    existing_host_pid = _read_pid(host_pid_file)
    host_running = bool(existing_host_pid and _is_pid_running(existing_host_pid))

    if not host_running or (not custom_ws and int(health.get("online_hosts", 0)) == 0):
        if existing_host_pid and _is_pid_running(existing_host_pid):
            _terminate_pid(existing_host_pid)
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "host.cli",
            "start",
            "--server",
            server_ws_url,
            "--config",
            str(config_path),
        ]
        if mock_desktop:
            cmd.append("--mock-desktop")
        if keep_awake:
            cmd.append("--keep-awake")
        _spawn_background(cmd, log_path=s_dir / "host.log", pid_file=host_pid_file)

        if not custom_ws:
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                h = fetch_health(base_url)
                if h and int(h.get("online_hosts", 0)) >= 1:
                    health = h
                    break
                time.sleep(0.15)

    lan_ip = discover_lan_ip()
    print("WebDesk Remote is running!")
    print(f"  Local URL : {base_url}")
    if not custom_ws:
        print(f"  LAN URL   : http://{lan_ip}:{port}")
    print(f"  Logs      : {s_dir}")

    if open_browser:
        webbrowser.open(base_url)

    return {
        "base_url": base_url,
        "lan_url": f"http://{lan_ip}:{port}",
        "health": health,
    }


def start_supervisor(
    port: int = 8080,
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_dir: Path | None = None,
    mock_desktop: bool = False,
) -> int:
    """Spawn detached background watchdog keeping server, host, and tunnel alive."""
    s_dir = state_dir or config_path.parent
    s_dir.mkdir(parents=True, exist_ok=True)
    sup_pid_file = s_dir / "supervisor.pid"
    existing = _read_pid(sup_pid_file)
    if existing and _is_pid_running(existing):
        return existing

    py = str(_preferred_pythonw())
    cmd = [
        py,
        "-u",
        "-m",
        "host.launcher",
        "watch",
        "--port",
        str(port),
        "--config",
        str(config_path),
    ]
    if mock_desktop:
        cmd.append("--mock-desktop")
    return _spawn_background(cmd, log_path=s_dir / "supervisor.log", pid_file=sup_pid_file)


def run_watch_loop(
    port: int = 8080,
    config_path: Path = DEFAULT_CONFIG_PATH,
    mock_desktop: bool = False,
) -> int:
    """Always-on background supervisor loop: keeps host, signaling server, and tunnel healthy."""
    state_dir = config_path.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        print(f"Host config not found at {config_path}; supervisor exiting.")
        return 1

    sup_pid_file = state_dir / "supervisor.pid"
    my_pid = os.getpid()
    sup_pid_file.write_text(str(my_pid), encoding="utf-8")

    while True:
        current = _read_pid(sup_pid_file)
        if current != my_pid:
            break

        prefs = load_service_prefs(state_dir)
        use_port = int(prefs.get("port") or port)
        custom_ws = str(prefs.get("custom_server_url") or "").strip()
        keep_awake = bool(prefs.get("keep_awake", False))
        auto_tunnel = bool(prefs.get("autostart_tunnel", True))

        try:
            start_all(
                port=use_port,
                config_path=config_path,
                state_dir=state_dir,
                open_browser=False,
                mock_desktop=mock_desktop,
                keep_awake=keep_awake,
                custom_server_url=custom_ws,
            )
            if auto_tunnel and not custom_ws:
                tun_pid = _read_pid(state_dir / "tunnel.pid")
                url_file = state_dir / "tunnel_url.txt"
                if not (tun_pid and _pid_alive(tun_pid) and url_file.exists()):
                    start_cloudflared_tunnel(use_port, state_dir=state_dir)
        except Exception as exc:
            print(f"[supervisor] check/recover error: {exc}", flush=True)

        for _ in range(10):
            if _read_pid(sup_pid_file) != my_pid:
                return 0
            time.sleep(1.0)
    return 0


def stop_all(state_dir: Path = DEFAULT_CONFIG_DIR) -> list[int]:
    stopped: list[int] = []
    # Stop supervisor first so it doesn't revive stopped services
    for name in ("supervisor.pid", "tunnel.pid", "host.pid", "server.pid"):
        pid_file = state_dir / name
        pid = _read_pid(pid_file)
        if pid and _is_pid_running(pid):
            _terminate_pid(pid)
            stopped.append(pid)
        pid_file.unlink(missing_ok=True)
    (state_dir / "tunnel_url.txt").unlink(missing_ok=True)
    return stopped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WebDesk Remote 1-Click Launcher")
    parser.add_argument(
        "action",
        choices=[
            "start",
            "stop",
            "share",
            "reset-pin",
            "phone",
            "autostart",
            "watch",
            "enable-startup",
            "disable-startup",
            "set-cloud",
        ],
        default="start",
        nargs="?",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--pin", default="", help="New PIN for reset-pin (optional)")
    parser.add_argument("--url", default="", help="Fixed cloud URL (e.g. https://your-app.onrender.com)")
    parser.add_argument("--token", default="", help="Optional workspace token for cloud server")
    parser.add_argument("--local", action="store_true", help="Reset set-cloud back to local mode")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tunnel", action="store_true")
    parser.add_argument("--keep-awake", action="store_true")
    parser.add_argument("--start", action="store_true", help="Also launch background service now")
    parser.add_argument("--no-start", action="store_true", help="Skip starting background service")
    parser.add_argument("--mock-desktop", action="store_true")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    state_dir = cfg_path.parent

    if args.action == "stop":
        stopped = stop_all(state_dir)
        if stopped:
            print(f"Stopped WebDesk Remote processes: {stopped}")
        else:
            print("No running WebDesk Remote launcher processes found.")
        return 0

    if args.action == "enable-startup":
        prefs = load_service_prefs(state_dir)
        prefs["port"] = args.port
        prefs["autostart_tunnel"] = not args.no_tunnel
        if args.keep_awake:
            prefs["keep_awake"] = True
        save_service_prefs(prefs, state_dir)
        cmd = enable_autostart(config_path=cfg_path)
        print(f"Enabled Windows startup background service: {cmd}")
        if args.start:
            if not cfg_path.exists():
                ensure_host_configured(cfg_path, f"ws://127.0.0.1:{args.port}/ws/host")
            sup_pid = start_supervisor(
                port=args.port,
                config_path=cfg_path,
                state_dir=state_dir,
                mock_desktop=args.mock_desktop,
            )
            print(f"Background supervisor started (PID={sup_pid}).")
        return 0

    if args.action == "disable-startup":
        disable_autostart()
        stopped = stop_all(state_dir)
        print(f"Disabled Windows startup auto-run. Stopped PIDs: {stopped}")
        return 0

    if args.action == "set-cloud":
        raw_url = "" if args.local else (args.url.strip() or input("Paste your fixed Render/Cloud URL (e.g. https://your-app.onrender.com, blank = local): ").strip())
        token_val = "" if args.local else args.token.strip()
        ws_url, http_base = configure_cloud_server(
            url=raw_url,
            config_path=cfg_path,
            state_dir=state_dir,
            port=args.port,
            workspace_token=token_val if (args.local or args.token or raw_url) else None,
            keep_awake=True if args.keep_awake else None,
        )
        if not args.no_start:
            enable_autostart(config_path=cfg_path)
            stop_all(state_dir)
            sup_pid = start_supervisor(
                port=args.port,
                config_path=cfg_path,
                state_dir=state_dir,
                mock_desktop=args.mock_desktop,
            )
            print(f"Background supervisor running (PID={sup_pid}).")
        if ws_url:
            print(f"\nFixed Cloud URL configured!")
            print(f"  Bookmark this on your phone forever : {http_base}")
            print(f"  Host WebSocket endpoint             : {ws_url}")
        else:
            print(f"\nReset back to local + Cloudflare Quick Tunnel mode (port {args.port}).")
        return 0

    if args.action == "autostart":
        if not cfg_path.exists():
            return 0
        prefs = load_service_prefs(state_dir)
        start_supervisor(
            port=int(prefs.get("port") or args.port),
            config_path=cfg_path,
            state_dir=state_dir,
            mock_desktop=args.mock_desktop,
        )
        return 0

    if args.action == "watch":
        return run_watch_loop(
            port=args.port,
            config_path=cfg_path,
            mock_desktop=args.mock_desktop,
        )

    if args.action == "reset-pin":
        pin = args.pin.strip() or _prompt_confirmed_pin("Enter a NEW Access PIN")
        stop_all(state_dir)
        reset_host_pin(pin, config_path=cfg_path, port=args.port)
        print("PIN updated cleanly. Starting WebDesk Remote...")
        start_all(
            port=args.port,
            config_path=cfg_path,
            state_dir=state_dir,
            open_browser=not args.no_browser,
            mock_desktop=args.mock_desktop,
        )
        return 0

    if args.action == "phone":
        start_all(
            port=args.port,
            config_path=cfg_path,
            state_dir=state_dir,
            open_browser=False,
            mock_desktop=args.mock_desktop,
        )
        tunnel_url = start_cloudflared_tunnel(args.port, state_dir=state_dir)
        print("\n====================================================")
        print("  FREE PHONE ACCESS READY (Open this on your phone):")
        print(f"  {tunnel_url}")
        print("====================================================\n")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return 0

    if args.action == "share":
        base_url = ensure_server_running(args.port, state_dir)
        if not args.no_browser:
            webbrowser.open(f"{base_url}/#support")
        cmd = [
            sys.executable,
            "-m",
            "host.cli",
            "share",
            "--server",
            f"ws://127.0.0.1:{args.port}/ws/host",
        ]
        if args.mock_desktop:
            cmd.append("--mock-desktop")
        return subprocess.call(cmd, cwd=str(PROJECT_ROOT))

    start_all(
        port=args.port,
        config_path=cfg_path,
        state_dir=state_dir,
        open_browser=not args.no_browser,
        mock_desktop=args.mock_desktop,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
