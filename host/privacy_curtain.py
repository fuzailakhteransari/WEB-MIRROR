"""Fullscreen privacy shield overlay spawned on Windows when Privacy Lock is engaged."""

from __future__ import annotations

import sys
import time
import tkinter as tk


def build_curtain_ui(root: tk.Tk) -> dict[str, object]:
    """Attach the Privacy Lock overlay UI and local emergency unlock controls to a Tk root."""
    root.title("WebDesk Privacy Shield")
    root.configure(bg="#05070c")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)

    if sys.platform == "win32":
        # Span full virtual desktop across multiple monitors when possible
        try:
            import ctypes

            user32 = ctypes.windll.user32
            vx = user32.GetSystemMetrics(76)
            vy = user32.GetSystemMetrics(77)
            vw = user32.GetSystemMetrics(78) or root.winfo_screenwidth()
            vh = user32.GetSystemMetrics(79) or root.winfo_screenheight()
            root.overrideredirect(True)
            root.geometry(f"{vw}x{vh}+{vx}+{vy}")
        except Exception:
            pass

    frame = tk.Frame(root, bg="#05070c")
    frame.place(relx=0.5, rely=0.5, anchor="center")

    tk.Label(
        frame,
        text="PC Privacy Lock Active",
        bg="#05070c",
        fg="#eef3fb",
        font=("Segoe UI", 28, "bold"),
    ).pack(pady=(0, 12))

    tk.Label(
        frame,
        text=(
            "Locked remotely via WebDesk Quick Actions.\n"
            "Tap 'Open Lock' on your phone — OR press Esc 3 times / Ctrl+Alt+U on this PC."
        ),
        bg="#05070c",
        fg="#9aa9c4",
        font=("Segoe UI", 13),
        justify="center",
    ).pack()

    hint_var = tk.StringVar(value="")
    lbl_hint = tk.Label(
        frame,
        textvariable=hint_var,
        bg="#05070c",
        fg="#f0b429",
        font=("Segoe UI", 11, "bold"),
    )
    lbl_hint.pack(pady=(14, 14))

    state: dict[str, object] = {"unlocked": False, "esc_times": []}

    def unlock(_event: object = None) -> str:
        if state["unlocked"]:
            return "break"
        state["unlocked"] = True
        try:
            root.destroy()
        except Exception:
            pass
        return "break"

    def on_escape(_event: object = None) -> str:
        now = time.monotonic()
        recent = [t for t in state["esc_times"] if now - t <= 2.5]  # type: ignore[operator]
        recent.append(now)
        state["esc_times"] = recent
        remaining = 3 - len(recent)
        if remaining <= 0:
            return unlock()
        hint_var.set(f"Press Esc {remaining} more time{'s' if remaining > 1 else ''} (or Ctrl+Alt+U) to unlock locally")
        return "break"

    unlock_btn = tk.Button(
        frame,
        text="Unlock This PC Locally",
        command=unlock,
        bg="#1c263b",
        fg="#eef3fb",
        activebackground="#2a3753",
        activeforeground="#ffffff",
        relief="flat",
        padx=18,
        pady=8,
        font=("Segoe UI", 10, "bold"),
        cursor="hand2",
    )
    unlock_btn.pack()

    root.bind_all("<Escape>", on_escape)
    root.bind_all("<Control-Alt-u>", unlock)
    root.bind_all("<Control-Alt-U>", unlock)

    state["unlock_btn"] = unlock_btn
    state["hint_var"] = hint_var
    state["unlock"] = unlock
    state["on_escape"] = on_escape
    return state


def main() -> int:
    root = tk.Tk()
    state = build_curtain_ui(root)

    def _keep_topmost() -> None:
        if state.get("unlocked"):
            return
        try:
            root.lift()
            root.focus_set()
            root.after(1800, _keep_topmost)
        except Exception:
            pass

    root.after(400, _keep_topmost)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
