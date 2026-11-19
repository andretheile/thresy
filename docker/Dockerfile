# syntax=docker/dockerfile:1
#
# The application image: the `coach` CLI plus one coding-agent CLI. The sync poller and the
# chat bridge run as separate Compose services from this image.
#
#   docker build -f docker/Dockerfile --build-arg AGENT_CLI=claude -t thresy-app .
#
# AGENT_CLI selects the coding agent: claude (default), codex, cursor or none.
# AGENT_CLI_VERSION optionally pins its version (claude: apt package version, codex: release).

ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.0

FROM ${UV_IMAGE} AS uv

# --- build: resolve the locked dependencies into /app/.venv --------------------------------
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
# Editable install on purpose: the package finds migrations/ next to src/.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# The coding agent: an isolated, switchable step.
ARG AGENT_CLI=claude
ARG AGENT_CLI_VERSION=
COPY docker/install-agent.sh /usr/local/bin/install-agent
RUN chmod 0755 /usr/local/bin/install-agent \
 && install-agent "$AGENT_CLI" "$AGENT_CLI_VERSION"

RUN groupadd --gid 10001 coach \
 && useradd --uid 10001 --gid coach --create-home --home-dir /home/coach --shell /bin/bash coach \
 && mkdir -p /data /workspace \
 && chown coach:coach /data /workspace

COPY --from=build /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    COACH_DATA_DIR=/data \
    COACH_WORKSPACE_DIR=/workspace \
    AGENT_CLI=${AGENT_CLI} \
    DISABLE_AUTOUPDATER=1

USER coach
WORKDIR /workspace
CMD ["coach", "--help"]
