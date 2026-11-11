"""Chromium Native Messaging stdin/stdout 32-bit framed JSON protocol host."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO

from host.config_store import DEFAULT_CONFIG_PATH, initialize_host_config, load_host_config

NATIVE_HOST_NAME = "com.webdesk.remote_host"
MAX_NATIVE_MESSAGE_BYTES = 1024 * 1024  # 1 MiB Chromium standard limit


def encode_native_message(payload: dict[str, Any]) -> bytes:
    """Encode a dict into Chromium's 32-bit native-endian length-prefixed UTF-8 JSON."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_NATIVE_MESSAGE_BYTES:
        raise ValueError("Native message exceeds 1 MiB limit")
    return struct.pack("=I", len(raw)) + raw


def read_native_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read a single length-prefixed UTF-8 JSON message from binary stdin; None on EOF."""
    header = stream.read(4)
    if not header:
        return None
    if len(header) < 4:
        raise EOFError("Truncated native message length prefix")
    (length,) = struct.unpack("=I", header)
    if length > MAX_NATIVE_MESSAGE_BYTES:
        raise ValueError(f"Native message length {length} exceeds limit")
    body = stream.read(length)
    if len(body) < length:
        raise EOFError("Truncated native message body")
    return json.loads(body.decode("utf-8"))


def write_native_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(encode_native_message(payload))
    stream.flush()


class NativeMessagingDispatcher:
    """Handles setup/status commands sent by the companion browser extension."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.config_path = config_path

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        action = message.get("action")
        if action == "get_host_status":
            try:
                cfg = load_host_config(self.config_path)
                return {
                    "ok": True,
                    "configured": True,
                    "host_id": cfg.host_id,
                    "host_name": cfg.host_name,
                    "server_url": cfg.server_url,
                    "paired_clients": len(cfg.pairing_registry),
                }
            except FileNotFoundError:
                return {"ok": True, "configured": False}

        if action == "configure_host":
            pin = str(message.get("pin") or "")
            name = str(message.get("host_name") or "WebDesk-Host")
            server_url = str(message.get("server_url") or "ws://127.0.0.1:8080/ws/host")
            try:
                cfg = initialize_host_config(
                    host_name=name,
                    pin=pin,
                    server_url=server_url,
                    path=self.config_path,
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "host_id": cfg.host_id, "host_name": cfg.host_name}

        if action == "ping":
            return {"ok": True, "pong": True, "host_name": NATIVE_HOST_NAME}

        return {"ok": False, "error": f"Unknown action: {action}"}


def install_chrome_native_host_manifest(
    extension_id: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
    manifest_dir: Path | None = None,
) -> Path:
    """Write the Native Messaging manifest JSON (and Windows HKCU registry key when applicable)."""
    target_dir = manifest_dir or (config_path.parent / "native_messaging")
    target_dir.mkdir(parents=True, exist_ok=True)
    launcher_bat = target_dir / "webdesk_native_host.bat"
    project_root = Path(__file__).resolve().parent.parent
    launcher_bat.write_text(
        f'@echo off\r\ncd /d "{project_root}"\r\n"{sys.executable}" -m host.cli native-messaging --config "{config_path}"\r\n',
        encoding="utf-8",
    )
    manifest_path = target_dir / f"{NATIVE_HOST_NAME}.json"
    manifest = {
        "name": NATIVE_HOST_NAME,
        "description": "WebDesk Remote Native Messaging Setup Host",
        "path": str(launcher_bat) if sys.platform == "win32" else sys.executable,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id.strip()}/"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if sys.platform == "win32" and manifest_dir is None:
        import winreg

        key_path = rf"Software\Google\Chrome\NativeMessagingHosts\{NATIVE_HOST_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))

    _ = project_root
    return manifest_path


def run_native_messaging_stdio(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    dispatcher = NativeMessagingDispatcher(config_path=config_path)
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer
    while True:
        msg = read_native_message(stdin_bin)
        if msg is None:
            break
        resp = dispatcher.handle(msg)
        write_native_message(stdout_bin, resp)
    return 0
