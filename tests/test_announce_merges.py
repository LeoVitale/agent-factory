#!/usr/bin/env python3
"""A merged pull request leaves the factory from any station, not only from review.

  python3 tests/test_announce_merges.py

Every call that reaches Linear, GitHub or Slack is replaced; nothing leaves the machine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import load_engine, project  # noqa: E402

project("merge-test-")
f = load_engine()
calls = []
f.notify = lambda rec, text, human=False, broadcast=False: calls.append(("notify", text, broadcast))
f.set_needs_human = lambda rec, on: calls.append(("needs_human", on))
f.mutate = lambda what: True


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


def issue(identifier, stage, number, **extra):
    rec = {"identifier": identifier, "stage": stage, "pr_number": number,
           "pr_url": f"https://github.com/example/pull/{number}", "base": "main"}
    rec.update(extra)
    return rec


def merged_pr(number, when="2026-10-24T21:30:15Z", base="chore/tech-debt-audit", login="LeoVitale"):
    return {"number": number, "mergedAt": when, "baseRefName": base,
            "mergedBy": {"login": login} if login else None}


def run(issues, prs, since="2026-10-21T00:00:00+00:00"):
    calls.clear()
    state = {"issues": issues, "merge_notices_since": since}
    f.gh_json = lambda *a, **kw: prs
    f.announce_merges(state)
    return state


waiting = issue("HES-45", "waiting", 106, waiting_stage="demo", question="The demo has no video.",
                inbox=[{"source": "Slack", "text": "pr merged"}])
review = issue("HES-1", "review", 7)
already = issue("HES-2", "merged", 8)
state = run({"HES-45": waiting, "HES-1": review, "HES-2": already},
            [merged_pr(106), merged_pr(7, when="2026-10-24T12:00:00Z", base="main", login=None)])
check("a waiting issue leaves when its PR merges", waiting["stage"] == "merged" and waiting["merged_at"] == "2026-10-24T21:30:15Z")
check("the stop is cleared", "waiting_stage" not in waiting and "question" not in waiting and "inbox" not in waiting)
check("needs-human comes off a waiting issue", ("needs_human", False) in calls)
check("review still hears about its merge", review["stage"] == "merged" and any(c[0] == "notify" and "PR #7" in c[1] for c in calls))
check("an issue already merged is left alone", already.get("merged_at") is None and not any("PR #8" in c[1] for c in calls if c[0] == "notify"))
note = next(c[1] for c in calls if c[0] == "notify" and "PR #106" in c[1])
check("the thread names the base and who merged", "chore/tech-debt-audit" in note and "LeoVitale" in note and calls[calls.index(("notify", note, True))][2])

quiet = issue("HES-3", "demo", 9)
run({"HES-3": quiet}, [merged_pr(9)], since="2026-10-25T00:00:00+00:00")
check("a merge from before notices starts is marked without a message", quiet["stage"] == "merged" and not calls)

stays = issue("HES-4", "build", 10)
run({"HES-4": stays}, [merged_pr(106)])
check("an open pull request stays in its station", stays["stage"] == "build" and not calls)
print("all passed")
