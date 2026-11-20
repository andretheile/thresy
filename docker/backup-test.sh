#!/bin/sh
# Run the backup tests with the real PostgreSQL client tools and age, in a throwaway container of
# the runtime base image, against the development Postgres (compose.dev.yml must be running).
# Installs the client the way docker/Dockerfile does (docker/install-pg-client.sh); nothing is
# kept afterwards. Fails if a test is skipped, since running what is skipped elsewhere is the
# point of this script. CI runs it too (.github/workflows/ci.yml).
#
#   sh docker/backup-test.sh          (from the repository root; needs Docker and network)
set -eu

PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.13-slim-trixie}"
PG_MAJOR="${PG_MAJOR:-16}"

# The development Postgres listens on 127.0.0.1 of the Docker host. Docker Desktop (macOS,
# Windows) reaches that as host.docker.internal; on Linux the container shares the host's network
# instead, because the host's loopback address is not reachable from a bridge network.
if [ "$(uname -s)" = Linux ]; then
  NETWORK="--network=host"
  DB_HOST=127.0.0.1
else
  NETWORK="--add-host=host.docker.internal:host-gateway"
  DB_HOST=host.docker.internal
fi
ADMIN_URL="${COACH_TEST_ADMIN_URL:-postgresql://postgres:postgres@$DB_HOST:55432/postgres}"

docker run --rm -v "$PWD:/src:ro" "$NETWORK" \
  -e COACH_TEST_ADMIN_URL="$ADMIN_URL" -e PG_MAJOR="$PG_MAJOR" \
  "$PYTHON_IMAGE" sh -eu -c '
    sh /src/docker/install-pg-client.sh "$PG_MAJOR" > /tmp/pg-client.log 2>&1 \
      || { cat /tmp/pg-client.log; exit 1; }
    tail -2 /tmp/pg-client.log
    apt-get update -qq && apt-get install -y -qq --no-install-recommends age > /dev/null
    age --version
    pip install -q --root-user-action=ignore --disable-pip-version-check \
      "psycopg[binary,pool]==3.3.9" "pydantic-settings==2.16.3" "typer==0.29.2" \
      "python-dotenv==1.2.4" "google-cloud-storage==3.17.1" "pytest==9.2.0"
    mkdir /work && cp -r /src/src /src/tests /src/migrations /src/pyproject.toml /work/
    cd /work
    status=0
    PYTHONPATH=src python -m pytest -q -p no:cacheprovider -rs tests/backup tests/test_store.py \
      > /tmp/pytest.log 2>&1 || status=$?
    cat /tmp/pytest.log
    [ "$status" -eq 0 ] || exit "$status"
    if grep -q "^SKIPPED" /tmp/pytest.log; then
      echo "backup-test: tests were skipped although the real tools are installed" >&2
      exit 1
    fi
  '
