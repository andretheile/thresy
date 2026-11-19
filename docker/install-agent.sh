#!/bin/sh
# Install the coding-agent CLI chosen at image build time (Dockerfile build argument AGENT_CLI).
#
#   install-agent.sh claude|codex|cursor|none [version]
#
# Binaries go to system locations (not $HOME), because the agent's home directory is a volume
# that would hide anything installed there and keep stale copies across image updates.
# Self-updates are not used: the version is pinned here and upgraded by rebuilding the image.
# claude and codex need a version (claude: apt package version such as 2.1.311-1; codex: release
# version); the word "latest" installs the newest release instead.
set -eu

AGENT="${1:-none}"
VERSION="${2:-}"

require_version() {
  if [ -z "$VERSION" ]; then
    echo "install-agent: set AGENT_CLI_VERSION for $AGENT (a pinned version, or 'latest')" >&2
    exit 1
  fi
}

# Fingerprint of the Claude Code release signing key, as published in the official setup docs.
CLAUDE_KEY_FPR="31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"

case "$AGENT" in
  none)
    echo "install-agent: no coding-agent CLI installed"
    ;;

  claude)
    require_version
    # Official signed apt repository, stable channel:
    # https://code.claude.com/docs/en/setup (Install with Linux package managers)
    apt-get update
    apt-get install -y --no-install-recommends gpg
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc
    fpr="$(gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc | awk -F: '/^fpr:/ { print $10; exit }')"
    if [ "$fpr" != "$CLAUDE_KEY_FPR" ]; then
      echo "install-agent: unexpected Claude Code signing key fingerprint: $fpr" >&2
      exit 1
    fi
    echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
      > /etc/apt/sources.list.d/claude-code.list
    apt-get update
    if [ "$VERSION" = latest ]; then
      apt-get install -y --no-install-recommends claude-code
    else
      apt-get install -y --no-install-recommends "claude-code=$VERSION"
    fi
    apt-get purge -y --auto-remove gpg
    rm -rf /var/lib/apt/lists/*
    claude --version
    ;;

  codex)
    require_version
    # Official static (musl) release binaries from GitHub: https://github.com/openai/codex
    arch="$(uname -m)"
    case "$arch" in
      x86_64 | aarch64) ;;
      *) echo "install-agent: codex has no Linux build for $arch" >&2; exit 1 ;;
    esac
    asset="codex-${arch}-unknown-linux-musl"
    if [ "$VERSION" = latest ]; then
      url="https://github.com/openai/codex/releases/latest/download/${asset}.tar.gz"
    else
      url="https://github.com/openai/codex/releases/download/rust-v${VERSION}/${asset}.tar.gz"
    fi
    tmp="$(mktemp -d)"
    curl -fsSL "$url" | tar -xz -C "$tmp"
    install -m 0755 "$tmp/$asset" /usr/local/bin/codex
    rm -rf "$tmp"
    codex --version
    ;;

  cursor)
    # Official installer (https://cursor.com/docs/cli/installation). It installs into
    # $HOME/.local, so point HOME at /opt/cursor and link the launchers into PATH.
    if [ -n "$VERSION" ]; then
      echo "install-agent: the cursor installer has no version option; installing latest" >&2
    fi
    mkdir -p /opt/cursor
    HOME=/opt/cursor bash -c 'curl -fsS https://cursor.com/install | bash'
    linked=0
    for bin in agent cursor-agent; do
      if [ -e "/opt/cursor/.local/bin/$bin" ]; then
        ln -sf "/opt/cursor/.local/bin/$bin" "/usr/local/bin/$bin"
        linked=1
      fi
    done
    if [ "$linked" -ne 1 ]; then
      echo "install-agent: cursor installer did not create a launcher" >&2
      exit 1
    fi
    chmod -R a+rX /opt/cursor
    ;;

  *)
    echo "install-agent: unknown AGENT_CLI '$AGENT' (expected claude, codex, cursor or none)" >&2
    exit 1
    ;;
esac
