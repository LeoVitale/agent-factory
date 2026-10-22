#!/usr/bin/env python3
"""A tech-debt record names the feature folder when the issue does not.

  python3 tools/agent-factory/factory/tests/test_debt_feature.py tools/agent-factory/factory
"""
import json
import os
import sys
import tempfile
from pathlib import Path

FACTORY = Path(sys.argv[1]).resolve()
ROOT = Path(tempfile.mkdtemp(prefix="debt-feature-"))
cfg = json.loads((FACTORY / "config.json").read_text())
(ROOT / "config.json").write_text(json.dumps(cfg))
os.environ.update(FACTORY_CONFIG=str(ROOT / "config.json"), FACTORY_STATE=str(ROOT / "state" / "state.json"), LINEAR_API_KEY="x")
sys.path.insert(0, str(FACTORY))
import factory as f  # noqa: E402

fails = 0
SOURCE = "docs/tech-debt/TD-WEB-01-erros-de-carregamento-e-mutacao-sao-engolidos-nos-dois-apps.md"
OTHER = "docs/tech-debt/TD-ADM-01-refresh-automatico-de-token-do-supabase-derruba-a-arvore-de.md"


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {detail}" if detail else ""))
    fails += 0 if cond else 1


def issue(description, identifier="HES-15"):
    return {"id": "u", "identifier": identifier, "title": "t", "url": "u",
            "project": {"name": "Pre-release Improvements"}, "description": description}


one = f"Base: `chore/tech-debt-audit`\n\nRacional em `{SOURCE}`.\n"
fields = f.issue_fields(one)
check("one record names td-web-01", fields["feature"] == "td-web-01" and fields["debt_source"] == SOURCE, fields)
check("base still comes from the line", fields["base"] == "chore/tech-debt-audit")

explicit = f.issue_fields(one + "\n.specs/features/sentry-errors/\n")
check("an explicit folder wins", explicit["feature"] == "sentry-errors" and explicit["debt_source"] is None, explicit)

two = f.issue_fields(one + f"\nalso `{OTHER}`\n")
check("two records leave the feature empty", two["feature"] is None and two["debt_source"] is None, two)

same = f.issue_fields(one + one)
check("the same path twice is one record", same["feature"] == "td-web-01" and same["debt_source"] == SOURCE)

none = f.issue_fields("Base: `chore/tech-debt-audit`\n")
check("no record and no folder stays empty", none["feature"] is None and none["debt_source"] is None)

rec = f.new_record(issue(one))
check("the branch uses the derived folder", rec["branch"] == "feat/td-web-01-hes-15" and rec["base"] == "chore/tech-debt-audit", rec)
check("the record keeps the source path", rec.get("debt_source") == SOURCE)

named = f.new_record(issue(one + "\n.specs/features/sentry-errors/\n"))
check("a named folder does not carry a debt source", "debt_source" not in named and named["feature"] == "sentry-errors", named)

note = f.debt_note(rec)
prompt = f.render("plan.md", debt_note=note, identifier="HES-15", worktree="/w", branch=rec["branch"],
                  base=rec["base"], feature=rec["feature"], issue="ISSUE", result="RESULT")
check("plan is told to copy the record", SOURCE in prompt and f".specs/features/td-web-01/discovery.md" in prompt)
check("plan is told to leave the original", "Leave `" + SOURCE + "` in `docs/tech-debt/`." in prompt)
check("a named feature gets no copy note", f.debt_note(named) == "" and "{{debt_note}}" not in f.render(
    "plan.md", debt_note="", identifier="HES-15", worktree="/w", branch="feat/sentry-errors-hes-15",
    base="main", feature="sentry-errors", issue="ISSUE", result="RESULT"))

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
