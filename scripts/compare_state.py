#!/usr/bin/env python3
"""Exit 0 when every issue of the first state file keeps its stage, PR, conversation and Slack thread in the second.

  python3 scripts/compare_state.py before.json after.json
"""
import json
import sys

KEPT = ("stage", "pr_number", "conversation_id", "slack_channel", "slack_ts")

before, after = (json.load(open(path)) for path in sys.argv[1:3])
changed = []
for ident, rec in before["issues"].items():
    now = after["issues"].get(ident)
    if now is None:
        changed.append(f"{ident}: missing")
        continue
    changed += [f"{ident}: {key} {rec.get(key)!r} -> {now.get(key)!r}" for key in KEPT if rec.get(key) != now.get(key)]
print("\n".join(changed) or f"{len(before['issues'])} issues kept {', '.join(KEPT)}")
sys.exit(1 if changed else 0)
