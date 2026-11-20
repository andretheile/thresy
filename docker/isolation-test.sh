#!/bin/sh
# Test the agent's user switch on real Linux: build a small test image (docker/isolation-test.
# Dockerfile), run the process-group and isolation tests and `check --isolation` in it with the
# bridge's container settings (init process, all capabilities dropped except SETUID and SETGID),
# then remove the image. Fails if a test is skipped, since running what is skipped elsewhere is
# the point of this script. CI runs it too (.github/workflows/ci.yml).
#
#   sh docker/isolation-test.sh          (from the repository root; needs Docker)
set -eu

IMAGE=thresy-isolation-test:local
LOG="$(mktemp)"
trap 'docker image rm -f "$IMAGE" >/dev/null 2>&1 || true; rm -f "$LOG"' EXIT
docker build -f docker/isolation-test.Dockerfile -t "$IMAGE" .
echo "image size: $(docker image inspect "$IMAGE" --format '{{.Size}}') bytes"

run() {
  docker run --rm --init --cap-drop ALL --cap-add SETUID --cap-add SETGID "$@"
}

status=0
run "$IMAGE" python -m pytest -q --noconftest -p no:cacheprovider -o addopts="" -rs \
  tests/runner/test_runner_process.py tests/runner/test_isolation.py \
  tests/runner/test_isolation_linux.py > "$LOG" 2>&1 || status=$?
cat "$LOG"
[ "$status" -eq 0 ] || exit "$status"
if grep -q "^SKIPPED" "$LOG"; then
  echo "isolation-test: tests were skipped in the test image" >&2
  exit 1
fi

run -e COACH_AGENT_USER=agent "$IMAGE" sh -c \
  'umask 0007 && mkdir -m 2770 /workspace/inbox /workspace/out && python -m thresy.runner check --isolation'
