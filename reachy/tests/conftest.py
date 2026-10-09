from __future__ import annotations

import asyncio
import json
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

SR = 16_000


def speech_like(duration: float, amp: float = 0.15, seed: int = 0) -> np.ndarray:
    """Syllable-rate modulated noise: crosses any energy-based detector like speech."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration * SR)) / SR
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t)
    carrier = rng.normal(0, 1, t.size) * 0.5 + np.sin(2 * np.pi * 180 * t)
    return (amp * envelope * carrier / 1.2).astype(np.float32)


def silence(duration: float, amp: float = 0.002, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amp, int(duration * SR))).astype(np.float32)


def conversation(turns: list[float], gap: float = 1.2) -> np.ndarray:
    parts = [silence(0.8)]
    for i, d in enumerate(turns):
        parts.append(speech_like(d, seed=10 + i))
        parts.append(silence(gap, seed=20 + i))
    return np.concatenate(parts)


def write_wav(path: Path, audio: np.ndarray, rate: int = SR) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return path


class LoopThread:
    """An asyncio loop on a background thread, standing in for the server's loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Any, timeout: float = 10.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        async def shutdown() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            self.run(shutdown(), timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


@pytest.fixture
def loop_thread():
    lt = LoopThread()
    yield lt
    lt.close()


class FakeWebSocket:
    """Enough of Starlette's WebSocket for Hub.serve()."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self._cond = threading.Condition()

    async def accept(self) -> None:
        pass

    async def send_text(self, data: str) -> None:
        with self._cond:
            self.sent.append(json.loads(data))
            self._cond.notify_all()

    async def receive_text(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise ConnectionError("WebSocketDisconnect")
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, msg: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, json.dumps(msg))

    def disconnect(self) -> None:
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, None)

    def wait_for(self, pred: Any, timeout: float = 10.0) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self.sent:
                    if pred(m):
                        return m
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    types = [m.get("type") for m in self.sent]
                    raise AssertionError(f"message not received; got {types}")
                self._cond.wait(remaining)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        with self._cond:
            return [m for m in self.sent if m.get("type") == kind]
