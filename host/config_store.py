"""Persistent host configuration, SRP-6a PIN verifier storage, pairing registry, and rate limiting."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.srp import create_verifier, hash_pairing_secret


DEFAULT_CONFIG_DIR = Path.home() / ".webdesk-remote"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "host_config.json"


@dataclass
class HostConfig:
    host_id: str
    host_name: str
    server_url: str = "ws://127.0.0.1:8080/ws/host"
    workspace_token: str = ""
    srp_salt: str = ""
    srp_verifier: str = ""
    pairing_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    receive_dir: str = str(Path.home() / "Downloads" / "WebDeskReceived")

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "host_name": self.host_name,
            "server_url": self.server_url,
            "workspace_token": self.workspace_token,
            "srp_salt": self.srp_salt,
            "srp_verifier": self.srp_verifier,
            "pairing_registry": self.pairing_registry,
            "receive_dir": self.receive_dir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HostConfig":
        return cls(
            host_id=data["host_id"],
            host_name=data.get("host_name", "WebDesk-Host"),
            server_url=data.get("server_url", "ws://127.0.0.1:8080/ws/host"),
            workspace_token=data.get("workspace_token", ""),
            srp_salt=data.get("srp_salt", ""),
            srp_verifier=data.get("srp_verifier", ""),
            pairing_registry=dict(data.get("pairing_registry", {})),
            receive_dir=data.get(
                "receive_dir", str(Path.home() / "Downloads" / "WebDeskReceived")
            ),
        )


def load_host_config(path: Path = DEFAULT_CONFIG_PATH) -> HostConfig:
    if not path.exists():
        raise FileNotFoundError(f"Host config not found at {path}. Run 'python -m host.cli setup' first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return HostConfig.from_dict(data)


def save_host_config(config: HostConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")


def initialize_host_config(
    host_name: str,
    pin: str,
    server_url: str = "ws://127.0.0.1:8080/ws/host",
    workspace_token: str = "",
    path: Path = DEFAULT_CONFIG_PATH,
    host_id: str | None = None,
) -> HostConfig:
    if len(pin.strip()) < 6:
        raise ValueError("PIN must be at least 6 characters/digits long")
    assigned_id = host_id or str(uuid.uuid4())
    salt_hex, verifier_hex = create_verifier(assigned_id, pin)
    cfg = HostConfig(
        host_id=assigned_id,
        host_name=host_name.strip() or "WebDesk-Host",
        server_url=server_url.strip(),
        workspace_token=workspace_token.strip(),
        srp_salt=salt_hex,
        srp_verifier=verifier_hex,
    )
    save_host_config(cfg, path)
    return cfg


def create_pairing_entry(
    config: HostConfig,
    label: str = "Browser",
    path: Path | None = DEFAULT_CONFIG_PATH,
) -> tuple[str, str]:
    """Create a persistent trusted pairing record. Returns (pairing_id, raw_secret_hex)."""
    pairing_id = secrets.token_urlsafe(12)
    raw_secret_hex = secrets.token_bytes(32).hex()
    auth_key_hex = hash_pairing_secret(raw_secret_hex)
    config.pairing_registry[pairing_id] = {
        "auth_key_hex": auth_key_hex,
        "label": label,
        "created_at": time.time(),
    }
    if path is not None:
        save_host_config(config, path)
    return pairing_id, raw_secret_hex


def get_pairing_auth_key(config: HostConfig, pairing_id: str | None) -> str | None:
    if not pairing_id:
        return None
    record = config.pairing_registry.get(pairing_id)
    if not record:
        return None
    return record.get("auth_key_hex")


class PinRateLimiter:
    """Locks out brute-force PIN attempts using exponential backoff."""

    def __init__(self, max_failures: int = 5, base_lock_seconds: float = 30.0) -> None:
        self.max_failures = max_failures
        self.base_lock_seconds = base_lock_seconds
        self.failures = 0
        self.locked_until = 0.0

    def is_locked(self, now: float | None = None) -> tuple[bool, float]:
        current = time.monotonic() if now is None else now
        if current < self.locked_until:
            return True, round(self.locked_until - current, 1)
        return False, 0.0

    def record_failure(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.failures += 1
        if self.failures >= self.max_failures:
            exponent = self.failures - self.max_failures
            duration = min(300.0, self.base_lock_seconds * (2 ** exponent))
            self.locked_until = current + duration

    def record_success(self) -> None:
        self.failures = 0
        self.locked_until = 0.0
