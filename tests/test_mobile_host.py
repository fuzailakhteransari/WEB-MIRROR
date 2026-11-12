"""Tests for Android mobile host signaling, persistence, PWA manifest, and mobile E2E WSS mirror/touch."""

from __future__ import annotations

import asyncio
import io
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient
from PIL import Image
from playwright.async_api import async_playwright

from server.app import create_app
from server.config import ServerConfig
from server.state import SignalingState
from shared.srp import SRPServerSession, build_dtls_context, create_verifier


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_jpeg(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG", quality=75)
    return buf.getvalue()


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


def test_mobile_host_registration_persistence_and_manifest(tmp_path: Path) -> None:
    reg_path = tmp_path / "registry.json"
    cfg = ServerConfig(registry_path=reg_path)
    app = create_app(cfg)
    client = TestClient(app)

    caps = ["video", "input", "clipboard", "files", "mobile", "ws_direct"]
    with client.websocket_connect("/ws/host") as host_ws:
        host_ws.send_json(
            {
                "type": "register_host",
                "payload": {
                    "host_id": "android-test-1",
                    "name": "Galaxy-S24",
                    "platform": "android",
                    "mode": "me2me",
                    "monitors": [
                        {
                            "index": 1,
                            "name": "Phone Screen (1080x2340)",
                            "left": 0,
                            "top": 0,
                            "width": 1080,
                            "height": 2340,
                            "is_primary": True,
                        }
                    ],
                    "capabilities": caps,
                },
            }
        )
        assert host_ws.receive_json()["type"] == "host_registered"

        hosts = client.get("/api/hosts").json()["hosts"]
        assert len(hosts) == 1
        assert hosts[0]["platform"] == "android"
        assert hosts[0]["capabilities"] == caps

        # session_created echoes platform & capabilities
        with client.websocket_connect("/ws/client") as viewer_ws:
            viewer_ws.send_json(
                {
                    "type": "connect_host",
                    "host_id": "android-test-1",
                    "payload": {"sdp_offer": "", "ws_direct": True},
                }
            )
            created = viewer_ws.receive_json()
            assert created["type"] == "session_created"
            assert created["payload"]["platform"] == "android"
            assert "ws_direct" in created["payload"]["capabilities"]
            req = host_ws.receive_json()
            assert req["type"] == "connect_request"
            assert req["payload"]["ws_direct"] is True

    # Persistence preserves platform + capabilities
    reloaded = SignalingState(registry_path=reg_path)
    listed = reloaded.list_me2me_hosts()
    assert len(listed) == 1
    assert listed[0].platform == "android"
    assert listed[0].capabilities == caps
    assert listed[0].status == "offline"

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"

    apk_status = client.get("/api/phone-apk")
    assert apk_status.status_code == 200
    assert apk_status.json()["available"] is True
    assert apk_status.json()["download_url"] == "/downloads/webdesk-phone-host.apk"

    apk_download = client.get("/downloads/webdesk-phone-host.apk")
    assert apk_download.status_code == 200
    assert apk_download.headers["content-type"].startswith(
        "application/vnd.android.package-archive"
    )
    assert len(apk_download.content) > 1_000_000


@pytest.mark.asyncio
async def test_e2e_android_ws_direct_mirror_touch_nav_and_rotation() -> None:
    port = _free_port()
    host_id = "android-sim-1"
    pin = "987654"
    salt_hex, verifier_hex = create_verifier(host_id, pin)
    caps = ["video", "input", "clipboard", "files", "mobile", "ws_direct"]

    received_inputs: list[dict] = []
    received_actions: list[str] = []
    frame_dims = {"w": 540, "h": 1200, "color": (40, 110, 220)}
    ready_event = asyncio.Event()
    stop_event = asyncio.Event()

    async def sim_android_host() -> None:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/host") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register_host",
                        "payload": {
                            "host_id": host_id,
                            "name": "Pixel-Host",
                            "platform": "android",
                            "mode": "me2me",
                            "monitors": [
                                {
                                    "index": 1,
                                    "name": "Phone Screen (540x1200)",
                                    "left": 0,
                                    "top": 0,
                                    "width": 540,
                                    "height": 1200,
                                    "is_primary": True,
                                }
                            ],
                            "capabilities": caps,
                        },
                    }
                )
            )
            reg = json.loads(await ws.recv())
            assert reg["type"] == "host_registered"
            ready_event.set()

            srp_sess: SRPServerSession | None = None
            session_id = ""
            streaming = False
            stream_task: asyncio.Task | None = None

            async def push_frames() -> None:
                while streaming and not stop_event.is_set():
                    sid_b = session_id.encode("utf-8")
                    hdr = struct.pack(">H", len(sid_b)) + sid_b
                    jpg = _make_jpeg(frame_dims["w"], frame_dims["h"], frame_dims["color"])
                    await ws.send(hdr + jpg)
                    await asyncio.sleep(0.08)

            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                payload = msg.get("payload") or {}

                if mtype == "connect_request":
                    session_id = msg["session_id"]
                    ctx = build_dtls_context(f"ws:{session_id}", "ws")
                    srp_sess = SRPServerSession.start(host_id, salt_hex, verifier_hex, ctx)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "pake_challenge",
                                "session_id": session_id,
                                "payload": {
                                    "identity": host_id,
                                    "salt": srp_sess.salt_hex,
                                    "B": srp_sess.B_hex,
                                    "host_fp": f"ws:{session_id}",
                                    "viewer_fp": "ws",
                                    "ws_direct": True,
                                },
                            }
                        )
                    )
                elif mtype == "pake_client_proof":
                    assert srp_sess is not None
                    m2 = srp_sess.verify_client_proof(payload["A"], payload["M1"])
                    await ws.send(
                        json.dumps(
                            {
                                "type": "pake_server_proof",
                                "session_id": session_id,
                                "payload": {"M2": m2, "ws_direct": True},
                            }
                        )
                    )
                elif mtype == "enable_ws_fallback":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ws_fallback_ready",
                                "session_id": session_id,
                                "payload": {
                                    "platform": "android",
                                    "capabilities": caps,
                                    "monitors": [
                                        {
                                            "index": 1,
                                            "name": "Phone Screen (540x1200)",
                                            "left": 0,
                                            "top": 0,
                                            "width": 540,
                                            "height": 1200,
                                            "is_primary": True,
                                        }
                                    ],
                                    "active_monitor": 1,
                                },
                            }
                        )
                    )
                    streaming = True
                    stream_task = asyncio.create_task(push_frames())
                elif mtype == "ws_channel":
                    ch = payload.get("channel")
                    data = payload.get("data") or {}
                    if ch == "input":
                        received_inputs.append(data)
                    elif ch == "control" and data.get("type") == "system_status":
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "ws_channel",
                                    "session_id": session_id,
                                    "payload": {
                                        "channel": "control",
                                        "data": {
                                            "type": "system_status",
                                            "status": {
                                                "battery_percent": 82,
                                                "power_plugged": True,
                                                "ram_used_mb": 4200,
                                                "ram_total_mb": 8192,
                                                "screen_on": True,
                                                "device_locked": False,
                                                "accessibility_enabled": True,
                                                "network_type": "Wi-Fi",
                                            },
                                        },
                                    },
                                }
                            )
                        )
                    elif ch == "control" and data.get("type") == "quick_action":
                        act = data.get("action", "")
                        received_actions.append(act)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "ws_channel",
                                    "session_id": session_id,
                                    "payload": {
                                        "channel": "control",
                                        "data": {
                                            "type": "quick_action_result",
                                            "action_id": data.get("action_id"),
                                            "action": act,
                                            "ok": True,
                                            "message": f"Executed {act}",
                                        },
                                    },
                                }
                            )
                        )
                elif mtype == "session_end":
                    streaming = False
                    if stream_task:
                        stream_task.cancel()

            streaming = False
            if stream_task:
                stream_task.cancel()

    with _ServerRunner(port):
        host_task = asyncio.create_task(sim_android_host())
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=10.0)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    has_touch=True,
                )
                page = await context.new_page()
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

                await page.wait_for_selector('.host-card:has-text("Pixel-Host")')
                assert "Android Phone" in (
                    await page.text_content('.host-card:has-text("Pixel-Host")') or ""
                )

                await page.click('.host-card:has-text("Pixel-Host") .btn-connect')
                await page.fill("#pin-input", pin)
                await page.click("#btn-pin-submit")

                await page.wait_for_selector("#session-overlay", state="hidden", timeout=10000)
                await page.wait_for_selector("#fallback-canvas", state="visible", timeout=10000)
                await page.wait_for_selector("#mobile-nav-dock", state="visible", timeout=10000)

                await page.wait_for_function(
                    "() => { const c = document.getElementById('fallback-canvas'); return c && c.width === 540 && c.height === 1200; }",
                    timeout=10000,
                )

                # Nav dock Back button sends android_back
                await page.click('#mobile-nav-dock [data-combo="android_back"]')
                await asyncio.sleep(0.15)
                assert any(
                    ev.get("type") == "special_combo" and ev.get("combo") == "android_back"
                    for ev in received_inputs
                )

                # Mouse tap + swipe on mobile host canvas translates to touch_tap & touch_swipe
                box = await page.locator("#fallback-canvas").bounding_box()
                assert box is not None
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await asyncio.sleep(0.1)
                assert any(ev.get("type") == "touch_tap" for ev in received_inputs)

                await page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.7)
                await page.mouse.down()
                await page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.3, steps=5)
                await page.mouse.up()
                await asyncio.sleep(0.15)
                swipes = [ev for ev in received_inputs if ev.get("type") == "touch_swipe"]
                assert len(swipes) >= 1 and len(swipes[0]["points"]) >= 2

                # Simulate target phone rotating to landscape
                frame_dims["w"] = 1200
                frame_dims["h"] = 540
                await page.wait_for_function(
                    "() => { const c = document.getElementById('fallback-canvas'); return c && c.width === 1200 && c.height === 540; }",
                    timeout=10000,
                )

                # Quick Actions opens Android phone panel (not Windows panel)
                await page.click("#btn-quick-hud-actions")
                await page.wait_for_selector("#qa-android-cards", state="visible", timeout=5000)
                assert await page.is_hidden("#qa-windows-cards")
                await page.click('#qa-android-cards [data-action="find_phone_ring"]')
                await asyncio.sleep(0.15)
                assert "find_phone_ring" in received_actions

                await browser.close()
        finally:
            stop_event.set()
            await asyncio.wait_for(host_task, timeout=5.0)
