# CLI reference

The `redcell` command is a [Typer](https://typer.tiangolo.com/) app
(`redcell/cli.py`, entry point `redcell = redcell.cli:app`). Run any command with
`uv run redcell <command>` (or `redcell <command>` in an activated venv).

```
redcell version          # print the installed version
redcell chat             # interactive REPL against a configured model
redcell serve            # OpenAI-compatible HTTP server (+ AgentGateway)
redcell rag-seed         # load the RAG corpus into Qdrant via the gateway
```

All commands read configuration from env / `.env` (see
[configuration.md](configuration.md)).

---

## `redcell version`

Prints the package version (`redcell.__version__`). No options.

---

## `redcell chat`

Starts an interactive REPL. Each line you type is one `Agent.run()` turn against the
agent's **own in-memory history**, so the conversation accumulates within the session.
Tool calls (builtin tools only — `chat` does not launch the gateway) run as needed.

```bash
uv run redcell chat
uv run redcell chat --system "You are a terse pentest assistant."
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--system <text>` | `You are a helpful assistant.` | Base system prompt. The safety policy is still prepended on top of it when `AGENT_SAFETY_PROMPT=true`. |

The active guardrail (`AGENT_GUARDRAILS`) applies here too. Exit with `Ctrl-C`.

---

## `redcell serve`

Runs the OpenAI-compatible HTTP API (see [server-api.md](server-api.md)) and, unless
disabled, launches and supervises an AgentGateway process so MCP tools are available.

```bash
uv run redcell serve
uv run redcell serve --host 127.0.0.1 --port 9000
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--host <host>` | `AGENT_SERVER_HOST` (`0.0.0.0`) | Override bind host. |
| `--port <port>` | `AGENT_SERVER_PORT` (`8800`) | Override bind port. |

On start it prints the bind URLs and the **active security posture**, e.g.:

```
Serving agent (anthropic/claude-opus-4-8) as model 'redcell'.
  security: safety_prompt=on, guardrails=on
  local:  http://127.0.0.1:8800/v1
  docker: http://host.docker.internal:8800/v1  (use this in Open WebUI)
  gateway: launching 'agentgateway' on :3030
```

If `AGENT_MCP_TOOL_DENYLIST` is set, the denied tool names are printed too.

**Gateway behavior:** controlled by `AGENT_GATEWAY_*` (see
[configuration.md](configuration.md)). If the binary is missing or never becomes
ready, `serve` logs a warning and runs with builtin tools only — it does not fail.
Set `AGENT_GATEWAY_AUTOSTART=false` to run the gateway yourself.

---

## `redcell rag-seed`

Loads a corpus of documents into the RAG vector store **through the gateway's
`qdrant-store` tool** (so embeddings/collection match what `qdrant-find` retrieves).
Requires a running gateway with the `rag` target and a running Qdrant. See
[rag.md](rag.md).

```bash
docker compose up -d qdrant     # start Qdrant
uv run redcell serve            # in another shell — brings up the gateway + rag target
uv run redcell rag-seed         # load the bundled corpus
uv run redcell rag-seed --corpus path/to/your_corpus.json
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--corpus <path>` | bundled `seed_corpus.json` | Corpus JSON file to load. |

Prints the number of documents stored. Fails loudly if no `qdrant-store` tool is
reachable or if any store call errors (e.g. Qdrant down).
