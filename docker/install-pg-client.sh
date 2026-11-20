#!/bin/sh
# Install pg_dump, pg_restore and psql of one PostgreSQL major version from the PostgreSQL
# project's signed apt repository (https://www.postgresql.org/download/linux/debian/). The backup
# needs the client of the server's major version: Debian's own postgresql-client is a different
# major version, and a newer pg_dump writes statements an older server rejects.
#
#   install-pg-client.sh MAJOR        (as root, on Debian; installs into /usr/lib/postgresql/MAJOR)
set -eu

MAJOR="${1:?usage: install-pg-client.sh MAJOR}"
# Fingerprint of the PostgreSQL apt repository signing key (ACCC4CF8), as published on
# https://www.postgresql.org/download/linux/debian/ and in the apt.postgresql.org wiki.
PGDG_KEY_FPR="B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"

. /etc/os-release
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gpg
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /etc/apt/keyrings/pgdg.asc
fpr="$(gpg --show-keys --with-colons /etc/apt/keyrings/pgdg.asc | awk -F: '/^fpr:/ { print $10; exit }')"
if [ "$fpr" != "$PGDG_KEY_FPR" ]; then
  echo "install-pg-client: unexpected PostgreSQL repository key fingerprint: $fpr" >&2
  exit 1
fi
echo "deb [signed-by=/etc/apt/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update
apt-get install -y --no-install-recommends "postgresql-client-$MAJOR"
apt-get purge -y --auto-remove gpg
rm -rf /var/lib/apt/lists/*
"/usr/lib/postgresql/$MAJOR/bin/pg_dump" --version
"/usr/lib/postgresql/$MAJOR/bin/pg_restore" --version
