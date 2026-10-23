"""A conversation gets references to the saved secrets its Canvas profile lists, and nothing else.

  python3 -m unittest tests.test_canvas_secrets
"""
import unittest

from tests.helpers import load_engine, project

PROFILES = {"profiles": [{"id": "p-claude", "name": "Claude Code"}, {"id": "p-cursor", "name": "Cursor-ACP"}]}
DETAIL = {"Claude%20Code": {"secret_refs": ["CLAUDE_CODE_OAUTH_TOKEN"]}, "Cursor-ACP": {"secret_refs": None}}


class CanvasSecretsTest(unittest.TestCase):

    def setUp(self):
        root, _ = project("canvas-secrets-")
        (root / "api-key.txt").write_text("session-key\n")
        self.f = load_engine()
        self.f.CANVAS_KEY_FILE = root / "api-key.txt"
        self.posts = []

        def canvas(method, path, body=None):
            if method == "POST":
                self.posts.append(body)
                return {"id": "conv"}
            if path == "/api/agent-profiles":
                return PROFILES
            return {"name": "x", "profile": DETAIL[path.rsplit("/", 1)[1]]}
        self.f.canvas = canvas
        self.f.notify = lambda *a, **k: None

    def start(self, profile):
        self.f.CONFIG["agent_profile_id"] = profile
        self.f.start_station({"identifier": "ENG-1", "worktree": "/w"}, "build", "prompt")
        return self.posts[-1]["secrets"]

    def test_listed_secret_is_passed_by_reference(self):
        self.assertEqual(self.start("p-claude"), {"CLAUDE_CODE_OAUTH_TOKEN": {
            "kind": "LookupSecret", "url": "http://127.0.0.1:8000/api/settings/secrets/CLAUDE_CODE_OAUTH_TOKEN",
            "headers": {"X-Session-API-Key": "session-key"}}})

    def test_profile_without_a_list_gets_no_secret(self):
        self.assertEqual(self.start("p-cursor"), {})

    def test_unknown_profile_gets_no_secret(self):
        self.assertEqual(self.start("p-gone"), {})


if __name__ == "__main__":
    unittest.main()
