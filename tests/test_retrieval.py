"""Chunking source at symbol boundaries, the unit retrieval returns."""

from __future__ import annotations

import unittest
from pathlib import Path

from devdocs.index.chunk import chunk_file
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


if __name__ == "__main__":
    unittest.main()
