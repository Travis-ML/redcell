"""OpenAI-compatible HTTP server exposing the agent as a model provider.

This is the integration boundary for clients like Open WebUI: they add this as
an "OpenAI API" connection and drive the agent — tools, reasoning channel and
all — through a standard ``/v1/chat/completions`` contract. The agent itself
stays provider- and UI-agnostic; this module only translates HTTP <-> agent.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .agent import Agent, ChatResult
from .sessions import SessionStore

AgentFactory = Callable[[], Agent]

# Size of each simulated content delta when streaming (chars). See module docs
# on streaming in the design spec: the loop runs to completion, then the final
# text is chunked out so the UI shows a normal streaming experience.
_STREAM_CHUNK = 24


def create_app(
    agent_factory: AgentFactory,
    *,
    model_id: str = "redcell",
    api_key: str | None = None,
    gateway: object | None = None,
    mcp_manager: object | None = None,
    session_store: SessionStore | None = None,
    session_header: str = "x-redcell-session",
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        agent_factory: returns a fresh :class:`Agent` per request. By default the
            server is stateless (the client owns conversation history); when a
            request carries a session id and ``session_store`` is set, history is
            kept server-side instead. When ``mcp_manager`` is supplied, the
            factory is expected to merge ``mcp_manager.tools()`` into the agent's
            registry.
        model_id: the id advertised by ``/v1/models`` and echoed in responses.
        api_key: if set, requests must carry ``Authorization: Bearer <api_key>``.
        gateway: optional process supervisor with async ``start()``/``stop()``;
            driven by the app lifespan.
        mcp_manager: optional async context manager exposing ``tools()``; entered
            for the app's lifetime so the MCP session stays connected.
        session_store: if set, enables stateful sessions — a request bearing a
            session id recalls/extends that session's history server-side.
        session_header: request header carrying the client-generated session id
            (a body field ``session_id``/``sessionId`` is also accepted).
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # AsyncExitStack guarantees teardown even if a later startup step fails:
        # the gateway stop is registered immediately after it starts, so a failed
        # MCP connection can't leak the gateway process. Unwinds LIFO (manager
        # exits, then gateway stops) — the reverse of startup.
        async with AsyncExitStack() as stack:
            if gateway is not None:
                await gateway.start()
                stack.push_async_callback(gateway.stop)
            if mcp_manager is not None:
                await stack.enter_async_context(mcp_manager)
            yield

    app = FastAPI(title="redcell OpenAI-compatible API", lifespan=lifespan)

    def _auth(authorization: str | None = Header(default=None)) -> None:
        if api_key is None:
            return
        if authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    @app.get("/v1/models")
    def list_models(_: None = Depends(_auth)) -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "redcell",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, _: None = Depends(_auth)):
        body = await request.json()
        messages = body.get("messages") or []
        stream = bool(body.get("stream", False))

        # Stateful mode: a session id (header or body field) keys server-side
        # history, so the client may send only the new turn. Falls back to the
        # stateless path (client owns history) when no session id is present.
        sid = None
        if session_store is not None:
            sid = (
                request.headers.get(session_header)
                or body.get("session_id")
                or body.get("sessionId")
            )

        agent = agent_factory()
        try:
            if sid:
                memory = session_store.get_or_create(sid)
                incoming = [m for m in messages if m.get("role") != "system"]
                result = await agent.run_session(memory, incoming)
            else:
                result = await agent.run_messages(messages)
        except Exception as exc:  # surface as an OpenAI-style error, not a crash
            return JSONResponse(
                status_code=500,
                content={"error": {"message": str(exc), "type": "agent_error"}},
            )

        headers = {session_header: sid} if sid else None
        if stream:
            return StreamingResponse(
                _stream_completion(result, model_id),
                media_type="text/event-stream",
                headers=headers,
            )
        return JSONResponse(content=_completion_object(result, model_id), headers=headers)

    return app


def _completion_object(result: ChatResult, model_id: str) -> dict:
    """A single non-streamed ``chat.completion`` object."""
    message: dict = {"role": "assistant", "content": result.text}
    if result.reasoning:
        message["reasoning_content"] = result.reasoning
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": result.usage or {},
    }


async def _stream_completion(result: ChatResult, model_id: str) -> AsyncIterator[str]:
    """Yield SSE ``chat.completion.chunk`` events for the computed result."""
    cid = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_id}

    def chunk(delta: dict, finish: str | None = None) -> str:
        payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload)}\n\n"

    # Opening role delta, then the reasoning channel (if any), then content.
    yield chunk({"role": "assistant"})
    if result.reasoning:
        yield chunk({"reasoning_content": result.reasoning})
    for piece in _split(result.text or ""):
        yield chunk({"content": piece})
    yield chunk({}, finish="stop")
    yield "data: [DONE]\n\n"


def _split(text: str) -> Iterator[str]:
    """Split text into fixed-size pieces; joining them reproduces the original."""
    if not text:
        return
    for i in range(0, len(text), _STREAM_CHUNK):
        yield text[i : i + _STREAM_CHUNK]
