"""The engine takes every project value from `.factory/` at the instance's config_ref.

  python3 -m unittest tests.test_config
"""
import unittest
from pathlib import Path

from tests.helpers import load_engine, project, sh

SENTINEL = {
    "linear": {"team_key": "ZZQ", "states": {"ready": "S-ready", "progress": "S-progress", "review": "S-review"},
               "label": "lbl-go", "needs_human_label": "lbl-help"},
    "github_repo": "sentinel-owner/sentinel-repo",
    "base_branch": "sentinel-base",
    "artifacts": {"feature": "docs/work/([a-z0-9-]+)/", "folder": "docs/work/{feature}/",
                  "discovery": "docs/work/{feature}/brief.md",
                  "debt": {"pattern": "debt/(DBT-\\d+)-[a-z-]+\\.md", "folder": "lowercase"}},
    "schema_guard": {"paths": ["db/"], "command": "make sentinel-db-proof"},
    "ci": {"ignore": [{"context_prefix": "Sentinel", "description_contains": "quota"}]},
    "slack": {"channel_prefix": "sx-", "default_channel": "sx-default", "project_channels": {}},
    "limits": {"max_fix_rounds": 5, "max_base_syncs": 6, "max_active": 7},
    "demo": {"enabled": True, "vars": {"install": "x", "start_app": "x", "data_rules": "SENTINEL-DATA-RULES"}},
}


def stub_linear(f):
    calls = []
    f.linear = lambda query, **v: calls.append(v) or {"issues": {"nodes": []}, "issueUpdate": {"success": True}}
    f.team = lambda: {"viewer": "me", "states": {"S-progress": "st-p", "S-review": "st-r"}, "labels": {"lbl-help": "lb-h"}}
    return calls


class ProjectConfigTest(unittest.TestCase):

    def test_every_project_value_comes_from_config(self):
        project("cfg-sentinel-", config=SENTINEL)
        f = load_engine()
        f.notify = lambda *a, **k: notes.append(a[1])
        f.comment = lambda *a, **k: ""
        notes = []
        rec = {"identifier": "ZZQ-1", "issue_id": "i1", "feature": "alpha", "worktree": "/nowhere", "branch": "feat/a",
               "base": "sentinel-base", "issue_url": "u", "stage": "build", "round": 0}

        # team key, ready status, label
        calls = stub_linear(f)
        f.ready_issues()
        self.assertEqual((calls[0]["key"], calls[0]["state"], calls[0]["label"]), ("ZZQ", "S-ready", "lbl-go"))
        # progress and review statuses
        calls.clear()
        f.set_status(rec, "progress")
        f.set_status(rec, "review")
        self.assertEqual([c["s"] for c in calls], ["st-p", "st-r"])
        # needs-human label
        calls.clear()
        f.set_needs_human(rec, True)
        self.assertEqual(calls[0]["l"], ["lb-h"])
        # GitHub repository
        self.assertIn("github.com/sentinel-owner/sentinel-repo/blob/", f.blob("abc", "x.md"))
        # base branch
        issue = {"id": "i", "identifier": "ZZQ-2", "title": "t", "url": "u", "description": "see docs/work/alpha/"}
        self.assertEqual(f.new_record(issue)["base"], "sentinel-base")
        # feature pattern
        self.assertEqual(f.issue_fields("see docs/work/alpha/ for it")["feature"], "alpha")
        # feature folder, in the Plan line and in the station paths
        self.assertEqual(f.issue_fields("Plan: `docs/work/alpha/plan.md`")["plan"], "docs/work/alpha/plan.md")
        self.assertEqual(f.paths(rec)["plan"], "docs/work/alpha/plan.md")
        self.assertEqual(f.paths(rec)["checks"], "docs/work/alpha/checks.md")
        self.assertEqual(f.paths(rec)["verification"], "docs/work/alpha/verification.md")
        # discovery path
        self.assertEqual(f.paths(rec)["discovery"], "docs/work/alpha/brief.md")
        # tech-debt pattern and folder
        fields = f.issue_fields("fixes debt/DBT-12-slow-list.md")
        self.assertEqual((fields["feature"], fields["debt_source"]), ("dbt-12", "debt/DBT-12-slow-list.md"))
        # schema guard paths and command
        f.require_tracked = lambda r, p: None
        f.require_clean_and_pushed = lambda r: "head"
        f.gh_json = lambda *a, **k: [{"number": 3, "url": "https://pr/3"}]
        f.git = lambda *a, **k: "db/0001-init.sql"
        with self.assertRaises(f.Stop) as stop:
            f.after_build(dict(rec), {})
        self.assertIn("make sentinel-db-proof", str(stop.exception))
        # CI ignore rule
        f.gh_json = lambda *a, **k: ({"workflow_runs": []} if "actions/runs" in a[1] else
                                     {"statuses": [{"state": "failure", "context": "Sentinel CI",
                                                    "description": "Quota exceeded"}]})
        self.assertEqual(f.pr_checks(rec, "head")["pass"], ["Sentinel CI"])
        # Slack channel prefix and default channel
        made = []

        def slack(method, **p):
            if method == "conversations.create":
                made.append(p["name"])
                return {"channel": {"id": "C1"}}
            return {}
        f.slack = slack
        f.channel_for({"project": "My Proj"})
        f.channel_for({"project": None})
        self.assertEqual(made, ["sx-my-proj", "sx-default"])
        # limits: fix rounds
        f.start_station = lambda *a, **k: None
        with self.assertRaises(f.Stop) as stop:
            f.start_fix(dict(rec, round=5), "r", "head", "why")
        self.assertIn("after 5 fix rounds", str(stop.exception))
        # limits: base syncs
        f.base_is_merged = lambda r: False
        with self.assertRaises(f.Stop) as stop:
            f.check_final(dict(rec, base_syncs=6))
        self.assertIn("6 times", str(stop.exception))
        # limits: active issues
        notes.clear()
        state = {"issues": {f"A-{n}": {"stage": "build", "picked_at": str(n)} for n in range(7)}}
        state["issues"]["Q-1"] = dict(rec, identifier="Q-1", stage="queued", picked_at="9")
        f.fill_slots(state)
        self.assertTrue(any("Já há 7 issues" in n for n in notes), notes)
        # demo variables
        self.assertIn("SENTINEL-DATA-RULES", f.render("demo.md", **f.demo_vars()))

    def test_missing_config_has_no_fallback(self):
        project("cfg-missing-", kit=False)
        import settings
        with self.assertRaises(settings.ConfigError) as error:
            settings.load()
        self.assertIn(".factory/config.json", str(error.exception))

    def test_prompt_comes_from_config_ref(self):
        _, repo = project("cfg-ref-")
        f = load_engine()
        committed = (repo / ".factory/prompts/build.md").read_text()
        (repo / ".factory/prompts/build.md").write_text(committed + "\nUNCOMMITTED-MARK\n")
        rendered = f.render("build.md")
        self.assertNotIn("UNCOMMITTED-MARK", rendered)
        self.assertIn("You are the Build station", rendered)

    def test_new_commit_at_ref_reaches_next_station(self):
        _, repo = project("cfg-commit-")
        f = load_engine()
        prompts = []
        f.issue_detail = lambda i: {"identifier": "ENG-1", "title": "t", "url": "u", "description": "d",
                                    "comments": {"nodes": []}}
        f.start_station = lambda rec, station, prompt, **k: prompts.append(prompt)
        rec = {"identifier": "ENG-1", "issue_id": "i", "feature": "alpha", "worktree": "/w", "branch": "b",
               "base": "main", "issue_url": "u"}
        f.start_build(rec)
        path = repo / ".factory/prompts/build.md"
        path.write_text(path.read_text() + "\nNEW-COMMIT-MARK\n")
        sh("git", "commit", "-qam", "change the build prompt", cwd=repo)
        f.start_build(rec)
        self.assertNotIn("NEW-COMMIT-MARK", prompts[0])
        self.assertIn("NEW-COMMIT-MARK", prompts[1])

    def test_ci_ignore_rule_passes_matching_failure(self):
        project("cfg-ci-")
        f = load_engine()
        statuses = [{"state": "failure", "context": "Vercel – admin", "description": "Deployment rate limited"},
                    {"state": "failure", "context": "Vercel – web", "description": "Build failed"}]
        f.gh_json = lambda *a, **k: {"workflow_runs": []} if "actions/runs" in a[1] else {"statuses": statuses}
        checks = f.pr_checks({"worktree": "/w"}, "head")
        self.assertEqual(checks["pass"], ["Vercel – admin"])
        self.assertEqual(checks["fail"], ["Vercel – web"])


if __name__ == "__main__":
    unittest.main()
