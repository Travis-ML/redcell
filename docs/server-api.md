# Server & API

`redcell serve` exposes an **OpenAI-compatible** HTTP API (`redcell/server.py`,
built with FastAPI). Any OpenAI client/SDK or scanner can drive the agent through
the standard chat-completions contract — tools, reasoning channel, and all.

Base URL: `http://<host>:<port>/v1` (default `http://127.0.0.1:8800/v1`).

## Endpoints

### `GET /v1/models`

Lists the single advertised model (id = `AGENT_MODEL_ID`, default `redcell`).

```json
{ "object": "list",
  "data": [ { "id": "redcell", "object": "model", "created": 0, "owned_by": "redcell" } ] }
```

### `POST /v1/chat/completions`

Runs one agent turn. Accepts the OpenAI request shape:

```json
{
  "model": "redcell",
  "messages": [{ "role": "user", "content": "hello" }],
  "stream": false
}
```

Non-streaming response (a `chat.completion`):

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "redcell",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "…", "reasoning_content": "…" },
    "finish_reason": "stop"
  }],
  "usage": { … }
}
```

- `reasoning_content` is present only when the model exposed a reasoning channel.
- Errors during the run are returned as HTTP 500 with an OpenAI-style error body
  (`{"error": {"message": …, "type": "agent_error"}}`) rather than crashing.

### Streaming

Set `"stream": true` to receive `text/event-stream` `chat.completion.chunk` events
ending with `data: [DONE]`. Note redcell runs the **full tool-use loop to completion
first**, then chunks the final text out — so streaming is a UI nicety, not
token-by-token generation. Order of deltas: role → reasoning (if any) → content → stop.

## Authentication

If `AGENT_SERVER_API_KEY` is set, every request must carry
`Authorization: Bearer <key>` (401 otherwise). If unset, the API is open. Example:

```bash
curl http://127.0.0.1:8800/v1/chat/completions \
  -H "Authorization: Bearer $AGENT_SERVER_API_KEY" \
  -H "content-type: application/json" \
  -d '{"model":"redcell","messages":[{"role":"user","content":"hi"}]}'
```

## Client setup notes

- **Open WebUI (Docker):** Base URL `http://host.docker.internal:8800/v1`, any API key.
- **Garak / Promptfoo:** target `http://127.0.0.1:8800/v1/chat/completions`, model `redcell`.
- **OpenAI SDK:** point `base_url` at `.../v1`, `model="redcell"`.

---

## Sessions

By default the server is **stateless**: it builds a fresh `Agent` per request and runs
only the `messages` in that request. The client owns conversation history and resends
it each turn (promptfoo: *"No — resend the full interaction history"*). Multi-turn just
works with no extra setup.

To run redcell as a **stateful** target — the client sends only the *new* turn plus a
session id and redcell remembers the rest — attach a **session id** to each request.

### How a session id is resolved

`server.py` looks for an id in this order:

1. The header named by `AGENT_SESSION_HEADER` (default `x-redcell-session`).
2. A body field `session_id`.
3. A body field `sessionId`.

If one is found (and the server has a `SessionStore`), the request uses the stateful
path: redcell loads that session's history, appends the incoming turn(s), runs the
loop, and persists the **full expansion** (assistant text, tool-call turns, tool
results). The id is echoed back in the response header. No id → unchanged stateless
behavior.

### Store semantics

`SessionStore` (`redcell/sessions.py`) is an in-memory map of id → history with:

- **idle TTL** eviction (`AGENT_SESSION_TTL_SECONDS`, default 1h), and
- **LRU** eviction past `AGENT_SESSION_MAX` (default 1000) concurrent sessions.

It is lost on restart — adequate for a local red-team target. It assumes a single
conversation is driven sequentially (true for promptfoo multi-turn strategies);
overlapping concurrent requests on the *same* id would race on that session's memory.

### Promptfoo wiring (client-generated session id)

Wizard answers: *Remembers history → **Yes***, *Session management →
**Client-generated Session ID***, *Session ID Extraction → **(leave empty)***. The
generated target wires a fresh UUID per test case into each request:

```yaml
targets:
  - id: openai:chat:redcell
    config:
      apiBaseUrl: http://127.0.0.1:8800/v1
      apiKey: redcell            # any value unless AGENT_SERVER_API_KEY is set
      headers:
        x-redcell-session: '{{sessionId}}'
defaultTest:
  options:
    transformVars: '{ ...vars, sessionId: context.uuid }'
```

### Quick manual check

```bash
SID=demo-1
curl -s localhost:8800/v1/chat/completions -H "x-redcell-session: $SID" \
  -d '{"model":"redcell","messages":[{"role":"user","content":"My name is Travis."}]}'
curl -s localhost:8800/v1/chat/completions -H "x-redcell-session: $SID" \
  -d '{"model":"redcell","messages":[{"role":"user","content":"What is my name?"}]}'   # recalls it
curl -s localhost:8800/v1/chat/completions \
  -d '{"model":"redcell","messages":[{"role":"user","content":"What is my name?"}]}'   # no id → stateless, does not recall
```

> Caveat: very long sessions accumulate unbounded history and can exceed the model's
> context window. History trimming/summarization is not yet implemented.
