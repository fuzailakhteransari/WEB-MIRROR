"""Tests for the 1-click launcher process lifecycle (start, health wait, idempotency, stop)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

from host.config_store import initialize_host_config, load_host_config
from host.launcher import _is_pid_running, _read_pid, reset_host_pin
from host.tunnel import parse_tunnel_url
from shared.srp import SRPClientSession, SRPServerSession

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_windows_pid_check_hides_tasklist_console(monkeypatch) -> None:
    from types import SimpleNamespace

    import host.launcher as launcher

    expected_flag = getattr(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(launcher.subprocess, "CREATE_NO_WINDOW", expected_flag, raising=False)
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform="win32"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout='"pythonw.exe","4321","Console","12,345 K"')

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher._is_pid_running(4321, name_contains="python") is True
    assert calls[0][0][0] == "tasklist"
    assert calls[0][1]["creationflags"] == expected_flag


def test_windows_tunnel_pid_check_hides_tasklist_console(monkeypatch) -> None:
    from types import SimpleNamespace

    import host.tunnel as tunnel

    expected_flag = getattr(tunnel.subprocess, "CREATE_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(tunnel.subprocess, "CREATE_NO_WINDOW", expected_flag, raising=False)
    monkeypatch.setattr(tunnel, "sys", SimpleNamespace(platform="win32"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout='"cloudflared.exe","4321","Console","12,345 K"')

    monkeypatch.setattr(tunnel.subprocess, "run", fake_run)

    assert tunnel._pid_alive(4321) is True
    assert calls[0][0][0] == "tasklist"
    assert calls[0][1]["creationflags"] == expected_flag


def test_launcher_start_idempotent_and_stop(tmp_path: Path) -> None:
    port = _free_port()
    cfg_path = tmp_path / "host_config.json"
    initialize_host_config(
        host_name="Launcher-PC",
        pin="123456",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )

    try:
        # 1. Start both background server and host via launcher CLI
        start_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "host.launcher",
                "start",
                "--port",
                str(port),
                "--config",
                str(cfg_path),
                "--no-browser",
                "--mock-desktop",
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=True,
        )
        assert "WebDesk Remote is running!" in start_proc.stdout
        assert f"http://127.0.0.1:{port}" in start_proc.stdout

        server_pid = _read_pid(tmp_path / "server.pid")
        host_pid = _read_pid(tmp_path / "host.pid")
        assert server_pid is not None and _is_pid_running(server_pid)
        assert host_pid is not None and _is_pid_running(host_pid)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hosts", timeout=3) as resp:
            hosts = json.loads(resp.read().decode("utf-8"))["hosts"]
            assert len(hosts) == 1
            assert hosts[0]["name"] == "Launcher-PC"
            assert hosts[0]["status"] == "online"

        # 2. Second launch is idempotent and reuses existing PIDs
        subprocess.run(
            [
                sys.executable,
                "-m",
                "host.launcher",
                "start",
                "--port",
                str(port),
                "--config",
                str(cfg_path),
                "--no-browser",
                "--mock-desktop",
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=True,
        )
        assert _read_pid(tmp_path / "server.pid") == server_pid
        assert _read_pid(tmp_path / "host.pid") == host_pid
    finally:
        # 3. Stop cleans up background processes and removes PID files
        stop_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "host.launcher",
                "stop",
                "--config",
                str(cfg_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=True,
        )
        assert "Stopped WebDesk Remote processes" in stop_proc.stdout
        assert not (tmp_path / "server.pid").exists()
        assert not (tmp_path / "host.pid").exists()


def test_reset_host_pin_updates_verifier_and_clears_pairings(tmp_path: Path) -> None:
    cfg_path = tmp_path / "host_config.json"
    cfg = initialize_host_config("MyPC", "111111", path=cfg_path)
    old_id = cfg.host_id
    cfg.pairing_registry["old-client"] = {"auth_key_hex": "aa" * 32}

    reset_host_pin("999888", config_path=cfg_path)
    updated = load_host_config(cfg_path)
    assert updated.host_id == old_id
    assert updated.pairing_registry == {}

    srv = SRPServerSession.start(updated.host_id, updated.srp_salt, updated.srp_verifier)
    cli = SRPClientSession.start(updated.host_id, "999888")
    m1 = cli.process_challenge(srv.salt_hex, srv.B_hex)
    m2 = srv.verify_client_proof(cli.A_hex, m1)
    assert cli.verify_server_proof(m2) is True


def test_parse_cloudflared_tunnel_url() -> None:
    log_sample = (
        "2026-11-11T09:00:00Z INF +------------------------------------------------------------+\n"
        "2026-11-11T09:00:00Z INF |  https://hobby-remote-demo-123.trycloudflare.com          |\n"
        "2026-11-11T09:00:00Z INF +------------------------------------------------------------+\n"
    )
    assert parse_tunnel_url(log_sample) == "https://hobby-remote-demo-123.trycloudflare.com"
    assert parse_tunnel_url("no url here") is None


def test_desktop_app_qr_canvas_render(tmp_path: Path) -> None:
    import tkinter as tk
    from host.desktop_app import WebDeskControlCenter, draw_qr_on_canvas

    app = WebDeskControlCenter(port=59999, config_path=tmp_path / "gui_host.json")
    try:
        draw_qr_on_canvas(app.qr_canvas, "https://demo-tunnel.trycloudflare.com")
        assert len(app.qr_canvas.find_all()) > 50
        app.root.update_idletasks()
    finally:
        app._closing = True
        app.root.destroy()


def test_autostart_registry_and_service_prefs(tmp_path: Path) -> None:
    from host.launcher import (
        build_autostart_command,
        disable_autostart,
        enable_autostart,
        is_autostart_enabled,
        load_service_prefs,
        save_service_prefs,
    )

    cfg_path = tmp_path / "svc_host.json"
    prefs_path = save_service_prefs(
        {"autostart_tunnel": False, "keep_awake": True, "port": 8099},
        state_dir=tmp_path,
    )
    assert prefs_path.exists()
    loaded = load_service_prefs(tmp_path)
    assert loaded["autostart_tunnel"] is False
    assert loaded["keep_awake"] is True
    assert loaded["port"] == 8099

    cmd = build_autostart_command(cfg_path)
    launcher_script = str((PROJECT_ROOT / "host" / "launcher.py").resolve())
    assert launcher_script in cmd and " autostart " in cmd and str(cfg_path) in cmd

    if sys.platform == "win32":
        import winreg

        test_reg_key = r"Software\WebDeskRemoteTestRun"
        test_val = "WebDeskTestEntry"
        try:
            written = enable_autostart(
                config_path=cfg_path, reg_path=test_reg_key, value_name=test_val
            )
            assert "autostart" in written
            assert is_autostart_enabled(reg_path=test_reg_key, value_name=test_val) is True
            assert disable_autostart(reg_path=test_reg_key, value_name=test_val) is True
            assert is_autostart_enabled(reg_path=test_reg_key, value_name=test_val) is False
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_reg_key)
            except OSError:
                pass


def test_supervisor_starts_and_stops_cleanly(tmp_path: Path) -> None:
    import time
    from host.launcher import save_service_prefs, start_supervisor, stop_all

    port = _free_port()
    cfg_path = tmp_path / "sup_host.json"
    initialize_host_config(
        host_name="Supervisor-PC",
        pin="123456",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    save_service_prefs(
        {"autostart_tunnel": False, "keep_awake": False, "port": port},
        state_dir=tmp_path,
    )
    try:
        sup_pid = start_supervisor(
            port=port,
            config_path=cfg_path,
            state_dir=tmp_path,
            mock_desktop=True,
        )
        assert _is_pid_running(sup_pid)
        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
                    if json.loads(resp.read().decode("utf-8")).get("online_hosts", 0) >= 1:
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(0.25)
        assert ready is True
    finally:
        stopped = stop_all(tmp_path)
        assert sup_pid in stopped
        assert not (tmp_path / "supervisor.pid").exists()
        assert not (tmp_path / "server.pid").exists()
        assert not (tmp_path / "host.pid").exists()


def test_cloud_url_normalization_and_dockerfile_deps(tmp_path: Path) -> None:
    from host.launcher import (
        configure_cloud_server,
        load_service_prefs,
        normalize_cloud_ws_url,
        ws_url_to_http_base,
    )

    assert normalize_cloud_ws_url("") == ""
    assert (
        normalize_cloud_ws_url("https://webdesk-demo.onrender.com/")
        == "wss://webdesk-demo.onrender.com/ws/host"
    )
    assert (
        normalize_cloud_ws_url("webdesk-demo.onrender.com")
        == "wss://webdesk-demo.onrender.com/ws/host"
    )
    assert (
        normalize_cloud_ws_url("http://192.168.1.50:8080")
        == "ws://192.168.1.50:8080/ws/host"
    )
    assert (
        ws_url_to_http_base("wss://webdesk-demo.onrender.com/ws/host")
        == "https://webdesk-demo.onrender.com"
    )

    cfg_path = tmp_path / "cloud_host.json"
    initialize_host_config("Cloud-PC", "123456", path=cfg_path)
    ws_url, http_base = configure_cloud_server(
        "https://webdesk-demo.onrender.com/",
        config_path=cfg_path,
        state_dir=tmp_path,
        workspace_token="secret-k",
    )
    assert ws_url == "wss://webdesk-demo.onrender.com/ws/host"
    assert http_base == "https://webdesk-demo.onrender.com"
    loaded_cfg = load_host_config(cfg_path)
    assert loaded_cfg.server_url == ws_url
    assert loaded_cfg.workspace_token == "secret-k"
    assert load_service_prefs(tmp_path)["custom_server_url"] == ws_url

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for pkg in ("fastapi", "uvicorn", "websockets", "pydantic", "qrcode"):
        assert pkg in dockerfile


def test_autostart_works_from_outside_cwd_and_respects_fixed_cloud_phone_mode(
    tmp_path: Path, monkeypatch
) -> None:
    import os
    import shlex
    import time
    from host.launcher import (
        build_autostart_command,
        main as launcher_main,
        save_service_prefs,
        stop_all,
    )

    port = _free_port()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside_cwd = tmp_path / "sim_system32"
    outside_cwd.mkdir()
    cfg_path = state_dir / "boot_host.json"
    initialize_host_config(
        host_name="Boot-PC",
        pin="123456",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    save_service_prefs(
        {"autostart_tunnel": False, "keep_awake": False, "port": port},
        state_dir=state_dir,
    )

    try:
        raw_cmd = build_autostart_command(cfg_path, python_exe=Path(sys.executable))
        argv = shlex.split(raw_cmd, posix=False)
        argv = [arg.strip('"') for arg in argv]
        argv[0] = sys.executable
        argv.append("--mock-desktop")
        # Run with stripped PYTHONPATH and outside cwd (exactly like Windows HKCU Run at boot)
        clean_env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        subprocess.run(
            argv,
            cwd=str(outside_cwd),
            env=clean_env,
            check=True,
            capture_output=True,
            text=True,
        )

        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
                    if json.loads(resp.read().decode("utf-8")).get("online_hosts", 0) >= 1:
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(0.25)
        assert ready is True
        assert (state_dir / "autostart.log").exists()
    finally:
        stop_all(state_dir)

    # Verify Fixed Cloud phone action does not invoke cloudflared quick tunnel
    cloud_state = tmp_path / "cloud_state"
    cloud_state.mkdir()
    cloud_cfg = cloud_state / "cloud.json"
    initialize_host_config(
        host_name="CloudPhone-PC",
        pin="123456",
        server_url="wss://demo-fixed.onrender.com/ws/host",
        path=cloud_cfg,
    )
    save_service_prefs(
        {
            "autostart_tunnel": True,
            "keep_awake": False,
            "port": port,
            "custom_server_url": "wss://demo-fixed.onrender.com/ws/host",
        },
        state_dir=cloud_state,
    )

    tunnel_called = []
    started_urls = []
    monkeypatch.setattr(
        "host.launcher.start_cloudflared_tunnel",
        lambda *_a, **_kw: tunnel_called.append(True) or "https://unexpected.trycloudflare.com",
    )

    def fake_start_all(**kwargs):
        started_urls.append(kwargs.get("custom_server_url"))
        return {"base_url": "https://demo-fixed.onrender.com", "lan_url": "http://127.0.0.1:8080", "health": {}}

    monkeypatch.setattr("host.launcher.start_all", fake_start_all)
    rc = launcher_main(["phone", "--config", str(cloud_cfg), "--no-browser"])
    assert rc == 0
    assert tunnel_called == []
    assert started_urls == ["wss://demo-fixed.onrender.com/ws/host"]
