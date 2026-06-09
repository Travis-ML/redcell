"""Typed application settings loaded from environment and .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for an agent.

    Values are read from environment variables (prefixed ``AGENT_``) or a
    local ``.env`` file. Provider API keys (e.g. ``ANTHROPIC_API_KEY``) are
    read by LiteLLM directly from the environment.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    model: str = "anthropic/claude-opus-4-8"
    # OpenAI-compatible endpoint override (e.g. a self-hosted vLLM server).
    # Leave unset for hosted providers, which LiteLLM routes automatically.
    api_base: str | None = None
    # Bearer token for ``api_base``. vLLM accepts any non-empty value unless
    # started with ``--api-key``; LiteLLM still requires the key to be present.
    api_key: str | None = None
    temperature: float = 0.7
    max_tokens: int = 1024
    max_iterations: int = 25
    log_level: str = "INFO"
    # Render structured logs as JSON (and optionally to a file) so a scan run
    # produces a machine-readable JSONL event sink for later analysis.
    log_json: bool = False
    log_file: str | None = None

    # `redcell serve` — the OpenAI-compatible HTTP server.
    server_host: str = "0.0.0.0"
    server_port: int = 8800
    # If set, clients must send ``Authorization: Bearer <server_api_key>``.
    server_api_key: str | None = None
    # The model id advertised to clients (e.g. shown in Open WebUI's picker).
    model_id: str = "redcell"

    # --- Security controls (secure-by-default; toggle off to restore the
    # deliberately vulnerable target for red-team baselining) ---
    # Prepend the safety policy (redcell/prompts.py) to the system prompt.
    safety_prompt: bool = True
    # Enable the input/output guardrail (redcell/guardrails.py).
    guardrails: bool = True
    # Comma-separated subset of safety rules to include (empty = all). Names:
    # harm, copyright, truthfulness, commitments, disclosure, fairness. Lets you
    # measure each rule's individual contribution to a scan delta.
    safety_rules: str = ""
    # Comma-separated MCP tool names to drop before the agent can call them
    # (e.g. "shell,filesystem" to remove the worst exfiltration surface). Empty
    # = all gateway tools enabled.
    mcp_tool_denylist: str = ""

    # Stateful sessions: when a request carries this header (or a body
    # `session_id`/`sessionId`), the server remembers that conversation's history
    # server-side, so the client sends only the new turn. Used by promptfoo's
    # multi-turn red-team strategies in "client-generated session id" mode.
    session_header: str = "x-redcell-session"
    # Idle lifetime of a session and the cap on concurrent sessions (LRU-evicted).
    session_ttl_seconds: float = 3600.0
    session_max: int = 1000

    # Base URL of the SearXNG instance backing the `web_search` tool.
    searxng_url: str = "http://127.0.0.1:8989"

    # AgentGateway — `serve` launches this process and proxies MCP traffic through it.
    gateway_bin: str = "agentgateway"
    gateway_config_path: str = "agentgateway/config.yaml"
    # Host/port the gateway's MCP proxy binds (used for the readiness probe).
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 3030
    # The aggregated MCP endpoint the agent connects to. Path may need tuning to
    # match the gateway config (root vs /mcp); override via AGENT_GATEWAY_URL.
    gateway_url: str = "http://127.0.0.1:3030/mcp"
    # If false, `serve` does not spawn the gateway (e.g. you run it elsewhere).
    gateway_autostart: bool = True
    gateway_ready_timeout: float = 30.0
