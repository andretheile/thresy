#!/bin/sh
# Start the bridge service: bring the coach workspace up to date with the template shipped in
# the image (coach workspace init), then run the chat loop.
#
# umask 007: what the bridge creates in the shared workspace (files from the athlete in inbox/)
# stays readable for the agent's group and private otherwise. A failing workspace update is
# reported but does not keep the bridge from starting: the workspace belongs to the agent, which
# could otherwise stop the bridge by breaking a template path.
set -eu
umask 0007
coach workspace init || echo "bridge-start: coach workspace init failed; starting the bridge anyway" >&2
exec coach bridge run
