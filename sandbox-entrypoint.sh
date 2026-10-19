#!/usr/bin/env bash
# Links the host skill folders (mounted read-only at their host paths, so the
# symlinks inside them still resolve) and the sandbox Cursor MCP config into
# the container home, then hands off to the stock Agent Canvas entrypoint.
set -euo pipefail

HOST_HOME=/Users/vitale

for root in .agents .claude .cursor .codex; do
  mkdir -p "$HOME/$root"
  if [ -d "$HOST_HOME/$root/skills" ]; then
    ln -sfn "$HOST_HOME/$root/skills" "$HOME/$root/skills"
  fi
done

ln -sf /opt/sandbox/cursor-mcp.json "$HOME/.cursor/mcp.json"

# Lets git push and fetch over HTTPS with the GitHub token from .env.
if [ -n "${GH_TOKEN:-}" ]; then
  gh auth setup-git
fi

# The factory service runs its own command instead of the Canvas stack.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# RTK's global Cursor hook in ~/.cursor/hooks.json. ACP sessions read only the
# user-level hooks file, not the project's .cursor/hooks.json. Re-running only
# keeps the entry in place.
rtk init -g --agent cursor </dev/null >/dev/null || echo "rtk: could not install the Cursor hook" >&2

exec /opt/agent-canvas/entrypoint.sh
