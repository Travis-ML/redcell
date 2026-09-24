# Execution sandbox

This is where the agent's `shell` and `filesystem` tools actually run. Nothing in
here executes on the redcell host — that separation is the whole point, and it is
the one property to preserve when changing anything in this directory.

Two files:

- **`Dockerfile`** — the sandbox image. Node plus the two MCP servers
  (`mcp-server-commands`, `mcp-server-filesystem`), both pinned and preinstalled.
- **`policy.yaml`** — the OpenShell policy: unprivileged user, `/sandbox` and
  `/tmp` writable, everything else read-only, no network egress.

## How the agent reaches it

AgentGateway launches each MCP server over SSH, tunnelled to the sandbox through
the OpenShell gateway (see the `shell` and `filesystem` targets in
`agentgateway/config.yaml`). No SSH keys are involved — the OpenShell gateway
authenticates the tunnel, and `openshell sandbox ssh-config` generates the client
config, which `redcell serve` writes at startup.

The transport is SSH rather than `openshell sandbox exec` for a concrete reason:
`sandbox exec` buffers stdin until EOF, so a long-lived JSON-RPC session over it
never gets a reply. SSH streams in both directions. If you are tempted to switch
back to `exec`, re-test with stdin held open before you do.

## Rebuilding

```bash
docker compose build sandbox-image    # or: docker build -t redcell-sandbox:local sandbox/
```

The tag is what `openshell/gateway.toml` sets as `default_image`, with
`image_pull_policy = "IfNotPresent"`, so the locally built image is used as-is and
never pulled. That also avoids NVIDIA/OpenShell#674, where pushing a built image
into a containerized gateway fails on macOS.

The sandbox is recreated from this image on a cold start, so a rebuild takes
effect after:

```bash
openshell sandbox delete redcell-sbx
```

`redcell serve` recreates it on the next run.

## Why the servers are baked in

The policy denies all network egress, so the sandbox cannot fetch anything at
runtime — `npx -y some-server@latest` would simply fail. Preinstalling is what
makes a deny-by-default network policy compatible with having tools at all, and
it means the tool surface can't shift underneath a scan because an upstream tag
moved.

## Widening the policy

`filesystem_policy` and `process` are locked when the sandbox is created;
changing them requires deleting and recreating it. `network_policies` is
hot-reloadable:

```bash
openshell policy set redcell-sbx --policy sandbox/policy.yaml --wait
```

Granting egress is a deliberate, reviewable edit to `policy.yaml` rather than a
runtime flag — if a scan needs to reach a target, that should show up in a diff.

## Inspecting a run

```bash
openshell sandbox list                  # phase
openshell logs redcell-sbx              # policy allow/deny decisions
openshell sandbox connect redcell-sbx   # interactive shell inside it
openshell term                          # live TUI
```
