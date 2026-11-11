"""Screen capture backends (with Windows cursor overlay) and auth-gated VideoStreamTrack."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import fractions
import io
import logging
import sys
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import av
import numpy as np
from aiortc import VideoStreamTrack
from PIL import Image

from shared.protocol import MonitorInfo

logger = logging.getLogger(__name__)
QualityPreset = Literal["ultra", "auto", "balanced", "low"]

# (max_width, max_height, target_fps, target_bitrate_bps)
QUALITY_PROFILES: dict[str, tuple[int, int, int, int]] = {
    "ultra": (3840, 2160, 30, 12_000_000),
    "auto": (1920, 1080, 30, 6_000_000),
    "balanced": (1280, 720, 30, 3_000_000),
    "low": (960, 540, 20, 1_500_000),
}

VIDEO_CLOCK_RATE = 90000
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)

# Clean crisp 16x16 white-with-black-border pointer sprite (RGBA/BGRA)
_ARROW_MASK = [
    "X...............",
    "XX..............",
    "XOX.............",
    "XOOX............",
    "XOOOX...........",
    "XOOOOX..........",
    "XOOOOOX.........",
    "XOOOOOOX........",
    "XOOOOOOOX.......",
    "XOOOOOOOOX......",
    "XOOOOOXXXXX.....",
    "XOOXOOX.........",
    "XOX.XOOX........",
    "XX..XOOX........",
    "X....XOOX.......",
    ".....XXXX.......",
]


def _build_cursor_pixels() -> list[tuple[int, int, tuple[int, int, int, int]]]:
    pts: list[tuple[int, int, tuple[int, int, int, int]]] = []
    for dy, row in enumerate(_ARROW_MASK):
        for dx, ch in enumerate(row):
            if ch == "X":
                pts.append((dx, dy, (16, 16, 16, 255)))  # Dark outline (BGRA)
            elif ch == "O":
                pts.append((dx, dy, (255, 255, 255, 255)))  # Crisp white fill
    return pts


_CURSOR_PIXELS = _build_cursor_pixels()


class _CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


def enable_dpi_awareness() -> None:
    """Ensure Windows reports physical pixel coordinates instead of DPI-scaled values."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _even(value: int) -> int:
    return max(16, value & ~1)


def compute_scaled_dimensions(
    src_w: int, src_h: int, max_w: int, max_h: int
) -> tuple[int, int]:
    """Scale preserving aspect ratio and rounding to even dimensions for video codecs."""
    if src_w <= 0 or src_h <= 0:
        return 640, 360
    scale = min(1.0, max_w / src_w, max_h / src_h)
    return _even(int(src_w * scale)), _even(int(src_h * scale))


def overlay_windows_cursor(arr_bgra: np.ndarray, mon_left: int, mon_top: int) -> None:
    """Draw the live Windows cursor onto a captured BGRA monitor array if visible."""
    if sys.platform != "win32":
        return
    try:
        ci = _CURSORINFO(cbSize=ctypes.sizeof(_CURSORINFO))
        if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)):
            return
        if not (ci.flags & 1):  # CURSOR_SHOWING == 1
            return
        cx = int(ci.ptScreenPos.x) - mon_left
        cy = int(ci.ptScreenPos.y) - mon_top
        h, w = arr_bgra.shape[:2]
        if cx < 0 or cy < 0 or cx >= w or cy >= h:
            return
        for dx, dy, color in _CURSOR_PIXELS:
            px, py = cx + dx, cy + dy
            if 0 <= px < w and 0 <= py < h:
                arr_bgra[py, px] = color
    except Exception:
        pass


class DesktopCapturer(ABC):
    """Abstract desktop capturer yielding VideoFrames and monitor metadata."""

    @abstractmethod
    def get_monitors(self) -> list[MonitorInfo]:
        ...

    @abstractmethod
    def capture_frame(self, monitor_index: int, max_w: int, max_h: int) -> av.VideoFrame:
        ...

    def capture_jpeg(
        self, monitor_index: int, max_w: int, max_h: int, jpeg_quality: int = 72
    ) -> tuple[bytes, int, int]:
        """Encode a captured frame as JPEG bytes for WebSocket TLS relay fallback."""
        frame = self.capture_frame(monitor_index, max_w, max_h)
        rgb = frame.to_ndarray(format="rgb24")
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=jpeg_quality)
        return buf.getvalue(), frame.width, frame.height

    def close(self) -> None:
        pass


class MssDesktopCapturer(DesktopCapturer):
    """Real multi-monitor screen capture using mss on a dedicated GDI-safe thread."""

    def __init__(self) -> None:
        enable_dpi_awareness()
        self._sct = None

    def _ensure_sct(self):
        if self._sct is None:
            import mss

            self._sct = mss.MSS()
        return self._sct

    def get_monitors(self) -> list[MonitorInfo]:
        sct = self._ensure_sct()
        monitors = sct.monitors
        physical = monitors[1:] if len(monitors) > 1 else monitors
        has_explicit_primary = any(bool(m.get("is_primary")) for m in physical)
        result: list[MonitorInfo] = []
        start_idx = 1 if len(monitors) > 1 else 0
        for idx, m in enumerate(physical, start=start_idx):
            is_prim = bool(m.get("is_primary")) if has_explicit_primary else (idx == start_idx)
            label = f"Display {idx} ({m['width']}x{m['height']})"
            if is_prim:
                label += " [Primary]"
            result.append(
                MonitorInfo(
                    index=idx,
                    name=label,
                    left=int(m["left"]),
                    top=int(m["top"]),
                    width=int(m["width"]),
                    height=int(m["height"]),
                    is_primary=is_prim,
                )
            )
        return result

    def _standby_frame(self, width: int, height: int) -> av.VideoFrame:
        w, h = _even(width), _even(height)
        img = np.full((h, w, 3), (30, 22, 16), dtype=np.uint8)
        cx, cy = w // 2, h // 2
        bw, bh = min(320, w // 2), min(64, h // 4)
        img[cy - bh // 2 : cy + bh // 2, cx - bw // 2 : cx + bw // 2] = (75, 50, 35)
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        return frame.reformat(width=w, height=h, format="yuv420p")

    def capture_frame(self, monitor_index: int, max_w: int, max_h: int) -> av.VideoFrame:
        from mss.exception import ScreenShotError

        sct = self._ensure_sct()
        monitors = sct.monitors
        idx = monitor_index if 0 <= monitor_index < len(monitors) else (1 if len(monitors) > 1 else 0)
        mon = monitors[idx]
        target_w, target_h = compute_scaled_dimensions(
            int(mon["width"]), int(mon["height"]), max_w, max_h
        )
        try:
            raw = sct.grab(mon)
        except ScreenShotError:
            self.close()
            return self._standby_frame(target_w, target_h)

        # Copy writable buffer to overlay Windows hardware cursor before YUV420p conversion
        arr = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4)).copy()
        overlay_windows_cursor(arr, int(mon["left"]), int(mon["top"]))
        frame = av.VideoFrame.from_ndarray(arr, format="bgra")
        if target_w == raw.width and target_h == raw.height:
            return frame.reformat(format="yuv420p")
        return frame.reformat(width=target_w, height=target_h, format="yuv420p")

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


class SyntheticCapturer(DesktopCapturer):
    """Deterministic animated desktop test pattern for CI/Playwright E2E verification."""

    def __init__(self, width: int = 960, height: int = 540) -> None:
        self.width = _even(width)
        self.height = _even(height)
        self._tick = 0

    def get_monitors(self) -> list[MonitorInfo]:
        return [
            MonitorInfo(
                index=1,
                name=f"Synthetic Display ({self.width}x{self.height})",
                left=0,
                top=0,
                width=self.width,
                height=self.height,
                is_primary=True,
            ),
            MonitorInfo(
                index=2,
                name="Synthetic Secondary (1280x720)",
                left=self.width,
                top=0,
                width=1280,
                height=720,
                is_primary=False,
            ),
        ]

    def capture_frame(self, monitor_index: int, max_w: int, max_h: int) -> av.VideoFrame:
        self._tick += 1
        w, h = (1280, 720) if monitor_index == 2 else (self.width, self.height)
        target_w, target_h = compute_scaled_dimensions(w, h, max_w, max_h)

        img = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        base_b = (self._tick * 7) % 180 + 40
        base_g = 90 if monitor_index == 1 else 160
        img[:, :] = (base_b, base_g, 55)

        box = max(32, min(target_w, target_h) // 8)
        x = (self._tick * 13) % max(1, target_w - box)
        y = (self._tick * 9) % max(1, target_h - box)
        img[y : y + box, x : x + box] = (240, 240, 240)

        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        return frame.reformat(width=target_w, height=target_h, format="yuv420p")


class DesktopVideoTrack(VideoStreamTrack):
    """Auth-gated WebRTC video track with drift-free wall-clock pacing."""

    kind = "video"

    def __init__(
        self,
        capturer: DesktopCapturer,
        auth_event: asyncio.Event,
        quality: QualityPreset = "auto",
        monitor_index: int = 1,
    ) -> None:
        super().__init__()
        self._capturer = capturer
        self._auth_event = auth_event
        self._quality = quality
        self._monitor_index = monitor_index
        self._stopped = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-cap")
        self._start_time: float | None = None
        self._next_frame_at: float | None = None
        self.frames_sent = 0

    def set_quality(self, preset: str) -> int | None:
        if preset in QUALITY_PROFILES:
            self._quality = preset  # type: ignore[assignment]
            return QUALITY_PROFILES[preset][3]
        return None

    def set_monitor(self, monitor_index: int) -> None:
        self._monitor_index = monitor_index

    def stop(self) -> None:
        self._stopped = True
        self._auth_event.set()
        super().stop()
        self._executor.shutdown(wait=False)

    async def recv(self) -> av.VideoFrame:
        while not self._auth_event.is_set():
            if self._stopped:
                raise asyncio.CancelledError("Track stopped before authentication")
            await self._auth_event.wait()

        if self._stopped:
            raise asyncio.CancelledError("Track stopped")

        max_w, max_h, target_fps, _ = QUALITY_PROFILES.get(
            self._quality, QUALITY_PROFILES["auto"]
        )
        interval = 1.0 / max(1, target_fps)
        now = time.monotonic()

        if self._start_time is None:
            self._start_time = now
            self._next_frame_at = now
        elif self._next_frame_at is not None and now < self._next_frame_at:
            await asyncio.sleep(self._next_frame_at - now)
            now = time.monotonic()

        # Advance schedule without accumulating drift if capture runs slightly slower
        self._next_frame_at = max(now, (self._next_frame_at or now)) + interval

        loop = asyncio.get_running_loop()
        try:
            frame: av.VideoFrame = await loop.run_in_executor(
                self._executor,
                self._capturer.capture_frame,
                self._monitor_index,
                max_w,
                max_h,
            )
        except Exception as exc:
            logger.exception("DesktopVideoTrack capture error on monitor %s: %s", self._monitor_index, exc)
            raise
        frame.pts = int((time.monotonic() - self._start_time) * VIDEO_CLOCK_RATE)
        frame.time_base = VIDEO_TIME_BASE
        self.frames_sent += 1
        if self.frames_sent == 1:
            logger.info(
                "DesktopVideoTrack yielded first frame (%sx%s, monitor=%s, quality=%s)",
                frame.width,
                frame.height,
                self._monitor_index,
                self._quality,
            )
        return frame
