"""Desktop Control Center GUI (Tkinter) for WebDesk Remote."""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
import webbrowser

import qrcode

from host.agent import create_ephemeral_support_agent
from host.config_store import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    initialize_host_config,
    load_host_config,
)
from host.launcher import (
    _is_pid_running,
    _read_pid,
    configure_cloud_server,
    disable_autostart,
    discover_lan_ip,
    enable_autostart,
    ensure_server_running,
    fetch_health,
    is_autostart_enabled,
    load_service_prefs,
    normalize_cloud_ws_url,
    reset_host_pin,
    save_service_prefs,
    start_all,
    start_supervisor,
    stop_all,
    ws_url_to_http_base,
)
from host.tunnel import start_cloudflared_tunnel

BG = "#0b101b"
CARD = "#141c2c"
SURFACE = "#1c263b"
TEXT = "#eef3fb"
MUTED = "#9aa9c4"
ACCENT = "#4f8cff"
SUCCESS = "#34c77b"
DANGER = "#e25555"


def draw_qr_on_canvas(canvas: tk.Canvas, url: str) -> None:
    """Render a scannable QR code directly onto a Tkinter Canvas without image deps."""
    canvas.delete("all")
    if not url:
        canvas.configure(bg=SURFACE)
        canvas.create_text(
            int(canvas["width"]) // 2,
            int(canvas["height"]) // 2,
            text="Phone QR\nappears here",
            fill=MUTED,
            font=("Segoe UI", 9),
            justify="center",
        )
        return

    qr = qrcode.QRCode(border=2, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    cw = int(canvas["width"])
    ch = int(canvas["height"])
    cell = max(1, min(cw, ch) // size)
    pad_x = (cw - cell * size) // 2
    pad_y = (ch - cell * size) // 2

    canvas.configure(bg="#ffffff")
    canvas.create_rectangle(0, 0, cw, ch, fill="#ffffff", outline="")
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                x0 = pad_x + c * cell
                y0 = pad_y + r * cell
                canvas.create_rectangle(
                    x0, y0, x0 + cell, y0 + cell, fill="#0b101b", outline=""
                )


class WebDeskControlCenter:
    def __init__(self, port: int = 8080, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.port = port
        self.config_path = config_path
        self.state_dir = config_path.parent
        self.root = tk.Tk()
        self.root.title("WebDesk Remote - Control Center")
        self.root.geometry("700x720")
        self.root.minsize(650, 660)
        self.root.configure(bg=BG)

        self._support_agent = None
        self._support_thread: threading.Thread | None = None
        self._busy = False
        self._closing = False

        self._build_ui()
        self._load_existing_config()
        self._start_status_poller()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        header = tk.Frame(self.root, bg=CARD, padx=18, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text="WebDesk Remote",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 15, "bold"),
        ).pack(side="left")
        self.lbl_overall = tk.Label(
            header,
            text="Checking...",
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=4,
        )
        self.lbl_overall.pack(side="right")

        body = tk.Frame(self.root, bg=BG, padx=16, pady=14)
        body.pack(fill="both", expand=True)

        # Card 1: Host & PIN Configuration
        pin_card = tk.Frame(body, bg=CARD, padx=16, pady=12, highlightbackground="#2a3753", highlightthickness=1)
        pin_card.pack(fill="x", pady=(0, 12))

        tk.Label(
            pin_card,
            text="1. THIS PC & ACCESS PIN",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        row1 = tk.Frame(pin_card, bg=CARD)
        row1.pack(fill="x", pady=4)
        tk.Label(row1, text="PC Name:", bg=CARD, fg=TEXT, width=14, anchor="w", font=("Segoe UI", 10)).pack(side="left")
        self.var_name = tk.StringVar(value=platform.node() or "My-Windows-PC")
        tk.Entry(row1, textvariable=self.var_name, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).pack(side="left", fill="x", expand=True, ipady=5, padx=6)

        row2 = tk.Frame(pin_card, bg=CARD)
        row2.pack(fill="x", pady=4)
        tk.Label(row2, text="New PIN (6+):", bg=CARD, fg=TEXT, width=14, anchor="w", font=("Segoe UI", 10)).pack(side="left")
        self.var_pin1 = tk.StringVar()
        self.entry_pin1 = tk.Entry(row2, textvariable=self.var_pin1, show="*", bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 10))
        self.entry_pin1.pack(side="left", fill="x", expand=True, ipady=5, padx=6)

        tk.Label(row2, text="Confirm:", bg=CARD, fg=TEXT, font=("Segoe UI", 10)).pack(side="left", padx=(6, 0))
        self.var_pin2 = tk.StringVar()
        self.entry_pin2 = tk.Entry(row2, textvariable=self.var_pin2, show="*", bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 10))
        self.entry_pin2.pack(side="left", fill="x", expand=True, ipady=5, padx=6)

        row3 = tk.Frame(pin_card, bg=CARD)
        row3.pack(fill="x", pady=(6, 0))
        self.var_show_pin = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row3,
            text="Show PIN characters",
            variable=self.var_show_pin,
            command=self._toggle_pin_visibility,
            bg=CARD,
            fg=MUTED,
            selectcolor=BG,
            activebackground=CARD,
            activeforeground=TEXT,
            font=("Segoe UI", 9),
        ).pack(side="left")
        self.lbl_pin_state = tk.Label(row3, text="", bg=CARD, fg=SUCCESS, font=("Segoe UI", 9))
        self.lbl_pin_state.pack(side="left", padx=12)
        tk.Button(
            row3,
            text="Save PIN & Apply",
            command=self._on_save_pin,
            bg=ACCENT,
            fg="#ffffff",
            relief="flat",
            padx=12,
            pady=5,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="right")

        # Card 2: Launch & Phone QR
        ctrl_card = tk.Frame(body, bg=CARD, padx=16, pady=12, highlightbackground="#2a3753", highlightthickness=1)
        ctrl_card.pack(fill="both", expand=True)

        tk.Label(
            ctrl_card,
            text="2. CONNECT & PHONE ACCESS",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        split = tk.Frame(ctrl_card, bg=CARD)
        split.pack(fill="both", expand=True)

        left = tk.Frame(split, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))

        btn_cfg = {"relief": "flat", "pady": 8, "padx": 12, "font": ("Segoe UI", 10, "bold"), "cursor": "hand2"}
        tk.Button(left, text="Start Local & Wi-Fi Host", command=self._on_start_local, bg=ACCENT, fg="#fff", **btn_cfg).pack(fill="x", pady=3)
        tk.Button(left, text="Start Free Phone Access (HTTPS + QR)", command=self._on_start_phone, bg="#247a56", fg="#fff", **btn_cfg).pack(fill="x", pady=3)
        tk.Button(left, text="Open Browser Portal", command=self._on_open_portal, bg=SURFACE, fg=TEXT, **btn_cfg).pack(fill="x", pady=3)
        tk.Button(left, text="Share 12-Digit Support Code", command=self._on_share_support, bg=SURFACE, fg=TEXT, **btn_cfg).pack(fill="x", pady=3)

        row_bottom = tk.Frame(left, bg=CARD)
        row_bottom.pack(fill="x", pady=(6, 0))
        tk.Button(row_bottom, text="4G/5G TURN Setup", command=self._on_turn_dialog, bg=SURFACE, fg=MUTED, relief="flat", pady=6, padx=10, font=("Segoe UI", 9), cursor="hand2").pack(side="left", fill="x", expand=True, padx=(0, 4))
        tk.Button(row_bottom, text="Stop All", command=self._on_stop_all, bg=DANGER, fg="#fff", relief="flat", pady=6, padx=10, font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="right", fill="x", expand=True, padx=(4, 0))

        self.var_urls = tk.StringVar(value=f"Local: http://127.0.0.1:{self.port}\nWi-Fi: http://{discover_lan_ip()}:{self.port}")
        tk.Label(left, textvariable=self.var_urls, bg=BG, fg=MUTED, justify="left", anchor="w", padx=10, pady=8, font=("Consolas", 9)).pack(fill="x", pady=(10, 0))

        right = tk.Frame(split, bg=CARD, width=210)
        right.pack(side="right", fill="y")
        self.qr_canvas = tk.Canvas(right, width=180, height=180, bg=SURFACE, highlightthickness=1, highlightbackground="#2a3753")
        self.qr_canvas.pack(pady=(2, 6))
        draw_qr_on_canvas(self.qr_canvas, "")

        self.var_tunnel = tk.StringVar(value="Phone tunnel off")
        tk.Label(right, textvariable=self.var_tunnel, bg=CARD, fg="#9dc2ff", wraplength=190, font=("Segoe UI", 8)).pack()
        tk.Button(right, text="Copy Phone URL", command=self._on_copy_tunnel, bg=SURFACE, fg=TEXT, relief="flat", padx=8, pady=4, font=("Segoe UI", 8, "bold"), cursor="hand2").pack(pady=4)

        # Card 3: Always-On Background Service (Chrome Remote Desktop style)
        svc_card = tk.Frame(body, bg=CARD, padx=16, pady=12, highlightbackground="#2a3753", highlightthickness=1)
        svc_card.pack(fill="x", pady=(12, 0))

        tk.Label(
            svc_card,
            text="3. ALWAYS-ON BACKGROUND SERVICE (CHROME REMOTE DESKTOP STYLE)",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        prefs = load_service_prefs(self.state_dir)
        self.var_autostart = tk.BooleanVar(value=is_autostart_enabled())
        self.var_auto_tunnel = tk.BooleanVar(value=bool(prefs.get("autostart_tunnel", True)))
        self.var_keep_awake = tk.BooleanVar(value=bool(prefs.get("keep_awake", False)))
        self.var_custom_server = tk.StringVar(value=str(prefs.get("custom_server_url") or ""))

        chk_cfg = {
            "bg": CARD,
            "fg": TEXT,
            "selectcolor": BG,
            "activebackground": CARD,
            "activeforeground": TEXT,
            "font": ("Segoe UI", 9),
        }
        chk_row = tk.Frame(svc_card, bg=CARD)
        chk_row.pack(fill="x")
        tk.Checkbutton(chk_row, text="Auto-start at Windows login", variable=self.var_autostart, **chk_cfg).pack(side="left", padx=(0, 12))
        tk.Checkbutton(chk_row, text="Auto-start Phone HTTPS Tunnel", variable=self.var_auto_tunnel, **chk_cfg).pack(side="left", padx=(0, 12))
        tk.Checkbutton(chk_row, text="Keep PC awake", variable=self.var_keep_awake, **chk_cfg).pack(side="left")

        url_row = tk.Frame(svc_card, bg=CARD)
        url_row.pack(fill="x", pady=(8, 0))
        tk.Label(url_row, text="Fixed Cloud WS (Optional):", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        tk.Entry(url_row, textvariable=self.var_custom_server, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True, ipady=4, padx=8)
        tk.Button(
            url_row,
            text="Apply & Run in Background",
            command=self._on_apply_service_settings,
            bg="#247a56",
            fg="#ffffff",
            relief="flat",
            padx=12,
            pady=5,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="right")

        self.lbl_log = tk.Label(self.root, text="Ready.", bg=BG, fg=MUTED, anchor="w", padx=16, pady=6, font=("Segoe UI", 9))
        self.lbl_log.pack(fill="x")

    def _toggle_pin_visibility(self) -> None:
        ch = "" if self.var_show_pin.get() else "*"
        self.entry_pin1.configure(show=ch)
        self.entry_pin2.configure(show=ch)

    def _load_existing_config(self) -> None:
        try:
            cfg = load_host_config(self.config_path)
            self.var_name.set(cfg.host_name)
            self.lbl_pin_state.configure(text="PIN configured", fg=SUCCESS)
        except FileNotFoundError:
            self.lbl_pin_state.configure(text="No PIN set yet", fg="#f0b429")

    def _set_status(self, msg: str) -> None:
        self.root.after(0, lambda: self.lbl_log.configure(text=msg))

    def _run_bg(self, fn) -> None:
        if self._busy:
            return
        self._busy = True

        def _worker() -> None:
            try:
                fn()
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("WebDesk Remote", str(exc)))
                self._set_status(f"Error: {exc}")
            finally:
                self._busy = False
                self.root.after(0, self._update_status_once)

        threading.Thread(target=_worker, daemon=True).start()

    def _ensure_configured_or_warn(self) -> bool:
        try:
            load_host_config(self.config_path)
            return True
        except FileNotFoundError:
            pin1 = self.var_pin1.get().strip()
            if len(pin1) >= 6 and pin1 == self.var_pin2.get().strip():
                initialize_host_config(
                    host_name=self.var_name.get().strip() or platform.node(),
                    pin=pin1,
                    server_url=f"ws://127.0.0.1:{self.port}/ws/host",
                    path=self.config_path,
                )
                self.lbl_pin_state.configure(text="PIN saved!", fg=SUCCESS)
                return True
            messagebox.showwarning(
                "PIN Required",
                "Enter a matching 6+ character Access PIN above and click 'Save PIN & Apply' first.",
            )
            return False

    def _on_save_pin(self) -> None:
        name = self.var_name.get().strip() or platform.node() or "My-Windows-PC"
        pin1 = self.var_pin1.get().strip()
        pin2 = self.var_pin2.get().strip()
        if len(pin1) < 6:
            messagebox.showwarning("Invalid PIN", "PIN must be at least 6 characters/digits.")
            return
        if pin1 != pin2:
            messagebox.showwarning("Mismatch", "PINs do not match.")
            return

        def task() -> None:
            self._set_status("Saving PIN and restarting background host...")
            was_running = fetch_health(f"http://127.0.0.1:{self.port}") is not None
            stop_all(self.state_dir)
            reset_host_pin(pin1, config_path=self.config_path, host_name=name, port=self.port)
            if was_running:
                start_all(port=self.port, config_path=self.config_path, state_dir=self.state_dir, open_browser=False)
            self.root.after(0, lambda: (
                self.var_pin1.set(""),
                self.var_pin2.set(""),
                self.lbl_pin_state.configure(text="PIN updated!", fg=SUCCESS),
            ))
            self._set_status("PIN saved cleanly.")

        self._run_bg(task)

    def _on_start_local(self) -> None:
        if not self._ensure_configured_or_warn():
            return

        def task() -> None:
            self._set_status("Starting signaling server and Windows host...")
            res = start_all(port=self.port, config_path=self.config_path, state_dir=self.state_dir, open_browser=False)
            self._set_status(f"Running locally at {res['base_url']} and {res['lan_url']}")

        self._run_bg(task)

    def _on_start_phone(self) -> None:
        if not self._ensure_configured_or_warn():
            return

        def task() -> None:
            self._set_status("Starting host + free Cloudflare HTTPS tunnel...")
            start_all(port=self.port, config_path=self.config_path, state_dir=self.state_dir, open_browser=False)
            url = start_cloudflared_tunnel(self.port, self.state_dir)
            self._set_status(f"Phone Access Ready: {url}")

        self._run_bg(task)

    def _on_stop_all(self) -> None:
        def task() -> None:
            if self._support_agent:
                self._support_agent.stop()
            stop_all(self.state_dir)
            self._set_status("All background WebDesk services stopped.")

        self._run_bg(task)

    def _active_phone_url(self) -> str:
        prefs = load_service_prefs(self.state_dir)
        custom_ws = str(prefs.get("custom_server_url") or "").strip()
        if custom_ws:
            return ws_url_to_http_base(custom_ws)
        url_file = self.state_dir / "tunnel_url.txt"
        return url_file.read_text(encoding="utf-8").strip() if url_file.exists() else ""

    def _on_open_portal(self) -> None:
        prefs = load_service_prefs(self.state_dir)
        custom_ws = str(prefs.get("custom_server_url") or "").strip()
        target = ws_url_to_http_base(custom_ws) if custom_ws else f"http://127.0.0.1:{self.port}"
        webbrowser.open(target)

    def _on_apply_service_settings(self) -> None:
        if not self._ensure_configured_or_warn():
            return
        auto_boot = bool(self.var_autostart.get())
        auto_tun = bool(self.var_auto_tunnel.get())
        keep_awake = bool(self.var_keep_awake.get())
        raw_server = self.var_custom_server.get().strip()
        try:
            custom_ws = normalize_cloud_ws_url(raw_server)
        except ValueError as exc:
            messagebox.showerror("Invalid Cloud URL", str(exc))
            return
        self.var_custom_server.set(custom_ws)

        def task() -> None:
            self._set_status("Applying always-on background service settings...")
            configure_cloud_server(
                url=custom_ws,
                config_path=self.config_path,
                state_dir=self.state_dir,
                port=self.port,
                keep_awake=keep_awake,
            )
            save_service_prefs(
                {
                    "port": self.port,
                    "autostart_tunnel": auto_tun,
                    "keep_awake": keep_awake,
                    "custom_server_url": custom_ws,
                },
                state_dir=self.state_dir,
            )
            if auto_boot:
                enable_autostart(config_path=self.config_path)
            else:
                disable_autostart()
            stop_all(self.state_dir)
            start_supervisor(port=self.port, config_path=self.config_path, state_dir=self.state_dir)
            mode_txt = "Windows Startup ENABLED + Background service active!" if auto_boot else "Background service started (auto-startup disabled)."
            self._set_status(mode_txt)

        self._run_bg(task)

    def _on_copy_tunnel(self) -> None:
        url = self._active_phone_url()
        if not url:
            messagebox.showinfo("Phone URL", "Start 'Free Phone Access' or set a Fixed Cloud URL first.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._set_status(f"Copied {url} to clipboard.")

    def _on_share_support(self) -> None:
        if self._support_agent is not None:
            messagebox.showinfo("Support Active", "A support session is already active.")
            return

        def _worker() -> None:
            self._set_status("Generating 12-digit Remote Support code...")
            ensure_server_running(self.port, self.state_dir)

            async def _run() -> None:
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

                def on_code(_raw: str, formatted: str, _exp: float) -> None:
                    self.root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Remote Support Code",
                            f"Share this one-time code:\n\n{formatted}\n\nKeep this window open until the helper connects.",
                        ),
                    )
                    self._set_status(f"Support Code Ready: {formatted}")

                agent = create_ephemeral_support_agent(
                    server_url=f"ws://127.0.0.1:{self.port}/ws/host",
                    on_support_code=on_code,
                )

                async def gui_consent(viewer_label: str, _sid: str) -> bool:
                    ev = threading.Event()
                    decision = [False]

                    def ask() -> None:
                        decision[0] = messagebox.askyesno(
                            "Allow Remote Support?",
                            f"'{viewer_label}' wants to view and control your PC.\nAllow access?",
                        )
                        ev.set()

                    self.root.after(0, ask)
                    await asyncio.get_running_loop().run_in_executor(None, ev.wait)
                    return decision[0]

                agent.consent_callback = gui_consent
                self._support_agent = agent
                try:
                    await agent.run_forever()
                finally:
                    self._support_agent = None

            asyncio.run(_run())

        self._support_thread = threading.Thread(target=_worker, daemon=True)
        self._support_thread.start()

    def _on_turn_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Configure 4G/5G TURN Relay (Optional)")
        dlg.geometry("460x280")
        dlg.configure(bg=CARD)

        tk.Label(
            dlg,
            text="Paste your free Metered.ca (or custom) TURN details for strict 4G/5G cellular networks:",
            bg=CARD,
            fg=MUTED,
            wraplength=420,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=(12, 8))

        def field(label: str, default: str = "") -> tk.StringVar:
            row = tk.Frame(dlg, bg=CARD)
            row.pack(fill="x", padx=16, pady=4)
            tk.Label(row, text=label, bg=CARD, fg=TEXT, width=13, anchor="w", font=("Segoe UI", 9)).pack(side="left")
            var = tk.StringVar(value=default)
            tk.Entry(row, textvariable=var, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", fill="x", expand=True, ipady=4)
            return var

        v_url = field("TURN URL:", "turn:global.relay.metered.ca:80")
        v_user = field("Username:")
        v_cred = field("Credential:")

        def save_turn() -> None:
            ice = [
                {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]},
            ]
            if v_url.get().strip() and v_user.get().strip():
                ice.append(
                    {
                        "urls": v_url.get().strip(),
                        "username": v_user.get().strip(),
                        "credential": v_cred.get().strip(),
                    }
                )
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "ice_servers.json").write_text(json.dumps(ice, indent=2), encoding="utf-8")
            dlg.destroy()
            messagebox.showinfo("Saved", "Saved ICE/TURN config! Restart services ('Stop All' -> 'Start') to apply.")

        tk.Button(dlg, text="Save ICE/TURN Config", command=save_turn, bg=ACCENT, fg="#fff", relief="flat", padx=14, pady=6, font=("Segoe UI", 9, "bold")).pack(pady=14)

    def _update_status_once(self) -> None:
        prefs = load_service_prefs(self.state_dir)
        custom_ws = str(prefs.get("custom_server_url") or "").strip()
        cloud_base = ws_url_to_http_base(custom_ws) if custom_ws else ""
        health_target = cloud_base or f"http://127.0.0.1:{self.port}"
        health = fetch_health(health_target, timeout=0.6 if cloud_base else 0.25)
        lan_ip = discover_lan_ip()
        url_file = self.state_dir / "tunnel_url.txt"
        tun_url = cloud_base or (url_file.read_text(encoding="utf-8").strip() if url_file.exists() else "")
        self.root.after(0, lambda: self._apply_status_ui(health, lan_ip, tun_url, cloud_base))

    def _apply_status_ui(self, health, lan_ip: str, tun_url: str, cloud_base: str = "") -> None:
        if self._closing:
            return
        if health:
            hosts_on = int(health.get("online_hosts", 0))
            prefix = "Cloud OK" if cloud_base else "Server OK"
            self.lbl_overall.configure(
                text=f"{prefix} • {hosts_on} host online",
                fg=SUCCESS if hosts_on > 0 else "#f0b429",
            )
        else:
            host_pid = _read_pid(self.state_dir / "host.pid")
            if cloud_base and host_pid and _is_pid_running(host_pid):
                self.lbl_overall.configure(text="Cloud sleeping / reconnecting...", fg="#f0b429")
            else:
                self.lbl_overall.configure(text="Stopped", fg=MUTED)

        if cloud_base:
            lines = [f"Fixed Cloud: {cloud_base}", f"Local Host: running on {self.state_dir}"]
        else:
            lines = [f"Local: http://127.0.0.1:{self.port}", f"Wi-Fi: http://{lan_ip}:{self.port}"]
        if tun_url:
            lines.append(f"Phone: {tun_url}")
            if self.var_tunnel.get() != tun_url:
                self.var_tunnel.set(tun_url)
                draw_qr_on_canvas(self.qr_canvas, tun_url)
        else:
            if self.var_tunnel.get() != "Phone tunnel off":
                self.var_tunnel.set("Phone tunnel off")
                draw_qr_on_canvas(self.qr_canvas, "")
        self.var_urls.set("\n".join(lines))

    def _start_status_poller(self) -> None:
        def _poll() -> None:
            import time

            while not self._closing:
                try:
                    self._update_status_once()
                except Exception:
                    pass
                time.sleep(2.0)

        threading.Thread(target=_poll, daemon=True).start()

    def _on_close(self) -> None:
        host_pid = _read_pid(self.state_dir / "host.pid")
        sup_pid = _read_pid(self.state_dir / "supervisor.pid")
        running = (
            fetch_health(f"http://127.0.0.1:{self.port}", timeout=0.25) is not None
            or (host_pid is not None and _is_pid_running(host_pid))
            or (sup_pid is not None and _is_pid_running(sup_pid))
        )
        if running:
            choice = messagebox.askyesnocancel(
                "WebDesk Remote",
                "Remote access is currently running.\n\n"
                "Yes = Keep running in background\n"
                "No = Stop all services and exit",
            )
            if choice is None:
                return
            if choice is False:
                if self._support_agent:
                    self._support_agent.stop()
                stop_all(self.state_dir)
        self._closing = True
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = WebDeskControlCenter()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
