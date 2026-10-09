"""HTTP + WebSocket server (Starlette). No UI: client apps connect to it.

Routes:

* ``WS /ws?p=a`` — the live stream (see ``PROTOCOL.md``);
* ``GET /`` and ``GET /api/state`` — the same snapshot a new WebSocket client receives;
* ``GET /api/transcript.json`` / ``.md`` — the final transcript, for after the demo;
* ``GET /healthz``.

CORS is open so browser-based clients served from elsewhere can call the REST
routes (WebSockets are not subject to CORS).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from typing import TYPE_CHECKING, Any, AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .languages import language_name

if TYPE_CHECKING:
    from .runtime import Runtime


def transcript_markdown(runtime: "Runtime") -> str:
    people = {p.id: p for p in runtime.session.participants()}
    lines = ["# Conversation transcript", ""]
    lines.append("Participants: " + ", ".join(f"{p.name} ({language_name(p.lang)})" for p in people.values()))
    lines.append("")
    for u in runtime.hub.transcript():
        p = people.get(u.get("speaker") or "")
        who = p.name if p else "Unknown speaker"
        when = dt.datetime.fromtimestamp(u["start"]).strftime("%H:%M:%S")
        lines.append(f"**{who}**, {when}, {language_name(u.get('lang'))}")
        lines.append("")
        lines.append(f"> {u['text']}")
        for lang, text in (u.get("translations") or {}).items():
            lines.append(f">\n> _{language_name(lang)}:_ {text}")
        lines.append("")
    return "\n".join(lines)


def create_app(runtime: "Runtime") -> Starlette:
    async def state(request: Request) -> Response:
        return JSONResponse(runtime.hub.snapshot())

    async def transcript_json(request: Request) -> Response:
        return JSONResponse({
            "participants": [p.to_dict() for p in runtime.session.participants()],
            "utterances": runtime.hub.transcript(),
        })

    async def transcript_md(request: Request) -> Response:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        return PlainTextResponse(
            transcript_markdown(runtime), media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="conversation-{stamp}.md"'},
        )

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "clients": len(runtime.hub.clients)})

    async def ws_endpoint(websocket: WebSocket) -> None:
        await runtime.hub.serve(websocket, websocket.query_params.get("p"))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        runtime.hub.attach(asyncio.get_running_loop())
        await asyncio.to_thread(runtime.start)
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.stop)

    routes: list[Any] = [
        Route("/", state),
        Route("/api/state", state),
        Route("/api/transcript.json", transcript_json),
        Route("/api/transcript.md", transcript_md),
        Route("/healthz", health),
        WebSocketRoute("/ws", ws_endpoint),
    ]
    middleware = [Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
