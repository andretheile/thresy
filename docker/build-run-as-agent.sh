#!/bin/sh
# Compile docker/run-as-agent.c (the user-switch helper) with hardening flags.
#
#   build-run-as-agent.sh SOURCE OUTPUT
#
# The user and group ids must match docker/setup-users.sh; both read the same variables.
# Needs a C compiler and the C library headers (Debian: gcc libc6-dev), only at build time.
set -eu

SOURCE="$1"
OUTPUT="$2"
: "${COACH_UID:=10001}"
: "${AGENT_UID:=10002}"
: "${AGENT_GID:=10002}"
: "${AGENT_HOME:=/home/agent}"

cc -std=c11 -O2 -Wall -Wextra -Werror \
  -D_FORTIFY_SOURCE=2 -fstack-protector-strong -fPIE -pie -Wl,-z,relro,-z,now \
  -DCALLER_UID="$COACH_UID" -DAGENT_UID="$AGENT_UID" -DAGENT_GID="$AGENT_GID" \
  -DCREDENTIALS_FILE="\"$AGENT_HOME/.thresy/credentials.env\"" \
  -o "$OUTPUT" "$SOURCE"
strip "$OUTPUT"
