"""Unit and Windows smoke tests for input mapping, mss capture, and Native Messaging framing."""

from __future__ import annotations

import ctypes
import io
import subprocess
import sys
from pathlib import Path

from host.capture import MssDesktopCapturer, SyntheticCapturer, compute_scaled_dimensions
from host.input_win32 import (
    DOM_CODE_TO_VK,
    INPUT,
    MockInputInjector,
    normalize_to_virtual_desktop,
)
from host.native_messaging import (
    NativeMessagingDispatcher,
    encode_native_message,
    install_chrome_native_host_manifest,
    read_native_message,
)
from shared.protocol import MonitorInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_win32_input_struct_size_and_coords() -> None:
    if sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(INPUT) == 40

    # Single monitor 1920x1080
    mon1 = MonitorInfo(index=1, name="Primary", left=0, top=0, width=1920, height=1080, is_primary=True)
    assert normalize_to_virtual_desktop(0.0, 0.0, mon1, 0, 0, 1920, 1080) == (0, 0)
    assert normalize_to_virtual_desktop(1.0, 1.0, mon1, 0, 0, 1920, 1080) == (65535, 65535)

    # Secondary monitor placed to the LEFT (-1920..0) in a 3840x1080 virtual desktop
    mon_left = MonitorInfo(index=2, name="Left", left=-1920, top=0, width=1920, height=1080)
    ax, ay = normalize_to_virtual_desktop(0.0, 0.5, mon_left, -1920, 0, 3840, 1080)
    assert ax == 0
    assert 32700 <= ay <= 32850

    assert "KeyA" in DOM_CODE_TO_VK
    assert "Enter" in DOM_CODE_TO_VK
    assert "ArrowUp" in DOM_CODE_TO_VK and DOM_CODE_TO_VK["ArrowUp"][1] is True

    mock = MockInputInjector()
    mock.mouse_move(0.5, 0.5, mon1)
    mock.key_event("KeyA", True, "a")
    mock.release_all_keys()
    assert len(mock.events) == 3
    assert mock.released_count == 1


def test_scaled_dimensions_and_synthetic_capture() -> None:
    assert compute_scaled_dimensions(1920, 1080, 1280, 720) == (1280, 720)
    assert compute_scaled_dimensions(1921, 1081, 1920, 1080)[0] % 2 == 0
    assert compute_scaled_dimensions(1921, 1081, 1920, 1080)[1] % 2 == 0

    cap = SyntheticCapturer(960, 540)
    frame = cap.capture_frame(1, 1600, 900)
    assert frame.width == 960
    assert frame.height == 540
    assert frame.format.name == "yuv420p"


def test_real_mss_capture_smoke() -> None:
    from mss.exception import ScreenShotError

    cap = MssDesktopCapturer()
    try:
        monitors = cap.get_monitors()
        assert len(monitors) >= 1
        assert any(m.is_primary for m in monitors)
        frame = cap.capture_frame(monitors[0].index, 1280, 720)
        assert frame.width > 0 and frame.height > 0
        assert frame.width % 2 == 0 and frame.height % 2 == 0
        assert frame.format.name == "yuv420p"

        # Simulate BitBlt failure (locked desktop / UAC) and verify graceful standby frame + handle reset
        class _LockedSct:
            monitors = [
                {"left": 0, "top": 0, "width": 1280, "height": 720},
                {"left": 0, "top": 0, "width": 1280, "height": 720},
            ]

            def grab(self, _mon):
                raise ScreenShotError("Simulated locked screen BitBlt failure")

            def close(self):
                pass

        cap._sct = _LockedSct()
        standby = cap.capture_frame(1, 1280, 720)
        assert standby.width == 1280 and standby.height == 720
        assert standby.format.name == "yuv420p"
        assert cap._sct is None  # Handle was reset for clean recovery
    finally:
        cap.close()


def test_native_messaging_framing_and_subprocess_stdio(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nm_host.json"
    dispatcher = NativeMessagingDispatcher(config_path=cfg_path)
    assert dispatcher.handle({"action": "get_host_status"})["configured"] is False

    # Round-trip framing via BytesIO
    framed = encode_native_message({"action": "ping"})
    assert int.from_bytes(framed[:4], sys.byteorder) == len(framed) - 4
    decoded = read_native_message(io.BytesIO(framed))
    assert decoded == {"action": "ping"}

    # Real subprocess stdin/stdout pipe test
    stdin_payload = (
        encode_native_message({"action": "configure_host", "host_name": "NM-PC", "pin": "112233"})
        + encode_native_message({"action": "get_host_status"})
    )
    proc = subprocess.run(
        [sys.executable, "-m", "host.cli", "native-messaging", "--config", str(cfg_path)],
        input=stdin_payload,
        capture_output=True,
        cwd=str(PROJECT_ROOT),
        check=True,
    )
    stream = io.BytesIO(proc.stdout)
    r1 = read_native_message(stream)
    r2 = read_native_message(stream)
    assert r1 is not None and r1["ok"] is True and r1["host_name"] == "NM-PC"
    assert r2 is not None and r2["configured"] is True and r2["host_name"] == "NM-PC"

    # Manifest writer creates valid JSON without modifying HKCU when manifest_dir is explicit
    manifest_dir = tmp_path / "manifest"
    m_path = install_chrome_native_host_manifest(
        extension_id="abcdefghijklmnopabcdefghijklmnop",
        config_path=cfg_path,
        manifest_dir=manifest_dir,
    )
    assert m_path.exists()
    assert "chrome-extension://abcdefghijklmnopabcdefghijklmnop/" in m_path.read_text(encoding="utf-8")


def test_cli_setup_and_help(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cli_host.json"
    help_proc = subprocess.run(
        [sys.executable, "-m", "host", "--help"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=True,
    )
    assert "setup" in help_proc.stdout and "share" in help_proc.stdout

    setup_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "host.cli",
            "setup",
            "--name",
            "CLI-PC",
            "--pin",
            "654321",
            "--config",
            str(cfg_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=True,
    )
    assert "Configured host 'CLI-PC'" in setup_proc.stdout
    raw_cfg = cfg_path.read_text(encoding="utf-8")
    assert "654321" not in raw_cfg  # Plaintext PIN must never be stored
    assert "srp_verifier" in raw_cfg


def test_codec_tuning_and_cursor_overlay() -> None:
    import numpy as np
    from aiortc.codecs import h264, vpx
    from host.capture import overlay_windows_cursor
    from host.codec_tuning import apply_high_quality_codec_tuning, boost_answer_sdp_bandwidth

    apply_high_quality_codec_tuning()
    assert h264.DEFAULT_BITRATE >= 6_000_000
    assert h264.MAX_BITRATE >= 12_000_000
    assert vpx.DEFAULT_BITRATE >= 4_000_000

    sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 97\r\nc=IN IP4 127.0.0.1\r\n"
    boosted = boost_answer_sdp_bandwidth(sdp, max_kbps=12000)
    assert "b=AS:12000" in boosted

    # Cursor overlay must never throw even on locked/headless desktops
    arr = np.zeros((120, 160, 4), dtype=np.uint8)
    overlay_windows_cursor(arr, 0, 0)

    mock = MockInputInjector()
    mock.type_text("Hello123!")
    assert mock.events[-1] == {"type": "type_text", "text": "Hello123!"}


def test_quick_actions_validation_and_telemetry() -> None:
    import pytest
    from host.quick_actions import (
        MockQuickActionExecutor,
        Win32QuickActionExecutor,
        validate_http_url,
    )

    assert validate_http_url("https://youtube.com") == "https://youtube.com"
    assert validate_http_url("youtube.com") == "https://youtube.com"
    assert validate_http_url("localhost:8080/ui") == "http://localhost:8080/ui"
    with pytest.raises(ValueError):
        validate_http_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        validate_http_url("javascript:123")
    with pytest.raises(ValueError):
        validate_http_url("file:///c:/windows/win.ini")
    with pytest.raises(ValueError):
        validate_http_url("data:text/html,hi")

    qa = MockQuickActionExecutor()
    ok_bare, _ = qa.execute("open_url", {"url": "youtube.com"})
    assert ok_bare is True
    ok_preset, _ = qa.execute("launch_preset", {"preset": "taskmgr"})
    assert ok_preset is True
    ok, _ = qa.execute("privacy_lock")
    assert ok and qa.get_system_status()["privacy_lock_active"] is True
    ok, _ = qa.execute("open_lock")
    assert ok and qa.get_system_status()["privacy_lock_active"] is False
    ok, _ = qa.execute("keep_awake_on")
    assert ok and qa.get_system_status()["keep_awake"] is True
    ok_bad, _ = qa.execute("unknown_xyz")
    assert ok_bad is False

    if sys.platform == "win32":
        win_qa = Win32QuickActionExecutor()
        try:
            st = win_qa.get_system_status()
            assert st["memory_total_gb"] > 0
            assert isinstance(st["windows_locked"], bool)
        finally:
            win_qa.close()


def test_privacy_curtain_local_emergency_unlock() -> None:
    import tkinter as tk
    from host.privacy_curtain import build_curtain_ui

    # 1. Verify clicking the Unlock button clears the curtain
    root1 = tk.Tk()
    root1.withdraw()
    st1 = build_curtain_ui(root1)
    assert st1["unlocked"] is False
    st1["unlock_btn"].invoke()  # type: ignore[union-attr]
    assert st1["unlocked"] is True

    # 2. Verify pressing Escape 3 times displays countdown hints and then unlocks cleanly
    root2 = tk.Tk()
    root2.withdraw()
    st2 = build_curtain_ui(root2)
    on_esc = st2["on_escape"]
    hint_var = st2["hint_var"]
    on_esc()  # type: ignore[operator]
    assert "2 more" in hint_var.get()  # type: ignore[union-attr]
    on_esc()  # type: ignore[operator]
    assert "1 more" in hint_var.get()  # type: ignore[union-attr]
    on_esc()  # type: ignore[operator]
    assert st2["unlocked"] is True


def test_webrtc_session_quick_action_exception_safety(tmp_path: Path) -> None:
    from host.config_store import PinRateLimiter, initialize_host_config
    from host.input_win32 import ClipboardManager
    from host.quick_actions import MockQuickActionExecutor
    from host.webrtc_session import HostPeerSession

    class _ExplodingQA(MockQuickActionExecutor):
        def execute(self, action: str, params: dict | None = None) -> tuple[bool, str]:
            raise OSError(22, "Elevated launch blocked")

    cfg_path = tmp_path / "qa_exc.json"
    cfg = initialize_host_config("ExcPC", "123456", path=cfg_path)
    sent: list[tuple[str, dict]] = []

    async def _noop_signal(_msg: dict) -> None:
        return None

    sess = HostPeerSession(
        session_id="s-exc",
        mode="me2me",
        config=cfg,
        config_path=cfg_path,
        capturer=SyntheticCapturer(640, 360),
        injector=MockInputInjector(),
        clipboard=ClipboardManager(mock=True),
        rate_limiter=PinRateLimiter(),
        ice_servers=[],
        send_signal=_noop_signal,
        quick_actions=_ExplodingQA(),
        control_only=True,
    )
    sess._authenticated = True
    sess._send_dc = lambda ch, data: sent.append((ch, data))  # type: ignore[method-assign]

    sess._dispatch_channel_message(
        "control",
        {"type": "quick_action", "action_id": "id-1", "action": "launch_preset", "params": {"preset": "taskmgr"}},
    )
    assert len(sent) == 1
    ch, payload = sent[0]
    assert ch == "control"
    assert payload["type"] == "quick_action_result"
    assert payload["ok"] is False
    assert "Elevated launch blocked" in payload["message"]
