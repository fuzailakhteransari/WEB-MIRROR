"""Windows Quick Actions executor, live system telemetry, display wake/lock, and mock backend."""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from abc import ABC, abstractmethod
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Win32 power, execution state, and virtual-key constants
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_ON = -1
MONITOR_OFF = 2

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_int(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def validate_http_url(url: str) -> str:
    """Validate and return clean http(s) URL, rejecting javascript:/file:/shell inputs."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("URL cannot be empty")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Only http:// and https:// URLs are allowed")
    return cleaned


class QuickActionExecutor(ABC):
    """Interface for non-mirroring PC actions and live host telemetry."""

    @abstractmethod
    def get_system_status(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def execute(self, action: str, params: dict[str, Any] | None = None) -> tuple[bool, str]:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class Win32QuickActionExecutor(QuickActionExecutor):
    """Real Windows OS quick action backend using Win32 APIs."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32QuickActionExecutor only runs on Windows")
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._shell32 = ctypes.windll.shell32
        self._curtain_proc: subprocess.Popen[bytes] | None = None
        self._keep_awake = False
        self._last_cpu_times: tuple[int, int, int] | None = None

    def _read_cpu_percent(self) -> float:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not self._kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return 0.0
        cur = (_filetime_to_int(idle), _filetime_to_int(kernel), _filetime_to_int(user))
        prev = self._last_cpu_times
        self._last_cpu_times = cur
        if prev is None:
            return 0.0
        d_idle = cur[0] - prev[0]
        d_total = (cur[1] - prev[1]) + (cur[2] - prev[2])
        if d_total <= 0:
            return 0.0
        busy = max(0, d_total - d_idle)
        return round(min(100.0, (busy * 100.0) / d_total), 1)

    def _is_windows_locked(self) -> bool:
        handle = self._user32.OpenInputDesktop(0, False, 0x0001)
        if not handle:
            return True
        try:
            buf = ctypes.create_unicode_buffer(128)
            needed = wintypes.DWORD(0)
            if self._user32.GetUserObjectInformationW(
                handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)
            ):
                return buf.value.lower() != "default"
            return False
        finally:
            self._user32.CloseDesktop(handle)

    def _is_curtain_running(self) -> bool:
        return self._curtain_proc is not None and self._curtain_proc.poll() is None

    def _tap_vk(self, vk: int, extended: bool = True) -> None:
        flags = KEYEVENTF_EXTENDEDKEY if extended else 0
        self._user32.keybd_event(vk, 0, flags, 0)
        self._user32.keybd_event(vk, 0, flags | KEYEVENTF_KEYUP, 0)

    def _send_chord(self, *vks: int) -> None:
        for vk in vks:
            self._user32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(vks):
            self._user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def _wake_display(self) -> None:
        self._user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_ON)
        state = ES_CONTINUOUS | ES_DISPLAY_REQUIRED | (ES_SYSTEM_REQUIRED if self._keep_awake else 0)
        self._kernel32.SetThreadExecutionState(state)
        # Clear screen-blank idle timer gently via zero-delta Ctrl key release
        self._user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)

    def _start_privacy_curtain(self) -> None:
        if self._is_curtain_running():
            return
        py_exe = Path(sys.executable)
        pyw = py_exe.with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else py_exe)
        self._curtain_proc = subprocess.Popen(
            [exe, "-m", "host.privacy_curtain"],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    def _stop_privacy_curtain(self) -> bool:
        was_active = self._is_curtain_running()
        if self._curtain_proc is not None:
            try:
                self._curtain_proc.terminate()
                self._curtain_proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._curtain_proc.kill()
                except Exception:
                    pass
            self._curtain_proc = None
        return was_active

    def get_system_status(self) -> dict[str, Any]:
        mem = _MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        self._kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        total_gb = round(mem.ullTotalPhys / (1024 ** 3), 1)
        used_gb = round((mem.ullTotalPhys - mem.ullAvailPhys) / (1024 ** 3), 1)

        pwr = _SYSTEM_POWER_STATUS()
        bat_pct: int | None = None
        plugged: bool | None = None
        if self._kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
            if 0 <= int(pwr.BatteryLifePercent) <= 100 and int(pwr.BatteryFlag) != 128:
                bat_pct = int(pwr.BatteryLifePercent)
            if int(pwr.ACLineStatus) in (0, 1):
                plugged = int(pwr.ACLineStatus) == 1

        return {
            "hostname": platform.node() or "Windows PC",
            "cpu_percent": self._read_cpu_percent(),
            "memory_percent": int(mem.dwMemoryLoad),
            "memory_used_gb": used_gb,
            "memory_total_gb": total_gb,
            "battery_percent": bat_pct,
            "power_plugged": plugged,
            "windows_locked": self._is_windows_locked(),
            "privacy_lock_active": self._is_curtain_running(),
            "keep_awake": self._keep_awake,
            "timestamp": time.time(),
        }

    def execute(self, action: str, params: dict[str, Any] | None = None) -> tuple[bool, str]:
        p = params or {}

        if action == "lock_windows":
            self._user32.LockWorkStation()
            return True, (
                "Windows OS Lock activated. Note: Windows Lock Screen requires your OS password/PIN on the PC."
            )

        if action == "privacy_lock":
            self._start_privacy_curtain()
            self._user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
            return True, "Privacy Screen Lock activated and display turned off."

        if action == "open_lock":
            cleared = self._stop_privacy_curtain()
            self._wake_display()
            if self._is_windows_locked():
                return True, (
                    "Display woken & WebDesk Privacy Shield cleared. Windows OS password Lock Screen is active (requires OS PIN on PC)."
                )
            msg = "Privacy Lock opened & display woken!" if cleared else "Display woken & unlocked."
            return True, msg

        if action == "screen_off":
            self._user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
            return True, "Monitor display turned off."

        if action == "screen_on":
            self._wake_display()
            return True, "Monitor display woken up."

        if action == "keep_awake_on":
            self._keep_awake = True
            self._kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            return True, "Keep Awake enabled (PC will stay online and avoid auto-sleep)."

        if action == "keep_awake_off":
            self._keep_awake = False
            self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            return True, "Keep Awake disabled (normal Windows sleep schedule restored)."

        if action == "shutdown":
            subprocess.Popen(
                ["shutdown", "/s", "/t", "5", "/c", "Shutdown requested via WebDesk Remote"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True, "Shutdown scheduled in 5 seconds (tap Abort to cancel)."

        if action == "restart":
            subprocess.Popen(
                ["shutdown", "/r", "/t", "5", "/c", "Restart requested via WebDesk Remote"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True, "Restart scheduled in 5 seconds (tap Abort to cancel)."

        if action == "abort_shutdown":
            res = subprocess.run(
                ["shutdown", "/a"],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                return True, "Scheduled shutdown/restart aborted!"
            return False, "No pending shutdown or restart to cancel."

        if action == "sleep":
            subprocess.Popen(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True, "Putting PC to sleep..."

        media_vks = {
            "vol_up": (VK_VOLUME_UP, "Volume increased."),
            "vol_down": (VK_VOLUME_DOWN, "Volume decreased."),
            "vol_mute": (VK_VOLUME_MUTE, "Audio mute toggled."),
            "media_prev": (VK_MEDIA_PREV_TRACK, "Previous media track."),
            "media_play_pause": (VK_MEDIA_PLAY_PAUSE, "Media Play/Pause toggled."),
            "media_next": (VK_MEDIA_NEXT_TRACK, "Next media track."),
        }
        if action in media_vks:
            vk, label = media_vks[action]
            self._tap_vk(vk, extended=True)
            return True, label

        hotkeys: dict[str, tuple[tuple[int, ...], str]] = {
            "hotkey_win_d": ((0x5B, ord("D")), "Minimized/restored desktop (Win+D)."),
            "hotkey_alt_tab": ((0x12, 0x09), "Switched active window (Alt+Tab)."),
            "hotkey_taskmgr": ((0x11, 0x10, 0x1B), "Opened Task Manager (Ctrl+Shift+Esc)."),
            "hotkey_alt_f4": ((0x12, 0x73), "Sent Close Window (Alt+F4)."),
            "hotkey_esc": ((0x1B,), "Pressed Escape."),
            "hotkey_space": ((0x20,), "Pressed Space."),
            "hotkey_enter": ((0x0D,), "Pressed Enter."),
            "hotkey_f11": ((0x7A,), "Toggled Fullscreen (F11)."),
        }
        if action in hotkeys:
            seq, msg = hotkeys[action]
            self._send_chord(*seq)
            return True, msg

        if action == "launch_preset":
            preset = str(p.get("preset") or "")
            if preset == "downloads":
                os.startfile(str(Path.home() / "Downloads"))  # type: ignore[attr-defined]
                return True, "Opened Downloads folder."
            preset_cmds: dict[str, list[str]] = {
                "explorer": ["explorer.exe"],
                "taskmgr": ["taskmgr.exe"],
                "cmd": ["cmd.exe"],
                "notepad": ["notepad.exe"],
                "calc": ["calc.exe"],
                "settings": ["cmd.exe", "/c", "start", "ms-settings:"],
            }
            cmd = preset_cmds.get(preset)
            if not cmd:
                return False, f"Unknown app preset: {preset}"
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            return True, f"Launched {preset}."

        if action == "open_url":
            try:
                url = validate_http_url(str(p.get("url") or ""))
            except ValueError as exc:
                return False, str(exc)
            webbrowser.open(url)
            return True, f"Opened {url} in PC browser."

        if action == "empty_recycle_bin":
            flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
            self._shell32.SHEmptyRecycleBinW(None, None, flags)
            return True, "Recycle Bin emptied."

        return False, f"Unsupported quick action: {action}"

    def close(self) -> None:
        self._stop_privacy_curtain()
        if self._keep_awake:
            self._keep_awake = False
            try:
                self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass


@dataclass
class MockQuickActionExecutor(QuickActionExecutor):
    """In-memory Quick Action executor for automated cross-platform tests."""

    history: list[dict[str, Any]] = field(default_factory=list)
    windows_locked: bool = False
    privacy_lock_active: bool = False
    keep_awake: bool = False

    def get_system_status(self) -> dict[str, Any]:
        return {
            "hostname": "Mock-Windows-PC",
            "cpu_percent": 18.4,
            "memory_percent": 42,
            "memory_used_gb": 6.7,
            "memory_total_gb": 16.0,
            "battery_percent": 87,
            "power_plugged": True,
            "windows_locked": self.windows_locked,
            "privacy_lock_active": self.privacy_lock_active,
            "keep_awake": self.keep_awake,
            "timestamp": time.time(),
        }

    def execute(self, action: str, params: dict[str, Any] | None = None) -> tuple[bool, str]:
        p = dict(params or {})
        self.history.append({"action": action, "params": p})

        if action == "open_url":
            try:
                url = validate_http_url(str(p.get("url") or ""))
            except ValueError as exc:
                return False, str(exc)
            return True, f"Opened {url} in PC browser."

        if action == "privacy_lock":
            self.privacy_lock_active = True
            return True, "Privacy Screen Lock activated and display turned off."

        if action == "open_lock":
            self.privacy_lock_active = False
            return True, "Privacy Lock opened & display woken!"

        if action == "lock_windows":
            self.windows_locked = True
            return True, "Windows OS Lock activated."

        if action == "keep_awake_on":
            self.keep_awake = True
            return True, "Keep Awake enabled."

        if action == "keep_awake_off":
            self.keep_awake = False
            return True, "Keep Awake disabled."

        known = {
            "screen_off",
            "screen_on",
            "shutdown",
            "restart",
            "abort_shutdown",
            "sleep",
            "vol_up",
            "vol_down",
            "vol_mute",
            "media_prev",
            "media_play_pause",
            "media_next",
            "hotkey_win_d",
            "hotkey_alt_tab",
            "hotkey_taskmgr",
            "hotkey_alt_f4",
            "hotkey_esc",
            "hotkey_space",
            "hotkey_enter",
            "hotkey_f11",
            "launch_preset",
            "empty_recycle_bin",
        }
        if action in known:
            return True, f"Executed {action}."
        return False, f"Unsupported quick action: {action}"

    def close(self) -> None:
        self.privacy_lock_active = False
        self.keep_awake = False
