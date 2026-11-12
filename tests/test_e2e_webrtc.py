"""End-to-end Playwright Chromium <-> FastAPI Signaling <-> aiortc Host WebRTC tests."""

from __future__ import annotations

import asyncio
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright

from host.agent import HostAgent, create_ephemeral_support_agent
from host.capture import SyntheticCapturer
from host.config_store import initialize_host_config
from host.input_win32 import ClipboardManager, MockInputInjector
from server.app import create_app
from server.config import ServerConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ServerRunner:
    def __init__(self, port: int) -> None:
        self.port = port
        cfg = ServerConfig(host="127.0.0.1", port=port, ice_servers=[])
        self.app = create_app(cfg)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_ServerRunner":
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("Uvicorn test server failed to start")
        return self

    def __exit__(self, *_args) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)


@pytest.mark.asyncio
async def test_e2e_me2me_wrong_pin_right_pin_pairing_input_clipboard_file(tmp_path: Path) -> None:
    if sys.platform == "win32":
        # Note: pytest-asyncio uses default policy; ensure clean socket ops
        pass

    port = _free_port()
    cfg_path = tmp_path / "host_config.json"
    cfg = initialize_host_config(
        host_name="E2E-Desktop",
        pin="246810",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    cfg.receive_dir = str(tmp_path / "received")

    capturer = SyntheticCapturer(640, 360)
    injector = MockInputInjector()
    clipboard = ClipboardManager(mock=True)
    agent = HostAgent(
        config=cfg,
        config_path=cfg_path,
        mode="me2me",
        capturer=capturer,
        injector=injector,
        clipboard=clipboard,
    )

    with _ServerRunner(port):
        agent_task = asyncio.create_task(agent.run_forever())
        try:
            await agent.wait_ready(timeout=10.0)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()

                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                await page.wait_for_selector('.host-card:has-text("E2E-Desktop")')

                # 1. Wrong PIN should be rejected without starting video capture
                await page.click('.host-card:has-text("E2E-Desktop") .btn-connect')
                await page.fill("#pin-input", "000000")
                await page.click("#btn-pin-submit")
                await page.wait_for_selector("#pin-error", state="visible", timeout=10000)
                assert "Invalid PIN" in (await page.text_content("#pin-error") or "")
                assert capturer._tick == 0  # Zero frames captured on failed auth

                # 2. Correct PIN + Remember Device connects WebRTC & decodes real video frames
                await page.fill("#pin-input", "246810")
                await page.check("#remember-pin-checkbox")
                await page.click("#btn-pin-submit")

                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_function(
                    """() => {
                        const v = document.getElementById('remote-video');
                        return v && v.videoWidth > 0 && v.videoHeight > 0;
                    }""",
                    timeout=15000,
                )
                dims = await page.evaluate(
                    "() => ({ w: document.getElementById('remote-video').videoWidth, h: document.getElementById('remote-video').videoHeight })"
                )
                assert dims["w"] == 640 and dims["h"] == 360

                # Wait briefly for multiple frames to decode
                await asyncio.sleep(0.4)
                assert capturer._tick >= 2

                # 3. Mouse & keyboard input over RTCDataChannel
                video_box = await page.locator("#remote-video").bounding_box()
                assert video_box is not None
                await page.mouse.click(
                    video_box["x"] + video_box["width"] * 0.5,
                    video_box["y"] + video_box["height"] * 0.5,
                )
                await page.keyboard.press("KeyH")
                await asyncio.sleep(0.2)
                assert any(e["type"] == "mouse_button" and e["down"] for e in injector.events)
                assert any(e["type"] == "key_event" and e["code"] == "KeyH" for e in injector.events)

                # 4. Open sidebar, sync clipboard (send & pull), special combo, monitor switch, and transfer file
                await page.click("#btn-toggle-sidebar")
                await page.fill("#clipboard-box", "Hello from Chromium Viewer!")
                await page.click("#btn-clipboard-send")
                await asyncio.sleep(0.2)
                assert clipboard.get_text() == "Hello from Chromium Viewer!"

                clipboard.set_text("Host Clipboard Reply")
                await page.click("#btn-clipboard-pull")
                await page.wait_for_function(
                    "() => document.getElementById('clipboard-box').value === 'Host Clipboard Reply'",
                    timeout=5000,
                )

                await page.click('.combo-btn[data-combo="win_d"]')
                await page.fill("#paste-text-input", "DirectUnicode123!")
                await page.click("#btn-paste-text")
                await page.select_option("#select-quality", "low")
                await page.select_option("#select-monitor", "2")
                await page.wait_for_function(
                    "() => document.getElementById('remote-video').videoWidth === 960",
                    timeout=10000,
                )
                assert any(e["type"] == "special_combo" and e["combo"] == "win_d" for e in injector.events)
                assert any(e["type"] == "type_text" and e["text"] == "DirectUnicode123!" for e in injector.events)

                sample_bytes = b"WebDesk Remote WebRTC File Transfer Payload " * 256
                sample_file = tmp_path / "payload.txt"
                sample_file.write_bytes(sample_bytes)
                await page.set_input_files("#file-input", str(sample_file))
                await page.click("#btn-upload-file")
                await page.wait_for_function(
                    "() => (document.getElementById('file-progress').textContent || '').includes('Saved on host: payload.txt')",
                    timeout=10000,
                )
                saved_path = tmp_path / "received" / "payload.txt"
                assert saved_path.exists()
                assert hashlib.sha256(saved_path.read_bytes()).hexdigest() == hashlib.sha256(sample_bytes).hexdigest()

                # 5. Disconnect, reload page, and reconnect WITHOUT entering PIN (Paired flow)
                await page.click("#btn-disconnect")
                await page.wait_for_selector("#portal-view", state="visible", timeout=10000)
                await page.reload(wait_until="networkidle")
                await page.wait_for_selector('.host-card:has-text("Connect (Paired)")', timeout=10000)
                await page.click('.host-card:has-text("E2E-Desktop") .btn-connect')

                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_function(
                    "() => document.getElementById('remote-video').videoWidth > 0",
                    timeout=15000,
                )
                await page.click("#btn-toggle-sidebar")
                await page.click("#btn-disconnect")
                await page.wait_for_selector("#portal-view", state="visible", timeout=10000)

                # 6. Revoked pairing on host cleanly falls back to PIN prompt
                cfg.pairing_registry.clear()
                await page.click('.host-card:has-text("E2E-Desktop") .btn-connect')
                await page.wait_for_selector("#pin-modal", state="visible", timeout=10000)
                await page.wait_for_selector("#pin-error", state="visible", timeout=10000)
                await page.click("#btn-pin-cancel")
                await browser.close()
        finally:
            agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_e2e_it2me_support_code_session(tmp_path: Path) -> None:
    port = _free_port()
    support_agent = create_ephemeral_support_agent(
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        host_name="Helper-Target",
        mock_desktop=True,
        auto_accept=True,
    )

    with _ServerRunner(port):
        agent_task = asyncio.create_task(support_agent.run_forever())
        try:
            await support_agent.wait_ready(timeout=10.0)
            code = support_agent.support_code
            assert code is not None and len(code) == 12

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                # IT2Me host must not show in Remote Access directory
                assert await page.locator('.host-card:has-text("Helper-Target")').count() == 0

                await page.click("#tab-support")
                await page.fill("#support-code-input", code)
                await page.click("#btn-connect-support")

                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_function(
                    "() => document.getElementById('remote-video').videoWidth > 0",
                    timeout=15000,
                )
                await page.click("#btn-toggle-sidebar")
                await page.click("#btn-disconnect")
                await page.wait_for_selector("#portal-view", state="visible", timeout=10000)

                # Reusing the same single-use 12-digit code must fail
                await page.fill("#support-code-input", code)
                await page.click("#btn-connect-support")
                await page.wait_for_selector("#support-error", state="visible", timeout=10000)
                await browser.close()
        finally:
            support_agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_e2e_mobile_touch_trackpad_and_soft_keyboard(tmp_path: Path) -> None:
    port = _free_port()
    cfg_path = tmp_path / "mobile_host.json"
    cfg = initialize_host_config(
        host_name="Mobile-Target",
        pin="778899",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    injector = MockInputInjector()
    agent = HostAgent(
        config=cfg,
        config_path=cfg_path,
        mode="me2me",
        capturer=SyntheticCapturer(640, 360),
        injector=injector,
        clipboard=ClipboardManager(mock=True),
    )

    with _ServerRunner(port):
        agent_task = asyncio.create_task(agent.run_forever())
        try:
            await agent.wait_ready(timeout=10.0)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    has_touch=True,
                    is_mobile=True,
                )
                page = await context.new_page()
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                await page.click('.host-card:has-text("Mobile-Target") .btn-connect')
                await page.fill("#pin-input", "778899")
                await page.click("#btn-pin-submit")
                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_function(
                    "() => document.getElementById('remote-video').videoWidth > 0",
                    timeout=15000,
                )

                # Touch devices default to one-finger scroll with tap-to-click.
                assert await page.text_content("#btn-quick-mode") == "Scroll"
                await page.wait_for_selector("#trackpad-cursor", state="hidden", timeout=5000)
                mobile_controls = await page.evaluate(
                    """() => {
                        const stage = document.getElementById('stage');
                        const video = document.getElementById('remote-video');
                        const hud = document.getElementById('hud-pill');
                        const actions = document.getElementById('btn-quick-hud-actions').getBoundingClientRect();
                        return {
                            stageTouchAction: getComputedStyle(stage).touchAction,
                            videoTouchAction: getComputedStyle(video).touchAction,
                            hudTouchAction: getComputedStyle(hud).touchAction,
                            actionsVisible: actions.left >= 0 && actions.right <= innerWidth,
                        };
                    }"""
                )
                assert mobile_controls == {
                    "stageTouchAction": "auto",
                    "videoTouchAction": "none",
                    "hudTouchAction": "pan-x",
                    "actionsVisible": True,
                }

                before_scroll = len(injector.events)
                await page.evaluate(
                    """() => {
                        const v = document.getElementById('remote-video');
                        const r = v.getBoundingClientRect();
                        const x = r.left + r.width / 2;
                        const mk = y => new Touch({identifier: 41, target: v, clientX: x, clientY: y, pageX: x, pageY: y});
                        const a = mk(r.top + 60), b = mk(r.top + 160);
                        const fire = (type, touches, changed) => v.dispatchEvent(new TouchEvent(type, {
                            bubbles: true, cancelable: true, touches, targetTouches: touches, changedTouches: [changed]
                        }));
                        fire('touchstart', [a], a);
                        fire('touchmove', [b], b);
                        fire('touchend', [], b);
                    }"""
                )
                scroll_events = injector.events[before_scroll:]
                assert any(e["type"] == "mouse_wheel" and abs(e["dy"]) >= 3.9 for e in scroll_events)
                assert not any(e["type"] == "mouse_button" for e in scroll_events)

                before_tap = len(injector.events)
                await page.evaluate(
                    """() => {
                        const v = document.getElementById('remote-video');
                        const r = v.getBoundingClientRect();
                        const x = r.left + r.width / 2, y = r.top + r.height / 2;
                        const t = new Touch({identifier: 42, target: v, clientX: x, clientY: y, pageX: x, pageY: y});
                        const fire = (type, touches) => v.dispatchEvent(new TouchEvent(type, {
                            bubbles: true, cancelable: true, touches, targetTouches: touches, changedTouches: [t]
                        }));
                        fire('touchstart', [t]);
                        fire('touchend', []);
                    }"""
                )
                tap_events = injector.events[before_tap:]
                assert [e["down"] for e in tap_events if e["type"] == "mouse_button"] == [True, False]

                # Direct Touch pinch begins with one finger down; no click may leak to the host.
                await page.click("#btn-quick-mode")
                assert await page.text_content("#btn-quick-mode") == "Touch"
                before_pinch = len(injector.events)
                await page.evaluate(
                    """() => {
                        const v = document.getElementById('remote-video');
                        const r = v.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                        const mk = (id, x) => new Touch({identifier: id, target: v, clientX: x, clientY: cy, pageX: x, pageY: cy});
                        const first = mk(1, cx - 30), second = mk(2, cx + 30);
                        const fire = (type, touches, changed) => v.dispatchEvent(new TouchEvent(type, {
                            bubbles: true, cancelable: true, touches, targetTouches: touches, changedTouches: changed
                        }));
                        fire('touchstart', [first], [first]);
                        fire('touchstart', [first, second], [second]);
                        for (const half of [45, 60, 75, 90]) {
                            fire('touchmove', [mk(1, cx - half), mk(2, cx + half)], [mk(1, cx - half), mk(2, cx + half)]);
                        }
                        fire('touchend', [], [first, second]);
                    }"""
                )
                await page.wait_for_function(
                    "() => (document.getElementById('remote-video').style.transform || '').includes('scale(')",
                    timeout=5000,
                )
                assert not any(
                    e["type"] in {"mouse_button", "touch_tap"}
                    for e in injector.events[before_pinch:]
                )

                # Mobile soft keyboard button + typing forwards text
                await page.click("#btn-quick-keyboard")
                await page.fill("#mobile-kb-input", "PhoneHello")
                await asyncio.sleep(0.2)
                assert any(e["type"] == "type_text" and e["text"] == "PhoneHello" for e in injector.events)

                # Cycle Touch -> Trackpad -> Desktop Mouse cleanly
                await page.click("#btn-quick-mode")
                assert await page.text_content("#btn-quick-mode") == "Trackpad"
                await page.click("#btn-quick-mode")
                assert await page.text_content("#btn-quick-mode") == "Mouse"
                await page.click("#btn-quick-disconnect")
                await browser.close()
        finally:
            agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_e2e_ws_relay_fallback_when_p2p_blocked(tmp_path: Path) -> None:
    port = _free_port()
    cfg_path = tmp_path / "fallback_host.json"
    cfg = initialize_host_config(
        host_name="Fallback-PC",
        pin="112233",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    cfg.receive_dir = str(tmp_path / "received")
    injector = MockInputInjector()
    clipboard = ClipboardManager(mock=True)
    agent = HostAgent(
        config=cfg,
        config_path=cfg_path,
        mode="me2me",
        capturer=SyntheticCapturer(640, 360),
        injector=injector,
        clipboard=clipboard,
    )

    with _ServerRunner(port):
        agent_task = asyncio.create_task(agent.run_forever())
        try:
            await agent.wait_ready(timeout=10.0)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    has_touch=True,
                    is_mobile=True,
                )
                page = await context.new_page()
                # Simulate mobile CGNAT blocking P2P UDP so automatic WSS Relay fallback engages
                await page.add_init_script("window.__WEBDESK_FORCE_WS_FALLBACK = true;")
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                await page.click('.host-card:has-text("Fallback-PC") .btn-connect')
                await page.fill("#pin-input", "112233")
                await page.click("#btn-pin-submit")

                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_selector("#fallback-canvas", state="visible", timeout=10000)
                await page.wait_for_function(
                    """() => {
                        const c = document.getElementById('fallback-canvas');
                        return c && c.width === 640 && c.height === 360;
                    }""",
                    timeout=10000,
                )
                assert "WSS Relay" in (await page.text_content("#hud-transport") or "")

                # Verify input, clipboard, and file transfer work over WSS Relay
                await page.click("#btn-quick-keyboard")
                await page.fill("#mobile-kb-input", "RelayKeyboard")
                await asyncio.sleep(0.2)
                assert any(e["type"] == "type_text" and e["text"] == "RelayKeyboard" for e in injector.events)

                await page.click("#btn-quick-sidebar")
                await page.fill("#clipboard-box", "Relay Clipboard")
                await page.click("#btn-clipboard-send")
                await asyncio.sleep(0.2)
                assert clipboard.get_text() == "Relay Clipboard"

                sample_bytes = b"WSS Relay File Transfer " * 128
                sample_file = tmp_path / "relay.txt"
                sample_file.write_bytes(sample_bytes)
                await page.set_input_files("#file-input", str(sample_file))
                await page.click("#btn-upload-file")
                await page.wait_for_function(
                    "() => (document.getElementById('file-progress').textContent || '').includes('Saved on host: relay.txt')",
                    timeout=10000,
                )
                assert (tmp_path / "received" / "relay.txt").read_bytes() == sample_bytes

                await page.click("#btn-disconnect")
                await browser.close()
        finally:
            agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_e2e_ws_relay_fallback_when_datachannel_stalled_after_connected(
    tmp_path: Path,
) -> None:
    port = _free_port()
    cfg_path = tmp_path / "stalled_dc_host.json"
    cfg = initialize_host_config(
        host_name="Stalled-DC-PC",
        pin="445566",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    injector = MockInputInjector()
    agent = HostAgent(
        config=cfg,
        config_path=cfg_path,
        mode="me2me",
        capturer=SyntheticCapturer(640, 360),
        injector=injector,
        clipboard=ClipboardManager(mock=True),
    )

    with _ServerRunner(port):
        agent_task = asyncio.create_task(agent.run_forever())
        try:
            await agent.wait_ready(timeout=10.0)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    has_touch=True,
                    is_mobile=True,
                )
                page = await context.new_page()
                await page.add_init_script(
                    """
                    window.__WEBDESK_ICE_TIMEOUT_MS = 1500;
                    const origCreateDC = RTCPeerConnection.prototype.createDataChannel;
                    const origOnMsgDesc = Object.getOwnPropertyDescriptor(RTCDataChannel.prototype, 'onmessage');
                    RTCPeerConnection.prototype.createDataChannel = function(label, opts) {
                      const dc = origCreateDC.call(this, label, opts);
                      if (label === 'control' && origOnMsgDesc && origOnMsgDesc.set) {
                        Object.defineProperty(dc, 'onmessage', {
                          configurable: true,
                          set(fn) {
                            origOnMsgDesc.set.call(dc, (ev) => {
                              try {
                                const msg = JSON.parse(ev.data);
                                if (msg.type === 'session_ready') return;
                              } catch {}
                              return fn ? fn.call(dc, ev) : undefined;
                            });
                          },
                        });
                      }
                      return dc;
                    };
                    """
                )
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                await page.click('.host-card:has-text("Stalled-DC-PC") .btn-connect')
                await page.fill("#pin-input", "445566")
                await page.click("#btn-pin-submit")

                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_selector("#fallback-canvas", state="visible", timeout=10000)
                await page.wait_for_function(
                    """() => {
                        const c = document.getElementById('fallback-canvas');
                        return c && c.width === 640 && c.height === 360;
                    }""",
                    timeout=10000,
                )
                assert "WSS Relay" in (await page.text_content("#hud-transport") or "")

                await page.click("#btn-quick-disconnect")
                await browser.close()
        finally:
            agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_e2e_quick_actions_without_mirroring_and_upgrade_to_mirror(
    tmp_path: Path,
) -> None:
    from host.quick_actions import MockQuickActionExecutor

    port = _free_port()
    cfg_path = tmp_path / "qa_host.json"
    cfg = initialize_host_config(
        host_name="QuickActions-PC",
        pin="246810",
        server_url=f"ws://127.0.0.1:{port}/ws/host",
        path=cfg_path,
    )
    qa_exec = MockQuickActionExecutor()
    agent = HostAgent(
        config=cfg,
        config_path=cfg_path,
        mode="me2me",
        capturer=SyntheticCapturer(640, 360),
        injector=MockInputInjector(),
        clipboard=ClipboardManager(mock=True),
        quick_actions=qa_exec,
    )

    with _ServerRunner(port):
        agent_task = asyncio.create_task(agent.run_forever())
        try:
            await agent.wait_ready(timeout=10.0)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    has_touch=True,
                    is_mobile=True,
                )
                page = await context.new_page()
                page.on("dialog", lambda dlg: asyncio.create_task(dlg.accept()))
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                # Connect directly in Quick Actions mode (no video mirroring)
                await page.click('.host-card:has-text("QuickActions-PC") .btn-quick-actions')
                await page.fill("#pin-input", "246810")
                await page.click("#btn-pin-submit")

                await page.wait_for_selector("#quick-actions-view", state="visible", timeout=10000)
                assert await page.is_hidden("#session-view")
                assert await page.evaluate("() => document.getElementById('remote-video').srcObject === null")

                assert "CPU:" in (await page.text_content("#qa-stat-cpu") or "")
                assert "Unlocked" in (await page.text_content("#qa-stat-lock") or "")

                # Lock -> Open Lock -> Media -> Power -> Open URL -> Snapshot
                await page.click('.qa-action-btn[data-action="privacy_lock"]')
                await page.wait_for_function(
                    "() => (document.getElementById('qa-stat-lock').textContent || '').includes('Privacy Shield')",
                    timeout=5000,
                )
                await page.click('.qa-action-btn[data-action="open_lock"]')
                await page.wait_for_function(
                    "() => (document.getElementById('qa-stat-lock').textContent || '').includes('Unlocked')",
                    timeout=5000,
                )

                await page.click('.qa-action-btn[data-action="vol_up"]')
                await page.fill("#qa-url-input", "https://example.com")
                await page.click("#qa-btn-open-url")
                await page.click('.qa-action-btn[data-action="shutdown"]')
                await page.click('.qa-action-btn[data-action="abort_shutdown"]')
                await page.click("#qa-btn-snapshot")
                await page.wait_for_selector("#qa-snapshot-img", state="visible", timeout=10000)
                snap_src = await page.get_attribute("#qa-snapshot-img", "src") or ""
                assert snap_src.startswith("data:image/jpeg;base64,")

                actions_seen = [item["action"] for item in qa_exec.history]
                assert "privacy_lock" in actions_seen
                assert "open_lock" in actions_seen
                assert "vol_up" in actions_seen
                assert "open_url" in actions_seen
                assert "shutdown" in actions_seen
                assert "abort_shutdown" in actions_seen

                # 1-tap upgrade from Quick Actions into live WebRTC Screen Mirroring
                await page.click("#qa-btn-mirror")
                await page.wait_for_selector("#session-overlay", state="hidden", timeout=15000)
                await page.wait_for_function(
                    "() => document.getElementById('remote-video').videoWidth === 640",
                    timeout=15000,
                )

                # Toggle Quick Actions mid-mirroring and return cleanly
                await page.click("#btn-quick-hud-actions")
                await page.wait_for_selector("#quick-actions-view", state="visible", timeout=5000)
                assert await page.text_content("#qa-btn-mirror") == "Back to Mirror"
                await page.click("#qa-btn-mirror")
                assert await page.is_hidden("#quick-actions-view")

                await page.click("#btn-quick-disconnect")
                await browser.close()
        finally:
            agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass
