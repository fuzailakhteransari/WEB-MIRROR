"""Per-session WebRTC PeerConnection, PAKE/Pairing gate, media track, and DataChannel handlers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from host.capture import QUALITY_PROFILES, DesktopCapturer, DesktopVideoTrack
from host.codec_tuning import (
    apply_high_quality_codec_tuning,
    boost_answer_sdp_bandwidth,
    set_video_sender_bitrate,
)
from host.config_store import (
    HostConfig,
    PinRateLimiter,
    create_pairing_entry,
    get_pairing_auth_key,
)
from host.input_win32 import ClipboardManager, InputInjector
from host.quick_actions import (
    MockQuickActionExecutor,
    QuickActionExecutor,
    Win32QuickActionExecutor,
)
from shared.protocol import MonitorInfo
from shared.srp import (
    SRPAuthError,
    SRPServerSession,
    build_dtls_context,
    create_verifier,
    extract_dtls_fingerprint,
    verify_pairing_hmac,
)

SendSignalFn = Callable[[dict[str, Any]], Awaitable[None]]
SendBinaryFn = Callable[[bytes], Awaitable[None]]

logger = logging.getLogger(__name__)


def _build_rtc_config(ice_servers_raw: list[dict[str, Any]]) -> RTCConfiguration:
    servers: list[RTCIceServer] = []
    for item in ice_servers_raw:
        urls = item.get("urls")
        if not urls:
            continue
        servers.append(
            RTCIceServer(
                urls=urls,
                username=item.get("username"),
                credential=item.get("credential"),
            )
        )
    return RTCConfiguration(iceServers=servers)


class HostPeerSession:
    """Manages one authenticated WebRTC remote desktop session on the host."""

    def __init__(
        self,
        session_id: str,
        mode: str,
        config: HostConfig,
        config_path: Path | None,
        capturer: DesktopCapturer,
        injector: InputInjector,
        clipboard: ClipboardManager,
        rate_limiter: PinRateLimiter,
        ice_servers: list[dict[str, Any]],
        send_signal: SendSignalFn,
        send_binary: SendBinaryFn | None = None,
        support_pin: str | None = None,
        on_closed: Callable[[str], None] | None = None,
        quick_actions: QuickActionExecutor | None = None,
        control_only: bool = False,
    ) -> None:
        self.session_id = session_id
        self.mode = mode
        self.config = config
        self.config_path = config_path
        self.capturer = capturer
        self.injector = injector
        self.clipboard = clipboard
        self.rate_limiter = rate_limiter
        self.send_signal = send_signal
        self.send_binary = send_binary
        self.support_pin = support_pin
        self.on_closed = on_closed
        self.quick_actions = quick_actions or MockQuickActionExecutor()
        self._control_only = control_only

        apply_high_quality_codec_tuning()
        self.pc: RTCPeerConnection | None = (
            None
            if control_only
            else RTCPeerConnection(configuration=_build_rtc_config(ice_servers))
        )
        self._auth_event = asyncio.Event()
        self._authenticated = False
        self._closed = False
        self._srp_session: SRPServerSession | None = None
        self._pairing_nonce: str | None = None
        self._pairing_id_pending: str | None = None
        self._dtls_context: str = ""
        self._pending_pairing_grant: tuple[str, str] | None = None
        self._post_auth_sent = False

        # WebSocket TLS Relay Fallback state (also used for control_only Quick Actions)
        self._ws_fallback_active = control_only
        self._switching_to_fallback = False
        self._fallback_task: asyncio.Task[None] | None = None
        self._fallback_executor: ThreadPoolExecutor | None = None
        self._snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-snap")
        self._ws_quality = "auto"
        self._ws_sent_seq = 0
        self._ws_acked_seq = 0
        self._ws_ack_event = asyncio.Event()

        self._monitors: list[MonitorInfo] = capturer.get_monitors()
        primary = next((m for m in self._monitors if m.is_primary), self._monitors[0])
        self._active_monitor: MonitorInfo = primary
        self._video_track = DesktopVideoTrack(
            capturer=capturer,
            auth_event=self._auth_event,
            monitor_index=primary.index,
        )
        self._channels: dict[str, Any] = {}
        self._incoming_files: dict[str, dict[str, Any]] = {}

        if self.pc is not None:
            self._wire_pc_events()

    def _wire_pc_events(self) -> None:
        @self.pc.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            label = channel.label
            state = getattr(channel, "readyState", None)
            logger.info("Session %s datachannel received: %s state=%s", self.session_id, label, state)
            self._channels[label] = channel

            @channel.on("open")
            def on_open() -> None:
                logger.info("Session %s datachannel opened: %s", self.session_id, label)
                if label == "control":
                    self._maybe_send_post_auth_control()

            @channel.on("message")
            def on_message(message: str | bytes) -> None:
                if not self._authenticated:
                    return
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    return
                self._dispatch_channel_message(label, data)

            if label == "control" and state == "open":
                self._maybe_send_post_auth_control()

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            logger.info("Session %s WebRTC connectionState=%s", self.session_id, self.pc.connectionState)
            if self._switching_to_fallback or self._ws_fallback_active:
                return
            if self.pc.connectionState == "connected":
                self._maybe_send_post_auth_control()
            elif self.pc.connectionState == "closed":
                await self.close()
            elif self.pc.connectionState == "failed" and not self._authenticated:
                await self.close()

    async def start_from_offer(
        self, sdp_offer: str, pairing_id: str | None = None, control_only: bool | None = None
    ) -> None:
        """Process viewer SDP offer (or lightweight control_only session) and issue auth challenge."""
        if control_only is not None and control_only != self._control_only:
            self._control_only = control_only
            if control_only:
                self._ws_fallback_active = True

        locked, wait_sec = self.rate_limiter.is_locked()
        if locked:
            await self.send_signal(
                {
                    "type": "session_reject",
                    "session_id": self.session_id,
                    "payload": {
                        "reason": f"Too many wrong PIN attempts. Retry in {wait_sec}s."
                    },
                }
            )
            await self.close()
            return

        if self._control_only:
            host_fp = f"ws:{self.session_id}"
            viewer_fp = "ws"
            self._dtls_context = build_dtls_context(host_fp, viewer_fp)
            logger.info(
                "Session %s starting control-only Quick Actions (mode=%s, pairing_requested=%s)",
                self.session_id,
                self.mode,
                bool(pairing_id),
            )
            await self._issue_auth_challenge(host_fp, viewer_fp, pairing_id)
            return

        if not sdp_offer or self.pc is None:
            await self.send_signal(
                {
                    "type": "session_reject",
                    "session_id": self.session_id,
                    "payload": {"reason": "Missing SDP offer"},
                }
            )
            await self.close()
            return

        viewer_fp = extract_dtls_fingerprint(sdp_offer)
        self.pc.addTrack(self._video_track)
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp_offer, type="offer"))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        answer_sdp = boost_answer_sdp_bandwidth(self.pc.localDescription.sdp, max_kbps=12000)
        host_fp = extract_dtls_fingerprint(answer_sdp)
        self._dtls_context = build_dtls_context(host_fp, viewer_fp)

        await self.send_signal(
            {
                "type": "sdp_answer",
                "session_id": self.session_id,
                "payload": {"sdp_answer": answer_sdp},
            }
        )
        logger.info(
            "Session %s sent sdp_answer (mode=%s, pairing_requested=%s)",
            self.session_id,
            self.mode,
            bool(pairing_id),
        )
        await self._issue_auth_challenge(host_fp, viewer_fp, pairing_id)

    async def _issue_auth_challenge(
        self, host_fp: str, viewer_fp: str, pairing_id: str | None
    ) -> None:
        # If viewer sent a known pairing_id (Me2Me only), offer HMAC challenge first
        if self.mode == "me2me" and pairing_id and get_pairing_auth_key(self.config, pairing_id):
            self._pairing_id_pending = pairing_id
            self._pairing_nonce = secrets.token_hex(24)
            await self.send_signal(
                {
                    "type": "pairing_challenge",
                    "session_id": self.session_id,
                    "payload": {
                        "pairing_id": pairing_id,
                        "nonce": self._pairing_nonce,
                        "host_fp": host_fp,
                        "viewer_fp": viewer_fp,
                    },
                }
            )
            return

        await self._send_srp_challenge(host_fp, viewer_fp)

    async def _send_srp_challenge(self, host_fp: str, viewer_fp: str) -> None:
        if self.mode == "it2me":
            if not self.support_pin:
                await self.reject("Support PIN missing on host")
                return
            identity = self.session_id
            salt_hex, verifier_hex = create_verifier(identity, self.support_pin)
        else:
            identity = self.config.host_id
            salt_hex, verifier_hex = self.config.srp_salt, self.config.srp_verifier

        self._srp_session = SRPServerSession.start(
            identity=identity,
            salt_hex=salt_hex,
            verifier_hex=verifier_hex,
            dtls_context=self._dtls_context,
        )
        await self.send_signal(
            {
                "type": "pake_challenge",
                "session_id": self.session_id,
                "payload": {
                    "identity": identity,
                    "salt": self._srp_session.salt_hex,
                    "B": self._srp_session.B_hex,
                    "host_fp": host_fp,
                    "viewer_fp": viewer_fp,
                },
            }
        )

    async def handle_signal_message(self, msg: dict[str, Any]) -> None:
        if self._closed:
            return
        msg_type = msg.get("type")
        payload = msg.get("payload") or {}

        if msg_type == "pake_client_proof":
            if not self._srp_session:
                await self.reject("Unexpected PAKE proof")
                return
            A_hex = str(payload.get("A") or "")
            M1_hex = str(payload.get("M1") or "")
            remember_device = bool(payload.get("remember_device", False))
            viewer_label = str(payload.get("viewer_label") or "Browser")
            try:
                M2_hex = self._srp_session.verify_client_proof(A_hex, M1_hex)
            except SRPAuthError:
                self.rate_limiter.record_failure()
                await self.reject("Invalid PIN or verification context")
                return

            self.rate_limiter.record_success()
            if remember_device and self.mode == "me2me":
                pid, raw_secret = create_pairing_entry(
                    self.config, label=viewer_label, path=self.config_path
                )
                self._pending_pairing_grant = (pid, raw_secret)

            await self.send_signal(
                {
                    "type": "pake_server_proof",
                    "session_id": self.session_id,
                    "payload": {"M2": M2_hex},
                }
            )
            self._unlock_session()

        elif msg_type == "pairing_client_proof":
            fallback_to_pin = bool(payload.get("fallback_to_pin", False))
            if fallback_to_pin:
                parts = self._dtls_context.split("|")
                h_fp = parts[0].removeprefix("host:") if len(parts) == 2 else ""
                v_fp = parts[1].removeprefix("viewer:") if len(parts) == 2 else ""
                await self._send_srp_challenge(h_fp, v_fp)
                return

            auth_key = get_pairing_auth_key(self.config, self._pairing_id_pending)
            proof = str(payload.get("proof") or "")
            if not auth_key or not self._pairing_nonce:
                await self.reject("Pairing credential invalid or revoked", code="pairing_invalid")
                return
            if not verify_pairing_hmac(auth_key, proof, self._pairing_nonce, self._dtls_context):
                self.rate_limiter.record_failure()
                await self.reject("Pairing proof rejected", code="pairing_invalid")
                return

            self.rate_limiter.record_success()
            await self.send_signal(
                {
                    "type": "pairing_accepted",
                    "session_id": self.session_id,
                    "payload": {"pairing_id": self._pairing_id_pending},
                }
            )
            self._unlock_session()

        elif msg_type == "ice_candidate":
            cand_raw = payload.get("candidate")
            if not cand_raw:
                return
            if isinstance(cand_raw, dict):
                cand_str = str(cand_raw.get("candidate") or "")
                sdp_mid = cand_raw.get("sdpMid")
                sdp_mline_idx = cand_raw.get("sdpMLineIndex")
            else:
                cand_str = str(cand_raw)
                sdp_mid = payload.get("sdpMid")
                sdp_mline_idx = payload.get("sdpMLineIndex")
            if not cand_str:
                return
            try:
                stripped = cand_str.removeprefix("candidate:")
                candidate = candidate_from_sdp(stripped)
                candidate.sdpMid = sdp_mid
                candidate.sdpMLineIndex = sdp_mline_idx
                await self.pc.addIceCandidate(candidate)
            except Exception:
                pass

        elif msg_type == "enable_ws_fallback":
            if not self._authenticated:
                await self.reject("WebSocket relay requires authentication")
                return
            await self._activate_ws_fallback()

        elif msg_type == "ws_frame_ack":
            if not self._authenticated:
                return
            ack = int(payload.get("seq", 0))
            if ack > self._ws_acked_seq:
                self._ws_acked_seq = ack
            self._ws_ack_event.set()

        elif msg_type == "ws_channel":
            if not self._authenticated:
                return
            label = str(payload.get("channel") or "")
            data = payload.get("data")
            if label and isinstance(data, dict):
                self._dispatch_channel_message(label, data)

        elif msg_type == "session_end":
            await self.close()

    def _unlock_session(self) -> None:
        logger.info(
            "Session %s authenticated (%s mode, control_only=%s); unlocking",
            self.session_id,
            self.mode,
            self._control_only,
        )
        self._authenticated = True
        if not self._control_only:
            self._auth_event.set()
        self._maybe_send_post_auth_control()

    def _maybe_send_post_auth_control(self) -> None:
        if not self._authenticated or self._post_auth_sent:
            return
        if not self._ws_fallback_active:
            ch = self._channels.get("control")
            if not ch or ch.readyState != "open":
                logger.info(
                    "Session %s waiting for control DataChannel (present=%s, state=%s)",
                    self.session_id,
                    ch is not None,
                    getattr(ch, "readyState", None),
                )
                return
        self._post_auth_sent = True

        self._monitors = self.capturer.get_monitors()
        logger.info(
            "Session %s sending session_ready (%d monitors, control_only=%s)",
            self.session_id,
            len(self._monitors),
            self._control_only,
        )
        ready_payload: dict[str, Any] = {
            "type": "session_ready",
            "control_only": self._control_only,
            "monitors": [m.model_dump() for m in self._monitors],
            "active_monitor": self._active_monitor.index,
        }
        if self._control_only:
            ready_payload["system_status"] = self.quick_actions.get_system_status()
        self._send_dc("control", ready_payload)
        if self._pending_pairing_grant:
            pid, raw_secret = self._pending_pairing_grant
            self._pending_pairing_grant = None
            # Pairing secret travels ONLY over encrypted DataChannel or authenticated TLS WS
            self._send_dc(
                "control",
                {
                    "type": "pairing_grant",
                    "pairing_id": pid,
                    "pairing_secret": raw_secret,
                },
            )

    def _send_dc(self, label: str, data: dict[str, Any]) -> None:
        if self._ws_fallback_active:
            asyncio.create_task(
                self.send_signal(
                    {
                        "type": "ws_channel",
                        "session_id": self.session_id,
                        "payload": {"channel": label, "data": data},
                    }
                )
            )
            return
        ch = self._channels.get(label)
        if ch and ch.readyState == "open":
            try:
                ch.send(json.dumps(data))
            except Exception as exc:
                logger.warning("Session %s DataChannel %s send error: %s", self.session_id, label, exc)

    async def _activate_ws_fallback(self) -> None:
        if self._ws_fallback_active or self._closed or self.send_binary is None:
            return
        logger.info("Session %s switching to WSS TLS Relay fallback", self.session_id)
        self._switching_to_fallback = True
        self._ws_fallback_active = True
        self._video_track.stop()
        try:
            await self.pc.close()
        except Exception:
            pass

        self._monitors = self.capturer.get_monitors()
        await self.send_signal(
            {
                "type": "ws_fallback_ready",
                "session_id": self.session_id,
                "payload": {
                    "monitors": [m.model_dump() for m in self._monitors],
                    "active_monitor": self._active_monitor.index,
                },
            }
        )
        if self._pending_pairing_grant:
            pid, raw_secret = self._pending_pairing_grant
            self._pending_pairing_grant = None
            self._send_dc(
                "control",
                {"type": "pairing_grant", "pairing_id": pid, "pairing_secret": raw_secret},
            )

        self._fallback_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ws-cap")
        self._fallback_task = asyncio.create_task(self._ws_fallback_loop())

    async def _ws_fallback_loop(self) -> None:
        sid_bytes = self.session_id.encode("utf-8")
        header = struct.pack(">H", len(sid_bytes)) + sid_bytes
        loop = asyncio.get_running_loop()
        jpeg_profiles = {
            "ultra": (1600, 900, 20, 82),
            "auto": (1280, 720, 18, 74),
            "balanced": (1024, 576, 15, 68),
            "low": (854, 480, 12, 58),
        }
        try:
            while not self._closed and self._ws_fallback_active:
                while (self._ws_sent_seq - self._ws_acked_seq) >= 2 and not self._closed:
                    self._ws_ack_event.clear()
                    try:
                        await asyncio.wait_for(self._ws_ack_event.wait(), timeout=0.6)
                    except asyncio.TimeoutError:
                        break
                if self._closed:
                    break

                max_w, max_h, fps, jq = jpeg_profiles.get(
                    self._ws_quality, jpeg_profiles["auto"]
                )
                assert self._fallback_executor is not None
                jpeg_bytes, _w, _h = await loop.run_in_executor(
                    self._fallback_executor,
                    self.capturer.capture_jpeg,
                    self._active_monitor.index,
                    max_w,
                    max_h,
                    jq,
                )
                self._ws_sent_seq += 1
                assert self.send_binary is not None
                await self.send_binary(header + jpeg_bytes)
                await asyncio.sleep(1.0 / max(1, fps))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("WSS fallback stream ended for %s: %s", self.session_id, exc)

    def _dispatch_channel_message(self, label: str, data: dict[str, Any]) -> None:
        msg_type = data.get("type")

        if label == "input":
            if msg_type == "mouse_move":
                self.injector.mouse_move(
                    float(data.get("x", 0.0)),
                    float(data.get("y", 0.0)),
                    self._active_monitor,
                )
            elif msg_type == "mouse_button":
                self.injector.mouse_button(
                    int(data.get("button", 0)),
                    bool(data.get("down", True)),
                    float(data.get("x", 0.0)),
                    float(data.get("y", 0.0)),
                    self._active_monitor,
                )
            elif msg_type == "mouse_wheel":
                self.injector.mouse_wheel(
                    float(data.get("dx", 0.0)),
                    float(data.get("dy", 0.0)),
                )
            elif msg_type == "key_event":
                self.injector.key_event(
                    str(data.get("code") or ""),
                    bool(data.get("down", True)),
                    str(data.get("key") or ""),
                )
            elif msg_type == "special_combo":
                self.injector.special_combo(str(data.get("combo") or ""))
            elif msg_type == "paste_text":
                self.injector.type_text(str(data.get("text") or ""))
            elif msg_type == "release_all_keys":
                self.injector.release_all_keys()

        elif label == "control":
            if msg_type == "select_monitor":
                idx = int(data.get("index", 1))
                found = next((m for m in self._monitors if m.index == idx), None)
                if found:
                    self._active_monitor = found
                    self._video_track.set_monitor(found.index)
                    self._send_dc("control", {"type": "monitor_changed", "index": found.index})
            elif msg_type == "set_quality":
                preset = str(data.get("preset") or "auto")
                self._ws_quality = preset
                bitrate = self._video_track.set_quality(preset)
                if bitrate is not None and self.pc is not None:
                    set_video_sender_bitrate(self.pc, bitrate)
                self._send_dc("control", {"type": "quality_changed", "preset": preset})
            elif msg_type == "ping":
                self._send_dc(
                    "control",
                    {"type": "pong", "ts": data.get("ts"), "frames_sent": self._video_track.frames_sent},
                )
            elif msg_type == "system_status":
                self._send_dc(
                    "control",
                    {"type": "system_status", "status": self.quick_actions.get_system_status()},
                )
            elif msg_type == "quick_snapshot":
                asyncio.create_task(self._send_quick_snapshot())
            elif msg_type == "quick_action":
                action = str(data.get("action") or "")
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                ok, message = self.quick_actions.execute(action, params)
                self._send_dc(
                    "control",
                    {
                        "type": "quick_action_result",
                        "action_id": data.get("action_id"),
                        "action": action,
                        "ok": ok,
                        "message": message,
                        "status": self.quick_actions.get_system_status(),
                    },
                )

        elif label == "clipboard":
            if msg_type == "clipboard_set":
                text = str(data.get("text") or "")
                self.clipboard.set_text(text)
                self._send_dc("clipboard", {"type": "clipboard_ack", "length": len(text)})
            elif msg_type == "clipboard_pull":
                self._send_dc(
                    "clipboard",
                    {"type": "clipboard_sync", "text": self.clipboard.get_text()},
                )

        elif label == "files":
            self._handle_file_message(data)

    def _handle_file_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")
        file_id = str(data.get("file_id") or "")
        if not file_id:
            return

        if msg_type == "file_offer":
            raw_name = str(data.get("name") or "upload.bin")
            safe_name = Path(raw_name).name or "upload.bin"
            size = int(data.get("size", 0))
            if size < 0 or size > 100 * 1024 * 1024:
                self._send_dc("files", {"type": "file_error", "file_id": file_id, "reason": "File exceeds 100MB limit"})
                return
            receive_dir = Path(self.config.receive_dir)
            receive_dir.mkdir(parents=True, exist_ok=True)
            dest = receive_dir / safe_name
            if dest.exists():
                dest = receive_dir / f"{dest.stem}_{secrets.token_hex(3)}{dest.suffix}"
            self._incoming_files[file_id] = {
                "path": dest,
                "handle": dest.open("wb"),
                "hasher": hashlib.sha256(),
                "received": 0,
                "expected_size": size,
            }
            self._send_dc("files", {"type": "file_accept", "file_id": file_id, "saved_name": dest.name})

        elif msg_type == "file_chunk":
            entry = self._incoming_files.get(file_id)
            if not entry:
                return
            raw_bytes = base64.b64decode(str(data.get("data") or ""))
            entry["handle"].write(raw_bytes)
            entry["hasher"].update(raw_bytes)
            entry["received"] += len(raw_bytes)

        elif msg_type == "file_complete":
            entry = self._incoming_files.pop(file_id, None)
            if not entry:
                return
            entry["handle"].close()
            digest = entry["hasher"].hexdigest()
            expected_sha = str(data.get("sha256") or "")
            if expected_sha and expected_sha.lower() != digest.lower():
                entry["path"].unlink(missing_ok=True)
                self._send_dc("files", {"type": "file_error", "file_id": file_id, "reason": "SHA-256 mismatch"})
                return
            self._send_dc(
                "files",
                {
                    "type": "file_saved",
                    "file_id": file_id,
                    "path": str(entry["path"]),
                    "name": entry["path"].name,
                    "size": entry["received"],
                    "sha256": digest,
                },
            )

    async def reject(self, reason: str, code: str = "auth_failed") -> None:
        await self.send_signal(
            {
                "type": "session_reject",
                "session_id": self.session_id,
                "payload": {"reason": reason, "code": code},
            }
        )
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._authenticated = False
        self.injector.release_all_keys()
        for entry in list(self._incoming_files.values()):
            try:
                entry["handle"].close()
            except Exception:
                pass
        self._incoming_files.clear()
        self._ws_fallback_active = False
        self._ws_ack_event.set()
        if self._fallback_task:
            self._fallback_task.cancel()
        if self._fallback_executor:
            self._fallback_executor.shutdown(wait=False)
        self._video_track.stop()
        self._snapshot_executor.shutdown(wait=False)
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                pass
        if self.on_closed:
            self.on_closed(self.session_id)

    async def _send_quick_snapshot(self) -> None:
        if self._closed or not self._authenticated:
            return
        try:
            loop = asyncio.get_running_loop()
            jpeg_bytes, w, h = await loop.run_in_executor(
                self._snapshot_executor,
                self.capturer.capture_jpeg,
                self._active_monitor.index,
                1280,
                720,
                76,
            )
            self._send_dc(
                "control",
                {
                    "type": "quick_snapshot",
                    "image_b64": base64.b64encode(jpeg_bytes).decode("ascii"),
                    "width": w,
                    "height": h,
                },
            )
        except Exception as exc:
            self._send_dc(
                "control",
                {
                    "type": "quick_action_result",
                    "action": "quick_snapshot",
                    "ok": False,
                    "message": f"Snapshot failed: {exc}",
                },
            )
