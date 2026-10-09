"""Publishing finished utterances to the Hashi server (the mobile app's backend).

Hashi keeps the shared conversation and translates on demand; it accepts a
*publisher* connection that sends ``{"type": "message", "message": {...}}``
events (see ``mobile-app/WEBSOCKET.md``). The robot is that publisher: every
final transcript with a known speaker becomes one immutable message.

* participant ``a`` → ``person-1``, ``b`` → ``person-2`` (configurable);
* ids are ``reachy-<start time>-<n>`` so they stay unique across robot restarts,
  which Hashi requires (it rejects duplicates);
* messages queued while Hashi is unreachable are sent, in order, on reconnect.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_LANG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")


def hashi_message(u: dict[str, Any], user_id: str, prefix: str, fallback_lang: str | None) -> dict[str, Any] | None:
    """Our final utterance → a Hashi ``Message``, or ``None`` if it can't be valid."""
    text = str(u.get("text") or "").strip()[:10_000]
    lang = str(u.get("lang") or fallback_lang or "").lower()
    if not text or not _LANG.match(lang):
        return None
    sent = dt.datetime.fromtimestamp(float(u.get("start") or time.time()), tz=dt.timezone.utc)
    return {
        "id": f"{prefix}-{u['id']}"[:128],
        "userId": user_id,
        "text": text,
        "language": lang,
        "sentAt": sent.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


class HashiPublisher(threading.Thread):
    """Keeps a WebSocket to Hashi open and sends messages through it."""

    def __init__(self, url: str, user_ids: dict[str, str], languages: Callable[[str], str | None],
                 connect: Callable[[str], Any] | None = None, on_error: Callable[[str | None], None] | None = None) -> None:
        super().__init__(name="hashi-publisher", daemon=True)
        self.url = url
        self.user_ids = user_ids
        self.languages = languages
        self.prefix = f"reachy-{int(time.time())}"
        self._connect = connect
        self._on_error = on_error or (lambda msg: None)
        self._pending: collections.deque[dict[str, Any]] = collections.deque(maxlen=500)
        self._cv = threading.Condition()
        self._halt = threading.Event()
        self._ws: Any = None
        self.sent: list[str] = []

    # Pipeline side ------------------------------------------------------------------
    def publish(self, u: dict[str, Any]) -> bool:
        pid = u.get("speaker")
        user_id = self.user_ids.get(pid or "")
        if user_id is None:
            logger.info("Not publishing %s: speaker unknown", u.get("id"))
            return False
        msg = hashi_message(u, user_id, self.prefix, self.languages(pid))
        if msg is None:
            logger.info("Not publishing %s: empty text or no language", u.get("id"))
            return False
        with self._cv:
            self._pending.append(msg)
            self._cv.notify_all()
        return True

    def stop(self) -> None:
        self._halt.set()
        with self._cv:
            self._cv.notify_all()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # Connection thread ----------------------------------------------------------------
    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self.url)
        from websockets.sync.client import connect

        return connect(self.url, open_timeout=10, max_size=2**22)

    def _reader(self, ws: Any) -> None:
        """Hashi echoes snapshots and broadcasts to every client; only errors matter here."""
        try:
            for raw in ws:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("type") == "error" and "requestId" not in event:
                    logger.warning("Hashi rejected a message: %s", event.get("error"))
                    self._on_error(f"App server: {event.get('error')}")
        except Exception:
            pass

    def run(self) -> None:
        backoff = 1.0
        while not self._halt.is_set():
            try:
                ws = self._open()
            except Exception as e:
                logger.warning("Cannot reach the app server at %s: %s", self.url, e)
                self._on_error(f"App server unreachable at {self.url}")
                self._halt.wait(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            logger.info("Publishing to the app server at %s", self.url)
            self._on_error(None)
            self._ws = ws
            backoff = 1.0
            reader = threading.Thread(target=self._reader, args=(ws,), daemon=True)
            reader.start()
            try:
                while not self._halt.is_set():
                    with self._cv:
                        while not self._pending and not self._halt.is_set() and reader.is_alive():
                            self._cv.wait(0.5)
                        if self._halt.is_set():
                            break
                        if not reader.is_alive():
                            raise ConnectionError("connection closed")
                        msg = self._pending[0]
                    ws.send(json.dumps({"type": "message", "message": msg}, ensure_ascii=False))
                    with self._cv:
                        self._pending.popleft()  # only after a successful send
                    self.sent.append(msg["id"])
            except Exception as e:
                if not self._halt.is_set():
                    logger.warning("App server connection lost: %s", e)
            finally:
                self._ws = None
                try:
                    ws.close()
                except Exception:
                    pass
            self._halt.wait(backoff)
