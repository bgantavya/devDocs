"""The output contract: tagged replies parse leniently and leave clean, grounded."""

from __future__ import annotations

import random
import unittest

from devdocs.render.style import Reply, StyleGuard, acceptable, parse_reply

KNOWN = {"Cache", "get", "os", "replace", "os.replace", "Cache.get", "cart.py",
         "money.py", "languages.py"}


def guard() -> StyleGuard:
    return StyleGuard(known=KNOWN.__contains__)


class TestParse(unittest.TestCase):
    def test_decorated_tags_bullets_and_fences(self) -> None:
        reply = parse_reply(
            "```\n**PURPOSE:** Parses config.\n- **SYMBOL** `load`: Loads it.\n"
            "NOTE:\n- First.\n- Second.\n```",
            "file",
        )
        self.assertEqual(reply.purpose, "Parses config.")
        self.assertEqual(reply.symbols, (("load", "Loads it."),))
        self.assertEqual(reply.notes, ("First.", "Second."))

    def test_untagged_lead_becomes_the_purpose(self) -> None:
        reply = parse_reply("Handles billing.\nRELATION: a calls b.", "module")
        self.assertEqual((reply.purpose, reply.relations),
                         ("Handles billing.", ("a calls b.",)))

    def test_tags_of_another_kind_are_ignored(self) -> None:
        reply = parse_reply("PURPOSE: Does x.\nFLOW: not for files.", "file")
        self.assertEqual(reply.flow, ())
        overview = parse_reply("OVERVIEW: A system.\nFLOW: Step one.", "architecture")
        self.assertEqual((overview.purpose, overview.flow),
                         ("A system.", ("Step one.",)))

    def test_acceptable_needs_a_purpose(self) -> None:
        self.assertFalse(acceptable(Reply(), "file"))
        self.assertTrue(acceptable(Reply(purpose="Does x."), "file"))


class TestGuard(unittest.TestCase):
    def test_filler_marketing_and_emoji_go(self) -> None:
        reply = parse_reply(
            "PURPOSE: This module provides a robust, seamless cache for HTTP "
            "responses 🚀.", "file",
        )
        self.assertEqual(guard().apply(reply, "file").purpose,
                         "Provides a cache for HTTP responses.")

    def test_plain_words_replace_inflated_ones(self) -> None:
        reply = parse_reply("PURPOSE: It utilizes tomllib in order to read TOML.",
                            "file")
        self.assertEqual(guard().apply(reply, "file").purpose,
                         "It uses tomllib to read TOML.")

    def test_hedges_meta_talk_and_invented_names_are_dropped(self) -> None:
        reply = parse_reply(
            "PURPOSE: Caches.\nNOTE: Probably thread-safe.\n"
            "NOTE: Writes are atomic via `os.replace`.\n"
            "NOTE: Calls `invented_helper` on miss.\n"
            "NOTE: As an AI, I cannot see everything.",
            "file",
        )
        cleaned = guard().apply(reply, "file")
        self.assertEqual(cleaned.notes, ("Writes are atomic via `os.replace`.",))
        self.assertEqual(len(cleaned.dropped), 3)
        self.assertTrue(any("ungrounded invented_helper" in d for d in cleaned.dropped))

    def test_only_requested_symbols_survive(self) -> None:
        reply = parse_reply(
            "PURPOSE: Caches.\nSYMBOL Cache.get: Returns the value.\n"
            "SYMBOL Imaginary.call: Does nothing.", "file",
        )
        cleaned = guard().apply(reply, "file", allowed_symbols=("Cache.get",))
        self.assertEqual(cleaned.symbols, (("Cache.get", "Returns the value."),))

    def test_overview_openers_are_removed(self) -> None:
        reply = parse_reply("OVERVIEW: In summary, the system documents code.",
                            "architecture")
        self.assertEqual(guard().apply(reply, "architecture").purpose,
                         "The system documents code.")


class TestClean(unittest.TestCase):
    def test_markdown_links_html_and_emphasis_are_flattened(self) -> None:
        self.assertEqual(
            StyleGuard().clean(
                "**Bold** _emphasis_ in snake_case and [a link](http://x) ✅ <b>tag</b>"
            ),
            "Bold emphasis in snake_case and a link tag.",
        )

    def test_nothing_professional_left_is_empty(self) -> None:
        self.assertEqual(StyleGuard().clean(""), "")
        self.assertEqual(StyleGuard().clean("🚀🚀"), "")

    def test_never_raises_and_is_idempotent(self) -> None:
        rng = random.Random(4)
        alphabet = "ab _*`#[]()<>!?.,:\n🚀é数" + "‍️"
        cleaner = StyleGuard()
        for _ in range(400):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            once = cleaner.clean(text)
            self.assertEqual(cleaner.clean(once), once, repr(text))


if __name__ == "__main__":
    unittest.main()
