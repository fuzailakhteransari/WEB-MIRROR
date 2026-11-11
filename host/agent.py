"""Persistent host signaling daemon supporting Me2Me (unattended) and IT2Me (support) modes."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)

from host.capture import DesktopCapturer, MssDesktopCapturer, SyntheticCapturer
from host.config_store import DEFAULT_CONFIG_PATH, HostConfig, PinRateLimiter, load_host_config
from host.input_win32 import (
    ClipboardManager,
    InputInjector,
    MockInputInjector,
    Win32InputInjector,
)
from host.quick_actions import (
    MockQuickActionExecutor,
    QuickActionExecutor,
    Win32QuickActionExecutor,
)
from host.webrtc_session import HostPeerSession

ConsentFn = Callable[[str, str], Awaitable[bool]]


class HostAgent:
    """Maintains signaling WebSocket connection and spawns WebRTC peer sessions."""

    def __init__(
        self,
        config: HostConfig,
        config_path: Path | None = DEFAULT_CONFIG_PATH,
        mode: str = "me2me",
        capturer: DesktopCapturer | None = None,
        injector: InputInjector | None = None,
        clipboard: ClipboardManager | None = None,
        quick_actions: QuickActionExecutor | None = None,
        consent_callback: ConsentFn | None = None,
        on_support_code: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.mode = mode
        self.capturer = capturer or MssDesktopCapturer()
        if injector is not None:
            self.injector = injector
        elif sys.platform == "win32":
            self.injector = Win32InputInjector()
        else:
            self.injector = MockInputInjector()
        self.clipboard = clipboard or ClipboardManager()
        if quick_actions is not None:
            self.quick_actions = quick_actions
        elif sys.platform == "win32":
            self.quick_actions = Win32QuickActionExecutor()
        else:
            self.quick_actions = MockQuickActionExecutor()
        self.consent_callback = consent_callback
        self.on_support_code = on_support_code

        self.rate_limiter = PinRateLimiter()
        self.ice_servers: list[dict[str, Any]] = []
        self.sessions: dict[str, HostPeerSession] = {}
        self._support_pin: str | None = None
        self._ws: Any = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self.support_code: str | None = None

    def _ws_url(self) -> str:
        url = self.config.server_url
        if self.config.workspace_token:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}token={self.config.workspace_token}"
        return url

    async def send_signal(self, msg: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(msg))

    async def send_binary(self, payload: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(payload)

    def stop(self) -> None:
        self._stop_event.set()

    async def wait_ready(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    self._ws_url(),
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    monitors = [m.model_dump() for m in self.capturer.get_monitors()]
                    await self.send_signal(
                        {
                            "type": "register_host",
                            "payload": {
                                "host_id": self.config.host_id,
                                "name": self.config.host_name,
                                "platform": platform.system().lower(),
                                "mode": self.mode,
                                "monitors": monitors,
                                "capabilities": ["video", "input", "clipboard", "files"],
                            },
                        }
                    )
                    hb_task = asyncio.create_task(self._heartbeat_loop())
                    try:
                        await self._read_loop(ws)
                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("Host signaling loop error (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(5.0, backoff * 1.5)
            finally:
                self._ws = None
                await self._close_all_sessions()

        self.capturer.close()
        self.quick_actions.close()

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(15.0)
            if self._ws is not None:
                await self.send_signal(
                    {
                        "type": "heartbeat",
                        "payload": {"monitors": [m.model_dump() for m in self.capturer.get_monitors()]},
                    }
                )

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            if self._stop_event.is_set():
                break
            msg = json.loads(raw)
            msg_type = msg.get("type")
            payload = msg.get("payload") or {}

            if msg_type == "host_registered":
                self.ice_servers = list(payload.get("iceServers") or [])
                if self.mode == "it2me":
                    await self.send_signal({"type": "create_support_code", "payload": {}})
                else:
                    self._ready_event.set()

            elif msg_type == "support_code_created":
                code = str(payload.get("code") or "")
                formatted = str(payload.get("formatted") or code)
                expires_at = float(payload.get("expires_at") or 0.0)
                self._support_pin = code
                self.support_code = code
                self._ready_event.set()
                if self.on_support_code:
                    self.on_support_code(code, formatted, expires_at)

            elif msg_type == "connect_request":
                await self._handle_connect_request(msg)

            elif msg_type in {
                "pake_client_proof",
                "pairing_client_proof",
                "ice_candidate",
                "enable_ws_fallback",
                "ws_channel",
                "ws_frame_ack",
                "session_end",
            }:
                sid = msg.get("session_id")
                session = self.sessions.get(sid or "")
                if session:
                    await session.handle_signal_message(msg)
                elif msg_type != "ice_candidate":
                    logger.warning("Ignoring %s for unknown/closed session %s", msg_type, sid)

    async def _handle_connect_request(self, msg: dict[str, Any]) -> None:
        session_id = str(msg.get("session_id") or "")
        payload = msg.get("payload") or {}
        mode = str(payload.get("mode") or self.mode)
        viewer_label = str(payload.get("viewer_label") or "Viewer")

        if mode == "it2me" and self.consent_callback is not None:
            approved = await self.consent_callback(viewer_label, session_id)
            if not approved:
                await self.send_signal(
                    {
                        "type": "session_reject",
                        "session_id": session_id,
                        "payload": {"reason": "Host user declined remote support request"},
                    }
                )
                return

        control_only = bool(payload.get("control_only", False))
        session = HostPeerSession(
            session_id=session_id,
            mode=mode,
            config=self.config,
            config_path=self.config_path,
            capturer=self.capturer,
            injector=self.injector,
            clipboard=self.clipboard,
            rate_limiter=self.rate_limiter,
            ice_servers=self.ice_servers,
            send_signal=self.send_signal,
            send_binary=self.send_binary,
            support_pin=self._support_pin,
            on_closed=lambda sid: self.sessions.pop(sid, None),
            quick_actions=self.quick_actions,
            control_only=control_only,
        )
        self.sessions[session_id] = session
        try:
            await session.start_from_offer(
                sdp_offer=str(payload.get("sdp_offer") or ""),
                pairing_id=payload.get("pairing_id"),
                control_only=control_only,
            )
        except Exception as exc:
            logger.exception("Failed starting WebRTC session %s: %s", session_id, exc)
            await session.reject(f"Host WebRTC initialization error: {exc}")

    async def _close_all_sessions(self) -> None:
        for sid in list(self.sessions.keys()):
            sess = self.sessions.pop(sid, None)
            if sess:
                await sess.close()


def create_ephemeral_support_agent(
    server_url: str = "ws://127.0.0.1:8080/ws/host",
    workspace_token: str = "",
    host_name: str | None = None,
    mock_desktop: bool = False,
    auto_accept: bool = False,
    on_support_code: Callable[[str, str, float], None] | None = None,
) -> HostAgent:
    """Build an ephemeral IT2Me host agent without modifying persistent Me2Me config."""
    cfg = HostConfig(
        host_id=f"it2me-{uuid.uuid4()}",
        host_name=host_name or f"{platform.node()}-Support",
        server_url=server_url,
        workspace_token=workspace_token,
    )

    async def consent(viewer_label: str, _sid: str) -> bool:
        if auto_accept:
            return True
        loop = asyncio.get_running_loop()
        ans = await loop.run_in_executor(
            None,
            input,
            f"\n[IT2Me] '{viewer_label}' wants to view and control your PC. Allow? [y/N]: ",
        )
        return ans.strip().lower() in {"y", "yes"}

    capturer: DesktopCapturer = SyntheticCapturer() if mock_desktop else MssDesktopCapturer()
    injector: InputInjector = MockInputInjector() if mock_desktop else (
        Win32InputInjector() if sys.platform == "win32" else MockInputInjector()
    )
    clipboard = ClipboardManager(mock=mock_desktop)
    return HostAgent(
        config=cfg,
        config_path=None,
        mode="it2me",
        capturer=capturer,
        injector=injector,
        clipboard=clipboard,
        consent_callback=consent,
        on_support_code=on_support_code,
    )


async def run_agent_until_stopped(agent: HostAgent) -> None:
    try:
        await agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
        await agent._close_all_sessions()
