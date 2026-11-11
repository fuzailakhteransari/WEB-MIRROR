"""Allow running via `python -m host ...`."""

from host.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
