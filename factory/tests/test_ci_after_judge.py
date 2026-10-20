#!/usr/bin/env python3
"""Offline tests: CI is checked only after the-judge approves, never between Build/Fix and Verify.

  python3 tools/agent-factory/factory/tests/test_ci_after_judge.py tools/agent-factory/factory
"""
import importlib.util, os, sys, tempfile
from pathlib import Path

FACTORY = Path(sys.argv[1]).resolve()
os.environ["FACTORY_STATE"] = os.path.join(tempfile.mkdtemp(prefix="ci-test-"), "state.json")
os.environ.setdefault("LINEAR_API_KEY", "x")
sys.path.insert(0, str(FACTORY))
spec = importlib.util.spec_from_file_location("factory", FACTORY / "factory.py")
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)

calls = []
f.notify = lambda rec, text, human=False, broadcast=False: calls.append(("notify", text))
f.comment = lambda rec, body: calls.append(("comment", body))
f.set_status = lambda rec, key: calls.append(("status", key))
f.run = lambda args, **kw: calls.append(("run", " ".join(args)))
f.mutate = lambda what: True
f.require_clean_and_pushed = lambda rec: "HEAD2"
f.base_is_merged = lambda rec: True
f.sync_with_base = lambda rec: "HEAD2"
f.start_verify = lambda rec, head: (rec.update(stage="verify"), calls.append(("verify", head)))
f.start_demo = lambda rec, head, back: (rec.update(stage="demo"), calls.append(("demo", head)))
f.start_fix = lambda rec, reason, head, why: (rec.update(stage="fix"), calls.append(("fix", reason)))
f.pr_checks = lambda rec, head: calls.append(("ci", head)) or CI
f.gh_json = lambda *a, **kw: {"mergeable": "MERGEABLE", "headRefOid": "HEAD2"}
f.DEMO = {"enabled": True}
f.demo_busy = lambda: False
CI = {"pass": [], "fail": [], "pending": ["PR checks"]}
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {detail}" if detail else ""))
    fails += 0 if cond else 1


def rec(**extra):
    return {"identifier": "HES-1", "pr_number": 7, "pr_url": "u", "worktree": "/tmp", "branch": "b", "base": "main",
            "round": 1, "fix_from": "HEAD1", **extra}


def ci_calls():
    return [c for c in calls if c[0] == "ci"]


# 1. a Fix goes straight to Verify, with CI pending, and never reads CI
calls.clear(); r = rec(stage="fix")
f.after_fix(r, {"status": "done"})
check("fix goes straight to verify", r["stage"] == "verify" and ("verify", "HEAD2") in calls, calls)
check("fix does not read CI", not ci_calls(), calls)

# 2. the sync after Build goes straight to Verify
calls.clear(); r = rec(stage="build")
f.sync_then_verify(r, "HEAD1")
check("build sync goes straight to verify", r["stage"] == "verify" and not ci_calls(), calls)

# 3. a conflict still opens a Fix
calls.clear(); r = rec(stage="build")
def conflict(rec): raise f.Conflict(["a.ts"])
f.sync_with_base, keep = conflict, f.sync_with_base
f.sync_then_verify(r, "HEAD1")
f.sync_with_base = keep
check("conflict opens a fix", r["stage"] == "fix" and not ci_calls(), calls)

# 4. a record saved in the old `ci` stage moves on to Verify without reading CI
calls.clear(); r = rec(stage="ci", ci_head="HEAD2", ci_then="verify", ci_since="2000-01-01T00:00:00+00:00")
f.deliver_replies = lambda rec: None
f.advance(r)
check("legacy ci record goes to verify", r["stage"] == "verify" and not ci_calls(), calls)

# 5. APPROVE checks CI on the approved head; pending CI waits in final, no demo yet
calls.clear(); r = rec(stage="judge")
f.judge_verdict = lambda rec, head: ("APPROVE", "https://review")
f.after_judge(r, {"status": "done"})
check("approve checks CI on the head", ci_calls() == [("ci", "HEAD2")], calls)
check("pending CI waits before the demo", r["stage"] == "final" and not any(c[0] == "demo" for c in calls), r["stage"])
check("approve message says CI comes next", any("CI do commit aprovado" in c[1] for c in calls if c[0] == "notify"))

# 6. green CI starts the demo
calls.clear(); CI = {"pass": ["PR checks"], "fail": [], "pending": []}
f.check_final(r)
check("green CI starts the demo", ("demo", "HEAD2") in calls, calls)

# 7. red CI after APPROVE opens a Fix, and that Fix loops back through Verify
calls.clear(); r = rec(stage="final"); CI = {"pass": [], "fail": ["PR checks (failure)"], "pending": []}
f.check_final(r)
check("red CI after approve opens a fix", r["stage"] == "fix" and any("CI failing" in c[1] for c in calls if c[0] == "fix"), calls)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
