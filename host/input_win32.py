"""Windows SendInput keyboard/mouse injector, virtual key mapper, and clipboard helper."""

from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

from shared.protocol import MonitorInfo

# Windows SendInput constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Map browser KeyboardEvent.code -> (VK code, is_extended)
DOM_CODE_TO_VK: dict[str, tuple[int, bool]] = {
    "Backspace": (0x08, False),
    "Tab": (0x09, False),
    "Enter": (0x0D, False),
    "NumpadEnter": (0x0D, True),
    "ShiftLeft": (0xA0, False),
    "ShiftRight": (0xA1, True),
    "ControlLeft": (0xA2, False),
    "ControlRight": (0xA3, True),
    "AltLeft": (0xA4, False),
    "AltRight": (0xA5, True),
    "Pause": (0x13, False),
    "CapsLock": (0x14, False),
    "Escape": (0x1B, False),
    "Space": (0x20, False),
    "PageUp": (0x21, True),
    "PageDown": (0x22, True),
    "End": (0x23, True),
    "Home": (0x24, True),
    "ArrowLeft": (0x25, True),
    "ArrowUp": (0x26, True),
    "ArrowRight": (0x27, True),
    "ArrowDown": (0x28, True),
    "PrintScreen": (0x2C, True),
    "Insert": (0x2D, True),
    "Delete": (0x2E, True),
    "MetaLeft": (0x5B, True),
    "MetaRight": (0x5C, True),
    "ContextMenu": (0x5D, True),
    "Semicolon": (0xBA, False),
    "Equal": (0xBB, False),
    "Comma": (0xBC, False),
    "Minus": (0xBD, False),
    "Period": (0xBE, False),
    "Slash": (0xBF, False),
    "Backquote": (0xC0, False),
    "BracketLeft": (0xDB, False),
    "Backslash": (0xDC, False),
    "BracketRight": (0xDD, False),
    "Quote": (0xDE, False),
    "NumLock": (0x90, True),
    "ScrollLock": (0x91, False),
    "NumpadMultiply": (0x6A, False),
    "NumpadAdd": (0x6B, False),
    "NumpadSubtract": (0x6D, False),
    "NumpadDecimal": (0x6E, False),
    "NumpadDivide": (0x6F, True),
}

for _ch_ord in range(ord("A"), ord("Z") + 1):
    DOM_CODE_TO_VK[f"Key{chr(_ch_ord)}"] = (_ch_ord, False)
for _d in range(10):
    DOM_CODE_TO_VK[f"Digit{_d}"] = (0x30 + _d, False)
    DOM_CODE_TO_VK[f"Numpad{_d}"] = (0x60 + _d, False)
for _f in range(1, 25):
    DOM_CODE_TO_VK[f"F{_f}"] = (0x70 + _f - 1, False)

KEY_NAME_TO_VK: dict[str, tuple[int, bool]] = {
    "Backspace": (0x08, False),
    "Tab": (0x09, False),
    "Enter": (0x0D, False),
    "Escape": (0x1B, False),
    "ArrowLeft": (0x25, True),
    "ArrowUp": (0x26, True),
    "ArrowRight": (0x27, True),
    "ArrowDown": (0x28, True),
    "Delete": (0x2E, True),
}


# Win32 ctypes structures (matching exact x64 ABI: sizeof(INPUT) == 40)
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_union",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_union", _INPUT_UNION),
    ]


def normalize_to_virtual_desktop(
    nx: float,
    ny: float,
    monitor: MonitorInfo,
    v_left: int,
    v_top: int,
    v_width: int,
    v_height: int,
) -> tuple[int, int]:
    """Convert normalized [0..1] monitor coords to Windows SendInput 0..65535 virtual desktop coords."""
    cx = max(0.0, min(1.0, float(nx)))
    cy = max(0.0, min(1.0, float(ny)))
    px = monitor.left + cx * max(1, monitor.width - 1)
    py = monitor.top + cy * max(1, monitor.height - 1)
    abs_x = int(round(((px - v_left) * 65535.0) / max(1, v_width - 1)))
    abs_y = int(round(((py - v_top) * 65535.0) / max(1, v_height - 1)))
    return max(0, min(65535, abs_x)), max(0, min(65535, abs_y))


class InputInjector(ABC):
    @abstractmethod
    def mouse_move(self, nx: float, ny: float, monitor: MonitorInfo) -> None:
        ...

    @abstractmethod
    def mouse_button(
        self, button: int, down: bool, nx: float, ny: float, monitor: MonitorInfo
    ) -> None:
        ...

    @abstractmethod
    def mouse_wheel(self, dx: float, dy: float) -> None:
        ...

    @abstractmethod
    def key_event(self, code: str, down: bool, key: str = "") -> None:
        ...

    @abstractmethod
    def special_combo(self, combo: str) -> None:
        ...

    @abstractmethod
    def type_text(self, text: str) -> None:
        ...

    @abstractmethod
    def release_all_keys(self) -> None:
        ...


class Win32InputInjector(InputInjector):
    """Real Windows OS input injection via user32.SendInput."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32InputInjector only runs on Windows")
        self._user32 = ctypes.windll.user32
        self._user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = wintypes.UINT
        self._pressed_keys: set[tuple[int, bool]] = set()

    def _virtual_bounds(self) -> tuple[int, int, int, int]:
        vx = self._user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = self._user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = self._user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1920
        vh = self._user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1080
        return int(vx), int(vy), int(vw), int(vh)

    def _send(self, *items: INPUT) -> None:
        if not items:
            return
        arr = (INPUT * len(items))(*items)
        self._user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))

    def mouse_move(self, nx: float, ny: float, monitor: MonitorInfo) -> None:
        vx, vy, vw, vh = self._virtual_bounds()
        ax, ay = normalize_to_virtual_desktop(nx, ny, monitor, vx, vy, vw, vh)
        inp = INPUT(
            type=INPUT_MOUSE,
            mi=MOUSEINPUT(
                dx=ax,
                dy=ay,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=0,
            ),
        )
        self._send(inp)

    def mouse_button(
        self, button: int, down: bool, nx: float, ny: float, monitor: MonitorInfo
    ) -> None:
        self.mouse_move(nx, ny, monitor)
        mouse_data = 0
        if button == 0:
            flag = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
        elif button == 1:
            flag = MOUSEEVENTF_MIDDLEDOWN if down else MOUSEEVENTF_MIDDLEUP
        elif button == 2:
            flag = MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP
        elif button in (3, 4):
            flag = MOUSEEVENTF_XDOWN if down else MOUSEEVENTF_XUP
            mouse_data = 0x0001 if button == 3 else 0x0002
        else:
            return
        inp = INPUT(
            type=INPUT_MOUSE,
            mi=MOUSEINPUT(dx=0, dy=0, mouseData=mouse_data, dwFlags=flag, time=0, dwExtraInfo=0),
        )
        self._send(inp)

    def mouse_wheel(self, dx: float, dy: float) -> None:
        inputs: list[INPUT] = []
        if abs(dy) > 0.01:
            # Browser wheel deltaY > 0 means scroll down -> Win32 negative WHEEL_DELTA
            delta_y = int(round(-dy * 120))
            if delta_y != 0:
                inputs.append(
                    INPUT(
                        type=INPUT_MOUSE,
                        mi=MOUSEINPUT(
                            dx=0,
                            dy=0,
                            mouseData=delta_y & 0xFFFFFFFF,
                            dwFlags=MOUSEEVENTF_WHEEL,
                            time=0,
                            dwExtraInfo=0,
                        ),
                    )
                )
        if abs(dx) > 0.01:
            delta_x = int(round(dx * 120))
            if delta_x != 0:
                inputs.append(
                    INPUT(
                        type=INPUT_MOUSE,
                        mi=MOUSEINPUT(
                            dx=0,
                            dy=0,
                            mouseData=delta_x & 0xFFFFFFFF,
                            dwFlags=MOUSEEVENTF_HWHEEL,
                            time=0,
                            dwExtraInfo=0,
                        ),
                    )
                )
        self._send(*inputs)

    def _make_key_input(self, vk: int, extended: bool, down: bool) -> INPUT:
        scan = self._user32.MapVirtualKeyW(vk, 0) & 0xFFFF
        flags = 0 if down else KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0),
        )

    def key_event(self, code: str, down: bool, key: str = "") -> None:
        mapped = DOM_CODE_TO_VK.get(code) or KEY_NAME_TO_VK.get(key)
        if mapped is not None:
            vk, extended = mapped
            if down:
                self._pressed_keys.add((vk, extended))
            else:
                self._pressed_keys.discard((vk, extended))
            self._send(self._make_key_input(vk, extended, down))
            return

        # Fallback: printable single Unicode character when DOM code isn't mapped
        if len(key) == 1:
            ch = ord(key)
            flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
            inp = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(wVk=0, wScan=ch, dwFlags=flags, time=0, dwExtraInfo=0),
            )
            self._send(inp)

    def type_text(self, text: str) -> None:
        """Type an arbitrary string safely using Win32 KEYEVENTF_UNICODE events."""
        inputs: list[INPUT] = []
        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                inputs.append(self._make_key_input(0x0D, False, True))
                inputs.append(self._make_key_input(0x0D, False, False))
                continue
            utf16 = ch.encode("utf-16-le")
            for idx in range(0, len(utf16), 2):
                unit = int.from_bytes(utf16[idx : idx + 2], "little")
                inputs.append(
                    INPUT(
                        type=INPUT_KEYBOARD,
                        ki=KEYBDINPUT(wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0),
                    )
                )
                inputs.append(
                    INPUT(
                        type=INPUT_KEYBOARD,
                        ki=KEYBDINPUT(
                            wVk=0,
                            wScan=unit,
                            dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                            time=0,
                            dwExtraInfo=0,
                        ),
                    )
                )
        if inputs:
            self._send(*inputs)

    def special_combo(self, combo: str) -> None:
        combos: dict[str, list[tuple[int, bool]]] = {
            "win": [(0x5B, True)],
            "alt_tab": [(0xA4, False), (0x09, False)],
            "ctrl_shift_esc": [(0xA2, False), (0xA0, False), (0x1B, False)],
            "win_d": [(0x5B, True), (ord("D"), False)],
        }
        seq = combos.get(combo)
        if not seq:
            return
        downs = [self._make_key_input(vk, ext, True) for vk, ext in seq]
        ups = [self._make_key_input(vk, ext, False) for vk, ext in reversed(seq)]
        self._send(*(downs + ups))

    def release_all_keys(self) -> None:
        if not self._pressed_keys:
            return
        ups = [self._make_key_input(vk, ext, False) for vk, ext in list(self._pressed_keys)]
        self._pressed_keys.clear()
        self._send(*ups)


@dataclass
class MockInputInjector(InputInjector):
    """In-memory recorder for automated unit and Playwright E2E tests."""

    events: list[dict[str, Any]] = field(default_factory=list)
    released_count: int = 0

    def mouse_move(self, nx: float, ny: float, monitor: MonitorInfo) -> None:
        self.events.append({"type": "mouse_move", "nx": nx, "ny": ny, "monitor": monitor.index})

    def mouse_button(
        self, button: int, down: bool, nx: float, ny: float, monitor: MonitorInfo
    ) -> None:
        self.events.append(
            {"type": "mouse_button", "button": button, "down": down, "nx": nx, "ny": ny}
        )

    def mouse_wheel(self, dx: float, dy: float) -> None:
        self.events.append({"type": "mouse_wheel", "dx": dx, "dy": dy})

    def key_event(self, code: str, down: bool, key: str = "") -> None:
        self.events.append({"type": "key_event", "code": code, "down": down, "key": key})

    def special_combo(self, combo: str) -> None:
        self.events.append({"type": "special_combo", "combo": combo})

    def type_text(self, text: str) -> None:
        self.events.append({"type": "type_text", "text": text})

    def release_all_keys(self) -> None:
        self.released_count += 1
        self.events.append({"type": "release_all_keys"})


class ClipboardManager:
    """Thread-safe Windows CF_UNICODETEXT clipboard reader/writer with mock mode."""

    def __init__(self, mock: bool = False) -> None:
        self._mock = mock or sys.platform != "win32"
        self._mock_text = ""

    def get_text(self) -> str:
        if self._mock:
            return self._mock_text
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_text(self, text: str) -> bool:
        if self._mock:
            self._mock_text = text
            return True
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            encoded = (text + "\0").encode("utf-16-le")
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not h_mem:
                return False
            ptr = kernel32.GlobalLock(h_mem)
            if not ptr:
                kernel32.GlobalFree(h_mem)
                return False
            ctypes.memmove(ptr, encoded, len(encoded))
            kernel32.GlobalUnlock(h_mem)
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                kernel32.GlobalFree(h_mem)
                return False
            return True
        finally:
            user32.CloseClipboard()
