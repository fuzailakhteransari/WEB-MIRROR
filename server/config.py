"""Server configuration loaded from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_ICE_SERVERS: list[dict] = [
    {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}
]


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    workspace_token: str = ""
    support_code_ttl_seconds: int = 300
    ice_servers: list[dict] = field(default_factory=lambda: list(DEFAULT_ICE_SERVERS))
    web_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "web"
    )
    registry_path: Path | None = None
    state_dir: Path = field(default_factory=lambda: Path.home() / ".webdesk-remote")

    @classmethod
    def from_env(cls) -> "ServerConfig":
        state_dir_env = os.environ.get("WEBDESK_STATE_DIR", "").strip()
        state_dir = Path(state_dir_env) if state_dir_env else (Path.home() / ".webdesk-remote")

        raw_ice = os.environ.get("WEBDESK_ICE_SERVERS_JSON", "").strip()
        ice_servers = list(DEFAULT_ICE_SERVERS)
        if raw_ice:
            parsed = json.loads(raw_ice)
            if isinstance(parsed, list):
                ice_servers = parsed
        else:
            ice_file = state_dir / "ice_servers.json"
            if ice_file.exists():
                try:
                    parsed = json.loads(ice_file.read_text(encoding="utf-8"))
                    if isinstance(parsed, list) and parsed:
                        ice_servers = parsed
                except (OSError, json.JSONDecodeError):
                    pass

        reg_env = os.environ.get("WEBDESK_REGISTRY_PATH", "").strip()
        registry_path = Path(reg_env) if reg_env else None

        return cls(
            host=os.environ.get("WEBDESK_HOST", "127.0.0.1"),
            port=int(os.environ.get("WEBDESK_PORT", "8080")),
            workspace_token=os.environ.get("WEBDESK_WORKSPACE_TOKEN", "").strip(),
            support_code_ttl_seconds=int(
                os.environ.get("WEBDESK_SUPPORT_TTL", "300")
            ),
            ice_servers=ice_servers,
            registry_path=registry_path,
            state_dir=state_dir,
        )
