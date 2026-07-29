# Execution sandbox image for redcell's `shell` and `filesystem` MCP targets.
#
# OpenShell creates a container from this image and the agent reaches the two
# MCP servers over SSH (see agentgateway/config.yaml). The servers are baked in
# so the sandbox needs no network at runtime -- `npx` never fetches during a
# scan, and the deny-by-default egress policy in policy.yaml stays intact.
#
# OpenShell's bring-your-own-container requirements this image satisfies:
#   - a normal glibc distro (distroless and scratch are unsupported)
#   - iproute2, for the supervisor's network-namespace setup
#   - a non-root OCI USER
#   - /sandbox, writable by that user
# The supervisor replaces CMD/ENTRYPOINT at runtime, so there is no CMD here;
# the command comes from the SSH invocation instead.
FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      iproute2 \
 && rm -rf /var/lib/apt/lists/*

# Pinned: an unpinned tag would make the sandbox's tool surface change
# underneath a scan, and re-resolving at runtime needs the egress this image
# deliberately does not have.
RUN npm install -g --no-fund --no-audit \
      mcp-server-commands@0.8.2 \
      @modelcontextprotocol/server-filesystem@2026.7.10

# Reuse the base image's uid/gid 1000 (`node`) rather than adding a second
# unprivileged account, but rename it to `sandbox`: OpenShell's policy validator
# only accepts `run_as_user: sandbox` or a numeric uid >= 1000, and the name
# reads better in policy.yaml than a bare number.
RUN usermod --login sandbox --move-home --home /home/sandbox node \
 && groupmod --new-name sandbox node \
 && mkdir -p /sandbox \
 && chown sandbox:sandbox /sandbox

USER sandbox
WORKDIR /sandbox
