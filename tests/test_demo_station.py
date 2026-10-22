#!/usr/bin/env python3
"""Offline tests for the Demo station: manifest checks, markdown, evidence push, Linear upload, stage flow.

  python3 tests/test_demo_station.py

Uses a throwaway git origin and a local HTTP server; nothing reaches Linear, GitHub or Slack.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="demo-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import load_engine, project  # noqa: E402

# a throwaway origin + clone standing in for the project's clone in /projects
bare, clone = ROOT / "origin.git", ROOT / "clone"
subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
os.environ.update({k: env[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")})
(clone / "README.md").write_text("hi\n")
subprocess.run("git add . && git commit -qm init && git push -q origin HEAD:main", shell=True, cwd=clone, check=True)

ROOT_DEMO = {"demo": {"evidence_path": str(ROOT / "evidence"), "tools_path": str(ROOT / "tools")}}
project("demo-test-", repo=clone, instance=ROOT_DEMO)
f = load_engine()

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {detail}" if detail else ""))
    fails += 0 if cond else 1


# ── manifest ──
demo = ROOT / "evidence" / "HES-1" / "abcdef123456"
demo.mkdir(parents=True)
png = bytes.fromhex("89504e470d0a1a0a") + b"\0" * 64
for n in ("list__default__1440__light__before.png", "list__default__1440__light__after.png",
          "list__default__1440__light__design.png", "new__x__390__light__after.png"):
    (demo / n).write_bytes(png)
(demo / "filter.mp4").write_bytes(b"\0" * 2048)
(demo / "filter.gif").write_bytes(b"GIF89a" + b"\0" * 100)
manifest = {
    "issue": "HES-1", "head": "abcdef1234567890", "summary": "The list gets the new rows.",
    "screens": [
        {"title": "Cleanings list", "criterion": "Rows show the city", "shots": [
            {"viewport": "1440", "theme": "light", "before": "list__default__1440__light__before.png",
             "after": "list__default__1440__light__after.png", "design": "list__default__1440__light__design.png"}]},
        {"title": "New sheet", "shots": [{"viewport": "390", "theme": "light", "before": None,
                                          "after": "new__x__390__light__after.png", "design": None}]}],
    "videos": [{"title": "Filter by cleaner", "criterion": "Filter works", "steps": ["Open", "Pick", "See"],
                "mp4": "filter.mp4", "gif": "filter.gif", "seconds": 12}],
    "gaps": ["No error state in seed data"]}
(demo / "manifest.json").write_text(json.dumps(manifest))
m = f.load_manifest(demo)
check("valid manifest loads", m["issue"] == "HES-1")
check("files listed once each", f.demo_files(m) == ["list__default__1440__light__design.png", "list__default__1440__light__before.png",
                                                    "list__default__1440__light__after.png", "new__x__390__light__after.png",
                                                    "filter.gif", "filter.mp4"], f.demo_files(m))

for bad, why in ((dict(manifest, videos=[]), "no video"),
                 (dict(manifest, screens=[{"title": "x", "shots": [{"after": "../../../etc/passwd.png"}]}]), "escape"),
                 (dict(manifest, screens=[{"title": "x", "shots": [{"after": "missing.png"}]}]), "missing file"),
                 (dict(manifest, videos=[{"title": "v", "mp4": "filter.gif"}]), "wrong type")):
    (demo / "manifest.json").write_text(json.dumps(bad))
    try:
        f.load_manifest(demo)
        check(f"rejects {why}", False)
    except f.Stop as e:
        check(f"rejects {why}", True, str(e)[:90])
(demo / "manifest.json").write_text(json.dumps(manifest))

# ── markdown ──
rec = {"identifier": "HES-1", "pr_number": 7, "pr_url": "https://github.com/o/r/pull/7", "demo_for": "abcdef1234567890",
       "worktree": str(clone), "issue_id": "i1", "base": "main", "demo_dir": str(demo), "feature": "visual-refresh",
       "issue_url": "https://linear.app/x/HES-1", "branch": "feat/x", "tasks_file": ".specs/features/visual-refresh/tasks/01-x.md",
       "checklist": ".specs/features/visual-refresh/checks/01-x.md"}
gh_md = f.demo_markdown(rec, m, lambda n, raw=True: f"https://x/{n}" + ("?raw=true" if raw else ""), "github", "Videos: L")
li_md = f.demo_markdown(rec, m, lambda n, raw=True: f"https://u/{n}", "linear")
check("github: before/after table with 3 columns", "| 🎨 Design | ⬅️ Before | ✨ After |" in gh_md)
check("github: new screen marked", "_new screen_" in gh_md)
check("github: gif inline + mp4 link", "![Filter by cleaner](https://x/filter.gif?raw=true)" in gh_md and "(https://x/filter.mp4)" in gh_md)
check("linear: mp4 embedded, no gif", "![Filter by cleaner](https://u/filter.mp4)" in li_md and "filter.gif" not in li_md)
check("linear: images in order", li_md.index("Design:") < li_md.index("Before:") < li_md.index("After:"))
check("gaps listed", "- No error state in seed data" in li_md)

# ── evidence push (plumbing) ──
commit, prefix = f.push_evidence(rec, f.demo_files(m), demo)
listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit], cwd=bare, capture_output=True, text=True).stdout.split()
check("evidence branch has the files", f"{prefix}/filter.mp4" in listed and len(listed) == 8, listed)
vj = subprocess.run(["git", "show", f"{commit}:vercel.json"], cwd=bare, capture_output=True, text=True).stdout
check("evidence branch turns Vercel deployments off", json.loads(vj) == {"git": {"deploymentEnabled": False}}, vj)
check("clone work tree untouched", subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True,
                                                   text=True).stdout == "")
rec2 = dict(rec, identifier="HES-2")
commit2, _ = f.push_evidence(rec2, ["filter.mp4"], demo)
listed2 = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit2], cwd=bare, capture_output=True, text=True).stdout.split()
check("second push appends, keeps the first", len(listed2) == 9 and "HES-2/abcdef123456/filter.mp4" in listed2)
main_files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=bare, capture_output=True, text=True).stdout.split()
check("main untouched", main_files == ["README.md"], main_files)

# ── Linear upload (PUT to a local server) ──
received = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        received["headers"] = {k.lower(): v for k, v in self.headers.items()}
        received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.end_headers()

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
f.linear = lambda q, **v: {"fileUpload": {"success": True, "uploadFile": {
    "uploadUrl": f"http://127.0.0.1:{srv.server_port}/up", "assetUrl": f"https://uploads.linear.app/{v['f']}",
    "headers": [{"key": "x-goog-meta-test", "value": "1"}]}}}
url = f.linear_upload(demo / "filter.mp4")
check("upload returns asset URL", url == "https://uploads.linear.app/filter.mp4")
check("PUT carries returned headers + content type", received["headers"].get("x-goog-meta-test") == "1"
      and received["headers"].get("content-type") == "video/mp4" and len(received["body"]) == 2048, received["headers"])

# ── stage flow: final -> demo -> final -> review ──
calls = []
f.notify = lambda r, t, human=False: calls.append(("notify", t))
f.comment = lambda r, b: calls.append(("comment", b)) or "https://linear.app/c/1"
f.issue_detail = lambda i: {"identifier": "HES-1", "title": "t", "url": "u", "description": "d", "comments": {"nodes": []}}
f.start_station = lambda r, st, prompt, **k: (r.update(stage=st, conversation_id="conv"), calls.append(("start", st, prompt)))
f.set_status = lambda r, k: calls.append(("status", k))
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip()
f.require_clean_and_pushed = lambda r: head
f.base_is_merged = lambda r: True
f.gh_json = lambda *a, **k: {"mergeable": "MERGEABLE", "headRefOid": head}
f.pr_checks = lambda r, h: {"pass": ["PR checks"], "fail": [], "pending": []}
real_run = f.run
f.run = lambda args, **k: calls.append(("run", args)) if args[:2] == ["gh", "pr"] else real_run(args, **k)
state = {"issues": {"HES-1": dict(rec, stage="final", last_review="https://r", round=1)}}
f._STATE["issues"] = state["issues"]
r = state["issues"]["HES-1"]
f.check_final(r)
started = [c for c in calls if c[0] == "start"]
check("APPROVE + mergeable starts the demo", r["stage"] == "demo" and started and started[0][1] == "demo")
prompt = started[0][2]
check("prompt filled", "{{" not in prompt and head in prompt and "host.docker.internal:54321" in prompt
      and "FACTORY_RESULT" in prompt, [l for l in prompt.splitlines() if "{{" in l][:3])
check("PR not marked ready yet", not any(c[0] == "run" and "ready" in c[1] for c in calls))
# the agent writes the manifest into the demo dir of this head
ddir = Path(r["demo_dir"])
for n in f.demo_files(m):
    (ddir / n).write_bytes((demo / n).read_bytes())
(ddir / "manifest.json").write_text(json.dumps(manifest))
f.publish_linear = lambda rr, mm, dd: "https://linear.app/c/demo"
f.publish_github = lambda rr, mm, dd, lu: "https://github.com/o/r/pull/7#issuecomment-1"
f.after_demo(r, {"status": "done"})
check("after the demo: review, PR ready", r["stage"] == "review" and any(c[0] == "run" and "ready" in c[1] for c in calls))
final = [c for c in calls if c[0] == "notify"][-1][1]
check("final message links the demo", "https://linear.app/c/demo" in final, final)
check("demo recorded for the head", r["demo_head"] == head and r["demos"][-1]["linear"] == "https://linear.app/c/demo")

# ── request for an issue already in review ──
calls.clear()
r.pop("demo_head")
f.REQUESTS.mkdir(parents=True, exist_ok=True)
(f.REQUESTS / "demo-HES-1").touch()
(f.REQUESTS / "demo-HES-9").touch()
f.take_requests(state)
check("request starts a demo that returns to review", r["stage"] == "demo" and r["demo_back"] == "review")
check("request files consumed", not list(f.REQUESTS.iterdir()), list(f.REQUESTS.iterdir()))
f.after_demo(r, {"status": "done"})
check("back in review without touching status", r["stage"] == "review" and not any(c[0] == "status" for c in calls))

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
