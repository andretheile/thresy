# syntax=docker/dockerfile:1
#
# The application image: the `coach` CLI plus one coding-agent CLI. The Compose services
# migrate, sync, bridge and backup all run from it, as the non-root user coach; the bridge
# starts the agent CLI as the user agent (docs/runners.md, "The agent's own Unix user").
#
#   docker build -f docker/Dockerfile \
#     --build-arg AGENT_CLI=claude --build-arg AGENT_CLI_VERSION=2.1.311-1 -t thresy-app .
#
# AGENT_CLI selects the coding agent: claude (default), codex, cursor or none.
# AGENT_CLI_VERSION pins its version and is required for claude and codex, so that upgrades are
# deliberate: claude takes the apt package version (e.g. 2.1.311-1), codex a release version
# (the GitHub release tag without its rust-v prefix); "latest" opts out of pinning. cursor's
# installer cannot pin a version.
# PG_MAJOR is the PostgreSQL major version of the db service (postgres:16-alpine in
# compose.yml); the backup's pg_dump and pg_restore must have the same one.
#
# The build needs the repository's workspace/ directory (the coach workspace template).

ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.0

FROM ${UV_IMAGE} AS uv

# --- helper: the setuid user-switch helper (docker/run-as-agent.c) -----------------------------
# Only the compiled binary leaves this stage; the compiler does not reach the runtime image.
FROM ${PYTHON_IMAGE} AS helper
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY docker/run-as-agent.c docker/build-run-as-agent.sh /build/
RUN sh /build/build-run-as-agent.sh /build/run-as-agent.c /build/run-as-agent

# --- build: resolve the locked dependencies into /app/.venv ------------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
# Editable install on purpose: the package finds migrations/ and workspace/ next to src/.
# The sources are compiled here because /app is read-only for the users at run time.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
 && /app/.venv/bin/python -m compileall -q src

# --- runtime -------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# curl and git for the agent, age for encrypted backups.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git age \
 && rm -rf /var/lib/apt/lists/*

# pg_dump and pg_restore of the database's major version, for the backup service.
ARG PG_MAJOR=16
COPY docker/install-pg-client.sh /tmp/install-pg-client.sh
RUN sh /tmp/install-pg-client.sh "$PG_MAJOR" && rm /tmp/install-pg-client.sh

# Users coach and agent, the volume mount points, and the setuid helper
# /usr/local/libexec/thresy/run-as-agent (root:coach, 4750).
COPY docker/setup-users.sh /tmp/setup-users.sh
COPY --from=helper /build/run-as-agent /tmp/run-as-agent
RUN sh /tmp/setup-users.sh /tmp/run-as-agent && rm /tmp/setup-users.sh /tmp/run-as-agent

# The coding agent: an isolated, switchable step (after the users, which change less often).
ARG AGENT_CLI=claude
ARG AGENT_CLI_VERSION=
COPY docker/install-agent.sh /usr/local/bin/install-agent
RUN chmod 0755 /usr/local/bin/install-agent \
 && install-agent "$AGENT_CLI" "$AGENT_CLI_VERSION"

COPY --from=build /app /app
COPY docker/bridge-start.sh /usr/local/bin/bridge-start
# The coach workspace template; `coach workspace init` copies it into the workspace volume.
COPY workspace /app/workspace
RUN chmod 0755 /usr/local/bin/bridge-start \
 && chmod -R a+rX,go-w /app/workspace

# DISABLE_AUTOUPDATER / DISABLE_UPDATES: the agent CLI version is pinned by the image build
# (compose.yml also passes DISABLE_AUTOUPDATER on to the agent).
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COACH_DATA_DIR=/data \
    COACH_WORKSPACE_DIR=/workspace \
    AGENT_CLI=${AGENT_CLI} \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1

# matplotlib keeps its configuration and font cache in the user's home ($HOME/.config and
# $HOME/.cache); both homes are writable by their user (the agent's is the agent-home volume,
# which starts from this content). Building the font cache now saves the first plot a few
# seconds.
RUN for user in coach agent; do \
      home="$(getent passwd "$user" | cut -d: -f6)"; \
      runuser -u "$user" -- env HOME="$home" MPLBACKEND=Agg \
        /app/.venv/bin/python -c "import matplotlib.font_manager"; \
    done

USER coach
# Not /workspace: `coach` reads a .env file from its working directory, and the agent can write
# the workspace. /app belongs to root.
WORKDIR /app
CMD ["coach", "--help"]
