"""Fullscreen privacy shield overlay spawned on Windows when Privacy Lock is engaged."""

from __future__ import annotations

import sys
import tkinter as tk


def main() -> int:
    root = tk.Tk()
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
        text="Locked remotely via WebDesk Quick Actions.\nTap 'Open Lock' on your phone to unlock and wake the display.",
        bg="#05070c",
        fg="#9aa9c4",
        font=("Segoe UI", 13),
        justify="center",
    ).pack()

    def _keep_focus() -> None:
        try:
            root.lift()
            root.focus_force()
            root.after(1500, _keep_focus)
        except Exception:
            pass

    root.after(500, _keep_focus)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
