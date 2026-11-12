"""Free zero-signup Cloudflare Quick Tunnel helper for HTTPS mobile access."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CLOUDFLARED_WIN_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
)
_TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def parse_tunnel_url(text: str) -> str | None:
    """Extract a trycloudflare.com HTTPS URL from cloudflared log output."""
    match = _TRYCLOUDFLARE_RE.search(text or "")
    return match.group(0) if match else None


def find_cloudflared(bin_dir: Path) -> Path | None:
    found = shutil.which("cloudflared")
    if found:
        return Path(found)

    candidates = [
        bin_dir / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared"),
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def ensure_cloudflared(bin_dir: Path) -> Path:
    """Return path to cloudflared binary, downloading the official Windows standalone exe if absent."""
    existing = find_cloudflared(bin_dir)
    if existing:
        return existing

    if sys.platform != "win32":
        raise FileNotFoundError(
            "cloudflared is not installed. Install it via your package manager to use phone tunnel."
        )

    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / "cloudflared.exe"
    tmp_dest = bin_dir / "cloudflared.exe.part"
    print("Downloading official cloudflared binary (~35 MB, one-time setup)...")
    urllib.request.urlretrieve(CLOUDFLARED_WIN_URL, tmp_dest)
    tmp_dest.replace(dest)
    return dest


def start_cloudflared_tunnel(
    port: int,
    state_dir: Path,
    timeout: float = 30.0,
) -> str:
    """Start Cloudflare Quick Tunnel for http://127.0.0.1:<port> and return public HTTPS URL."""
    state_dir.mkdir(parents=True, exist_ok=True)
    url_file = state_dir / "tunnel_url.txt"
    pid_file = state_dir / "tunnel.pid"
    log_path = state_dir / "tunnel.log"

    # Reuse existing tunnel if still alive
    if pid_file.exists() and url_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            url = url_file.read_text(encoding="utf-8").strip()
            if url.startswith("https://") and _pid_alive(pid):
                return url
        except (ValueError, OSError):
            pass

    exe = ensure_cloudflared(state_dir / "bin")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    # Try IPv4 edge first (more reliable on CGNAT/IPv6 ISPs), then default edge on retry.
    attempt_flags = (["--edge-ip-version", "4"], [])
    last_error = ""
    per_attempt = max(8.0, timeout / len(attempt_flags))

    for extra_flags in attempt_flags:
        url_file.unlink(missing_ok=True)
        log_path.write_text("", encoding="utf-8")
        log_handle = log_path.open("ab")
        proc = subprocess.Popen(
            [
                str(exe),
                "tunnel",
                "--no-autoupdate",
                *extra_flags,
                "--url",
                f"http://127.0.0.1:{port}",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        log_handle.close()
        pid_file.write_text(str(proc.pid), encoding="utf-8")

        deadline = time.monotonic() + per_attempt
        while time.monotonic() < deadline:
            try:
                content = log_path.read_text(encoding="utf-8", errors="ignore")
                found = parse_tunnel_url(content)
                if found:
                    url_file.write_text(found, encoding="utf-8")
                    return found
            except OSError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.35)

        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        pid_file.unlink(missing_ok=True)
        lines = [
            ln.strip()
            for ln in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip()
        ]
        if lines:
            last_error = lines[-1]

    detail = f" ({last_error})" if last_error else ""
    raise RuntimeError(
        f"Cloudflare Tunnel failed to start within {timeout:.0f}s{detail}. Check {log_path}"
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in res.stdout.splitlines():
            if f'"{pid}"' in line and "cloudflared" in line.lower():
                return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
