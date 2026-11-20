#!/bin/sh
# Test the agent's user switch on real Linux: build a small test image (docker/isolation-test.
# Dockerfile), run the process-group and isolation tests and `check --isolation` in it with the
# bridge's container settings (init process, all capabilities dropped except SETUID and SETGID),
# then remove the image.
#
#   sh docker/isolation-test.sh          (from the repository root; needs Docker)
set -eu

IMAGE=thresy-isolation-test:local
docker build -f docker/isolation-test.Dockerfile -t "$IMAGE" .
trap 'docker image rm -f "$IMAGE" >/dev/null 2>&1 || true' EXIT
echo "image size: $(docker image inspect "$IMAGE" --format '{{.Size}}') bytes"

run() {
  docker run --rm --init --cap-drop ALL --cap-add SETUID --cap-add SETGID "$@"
}

run "$IMAGE" python -m pytest -q --noconftest -p no:cacheprovider -o addopts="" \
  tests/runner/test_runner_process.py tests/runner/test_isolation.py \
  tests/runner/test_isolation_linux.py

run -e COACH_AGENT_USER=agent "$IMAGE" sh -c \
  'umask 0007 && mkdir -m 2770 /workspace/inbox /workspace/out && python -m thresy.runner check --isolation'
