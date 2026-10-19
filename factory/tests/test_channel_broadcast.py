#!/usr/bin/env python3
"""Offline test: an issue's first station reaches the channel, later stations stay in the thread.

  python3 tools/agent-factory/factory/tests/test_channel_broadcast.py tools/agent-factory/factory
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

FACTORY = Path(sys.argv[1]).resolve()
os.environ["FACTORY_STATE"] = os.path.join(tempfile.mkdtemp(prefix="broadcast-test-"), "state.json")
os.environ.setdefault("LINEAR_API_KEY", "x")
os.environ.setdefault("SLACK_BOT_TOKEN", "x")
spec = importlib.util.spec_from_file_location("factory", FACTORY / "factory.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

posts = []
f.slack = lambda method, **p: posts.append((method, p)) or {"ts": "1.0"}
f.canvas = lambda m, path, body=None: {"id": f"conv{len(posts)}"}
f.mutate = lambda what: True
f.root_text = lambda r: "root"
rec = {"identifier": "HES-9", "slack_channel": "C1", "slack_ts": "0.1", "stage": "blocked", "worktree": "/tmp"}
f.start_station(rec, "build", "prompt")
f.start_station(rec, "verify", "prompt")
replies = [p for m, p in posts if m == "chat.postMessage"]
fails = 0
for name, ok in (("first station reaches the channel", replies[0]["reply_broadcast"] is True),
                 ("first station does not mention the person", "<@" not in replies[0]["text"]),
                 ("later station stays in the thread", replies[1]["reply_broadcast"] is False)):
    print(("PASS " if ok else "FAIL ") + name)
    fails += not ok
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
