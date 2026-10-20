#!/usr/bin/env python3
"""Offline tests for the final CI check and for change requests on an issue in review.

  python3 tools/agent-factory/factory/tests/test_review_requests.py tools/agent-factory/factory

Every call that reaches Linear, GitHub or Slack is replaced; nothing leaves the machine.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

FACTORY = Path(sys.argv[1]).resolve()
os.environ["FACTORY_STATE"] = os.path.join(tempfile.mkdtemp(prefix="review-test-"), "state.json")
os.environ.setdefault("LINEAR_API_KEY", "x")
sys.path.insert(0, str(FACTORY))
spec = importlib.util.spec_from_file_location("factory", FACTORY / "factory.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
calls = []
f.notify = lambda rec, text, human=False: calls.append(("notify", text))
f.comment = lambda rec, body: calls.append(("comment", body))
f.set_status = lambda rec, key: calls.append(("status", key))
f.run = lambda args, **kw: calls.append(("run", " ".join(args)))
f.require_clean_and_pushed = lambda rec: "HEAD1"
f.base_is_merged = lambda rec: True
f.start_demo = lambda rec, head, back: calls.append(("demo", head))
f.mutate = lambda what: True
f.DEMO = {}


def fake_fix(rec, reason, head, why):
    rec["round"] = rec.get("round", 0) + 1
    rec["stage"] = "fix"
    calls.append(("fix", reason, why))


f.start_fix = fake_fix


def rec(**extra):
    r = {"identifier": "HES-1", "pr_number": 7, "pr_url": "u", "worktree": "/tmp", "last_review": "r",
         "stage": "final", "round": 2, "base": "b"}
    r.update(extra)
    return r


def gh(state="OPEN", mergeable="MERGEABLE"):
    return lambda *a, **kw: {"mergeable": mergeable, "headRefOid": "HEAD1", "state": state}


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


# 1. red CI on the head opens a Fix and does not reach review
calls.clear(); f.gh_json = gh()
f.pr_checks = lambda r, h: {"pass": ["x"], "fail": ["PR checks (failure)"], "pending": []}
r = rec(); f.check_final(r)
check("red CI starts a Fix", r["stage"] == "fix" and any(c[0] == "fix" and "CI failing" in c[1] for c in calls))
check("red CI does not mark ready", not any(c[0] == "run" and "pr ready" in c[1] for c in calls))

# 2. pending CI waits
calls.clear(); f.pr_checks = lambda r, h: {"pass": [], "fail": [], "pending": ["PR checks"]}
r = rec(); f.check_final(r)
check("pending CI waits in final", r["stage"] == "final" and not calls)

# 3. no checks waits, then stops after the timeout
calls.clear(); f.pr_checks = lambda r, h: {"pass": [], "fail": [], "pending": []}
r = rec(); f.check_final(r)
check("no checks waits first", r["stage"] == "final" and "final_ci_since" in r)
r["final_ci_since"] = "2000-01-01T00:00:00+00:00"
try:
    f.check_final(r); check("no checks stops after timeout", False)
except f.Stop:
    check("no checks stops after timeout", True)

# 4. green CI reaches review
calls.clear(); f.pr_checks = lambda r, h: {"pass": ["PR checks"], "fail": [], "pending": []}
r = rec(); f.check_final(r)
check("green CI reaches review", r["stage"] == "review" and ("status", "review") in calls)

# 5. a Slack request in review reopens with a Fix round
calls.clear(); f.gh_json = gh()
r = rec(stage="review", round=3, rounds_exhausted=True, review_checks=2,
        inbox=[{"source": "Slack", "text": "<@U0C9X0X5F19> tente resolver essa issue"},
               {"source": "Slack", "text": "<@U0C9X0X5F19> responda com ok"}])
f.reopen_from_review(r)
fix = next(c for c in calls if c[0] == "fix")
check("review request starts a Fix", r["stage"] == "fix" and r["round"] == 1 and "inbox" not in r)
check("PR goes back to draft", ("run", "gh pr ready 7 --undo") in calls)
check("issue goes back to In Progress", ("status", "progress") in calls)
check("request text reaches the Fix without the mention",
      "tente resolver essa issue" in fix[1] and "responda com ok" in fix[1] and "<@" not in fix[1])
check("counters reset", "rounds_exhausted" not in r and "review_checks" not in r)

# 6. a merged PR is not reopened
calls.clear(); f.gh_json = gh(state="MERGED")
r = rec(stage="review", inbox=[{"source": "Slack", "text": "mude isso"}])
f.reopen_from_review(r)
check("merged PR is not reopened", r["stage"] == "review" and not any(c[0] == "fix" for c in calls)
      and any("Não reabro" in c[1] for c in calls if c[0] == "notify"))
print("all passed")
