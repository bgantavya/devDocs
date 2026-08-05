"""Prompts: the packaged system prompts, source excerpts and user prompts.

Expected excerpts and prompts are written out in full. A prompt is a cache key
and the model's whole view of the code, so a changed label, cap, order or
trimming rule should fail here rather than quietly change every call. The
hand-built facts mirror what the Python and TypeScript parsers report for the
same sources.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
import unittest
from importlib import resources
from pathlib import Path

from devdocs.context import Snippet
from devdocs.graph import FileNode, ModuleNode, ProjectGraph
from devdocs.llm.budget import estimate_tokens
from devdocs.llm.catalog import Tier
from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind, get_parser
from devdocs.plan import FilePlan, ModulePlan
from devdocs.prompting import Prompt, PromptBuilder, excerpt, system_prompt
from devdocs.render.model import Mode

CLASS = SymbolKind.CLASS
METHOD = SymbolKind.METHOD
FUNCTION = SymbolKind.FUNCTION
CONSTANT = SymbolKind.CONSTANT

_MARKER = re.compile(r"(?P<indent> *)\.\.\. \((?P<count>\d+) lines? omitted\)")
_MORE = re.compile(r"\.\.\. \(\d+ more declarations? omitted\)")


def sym(
    name: str,
    kind: SymbolKind,
    line: int,
    end_line: int = 0,
    signature: str = "",
    docstring: str = "",
    *children: Symbol,
    public: bool = True,
) -> Symbol:
    return Symbol(
        name,
        kind,
        line=line,
        end_line=end_line,
        signature=signature,
        docstring=docstring,
        children=children,
        is_public=public,
    )


def facts_of(
    *symbols: Symbol,
    language: str = "python",
    exports: tuple[str, ...] = (),
    module_docstring: str = "",
    loc: int = 0,
    confidence: Confidence = Confidence.EXACT,
) -> FileFacts:
    return FileFacts(
        Path("m.py"),
        language,
        symbols,
        exports=exports,
        module_docstring=module_docstring,
        loc=loc,
        confidence=confidence,
    )


CART = '''\
# Copyright 2024 Example Corp.
# Licensed under the Apache License, Version 2.0.

"""Cart storage and pricing."""

import json
from decimal import Decimal

MAX_ITEMS = 100


class Cart:
    """A shopping cart.

    Holds items until checkout.
    """

    owner: str
    items: list[str]

    def add(self, sku: str, qty: int = 1) -> None:
        """Add `qty` units of `sku`."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        for _ in range(qty):
            self.items.append(sku)
        self._dirty = True

    def total(self, prices: dict[str, Decimal]) -> Decimal:
        result = Decimal(0)
        for sku in self.items:
            result += prices[sku]
        return result


def load(path: str) -> Cart:
    """Read a cart from `path`."""
    with open(path) as handle:
        data = json.load(handle)
    cart = Cart()
    cart.owner = data["owner"]
    for sku in data["items"]:
        cart.add(sku)
    return cart
'''

CART_FACTS = FileFacts(
    Path("shop/cart.py"),
    "python",
    symbols=(
        sym(
            "Cart", CLASS, 12, 33, "class Cart",
            "A shopping cart.\n\nHolds items until checkout.",
            sym("add", METHOD, 21, 27, "def add(self, sku: str, qty: int = 1) -> None",
                "Add `qty` units of `sku`."),
            sym("total", METHOD, 29, 33,
                "def total(self, prices: dict[str, Decimal]) -> Decimal"),
        ),
        sym("load", FUNCTION, 36, 44, "def load(path: str) -> Cart",
            "Read a cart from `path`."),
        sym("MAX_ITEMS", CONSTANT, 9, 9, "MAX_ITEMS = 100"),
    ),
    imports=("json", "decimal"),
    exports=("Cart", "load", "MAX_ITEMS"),
    module_docstring="Cart storage and pricing.",
    loc=45,
)

# The licence and the blank line after it are gone; nothing else changes.
CART_TEXT = CART.split("\n", 3)[3].removesuffix("\n")

CART_HEADER = '''\
"""Cart storage and pricing."""

import json
from decimal import Decimal

MAX_ITEMS = 100


class Cart:
    """A shopping cart.'''

# Allowance 3: a function keeps three body lines, a method one.
CART_BODIES = CART_HEADER + '''

    Holds items until checkout.
    """

    owner: str
    items: list[str]

    def add(self, sku: str, qty: int = 1) -> None:
        """Add `qty` units of `sku`."""
        if qty <= 0:
        ... (4 lines omitted)

    def total(self, prices: dict[str, Decimal]) -> Decimal:
        result = Decimal(0)
        ... (3 lines omitted)


def load(path: str) -> Cart:
    """Read a cart from `path`."""
    with open(path) as handle:
        data = json.load(handle)
    cart = Cart()
    ... (4 lines omitted)'''

# Docstrings cut to their first line; class fields still kept.
CART_BRIEF = CART_HEADER + '''
    ... (3 lines omitted)

    owner: str
    items: list[str]

    def add(self, sku: str, qty: int = 1) -> None:
        """Add `qty` units of `sku`."""
        ... (5 lines omitted)

    def total(self, prices: dict[str, Decimal]) -> Decimal:
        ... (4 lines omitted)


def load(path: str) -> Cart:
    """Read a cart from `path`."""
    ... (7 lines omitted)'''

# Bare declarations: class fields go too.
CART_BARE = CART_HEADER + '''
    ... (6 lines omitted)

    def add(self, sku: str, qty: int = 1) -> None:
        """Add `qty` units of `sku`."""
        ... (5 lines omitted)

    def total(self, prices: dict[str, Decimal]) -> Decimal:
        ... (4 lines omitted)


def load(path: str) -> Cart:
    """Read a cart from `path`."""
    ... (7 lines omitted)'''

CART_FIRST = CART_HEADER + '''
    ... (6 lines omitted)

    def add(self, sku: str, qty: int = 1) -> None:
        """Add `qty` units of `sku`."""
        ... (5 lines omitted)
... (2 more declarations omitted)'''

CART_HEAD_AND_TAIL = '''\
"""Cart storage and pricing."""

import json
... (37 lines omitted)
    return cart'''

CART_TS = '''\
// Copyright (c) 2024 Example Corp. All rights reserved.
// SPDX-License-Identifier: MIT

import { Decimal } from "./decimal";

/** Largest cart. */
export const MAX_ITEMS = 100;

/**
 * A shopping cart.
 */
export class Cart
{
  private items: string[] = [];

  /** Adds `qty` units. */
  add(sku: string, qty = 1): void {
    if (qty <= 0) {
      throw new Error("qty must be positive");
    }
    for (let i = 0; i < qty; i++) {
      this.items.push(sku);
    }
  }

  total(prices: Map<string, Decimal>): Decimal {
    let result = new Decimal(0);
    for (const sku of this.items) {
      result = result.plus(prices.get(sku)!);
    }
    return result;
  }
}

export function load(json: string): Cart {
  const data = JSON.parse(json);
  const cart = new Cart();
  for (const sku of data.items) {
    cart.add(sku);
  }
  return cart;
}

load("{}");
console.log("ready");
'''


def ts_facts(*, ends: bool) -> FileFacts:
    def end(line: int) -> int:
        return line if ends else 0

    return FileFacts(
        Path("src/cart.ts"),
        "typescript",
        symbols=(
            sym("MAX_ITEMS", CONSTANT, 7, end(7), "export const MAX_ITEMS = 100",
                "Largest cart."),
            sym(
                "Cart", CLASS, 12, end(33), "export class Cart", "A shopping cart.",
                sym("add", METHOD, 17, end(24), "add(sku: string, qty = 1): void",
                    "Adds `qty` units."),
                sym("total", METHOD, 26, end(32),
                    "total(prices: Map<string, Decimal>): Decimal"),
            ),
            sym("load", FUNCTION, 35, end(42),
                "export function load(json: string): Cart"),
        ),
        confidence=Confidence.HEURISTIC,
    )


CART_TS_HEADER = '''\
import { Decimal } from "./decimal";

/** Largest cart. */
export const MAX_ITEMS = 100;

/**
 * A shopping cart.
 */
export class Cart
{
  private items: string[] = [];

  /** Adds `qty` units. */
  add(sku: string, qty = 1): void {'''

CART_TS_TRIMMED = CART_TS_HEADER + '''
    ... (6 lines omitted)
  }

  total(prices: Map<string, Decimal>): Decimal {
    ... (5 lines omitted)
  }
}

export function load(json: string): Cart {
  const data = JSON.parse(json);
  ... (5 lines omitted)
}

load("{}");
console.log("ready");'''

# Unknown ends run to the next symbol, short of an enclosing block's lines;
# the last symbol runs to the end of the file.
CART_TS_OPEN = CART_TS_HEADER + '''
    if (qty <= 0) {
    ... (5 lines omitted)
  }

  total(prices: Map<string, Decimal>): Decimal {
    let result = new Decimal(0);
    ... (4 lines omitted)
  }
}

export function load(json: string): Cart {
  const data = JSON.parse(json);
  const cart = new Cart();
... (8 lines omitted)'''

ROUTES = '''\
#!/usr/bin/env python3
"""HTTP routes."""

from shop import app


@app.route(
    "/cart",
    methods=["GET"],
)
def show_cart(
    request,
    *,
    limit: int = 10,
) -> Response:
    """Render the cart.

    Longer explanation here.
    """
    items = load(request)
    total = sum(items)
    return render(items, total)


if __name__ == "__main__":
    app.run()
    print("done")
'''

ROUTES_FACTS = facts_of(
    sym("show_cart", FUNCTION, 11, 22,
        "def show_cart( request, *, limit: int = 10, ) -> Response",
        "Render the cart.\n\nLonger explanation here."),
)

ROUTES_BRIEF = '''\
#!/usr/bin/env python3
"""HTTP routes."""

from shop import app


@app.route(
    "/cart",
    methods=["GET"],
)
def show_cart(
    request,
    *,
    limit: int = 10,
) -> Response:
    """Render the cart.
    ... (6 lines omitted)
... (5 lines omitted)'''


TAGS = {
    "file": ("PURPOSE:", "SYMBOL ", "NOTE:"),
    "module": ("PURPOSE:", "RELATION:", "NOTE:"),
    "architecture": ("OVERVIEW:", "FLOW:", "EXTEND:", "NOTE:"),
}
EVERY_TAG = frozenset(tag for tags in TAGS.values() for tag in tags)


class SystemPromptTest(unittest.TestCase):
    def test_loads_without_the_maintainer_comment(self) -> None:
        for kind in TAGS:
            with self.subTest(kind=kind):
                raw = (resources.files("devdocs.prompts") / f"{kind}.md").read_text(
                    encoding="utf-8"
                )
                self.assertTrue(raw.startswith("<!--"))
                text = system_prompt(kind)
                self.assertEqual(text, raw.split("-->", 1)[1].strip())
                self.assertNotIn("<!--", text)
                self.assertNotIn("-->", text)

    def test_text_is_plain_and_short(self) -> None:
        for kind in TAGS:
            with self.subTest(kind=kind):
                text = system_prompt(kind)
                self.assertTrue(text.isascii())
                self.assertEqual(text, text.strip())
                self.assertEqual(
                    [line.rstrip() for line in text.split("\n")], text.split("\n")
                )
                self.assertNotRegex(text, r"(?m)^#")
                self.assertLessEqual(estimate_tokens(text), 450)

    def test_mentions_every_tag_of_its_kind_in_order_and_no_other(self) -> None:
        for kind, tags in TAGS.items():
            with self.subTest(kind=kind):
                text = system_prompt(kind)
                starts = [text.find(f"\n{tag}") for tag in tags]
                self.assertNotIn(-1, starts)
                self.assertEqual(starts, sorted(starts))
                for tag in EVERY_TAG - set(tags):
                    self.assertNotIn(tag, text)

    def test_states_the_rules(self) -> None:
        for kind in TAGS:
            with self.subTest(kind=kind):
                text = system_prompt(kind)
                for phrase in (
                    "one item per line, in this order, nothing else",
                    "present tense",
                    "Describe behaviour, not code layout",
                    "No emoji, headings or Markdown",
                    "No filler or marketing words",
                    "Never speculate or hedge",
                    "Use only identifiers that appear in the given",
                    "Omit an optional tag",
                    "only",
                    "Example:",
                ):
                    self.assertIn(phrase, text)

    def test_example_reply_uses_the_tags_in_order(self) -> None:
        for kind, tags in TAGS.items():
            with self.subTest(kind=kind):
                example = system_prompt(kind).split("\nExample:\n", 1)[1]
                order = [
                    next(i for i, tag in enumerate(tags) if line.startswith(tag))
                    for line in example.split("\n")
                ]
                self.assertEqual(order, sorted(order))
                self.assertEqual(order[0], 0)

    def test_cached_and_validated(self) -> None:
        self.assertIs(system_prompt("file"), system_prompt("file"))
        for kind in ("", "File", "symbol", "file.md"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                system_prompt(kind)


class NormaliseTest(unittest.TestCase):
    """Sources that fit come back normalised and untruncated."""

    def check(self, source: str, expected: str, language: str = "python") -> None:
        self.assertEqual(
            excerpt(source, facts_of(language=language), 10_000), (expected, False)
        )

    def test_line_ends_trailing_space_and_blank_runs(self) -> None:
        source = (
            "\r\n\r\ndef f():  \r\n    return 1\t\r\n\r\n\r\n\r\n\r\n"
            "def g():\r\n\r\n\r\n    pass\r\n\r\n"
        )
        self.check(source, "def f():\n    return 1\n\ndef g():\n\n\n    pass")

    def test_lone_carriage_returns_break_python_lines_only(self) -> None:
        self.check("x = 1\ry = 2\r", "x = 1\ny = 2")
        self.check("x := 1\ry := 2\r", "x := 1\ry := 2", language="go")

    def test_byte_order_mark_and_empty_sources(self) -> None:
        self.check("\ufeffimport os\n", "import os")
        self.check("", "")
        self.check(" \n\t\n\n", "")

    def test_licence_dropped_shebang_and_docstring_kept(self) -> None:
        source = (
            "#!/usr/bin/env python3\n"
            "# -*- coding: utf-8 -*-\n"
            "\n"
            "# Copyright 2024 Example Corp.\n"
            "# SPDX-License-Identifier: Apache-2.0\n"
            "\n"
            '"""Licence checks: copyright notices are validated here."""\n'
            "import re\n"
        )
        self.check(
            source,
            "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n\n\n"
            '"""Licence checks: copyright notices are validated here."""\n'
            "import re",
        )

    def test_comment_after_code_or_past_line_40_is_kept(self) -> None:
        late = "import os\n# Copyright 2024 Example Corp.\nx = 1\n"
        self.check(late, late.removesuffix("\n"))
        far = "\n" * 40 + "# Copyright 2024 Example Corp.\nx = 1\n"
        self.check(far, "# Copyright 2024 Example Corp.\nx = 1")

    def test_shell_licence(self) -> None:
        source = (
            "#!/bin/sh\n# Copyright 2024 Acme\n# SPDX-License-Identifier: MIT\n"
            "\nset -e\n"
        )
        self.check(source, "#!/bin/sh\n\nset -e", language="shell")

    def test_block_comment_licence(self) -> None:
        source = (
            "/**\n * @file Checkout page.\n */\n"
            "/*\n * Copyright (c) 2024 Acme.\n * Licensed under the MIT licence.\n */\n"
            "'use strict';\n"
        )
        self.check(
            source,
            "/**\n * @file Checkout page.\n */\n'use strict';",
            language="javascript",
        )
        inline = "/* Copyright 2024 Acme */ export const x = 1;"
        self.check(inline, inline, language="javascript")
        unclosed = "/* Copyright 2024 Acme\nexport const x = 1;"
        self.check(unclosed, unclosed, language="javascript")

    def test_go_package_comment_is_documentation(self) -> None:
        source = (
            "// Copyright 2024 The Acme Authors.\n"
            "// Use of this source code is governed by a BSD-style license.\n"
            "\n"
            "// Package store persists carts.\n"
            "package store\n"
        )
        self.check(source, "// Package store persists carts.\npackage store", "go")
        package = "// Package license checks license keys.\npackage license"
        self.check(package, package, language="go")

    def test_licence_only_file(self) -> None:
        for end in ("\n", ""):
            with self.subTest(end=end):
                self.check(f"// Copyright 2024 Acme\n// All rights reserved.{end}",
                           "", "go")

    def test_rust_doc_comments_are_documentation(self) -> None:
        docs = "//! Licensed under MIT; see LICENSE.\n//! Cart engine.\nuse std::fmt;"
        self.check(docs, docs, language="rust")
        source = "// SPDX-License-Identifier: MIT\n//! Cart engine.\nuse std::fmt;\n"
        self.check(source, "//! Cart engine.\nuse std::fmt;", language="rust")


class SkeletonTest(unittest.TestCase):
    def assert_faithful(self, text: str, source: str, facts: FileFacts) -> None:
        """Every line is the next normalised line, or a marker at its block's
        indentation standing for exactly the lines it skips; a declaration
        count may end the excerpt early."""
        if not text:
            return
        normalised = excerpt(source, facts, 1_000_000)[0].split("\n")
        rows = text.split("\n")
        position = 0
        for index, row in enumerate(rows):
            marker = _MARKER.fullmatch(row)
            if marker is not None:
                count = int(marker["count"])
                skipped = normalised[position : position + count]
                indent = min(
                    (len(line) - len(line.lstrip()) for line in skipped if line),
                    default=0,
                )
                self.assertEqual(len(marker["indent"]), indent, row)
                position += count
            elif _MORE.fullmatch(row):
                self.assertEqual(index, len(rows) - 1)
                return
            else:
                self.assertEqual(row, normalised[position])
                position += 1
        self.assertEqual(position, len(normalised))

    def test_fits(self) -> None:
        self.assertEqual(excerpt(CART, CART_FACTS, 253), (CART_TEXT, False))
        self.assertTrue(CART_TEXT.startswith('"""Cart storage and pricing."""\n'))

    def test_trims_bodies_methods_first(self) -> None:
        self.assertEqual(excerpt(CART, CART_FACTS, 200), (CART_BODIES, True))
        self.assertEqual(estimate_tokens(CART_BODIES), 184)

    def test_cuts_docstrings_then_fields_before_declarations(self) -> None:
        self.assertEqual(excerpt(CART, CART_FACTS, 145), (CART_BRIEF, True))
        self.assertEqual(excerpt(CART, CART_FACTS, 140), (CART_BARE, True))

    def test_drops_declarations_from_the_end(self) -> None:
        self.assertEqual(excerpt(CART, CART_FACTS, 100), (CART_FIRST, True))
        self.assertEqual(
            excerpt(CART, CART_FACTS, 40)[0].split("\n")[-2:],
            ["MAX_ITEMS = 100", "... (4 more declarations omitted)"],
        )

    def test_head_and_tail_when_not_even_the_header_fits(self) -> None:
        self.assertEqual(excerpt(CART, CART_FACTS, 30), (CART_HEAD_AND_TAIL, True))

    def test_every_declaration_kept_while_a_skeleton_fits(self) -> None:
        declarations = (
            "MAX_ITEMS = 100",
            "class Cart:",
            "    def add(self, sku: str, qty: int = 1) -> None:",
            "    def total(self, prices: dict[str, Decimal]) -> Decimal:",
            "def load(path: str) -> Cart:",
        )
        for budget in range(estimate_tokens(CART_BARE), 253):
            with self.subTest(budget=budget):
                text, cut = excerpt(CART, CART_FACTS, budget)
                self.assertTrue(cut)
                self.assertLessEqual(estimate_tokens(text), budget)
                rows = text.split("\n")
                for declaration in declarations:
                    self.assertIn(declaration, rows)

    def test_always_fits_and_stays_faithful(self) -> None:
        cases = (
            (CART, CART_FACTS),
            (CART_TS, ts_facts(ends=True)),
            (CART_TS, ts_facts(ends=False)),
            (ROUTES, ROUTES_FACTS),
        )
        for case, (source, facts) in enumerate(cases):
            for budget in range(1, estimate_tokens(source) + 2):
                with self.subTest(case=case, budget=budget):
                    text, _ = excerpt(source, facts, budget)
                    self.assertLessEqual(estimate_tokens(text), budget)
                    self.assert_faithful(text, source, facts)

    def test_line_ends_do_not_shift_symbols(self) -> None:
        for ending in ("\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                source = CART.replace("\n", ending)
                self.assertEqual(excerpt(source, CART_FACTS, 145), (CART_BRIEF, True))

    def test_decorators_and_multi_line_signatures_are_declarations(self) -> None:
        self.assertEqual(excerpt(ROUTES, ROUTES_FACTS, 80), (ROUTES_BRIEF, True))

    def test_code_above_a_declaration_is_not_a_decorator(self) -> None:
        head = "import os\n\nx = compute(\n    1,\n)\ndef f():\n"
        facts = facts_of(sym("f", FUNCTION, 6, 16, "def f()"))
        self.assertEqual(
            excerpt(head + "    return x\n" * 10, facts, 21),
            (head + "    ... (10 lines omitted)", True),
        )

    def test_docstring_found_past_a_blank_line_and_unterminated_is_body(self) -> None:
        body = "    x = 1\n" * 20
        spaced = 'def f():\n\n    """Run it.\n\n    Details here.\n    """\n' + body
        facts = facts_of(
            sym("f", FUNCTION, 1, 26, "def f()", "Run it.\n\nDetails here.")
        )
        self.assertEqual(
            excerpt(spaced, facts, 24),
            (spaced[:-len(body)] + "    ... (20 lines omitted)", True),
        )
        unterminated = 'def g():\n    """Never closed\n' + body
        facts = facts_of(sym("g", FUNCTION, 1, 22, "def g()", "Never closed"))
        self.assertEqual(
            excerpt(unterminated, facts, 12),
            ("def g():\n    ... (21 lines omitted)", True),
        )

    def test_brace_language_keeps_closing_braces_and_doc_comments(self) -> None:
        self.assertEqual(
            excerpt(CART_TS, ts_facts(ends=True), 150), (CART_TS_TRIMMED, True)
        )

    def test_unknown_end_runs_to_the_next_symbol(self) -> None:
        self.assertEqual(
            excerpt(CART_TS, ts_facts(ends=False), 150), (CART_TS_OPEN, True)
        )

    def test_long_lines_are_clipped(self) -> None:
        source = "DATA = '" + "x" * 1000 + "'\n\ndef f():\n    return DATA\n"
        facts = facts_of(sym("f", FUNCTION, 3, 4, "def f()"))
        expected = (
            "DATA = '" + "x" * 392 + " ... (609 characters omitted)\n"
            "\ndef f():\n    return DATA"
        )
        self.assertEqual(excerpt(source, facts, 200), (expected, True))

    def test_symbol_order_in_facts_does_not_matter(self) -> None:
        shuffled = dataclasses.replace(
            CART_FACTS, symbols=tuple(reversed(CART_FACTS.symbols))
        )
        for budget in (200, 145, 140, 100):
            with self.subTest(budget=budget):
                self.assertEqual(
                    excerpt(CART, shuffled, budget), excerpt(CART, CART_FACTS, budget)
                )

    def test_real_parser_facts(self) -> None:
        parser = get_parser("python")
        assert parser is not None
        facts = parser.parse(Path("shop/cart.py"), CART)
        for budget in range(estimate_tokens(CART_BARE), 253, 7):
            with self.subTest(budget=budget):
                text, cut = excerpt(CART, facts, budget)
                self.assertTrue(cut)
                self.assertLessEqual(estimate_tokens(text), budget)
                for name in ("class Cart:", "    def add(", "    def total(",
                             "def load("):
                    self.assertIn(f"\n{name}", text)


class FallbackTest(unittest.TestCase):
    LINES = tuple(f"line {number:03d} " + "x" * 11 for number in range(1, 201))
    SOURCE = "\n".join(LINES) + "\n"

    def test_head_and_tail_split_70_30(self) -> None:
        # 100 tokens allow 349 characters; less the marker, 325: the head
        # gets 227 (10 lines of 21), the tail 98 (4 lines).
        expected = "\n".join(
            [*self.LINES[:10], "... (186 lines omitted)", *self.LINES[-4:]]
        )
        failed = facts_of(
            sym("f", FUNCTION, 3, 9), confidence=Confidence.FAILED
        )
        for facts in (facts_of(), failed):
            with self.subTest(confidence=facts.confidence):
                self.assertEqual(excerpt(self.SOURCE, facts, 100), (expected, True))
        self.assertEqual(estimate_tokens(expected), 91)

    def test_minified_line_is_clipped(self) -> None:
        source = "x" * 100_000
        self.assertEqual(
            excerpt(source, facts_of(language="javascript"), 200),
            ("x" * 400 + " ... (99600 characters omitted)", True),
        )

    def test_nothing_fits(self) -> None:
        for budget in (1, 0, -5):
            with self.subTest(budget=budget):
                self.assertEqual(excerpt(self.SOURCE, facts_of(), budget), ("", True))


class RobustnessTest(unittest.TestCase):
    WEIRD = facts_of(
        sym("a", FUNCTION, 0),
        sym("b", FUNCTION, -3, -1),
        sym("c", CLASS, 5, 2, "class c", "", sym("d", METHOD, 1, 99)),
        sym("e", FUNCTION, 5, 7),
        sym("f", FUNCTION, 10_000),
        sym("g", FUNCTION, 3, 10_000, "def g(" + "x" * 500),
        sym("h", FUNCTION, 12, 0, "", "doc"),
        confidence=Confidence.DEGRADED,
    )
    SOURCES = (
        CART,
        CART_TS,
        "",
        "\n\n",
        "x" * 5000,
        "def g():\n" + '    """unterminated\n' * 30,
        "\n".join(f"    {'{' * depth}" for depth in range(60)),
    )

    def test_inconsistent_facts_never_raise_and_still_fit(self) -> None:
        for index, source in enumerate(self.SOURCES):
            for budget in (1, 2, 10, 50, 120, 400):
                with self.subTest(source=index, budget=budget):
                    first = excerpt(source, self.WEIRD, budget)
                    self.assertEqual(first, excerpt(source, self.WEIRD, budget))
                    self.assertLessEqual(estimate_tokens(first[0]), budget)

    def test_deep_nesting(self) -> None:
        nested = sym("n39", CLASS, 40, 0, "class n39")
        for depth in range(38, -1, -1):
            name = f"n{depth}"
            nested = sym(name, CLASS, depth + 1, 0, f"class {name}", "", nested)
        source = "".join(f"{'    ' * depth}class n{depth}:\n" for depth in range(40))
        source += f"{'    ' * 40}x = 1\n" * 200
        body = "    " * 40
        bare, cut = excerpt(source, facts_of(nested), 1100)
        self.assertTrue(cut)
        self.assertEqual(
            bare.split("\n")[-2:],
            [f"{'    ' * 39}class n39:", f"{body}... (200 lines omitted)"],
        )
        # Depth 39 needs 2**39 of allowance per body line; two lines fit here.
        wider = excerpt(source, facts_of(nested), 1200)[0]
        self.assertEqual(
            wider.split("\n")[-3:],
            [f"{body}x = 1", f"{body}x = 1", f"{body}... (198 lines omitted)"],
        )


def graph_of(
    files: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]],
    language: str = "python",
) -> ProjectGraph:
    """A graph assembled the way GraphBuilder does it, from relpath ->
    (internal imports, external imports, entry point); `tests/` holds tests."""
    importers: dict[str, list[str]] = {relpath: [] for relpath in files}
    for relpath in sorted(files):
        for target in files[relpath][0]:
            importers[target].append(relpath)
    nodes: dict[str, FileNode] = {}
    for relpath in sorted(files):
        internal, external, entry = files[relpath]
        inbound = importers[relpath]
        nodes[relpath] = FileNode(
            relpath=relpath,
            language=language,
            module=posixpath.dirname(relpath),
            internal=tuple(sorted(internal)),
            external=tuple(sorted(external)),
            dependents=tuple(r for r in inbound if not r.startswith("tests/")),
            tested_by=tuple(r for r in inbound if r.startswith("tests/")),
            entry_point=entry,
        )
    members: dict[str, list[str]] = {}
    depends: dict[str, set[str]] = {}
    users: dict[str, set[str]] = {}
    for relpath, node in nodes.items():
        members.setdefault(node.module, []).append(relpath)
        for target in node.internal:
            other = nodes[target].module
            if other != node.module:
                depends.setdefault(node.module, set()).add(other)
                users.setdefault(other, set()).add(node.module)
    modules = {
        path: ModuleNode(
            path,
            tuple(members[path]),
            tuple(sorted(depends.get(path, ()))),
            tuple(sorted(users.get(path, ()))),
        )
        for path in sorted(members)
    }
    return ProjectGraph(files=nodes, modules=modules)


SHOP = graph_of({
    "manage.py": (("api/routes.py",), ("os", "sys"), "`__main__` block"),
    "api/routes.py": (("shop/cart.py",), ("flask", "json"), "Flask app `app`"),
    "lib/money.py": ((), ("babel",), ""),
    "shop/__main__.py": (("shop/checkout.py",), (), "runs `python -m shop`"),
    "shop/cart.py": (
        ("lib/money.py", "shop/models.py"), ("decimal", "requests"), ""
    ),
    "shop/checkout.py": (
        ("lib/money.py", "shop/cart.py", "shop/models.py", "shop/payments/stripe.py"),
        ("flask", "requests"),
        "",
    ),
    "shop/models.py": ((), (), ""),
    "shop/payments/stripe.py": (("shop/models.py",), ("stripe",), ""),
    "shop/admin/views/orders.py": (("shop/models.py",), (), ""),
    "tests/test_cart.py": (("shop/cart.py",), ("pytest",), ""),
})

SHOP_FACTS = {
    "shop/cart.py": facts_of(
        sym("Cart", CLASS, 12),
        sym("MAX_ITEMS", CONSTANT, 5),
        sym("DEFAULT_QTY", CONSTANT, 6),
        sym("Item", CLASS, 40),
        sym("load", FUNCTION, 60),
        sym("save", FUNCTION, 70),
        sym("merge", FUNCTION, 80),
        sym("total", FUNCTION, 90),
        sym("_dirty", FUNCTION, 100, public=False),
        module_docstring="Cart storage and pricing.",
    ),
    "shop/checkout.py": facts_of(sym("checkout", FUNCTION, 3)),
    "shop/models.py": facts_of(
        sym("Order", CLASS, 5),
        sym("Line", CLASS, 20),
        sym("helper", FUNCTION, 40),
        exports=("Order", "Line"),
        module_docstring="Order and item records.\n\nEach record is immutable.",
    ),
}

SHOP_SUMMARIES = {"shop/cart.py": "  Keeps the items a customer\n intends to buy. "}

SHOP_MODULE = ModulePlan(
    "shop",
    ("shop/__main__.py", "shop/cart.py", "shop/checkout.py", "shop/models.py"),
    6.5,
    Mode.PROSE,
    "ranked 1 of 2",
)


class FilePromptTest(unittest.TestCase):
    NODE = FileNode(
        relpath="shop/cart.py",
        language="python",
        module="shop",
        internal=("shop/models.py", "lib/money.py"),
        external=("requests", "json"),
        dependents=(
            "shop/reports.py", "api/orders.py", "api/cart.py", "cli/main.py",
            "api/users.py", "shop/checkout.py", "api/admin.py", "web/views.py",
            "api/checkout.py", "jobs/nightly.py",
        ),
        tested_by=(
            "tests/test_cart.py", "tests/test_api.py", "tests/test_load.py",
            "tests/test_checkout.py", "tests/test_reports.py",
        ),
    )

    def test_full_prompt(self) -> None:
        builder = PromptBuilder(project="shop", description="Online shop backend")
        plan = FilePlan(
            "shop/cart.py", 7.5, 0.6, Mode.PROSE, "ranked 1 of 9", ("Cart.total",)
        )
        user = "\n".join([
            "Project: shop — Online shop backend",
            "File: shop/cart.py (python, 45 lines)",
            "Imported by: api/admin.py, api/cart.py, api/checkout.py, api/orders.py, "
            "api/users.py, cli/main.py, jobs/nightly.py, shop/checkout.py "
            "and 2 more",
            "Tested by: tests/test_api.py, tests/test_cart.py, "
            "tests/test_checkout.py, tests/test_load.py and 1 more",
            "Imports: lib/money.py, shop/models.py",
            "External: json, requests",
            "Exports: Cart, load, MAX_ITEMS",
            "Describe: Cart.total",
            "",
            "Source:",
            "```python",
            CART_TEXT,
            "```",
        ])
        self.assertEqual(
            builder.file(plan, CART_FACTS, CART, self.NODE),
            Prompt(
                kind="file",
                key="shop/cart.py",
                tier=Tier.FILE,
                system=system_prompt("file"),
                user=user,
                max_output_tokens=256,
            ),
        )

    def test_trimmed_degraded_entry_point(self) -> None:
        builder = PromptBuilder(project="shop", source_tokens=145)
        facts = dataclasses.replace(
            CART_FACTS,
            path=Path("/home/dev/shop/bin/sync.py"),
            exports=(),
            confidence=Confidence.DEGRADED,
            notes=[
                "ast failed at line 3 (invalid syntax); signatures recovered by "
                "token scan, docstrings unavailable",
                " ",
            ],
        )
        node = FileNode(
            "bin/sync.py", "python", "bin", entry_point="console script\n  `shop-sync`"
        )
        plan = FilePlan("bin/sync.py", 4.0, 1.0, Mode.PROSE, "ranked 2 of 9")
        prompt = builder.file(plan, facts, CART, node)
        self.assertEqual(prompt.user, "\n".join([
            "Project: shop",
            "File: bin/sync.py (python, 45 lines)",
            "Entry point: console script `shop-sync`",
            "Parse: degraded; ast failed at line 3 (invalid syntax); signatures "
            "recovered by token scan, docstrings unavailable",
            "Describe: (none — write no SYMBOL lines)",
            "",
            "Source (excerpt; bodies trimmed to fit):",
            "```python",
            CART_BRIEF,
            "```",
        ]))
        self.assertEqual(prompt.max_output_tokens, 256)
        self.assertNotIn("/home/dev", prompt.user)

    def test_fence_outgrows_backtick_runs(self) -> None:
        plan = FilePlan(
            "docs/snippet", 0.5, 0.25, Mode.PROSE, "r", ("a", "b", "c", "d", "e")
        )
        source = "Use ````code```` fences.\nSecond line\n"
        prompt = PromptBuilder(project="shop").file(
            plan, facts_of(language=""), source, None
        )
        self.assertEqual(prompt.user, "\n".join([
            "Project: shop",
            "File: docs/snippet (3 lines)",
            "Describe: a, b, c, d, e",
            "",
            "Source:",
            "`````",
            "Use ````code```` fences.",
            "Second line",
            "`````",
        ]))
        self.assertEqual(prompt.max_output_tokens, 425)

    def test_empty_source_and_project(self) -> None:
        plan = FilePlan("empty.py", 0.0, 0.25, Mode.PROSE, "r", ("x",))
        prompt = PromptBuilder(project=" ").file(plan, facts_of(), "", None)
        self.assertEqual(
            prompt.user,
            "File: empty.py (python, 0 lines)\nDescribe: x\n\nSource:\n```python\n```",
        )

    def test_output_cap_grows_with_symbols_to_describe(self) -> None:
        builder = PromptBuilder(project="shop")
        for count, cap in ((0, 256), (1, 256), (2, 290), (20, 1100), (27, 1400)):
            with self.subTest(count=count):
                names = tuple(f"f{index}" for index in range(count))
                plan = FilePlan("m.py", 1.0, 1.0, Mode.PROSE, "r", names)
                prompt = builder.file(plan, facts_of(loc=1), "x = 1\n", None)
                self.assertEqual(prompt.max_output_tokens, cap)
                self.assertIn("File: m.py (python, 1 line)\n", prompt.user)

    def test_identical_inputs_identical_prompt(self) -> None:
        plan = FilePlan("shop/cart.py", 7.5, 0.6, Mode.PROSE, "r", ("Cart.total",))
        first = PromptBuilder(project="shop", source_tokens=150).file(
            plan, CART_FACTS, CART, self.NODE
        )
        second = PromptBuilder(project="shop", source_tokens=150).file(
            plan, CART_FACTS, CART.replace("\n", "\r\n"), self.NODE
        )
        self.assertEqual(first, second)


class ModulePromptTest(unittest.TestCase):

    def test_full_prompt(self) -> None:
        builder = PromptBuilder(project="shop", description="Online shop backend")
        prompt = builder.module(SHOP_MODULE, SHOP, SHOP_SUMMARIES, SHOP_FACTS)
        user = "\n".join([
            "Project: shop — Online shop backend",
            "Module: shop",
            "Files:",
            "- __main__.py: (no summary)",
            "- cart.py: Keeps the items a customer intends to buy; defines: Cart, "
            "Item, load, save, merge, total and 2 more",
            "- checkout.py: (no summary); defines: checkout",
            "- models.py: Order and item records; defines: Order, Line",
            "Internal imports:",
            "- __main__.py -> checkout.py",
            "- cart.py -> models.py",
            "- checkout.py -> cart.py",
            "- checkout.py -> models.py",
            "Depends on: lib, shop/payments",
            "Used by: api, shop/admin/views, shop/payments, tests",
            "Entry points:",
            "- __main__.py — runs `python -m shop`",
            "Submodules: shop/admin/views, shop/payments",
        ])
        self.assertEqual(
            prompt,
            Prompt(
                kind="module",
                key="shop",
                tier=Tier.MODULE,
                system=system_prompt("module"),
                user=user,
                max_output_tokens=600,
            ),
        )

    def test_project_root(self) -> None:
        plan = ModulePlan("", ("manage.py",), 1.0, Mode.PROSE, "ranked 2 of 2")
        prompt = PromptBuilder(project="shop").module(plan, SHOP, {}, {})
        self.assertEqual(prompt.key, "")
        self.assertEqual(prompt.user, "\n".join([
            "Project: shop",
            "Module: (project root)",
            "Files:",
            "- manage.py: (no summary)",
            "Depends on: api",
            "Entry points:",
            "- manage.py — `__main__` block",
            "Submodules: api, lib, shop, tests",
        ]))
        used = PromptBuilder(project="shop").module(
            ModulePlan("api", ("api/routes.py",), 1.0, Mode.PROSE, "r"), SHOP, {}, {}
        )
        self.assertIn("\nUsed by: (project root)\n", used.user)

    def test_large_directory_lists_its_most_imported_files(self) -> None:
        files: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
            f"big/f{index:03d}.py": ((), (), "") for index in range(103)
        }
        files["app/main.py"] = (("big/f100.py", "big/f101.py", "big/f102.py"), (), "")
        files["app/other.py"] = (("big/f101.py", "big/f102.py"), (), "")
        files["app/third.py"] = (("big/f102.py",), (), "")
        graph = graph_of(files)
        plan = ModulePlan(
            "big", tuple(sorted(r for r in files if r.startswith("big/"))),
            1.0, Mode.PROSE, "r",
        )
        prompt = PromptBuilder(project="shop").module(plan, graph, {}, {})
        shown = [*range(97), 100, 101, 102]
        self.assertEqual(prompt.user, "\n".join([
            "Project: shop",
            "Module: big",
            "Files:",
            *(f"- f{index:03d}.py: (no summary)" for index in shown),
            "- and 3 more",
            "Used by: app",
        ]))

    def test_lists_are_capped(self) -> None:
        chain = [f"hub/c{index:02d}.py" for index in range(32)]
        deps = tuple(f"dep{index:02d}/x.py" for index in range(14))
        subs = [f"hub/sub{index:02d}" for index in range(22)]
        files: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
            relpath: (tuple(chain[index + 1 : index + 2]), (), f"script {index}")
            for index, relpath in enumerate(chain)
        }
        files[chain[-1]] = (deps, (), "script 31")
        files.update(dict.fromkeys(deps, ((), (), "")))
        files.update({f"{sub}/y.py": ((chain[0],), (), "") for sub in subs})
        plan = ModulePlan("hub", tuple(chain), 1.0, Mode.PROSE, "r")
        user = PromptBuilder(project="shop").module(plan, graph_of(files), {}, {}).user
        rows = user.split("\n")
        imports = rows.index("Internal imports:")
        self.assertEqual(rows[imports + 1], "- c00.py -> c01.py")
        self.assertEqual(rows[imports + 30], "- c29.py -> c30.py")
        self.assertEqual(rows[imports + 31], "- and 1 more")
        names = ", ".join(posixpath.dirname(dep) for dep in deps[:12])
        self.assertIn(f"Depends on: {names} and 2 more", rows)
        self.assertIn(f"Used by: {', '.join(subs[:12])} and 10 more", rows)
        entries = rows.index("Entry points:")
        self.assertEqual(rows[entries + 15], "- c14.py — script 14")
        self.assertEqual(rows[entries + 16], "- and 17 more")
        self.assertEqual(rows[-1], f"Submodules: {', '.join(subs[:20])} and 2 more")

    def test_files_outside_the_graph(self) -> None:
        plan = ModulePlan(
            "scripts", ("scripts/a.py", "scripts/b.py"), 0.5, Mode.PROSE, "r"
        )
        summaries = {"scripts/b.py": "Seeds the database."}
        prompt = PromptBuilder(project="shop").module(plan, SHOP, summaries, {})
        self.assertEqual(
            prompt.user,
            "Project: shop\nModule: scripts\nFiles:\n- a.py: (no summary)\n"
            "- b.py: Seeds the database.",
        )

    def test_submodule_index_follows_the_graph(self) -> None:
        builder = PromptBuilder(project="shop")
        plan = ModulePlan("shop", ("shop/cart.py",), 1.0, Mode.PROSE, "r")
        for _ in range(2):
            self.assertTrue(builder.module(plan, SHOP, {}, {}).user.endswith(
                "Submodules: shop/admin/views, shop/payments"
            ))
        other = graph_of(
            {"shop/cart.py": ((), (), ""), "shop/tax/vat.py": ((), (), "")}
        )
        self.assertTrue(builder.module(plan, other, {}, {}).user.endswith(
            "Submodules: shop/tax"
        ))

    def test_insertion_order_does_not_matter(self) -> None:
        shuffled = ProjectGraph(
            files=dict(reversed(list(SHOP.files.items()))),
            modules=dict(reversed(list(SHOP.modules.items()))),
        )
        for graph in (SHOP, shuffled):
            builder = PromptBuilder(project="shop")
            self.assertEqual(
                builder.module(SHOP_MODULE, graph, SHOP_SUMMARIES, SHOP_FACTS),
                PromptBuilder(project="shop").module(
                    SHOP_MODULE, SHOP, SHOP_SUMMARIES, SHOP_FACTS
                ),
            )
            self.assertEqual(
                builder.architecture(graph, {}, {"python": 9}),
                PromptBuilder(project="shop").architecture(SHOP, {}, {"python": 9}),
            )


class ArchitecturePromptTest(unittest.TestCase):
    def test_full_prompt(self) -> None:
        builder = PromptBuilder(project="shop", description="Online shop backend")
        summaries = {
            "shop": "Carts, checkout and payment for the storefront.",
            "api": "HTTP routes\nfor the storefront.",
        }
        prompt = builder.architecture(SHOP, summaries, {"python": 9, "go": 0})
        user = "\n".join([
            "Project: shop — Online shop backend",
            "Languages: python (9 files)",
            "Components:",
            "- (project root): 1 file",
            "- api: HTTP routes for the storefront; 1 file",
            "- lib: 1 file",
            "- shop: Carts, checkout and payment for the storefront; 4 files",
            "- shop/admin/views: 1 file",
            "- shop/payments: 1 file",
            "- tests: 1 file",
            "Component dependencies:",
            "- shop -> lib (2 imports)",
            "- (project root) -> api (1 import)",
            "- api -> shop (1 import)",
            "- shop -> shop/payments (1 import)",
            "- shop/admin/views -> shop (1 import)",
            "- shop/payments -> shop (1 import)",
            "- tests -> shop (1 import)",
            "Entry points:",
            "- api/routes.py — Flask app `app`",
            "- manage.py — `__main__` block",
            "- shop/__main__.py — runs `python -m shop`",
            "External packages: flask, requests, babel, pytest, stripe",
        ])
        self.assertEqual(
            prompt,
            Prompt(
                kind="architecture",
                key="",
                tier=Tier.ARCHITECTURE,
                system=system_prompt("architecture"),
                user=user,
                max_output_tokens=1500,
            ),
        )

    def test_modules_collapse_into_components(self) -> None:
        files: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
            "src/cli/main.py": (
                ("src/core/run.py", "src/web/server.py"), (), "console script `app`"
            ),
            "src/core/run.py": (
                ("src/core/io/read.py", "src/core/parse/lex.py"), (), ""
            ),
            "src/core/io/read.py": ((), (), ""),
            "src/core/parse/lex.py": (("src/core/io/read.py",), (), ""),
            "src/web/server.py": (("src/web/api/routes.py", "src/core/run.py"), (), ""),
            "src/web/api/routes.py": (
                ("src/web/views/page.py", "src/core/io/read.py"), (), ""
            ),
            "src/web/views/page.py": ((), (), ""),
        }
        for index in range(1, 9):
            files[f"src/plugins/p{index}/plugin.py"] = (("src/core/run.py",), (), "")
        prompt = PromptBuilder(project="app").architecture(
            graph_of(files), {"src/core": "Runs jobs."},
            {"go": 1, "typescript": 2, "python": 15, "rust": 2},
        )
        self.assertEqual(prompt.user, "\n".join([
            "Project: app",
            "Languages: python (15 files), rust (2 files), typescript (2 files), "
            "go (1 file)",
            "Components:",
            "- src/cli: 1 file",
            "- src/core: Runs jobs; 3 files",
            "- src/plugins: 8 files",
            "- src/web: 3 files",
            "Component dependencies:",
            "- src/plugins -> src/core (8 imports)",
            "- src/web -> src/core (2 imports)",
            "- src/cli -> src/core (1 import)",
            "- src/cli -> src/web (1 import)",
            "Entry points:",
            "- src/cli/main.py — console script `app`",
        ]))

    def test_dependencies_and_entry_points_are_capped(self) -> None:
        names = [f"m{index:02d}" for index in range(12)]
        files: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
            f"{name}/a.py": (
                tuple(f"{other}/a.py" for other in names if other != name),
                (),
                f"entry {name}",
            )
            for name in names
        }
        for index in range(5):
            files[f"m00/b{index}.py"] = ((), (), f"extra {index}")
        rows = PromptBuilder(project="p").architecture(graph_of(files), {}, {}).user
        lines = rows.split("\n")
        edges = lines.index("Component dependencies:")
        self.assertEqual(lines[edges + 1], "- m00 -> m01 (1 import)")
        self.assertEqual(lines[edges + 40], "- m03 -> m07 (1 import)")
        self.assertEqual(lines[edges + 41], "- and 92 more")
        entries = lines.index("Entry points:")
        self.assertEqual(lines[entries + 1], "- m00/a.py — entry m00")
        self.assertEqual(lines[entries + 2], "- m00/b0.py — extra 0")
        self.assertEqual(lines[entries + 15], "- m09/a.py — entry m09")
        self.assertEqual(lines[entries + 16], "- and 2 more")
        self.assertEqual(lines[-1], "- and 2 more")

    def test_external_packages_by_importing_files_per_language(self) -> None:
        nodes = {
            node.relpath: node
            for node in (
                FileNode("a.py", "python", "", external=(
                    "numpy", "os", "numpy.linalg", "path"
                )),
                FileNode("b.py", "python", "", external=("numpy", "numpy")),
                FileNode("web/app.ts", "typescript", "web", external=(
                    "node:fs", "fs", "react", "@scope/ui"
                )),
                FileNode("web/b.ts", "typescript", "web", external=("react", "path")),
                FileNode("cmd/main.go", "go", "cmd", external=(
                    "fmt", "net/http", "github.com/spf13/cobra"
                )),
                FileNode("src/lib.rs", "rust", "src", external=(
                    "std::collections", "serde"
                )),
                FileNode("Main.java", "java", "", external=(
                    "java.util.List", "com.google.gson.Gson"
                )),
            )
        }
        graph = ProjectGraph(files=nodes)
        prompt = PromptBuilder(project="p").architecture(graph, {}, {})
        self.assertEqual(
            prompt.user,
            "Project: p\nExternal packages: numpy, react, @scope/ui, "
            "com.google.gson.Gson, github.com/spf13/cobra, numpy.linalg, path, serde",
        )
        many = ProjectGraph(files={
            f"f{index}.py": FileNode(
                f"f{index}.py", "python", "",
                external=tuple(f"pkg{n:02d}" for n in range(index + 1)),
            )
            for index in range(20)
        })
        packages = PromptBuilder(project="p").architecture(many, {}, {}).user
        self.assertEqual(
            packages.split("\n")[-1],
            "External packages: " + ", ".join(f"pkg{n:02d}" for n in range(15)),
        )

    def test_empty_graph(self) -> None:
        prompt = PromptBuilder(project="shop").architecture(ProjectGraph(), {}, {})
        self.assertEqual(prompt.user, "Project: shop")
        self.assertEqual(
            PromptBuilder(project="", description="").architecture(
                ProjectGraph(), {}, {"python": 0}
            ).user,
            "",
        )


class ContextTest(unittest.TestCase):
    """Retrieved context goes into the prompt but never into its identity."""

    USE = Snippet("api/orders.py", 12, "cart.total()", "uses", "Cart")
    DEF = Snippet("lib/money.py", 3, "class Money(int)", "defines", "Money")
    PLAN = FilePlan("shop/cart.py", 7.5, 0.6, Mode.PROSE, "r", ("Cart.total",))

    def test_file_context_sits_between_the_facts_and_the_source(self) -> None:
        builder = PromptBuilder(project="shop")
        node = FilePromptTest.NODE
        plain = builder.file(self.PLAN, CART_FACTS, CART, node)
        rich = builder.file(self.PLAN, CART_FACTS, CART, node, [self.USE, self.DEF])
        block = "\n\n" + "\n".join([
            "How other files use this one, and what it calls (for its role; "
            "do not describe them):",
            "- api/orders.py:12 uses Cart: cart.total()",
            "- lib/money.py:3 defines Money: class Money(int)",
        ])
        head, _, tail = plain.user.partition("\n\nSource:")
        self.assertEqual(rich.user, f"{head}{block}\n\nSource:{tail}")
        self.assertEqual(rich.cache_text, plain.user)
        self.assertEqual((plain.identity, plain.cache_text), ("", plain.user))

    def test_changed_context_keeps_the_identity(self) -> None:
        builder = PromptBuilder(project="shop")
        node = FilePromptTest.NODE
        moved = dataclasses.replace(self.USE, line=40)
        first = builder.file(self.PLAN, CART_FACTS, CART, node, [self.USE])
        second = builder.file(self.PLAN, CART_FACTS, CART, node, [moved])
        self.assertNotEqual(first.user, second.user)
        self.assertEqual(first.cache_text, second.cache_text)

    def test_module_context_comes_last(self) -> None:
        builder = PromptBuilder(project="shop")
        related = Snippet("web/views.py", 8, "def checkout(request):", "related",
                          "checkout")
        plain = builder.module(SHOP_MODULE, SHOP, SHOP_SUMMARIES, SHOP_FACTS)
        rich = builder.module(SHOP_MODULE, SHOP, SHOP_SUMMARIES, SHOP_FACTS, [related])
        self.assertEqual(rich.user, plain.user + "\n\n" + "\n".join([
            "Related code elsewhere (reference only; do not describe it):",
            "- web/views.py:8 related checkout: def checkout(request):",
        ]))
        self.assertEqual(rich.cache_text, plain.user)

    def test_entry_point_code_is_fenced_past_its_backticks(self) -> None:
        body = 'def main():\n    print("```")'
        entry = Snippet("shop/cli.py", 4, body, "entry", "main")
        prompt = PromptBuilder(project="p").architecture(
            ProjectGraph(), {}, {}, [entry]
        )
        self.assertEqual(prompt.user, "\n".join([
            "Project: p",
            "",
            "Entry point code (shop/cli.py:4; FLOW follows its order):",
            "````",
            body,
            "````",
        ]))
        self.assertEqual(prompt.cache_text, "Project: p")


if __name__ == "__main__":
    unittest.main()
