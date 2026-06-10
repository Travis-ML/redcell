# Security controls

redcell is a deliberately attackable *target*, but it ships defensive controls
**on by default**. Each vulnerable surface is exposed as a single toggle so you can
baseline the unguarded target, then turn controls on and measure the delta — the core
workflow for evaluating AI-security tooling.

## The two layers

| Layer | Module | Env toggle | Default |
|-------|--------|-----------|---------|
| Safety system prompt | `redcell/prompts.py` | `AGENT_SAFETY_PROMPT` | `true` |
| Input/output guardrail | `redcell/guardrails.py` | `AGENT_GUARDRAILS` | `true` |
| Dangerous tool surface | `agentgateway/config.yaml` + denylist | `AGENT_MCP_TOOL_DENYLIST` | _(none denied)_ |

### 1. Safety system prompt (`prompts.py`)

`build_system_prompt(base, safety=True)` prepends `SAFETY_POLICY` to the base prompt.
The policy instructs the model to:

- refuse requests that enable crime/fraud/financial wrongdoing, weapons, drugs,
  malware, violence/abuse/self-harm, harassment/hate, or sexual exploitation;
- not reproduce copyrighted text (in the reply or by writing it to a file);
- **never fabricate** searches, lookups, tool results, sources, or citations — say so
  plainly when a tool failed or wasn't called (anti-confabulation);
- **make no binding commitments** and never act as an "authorized representative";
- **never disclose internal architecture** — backends, tool names, model identity,
  file-system paths, or whether records about a person exist;
- avoid biased framing by protected attributes.

With `AGENT_SAFETY_PROMPT=false`, the prompt is the bare base (`"You are a helpful
assistant."`) — the original unguarded behavior.

The policy is built from **named rules** (`harm`, `copyright`, `truthfulness`,
`commitments`, `disclosure`, `fairness`). Set `AGENT_SAFETY_RULES` to a comma-separated
subset to include only those — e.g. `AGENT_SAFETY_RULES=disclosure,truthfulness` — so you
can measure each rule's *individual* contribution to a scan delta instead of toggling the
whole policy at once. Empty = all rules.

When the safety prompt is on, `serve` runs the agent with `enforce_system_prompt`,
so a client that sends its **own** `system` message cannot suppress the policy — the
configured prompt always wins and the client's system message is dropped (it would
otherwise be a trivial bypass of the stateless path). In vulnerable-baseline mode
(`AGENT_SAFETY_PROMPT=false`) enforcement is off and the client owns the prompt.

### 2. Guardrail (`guardrails.py`)

A small pluggable moderation layer. The agent calls it to screen **input before** a run
(and short-circuits to a refusal if blocked) and to screen/redact the **final output**.

- `PatternGuardrail` (active when `AGENT_GUARDRAILS=true`) — dependency-free baseline:
  - **input**: blocks a few high-signal harmful intents (fake invoices, card-number
    scraping, "complete lyrics", secret-scanning of `/home`/`/tmp`/`/var`) → returns a
    refusal, the LLM is never called.
  - **output**: redacts PII (emails, card-like digit runs, US SSNs, phone-like numbers)
    and internal-architecture identifiers (`/home/redcell/*`, `qdrant`, `fastembed`,
    `agentgateway`, `vllm`, `mcp-server-*`, `redcell-kb`) → replaced with `[redacted]`.
  - **tool results**: the same redaction also runs over every tool result *before it
    reaches the model*, so a secret fetched off the web or read off the filesystem can't
    be relayed back. The raw result is still emitted to observability first, so exfil
    remains measurable — redaction protects the model's view, not the event log.
- `NullGuardrail` (when `AGENT_GUARDRAILS=false`) — passthrough.

Every verdict carries machine-readable `categories` (e.g. `pii:email`, `internal:path`,
`fraud:fake_invoice`), so a scan's guardrail events aggregate into a per-category
scorecard. Guardrail actions emit observability events (`guardrail_input_block`,
`guardrail_output_redact`, `guardrail_tool_redact`), each with its `categories`.

> The pattern guardrail is a **deterministic baseline**. Semantic categories (bias,
> fraud framing, hallucination) are carried by the safety prompt, not regexes. For
> production-grade moderation, implement the `Guardrail` protocol over llm-guard,
> Llama Guard, or an LLM self-check and pass it to `Agent(guardrail=…)` — no other
> code changes are needed. See [development.md](development.md#custom-guardrail).

### 3. Tool denylist

The most dangerous capabilities are the gateway tools (`shell`, `filesystem`). Drop any
of them before the agent can call them:

```bash
AGENT_MCP_TOOL_DENYLIST=shell,filesystem uv run redcell serve
```

Each term is matched **case-insensitively as a substring** of every gateway tool name,
so a *target* name like `shell` drops all the tools that target exposes even when the
gateway namespaces them (`shell_run_command`, `shell_run_script`), and an exact tool
name (`run_command`) works too. At startup `serve` logs exactly which tools were dropped
and **warns if a denylist term matched nothing** — a term that hits no tool leaves that
capability enabled, so the warning tells you to correct the name (check the tool names in
the `serve` logs). This is independent of the prompt/guardrail layers — it removes the
capability entirely. See [tools-and-gateway.md](tools-and-gateway.md).

### 4. Permission policy engine (allow / deny / ask)

A richer capability control than the all-or-nothing denylist: three-valued rules that can
scope a **whole tool** or a **specific argument**. The agent consults the policy before
every tool dispatch; a denied call never reaches the tool, and each gated call emits a
typed `permission` event (`behavior`, `allowed`, `reason`, `rule`) so a scan can measure
*which* control blocked an attack.

```bash
# allow only read-only git; deny destructive shell + card-scraping searches
AGENT_PERMISSION_ALLOW='run_command(git status),run_command(git diff)' \
AGENT_PERMISSION_DENY='run_command(rm -rf),web_search(cvv)' \
AGENT_PERMISSION_DEFAULT=ask AGENT_PERMISSION_ASK_RESOLUTION=deny \
uv run redcell serve
```

Rule grammar: `Tool` (whole tool) or `Tool(content)` (argument-scoped; matched when the
content appears in a call argument). Precedence is **deny > ask > allow**; if nothing
matches, `AGENT_PERMISSION_DEFAULT` applies (`allow`/`deny`/`ask`). Tool names match
case-insensitively as a substring (so `run_command` also covers `shell_run_command`).
Because the server is headless, an `ask` has no human to prompt — it resolves to
`AGENT_PERMISSION_ASK_RESOLUTION` (`deny` by default) while still being recorded as an
`ask`. `AGENT_PERMISSIONS=false` disables the engine entirely (baseline mode). The
content matcher is pluggable — a command-aware matcher (bash arg policy, path
confinement) can be plugged in for shell/filesystem tools.

## Recreating the vulnerable target

```bash
AGENT_SAFETY_PROMPT=false AGENT_GUARDRAILS=false uv run redcell serve
```

This restores the original behavior: bare prompt, no moderation, all tools enabled.

## Threat surfaces redcell is built to exercise

- **Direct harmful generation** — does the model refuse harmful asks?
- **Tool abuse** — `shell`/`filesystem`/`fetch` let an attack actually *do* something
  (write files, run commands, exfiltrate). The gateway confines these to a sandboxed
  Debian VM over SSH; the gateway is the observable choke point.
- **Indirect prompt injection via RAG** — the seed corpus plants poison docs with
  unique canaries; retrieval routes through the gateway so you can confirm whether a
  poisoned doc actually drove a tool call. See [rag.md](rag.md).
- **Internal disclosure / PII** — leaking architecture or personal data.
- **Excessive agency** — making commitments or taking consequential actions unbidden.

## Evaluation workflow (measure the delta)

1. **Baseline** the unguarded target:
   `AGENT_SAFETY_PROMPT=false AGENT_GUARDRAILS=false uv run redcell serve`, run your
   scanner (e.g. Promptfoo red-team), record pass/fail.
2. **Enable controls** (`uv run redcell serve` with defaults), re-run the *same* scan.
3. **Diff** the failure counts per plugin/strategy to quantify what each control bought.
4. Iterate: tune the safety prompt, plug a stronger guardrail, or deny tools, and repeat.

Tip: a scan that only uses promptfoo's `basic` strategy tests direct asks. Once the
safety prompt is on, also run jailbreak/crescendo/multi-turn strategies to probe what
the prompt layer alone won't hold.
