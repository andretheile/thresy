# syntax=docker/dockerfile:1
#
# Test image for the agent's user switch on real Linux; docker/isolation-test.sh builds it,
# runs the tests and the self-test in it, and removes it. It is not the application image: it
# has the runtime stage's base image, the two users and the helper exactly as docker/Dockerfile
# sets them up (same scripts), the standard-library-only runner package, and pytest.

ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ${PYTHON_IMAGE} AS helper
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY docker/run-as-agent.c docker/build-run-as-agent.sh /build/
RUN sh /build/build-run-as-agent.sh /build/run-as-agent.c /build/run-as-agent

FROM ${PYTHON_IMAGE}
RUN pip install --no-cache-dir --root-user-action=ignore "pytest==9.2.0"
COPY docker/setup-users.sh /tmp/setup-users.sh
COPY --from=helper /build/run-as-agent /tmp/run-as-agent
RUN sh /tmp/setup-users.sh /tmp/run-as-agent && rm /tmp/setup-users.sh /tmp/run-as-agent
# The agent's credential file as an operator writes it (synthetic values; see docs/deploy.md).
RUN printf '%s\n' '# test credential' 'export THRESY_TEST_CREDENTIAL="from the agent file"' \
      'THRESY_TEST_PLAIN=plain' > /home/agent/.thresy/credentials.env \
 && chown agent:agent /home/agent/.thresy/credentials.env \
 && chmod 0600 /home/agent/.thresy/credentials.env
COPY src/thresy/__init__.py /app/src/thresy/__init__.py
COPY src/thresy/runner /app/src/thresy/runner
COPY tests/__init__.py /app/tests/__init__.py
COPY tests/runner /app/tests/runner
RUN echo /app/src > "$(python -c 'import site; print(site.getsitepackages()[0])')/thresy-src.pth"
ENV PYTHONDONTWRITEBYTECODE=1 \
    THRESY_TEST_AGENT_USER=agent \
    COACH_WORKSPACE_DIR=/workspace \
    COACH_DATA_DIR=/data
USER coach
WORKDIR /app
