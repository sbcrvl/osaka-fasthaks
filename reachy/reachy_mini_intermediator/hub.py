"""Conversation state and WebSocket fan-out.

The pipeline threads call the thread-safe ``emit_*`` methods; every state
change is applied on the server's event loop and broadcast to all connected
clients. A client that connects (or reconnects) late receives a ``hello``
message with the full history, so both participants can always scroll back
through the whole conversation.

Wire protocol (JSON text frames, ``"v": 1``) is documented in ``PROTOCOL.md``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Any, Callable

from .languages import LANGUAGES
from .session import Session

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_QUEUE_SIZE = 1000


class Client:
    def __init__(self, ws: Any, participant: str | None = None) -> None:
        self.ws = ws
        self.participant = participant
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)


class Hub:
    def __init__(self, session: Session, info: dict[str, Any] | None = None, max_history: int = 1000) -> None:
        self.session = session
        self.info = dict(info or {})
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[Client] = set()
        self.utterances: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self.max_history = max_history
        self.status: dict[str, Any] = {"listening": False, "speech": False, "level": 0.0,
                                       "doa": None, "speaker": None, "speaking": None, "error": None}
        self._status_dirty = False
        #: Called with client messages the pipeline handles (text, reassign, ...).
        self.on_command: Callable[[dict[str, Any]], None] | None = None

    # ------------------------------------------------------------------ setup
    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        """Run ``fn`` on the event loop, from any thread."""
        if self.loop is None or self.loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            fn(*args)
        else:
            self.loop.call_soon_threadsafe(fn, *args)

    # ------------------------------------------------- thread-safe entry points
    def emit_utterance(self, utterance: dict[str, Any]) -> None:
        """Insert or update an utterance (partial or final)."""
        self._call(self._apply_utterance, dict(utterance))

    def emit_translation(self, uid: str, lang: str, text: str | None, error: str | None = None) -> None:
        self._call(self._apply_translation, uid, lang, text, error)

    def emit_drop(self, uid: str) -> None:
        self._call(self._apply_drop, uid)

    def emit_status(self, **fields: Any) -> None:
        self._call(self._apply_status, fields)

    def emit_participants(self) -> None:
        self._call(self._broadcast, {"type": "participants", "participants": self._participants()})

    def emit_cleared(self) -> None:
        self._call(self._apply_clear)

    # --------------------------------------------------- loop-thread handlers
    def _apply_utterance(self, u: dict[str, Any]) -> None:
        uid = u["id"]
        prev = self.utterances.get(uid)
        if "translations" not in u:
            # Updates without translations keep the ones that already arrived;
            # an explicit (even empty) dict replaces them, e.g. after a reassign.
            u["translations"] = dict(prev.get("translations", {})) if prev else {}
        self.utterances[uid] = u
        while len(self.utterances) > self.max_history:
            self.utterances.popitem(last=False)
        self._broadcast({"type": "final" if u.get("final") else "partial", "utterance": u})

    def _apply_translation(self, uid: str, lang: str, text: str | None, error: str | None) -> None:
        u = self.utterances.get(uid)
        if u is not None and text is not None:
            u.setdefault("translations", {})[lang] = text
        msg: dict[str, Any] = {"type": "translation", "id": uid, "lang": lang, "text": text}
        if error:
            msg["error"] = error
        self._broadcast(msg)

    def _apply_drop(self, uid: str) -> None:
        self.utterances.pop(uid, None)
        self._broadcast({"type": "drop", "id": uid})

    def _apply_clear(self) -> None:
        self.utterances.clear()
        self._broadcast({"type": "cleared"})

    def _apply_status(self, fields: dict[str, Any]) -> None:
        self.status.update(fields)
        if not self._status_dirty:
            # Coalesce bursts of status updates into one message per loop tick.
            self._status_dirty = True
            assert self.loop is not None
            self.loop.call_soon(self._flush_status)

    def _flush_status(self) -> None:
        self._status_dirty = False
        self._broadcast({"type": "status", "status": self._status_view()})

    def _status_view(self) -> dict[str, Any]:
        return dict(self.status, ptt=self.session.ptt_active(), paused=self.session.paused,
                    speak=self.session.speak_enabled)

    # --------------------------------------------------------------- snapshot
    def _participants(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.session.participants()]

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "v": PROTOCOL_VERSION,
            "server_time": time.time(),
            "info": self.info,
            "languages": {code: v for code, v in LANGUAGES.items()},
            "participants": self._participants(),
            "status": self._status_view(),
            "history": list(self.utterances.values()),
        }

    def transcript(self) -> list[dict[str, Any]]:
        return [u for u in self.utterances.values() if u.get("final")]

    # -------------------------------------------------------------- broadcast
    def _broadcast(self, msg: dict[str, Any]) -> None:
        msg.setdefault("v", PROTOCOL_VERSION)
        data = json.dumps(msg, ensure_ascii=False)
        for client in list(self.clients):
            try:
                client.queue.put_nowait(data)
            except asyncio.QueueFull:
                # A stalled client: drop it; it will reconnect and resync from hello.
                logger.warning("client too slow, disconnecting")
                self.clients.discard(client)
                try:
                    client.queue.get_nowait()
                    client.queue.put_nowait(None)
                except Exception:
                    pass

    # ------------------------------------------------------------ connections
    async def serve(self, ws: Any, participant: str | None = None) -> None:
        """Run one WebSocket connection (Starlette ``WebSocket``) until it closes."""
        await ws.accept()
        client = Client(ws, participant)
        self.clients.add(client)
        await ws.send_text(json.dumps(self.snapshot(), ensure_ascii=False))
        sender = asyncio.create_task(self._sender(client))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self._on_client_message(client, msg)
        except Exception as e:  # WebSocketDisconnect and friends
            if type(e).__name__ not in ("WebSocketDisconnect", "ConnectionClosed", "ClientDisconnected"):
                logger.debug("websocket closed: %r", e)
        finally:
            self.clients.discard(client)
            if client.participant:
                # A phone that disconnects mid-press must not hold the floor.
                self.session.set_ptt(client.participant, False)
            sender.cancel()

    async def _sender(self, client: Client) -> None:
        try:
            while True:
                data = await client.queue.get()
                if data is None:
                    await client.ws.close()
                    return
                await client.ws.send_text(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("send failed", exc_info=True)

    async def _on_client_message(self, client: Client, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "hello":
            client.participant = msg.get("participant") or client.participant
            await client.ws.send_text(json.dumps(self.snapshot(), ensure_ascii=False))
        elif kind == "ping":
            await client.ws.send_text(json.dumps({"type": "pong", "v": PROTOCOL_VERSION, "t": msg.get("t")}))
        elif kind == "update_participant":
            p = self.session.update_participant(str(msg.get("id", "")), name=msg.get("name"),
                                                lang=msg.get("lang"), side=msg.get("side"))
            if p is not None:
                self._broadcast({"type": "participants", "participants": self._participants()})
        elif kind == "ptt":
            pid = msg.get("participant") or client.participant
            if pid:
                self.session.set_ptt(str(pid), bool(msg.get("active")))
                self._apply_status({})
        elif kind == "control":
            action = msg.get("action")
            if action == "pause":
                self.session.set_paused(True)
                self._apply_status({"listening": False})
            elif action == "resume":
                self.session.set_paused(False)
                self._apply_status({"listening": True})
            elif action == "clear":
                self._apply_clear()
            elif action in ("speak_on", "speak_off"):
                self.session.speak_enabled = action == "speak_on"
                self._apply_status({})
        elif kind in ("text", "reassign", "retranslate"):
            if self.on_command is not None:
                await asyncio.get_running_loop().run_in_executor(None, self.on_command, msg)
