"""Chunking, lexical and vector search, and structural context."""

from __future__ import annotations

import math
import random
import unittest
from array import array
from pathlib import Path

from devdocs.context import StructuralContext, masked_source
from devdocs.graph import FileNode, ModuleNode, ProjectGraph
from devdocs.index.chunk import chunk_file
from devdocs.index.search import LexicalIndex, VectorIndex, fuse, terms
from devdocs.parse import get_parser

MONEY = '''"""Money as integer cents."""

class Money(int):
    """An amount in cents."""

    def plus(self, other: "Money") -> "Money":
        return Money(self + other)


def parse_amount(text: str) -> Money:
    """Read "12.50" as 1250 cents."""
    return Money(round(float(text) * 100))
'''

CART = '''from shop.money import Money, parse_amount

# Money is mentioned here in a comment only.
class Cart:
    def add(self, price: str) -> None:
        self.total = parse_amount(price)   # a real use
        label = "Money"                    # a string, not a use
'''


def facts(relpath: str, source: str):  # type: ignore[no-untyped-def]
    parser = get_parser("python")
    assert parser is not None
    return parser.parse(Path(relpath), source)


class TestChunks(unittest.TestCase):
    def test_header_and_one_chunk_per_declaration(self) -> None:
        chunks = chunk_file("shop/money.py", MONEY, facts("shop/money.py", MONEY))
        self.assertEqual([c.symbol for c in chunks], ["", "Money", "parse_amount"])
        head = "shop/money.py :: Money\nclass Money"
        self.assertTrue(chunks[1].text.startswith(head))
        self.assertIn("Money as integer cents.", chunks[0].text)

    def test_ids_are_content_addresses(self) -> None:
        first = chunk_file("shop/money.py", MONEY, facts("shop/money.py", MONEY))
        edited = MONEY.replace("round(", "int(")
        second = chunk_file("shop/money.py", edited, facts("shop/money.py", edited))
        self.assertEqual(first[1].id, second[1].id)        # Money unchanged
        self.assertNotEqual(first[2].id, second[2].id)     # parse_amount edited

    def test_long_classes_split_into_members(self) -> None:
        body = "".join(
            f"    def m{i}(self):\n        return {i}\n\n" for i in range(40)
        )
        source = f"class Big:\n    '''Doc.'''\n\n{body}"
        chunks = chunk_file("big.py", source, facts("big.py", source))
        self.assertEqual(chunks[0].symbol, "Big")
        self.assertEqual(chunks[1].symbol, "Big.m0")
        self.assertEqual(len(chunks), 41)

    def test_empty_file(self) -> None:
        self.assertEqual(chunk_file("e.py", "", None), [])


class TestLexical(unittest.TestCase):
    def test_terms_split_identifiers(self) -> None:
        self.assertEqual(terms("parseRetryAfter parse_retry_after HTTPServer v2"),
                         ["parse", "retry", "after", "parse", "retry", "after",
                          "http", "server"])

    def test_bm25_ranks_the_defining_chunk_first(self) -> None:
        chunks = [
            *chunk_file("shop/money.py", MONEY, facts("shop/money.py", MONEY)),
            *chunk_file("shop/cart.py", CART, facts("shop/cart.py", CART)),
        ]
        index = LexicalIndex(chunks)
        best, _ = index.search("parse an amount of money", k=1)[0]
        self.assertEqual((best.source, best.symbol), ("shop/money.py", "parse_amount"))
        excluded = index.search("parse amount", exclude={"shop/money.py"})
        self.assertTrue(all(c.source == "shop/cart.py" for c, _ in excluded))

    def test_unknown_terms_find_nothing(self) -> None:
        self.assertEqual(LexicalIndex([]).search("anything"), [])


def unit(values: list[float]) -> array[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return array("f", [v / norm for v in values])


class TestVectors(unittest.TestCase):
    def test_nearest_by_dot_product(self) -> None:
        index = VectorIndex(["a", "b", "c"], [unit([1, 0]), unit([1, 1]), unit([0, 1])])
        self.assertEqual([i for i, _ in index.search(unit([1, 0.1]), k=2)], ["a", "b"])
        self.assertEqual(index.search(unit([1, 0]), k=1, exclude={"a"})[0][0], "b")

    def test_prefilter_finds_the_same_neighbour(self) -> None:
        rng = random.Random(7)
        vectors = [unit([rng.gauss(0, 1) for _ in range(32)]) for _ in range(400)]
        ids = [f"v{i}" for i in range(400)]
        exact = VectorIndex(ids, vectors, prefilter_above=10_000)
        quick = VectorIndex(ids, vectors, prefilter_above=100)
        probe = vectors[123]
        self.assertEqual(exact.search(probe, k=1)[0][0], "v123")
        self.assertEqual(quick.search(probe, k=1)[0][0], "v123")

    def test_mismatched_lengths_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            VectorIndex(["a"], [])

    def test_fuse(self) -> None:
        self.assertEqual(fuse(["a", "b", "c"], ["b", "c", "d"])[:2], ["b", "c"])


class TestStructural(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = ProjectGraph(
            files={
                "shop/money.py": FileNode("shop/money.py", "python", "shop",
                                          dependents=("shop/cart.py",)),
                "shop/cart.py": FileNode("shop/cart.py", "python", "shop",
                                         internal=("shop/money.py",),
                                         entry_point="runs as a script"),
            },
            modules={"shop": ModuleNode("shop", ("shop/cart.py", "shop/money.py"))},
        )
        self.context = StructuralContext(
            self.graph,
            {"shop/money.py": facts("shop/money.py", MONEY),
             "shop/cart.py": facts("shop/cart.py", CART)},
            {"shop/money.py": MONEY, "shop/cart.py": CART},
        )

    def test_uses_skip_imports_comments_and_strings(self) -> None:
        uses = self.context.uses("shop/money.py")
        self.assertEqual([(u.line, u.symbol) for u in uses], [(6, "parse_amount")])
        self.assertIn("parse_amount(price)", uses[0].text)

    def test_defines_lists_signatures_of_what_is_used(self) -> None:
        defined = self.context.defines("shop/cart.py")
        self.assertEqual([d.symbol for d in defined], ["Money", "parse_amount"])
        self.assertIn("def parse_amount(text: str) -> Money", defined[1].text)
        self.assertIn('Read "12.50" as 1250 cents.', defined[1].text)

    def test_budget_is_respected(self) -> None:
        self.assertEqual(self.context.for_file("shop/cart.py", budget_tokens=5), [])

    def test_lines_are_spread_over_importers(self) -> None:
        till = (
            "from shop.money import Money, parse_amount\n"
            "a = parse_amount('1')\nb = Money(2)\n"
            "c = Money(3)\nd = parse_amount('4')\n"
        )
        graph = ProjectGraph(files={
            "shop/money.py": FileNode("shop/money.py", "python", "shop",
                                      dependents=("shop/till.py", "shop/cart.py")),
            "shop/till.py": FileNode("shop/till.py", "python", "shop"),
            "shop/cart.py": FileNode("shop/cart.py", "python", "shop"),
        })
        context = StructuralContext(
            graph, {"shop/money.py": facts("shop/money.py", MONEY)},
            {"shop/money.py": MONEY, "shop/till.py": till, "shop/cart.py": CART},
        )
        uses = [(u.source, u.line) for u in context.uses("shop/money.py")]
        self.assertEqual(
            uses, [("shop/till.py", 2), ("shop/till.py", 3), ("shop/cart.py", 6)]
        )

    def test_entry_bodies(self) -> None:
        bodies = self.context.entry_bodies()
        self.assertEqual(bodies[0].source, "shop/cart.py")
        self.assertIn("class Cart", bodies[0].text)

    def test_masking_keeps_positions(self) -> None:
        masked = masked_source(CART, "python")
        self.assertEqual(len(masked), len(CART))
        self.assertNotIn("comment only", masked)
        self.assertIn("parse_amount(price)", masked)


if __name__ == "__main__":
    unittest.main()
