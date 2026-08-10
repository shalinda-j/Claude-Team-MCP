# Claude Team MCP — container image.
#
# The server speaks MCP on stdio, so a client attaches to the container's stdin
# and stdout; there is no port to expose unless you start the dashboard. Run it
# with `-i` (not `-t`: a TTY would corrupt the JSON-RPC stream).
#
#   docker build -t claude-team-mcp .
#   docker run --rm -i -v claude-team-state:/data claude-team-mcp
#
# Register it with a client:
#   claude mcp add team -s user -- docker run --rm -i \
#       -v claude-team-state:/data claude-team-mcp
#
# Everything the server writes lives under /data, which is a volume, so state
# survives `docker rm`. Without the volume a container restart loses the team.

FROM python:3.12-slim

# tini reaps the zombie processes the hub creates when it spawns downstream MCP
# servers, and forwards signals so `docker stop` is not a 10-second wait.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the metadata first so the dependency layer is cached independently of
# the source; editing team_coordinator.py should not re-resolve pip.
COPY pyproject.toml README.md ./
COPY team_coordinator.py ./
RUN pip install --no-cache-dir .

# Unprivileged. The hub can spawn processes an operator registers, so it should
# not be doing that as root, and /data must be writable by this user.
RUN useradd --create-home --uid 10001 team \
 && mkdir -p /data \
 && chown -R team:team /data
USER team

# One directory for everything stateful, so a single volume covers it.
ENV TEAM_STATE_FILE=/data/shared_state.json \
    BRAIN_DIR=/data/second_brain \
    TEAM_BACKUP_DIR=/data/backups \
    GATEWAY_FILE=/data/gateway.json \
    GATEWAY_VAULT_FILE=/data/gateway_vault.json \
    ADAPTER_DIR=/data/adapters \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

VOLUME ["/data"]

# doctor exits non-zero when the environment is broken, which is exactly the
# question a healthcheck asks. It also runs without a client attached.
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s --retries=3 \
    CMD ["claude-team-mcp", "doctor"]

ENTRYPOINT ["/usr/bin/tini", "--", "claude-team-mcp"]
