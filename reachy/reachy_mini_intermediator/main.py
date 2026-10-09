"""Reachy Mini app entry point (listed in the robot's dashboard).

The app runs the same server as the standalone launcher, using the robot
connection the app manager provides. Configure it with ``INTERMEDIATOR_*``
environment variables (and ``SHISA_API_KEY`` / ``ANTHROPIC_API_KEY``), since
the dashboard starts apps without arguments.
"""

from __future__ import annotations

import logging
import os
import threading

from reachy_mini import ReachyMini, ReachyMiniApp

from .config import ENV_PREFIX, Settings
from .runtime import Runtime


class ReachyMiniIntermediator(ReachyMiniApp):
    # No settings page: the client apps connect to our WebSocket on port 8042.
    custom_app_url: str | None = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        settings = Settings.from_env()
        settings.source = os.environ.get(ENV_PREFIX + "SOURCE", "reachy")
        if ENV_PREFIX + "GESTURES" not in os.environ:
            settings.gestures = True  # a robot app should use its body
        logging.getLogger("reachy_mini_intermediator").setLevel(settings.log_level.upper())
        Runtime(settings, mini=reachy_mini).serve(stop_event)


if __name__ == "__main__":
    app = ReachyMiniIntermediator()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
