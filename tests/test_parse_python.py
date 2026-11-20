"""Python facts: exact via ast, and honest when ast cannot parse."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from devdocs.parse import (
    Confidence,
    FileFacts,
    Symbol,
    SymbolKind,
    get_parser,
    supported_languages,
)
from devdocs.parse.python import PythonParser, _reuses_quote

ROOT = Path(__file__).resolve().parent.parent
# Python 2's print statement: every ast rejects it and tokenize accepts it, so
# appending it forces the token fallback on any interpreter.
PY2 = 'print "py2"\n'
CLASS, METHOD = SymbolKind.CLASS, SymbolKind.METHOD
FUNCTION, CONSTANT, TYPE = SymbolKind.FUNCTION, SymbolKind.CONSTANT, SymbolKind.TYPE

SAMPLE = '''"""Module docs."""
from os.path import join
import a.b.c
from . import sibling

__all__ = ["Alpha", "run"]

MAX_SIZE = 100
_PRIVATE = 1

class Alpha(Base, Mixin):
    """Alpha does things."""

    @property
    def method(self, x: int) -> str:
        return str(x)

    async def amethod(self): ...

    def multi(self,
              a: dict[str, int] = {"k": 1},
              key=lambda v: v) -> list[str]:
        pass

def run(a: int, b: str = "x") -> bool:
    """Run it."""
    return True

def _helper(): pass
'''

BLOCKS = '''import sys

class Article(models.Model):
    class Meta:
        ordering = ["-created"]

class Settings(BaseSettings):
    class Config:
        env_prefix = "APP_"

        def customise(cls): ...

if sys.version_info >= (3, 9):
    def removeprefix(s, p):
        return s.removeprefix(p)
else:
    def removeprefix(s, p):
        return s[len(p):] if s.startswith(p) else s

try:
    from functools import cache
except ImportError:
    def cache(fn):
        return fn
'''

ALIASES = "type Vector = list[float]\ntype Pair[T] = tuple[\n    T, T]  # c\n"


def parse(source: str, name: str = "m.py") -> FileFacts:
    return PythonParser().parse(Path(name), source)


def signatures(facts: FileFacts) -> dict[str, str]:
    return {symbol.name: symbol.signature for symbol in facts.all_symbols}


def outline(symbols: Sequence[Symbol]) -> list[tuple[object, ...]]:
    return [
        (s.name, s.kind, s.line, s.end_line, s.signature, outline(s.children))
        for s in symbols
    ]


def shape(symbols: Sequence[Symbol]) -> list[tuple[object, ...]]:
    """Every field the token scan can know, children included."""
    return [
        (s.name, s.kind, s.line, s.end_line, s.signature, s.is_public, s.is_async,
         shape(s.children))
        for s in symbols
    ]


def fresh_python(script: str) -> str:
    """Stdout of `script` in a new interpreter, where nothing is loaded yet."""
    path = os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": path},
    )
    return result.stdout + result.stderr


class TestRegistry(unittest.TestCase):
    def test_python_is_registered(self) -> None:
        self.assertIn("python", supported_languages())
        self.assertIsInstance(get_parser("python"), PythonParser)

    def test_unknown_language_is_none(self) -> None:
        self.assertIsNone(get_parser("cobol"))

    def test_concurrent_first_use_waits_for_the_full_registry(self) -> None:
        output = fresh_python(
            "import threading\n"
            "from devdocs.parse import get_parser\n"
            "barrier = threading.Barrier(16)\n"
            "found = []\n"
            "def first_use():\n"
            "    barrier.wait()\n"
            "    found.append(type(get_parser('python')).__name__)\n"
            "threads = [threading.Thread(target=first_use) for _ in range(16)]\n"
            "for thread in threads:\n"
            "    thread.start()\n"
            "for thread in threads:\n"
            "    thread.join()\n"
            "print(found.count('PythonParser'), len(found))\n"
        )
        self.assertEqual(output.strip(), "16 16")

    def test_a_failed_load_is_retried(self) -> None:
        output = fresh_python(
            "import sys\n"
            "from devdocs.parse import get_parser\n"
            "sys.modules['devdocs.parse.languages'] = None\n"
            "try:\n"
            "    get_parser('python')\n"
            "except ImportError:\n"
            "    print('first load failed')\n"
            "del sys.modules['devdocs.parse.languages']\n"
            "print(type(get_parser('python')).__name__)\n"
        )
        self.assertEqual(output.split(), ["first", "load", "failed", "PythonParser"])


class TestExact(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = PythonParser().parse(Path("m.py"), SAMPLE)
        self.by_name = {s.name: s for s in self.facts.all_symbols}

    def test_confidence_and_docstring(self) -> None:
        self.assertIs(self.facts.confidence, Confidence.EXACT)
        self.assertEqual(self.facts.module_docstring, "Module docs.")

    def test_imports_resolve_to_the_specific_module(self) -> None:
        self.assertEqual(self.facts.imports, (".sibling", "a.b.c", "os.path.join"))

    def test_dunder_all_defines_public_surface(self) -> None:
        self.assertEqual(self.facts.exports, ("Alpha", "run"))
        self.assertEqual([s.name for s in self.facts.public_symbols], ["Alpha", "run"])

    def test_kinds(self) -> None:
        self.assertIs(self.by_name["Alpha"].kind, SymbolKind.CLASS)
        self.assertIs(self.by_name["method"].kind, SymbolKind.METHOD)
        self.assertIs(self.by_name["run"].kind, SymbolKind.FUNCTION)
        self.assertIs(self.by_name["MAX_SIZE"].kind, SymbolKind.CONSTANT)

    def test_private_constants_are_omitted(self) -> None:
        self.assertNotIn("_PRIVATE", self.by_name)

    def test_signatures_are_verbatim(self) -> None:
        self.assertEqual(self.by_name["run"].signature,
                         'def run(a: int, b: str = "x") -> bool')
        self.assertEqual(self.by_name["Alpha"].signature, "class Alpha(Base, Mixin)")

    def test_one_line_body_does_not_leak_into_signature(self) -> None:
        self.assertEqual(self.by_name["amethod"].signature, "async def amethod(self)")

    def test_colons_inside_brackets_do_not_end_signature(self) -> None:
        expected = (
            'def multi(self, a: dict[str, int] = {"k": 1}, '
            "key=lambda v: v) -> list[str]"
        )
        self.assertEqual(self.by_name["multi"].signature, expected)

    def test_decorators_bases_async_and_docstrings(self) -> None:
        self.assertEqual(self.by_name["method"].decorators, ("property",))
        self.assertEqual(self.by_name["Alpha"].bases, ("Base", "Mixin"))
        self.assertTrue(self.by_name["amethod"].is_async)
        self.assertEqual(self.by_name["run"].docstring, "Run it.")

    def test_visibility(self) -> None:
        self.assertFalse(self.by_name["_helper"].is_public)
        self.assertTrue(self.by_name["run"].is_public)

    def test_symbol_names_for_grounding(self) -> None:
        self.assertLessEqual({"Alpha", "method", "run", "MAX_SIZE"},
                             set(self.facts.symbol_names))

    def test_line_numbers(self) -> None:
        self.assertEqual(self.by_name["Alpha"].line, 11)
        self.assertGreater(self.by_name["Alpha"].end_line, self.by_name["Alpha"].line)


class TestSignatures(unittest.TestCase):
    """Headers come from tokens: no comments, strings verbatim, no length cap."""

    def test_comments_drop_out_and_strings_stay_verbatim(self) -> None:
        facts = parse(
            "def connect(\n"
            "    host: str,  # hostname or IP\n"
            "    port: int = 5432,  # default port\n"
            ") -> None:\n"
            "    pass\n"
            "\n"
            'def indent(text, prefix="    "):\n'
            "    return prefix + text\n"
            "\n"
            'def closer_for(ch="("):\n'
            '    if ch == ")":\n'
            "        return 1\n"
        )
        self.assertEqual(
            signatures(facts),
            {
                "connect": "def connect( host: str, port: int = 5432, ) -> None",
                "indent": 'def indent(text, prefix="    ")',
                "closer_for": 'def closer_for(ch="(")',
            },
        )

    def test_no_string_lambda_or_fstring_ends_a_header(self) -> None:
        facts = parse(
            'def f(a="#not a comment", b=f"{x!r:>{w}} {{(}}", *,\n'
            "      key=lambda v: v[0],  # trailing\n"
            '      c="""two\n'
            '   lines""") -> "A:B":\n'
            "    pass\n"
            "def g() -> lambda: 1: pass\n"
        )
        self.assertEqual(
            signatures(facts),
            {
                "f": 'def f(a="#not a comment", b=f"{x!r:>{w}} {{(}}", *, '
                'key=lambda v: v[0], c="""two\n   lines""") -> "A:B"',
                "g": "def g() -> lambda: 1",
            },
        )

    def test_long_headers_are_read_to_their_colon(self) -> None:
        params = "".join(f"    option_{i}: int | None = None,\n" for i in range(35))
        facts = parse(
            "def read_table(\n    path: str,\n" + params + ") -> list[dict]:\n"
            "    return []\n"
        )
        expected = (
            "def read_table( path: str, "
            + "".join(f"option_{i}: int | None = None, " for i in range(35))
            + ") -> list[dict]"
        )
        self.assertEqual(
            outline(facts.symbols), [("read_table", FUNCTION, 1, 39, expected, [])]
        )

    def test_the_first_body_statement_bounds_the_header(self) -> None:
        facts = parse(
            'class C(Base):  # "quoted"\n'
            "    @decorator(\n"
            '        "x")\n'
            "    def m(self): ...\n"
        )
        self.assertEqual(signatures(facts), {"C": "class C(Base)", "m": "def m(self)"})

    def test_lines_are_counted_as_the_compiler_counts_them(self) -> None:
        facts = parse(
            "import functools\n"
            "\x0c\n"
            "class Store:\n"
            "    def get(self, key):\n"
            "        return key\n"
            "\n"
            "    def put(self, key, value):\n"
            "        pass\n"
            "\x0c\n"
            "@functools.cache\n"
            "def lookup(name):\n"
            "    return name\n"
            "\n"
            "LIMIT = 10\n"
        )
        self.assertEqual(
            {s.name: (s.line, s.signature) for s in facts.all_symbols},
            {
                "Store": (3, "class Store"),
                "get": (4, "def get(self, key)"),
                "put": (7, "def put(self, key, value)"),
                "lookup": (11, "def lookup(name)"),
                "LIMIT": (14, "LIMIT = 10"),
            },
        )

    def test_unicode_line_separators_are_not_line_breaks(self) -> None:
        facts = parse(
            'SEPARATORS = "\x0b\x1c\x1d\x1e\x85\u2028\u2029"  # \u2028\n'
            "def after(x):\n"
            "    return x\n"
        )
        self.assertEqual(
            outline(facts.symbols),
            [
                ("after", FUNCTION, 2, 3, "def after(x)", []),
                ("SEPARATORS", CONSTANT, 1, 1,
                 'SEPARATORS = "\x0b\x1c\x1d\x1e\x85\u2028\u2029"', []),
            ],
        )

    def test_windows_and_classic_mac_line_endings(self) -> None:
        for newline in ("\r\n", "\r"):
            with self.subTest(newline=newline):
                facts = parse(newline.join(["X = 1", "def f(a,", "      b):",
                                            "    pass", ""]))
                self.assertEqual(
                    outline(facts.symbols),
                    [("f", FUNCTION, 2, 4, "def f(a, b)", []),
                     ("X", CONSTANT, 1, 1, "X = 1", [])],
                )
                self.assertEqual(facts.loc, 4)

    def test_a_leading_bom_is_valid_python(self) -> None:
        body = (
            '"""Billing helpers."""\n'
            "from decimal import Decimal\n"
            "\n"
            '__all__ = ["Invoice"]\n'
            "\n"
            "class Invoice:\n"
            '    """An invoice."""\n'
            "\n"
            "    def total(self) -> Decimal:\n"
            "        return Decimal(0)\n"
        )
        facts = parse("\ufeff" + body, "invoice.py")
        self.assertIs(facts.confidence, Confidence.EXACT)
        self.assertEqual(facts.module_docstring, "Billing helpers.")
        self.assertEqual(facts.imports, ("decimal.Decimal",))
        self.assertEqual(facts.exports, ("Invoice",))
        self.assertEqual(
            outline(facts.symbols),
            [("Invoice", CLASS, 6, 10, "class Invoice",
              [("total", METHOD, 9, 10, "def total(self) -> Decimal", [])])],
        )
        self.assertEqual(facts.loc, 10)

    def test_a_constant_signature_is_its_first_line(self) -> None:
        facts = parse(
            "TABLE = {  # the table\n"
            '    "a": 1,\n'
            "}\n"
            'DOC = """first\n'
            'second"""\n'
            "PAIR = (1,\n"
            "        2)\n"
            'A = 1; B = "x;y"\n'
            "C: int\n"
        )
        self.assertEqual(
            outline(facts.symbols),
            [
                ("TABLE", CONSTANT, 1, 3, "TABLE = {", []),
                ("DOC", CONSTANT, 4, 5, 'DOC = """first', []),
                ("PAIR", CONSTANT, 6, 7, "PAIR = (1,", []),
                ("A", CONSTANT, 8, 8, "A = 1", []),
                ("B", CONSTANT, 8, 8, 'B = "x;y"', []),
                ("C", CONSTANT, 9, 9, "C: int", []),
            ],
        )

    def test_a_huge_one_line_header_stays_linear(self) -> None:
        params = ", ".join(f"a{i}: int" for i in range(25_000))
        start = time.perf_counter()
        facts = parse(f"def f({params}): pass\n")
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertEqual(signatures(facts), {"f": f"def f({params})"})

    def test_a_minified_non_ascii_line_stays_linear(self) -> None:
        start = time.perf_counter()
        facts = parse("; ".join(f"X{i} = 'é{i}'" for i in range(20_000)) + "\n")
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertEqual(len(facts.symbols), 20_000)
        self.assertEqual(outline(facts.symbols[-1:]),
                         [("X19999", CONSTANT, 1, 1, "X19999 = 'é19999'", [])])


class TestSymbols(unittest.TestCase):
    def test_nested_classes_and_conditional_definitions(self) -> None:
        facts = parse(BLOCKS)
        self.assertIs(facts.confidence, Confidence.EXACT)
        self.assertEqual(
            outline(facts.symbols),
            [
                ("Article", CLASS, 3, 5, "class Article(models.Model)",
                 [("Meta", CLASS, 4, 5, "class Meta", [])]),
                ("Settings", CLASS, 7, 11, "class Settings(BaseSettings)",
                 [("Config", CLASS, 8, 11, "class Config",
                   [("customise", METHOD, 11, 11, "def customise(cls)", [])])]),
                ("removeprefix", FUNCTION, 14, 15, "def removeprefix(s, p)", []),
                ("cache", FUNCTION, 23, 24, "def cache(fn)", []),
            ],
        )

    @unittest.skipIf(sys.version_info < (3, 12), "ast reads PEP 695 from 3.12")
    def test_type_aliases_are_types(self) -> None:
        facts = parse(ALIASES)
        self.assertIs(facts.confidence, Confidence.EXACT)
        self.assertEqual(
            outline(facts.symbols),
            [
                ("Vector", TYPE, 1, 1, "type Vector = list[float]", []),
                ("Pair", TYPE, 2, 3, "type Pair[T] = tuple[ T, T]", []),
            ],
        )

    def test_dunders_are_public_and_mangled_names_private(self) -> None:
        facts = parse(
            "class Account:\n"
            "    def __init__(self): pass\n"
            "    def __audit(self): pass\n"
            "\n"
            "def __reset_cache(): pass\n"
            "def open_account(): pass\n"
        )
        self.assertEqual(
            {s.name: s.is_public for s in facts.all_symbols},
            {"Account": True, "__init__": True, "__audit": False,
             "__reset_cache": False, "open_account": True},
        )
        self.assertEqual([s.name for s in facts.public_symbols],
                         ["Account", "open_account"])

    def test_both_paths_agree_on_visibility(self) -> None:
        source = (
            "def __getattr__(name): pass\n"
            "def _private(): pass\n"
            "class _Hidden:\n"
            "    def __eq__(self, other): pass\n"
            "    def __x(self): pass\n"
            "    def _y(self): pass\n"
        )
        expected = {"__getattr__": True, "_private": False, "_Hidden": False,
                    "__eq__": True, "__x": False, "_y": False}
        for facts in (parse(source), parse(source + PY2)):
            with self.subTest(facts.confidence):
                self.assertEqual({s.name: s.is_public for s in facts.all_symbols},
                                 expected)


class TestDunderAll(unittest.TestCase):
    DEFS = "def connect(): pass\ndef disconnect(): pass\ndef debug_dump(): pass\n"

    def test_every_spelling_accumulates_in_order(self) -> None:
        cases = {
            '__all__: list[str] = ["connect"]\n': ("connect",),
            '__all__ = ["connect"]\n__all__ += ["disconnect"]\n':
                ("connect", "disconnect"),
            '__all__ = ["connect"]\n__all__.extend(["disconnect"])\n':
                ("connect", "disconnect"),
            '__all__ = ("connect",)\n__all__.append("disconnect")\n':
                ("connect", "disconnect"),
            '__all__ = ["connect"] + ["disconnect"]\n': ("connect", "disconnect"),
            '__all__ = ["connect", name, 1]\n': ("connect",),
            '__all__ = ["debug_dump"]\n__all__ = ["connect"]\n': ("connect",),
            '__all__ = ["connect"]\n__all__ += ["connect"]\n': ("connect",),
        }
        for header, expected in cases.items():
            with self.subTest(header):
                self.assertEqual(parse(header + self.DEFS).exports, expected)

    def test_public_symbols_follow_the_accumulated_list(self) -> None:
        annotated = parse('__all__: list[str] = ["connect"]\n\n'
                          "def connect(): pass\ndef debug_dump(): pass\n")
        augmented = parse('__all__ = ["connect"]\n__all__ += ["disconnect"]\n\n'
                          + self.DEFS)
        extended = parse('__all__ = ["connect"]\n__all__.extend(["disconnect"])\n\n'
                         + self.DEFS)
        self.assertEqual([s.name for s in annotated.public_symbols], ["connect"])
        for facts in (augmented, extended):
            self.assertEqual([s.name for s in facts.public_symbols],
                             ["connect", "disconnect"])

    def test_a_long_concatenation_does_not_fail_the_file(self) -> None:
        source = "__all__ = " + " + ".join(f'["n{i}"]' for i in range(1500)) + "\n"
        try:
            ast.parse(source)
        except (RecursionError, MemoryError):
            self.skipTest("this interpreter's ast cannot build the chain")
        facts = parse(source)
        self.assertIs(facts.confidence, Confidence.EXACT)
        self.assertEqual(facts.exports, tuple(f"n{i}" for i in range(1500)))


class TestImports(unittest.TestCase):
    def test_every_form_records_the_most_specific_name(self) -> None:
        facts = parse(
            "import a.b\n"
            "import a.b as alias\n"
            "from a.b import c, d\n"
            "from star import *\n"
            "from .m import x\n"
            "from . import models, views\n"
            "from .. import settings\n"
            "from ..pkg.sub import thing\n"
            "from . import *\n"
            "def f():\n"
            "    import inner.mod\n",
            "pkg/app/__init__.py",
        )
        self.assertEqual(
            facts.imports,
            (".", "..pkg.sub.thing", "..settings", ".m.x", ".models", ".views",
             "a.b", "a.b.c", "a.b.d", "inner.mod", "star"),
        )

    def test_relative_package_imports_are_recorded(self) -> None:
        facts = parse(
            "from . import models, views\n"
            "from .. import settings\n"
            "from .utils import slugify\n",
            "pkg/app/__init__.py",
        )
        self.assertEqual(facts.imports,
                         ("..settings", ".models", ".utils.slugify", ".views"))

    def test_imports_inside_every_kind_of_block(self) -> None:
        facts = parse(
            "if TYPE_CHECKING:\n    import in_if\nelse:\n    import in_else\n"
            "try:\n    import in_try\nexcept ImportError:\n    import in_except\n"
            "else:\n    import in_try_else\nfinally:\n    import in_finally\n"
            "class C:\n    import in_class\n"
            "async def f():\n"
            "    async with x:\n        import in_with\n"
            "    async for y in z:\n        pass\n"
            "    else:\n        import in_for_else\n"
            "while True:\n    import in_while\n"
            "match command:\n    case [x]:\n        import in_case\n"
            "try:\n    pass\nexcept* ValueError:\n    import in_except_star\n"
        )
        self.assertEqual(
            facts.imports,
            ("in_case", "in_class", "in_else", "in_except", "in_except_star",
             "in_finally", "in_for_else", "in_if", "in_try", "in_try_else",
             "in_while", "in_with"),
        )


class TestDegraded(unittest.TestCase):
    def test_hard_syntax_error_fails_cleanly(self) -> None:
        facts = PythonParser().parse(Path("x.py"), "def (:\n  ???\n")
        self.assertIs(facts.confidence, Confidence.FAILED)
        self.assertEqual(facts.symbols, ())
        self.assertTrue(facts.notes)
        self.assertFalse(facts.is_usable)

    def test_null_bytes_fail_cleanly(self) -> None:
        facts = PythonParser().parse(Path("x.py"), "def a():\x00 pass\n")
        self.assertIs(facts.confidence, Confidence.FAILED)

    def test_token_scan_takes_what_the_exact_path_takes(self) -> None:
        source = (
            "MAX = 1\nclass C:\n    def inner(self): pass\ndef top(a): pass\n"
            "async def atop(): pass\n"
        )
        facts = parse(source + PY2)
        self.assertIs(facts.confidence, Confidence.DEGRADED)
        self.assertEqual([s.name for s in facts.symbols], ["C", "top", "atop", "MAX"])
        self.assertTrue(next(s for s in facts.symbols if s.name == "atop").is_async)
        self.assertEqual(shape(facts.symbols), shape(parse(source).symbols))

    def test_token_scan_matches_the_exact_path_block_for_block(self) -> None:
        for source in (SAMPLE, BLOCKS):
            exact, degraded = parse(source), parse(source + PY2)
            with self.subTest(source[:20]):
                self.assertIs(exact.confidence, Confidence.EXACT)
                self.assertIs(degraded.confidence, Confidence.DEGRADED)
                self.assertEqual(shape(degraded.symbols), shape(exact.symbols))

    def test_strings_never_become_symbols(self) -> None:
        facts = parse(
            '"""Plugin loader.\n'
            "\n"
            "Example plugin:\n"
            "\n"
            "class ExamplePlugin(Base):\n"
            "    pass\n"
            '"""\n'
            'print "loading"\n'
            'TEMPLATE = """\n'
            "def generated_entry():\n"
            "    return 1\n"
            "DEBUG = True\n"
            '"""\n'
            "_CACHE = {}\n"
            "def load(name):\n"
            "    return name\n",
            "loader.py",
        )
        self.assertIs(facts.confidence, Confidence.DEGRADED)
        self.assertEqual(
            outline(facts.symbols),
            [("load", FUNCTION, 15, 16, "def load(name)", []),
             ("TEMPLATE", CONSTANT, 9, 13, 'TEMPLATE = """', [])],
        )
        self.assertEqual([s.name for s in facts.public_symbols], ["load", "TEMPLATE"])

    def test_pep_695_shapes_and_token_signatures(self) -> None:
        facts = parse(
            "class Box[T]:\n"
            "    def get(self) -> T: ...\n"
            "\n"
            "def first[T](items: list[T]) -> T:\n"
            "    return items[0]\n"
            "\n"
            "def load(path,\n"
            '         mode="r"):  # opens it\n'
            "    try:\n"
            "        return open(path, mode)\n"
            "    except OSError:\n"
            '        print "failed"\n'
            "\n"
            "def ident(x): return x\n"
            "\n"
            "type Pair[T] = tuple[T, T]\n"
        )
        self.assertIs(facts.confidence, Confidence.DEGRADED)
        self.assertEqual(
            outline(facts.symbols),
            [
                ("Box", CLASS, 1, 2, "class Box[T]",
                 [("get", METHOD, 2, 2, "def get(self) -> T", [])]),
                ("first", FUNCTION, 4, 5, "def first[T](items: list[T]) -> T", []),
                ("load", FUNCTION, 7, 12, 'def load(path, mode="r")', []),
                ("ident", FUNCTION, 14, 14, "def ident(x)", []),
                ("Pair", TYPE, 16, 16, "type Pair[T] = tuple[T, T]", []),
            ],
        )

    def test_type_aliases_survive_the_fallback(self) -> None:
        facts = parse(ALIASES + PY2)
        self.assertIs(facts.confidence, Confidence.DEGRADED)
        self.assertEqual(
            outline(facts.symbols),
            [
                ("Vector", TYPE, 1, 1, "type Vector = list[float]", []),
                ("Pair", TYPE, 2, 3, "type Pair[T] = tuple[ T, T]", []),
            ],
        )

    def test_parser_stack_overflow_does_not_escape(self) -> None:
        # A generated dispatch table: 7,000 elifs overflow the PEG parser.
        source = "def lookup(a):\n    if a == 0:\n        return 0\n" + "".join(
            f"    elif a == {i}:\n        return {i}\n" for i in range(1, 7000)
        )
        try:
            ast.parse(source)
            expected = Confidence.EXACT
        except (MemoryError, RecursionError):
            expected = Confidence.DEGRADED
        facts = parse(source, "generated_dispatch.py")
        self.assertIs(facts.confidence, expected)
        self.assertEqual(outline(facts.symbols),
                         [("lookup", FUNCTION, 1, 14001, "def lookup(a)", [])])

    def test_every_ast_error_is_contained(self) -> None:
        cases = [
            (MemoryError("Parser stack overflowed"), Confidence.DEGRADED, ["f"],
             "ast failed (MemoryError: Parser stack overflowed); symbols recovered"),
            (RecursionError("too deep"), Confidence.DEGRADED, ["f"],
             "ast failed (RecursionError: too deep); symbols recovered"),
            (ValueError("null bytes"), Confidence.FAILED, [],
             "unparseable: ValueError"),
        ]
        for error, confidence, names, note in cases:
            with self.subTest(type(error).__name__):
                with mock.patch("ast.parse", side_effect=error):
                    facts = parse("def f(a):\n    return a\n")
                self.assertIs(facts.confidence, confidence)
                self.assertEqual([s.name for s in facts.symbols], names)
                self.assertTrue(facts.notes[0].startswith(note), facts.notes)

    def test_an_unexpected_error_fails_the_file_not_the_run(self) -> None:
        boom = TypeError("boom")
        with mock.patch.object(PythonParser, "_from_ast", side_effect=boom):
            facts = parse("x = 1\n")
        self.assertIs(facts.confidence, Confidence.FAILED)
        self.assertEqual(facts.notes, ["parser error: TypeError: boom"])
        self.assertEqual(facts.loc, 1)

    def test_lines_are_counted_whatever_ends_the_file(self) -> None:
        # Regression: a final newline counted as one more line.
        for source, loc in {"": 0, "\n": 1, "x = 1": 1, "x = 1\n": 1, "x = 1\n\n": 2,
                            "x = 1\ny = 2": 2, "x = 1\ny = 2\n": 2, "\ufeff": 0,
                            "\ufeffx = 1\n": 1, "x = 1\r\ny = 2\r\n": 2,
                            "x = 1\ry = 2\r": 2}.items():
            with self.subTest(source=source):
                self.assertEqual(parse(source).loc, loc)
        for failed in ("(\n", "x = '\udcff'\n"):
            with self.subTest(source=failed):
                self.assertEqual(parse(failed).loc, 1)

    def test_odd_inputs_never_raise(self) -> None:
        cases = {
            "": Confidence.EXACT,
            "\ufeff": Confidence.EXACT,
            "x = '\udcff'\n": Confidence.FAILED,
            "(" * 1000 + "\n": Confidence.FAILED,
            '"""unterminated\ndef f(): pass\n': Confidence.FAILED,
        }
        for source, confidence in cases.items():
            with self.subTest(source=source[:16]):
                self.assertIs(parse(source).confidence, confidence)

    def test_pep_701_fstrings_are_parsed_or_refused(self) -> None:
        facts = parse('X = f"{"a"}"\ndef f(): pass\n')
        if sys.version_info >= (3, 12):
            self.assertEqual(signatures(facts), {"f": "def f()", "X": 'X = f"{"a"}"'})
        else:   # the old tokenizer would split it silently
            self.assertIs(facts.confidence, Confidence.FAILED)

    def test_nested_quote_check_is_one_pass(self) -> None:
        self.assertTrue(_reuses_quote('x = f"{"a"}"\n'))
        self.assertTrue(_reuses_quote('x = f"{a + "b"}"\n'))
        self.assertFalse(_reuses_quote('x = f"{a}" + "b"\n'))
        self.assertFalse(_reuses_quote("x = f\"{d['k']}\"\n"))
        self.assertFalse(_reuses_quote('x = f"{a\n'))
        start = time.perf_counter()
        self.assertFalse(_reuses_quote('f"' + "{" * 100_000 + "\n"))
        self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
