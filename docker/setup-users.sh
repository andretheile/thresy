#!/bin/sh
# Users, volume mount points and the user-switch helper of the application image. Runs as root
# at build time, in docker/Dockerfile and in the isolation test image.
#
#   setup-users.sh [HELPER]      HELPER: the compiled run-as-agent to install (optional)
#
#   coach (COACH_UID)  runs every service: migrate, sync, bridge, backup
#   agent (AGENT_UID)  runs the coding-agent CLI; the bridge starts it through
#                      /usr/local/libexec/thresy/run-as-agent (docs/runners.md)
#
# coach is also a member of the agent group: the workspace is shared through that group (the
# bridge writes inbox/ and reads out/, the agent the other way round), while the agent is in no
# group of coach's. A new named volume takes the owner and mode of its mount point in the image,
# which is why the mount points are created here.
set -eu

: "${COACH_UID:=10001}"
: "${AGENT_UID:=10002}"
: "${AGENT_GID:=10002}"
: "${AGENT_HOME:=/home/agent}"
HELPER="${1:-}"

groupadd --gid "$COACH_UID" coach
useradd --uid "$COACH_UID" --gid coach --create-home --home-dir /home/coach --shell /bin/bash coach
groupadd --gid "$AGENT_GID" agent
useradd --uid "$AGENT_UID" --gid agent --create-home --home-dir "$AGENT_HOME" --shell /bin/bash agent
usermod --append --groups agent coach

chmod 0700 /home/coach
# The agent's home (volume agent-home) and the directory of its credential file.
install -d -o agent -g agent -m 0700 "$AGENT_HOME" "$AGENT_HOME/.thresy"
# The coach workspace (volume workspace): owned by the agent, shared with coach through the
# group; setgid so that everything created inside keeps the group.
install -d -o agent -g agent -m 2770 /workspace
# FIT files (volume app-data; read-only in the bridge), Garmin session (volume garmin-auth, sync
# only), local backups (volume backups, backup only).
install -d -o coach -g coach -m 0755 /data
install -d -o coach -g coach -m 0700 /garmin /backups

if [ -n "$HELPER" ]; then
  install -d -o root -g root -m 0755 /usr/local/libexec/thresy
  # setuid root, executable only by root and the coach group.
  install -o root -g coach -m 4750 "$HELPER" /usr/local/libexec/thresy/run-as-agent
fi
