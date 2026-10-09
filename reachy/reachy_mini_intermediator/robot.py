"""Robot-side helpers: sound direction polling and body language.

Direction of arrival is read from the daemon's REST endpoint
(``GET /api/state/doa``) rather than through the SDK, because the SDK only
reads it when it has local USB access to the microphone array. The REST route
works the same on the robot and from a laptop.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import numpy as np

from .speakers import DoATracker

logger = logging.getLogger(__name__)


def connection_mode_for(host: str) -> str:
    """``network`` for a named robot, so a local daemon (e.g. the Reachy Mini
    Control desktop app) never shadows it; ``auto`` for localhost."""
    return "auto" if host in ("localhost", "127.0.0.1", "") else "network"


def daemon_host_for(mini: Any, fallback: str, port: int = 8000) -> str:
    """Host to reach the daemon's REST API, mirroring how the SDK connected.

    A daemon on this machine (on the robot itself, or a Lite over USB) wins;
    otherwise the robot's Wi-Fi address as the daemon reports it; otherwise the
    configured host name.
    """
    import socket

    if getattr(mini, "connection_mode", None) != "network":
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return "localhost"
        except OSError:
            pass
    try:
        status = mini.client.get_status(wait=False)
        if getattr(status, "wlan_ip", None):
            return str(status.wlan_ip)
    except Exception:
        logger.debug("could not read daemon status", exc_info=True)
    return fallback


class DoAPoller(threading.Thread):
    """Polls ``/api/state/doa`` at ~10 Hz into a :class:`DoATracker`."""

    def __init__(self, tracker: DoATracker, host: str, port: int = 8000, period: float = 0.1) -> None:
        super().__init__(name="doa-poller", daemon=True)
        self.tracker = tracker
        self.url = f"http://{host}:{port}/api/state/doa"
        self.period = period
        self._halt = threading.Event()
        self.available = False

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        import requests

        session = requests.Session()  # one connection: Wi-Fi handshakes are slow
        failures = 0
        logger.info("Polling sound direction from %s", self.url)
        while not self._halt.is_set():
            t = time.time()
            try:
                r = session.get(self.url, timeout=2.0)
                r.raise_for_status()
                body = r.json()
                if body is not None:
                    self.tracker.add(t, float(body["angle"]), bool(body["speech_detected"]))
                    if not self.available:
                        logger.info("Sound direction available")
                    self.available = True
                failures = 0
            except Exception as e:
                failures += 1
                if failures == 3:
                    logger.warning("Sound direction unavailable (%s); retrying in the background", e)
                self.available = False
                self._halt.wait(min(5.0, 0.5 * failures))
                continue
            self._halt.wait(max(0.0, self.period - (time.time() - t)))


class Gestures(threading.Thread):
    """Small, readable body language for an interpreter robot.

    * someone starts talking → look toward them, antennas up (listening);
    * their sentence is translated → turn to the other person with a small nod
      (passing the message on);
    * nothing happens for a while → back to neutral.

    Motions run on their own thread because ``goto_target`` blocks; only the
    latest intent is kept so the robot never lags behind the conversation.
    """

    IDLE_AFTER_S = 8.0

    def __init__(self, mini: Any, look_yaw_deg: float = 28.0) -> None:
        super().__init__(name="gestures", daemon=True)
        self.mini = mini
        self.look_yaw = look_yaw_deg
        self._q: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=1)
        self._halt = threading.Event()
        self._last_activity = time.monotonic()
        self._idle = True

    # Called from the pipeline threads ------------------------------------------
    def listen_to(self, side: str | None) -> None:
        self._post("listen", side)

    def relay_to(self, side: str | None) -> None:
        self._post("relay", side)

    def stop(self) -> None:
        self._halt.set()
        self._post("idle", None)

    def _post(self, kind: str, side: str | None) -> None:
        try:
            self._q.get_nowait()  # drop the stale intent
        except queue.Empty:
            pass
        try:
            self._q.put_nowait((kind, side))
        except queue.Full:
            pass

    # Motion thread ---------------------------------------------------------------
    def _yaw(self, side: str | None) -> float:
        # Positive yaw turns the head to the robot's left.
        return {"left": self.look_yaw, "right": -self.look_yaw}.get(side or "", 0.0)

    def _goto(self, yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0,
              antennas_deg: tuple[float, float] = (0.0, 0.0), duration: float = 0.6) -> None:
        from reachy_mini.utils import create_head_pose

        self.mini.goto_target(
            head=create_head_pose(yaw=yaw, pitch=pitch, roll=roll, degrees=True),
            antennas=np.deg2rad(antennas_deg),
            duration=duration,
            body_yaw=0.0,
        )

    def run(self) -> None:
        try:
            self._goto(duration=1.0)
        except Exception:
            logger.warning("Robot did not accept a motion command; gestures disabled", exc_info=True)
            return
        while not self._halt.is_set():
            try:
                kind, side = self._q.get(timeout=0.5)
            except queue.Empty:
                if not self._idle and time.monotonic() - self._last_activity > self.IDLE_AFTER_S:
                    self._safe(self._goto, duration=1.2)
                    self._idle = True
                continue
            self._last_activity = time.monotonic()
            self._idle = kind == "idle"
            if kind == "listen":
                # Slight tilt toward the speaker reads as attention.
                roll = 4.0 if side == "left" else -4.0 if side == "right" else 0.0
                self._safe(self._goto, yaw=self._yaw(side), roll=roll, antennas_deg=(18.0, -18.0), duration=0.5)
            elif kind == "relay":
                yaw = self._yaw(side)
                self._safe(self._goto, yaw=yaw, antennas_deg=(-10.0, 10.0), duration=0.55)
                self._safe(self._goto, yaw=yaw, pitch=9.0, antennas_deg=(-10.0, 10.0), duration=0.25)
                self._safe(self._goto, yaw=yaw, antennas_deg=(0.0, 0.0), duration=0.3)
            elif kind == "idle":
                self._safe(self._goto, duration=1.0)

    def _safe(self, fn: Any, **kwargs: Any) -> None:
        try:
            fn(**kwargs)
        except Exception:
            logger.debug("gesture failed", exc_info=True)

