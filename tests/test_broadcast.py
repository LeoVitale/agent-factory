"""`slack.broadcast` decides which thread replies also show in the channel.

  python3 -m unittest tests.test_broadcast
"""
import unittest

from tests.helpers import load_engine, project


class BroadcastTest(unittest.TestCase):

    def replies(self, mode):
        project(f"broadcast-{mode}-", config={"slack": {"broadcast": mode}} if mode else None)
        f = load_engine()
        f.SLACK_BOT = "x"
        f.SLACK_PERSON = "U1"
        f.mutate = lambda what: True
        f.root_text = lambda rec: "root"
        posts = []
        f.slack = lambda method, **p: posts.append(p) or {"ts": "1.0"}
        rec = {"identifier": "ENG-1", "slack_channel": "C1", "slack_ts": "1.0"}
        for human, broadcast in ((False, False), (False, True), (True, False)):
            f.notify(rec, "x", human=human, broadcast=broadcast)
        return [p["reply_broadcast"] for p in posts if "thread_ts" in p]

    def test_none_keeps_every_reply_in_the_thread(self):
        self.assertEqual(self.replies("none"), [False, False, False])

    def test_needs_you_sends_only_the_stops(self):
        self.assertEqual(self.replies("needs_you"), [False, False, True])

    def test_all_sends_stops_and_broadcasts(self):
        self.assertEqual(self.replies("all"), [False, True, True])

    def test_missing_key_behaves_as_all(self):
        self.assertEqual(self.replies(None), [False, True, True])


if __name__ == "__main__":
    unittest.main()
