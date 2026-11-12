"""Visible Chrome automation for the Bizpeople attendance Quick Actions."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "https://bizprospex-hr.vercel.app/dashboard"
PROFILE_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    / "BizProspexCheckin"
    / "ChromeProfile"
)
LOGIN_WAIT_SECONDS = 5 * 60
ACTION_LABELS = {"check_in": "Clock In", "check_out": "Clock Out"}


def run_attendance_action(action: str) -> int:
    """Open the saved Bizpeople Chrome profile and perform one requested action."""
    label = ACTION_LABELS.get(action)
    if label is None:
        raise ValueError("Unsupported Bizpeople attendance action.")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    context = None
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--start-maximized"],
                no_viewport=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000)

            target = page.get_by_role(
                "button", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)
            )
            deadline = time.monotonic() + LOGIN_WAIT_SECONDS
            while time.monotonic() < deadline:
                if page.is_closed():
                    return 1
                try:
                    if target.count() and target.first.is_visible():
                        button = target.first
                        if not button.is_enabled():
                            # Do not repeat an action the dashboard currently disables.
                            break
                        # WebDesk already requires explicit confirmation before this action.
                        page.on("dialog", lambda dialog: dialog.accept())
                        button.click(timeout=15_000)
                        break
                except PlaywrightError:
                    if page.is_closed():
                        return 1
                page.wait_for_timeout(500)

            # Keep the result visible. Closing Chrome releases its persistent profile.
            while context.pages:
                time.sleep(0.5)
            return 0
    finally:
        if context is not None:
            try:
                context.close()
            except PlaywrightError:
                pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m host.bizpeople_attendance check_in|check_out")
    raise SystemExit(run_attendance_action(sys.argv[1]))
