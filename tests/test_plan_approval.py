"""Build waits for the person to approve the plan, and a reply that is not an approval goes back to Plan.

  python3 -m unittest tests.test_plan_approval
"""
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_engine, project, sh

PLAN = ".specs/features/alpha/plan.md"
TEMPLATE = ("# Alpha\n\n**Approved**: <written when the user approves - YYYY-MM-DD - user: \"their words\">\n\n"
            "## Problem\n\nThe list has no filter.\n")


class PlanApprovalCase(unittest.TestCase):

    def setUp(self):
        project("plan-approval-")
        self.f = f = load_engine()
        self.notes, self.comments, self.builds, self.sent, self.prompts = [], [], [], [], []
        f.notify = lambda rec, text, human=False, broadcast=False: self.notes.append((text, human))
        f.comment = lambda rec, body: self.comments.append(body) or ""
        f.start_build = lambda rec, handoff=False: self.builds.append(rec["identifier"])
        f.send_to_conversation = lambda rec, text: self.sent.append(text)
        f.require_tracked = lambda rec, path: None
        f.open_questions = lambda path: ([], [])
        f.linear_replies = lambda rec: []
        f.issue_detail = lambda i: {"identifier": "ENG-1", "title": "t", "url": "u", "description": "d",
                                    "comments": {"nodes": []}}
        f.start_station = lambda rec, station, prompt, **k: self.prompts.append(prompt)
        self.worktree = Path(tempfile.mkdtemp(prefix="plan-wt-"))
        sh("git", "init", "-q", "-b", "feat/alpha", cwd=self.worktree)
        (self.worktree / PLAN).parent.mkdir(parents=True)
        (self.worktree / PLAN).write_text(TEMPLATE)
        sh("git", "add", ".", cwd=self.worktree)
        sh("git", "commit", "-qm", "docs(plan): alpha", cwd=self.worktree)
        self.rec = {"identifier": "ENG-1", "issue_id": "i", "feature": "alpha", "worktree": str(self.worktree),
                    "branch": "feat/alpha", "base": "main", "issue_url": "u", "stage": "plan", "conversation_id": "conv"}

    def finish_plan(self, summary="- Slice 1: the list gets a filter"):
        self.f.after_plan(self.rec, {"status": "done", "summary": summary})


class PlanApprovalTest(PlanApprovalCase):

    def test_plan_prompt_asks_for_the_summary_and_names_the_plan(self):
        self.f.start_plan(self.rec)
        self.assertIn('"summary"', self.prompts[0])
        self.assertIn("written in Brazilian Portuguese", self.prompts[0])
        self.assertIn(f"Write one plan, at `{PLAN}`", self.prompts[0])

    def test_finished_plan_waits_with_the_summary(self):
        self.finish_plan()
        self.assertEqual(self.rec["stage"], "approval")
        self.assertEqual(self.builds, [])
        text, human = self.notes[-1]
        self.assertIn("Slice 1: the list gets a filter", text)
        self.assertIn(f"`{PLAN}`", text)
        self.assertIn("aprovo", text)
        self.assertTrue(human)
        self.assertIn("Plan ready for your approval", self.comments[-1])

    def test_blocking_question_stops_before_approval(self):
        self.f.open_questions = lambda path: (["which statuses count as overdue?"], [])
        with self.assertRaises(self.f.Stop) as stop:
            self.finish_plan()
        self.assertIn("which statuses count as overdue?", str(stop.exception))
        self.assertEqual(self.notes, [])

    def test_approval_reply_starts_build(self):
        self.finish_plan()
        for word in ("aprovo", "Entendi!", "<@U123> ok", "approve", "👍"):
            with self.subTest(word=word):
                self.builds.clear()
                self.rec.update(stage="approval", inbox=[{"source": "Slack", "text": word}])
                self.f.handle_approval(self.rec)
                self.assertEqual(self.builds, ["ENG-1"])

    def test_approval_commits_the_persons_words_in_the_plan(self):
        self.finish_plan()
        self.rec["inbox"] = [{"source": "Slack", "text": "<@U123> aprovo"}]
        self.f.handle_approval(self.rec)
        plan = (self.worktree / PLAN).read_text()
        self.assertRegex(plan, r'\n\*\*Approved\*\*: \d{4}-\d{2}-\d{2} - user: "aprovo"\n')
        self.assertNotIn("<written when", plan)
        self.assertTrue(plan.startswith("# Alpha\n"))
        self.assertEqual(sh("git", "log", "-1", "--format=%s", cwd=self.worktree),
                         "docs(plan): record the approval of ENG-1")
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.worktree), "")

    def test_same_approval_twice_commits_once(self):
        self.finish_plan()
        for _ in range(2):
            self.rec.update(stage="approval", inbox=[{"source": "Slack", "text": "aprovo"}])
            self.f.handle_approval(self.rec)
        self.assertEqual(sh("git", "rev-list", "--count", "HEAD", cwd=self.worktree), "2")

    def test_other_reply_goes_back_to_plan(self):
        self.finish_plan()
        self.rec["inbox"] = [{"source": "Slack", "text": "ok, mas deixe o filtro de data de fora"}]
        self.f.handle_approval(self.rec)
        self.assertEqual(self.builds, [])
        self.assertEqual(self.rec["stage"], "plan")
        self.assertIn("deixe o filtro de data de fora", self.sent[-1])
        self.assertIn("new summary", self.sent[-1])
        self.assertIn("<written when", (self.worktree / PLAN).read_text())

    def test_no_reply_keeps_waiting(self):
        self.finish_plan()
        self.f.handle_approval(self.rec)
        self.assertEqual((self.rec["stage"], self.builds, self.sent), ("approval", [], []))

    def test_approval_stage_holds_no_slot(self):
        self.assertIn("approval", self.f.IDLE_STAGES)


class OpenQuestionsTest(unittest.TestCase):

    def setUp(self):
        project("open-questions-")
        self.f = load_engine()

    def questions(self, body):
        path = Path(tempfile.mkdtemp(prefix="plan-")) / "plan.md"
        path.write_text("# Alpha\n\n## Assumptions\n\n" + body + "\n## Sources\n\n- HES-1\n")
        return self.f.open_questions(path)

    def test_none_has_no_questions(self):
        self.assertEqual(self.questions("**Open questions:** none - all resolved or logged above.\n"), ([], []))

    def test_unmarked_question_blocks(self):
        self.assertEqual(self.questions("**Open questions:** what happens on a chargeback?\n"),
                         (["what happens on a chargeback?"], []))

    def test_marked_list(self):
        blocks, go_live = self.questions(
            "**Open questions:**\n\n"
            "- `blocks` - which statuses count as overdue?\n"
            "- `blocks go-live` - who signs off the copy?\n"
            "- `open` - should the filter remember its value?\n"
            "  It can wait for a later slice.\n")
        self.assertEqual(blocks, ["`blocks` - which statuses count as overdue?"])
        self.assertEqual(go_live, ["`blocks go-live` - who signs off the copy?"])

    def test_leading_word_marks(self):
        blocks, go_live = self.questions("**Open questions:**\n\n1. open: does the list paginate?\n"
                                         "2. blocks go-live: legal review of the export\n")
        self.assertEqual((blocks, go_live), ([], ["blocks go-live: legal review of the export"]))

    def test_open_in_the_prose_does_not_mark(self):
        blocks, _ = self.questions("**Open questions:**\n\n- Open the sheet first, or the list?\n")
        self.assertEqual(blocks, ["Open the sheet first, or the list?"])

    def test_plan_without_the_line(self):
        self.assertEqual(self.questions(""), ([], []))


class PlanFolderTest(unittest.TestCase):

    def setUp(self):
        project("plan-folder-")
        self.f = load_engine()
        self.on_base = set()
        self.f.on_base = lambda rec, path: path in self.on_base
        self.rec = {"identifier": "ENG-7", "feature": "visual-refresh", "base": "main"}

    def test_first_issue_plans_in_the_feature_folder(self):
        self.assertEqual(self.f.plan_folder(self.rec, set()), "visual-refresh")

    def test_folder_with_a_plan_on_the_base(self):
        self.on_base.add(".specs/features/visual-refresh/plan.md")
        self.assertEqual(self.f.plan_folder(self.rec, set()), "visual-refresh-eng-7")

    def test_folder_another_open_issue_plans_in(self):
        self.assertEqual(self.f.plan_folder(self.rec, {"visual-refresh", None}), "visual-refresh-eng-7")

    def test_issue_folder_reads_the_feature_discovery(self):
        self.rec.update(plan_feature="visual-refresh-eng-7", issue_url="u", worktree="/w", branch="b")
        p = self.f.paths(self.rec)
        self.assertEqual(p["plan"], ".specs/features/visual-refresh-eng-7/plan.md")
        self.assertEqual(p["discovery"], ".specs/features/visual-refresh/discovery.md")


if __name__ == "__main__":
    unittest.main()
