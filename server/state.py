"""In-memory and optional JSON-persisted host directory, IT2Me codes, and session routing."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from shared.protocol import HostSummary, MonitorInfo, format_support_code, normalize_support_code


@dataclass
class HostEntry:
    host_id: str
    name: str
    platform: str = "windows"
    mode: str = "me2me"
    monitors: list[MonitorInfo] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=lambda: ["video", "input", "clipboard", "files"])
    status: str = "online"
    last_seen: float = field(default_factory=time.time)
    websocket: WebSocket | None = None
    active_sessions: set[str] = field(default_factory=set)

    def to_summary(self) -> HostSummary:
        effective_status = self.status
        if self.websocket is None:
            effective_status = "offline"
        elif self.active_sessions:
            effective_status = "busy"
        return HostSummary(
            host_id=self.host_id,
            name=self.name,
            platform=self.platform,
            status=effective_status,  # type: ignore[arg-type]
            monitors=self.monitors,
            last_seen=self.last_seen,
            capabilities=self.capabilities,
        )


@dataclass
class SupportCodeEntry:
    code: str
    host_id: str
    expires_at: float
    consumed: bool = False


@dataclass
class SignalingSession:
    session_id: str
    host_id: str
    mode: str
    viewer_ws: WebSocket
    host_ws: WebSocket
    created_at: float = field(default_factory=time.time)
    authenticated: bool = False


class SignalingState:
    """Thread/async-safe single-event-loop coordinator for hosts, codes, and sessions."""

    def __init__(
        self,
        support_code_ttl_seconds: int = 300,
        registry_path: Path | None = None,
    ) -> None:
        self.support_code_ttl_seconds = support_code_ttl_seconds
        self.registry_path = registry_path
        self.hosts: dict[str, HostEntry] = {}
        self.support_codes: dict[str, SupportCodeEntry] = {}
        self.sessions: dict[str, SignalingSession] = {}
        self._load_persisted_hosts()

    def _load_persisted_hosts(self) -> None:
        if not self.registry_path or not self.registry_path.exists():
            return
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("hosts", []):
            hid = item.get("host_id")
            if not hid:
                continue
            caps = item.get("capabilities")
            self.hosts[hid] = HostEntry(
                host_id=hid,
                name=item.get("name", hid),
                platform=item.get("platform", "windows"),
                mode="me2me",
                capabilities=list(caps) if isinstance(caps, list) and caps else ["video", "input", "clipboard", "files"],
                status="offline",
                last_seen=float(item.get("last_seen", 0.0)),
                websocket=None,
            )

    def _persist_hosts(self) -> None:
        if not self.registry_path:
            return
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        me2me = [
            {
                "host_id": h.host_id,
                "name": h.name,
                "platform": h.platform,
                "capabilities": h.capabilities,
                "last_seen": h.last_seen,
            }
            for h in self.hosts.values()
            if h.mode == "me2me"
        ]
        self.registry_path.write_text(
            json.dumps({"hosts": me2me}, indent=2), encoding="utf-8"
        )

    def register_host(
        self,
        host_id: str,
        name: str,
        platform: str,
        mode: str,
        monitors: list[dict[str, Any]],
        capabilities: list[str],
        ws: WebSocket,
    ) -> HostEntry:
        parsed_monitors = [MonitorInfo(**m) for m in monitors]
        existing = self.hosts.get(host_id)
        entry = HostEntry(
            host_id=host_id,
            name=name or (existing.name if existing else host_id),
            platform=platform or "windows",
            mode=mode or "me2me",
            monitors=parsed_monitors,
            capabilities=capabilities or ["video", "input", "clipboard", "files"],
            status="online",
            last_seen=time.time(),
            websocket=ws,
            active_sessions=set(),
        )
        self.hosts[host_id] = entry
        self._persist_hosts()
        return entry

    def update_host_heartbeat(self, host_id: str, monitors: list[dict[str, Any]] | None = None) -> None:
        entry = self.hosts.get(host_id)
        if not entry:
            return
        entry.last_seen = time.time()
        if monitors is not None:
            entry.monitors = [MonitorInfo(**m) for m in monitors]

    def list_me2me_hosts(self) -> list[HostSummary]:
        items = [h.to_summary() for h in self.hosts.values() if h.mode == "me2me"]
        items.sort(key=lambda s: (s.status == "offline", -s.last_seen, s.name.lower()))
        return items

    def rename_host(self, host_id: str, new_name: str) -> HostSummary | None:
        entry = self.hosts.get(host_id)
        if not entry:
            return None
        entry.name = new_name.strip() or entry.name
        self._persist_hosts()
        return entry.to_summary()

    def remove_host(self, host_id: str) -> bool:
        entry = self.hosts.pop(host_id, None)
        if entry is None:
            return False
        for code in [c for c, sc in self.support_codes.items() if sc.host_id == host_id]:
            self.support_codes.pop(code, None)
        self._persist_hosts()
        return True

    def create_support_code(self, host_id: str) -> SupportCodeEntry | None:
        host = self.hosts.get(host_id)
        if not host or host.websocket is None:
            return None
        self._prune_expired_codes()
        for _ in range(16):
            digits = "".join(secrets.choice("0123456789") for _ in range(12))
            if digits not in self.support_codes:
                entry = SupportCodeEntry(
                    code=digits,
                    host_id=host_id,
                    expires_at=time.time() + self.support_code_ttl_seconds,
                )
                self.support_codes[digits] = entry
                return entry
        raise RuntimeError("Failed to allocate unique 12-digit support code")

    def consume_support_code(self, raw_code: str) -> tuple[str | None, str | None]:
        """Validate and consume a 12-digit IT2Me code. Returns (host_id, error_reason)."""
        self._prune_expired_codes()
        digits = normalize_support_code(raw_code)
        if len(digits) != 12:
            return None, "Support code must be exactly 12 digits"
        entry = self.support_codes.get(digits)
        if not entry:
            return None, "Invalid or expired support code"
        if entry.consumed:
            return None, "Support code has already been used"
        if entry.expires_at < time.time():
            self.support_codes.pop(digits, None)
            return None, "Support code has expired"
        host = self.hosts.get(entry.host_id)
        if not host or host.websocket is None:
            return None, "Support host is no longer online"
        entry.consumed = True
        return entry.host_id, None

    def _prune_expired_codes(self) -> None:
        now = time.time()
        expired = [k for k, v in self.support_codes.items() if v.expires_at < now or v.consumed]
        for k in expired:
            self.support_codes.pop(k, None)

    def create_session(
        self, host_id: str, mode: str, viewer_ws: WebSocket
    ) -> tuple[SignalingSession | None, str | None]:
        host = self.hosts.get(host_id)
        if not host or host.websocket is None:
            return None, "Target host is offline"
        if host.mode != mode:
            return None, "Target host is not accepting this connection mode"
        if host.active_sessions:
            if host.mode != "me2me":
                return None, "Target host is already in an active session"
            # Prune stalled unauthenticated Me2Me handshake attempts so reconnects never wedge.
            now = time.time()
            for sid in list(host.active_sessions):
                existing = self.sessions.get(sid)
                if not existing or (not existing.authenticated and (now - existing.created_at) > 45.0):
                    self.end_session(sid)
            if len(host.active_sessions) >= 3:
                return None, "Too many concurrent connection attempts"
        session_id = secrets.token_urlsafe(16)
        session = SignalingSession(
            session_id=session_id,
            host_id=host_id,
            mode=mode,
            viewer_ws=viewer_ws,
            host_ws=host.websocket,
        )
        self.sessions[session_id] = session
        host.active_sessions.add(session_id)
        host.status = "busy"
        return session, None

    def end_session(self, session_id: str) -> SignalingSession | None:
        session = self.sessions.pop(session_id, None)
        if not session:
            return None
        host = self.hosts.get(session.host_id)
        if host:
            host.active_sessions.discard(session_id)
            if host.websocket is None:
                host.status = "offline"
            elif host.active_sessions:
                host.status = "busy"
            else:
                host.status = "online"
        return session

    def end_other_sessions(self, host_id: str, keep_session_id: str) -> list[SignalingSession]:
        host = self.hosts.get(host_id)
        if not host:
            return []
        keep = self.sessions.get(keep_session_id)
        if keep:
            keep.authenticated = True
        ended: list[SignalingSession] = []
        for sid in list(host.active_sessions):
            if sid == keep_session_id:
                continue
            popped = self.end_session(sid)
            if popped:
                ended.append(popped)
        return ended

    def disconnect_host_ws(self, ws: WebSocket) -> tuple[str | None, list[SignalingSession]]:
        target_id: str | None = None
        for hid, entry in list(self.hosts.items()):
            if entry.websocket is ws:
                target_id = hid
                break
        if target_id is None:
            return None, []

        entry = self.hosts[target_id]
        entry.websocket = None
        entry.status = "offline"
        entry.last_seen = time.time()

        orphaned: list[SignalingSession] = []
        for sid in list(entry.active_sessions):
            sess = self.end_session(sid)
            if sess:
                orphaned.append(sess)

        # Drop IT2Me ephemeral hosts completely on disconnect
        if entry.mode == "it2me":
            self.hosts.pop(target_id, None)
        else:
            self._persist_hosts()

        for code in [c for c, sc in self.support_codes.items() if sc.host_id == target_id]:
            self.support_codes.pop(code, None)

        return target_id, orphaned

    def disconnect_viewer_ws(self, ws: WebSocket) -> list[SignalingSession]:
        ended: list[SignalingSession] = []
        for sid, sess in list(self.sessions.items()):
            if sess.viewer_ws is ws:
                popped = self.end_session(sid)
                if popped:
                    ended.append(popped)
        return ended

    @staticmethod
    def format_code(digits: str) -> str:
        return format_support_code(digits)
