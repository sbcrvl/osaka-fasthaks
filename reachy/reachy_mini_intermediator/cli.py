"""Standalone launcher: ``reachy-mini-intermediator`` / ``python -m reachy_mini_intermediator``.

Runs as its own process, separate from the Reachy Mini daemon and its app
manager. It can run on the robot or on a laptop on the same network; the robot
is reached through the SDK (microphone) and the daemon's REST API (sound
direction).
"""

from __future__ import annotations

import logging
import sys

from .config import Settings
from .runtime import Runtime, lan_addresses


def main(argv: list[str] | None = None) -> int:
    try:
        settings = Settings.from_args(argv if argv is not None else sys.argv[1:])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # At DEBUG, websockets logs request headers, including the Shisa Bearer key.
    logging.getLogger("websockets").setLevel(logging.INFO)
    runtime = Runtime(settings)
    hosts = lan_addresses() or ["localhost"]
    a, b = settings.participants
    print(
        "\n  Reachy Mini Intermediator\n"
        f"    Participants: {a.name} ({a.lang}, {a.side}) / {b.name} ({b.lang}, {b.side})\n"
        f"    WebSocket:    ws://{hosts[0]}:{settings.port}/ws?p=a  (or ?p=b, or no p for a shared screen)\n"
        f"    Snapshot:     http://{hosts[0]}:{settings.port}/api/state\n",
        flush=True,
    )
    try:
        runtime.serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
