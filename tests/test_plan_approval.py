"""Build waits for the person to approve the plan, and a reply that is not an approval goes back to Plan.

  python3 -m unittest tests.test_plan_approval
"""
import unittest

from tests.helpers import load_engine, project


class PlanApprovalCase(unittest.TestCase):
    config = {"plan_approval": True}

    def setUp(self):
        project("plan-approval-", config=self.config)
        self.f = f = load_engine()
        self.notes, self.comments, self.builds, self.sent, self.prompts = [], [], [], [], []
        f.notify = lambda rec, text, human=False, broadcast=False: self.notes.append((text, human))
        f.comment = lambda rec, body: self.comments.append(body) or ""
        f.start_build = lambda rec, handoff=False: self.builds.append(rec["identifier"])
        f.send_to_conversation = lambda rec, text: self.sent.append(text)
        f.require_tracked = lambda rec, path: None
        f.blocking_unresolved = lambda path: ([], [])
        f.linear_replies = lambda rec: []
        f.issue_detail = lambda i: {"identifier": "ENG-1", "title": "t", "url": "u", "description": "d",
                                    "comments": {"nodes": []}}
        f.start_station = lambda rec, station, prompt, **k: self.prompts.append(prompt)
        self.rec = {"identifier": "ENG-1", "issue_id": "i", "feature": "alpha", "worktree": "/w", "branch": "b",
                    "base": "main", "issue_url": "u", "stage": "plan", "conversation_id": "conv"}

    def finish_plan(self, summary="- Task 1: the list gets a filter"):
        self.f.after_plan(self.rec, {"status": "done", "tasks_file": ".specs/features/alpha/tasks/01-a.md",
                                     "summary": summary})


class PlanApprovalTest(PlanApprovalCase):

    def test_plan_prompt_asks_for_the_summary(self):
        self.f.start_plan(self.rec)
        self.assertIn('"summary"', self.prompts[0])
        self.assertIn("written in Brazilian Portuguese", self.prompts[0])

    def test_finished_plan_waits_with_the_summary(self):
        self.finish_plan()
        self.assertEqual(self.rec["stage"], "approval")
        self.assertEqual(self.builds, [])
        text, human = self.notes[-1]
        self.assertIn("Task 1: the list gets a filter", text)
        self.assertIn("aprovo", text)
        self.assertTrue(human)
        self.assertIn("Plan ready for your approval", self.comments[-1])

    def test_approval_reply_starts_build(self):
        self.finish_plan()
        for word in ("aprovo", "Entendi!", "<@U123> ok", "approve", "👍"):
            with self.subTest(word=word):
                self.builds.clear()
                self.rec.update(stage="approval", inbox=[{"source": "Slack", "text": word}])
                self.f.handle_approval(self.rec)
                self.assertEqual(self.builds, ["ENG-1"])

    def test_other_reply_goes_back_to_plan(self):
        self.finish_plan()
        self.rec["inbox"] = [{"source": "Slack", "text": "ok, mas deixe o filtro de data de fora"}]
        self.f.handle_approval(self.rec)
        self.assertEqual(self.builds, [])
        self.assertEqual(self.rec["stage"], "plan")
        self.assertIn("deixe o filtro de data de fora", self.sent[-1])
        self.assertIn("new summary", self.sent[-1])

    def test_no_reply_keeps_waiting(self):
        self.finish_plan()
        self.f.handle_approval(self.rec)
        self.assertEqual((self.rec["stage"], self.builds, self.sent), ("approval", [], []))

    def test_approval_stage_holds_no_slot(self):
        self.assertIn("approval", self.f.IDLE_STAGES)


class NoApprovalTest(PlanApprovalCase):
    config = {"plan_approval": False}

    def test_plan_goes_straight_to_build(self):
        self.finish_plan()
        self.assertEqual(self.builds, ["ENG-1"])
        self.f.start_plan(self.rec)
        self.assertNotIn('"summary"', self.prompts[-1])


if __name__ == "__main__":
    unittest.main()
