"""`max_active` holds every move from an idle stage into a station, not only the first pickup: a plan approval, a
change to the plan, an answer to a stop, a retry or demo request, and a change asked in review all wait for a slot.

  python3 -m unittest tests.test_slots
"""
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_engine, project, sh

PLAN = ".specs/features/alpha/plan.md"


class SlotCase(unittest.TestCase):

    def setUp(self):
        project("slots-")
        self.f = f = load_engine()
        self.notes, self.builds, self.sent, self.begun, self.resumed, self.fixes, self.demos = [], [], [], [], [], [], []
        f.notify = lambda rec, text, human=False, broadcast=False: self.notes.append((rec["identifier"], text))
        f.comment = lambda rec, body: ""
        f.set_needs_human = lambda rec, on: None
        f.set_status = lambda rec, key: None
        f.linear_replies = lambda rec: []
        f.send_to_conversation = lambda rec, text: self.sent.append((rec["identifier"], text))
        f.begin = lambda rec, issue=None, taken=(): self.begun.append(rec["identifier"])
        f.resume_station = lambda rec: self.resumed.append(rec["identifier"])
        f.require_clean_and_pushed = lambda rec: "head"
        f.gh_json = lambda *a, **k: {"state": "OPEN"}

        def start(rec, station, log):
            log.append(rec["identifier"])
            rec["stage"] = station
        f.start_build = lambda rec, handoff=False: start(rec, "build", self.builds)
        f.start_fix = lambda rec, reason, head, why: start(rec, "fix", self.fixes)
        f.start_demo = lambda rec, head, back: start(rec, "demo", self.demos)
        self.state = {"issues": {f"RUN-{n}": {"identifier": f"RUN-{n}", "stage": station, "picked_at": f"0{n}"}
                                 for n, station in enumerate(("build", "verify", "judge"))}}
        f._STATE["issues"] = self.state["issues"]

    def add(self, **fields):
        rec = {"identifier": "ENG-1", "issue_id": "i", "issue_url": "u", "feature": "alpha", "branch": "feat/alpha",
               "base": "main", "round": 0, "picked_at": "1", "conversation_id": "conv", "worktree": "/w", **fields}
        self.state["issues"][rec["identifier"]] = rec
        return rec

    def free_slot(self):
        self.state["issues"]["RUN-0"]["stage"] = "review"

    def notes_for(self, ident):
        return [text for who, text in self.notes if who == ident]


class ApprovalTest(SlotCase):

    def setUp(self):
        super().setUp()
        self.worktree = Path(tempfile.mkdtemp(prefix="slots-wt-"))
        sh("git", "init", "-q", "-b", "feat/alpha", cwd=self.worktree)
        (self.worktree / PLAN).parent.mkdir(parents=True)
        (self.worktree / PLAN).write_text("# Alpha\n\n## Problem\n\nThe list has no filter.\n")
        sh("git", "add", ".", cwd=self.worktree)
        sh("git", "commit", "-qm", "docs(plan): alpha", cwd=self.worktree)

    def test_approval_with_every_slot_taken_queues_build_and_keeps_the_approval(self):
        rec = self.add(stage="approval", worktree=str(self.worktree), conversations=[{"station": "plan"}],
                       inbox=[{"source": "Slack", "text": "aprovo"}])
        self.f.handle_approval(rec)
        self.assertEqual(self.builds, [])
        self.assertEqual((rec["stage"], rec["held_for"]), ("queued", "build"))
        self.assertIn('**Approved**: ', (self.worktree / PLAN).read_text())
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.worktree), "")
        self.assertEqual(self.notes_for("ENG-1"),
                         ["👍 *Plano aprovado*. ⏳ Já há 3 issues rodando: o Build começa quando uma vaga abrir."])
        self.assertEqual(self.f.root_note(rec), "⏳ Na fila")
        self.assertTrue(self.f.pipeline(rec).startswith("✅ Plan › ✅ Approval › ⏳ Build › ⚪ Verify"),
                        self.f.pipeline(rec))

        self.f.fill_slots(self.state)
        self.assertEqual((self.builds, self.begun, len(self.notes_for("ENG-1"))), ([], [], 1))

        self.free_slot()
        self.f.fill_slots(self.state)
        self.assertEqual((self.builds, self.begun), (["ENG-1"], []))
        self.assertNotIn("held_for", rec)
        self.assertEqual(len(self.notes_for("ENG-1")), 1)

    def test_approval_with_a_free_slot_starts_build(self):
        self.free_slot()
        rec = self.add(stage="approval", worktree=str(self.worktree), inbox=[{"source": "Slack", "text": "ok"}])
        self.f.handle_approval(rec)
        self.assertEqual(self.builds, ["ENG-1"])
        self.assertEqual(self.notes_for("ENG-1"), ["👍 *Plano aprovado*: o Build começa."])

    def test_change_request_waits_in_approval(self):
        reply = {"source": "Slack", "text": "deixe o filtro de data de fora"}
        rec = self.add(stage="approval", worktree=str(self.worktree), inbox=[reply])
        for _ in range(2):
            self.f.handle_approval(rec)
        self.assertEqual((rec["stage"], rec["inbox"], self.sent), ("approval", [reply], []))
        self.assertEqual(len(self.notes_for("ENG-1")), 1)
        self.assertNotIn("**Approved**", (self.worktree / PLAN).read_text())

        self.free_slot()
        self.f.handle_approval(rec)
        self.assertEqual(rec["stage"], "plan")
        self.assertIn("deixe o filtro de data de fora", self.sent[-1][1])


class StopAnswerTest(SlotCase):

    def test_answer_to_a_stop_waits_for_a_slot(self):
        reply = {"source": "Slack", "text": "use the demo seed"}
        rec = self.add(stage="waiting", waiting_stage="build", conversations=[{"station": "build"}], inbox=[reply])
        for _ in range(2):
            self.f.deliver_replies(rec)
        self.assertEqual((rec["stage"], rec["inbox"], self.sent), ("waiting", [reply], []))
        self.assertEqual(self.notes_for("ENG-1"), ["⏳ *Na fila*: já há 3 issues rodando. Esta segue quando uma vaga "
                                                   "abrir, com o que você mandou."])

        self.free_slot()
        self.f.deliver_replies(rec)
        self.assertEqual(rec["stage"], "build")
        self.assertIn("use the demo seed", self.sent[-1][1])
        self.assertNotIn("queue_noticed", rec)

    def test_a_later_hold_is_told_again(self):
        rec = self.add(stage="waiting", waiting_stage="build", conversations=[{"station": "build"}],
                       inbox=[{"source": "Slack", "text": "first"}])
        self.f.deliver_replies(rec)
        self.free_slot()
        self.f.deliver_replies(rec)
        self.state["issues"]["RUN-0"]["stage"] = "build"
        rec.update(stage="waiting", inbox=[{"source": "Slack", "text": "second"}])
        self.f.deliver_replies(rec)
        self.assertEqual(len([n for n in self.notes_for("ENG-1") if n.startswith("⏳")]), 2)

    def test_answer_after_the_fix_rounds_ran_out_waits_for_a_slot(self):
        rec = self.add(stage="waiting", waiting_stage="verify", rounds_exhausted=True, round=3,
                       conversations=[{"station": "verify"}], inbox=[{"source": "Slack", "text": "go on"}])
        self.f.deliver_replies(rec)
        self.assertEqual((rec["stage"], self.fixes), ("waiting", []))
        self.free_slot()
        self.f.deliver_replies(rec)
        self.assertEqual(self.fixes, ["ENG-1"])

    def test_stop_before_any_conversation_is_not_held(self):
        rec = self.add(stage="waiting", waiting_stage="plan", inbox=[{"source": "Slack", "text": "fixed it"}])
        rec.pop("conversation_id")
        self.f.deliver_replies(rec)
        self.assertEqual(rec["stage"], "blocked")


class RequestTest(SlotCase):

    def request(self, name):
        self.f.REQUESTS.mkdir(parents=True, exist_ok=True)
        path = self.f.REQUESTS / name
        path.write_text("")
        return path

    def test_retry_request_waits_for_a_slot(self):
        self.add(stage="waiting", waiting_stage="build", conversations=[{"station": "build"}])
        path = self.request("retry-ENG-1")
        for _ in range(2):
            self.f.take_requests(self.state)
        self.assertTrue(path.exists())
        self.assertEqual((self.resumed, len(self.notes_for("ENG-1"))), ([], 1))

        self.free_slot()
        self.f.take_requests(self.state)
        self.assertFalse(path.exists())
        self.assertEqual(self.resumed, ["ENG-1"])

    def test_demo_request_waits_for_a_slot(self):
        self.add(stage="review", pr_number=7, worktree="/w")
        path = self.request("demo-ENG-1")
        self.f.take_requests(self.state)
        self.assertTrue(path.exists())
        self.assertEqual(self.demos, [])

        self.free_slot()
        self.f.take_requests(self.state)
        self.assertFalse(path.exists())
        self.assertEqual(self.demos, ["ENG-1"])


class ReviewTest(SlotCase):

    def test_change_asked_in_review_waits_for_a_slot(self):
        self.f.run = lambda *a, **k: None
        ask ={"source": "Slack", "text": "rename the button"}
        rec = self.add(stage="review", pr_number=7, pr_url="https://github.com/o/r/pull/7", worktree="/w", inbox=[ask])
        self.f.reopen_from_review(rec)
        self.assertEqual((rec["stage"], rec["inbox"], self.fixes), ("review", [ask], []))

        self.free_slot()
        self.f.reopen_from_review(rec)
        self.assertEqual((rec["stage"], self.fixes), ("fix", ["ENG-1"]))


if __name__ == "__main__":
    unittest.main()
