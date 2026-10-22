#!/usr/bin/env bash
# Links the host skill folders (mounted read-only at their host paths, so the
# symlinks inside them still resolve) and the sandbox Cursor and Claude Code MCP
# configs into the container home, then hands off to the stock Agent Canvas
# entrypoint.
set -euo pipefail

HOST_HOME="${HOST_HOME:-}"

for root in .agents .claude .cursor .codex; do
  mkdir -p "$HOME/$root"
  if [ -n "$HOST_HOME" ] && [ -d "$HOST_HOME/$root/skills" ]; then
    ln -sfn "$HOST_HOME/$root/skills" "$HOME/$root/skills"
  fi
done

ln -sf /opt/sandbox/cursor-mcp.json "$HOME/.cursor/mcp.json"

# Claude Code keeps its user MCP servers inside ~/.claude.json, which it also
# rewrites, so the servers are merged into it instead of linked.
if [ -f /opt/sandbox/claude-mcp.json ]; then
  python3 - <<'PY'
import json, pathlib
path = pathlib.Path.home() / ".claude.json"
config = json.loads(path.read_text()) if path.exists() else {}
servers = json.loads(pathlib.Path("/opt/sandbox/claude-mcp.json").read_text())["mcpServers"]
config.setdefault("mcpServers", {}).update(servers)
path.write_text(json.dumps(config, indent=2))
path.chmod(0o600)
PY
fi

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

# The same hook for Claude Code, in ~/.claude/settings.json. Hook only: no
# RTK.md added to the context of every conversation.
rtk init -g --agent claude --hook-only --auto-patch </dev/null >/dev/null || echo "rtk: could not install the Claude Code hook" >&2

exec /opt/agent-canvas/entrypoint.sh
