"""Typed signaling and WebRTC DataChannel message schemas."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class MonitorInfo(BaseModel):
    index: int
    name: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False


class HostSummary(BaseModel):
    host_id: str
    name: str
    platform: str = "windows"
    status: Literal["online", "busy", "offline"] = "online"
    monitors: list[MonitorInfo] = Field(default_factory=list)
    last_seen: float = 0.0
    capabilities: list[str] = Field(default_factory=lambda: ["video", "input", "clipboard", "files"])


class SignalingMessage(BaseModel):
    type: str
    session_id: str | None = None
    host_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def normalize_support_code(code: str) -> str:
    """Strip whitespace and hyphens from a 12-digit IT2Me support code."""
    return "".join(ch for ch in code if ch.isdigit())


def format_support_code(digits: str) -> str:
    """Format 12 digits as XXXX-XXXX-XXXX."""
    clean = normalize_support_code(digits)
    if len(clean) != 12:
        return clean
    return f"{clean[:4]}-{clean[4:8]}-{clean[8:12]}"
