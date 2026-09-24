# redcell: the agent server, plus everything AgentGateway spawns as a child.
#
# This image is large and that is the intended trade. AgentGateway launches its
# MCP servers as stdio subprocesses, so they have to live in the same container
# as the gateway: Node, Chromium, uv, the agentgateway binary and the openshell
# CLI all belong here. Baking them in also removes the per-start
# `npx -y ...@latest` and `uvx` network fetches, so a scan does not depend on npm
# and PyPI being reachable, and the tool surface cannot shift between runs.
#
# Layers are ordered so the cheapest-to-invalidate come last: system packages,
# pinned external binaries, Python dependencies, then application source.

# ---- agentgateway binary -----------------------------------------------------
# Lifted from the official image rather than downloaded, so there is no
# release-URL scraping to maintain.
# Pinned: the gateway config schema has changed between releases.
FROM ghcr.io/agentgateway/agentgateway:v1.4.1 AS agentgateway

# ---- runtime -----------------------------------------------------------------
# Node base rather than a Playwright base image. @playwright/mcp pins an exact
# (often alpha) playwright version, and MCR does not publish an image for every
# one of those — pairing them by hand means the browser and driver silently drift
# apart and fail at launch. Installing the browser via the MCP package's own
# playwright gets the matching build by construction.
#
# trixie, not bookworm: the openshell wheels are manylinux_2_39, and bookworm
# ships glibc 2.36. On bookworm pip/uv silently falls back to a stub sdist that
# provides no `openshell` executable, and the failure surfaces much later as
# shell/filesystem having no tools. trixie is glibc 2.41.
FROM node:22-trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    # A real venv at a fixed path, with its bin on PATH, so CMD can invoke the
    # console script directly without `uv run` or an activate step. Pointing this
    # at /usr/local instead fails: uv requires a directory that is already a valid
    # Python environment.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    UV_LINK_MODE=copy \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    # Where `uv python install` puts interpreters, so the runtime user finds them.
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      # The shell/filesystem MCP servers are launched over SSH into the OpenShell
      # sandbox, so the client is a hard requirement, not a convenience.
      openssh-client \
 && rm -rf /var/lib/apt/lists/*

# /app/agentgateway, not /usr/local/bin: the upstream image is distroless with the
# binary at its ENTRYPOINT path.
COPY --from=agentgateway /app/agentgateway /usr/local/bin/agentgateway
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /usr/local/bin/

# Let uv own the interpreter instead of apt: it pins the version against
# pyproject's requires-python, and Debian's python3 is whatever the release
# froze.
RUN uv python install 3.11

# Pinned, and verified rather than trusted: when the platform wheel does not
# match, uv installs a stub that provides no executable and still exits 0, so
# check the binary actually exists at build time instead of discovering it at
# runtime as two silently missing tools.
RUN uv tool install openshell==0.0.92 \
 && openshell --version

# Pinned, not `@latest`: the gateway config invokes this at every start, and an
# unpinned upgrade can change the tool surface mid-project. `--with-deps` pulls
# both the browser build this exact version wants and the system libraries it
# needs.
#
# The browser is installed by @playwright/mcp's own `install-browser`, which runs
# the playwright-core bundled inside the package. `npx playwright install` looks
# the same but is not: nothing global provides a `playwright` bin, so npx fetches
# the newest `playwright` from npm and installs *its* browser build, which the
# pinned MCP package then cannot find at launch.
RUN npm install -g --no-fund --no-audit @playwright/mcp@0.0.78 \
 && playwright-mcp install-browser --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# @playwright/mcp defaults to the branded Chrome channel, which is not installed
# here and has no linux/arm64 build at all, so point it at the Chromium above.
# Headless because the container has no display. Read from the environment by
# @playwright/mcp itself, so agentgateway/config.yaml stays the same for host
# mode, where a desktop Chrome is the better default.
ENV PLAYWRIGHT_MCP_BROWSER=chromium \
    PLAYWRIGHT_MCP_HEADLESS=true

# Warm the uv cache for the two uvx-launched MCP servers so the gateway does not
# hit PyPI at startup. `mcp<2` is required, not incidental: mcp 2.0 renamed
# McpError to MCPError and mcp-server-fetch still imports the old name, so an
# unpinned resolve crashes it on import — and AgentGateway reports a broken stdio
# target as a 500 on the *aggregated* endpoint, taking every other target down
# with it. Failures here are tolerated: a cold cache costs startup time, not
# correctness.
RUN uvx --with 'mcp<2' mcp-server-fetch --help >/dev/null 2>&1 || true \
 && uvx mcp-server-qdrant --help >/dev/null 2>&1 || true

WORKDIR /app

# Dependencies from the lockfile first, so editing source does not reinstall them.
# LICENSE is required, not incidental: pyproject declares `license = { file =
# "LICENSE" }`, and hatchling refuses to build the project without it.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra tracing --no-install-project

COPY redcell/ ./redcell/
COPY agentgateway/ ./agentgateway/
COPY sandbox/ ./sandbox/
RUN uv sync --frozen --no-dev --extra tracing

# Written at runtime: the ingestion manifest, the generated OpenShell SSH config,
# and Playwright traces. Created here so they exist even with no volume mounted.
RUN mkdir -p /app/.redcell /app/.playwright-mcp /app/documents

EXPOSE 8800

# Exec form with no shell wrapper, so uvicorn gets SIGTERM directly and the
# lifespan teardown actually runs (it stops AgentGateway and unwinds the
# supervisors).
CMD ["redcell", "serve"]
