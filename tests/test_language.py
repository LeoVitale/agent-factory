"""The Slack thread speaks the language the project picks in `slack.language`.

  python3 -m unittest tests.test_language
"""
import string
import unittest

from tests.helpers import load_engine, project

import messages


class LanguageTest(unittest.TestCase):

    def thread(self, language):
        project(f"language-{language}-", config={"slack": {"language": language}})
        f = load_engine()
        notes = []
        f.notify = lambda rec, text, human=False, broadcast=False: notes.append(text)
        f.notify({"identifier": "ENG-1"}, f.say("picked", base="main", branch="feat/x"))
        f.set_needs_human = lambda rec, on: None
        f.comment = lambda rec, body: ""
        f.stop_for_human({"identifier": "ENG-1", "stage": "build"}, "Which seed?")
        return f, notes

    def test_portuguese(self):
        f, notes = self.thread("pt-BR")
        self.assertEqual(notes[0], "📥 *Entrou na factory*: parte do `main` na branch `feat/x`.")
        self.assertIn("Preciso de você", notes[1])
        self.assertEqual(f.root_note({"stage": "queued"}), "⏳ Na fila")

    def test_english(self):
        f, notes = self.thread("en")
        self.assertEqual(notes[0], "📥 *Joined the factory*: starts from `main` on the branch `feat/x`.")
        self.assertIn("I need you", notes[1])
        self.assertEqual(f.root_note({"stage": "queued"}), "⏳ In the queue")

    def test_missing_language_keeps_portuguese(self):
        project("language-missing-", config={"slack": {"language": None}})
        f = load_engine()
        self.assertEqual(f.language(), "pt-BR")

    def test_every_message_has_both_languages_with_the_same_fields(self):
        fields = lambda text: sorted(n for _, n, _, _ in string.Formatter().parse(text) if n)
        for key, texts in messages.MESSAGES.items():
            with self.subTest(key=key):
                self.assertEqual(sorted(texts), sorted(messages.LANGUAGES))
                if isinstance(texts["en"], str):
                    self.assertEqual(fields(texts["pt-BR"]), fields(texts["en"]))


if __name__ == "__main__":
    unittest.main()
