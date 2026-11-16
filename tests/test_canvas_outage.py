"""A Canvas whose agent server died answers 502 to every call while its proxy stays up. The factory probes it once
per tick, moves no station while it is down, and tells the default channel once when it goes down and once when
it is back, never per issue.

  python3 -m unittest tests.test_canvas_outage
"""
import http.client
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from tests.helpers import load_engine, project

PROBE = "/api/agent-profiles"


class CanvasOutageTest(unittest.TestCase):

    def setUp(self):
        self.engine("en")

    def engine(self, language):
        project(f"canvas-outage-{language}-", config={"slack": {"language": language}})
        self.f = f = load_engine()
        f.SLACK_BOT, f.SLACK_PERSON = "x", "U1"
        self.posts, self.channels, self.calls, self.advanced, self.approvals, self.begun, self.notes = ([] for _ in range(7))
        self.down = 502

        def slack(method, **p):
            if method == "conversations.create":
                self.channels.append(p["name"])
                return {"channel": {"id": "C-DEFAULT"}}
            if method == "chat.postMessage":
                self.posts.append(p)
            return {"ts": "1.0"}

        def canvas(method, path, body=None):
            self.calls.append(path)
            if isinstance(self.down, Exception):
                raise self.down
            if self.down == "refused":
                raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
            if self.down:
                raise f.HTTPStatusError(f"{method} http://canvas:8000{path} -> {self.down}: "
                                        "b'Bad Gateway: connect ECONNREFUSED 127.0.0.1:18000'", self.down)
            return {"execution_status": "finished", "profiles": []}

        f.slack, f.canvas = slack, canvas
        f.refresh_config = lambda: None
        f.ready_issues = lambda: []
        f.notify = lambda rec, text, **k: self.notes.append(rec["identifier"])
        f.advance = lambda rec: self.advanced.append(rec["identifier"])
        f.handle_approval = lambda rec: self.approvals.append(rec["identifier"])
        f.begin = lambda rec, issue=None, taken=(): self.begun.append(rec["identifier"])
        self.state = {"issues": {
            "ENG-1": {"identifier": "ENG-1", "stage": "build", "conversation_id": "a", "picked_at": "1"},
            "ENG-2": {"identifier": "ENG-2", "stage": "verify", "conversation_id": "b", "picked_at": "2"},
            "ENG-3": {"identifier": "ENG-3", "stage": "approval", "conversation_id": "c", "picked_at": "3",
                      "inbox": [{"source": "Slack", "text": "approve"}]},
            "ENG-4": {"identifier": "ENG-4", "stage": "queued", "picked_at": "4"}}}

    def saved(self):
        return json.loads(self.f.STATE_PATH.read_text())

    def test_outage_is_told_once_and_moves_nothing(self):
        for _ in range(2):
            self.f.tick(self.state)
        self.assertEqual(self.calls, [PROBE, PROBE])
        self.assertEqual((self.advanced, self.approvals, self.begun, self.notes), ([], [], [], []))
        self.assertEqual(self.state["issues"]["ENG-3"]["inbox"], [{"source": "Slack", "text": "approve"}])
        self.assertEqual(len(self.posts), 1)
        post = self.posts[0]
        self.assertEqual((post["channel"], self.channels), ("C-DEFAULT", ["acme-factory"]))
        self.assertNotIn("thread_ts", post)
        self.assertTrue(post["text"].startswith("<@U1> 🔌 *The Canvas is down*: `GET http://canvas:8000"), post["text"])
        self.assertIn("502: b'Bad Gateway: connect ECONNREFUSED 127.0.0.1:18000'", post["text"])
        self.assertIn("since", self.saved()["canvas_down"])

        self.down = None
        self.f.tick(self.state)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.posts[1]["text"], "<@U1> ✅ *The Canvas is back* after 1 min down. The stations move again.")
        self.assertNotIn("canvas_down", self.saved())
        self.assertEqual((self.advanced, self.approvals, self.begun), (["ENG-1", "ENG-2"], ["ENG-3"], ["ENG-4"]))

    def test_quick_tick_probes_once_and_skips_the_stations(self):
        self.f.quick_tick(self.state)
        self.assertEqual((self.calls, self.advanced, len(self.posts)), ([PROBE], [], 1))

        self.down = None
        self.calls.clear()
        self.f.quick_tick(self.state)
        self.assertEqual(self.calls, [PROBE, "/api/conversations/a", "/api/conversations/b"])
        self.assertEqual(self.advanced, ["ENG-1", "ENG-2"])
        self.assertIn("The Canvas is back", self.posts[-1]["text"])

    def test_connection_refused_is_an_outage(self):
        self.down = "refused"
        self.f.tick(self.state)
        self.assertEqual(self.advanced, [])
        self.assertIn("Connection refused", self.posts[0]["text"])

    def test_any_other_probe_error_is_an_outage_and_stays_in_the_tick(self):
        # A response cut off as the server dies, or a body that is not JSON, must not end the loop.
        for error in (http.client.IncompleteRead(b"{"), json.JSONDecodeError("Expecting value", "<html>", 0)):
            with self.subTest(error=type(error).__name__):
                self.posts.clear()
                self.state.pop("canvas_down", None)
                self.down = error
                self.f.tick(self.state)
                self.f.quick_tick(self.state)
                self.assertEqual(self.advanced, [])
                self.assertEqual(len(self.posts), 1)
                self.assertIn(type(error).__name__, self.posts[0]["text"])

    def test_other_error_statuses_are_not_an_outage(self):
        for status in (401, 404, 500):
            with self.subTest(status=status):
                self.down = status
                self.advanced.clear()
                self.f.tick(self.state)
                self.assertEqual(self.advanced, ["ENG-1", "ENG-2"])
        self.assertEqual(self.posts, [])
        self.assertNotIn("canvas_down", self.state)

    def test_outage_saved_before_a_restart_is_not_told_again(self):
        since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        self.state["canvas_down"] = {"since": since, "error": "502"}
        self.f.tick(self.state)
        self.assertEqual(self.posts, [])
        self.down = None
        self.f.tick(self.state)
        self.assertEqual(self.posts[0]["text"], "<@U1> ✅ *The Canvas is back* after 30 min down. The stations move again.")

    def test_the_channel_hears_the_projects_language(self):
        self.engine("pt-BR")
        self.f.tick(self.state)
        self.assertTrue(self.posts[0]["text"].startswith("<@U1> 🔌 *A Canvas está fora do ar*"), self.posts[0]["text"])


if __name__ == "__main__":
    unittest.main()
