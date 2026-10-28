#!/usr/bin/env python3
"""Offline tests for what people read: the Slack thread root, station updates, stops, and Linear comments.

  python3 tests/test_messages.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import TEMPLATES, load_engine, project, sh  # noqa: E402

ROOT, CLONE = project("messages-test-", instance={"agent_profile_id": "default-profile", "station_profiles": {"demo": "demo-profile"}})
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


def rec_at(stage, **extra):
    return {"identifier": "HES-1", "title": "Rebuild the list", "issue_url": "https://linear.app/x/HES-1",
            "issue_id": "i1", "stage": stage, "round": 0, "worktree": str(ROOT), "branch": "feat/x-hes-1",
            "base": "main", "feature": "x", "conversations": [{"station": "plan"}], **extra}


# ── the progress line of the thread root ──
line = f.pipeline(rec_at("queued", conversations=[]))
check("queued: every step still grey", line.count("⚪") == 8 and "*" not in line, line)
line = f.pipeline(rec_at("build", conversations=[]))
check("skipped plan shows as skipped", line.startswith("⏭️ Plan › ⏭️ Approval › 🔨 *Build* › ⚪ Verify"), line)
line = f.pipeline(rec_at("verify"))
check("verify: done steps ticked, current bold",
      line == "✅ Plan › ✅ Approval › ✅ Build › 🔍 *Verify* › ⚪ Judge › ⚪ CI › ⚪ Demo › ⚪ Review", line)
line = f.pipeline(rec_at("fix", round=2))
check("fix round: the loop restarts after Fix", "✅ Build › 🔧 *Fix 2/3* › ⚪ Verify" in line, line)
line = f.pipeline(rec_at("verify", round=1))
check("after a fix: Fix ticked", "✅ Fix 1/3 › 🔍 *Verify*" in line, line)
line = f.pipeline(rec_at("waiting", waiting_stage="judge"))
check("waiting: the stopped step carries the hand", "✅ Verify › 🙋 *Judge* › ⚪ CI › ⚪ Demo" in line, line)
line = f.pipeline(rec_at("final"))
check("approved, demo pending", "✅ Judge › ⏳ CI › ⚪ Demo › ⚪ Review" in line, line)
line = f.pipeline(rec_at("review"))
check("review: all ticked", line.count("✅") == 7 and line.endswith("👀 *Review*"), line)

# ── the thread root ──
root = f.root_text(rec_at("verify", round=1, title="Fix <b> & co", pr_url="https://github.com/o/r/pull/7", pr_number=7))
lines = root.splitlines()
check("root: linked title, escaped", lines[0] == "*<https://linear.app/x/HES-1|HES-1 · Fix &lt;b&gt; &amp; co>*", lines[0])
check("root: PR and rounds", lines[2] == "🔗 <https://github.com/o/r/pull/7|PR #7> · 🔧 Correções: 1 de 3", lines[2])
root = f.root_text(rec_at("waiting", waiting_stage="verify"))
check("root: who it waits for", root.splitlines()[2].startswith("🙋 Esperando você no 🔍 Verify · 🔗 Sem PR ainda"),
      root.splitlines()[2])

# ── station updates ──
msgs, posts, linear_calls = [], [], []
f.notify = lambda r, t, human=False, broadcast=False: msgs.append((t, human))
f.canvas = lambda method, path, body=None: posts.append(body) or {"id": f"c{len(posts)}"}
f.profile_secrets = lambda profile: {}
f.linear = lambda q, **v: linear_calls.append(v) or {"commentCreate": {"comment": {"url": "https://linear.app/c/1"}}}
f.set_needs_human = lambda r, on: None

f.start_station(rec_at("ci"), "verify", "go")
check("station start: name, what it does, live link",
      msgs[-1][0].startswith("🔍 *Verify começou*: um agente que não escreveu o código")
      and msgs[-1][0].endswith("|👀 Acompanhar ao vivo>"), msgs[-1][0])

msgs.clear()
r = rec_at("judge")
f.start_fix(r, "the-judge returned COMMENT. Address the review at https://r.", "abc", why="responde à revisão.")
check("fix round: one message, with the round and why", len(msgs) == 1
      and msgs[0][0].startswith("🔧 *Correção 1 de 3*: responde à revisão.\n"), msgs)
check("fix prompt still gets the instruction", "Address the review at https://r." in posts[-1]["initial_message"]
      ["content"][0]["text"])

msgs.clear()
f.issue_detail = lambda i: {"identifier": "HES-1", "title": "t", "url": "u", "description": "d", "comments": {"nodes": []}}
f.start_build(rec_at("build"), handoff=True)
check("handoff: one message", len(msgs) == 1 and msgs[0][0].startswith("🔨 *Build continua*: "), msgs)

# ── stops ──
msgs.clear()
linear_calls.clear()
r = rec_at("verify")
f.stop_for_human(r, "Seed has no overdue job.\nAdd one to `<seed>`?")
text, human = msgs[-1]
check("stop: mentions the person, names the station", human and text.startswith("🙋 *Preciso de você* no 🔍 Verify\n"), text)
check("stop: question quoted line by line and escaped",
      "> Seed has no overdue job.\n> Add one to `&lt;seed&gt;`?" in text, text)
body = linear_calls[-1]["b"]
check("stop: Linear comment keeps the [factory] mark first", body.startswith("[factory] 🙋 **Needs you at 🔍 Verify**"), body)
check("stop: Linear quote is not escaped", "> Add one to `<seed>`?" in body, body)
check("stop: root shows the hand", "🙋 *Verify*" in f.root_text(r))

msgs.clear()
f.refuse(rec_at("queued"), "the description must name `.specs/features/<feature>/`.",
         "a descrição precisa citar `.specs/features/<feature>/`.")
check("refuse: Portuguese in Slack, escaped",
      msgs[-1][0].startswith("⛔ *Não consigo começar*: a descrição precisa citar `.specs/features/&lt;feature&gt;/`."),
      msgs[-1][0])
check("refuse: English on Linear", linear_calls[-1]["b"].startswith("[factory] ⛔ **Can't start yet:** the description"),
      linear_calls[-1]["b"])

# ── replies are still told apart from the factory's own comments ──
f.team = lambda: {"viewer": "me"}
f.issue_detail = lambda i: {"comments": {"nodes": [
    {"id": "1", "createdAt": "2030", "user": {"id": "me"}, "body": "[factory] 📨 Answer from the person (via Slack): yes"},
    {"id": "2", "createdAt": "2030", "user": {"id": "me"}, "body": "Yes, add the seed."}]}}
got = f.linear_replies(rec_at("waiting", waiting_since="2029"))
check("factory comments are not replies", [g["text"] for g in got] == ["Yes, add the seed."], got)

# ── a Demo stop that asks for a Fix round does not resume Demo ──
sent, started = [], []
f.send_to_conversation = lambda rec, text: sent.append(text)
f.require_clean_and_pushed = lambda rec: "abc123"
f.start_fix = lambda rec, reason, head, why: started.append((head, why, "that change is in scope" in reason))
r = rec_at("waiting", waiting_stage="demo", conversation_id="demo-1", waiting_since="2099",
           question="Should that round fix both comments? This needs a Fix round.",
           inbox=[{"source": "Linear", "text": "Yes. Both nits, and re-pin C6."}])
f.deliver_replies(r)
check("demo fix question starts Fix and leaves the demo conversation",
      started == [("abc123", "faz o que você confirmou para a rodada de Fix.", True)] and sent == []
      and "question" not in r, started)
r = rec_at("waiting", waiting_stage="demo", conversation_id="demo-1", conversations=[{"station": "demo"}],
           waiting_since="2099", question="The local stack is down.",
           inbox=[{"source": "Linear", "text": "It is up."}])
f.deliver_replies(r)
check("other demo answers resume Demo", sent and r["stage"] == "demo", r["stage"])
answer = (TEMPLATES / "prompts" / "answer.md").read_text()
check("answer prompt keeps the [factory] mark first", "`[factory] 📨 Answer from the person" in answer)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
