"""The real Starlette app, driven through ASGI (lifespan, HTTP and WebSocket),
with a robot-less runtime: WAV source, scripted recognition, echo translation."""

import asyncio
import json

from reachy_mini_intermediator.config import Settings
from reachy_mini_intermediator.runtime import Runtime
from reachy_mini_intermediator.server import create_app

from .conftest import conversation, write_wav


class ASGI:
    def __init__(self, app):
        self.app = app

    async def lifespan(self):
        rx, tx = asyncio.Queue(), asyncio.Queue()
        await rx.put({"type": "lifespan.startup"})
        task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, rx.get, tx.put))
        assert (await tx.get())["type"] == "lifespan.startup.complete"

        async def shutdown():
            await rx.put({"type": "lifespan.shutdown"})
            assert (await tx.get())["type"] == "lifespan.shutdown.complete"
            await task

        return shutdown

    async def get(self, path):
        sent = []
        msgs = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            return msgs.pop(0) if msgs else {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "headers": [(b"host", b"robot"), (b"origin", b"http://elsewhere")],
                 "client": ("1.2.3.4", 5), "server": ("robot", 8042), "root_path": ""}
        await self.app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], dict(start["headers"]), body

    async def websocket(self, path, query=b""):
        rx, tx = asyncio.Queue(), asyncio.Queue()
        await rx.put({"type": "websocket.connect"})
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": path,
                 "raw_path": path.encode(), "query_string": query, "headers": [(b"host", b"robot")],
                 "client": ("1.2.3.4", 5), "server": ("robot", 8042), "root_path": "", "subprotocols": []}
        task = asyncio.create_task(self.app(scope, rx.get, tx.put))
        assert (await tx.get())["type"] == "websocket.accept"

        class Conn:
            async def recv(self, timeout=10):
                m = await asyncio.wait_for(tx.get(), timeout)
                return json.loads(m["text"])

            async def send(self, obj):
                await rx.put({"type": "websocket.receive", "text": json.dumps(obj)})

            async def close(self):
                await rx.put({"type": "websocket.disconnect", "code": 1000})
                await asyncio.wait_for(task, 5)

        return Conn()


def test_server_end_to_end(tmp_path):
    wav = write_wav(tmp_path / "talk.wav", conversation([2.0, 1.5]))
    script = tmp_path / "script.txt"
    script.write_text("ja|はじめまして\nen|Nice to meet you\n", encoding="utf-8")
    settings = Settings.from_args(["--source", f"file:{wav}", "--file-speed", "3", "--asr", f"script:{script}",
                                   "--translator", "echo", "--a", "Aiko:ja:left", "--b", "Ben:en:right",
                                   "--end-silence-ms", "500"], {})
    runtime = Runtime(settings)
    runtime.build()
    asgi = ASGI(create_app(runtime))

    async def scenario():
        shutdown = await asgi.lifespan()
        a = await asgi.websocket("/ws", b"p=a")
        hello = await a.recv()
        assert hello["type"] == "hello" and hello["v"] == 1
        assert [p["name"] for p in hello["participants"]] == ["Aiko", "Ben"]
        assert hello["info"]["asr"] == "script" and hello["info"]["translator"] == "echo"

        seen = []
        while not any(m["type"] == "translation" and m["lang"] == "ja" for m in seen):
            seen.append(await a.recv())
        finals = [m["utterance"] for m in seen if m["type"] == "final"]
        assert [(u["speaker"], u["text"]) for u in finals] == [("a", "はじめまして"), ("b", "Nice to meet you")]

        status, headers, body = await asgi.get("/api/state")
        assert status == 200 and headers[b"access-control-allow-origin"] == b"*"
        state = json.loads(body)
        assert state["history"][0]["translations"] == {"en": "[en] はじめまして"}

        status, headers, body = await asgi.get("/api/transcript.md")
        assert status == 200 and "Nice to meet you" in body.decode()

        await a.close()
        await shutdown()

    asyncio.run(scenario())
