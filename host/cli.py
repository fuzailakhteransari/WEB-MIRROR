"""CLI entrypoint for setting up, starting, and sharing WebDesk Remote host."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import platform
import sys
from pathlib import Path

from host.agent import HostAgent, create_ephemeral_support_agent
from host.capture import MssDesktopCapturer, SyntheticCapturer
from host.config_store import DEFAULT_CONFIG_PATH, initialize_host_config, load_host_config
from host.input_win32 import ClipboardManager, MockInputInjector, Win32InputInjector
from host.native_messaging import install_chrome_native_host_manifest, run_native_messaging_stdio
from host.quick_actions import MockQuickActionExecutor, Win32QuickActionExecutor


def _configure_event_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def cmd_setup(args: argparse.Namespace) -> int:
    name = args.name or input(f"Host name [{platform.node()}]: ").strip() or platform.node()
    pin = args.pin or getpass.getpass("Enter host PIN (min 6 chars/digits): ").strip()
    cfg = initialize_host_config(
        host_name=name,
        pin=pin,
        server_url=args.server,
        workspace_token=args.token,
        path=Path(args.config),
    )
    print(f"Configured host '{cfg.host_name}' (host_id={cfg.host_id})")
    print(f"Config saved to {args.config} (PIN stored as salted SRP-6a verifier only)")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _configure_event_loop()
    cfg_path = Path(args.config)
    cfg = load_host_config(cfg_path)
    if args.server:
        cfg.server_url = args.server
    capturer = SyntheticCapturer() if args.mock_desktop else MssDesktopCapturer()
    injector = (
        MockInputInjector()
        if args.mock_desktop
        else (Win32InputInjector() if sys.platform == "win32" else MockInputInjector())
    )
    clipboard = ClipboardManager(mock=args.mock_desktop)
    quick_actions = (
        MockQuickActionExecutor()
        if args.mock_desktop
        else (Win32QuickActionExecutor() if sys.platform == "win32" else MockQuickActionExecutor())
    )
    if getattr(args, "keep_awake", False):
        quick_actions.execute("keep_awake_on")
    agent = HostAgent(
        config=cfg,
        config_path=cfg_path,
        mode="me2me",
        capturer=capturer,
        injector=injector,
        clipboard=clipboard,
        quick_actions=quick_actions,
    )
    print(f"Starting Me2Me host '{cfg.host_name}' ({cfg.host_id}) -> {cfg.server_url}")
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        print("\nHost stopped.")
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _configure_event_loop()

    def on_code(_code: str, formatted: str, _exp: float) -> None:
        print(f"\n=== Remote Support Code: {formatted} ===")
        print("Share this 12-digit code with the helper at the WebDesk portal.")

    agent = create_ephemeral_support_agent(
        server_url=args.server,
        workspace_token=args.token,
        host_name=args.name,
        mock_desktop=args.mock_desktop,
        auto_accept=args.auto_accept,
        on_support_code=on_code,
    )
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        print("\nSupport session closed.")
    return 0


def cmd_native_messaging(args: argparse.Namespace) -> int:
    if args.install_manifest:
        path = install_chrome_native_host_manifest(
            extension_id=args.extension_id,
            config_path=Path(args.config),
        )
        print(f"Installed Chrome Native Messaging manifest at {path}")
        return 0
    return run_native_messaging_stdio(config_path=Path(args.config))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webdesk-host", description="WebDesk Remote Host CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Configure unattended Remote Access (Me2Me) host")
    p_setup.add_argument("--name", default="", help="Display name for this PC")
    p_setup.add_argument("--pin", default="", help="Access PIN (minimum 6 chars)")
    p_setup.add_argument("--server", default="ws://127.0.0.1:8080/ws/host", help="Signaling WebSocket URL")
    p_setup.add_argument("--token", default="", help="Optional workspace access token")
    p_setup.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config JSON path")
    p_setup.set_defaults(func=cmd_setup)

    p_start = sub.add_parser("start", help="Run unattended Remote Access (Me2Me) host")
    p_start.add_argument("--server", default="", help="Override signaling WebSocket URL")
    p_start.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config JSON path")
    p_start.add_argument("--mock-desktop", action="store_true", help="Use synthetic video/mock input")
    p_start.add_argument("--keep-awake", action="store_true", help="Prevent Windows sleep while online")
    p_start.set_defaults(func=cmd_start)

    p_share = sub.add_parser("share", help="Start an on-demand Remote Support (IT2Me) session")
    p_share.add_argument("--name", default="", help="Display name for this support session")
    p_share.add_argument("--server", default="ws://127.0.0.1:8080/ws/host", help="Signaling WebSocket URL")
    p_share.add_argument("--token", default="", help="Optional workspace access token")
    p_share.add_argument("--auto-accept", action="store_true", help="Skip local interactive prompt")
    p_share.add_argument("--mock-desktop", action="store_true", help="Use synthetic video/mock input")
    p_share.set_defaults(func=cmd_share)

    p_nm = sub.add_parser("native-messaging", help="Run or install Chromium Native Messaging host")
    p_nm.add_argument("--install-manifest", action="store_true", help="Write Chrome Native Messaging manifest")
    p_nm.add_argument("--extension-id", default="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", help="Allowed Chrome extension ID")
    p_nm.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config JSON path")
    p_nm.set_defaults(func=cmd_native_messaging)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
