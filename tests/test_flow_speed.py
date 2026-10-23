#!/usr/bin/env python3
"""Offline tests for the speed changes: a profile per station, the quick pass between ticks, the demo kit.

  python3 tests/test_flow_speed.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import TEMPLATES, load_engine, project, sh  # noqa: E402

ROOT, CLONE = project("speed-test-", instance={"agent_profile_id": "default-profile", "station_profiles": {"demo": "demo-profile"}})
(CLONE / ".factory" / "demo-kit.mjs").write_text("// demo kit\n")
sh("git", "add", ".factory", cwd=CLONE)
sh("git", "commit", "-qm", "demo kit", cwd=CLONE)
cfg = {"agent_profile_id": "default-profile", "station_profiles": {"demo": "demo-profile"}}
f = load_engine()

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {detail}" if detail else ""))
    fails += 0 if cond else 1


posts, statuses, advanced = [], {}, []


def fake_canvas(method, path, body=None):
    if method == "POST":
        posts.append(body)
        return {"id": f"conv-{len(posts)}"}
    return {"execution_status": statuses.get(path.rsplit("/", 1)[-1], "running")}


f.canvas = fake_canvas
f.profile_secrets = lambda profile: {}
f.notify = lambda *a, **k: None
rec = {"identifier": "HES-1", "worktree": str(ROOT), "stage": "queued"}

# a profile per station
f.start_station(rec, "build", "go")
f.start_station(rec, "demo", "go")
check("build uses the default profile", posts[0]["agent_profile_id"] == cfg["agent_profile_id"])
check("demo uses its own profile", posts[1]["agent_profile_id"] == cfg["station_profiles"]["demo"]
      and rec["conversations"][-1]["profile"] == cfg["station_profiles"]["demo"])

# quick pass: only finished conversations advance, and without a full tick
f.advance = lambda r: advanced.append(r["identifier"])
state = {"issues": {
    "HES-1": {"identifier": "HES-1", "stage": "verify", "conversation_id": "a"},
    "HES-2": {"identifier": "HES-2", "stage": "judge", "conversation_id": "b"},
    "HES-3": {"identifier": "HES-3", "stage": "ci"},
    "HES-4": {"identifier": "HES-4", "stage": "review", "conversation_id": "c"}}}
statuses.update(a="finished", b="running", c="finished")
check("stations_active sees running stations", f.stations_active(state))
f.quick_tick(state)
check("quick pass advances only the finished station", advanced == ["HES-1"], advanced)
check("no station active once all are idle", not f.stations_active({"issues": {"x": {"stage": "review", "conversation_id": "c"}}}))

# the demo kit is installed next to the demo tools before a demo
f.git = lambda *a, **k: "abc123"
f.issue_detail = lambda i: {"identifier": "HES-1", "title": "t", "url": "u", "description": "d", "comments": {"nodes": []}}
demo_rec = {"identifier": "HES-1", "issue_id": "i", "worktree": str(ROOT), "base": "main", "feature": "x", "issue_url": "u",
            "branch": "feat/x", "stage": "final"}
f.start_demo(demo_rec, "f" * 40, "final")
kit = ROOT / "tools" / "demo-kit.mjs"
check("demo kit installed", kit.is_file() and kit.read_text() == (CLONE / ".factory" / "demo-kit.mjs").read_text())
check("demo started with the demo profile", posts[-1]["agent_profile_id"] == cfg["station_profiles"]["demo"])

# the prompts no longer wait for CI
build, fix = (TEMPLATES / "prompts" / "build.md").read_text(), (TEMPLATES / "prompts" / "fix.md").read_text()
check("build and fix do not wait for CI", "iterate" not in build and "iterate" not in fix and "Do not wait for CI" in build + fix)
check("demo prompt points at the kit", "demo-kit.mjs" in (TEMPLATES / "prompts" / "demo.md").read_text())

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
