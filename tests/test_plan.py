"""The planner: significance, need, the coverage cut and the budget fit.

Expected numbers are worked out by hand from the formulas (the arithmetic is
noted beside each one), so a changed weight, threshold or tie-break fails
here instead of quietly shifting what a run pays for.
"""

from __future__ import annotations

import math
import posixpath
import unittest
from collections.abc import Mapping
from pathlib import Path

from devdocs.graph import FileNode, ModuleNode, ProjectGraph
from devdocs.history import FileHistory
from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind
from devdocs.plan import (
    FilePlan,
    Fit,
    ModulePlan,
    Plan,
    Planner,
    PlannerOptions,
    Signals,
    fit_to_budget,
    need,
    score,
    signals_for,
)
from devdocs.render.model import Mode
from devdocs.scan import Role, SourceFile

# 98 characters: states a purpose (80+) without earning the long-docstring bonus.
PURPOSE = (
    "Prices a shopping cart against the catalogue and applies every discount "
    "that is still valid today."
)


def sym(
    name: str,
    line: int = 1,
    *,
    doc: str = "",
    kind: SymbolKind = SymbolKind.FUNCTION,
    public: bool = True,
    children: tuple[Symbol, ...] = (),
) -> Symbol:
    return Symbol(
        name, kind, line=line, docstring=doc, children=children, is_public=public
    )


def cls(
    name: str, line: int, *members: Symbol, doc: str = "", public: bool = True
) -> Symbol:
    return sym(
        name, line, doc=doc, kind=SymbolKind.CLASS, public=public, children=members
    )


def method(name: str, line: int, *, doc: str = "", public: bool = True) -> Symbol:
    return sym(name, line, doc=doc, kind=SymbolKind.METHOD, public=public)


def item_with_setter() -> Symbol:
    """A documented property and its bare setter, as the Python parser sees them."""
    return cls(
        "Item",
        1,
        method("price", 4, doc="The unit price in cents."),
        method("price", 9),
        doc="A line item.",
    )


def facts_of(
    *symbols: Symbol,
    doc: str = "",
    exports: tuple[str, ...] = (),
    loc: int = 0,
    confidence: Confidence = Confidence.EXACT,
) -> FileFacts:
    return FileFacts(
        Path("m.py"),
        "python",
        symbols,
        exports=exports,
        module_docstring=doc,
        loc=loc,
        confidence=confidence,
    )


def source(relpath: str, *, role: Role = Role.SOURCE, lines: int = 0) -> SourceFile:
    return SourceFile(Path(relpath), relpath, "python", role, size=1, lines=lines)


class Project:
    """Scan entries, facts, graph and history of a hand-built project."""

    def __init__(self) -> None:
        self.files: list[SourceFile] = []
        self.facts: dict[str, FileFacts] = {}
        self.nodes: dict[str, FileNode] = {}
        self.modules: dict[str, ModuleNode] = {}
        self.history: dict[str, FileHistory] = {}

    @property
    def graph(self) -> ProjectGraph:
        return ProjectGraph(files=dict(self.nodes), modules=dict(self.modules))

    def add(
        self,
        relpath: str,
        *symbols: Symbol,
        doc: str = "",
        exports: tuple[str, ...] = (),
        loc: int = 0,
        confidence: Confidence = Confidence.EXACT,
        parsed: bool = True,
        language: str = "python",
        role: Role = Role.SOURCE,
        lines: int = 0,
        fan_in: int = 0,
        entry: str = "",
        module: str | None = None,
        commits: int = 0,
        authors: int = 0,
    ) -> None:
        path = Path(relpath)
        self.files.append(SourceFile(path, relpath, language, role, 1, lines))
        if parsed:
            self.facts[relpath] = FileFacts(
                path,
                language,
                symbols,
                exports=exports,
                module_docstring=doc,
                loc=loc,
                confidence=confidence,
            )
        if fan_in or entry or module is not None:
            self.nodes[relpath] = FileNode(
                relpath,
                language,
                posixpath.dirname(relpath) if module is None else module,
                dependents=tuple(f"user{index}.py" for index in range(fan_in)),
                entry_point=entry,
            )
        if commits or authors:
            self.history[relpath] = FileHistory(commits=commits, authors=authors)

    def used_by(self, module: str, *importers: str) -> None:
        self.modules[module] = ModuleNode(module, used_by=importers)

    def plan(self, options: PlannerOptions | None = None) -> Plan:
        planner = Planner(options)
        return planner.plan(self.files, self.facts, self.graph, self.history)


def shop() -> Project:
    """A small project with one file of every kind the planner tells apart."""
    project = Project()
    project.add(
        "shop/cart.py",
        cls(
            "Cart",
            3,
            method("add", 4, doc="Add a line."),
            method("total", 9),
            method("_recalc", 14),
        ),
        sym("checkout", 20),
        doc=PURPOSE,
        loc=120,
        fan_in=3,
        commits=4,
        authors=2,
    )
    project.add("shop/api.py", sym("main"), loc=40, entry="console script")
    project.add("shop/_compat.py", sym("_shim", public=False), loc=10)
    project.add("shop/models.py", cls("Item", 1, doc="A line item."), doc=PURPOSE)
    project.add("shop/legacy.py", confidence=Confidence.FAILED)
    project.add("shop/billing.kt", language="kotlin", lines=30, parsed=False)
    project.add("tests/test_cart.py", sym("test_total"), role=Role.TEST)
    project.add("shop/cart_pb2.py", sym("CartMessage"), role=Role.GENERATED)
    project.add("README.md", language="", role=Role.DOCS, parsed=False)
    return project


def uniform(count: int) -> Project:
    """`count` interchangeable files in one directory: they rank by relpath."""
    project = Project()
    for index in range(count):
        project.add(f"src/f{index:05}.py", sym("run"))
    return project


def directories(count: int) -> Project:
    """`count` two-file directories that score alike: they rank by path."""
    project = Project()
    for index in range(count):
        for name in ("a", "b"):
            project.add(f"d{index:02}/{name}.py", sym("run"))
    return project


def rows(plan: Plan) -> list[tuple[str, float, float, Mode, str]]:
    return [(p.relpath, p.score, p.need, p.mode, p.reason) for p in plan.files]


class TestScore(unittest.TestCase):
    def test_no_signals_is_the_empty_api_penalty(self) -> None:
        self.assertEqual(score(Signals()), -2.0)

    def test_every_bonus(self) -> None:
        signals = Signals(
            fan_in=3,
            public_symbols=4,
            has_exports=True,
            is_entry_point=True,
            loc=250,
            docstring_chars=201,
            commits=5,
            authors=2,
        )
        # 3 ln 4 + 1.5 ln 5 + 2 + 1.5 + 0.8 ln 3.5 + 1 + 1.2 ln 6 + 2 = 16.22536
        self.assertEqual(score(signals), 16.2254)

    def test_each_penalty(self) -> None:
        # one public symbol is 1.5 ln 2 = 1.0397, less the penalty
        cases = {
            Signals(public_symbols=1, is_private_module=True): -0.9603,
            Signals(public_symbols=1, is_test=True): -1.9603,
            Signals(public_symbols=1, is_generated=True): -2.9603,
            Signals(public_symbols=1, is_vendored=True): -3.9603,
        }
        for signals, expected in cases.items():
            with self.subTest(signals=signals):
                self.assertEqual(score(signals), expected)

    def test_penalties_stack(self) -> None:
        signals = Signals(
            is_private_module=True, is_test=True, is_generated=True, is_vendored=True
        )
        self.assertEqual(score(signals), -16.0)

    def test_thresholds_and_caps(self) -> None:
        cases = {
            Signals(docstring_chars=200): -2.0,
            Signals(docstring_chars=201): -1.0,
            Signals(authors=3): 1.0,
            Signals(authors=40): 1.0,
            Signals(has_exports=True): 0.0,
            Signals(is_entry_point=True): -0.5,
        }
        for signals, expected in cases.items():
            with self.subTest(signals=signals):
                self.assertEqual(score(signals), expected)

    def test_counts_are_log_damped(self) -> None:
        cases = {
            Signals(fan_in=1, public_symbols=1): 3.1192,  # 4.5 ln 2
            Signals(fan_in=1000, public_symbols=1): 21.766,  # 3 ln 1001 + 1.5 ln 2
            Signals(public_symbols=1, loc=100): 1.5942,  # + 0.8 ln 2: hundreds
            Signals(public_symbols=1, commits=1): 1.8715,  # + 1.2 ln 2
        }
        for signals, expected in cases.items():
            with self.subTest(signals=signals):
                self.assertEqual(score(signals), expected)

    def test_rounds_to_four_decimals(self) -> None:
        self.assertEqual(score(Signals(fan_in=1)), 0.0794)  # 3 ln 2 - 2 = 0.07944

    def test_a_total_that_rounds_to_zero_is_positive_zero(self) -> None:
        # -2 - 2 + 1 + 0.8 ln 42.52 = -0.00002 before rounding
        result = score(Signals(loc=4152, is_private_module=True, docstring_chars=201))
        self.assertEqual(result, 0.0)
        self.assertEqual(math.copysign(1.0, result), 1.0)

    def test_negative_counts_read_as_none(self) -> None:
        signals = Signals(
            fan_in=-4, public_symbols=-1, loc=-500, commits=-3, authors=-2
        )
        self.assertEqual(score(signals), -2.0)


class TestNeed(unittest.TestCase):
    def test_failed_parse_needs_nothing(self) -> None:
        facts = facts_of(sym("run"), sym("stop", 2), confidence=Confidence.FAILED)
        self.assertEqual(need(facts), 0.0)

    def test_partial_parses_are_judged_on_what_they_found(self) -> None:
        for confidence in (Confidence.HEURISTIC, Confidence.DEGRADED):
            with self.subTest(confidence=confidence):
                self.assertEqual(need(facts_of(sym("run"), confidence=confidence)), 1.0)

    def test_fully_documented_needs_nothing(self) -> None:
        self.assertEqual(need(facts_of(sym("run", doc="Run it."), doc=PURPOSE)), 0.0)

    def test_empty_api(self) -> None:
        self.assertEqual(need(facts_of(doc=PURPOSE)), 0.0)
        self.assertEqual(need(facts_of()), 0.4)
        self.assertEqual(need(facts_of(sym("_helper", public=False))), 0.4)

    def test_purpose_needs_80_characters_after_stripping(self) -> None:
        documented = sym("run", doc="Run it.")
        padded = "\n  " + "x" * 79 + "  \n"
        self.assertEqual(need(facts_of(documented, doc=padded)), 0.4)
        self.assertEqual(need(facts_of(documented, doc="x" * 80)), 0.0)

    def test_undocumented_share_with_floor(self) -> None:
        cases = [
            # (docstrings of the API, module docstring, need)
            (("Doc.", ""), PURPOSE, 0.3),  # 0.6 * 1/2
            (("Doc.", "", ""), PURPOSE, 0.4),  # 0.6 * 2/3
            (("Doc.", "Doc.", "Doc.", ""), PURPOSE, 0.25),  # 0.6 * 1/4, floored
            (("Doc.", ""), "", 0.7),  # 0.4 + 0.6 * 1/2
            (("", ""), "", 1.0),
            (("Doc.",) * 5 + ("", ""), "", 0.5714),  # 0.4 + 0.6 * 2/7
        ]
        for docs, module_doc, expected in cases:
            symbols = [
                sym(f"f{line}", line, doc=text) for line, text in enumerate(docs)
            ]
            with self.subTest(docs=docs, purpose=bool(module_doc)):
                self.assertEqual(need(facts_of(*symbols, doc=module_doc)), expected)

    def test_blank_docstrings_are_missing(self) -> None:
        facts = facts_of(sym("a", 1, doc=" \n\t"), sym("b", 2, doc="B."), doc=PURPOSE)
        self.assertEqual(need(facts), 0.3)

    def test_class_members_join_the_api(self) -> None:
        cart = cls(
            "Cart",
            1,
            method("__init__", 2),
            method("__call__", 3, doc="Price the cart."),
            method("__repr__", 4),
            method("_recalc", 5),
            method("hidden", 6, public=False),
            method("total", 7),
            doc="A cart.",
        )
        # API: Cart, Cart.__init__, Cart.__call__, Cart.total; two undocumented
        self.assertEqual(need(facts_of(cart, doc=PURPOSE)), 0.3)

    def test_only_classes_and_interfaces_contribute_members(self) -> None:
        # each owner is documented and its one member is not
        cases = {
            SymbolKind.CLASS: 0.3,  # API: owner and member; 0.6 * 1/2
            SymbolKind.INTERFACE: 0.3,
            SymbolKind.TYPE: 0.0,  # a Java enum's methods are not API
            SymbolKind.FUNCTION: 0.0,  # nor are a function's nested definitions
        }
        for kind, expected in cases.items():
            owner = sym("Owner", 1, kind=kind, doc="Doc.", children=(method("m", 2),))
            with self.subTest(kind=kind):
                self.assertEqual(need(facts_of(owner, doc=PURPOSE)), expected)

    def test_members_are_one_level_deep(self) -> None:
        inner = cls("Inner", 2, method("run", 3), doc="A part.")
        outer = cls("Outer", 1, inner, doc="The whole.")
        # API: Outer and Outer.Inner, both documented; Inner.run is not API
        self.assertEqual(need(facts_of(outer, doc=PURPOSE)), 0.0)

    def test_members_of_private_classes_are_not_api(self) -> None:
        impl = cls("_Impl", 1, method("run", 2), public=False)
        facts = facts_of(impl, sym("api", 5, doc="Doc."), doc=PURPOSE)
        self.assertEqual(need(facts), 0.0)

    def test_exports_decide_the_api(self) -> None:
        facts = facts_of(
            sym("shown", 1, doc="Doc."),
            sym("unlisted", 2),
            doc=PURPOSE,
            exports=("shown",),
        )
        self.assertEqual(need(facts), 0.0)

    def test_every_definition_of_a_name_counts(self) -> None:
        # API: Item, Item.price and Item.price again; the setter is bare, and
        # 0.6 * 1/3 is floored to 0.25
        self.assertEqual(need(facts_of(item_with_setter(), doc=PURPOSE)), 0.25)
        # two bare overloads and the documented implementation: 0.6 * 2/3
        overloads = (sym("parse", 3), sym("parse", 4), sym("parse", 5, doc="Parse."))
        self.assertEqual(need(facts_of(*overloads, doc=PURPOSE)), 0.4)


class TestSignalsFor(unittest.TestCase):
    def test_gathers_every_input(self) -> None:
        file = SourceFile(
            Path("/repo/src/shop/cart.py"),
            "src/shop/cart.py",
            "python",
            Role.SOURCE,
            size=900,
            lines=130,
        )
        facts = FileFacts(
            file.path,
            "python",
            (sym("Cart", 1), sym("checkout", 9), sym("_tax", 12, public=False)),
            exports=("Cart", "checkout"),
            module_docstring=f"\n  {PURPOSE}  \n",
            loc=120,
        )
        node = FileNode(
            file.relpath,
            "python",
            "src/shop",
            dependents=("src/cli.py", "src/shop/api.py", "src/shop/views.py"),
            tested_by=("tests/test_cart.py",),
            entry_point="console script",
        )
        expected = Signals(
            fan_in=3,
            public_symbols=2,
            has_exports=True,
            is_entry_point=True,
            loc=120,
            docstring_chars=98,
            commits=7,
            authors=3,
        )
        self.assertEqual(signals_for(file, facts, node, FileHistory(7, 3)), expected)

    def test_missing_inputs_contribute_zeros(self) -> None:
        file = SourceFile(Path("tool.kt"), "tool.kt", "kotlin", Role.SOURCE, 10, 42)
        self.assertEqual(signals_for(file, None, None, None), Signals(loc=42))

    def test_loc_falls_back_to_scanned_lines(self) -> None:
        file = source("a.py", lines=50)
        self.assertEqual(signals_for(file, facts_of(loc=12), None, None).loc, 12)
        self.assertEqual(signals_for(file, facts_of(), None, None).loc, 50)

    def test_private_module_by_stem(self) -> None:
        cases = {
            "pkg/_util.py": True,
            "pkg/__version__.py": True,
            "pkg/__init__.py": False,
            "pkg/__main__.py": False,
            "pkg/util.py": False,
            "_internal/util.py": False,
        }
        for relpath, expected in cases.items():
            with self.subTest(relpath=relpath):
                signals = signals_for(source(relpath), None, None, None)
                self.assertEqual(signals.is_private_module, expected)

    def test_role_flags(self) -> None:
        cases = {
            Role.SOURCE: Signals(),
            Role.CONFIG: Signals(),
            Role.TEST: Signals(is_test=True),
            Role.GENERATED: Signals(is_generated=True),
            Role.VENDORED: Signals(is_vendored=True),
        }
        for role, expected in cases.items():
            with self.subTest(role=role):
                file = source("x.py", role=role)
                self.assertEqual(signals_for(file, None, None, None), expected)

    def test_entry_point_needs_a_reason(self) -> None:
        node = FileNode("a.py", "python", "", dependents=("b.py",))
        signals = signals_for(source("a.py"), None, node, None)
        self.assertEqual((signals.fan_in, signals.is_entry_point), (1, False))


class TestDescribe(unittest.TestCase):
    @staticmethod
    def describe(*symbols: Symbol) -> tuple[str, ...]:
        project = Project()
        project.add("m.py", *symbols)
        (item,) = project.plan().files
        return item.describe

    def test_source_order_interleaves_members(self) -> None:
        cart = cls(
            "Cart",
            10,
            method("total", 11),
            method("__init__", 15),
            method("__repr__", 16),
            method("_tax", 17),
            method("add", 18, doc="Add a line."),
        )
        names = self.describe(
            sym("zeta", 30),
            cart,
            sym("helper", 12),
            sym("alpha", 30),
            sym("done", 5, doc="Done."),
        )
        self.assertEqual(
            names, ("Cart", "Cart.total", "helper", "Cart.__init__", "alpha", "zeta")
        )

    def test_overloads_are_named_once(self) -> None:
        names = self.describe(
            sym("parse", 3), sym("parse", 4), sym("parse", 5, doc="Parse.")
        )
        self.assertEqual(names, ("parse",))

    def test_a_bare_setter_describes_its_property(self) -> None:
        self.assertEqual(self.describe(item_with_setter()), ("Item.price",))

    def test_blank_docstrings_are_described(self) -> None:
        names = self.describe(sym("load", 1, doc=" \n\t"), sym("save", 2, doc="Save."))
        self.assertEqual(names, ("load",))

    def test_capped_at_25_in_source_order(self) -> None:
        names = self.describe(*(sym(f"f{line:02}", line) for line in range(30, 0, -1)))
        self.assertEqual(names, tuple(f"f{line:02}" for line in range(1, 26)))

    def test_stubs_describe_nothing(self) -> None:
        project = Project()
        project.add("broken.py", sym("run"), confidence=Confidence.FAILED)
        project.add("tool.kt", language="kotlin", parsed=False)
        self.assertEqual([p.describe for p in project.plan().files], [(), ()])


class TestPlannerFiles(unittest.TestCase):
    def test_reference_project(self) -> None:
        # cart: 3 ln 4 + 1.5 ln 3 + 0.8 ln 2.2 + 1.2 ln 5 + 2 = 10.3689; need
        #   0.6 * 3/4 (Cart, Cart.total and checkout of four undocumented)
        # api: 1.5 ln 2 + 1.5 + 0.8 ln 1.4 = 2.8089; need 0.4 + 0.6
        # _compat: -2 - 2 + 0.8 ln 1.1 = -3.9238; need 0.4 (no API, no purpose)
        # billing.kt: -2 + 0.8 ln 1.3 = -1.7901 from scanned lines alone
        plan = shop().plan()
        files = [
            FilePlan(
                "shop/cart.py",
                10.3689,
                0.45,
                Mode.PROSE,
                "ranked 1 of 3",
                ("Cart", "Cart.total", "checkout"),
            ),
            FilePlan(
                "shop/api.py", 2.8089, 1.0, Mode.PROSE, "ranked 2 of 3", ("main",)
            ),
            FilePlan(
                "shop/models.py", 1.0397, 0.0, Mode.STUB, "fully documented in source"
            ),
            FilePlan(
                "shop/billing.kt", -1.7901, 0.0, Mode.STUB, "no parser for kotlin"
            ),
            FilePlan("shop/legacy.py", -2.0, 0.0, Mode.STUB, "parse failed"),
            FilePlan("shop/_compat.py", -3.9238, 0.4, Mode.PROSE, "ranked 3 of 3"),
        ]
        members = (
            "shop/_compat.py",
            "shop/api.py",
            "shop/billing.kt",
            "shop/cart.py",
            "shop/legacy.py",
            "shop/models.py",
        )
        # 10.3689 + 2.8089 + 1.0397, and three negative scores counted as 0
        modules = [ModulePlan("shop", members, 14.2175, Mode.PROSE, "ranked 1 of 1")]
        self.assertEqual(plan, Plan(files, modules, Mode.PROSE, "project overview"))

    def test_only_documentable_files_are_planned(self) -> None:
        project = Project()
        project.add("src/app.py", sym("run"))
        project.add("tests/test_app.py", sym("test_run"), role=Role.TEST)
        project.add("src/app_pb2.py", sym("Message"), role=Role.GENERATED)
        project.add("vendor/lib.py", sym("helper"), role=Role.VENDORED)
        project.add("docs/guide.md", language="", role=Role.DOCS, parsed=False)
        project.add("setup.cfg", language="", role=Role.CONFIG, parsed=False)
        self.assertEqual([p.relpath for p in project.plan().files], ["src/app.py"])

    def test_each_file_is_planned_once(self) -> None:
        project = Project()
        project.add("a.py", sym("run"))
        project.files.append(project.files[0])
        self.assertEqual([p.relpath for p in project.plan().files], ["a.py"])

    def test_equal_files_rank_by_relpath(self) -> None:
        project = Project()
        for relpath in ("c.py", "a.py", "b.py"):
            project.add(relpath, sym("run"))
        self.assertEqual(
            [(p.relpath, p.reason) for p in project.plan().files],
            [
                ("a.py", "ranked 1 of 3"),
                ("b.py", "ranked 2 of 3"),
                ("c.py", "ranked 3 of 3"),
            ],
        )

    def test_equal_values_rank_by_score(self) -> None:
        # a: 0 + 1.5 + 1 = 2.5 at need 0.4; b: 0 + 1 + 3 = 4.0 at need 0.25
        project = Project()
        project.add("a.py", exports=("reexported",), entry="main", authors=1)
        project.add("b.py", exports=("reexported",), doc="x" * 201, authors=3)
        plan = project.plan(PlannerOptions(include_documented=True))
        self.assertEqual(
            [(p.relpath, p.value) for p in plan.files], [("b.py", 1.0), ("a.py", 1.0)]
        )

    def test_value_ties_survive_float_noise(self) -> None:
        # 8.5304 * 0.48 and 6.3978 * 0.64 are both 4.094592, yet as floats the
        # second product is the larger: the higher score must still go first
        project = Project()
        project.add(
            "app/server.py",
            cls(
                "Server",
                1,
                method("start", 2),
                method("stop", 3),
                method("reload", 4),
                method("status", 5),
                doc="Serves the app.",
            ),
            doc=PURPOSE,
            fan_in=3,
            entry="console script",
            commits=1,
            authors=1,
        )
        project.add(
            "app/config.py",
            sym("load", 1, doc="Load."),
            sym("save", 2, doc="Save."),
            cls("Settings", 3, method("get", 4), method("set", 5), doc="Settings."),
            exports=("load", "save", "Settings"),
            commits=2,
            authors=1,
        )
        self.assertEqual(
            rows(project.plan()),
            [
                ("app/server.py", 8.5304, 0.48, Mode.PROSE, "ranked 1 of 2"),
                ("app/config.py", 6.3978, 0.64, Mode.PROSE, "ranked 2 of 2"),
            ],
        )

    def test_values_rank_at_full_precision(self) -> None:
        # sync: 1.5 ln 2 + 1.2 ln 5 = 2.971 at need 0.6, value 1.7826
        # store: 3 ln 2 + 1.5 ln 8 = 5.1986 at need 0.6 * 4/7 = 0.3429, value
        #   1.78259994: 6e-8 lower, so second despite its score and its name
        project = Project()
        project.add("sync.py", sym("run"), doc=PURPOSE, commits=4)
        documented = [sym(f"get{line}", line, doc="Doc.") for line in range(1, 4)]
        bare = [sym(f"set{line}", line) for line in range(4, 8)]
        project.add("store.py", *documented, *bare, doc=PURPOSE, fan_in=1)
        self.assertEqual(
            rows(project.plan()),
            [
                ("sync.py", 2.971, 0.6, Mode.PROSE, "ranked 1 of 2"),
                ("store.py", 5.1986, 0.3429, Mode.PROSE, "ranked 2 of 2"),
            ],
        )

    def test_input_order_does_not_matter(self) -> None:
        project = shop()
        options = PlannerOptions(min_prose_files=2, coverage=0.5)
        forward = project.plan(options)
        backward = Planner(options).plan(
            list(reversed(project.files)),
            dict(reversed(project.facts.items())),
            project.graph,
            dict(reversed(project.history.items())),
        )
        self.assertEqual(forward, backward)
        self.assertEqual(
            [p.relpath for p in forward.prose_files()], ["shop/cart.py", "shop/api.py"]
        )

    def test_include_documented_ranks_at_the_floor(self) -> None:
        project = Project()
        project.add("a.py", sym("run", doc="Run."), doc=PURPOSE)
        project.add("b.py", sym("go"))
        included = " (documented in source; included at need 0.25)"
        self.assertEqual(
            rows(project.plan(PlannerOptions(include_documented=True))),
            [
                ("b.py", 1.0397, 1.0, Mode.PROSE, "ranked 1 of 2"),
                ("a.py", 1.0397, 0.25, Mode.PROSE, "ranked 2 of 2" + included),
            ],
        )
        capped = PlannerOptions(include_documented=True, max_prose_files=1)
        self.assertEqual(
            rows(project.plan(capped))[1],
            ("a.py", 1.0397, 0.25, Mode.STUB, "below coverage cut" + included),
        )
        self.assertEqual(
            rows(project.plan()),
            [
                ("b.py", 1.0397, 1.0, Mode.PROSE, "ranked 1 of 1"),
                ("a.py", 1.0397, 0.0, Mode.STUB, "fully documented in source"),
            ],
        )

    def test_include_documented_never_rescues_files_without_facts(self) -> None:
        project = Project()
        project.add("done.py", sym("run", doc="Run."), doc=PURPOSE)
        project.add("broken.py", confidence=Confidence.FAILED)
        project.add("tool.kt", language="kotlin", lines=30, parsed=False)
        included = " (documented in source; included at need 0.25)"
        self.assertEqual(
            rows(project.plan(PlannerOptions(include_documented=True))),
            [
                ("done.py", 1.0397, 0.25, Mode.PROSE, "ranked 1 of 1" + included),
                ("tool.kt", -1.7901, 0.0, Mode.STUB, "no parser for kotlin"),
                ("broken.py", -2.0, 0.0, Mode.STUB, "parse failed"),
            ],
        )


class TestCoverageCut(unittest.TestCase):
    def prose(self, count: int, options: PlannerOptions | None = None) -> int:
        chosen = [p.relpath for p in uniform(count).plan(options).prose_files()]
        leading = [f"src/f{index:05}.py" for index in range(len(chosen))]
        self.assertEqual(chosen, leading)
        return len(chosen)

    def test_small_projects_get_prose_wherever_needed(self) -> None:
        self.assertEqual(self.prose(1), 1)
        self.assertEqual(self.prose(25), 25)

    def test_beyond_the_floor_coverage_decides(self) -> None:
        self.assertEqual(self.prose(26), 25)  # ceil(0.2 * 26) = 6, under the floor
        self.assertEqual(self.prose(126), 26)  # ceil(25.2)
        self.assertEqual(self.prose(200), 40)

    def test_options_and_caps(self) -> None:
        cases = [
            (PlannerOptions(max_prose_files=10), 200, 10),
            (PlannerOptions(max_prose_files=0), 5, 0),
            (PlannerOptions(min_prose_files=0, coverage=0.5), 9, 5),  # ceil(4.5)
            (PlannerOptions(min_prose_files=3, coverage=0.0), 9, 3),
            (PlannerOptions(coverage=1.5), 40, 40),
            (PlannerOptions(coverage=-1.0), 40, 25),
            (PlannerOptions(coverage=math.nan), 40, 25),
            # 0.07 * 100 is 7.000000000000001 in floats: still 7, not 8
            (PlannerOptions(min_prose_files=0, coverage=0.07), 100, 7),
        ]
        for options, count, expected in cases:
            with self.subTest(options=options, count=count):
                self.assertEqual(self.prose(count, options), expected)

    def test_reasons_name_the_rank(self) -> None:
        plan = uniform(30).plan()
        self.assertEqual(
            [(p.mode, p.reason) for p in plan.files[23:27]],
            [
                (Mode.PROSE, "ranked 24 of 30"),
                (Mode.PROSE, "ranked 25 of 30"),
                (Mode.STUB, "below coverage cut"),
                (Mode.STUB, "below coverage cut"),
            ],
        )

    def test_stubbed_files_are_not_counted(self) -> None:
        project = uniform(4)
        for index in range(3):
            project.add(f"src/done{index}.py", sym("run", doc="Run."), doc=PURPOSE)
            project.add(f"src/tool{index}.kt", language="kotlin", parsed=False)
        plan = project.plan(PlannerOptions(min_prose_files=0, coverage=0.5))
        self.assertEqual(len(plan.files), 10)
        self.assertEqual(
            [p.reason for p in plan.prose_files()], ["ranked 1 of 4", "ranked 2 of 4"]
        )

    def test_large_project(self) -> None:
        files = uniform(20_000).plan().files
        self.assertEqual(sum(p.mode is Mode.PROSE for p in files), 4_000)
        self.assertEqual(files[3_999].reason, "ranked 4,000 of 20,000")
        self.assertEqual(files[4_000].reason, "below coverage cut")


class TestModules(unittest.TestCase):
    def test_files_group_by_graph_module_else_directory(self) -> None:
        project = Project()
        project.add("lib/sub/deep.py", sym("a"), module="lib")
        project.add("lib/top.py", sym("b"))
        project.add("main.py", sym("c"))
        project.add("tests/test_main.py", sym("t"), role=Role.TEST)
        modules = {m.path: m.files for m in project.plan().modules}
        self.assertEqual(
            modules, {"lib": ("lib/sub/deep.py", "lib/top.py"), "": ("main.py",)}
        )

    def test_score_sums_the_best_five_files_and_importers(self) -> None:
        project = Project()
        for count in range(1, 7):
            symbols = [sym(f"s{line}", line) for line in range(1, count + 1)]
            project.add(f"core/f{count}.py", *symbols)
        project.used_by("core", "api", "cli")
        (module,) = project.plan().modules
        # 1.5 ln k for k = 3..7: 1.6479 + 2.0794 + 2.4142 + 2.6876 + 2.9189, + 2
        self.assertEqual(module.score, 13.748)
        self.assertEqual(len(module.files), 6)

    def test_negative_file_scores_count_as_zero(self) -> None:
        project = Project()
        project.add("util/api.py", sym("run"))
        project.add("util/_shim.py", sym("_x", public=False))  # -2 - 2
        (module,) = project.plan().modules
        self.assertEqual(module.score, 1.0397)

    def test_importers_lift_a_module(self) -> None:
        project = directories(2)
        project.used_by("d01", "d00", "cli")
        modules = project.plan().modules
        self.assertEqual(
            [(m.path, m.score, m.reason) for m in modules],
            [("d01", 4.0794, "ranked 1 of 2"), ("d00", 2.0794, "ranked 2 of 2")],
        )

    def test_one_ranking_with_small_modules_stubbed(self) -> None:
        project = Project()
        project.add("solo/only.py", sym("run"), fan_in=9)  # 3 ln 10 + 1.0397
        for name in ("a", "b"):
            project.add(f"pair/{name}.py", sym("run"))
        for name in ("a", "b", "c"):
            project.add(f"trio/{name}.py", sym("run"))

        def modules(options: PlannerOptions) -> list[tuple[str, float, Mode, str]]:
            plan = project.plan(options)
            return [(m.path, m.score, m.mode, m.reason) for m in plan.modules]

        self.assertEqual(
            modules(PlannerOptions()),
            [
                ("solo", 7.9475, Mode.STUB, "single file"),
                ("trio", 3.1191, Mode.PROSE, "ranked 1 of 2"),
                ("pair", 2.0794, Mode.PROSE, "ranked 2 of 2"),
            ],
        )
        self.assertEqual(
            modules(PlannerOptions(module_min_files=3)),
            [
                ("solo", 7.9475, Mode.STUB, "single file"),
                ("trio", 3.1191, Mode.PROSE, "ranked 1 of 1"),
                ("pair", 2.0794, Mode.STUB, "fewer than 3 files"),
            ],
        )

    def test_prose_modules_are_capped(self) -> None:
        cases = [
            (8, PlannerOptions(), 8),
            (12, PlannerOptions(), 8),
            (33, PlannerOptions(), 9),  # ceil(8.25)
            (40, PlannerOptions(), 10),
            (12, PlannerOptions(max_prose_modules=3), 3),
            (5, PlannerOptions(max_prose_modules=0), 0),
        ]
        for count, options, expected in cases:
            with self.subTest(count=count, options=options):
                plan = directories(count).plan(options)
                self.assertEqual(len(plan.prose_modules()), expected)

    def test_cut_reasons(self) -> None:
        modules = directories(12).plan().modules
        self.assertEqual(
            [(m.path, m.mode, m.reason) for m in modules[6:10]],
            [
                ("d06", Mode.PROSE, "ranked 7 of 12"),
                ("d07", Mode.PROSE, "ranked 8 of 12"),
                ("d08", Mode.STUB, "below module cut"),
                ("d09", Mode.STUB, "below module cut"),
            ],
        )

    def test_a_large_rank_is_grouped(self) -> None:
        modules = directories(4_000).plan().modules
        self.assertEqual(modules[999].reason, "ranked 1,000 of 4,000")


class TestArchitecture(unittest.TestCase):
    def test_overview_rules(self) -> None:
        two_singles = Project()
        two_singles.add("a/x.py", sym("run"))
        two_singles.add("b/y.py", sym("run"))
        tested = uniform(4)
        tested.add("tests/test_f.py", sym("test_run"), role=Role.TEST)
        cases = [
            (two_singles, PlannerOptions(), Mode.PROSE, "project overview"),
            (uniform(5), PlannerOptions(), Mode.PROSE, "project overview"),
            (uniform(4), PlannerOptions(), Mode.STUB, "project too small"),
            (tested, PlannerOptions(), Mode.STUB, "project too small"),
            (Project(), PlannerOptions(), Mode.STUB, "project too small"),
            (uniform(50), PlannerOptions(architecture=False), Mode.STUB, "disabled"),
        ]
        for index, (project, options, mode, reason) in enumerate(cases):
            with self.subTest(case=index):
                plan = project.plan(options)
                self.assertEqual(
                    (plan.architecture, plan.architecture_reason), (mode, reason)
                )


def priced() -> Plan:
    """An overview, two modules and four files, three of them with prose."""
    return Plan(
        files=[
            FilePlan("a.py", 5.0, 1.0, Mode.PROSE, "ranked 1 of 3"),
            FilePlan("b.py", 4.0, 1.0, Mode.PROSE, "ranked 2 of 3"),
            FilePlan("c.py", 3.0, 1.0, Mode.PROSE, "ranked 3 of 3"),
            FilePlan("d.py", 2.0, 0.0, Mode.STUB, "fully documented in source"),
        ],
        modules=[
            ModulePlan("core", ("a.py", "b.py"), 9.0, Mode.PROSE, "ranked 1 of 2"),
            ModulePlan("util", ("c.py", "d.py"), 5.0, Mode.PROSE, "ranked 2 of 2"),
        ],
        architecture=Mode.PROSE,
        architecture_reason="project overview",
    )


COSTS = {
    ("architecture", ""): 0.05,
    ("module", "core"): 0.03,
    ("module", "util"): 0.03,
    ("file", "a.py"): 0.02,
    ("file", "b.py"): 0.04,
    ("file", "c.py"): 0.01,
    ("file", "d.py"): 9.99,
}


def ordered() -> Plan:
    """A plan whose priority order is neither alphabetical nor by score.

    web/views.py outranks api/routes.py on value (2.0 against 1.5) despite its
    lower score, importers lift web above api, and every stub has a price.
    """
    return Plan(
        files=[
            FilePlan("web/views.py", 2.0, 1.0, Mode.PROSE, "ranked 1 of 3"),
            FilePlan("api/routes.py", 6.0, 0.25, Mode.PROSE, "ranked 2 of 3"),
            FilePlan("web/forms.py", 1.0, 1.0, Mode.PROSE, "ranked 3 of 3"),
            FilePlan(
                "api/schema.py", 1.0, 0.0, Mode.STUB, "fully documented in source"
            ),
            FilePlan("lone/tool.kt", 1.0, 0.0, Mode.STUB, "no parser for kotlin"),
        ],
        modules=[
            ModulePlan(
                "web",
                ("web/forms.py", "web/views.py"),
                9.0,
                Mode.PROSE,
                "ranked 1 of 2",
            ),
            ModulePlan("lone", ("lone/tool.kt",), 8.0, Mode.STUB, "single file"),
            ModulePlan(
                "api",
                ("api/routes.py", "api/schema.py"),
                7.0,
                Mode.PROSE,
                "ranked 2 of 2",
            ),
        ],
        architecture_reason="disabled",
    )


ORDERED_COSTS = {
    ("architecture", ""): 0.01,
    ("module", "web"): 0.03,
    ("module", "lone"): 0.01,
    ("module", "api"): 0.03,
    ("file", "web/views.py"): 0.02,
    ("file", "api/routes.py"): 0.02,
    ("file", "web/forms.py"): 0.02,
    ("file", "api/schema.py"): 0.01,
    ("file", "lone/tool.kt"): 0.01,
}


COST, REQUESTS = "max_cost", "max_requests"   # the limits, as the record names them


def cuts(cut: Mapping[tuple[str, str], str]) -> list[tuple[str, str, str]]:
    """What `fit_to_budget` cut, in walk order, each with the limit that cut it."""
    return [(kind, key, limit) for (kind, key), limit in cut.items()]


class TestFitToBudget(unittest.TestCase):
    def test_within_limits_nothing_is_cut(self) -> None:
        # the prose items sum to 0.18; d.py costs 9.99 but is a stub, never walked
        for cost, requests in ((0.0, 0), (0.18, 0), (0.0, 6), (0.18, 6)):
            with self.subTest(max_cost=cost, max_requests=requests):
                plan = priced()
                fit = fit_to_budget(plan, COSTS, max_cost=cost, max_requests=requests)
                self.assertEqual((fit.cut, fit.rebuilt), ({}, frozenset()))
                self.assertEqual(plan, priced())

    def test_cost_cap_skips_what_overflows_and_keeps_walking(self) -> None:
        plan = priced()
        # 0.05 + 0.03 + 0.03 = 0.11: a and b overflow 0.12, c lands on it exactly
        cut = fit_to_budget(plan, COSTS, max_cost=0.12).cut
        self.assertEqual(cuts(cut), [("file", "a.py", COST), ("file", "b.py", COST)])
        self.assertEqual(
            [(p.relpath, p.mode, p.reason) for p in plan.files],
            [
                ("a.py", Mode.STUB, "over budget"),
                ("b.py", Mode.STUB, "over budget"),
                ("c.py", Mode.PROSE, "ranked 3 of 3"),
                ("d.py", Mode.STUB, "fully documented in source"),
            ],
        )
        self.assertEqual(plan.calls(), 4)

    def test_a_cut_overview_leaves_room_for_the_rest(self) -> None:
        plan = priced()
        cut = fit_to_budget(plan, COSTS, max_cost=0.04).cut
        self.assertEqual(
            cuts(cut),
            [
                ("architecture", "", COST),
                ("module", "util", COST),
                ("file", "a.py", COST),
                ("file", "b.py", COST),
            ],
        )
        self.assertEqual(
            (plan.architecture, plan.architecture_reason), (Mode.STUB, "over budget")
        )
        self.assertEqual([m.path for m in plan.prose_modules()], ["core"])
        self.assertEqual([p.relpath for p in plan.prose_files()], ["c.py"])

    def test_exact_fit_survives_float_noise(self) -> None:
        costs = {("file", "a.py"): 0.1, ("file", "b.py"): 0.2}
        cases: list[tuple[float, list[tuple[str, str, str]]]] = [
            (0.3, []),  # 0.1 + 0.2 is 0.30000000000000004
            (0.3 - 1e-10, [("file", "b.py", COST)]),  # noise is forgiven, overspend not
            (0.29, [("file", "b.py", COST)]),
        ]
        for limit, expected in cases:
            plan = Plan(
                files=[
                    FilePlan("a.py", 2.0, 1.0, Mode.PROSE, "ranked 1 of 2"),
                    FilePlan("b.py", 1.0, 1.0, Mode.PROSE, "ranked 2 of 2"),
                ]
            )
            with self.subTest(max_cost=limit):
                cut = fit_to_budget(plan, costs, max_cost=limit).cut
                self.assertEqual(cuts(cut), expected)

    def test_request_cap(self) -> None:
        plan = priced()
        cut = fit_to_budget(plan, COSTS, max_requests=3).cut
        self.assertEqual(
            cuts(cut),
            [("file", "a.py", REQUESTS), ("file", "b.py", REQUESTS),
             ("file", "c.py", REQUESTS)],
        )
        self.assertEqual(plan.calls(), 3)

    def test_overviews_are_kept_before_files(self) -> None:
        files = [("file", "a.py"), ("file", "b.py"), ("file", "c.py")]
        cases: list[tuple[int, list[tuple[str, str]]]] = [
            (1, [("module", "core"), ("module", "util"), *files]),  # the overview
            (2, [("module", "util"), *files]),  # and the best module
        ]
        for requests, expected in cases:
            plan = priced()
            with self.subTest(max_requests=requests):
                cut = fit_to_budget(plan, COSTS, max_requests=requests).cut
                self.assertEqual(cuts(cut), [(*item, REQUESTS) for item in expected])
                self.assertIs(plan.architecture, Mode.PROSE)

    def test_both_limits_apply(self) -> None:
        plan = priced()
        costs = {key: value for key, value in COSTS.items() if key != ("file", "c.py")}
        # util overflows 0.10; a lands on it as request 3; b overflows both
        # limits, and is the requests' to cut, as Budget checks them first; c
        # is free but would be request 4
        cut = fit_to_budget(plan, costs, max_cost=0.10, max_requests=3).cut
        self.assertEqual(
            cuts(cut),
            [("module", "util", COST), ("file", "b.py", REQUESTS),
             ("file", "c.py", REQUESTS)],
        )

    def test_missing_costs_are_free_but_still_requests(self) -> None:
        plan = priced()
        costs = {("architecture", ""): 0.05}
        cut = fit_to_budget(plan, costs, max_cost=0.05, max_requests=4).cut
        self.assertEqual(
            cuts(cut), [("file", "b.py", REQUESTS), ("file", "c.py", REQUESTS)]
        )

    def test_the_walk_follows_plan_order(self) -> None:
        later = [("file", "api/routes.py"), ("file", "web/forms.py")]
        cases: list[tuple[float, int, list[tuple[str, str]], str]] = [
            # web before api as ranked, not by name; priced stubs are not walked
            (0.0, 1, [("module", "api"), ("file", "web/views.py"), *later], REQUESTS),
            # files by value: views before routes, despite routes' higher score
            (0.0, 3, later, REQUESTS),
            (0.08, 0, later, COST),  # 0.03 + 0.03 + 0.02
        ]
        for max_cost, max_requests, expected, limit in cases:
            plan = ordered()
            with self.subTest(max_cost=max_cost, max_requests=max_requests):
                cut = fit_to_budget(
                    plan, ORDERED_COSTS, max_cost=max_cost, max_requests=max_requests
                ).cut
                self.assertEqual(cuts(cut), [(*item, limit) for item in expected])
                self.assertEqual(plan.architecture_reason, "disabled")
                self.assertEqual(plan.modules[1], ordered().modules[1])
                self.assertEqual(plan.files[3:], ordered().files[3:])

    def test_everything_cut_in_walk_order(self) -> None:
        plan = priced()
        cut = fit_to_budget(plan, COSTS, max_cost=0.001).cut
        self.assertEqual(
            cuts(cut),
            [
                ("architecture", "", COST),
                ("module", "core", COST),
                ("module", "util", COST),
                ("file", "a.py", COST),
                ("file", "b.py", COST),
                ("file", "c.py", COST),
            ],
        )
        self.assertEqual(plan.calls(), 0)
        self.assertEqual({m.reason for m in plan.modules}, {"over budget"})
        self.assertEqual(plan.files[3].reason, "fully documented in source")


def shop_walk() -> Plan:
    """The plan of a shop: an overview, the shop module, then its six files
    by value, all with prose."""
    names = ("cart", "cli", "money", "orders", "tax", "__init__")
    files = [
        FilePlan(f"shop/{name}.py", 6.0 - index, 1.0, Mode.PROSE,
                 f"ranked {index + 1} of 6")
        for index, name in enumerate(names)
    ]
    shop = ModulePlan("shop", tuple(f.relpath for f in files), 9.0, Mode.PROSE,
                      "ranked 1 of 1")
    return Plan(files, [shop], Mode.PROSE, "project overview")


def kept(plan: Plan) -> list[str]:
    """What still has prose, in walk order: the overview, modules, files."""
    names = ["architecture"] * (plan.architecture is Mode.PROSE)
    names += [module.path for module in plan.prose_modules()]
    return names + [item.relpath for item in plan.prose_files()]


OVERVIEW, CORE, UTIL = ("architecture", ""), ("module", "core"), ("module", "util")
A, B, C = ("file", "a.py"), ("file", "b.py"), ("file", "c.py")


class TestCachedItemsAreFree(unittest.TestCase):
    """An item the cache answers makes no call: no limit counts it or cuts it.
    Regression: the walk counted each as a request, against a limit raised by
    their number, so calls walked first took their share and the free pages
    were cut to stubs."""

    # cart.py and cli.py changed since the last run; the other four are cached
    CACHED = frozenset({("file", "shop/money.py"), ("file", "shop/orders.py"),
                        ("file", "shop/tax.py"), ("file", "shop/__init__.py")})

    def test_after_two_files_change_every_cached_page_is_kept(self) -> None:
        calls = [("module", "shop"), ("file", "shop/cart.py"), ("file", "shop/cli.py")]
        for requests, bought, expected in (
            (1, ["architecture"], calls),
            (2, ["architecture", "shop"], calls[1:]),
            (4, ["architecture", "shop", "shop/cart.py", "shop/cli.py"], []),
        ):
            plan = shop_walk()
            with self.subTest(max_requests=requests):
                cut = fit_to_budget(plan, {}, max_requests=requests,
                                    cached=self.CACHED).cut
                self.assertEqual(cuts(cut), [(*item, REQUESTS) for item in expected])
                prose = {("file", p.relpath) for p in plan.prose_files()}
                self.assertLessEqual(self.CACHED, prose)
                self.assertEqual([name for name in kept(plan)
                                  if ("file", name) not in self.CACHED], bought)

    def test_a_cached_item_anywhere_in_the_walk_takes_no_request(self) -> None:
        for where, cached, requests, expected in (
            # a.py is the one call: core or util would rebuild the overview too
            ("start", OVERVIEW, 1, [CORE, UTIL, B, C]),
            ("middle", UTIL, 2, [A, B, C]),               # overview and core
            ("middle", B, 4, [C]),                        # overview, modules, a
            ("end", C, 1, [CORE, UTIL, A, B]),            # the overview
        ):
            plan = priced()
            with self.subTest(where, cached=cached):
                fit = fit_to_budget(plan, COSTS, max_requests=requests,
                                    cached={cached})
                self.assertEqual(cuts(fit.cut),
                                 [(*item, REQUESTS) for item in expected])
                self.assertEqual(plan.calls() - 1, requests)   # all but it are calls
                self.assertEqual(fit.rebuilt, frozenset())

    def test_a_cached_item_costs_nothing_whatever_its_quote(self) -> None:
        # a.py's call would be 0.02, but the cache answers it: 0.06 holds the
        # overview and c.py, which fits only while a.py comes free.
        plan = priced()
        cut = fit_to_budget(plan, COSTS, max_cost=0.06, cached={A}).cut
        self.assertEqual(kept(plan), ["architecture", "a.py", "c.py"])
        self.assertEqual(cuts(cut), [(*CORE, COST), (*UTIL, COST), (*B, COST)])

    def test_both_limits_cut_around_a_cached_item(self) -> None:
        # core overflows the cost; util and a spend the requests with the
        # overview; b is cached and kept; c finds no request left.
        costs = {("architecture", ""): 0.01, ("module", "core"): 0.50,
                 ("module", "util"): 0.01, ("file", "a.py"): 0.01,
                 ("file", "b.py"): 0.01, ("file", "c.py"): 0.01}
        plan = priced()
        cut = fit_to_budget(plan, costs, max_cost=0.10, max_requests=3,
                            cached={("file", "b.py")}).cut
        self.assertEqual(
            cuts(cut), [("module", "core", COST), ("file", "c.py", REQUESTS)]
        )
        self.assertEqual([p.relpath for p in plan.prose_files()], ["a.py", "b.py"])
        self.assertEqual([m.path for m in plan.prose_modules()], ["util"])


class TestOverviewsOfWhatIsBought(unittest.TestCase):
    """A cached overview is free only while the run sends its prompt as the
    cache holds it. A module's prompt carries its files' purposes and the
    architecture's its modules', so buying one rebuilds the overview: it is
    then a call at its quote, and what rebuilds it is kept only if both fit.
    Regression: it stayed free, the run asked it again, the budget refused,
    and the run stopped (exit 7) with that page, which had prose, a stub."""

    def fitted(self, cached: set[tuple[str, str]], *, max_cost: float = 0.0,
               max_requests: int = 0) -> tuple[Plan, Fit]:
        plan = priced()
        return plan, fit_to_budget(plan, COSTS, max_cost=max_cost,
                                   max_requests=max_requests, cached=cached)

    def test_a_module_whose_file_is_bought_is_a_call(self) -> None:
        # a.py rebuilds core: two requests, then b.py one, so c.py is left out
        plan, fit = self.fitted({CORE}, max_requests=5)
        self.assertEqual(kept(plan), ["architecture", "core", "util", "a.py", "b.py"])
        self.assertEqual((cuts(fit.cut), fit.rebuilt), ([(*C, REQUESTS)], {CORE}))

    def test_what_fits_alone_but_not_with_its_overview_is_cut(self) -> None:
        # a.py and b.py would each take core along: past the three requests
        plan, fit = self.fitted({CORE}, max_requests=3)
        self.assertEqual(kept(plan), ["architecture", "core", "util", "c.py"])
        self.assertEqual((cuts(fit.cut), fit.rebuilt),
                         ([(*A, REQUESTS), (*B, REQUESTS)], frozenset()))

    def test_a_rebuilt_overview_costs_its_quote(self) -> None:
        # 0.05 + 0.03 spent; a.py with core, 0.05, and b.py with it, 0.07,
        # pass 0.12; c.py fits
        plan, fit = self.fitted({CORE}, max_cost=0.12)
        self.assertEqual(kept(plan), ["architecture", "core", "util", "c.py"])
        self.assertEqual((cuts(fit.cut), fit.rebuilt),
                         ([(*A, COST), (*B, COST)], frozenset()))

    def test_a_module_over_files_cached_or_cut_stays_free(self) -> None:
        for files, limit, expected in (
            ({A, B}, 3, ["architecture", "core", "util", "a.py", "b.py", "c.py"]),
            (set(), 2, ["architecture", "core", "util"]),   # its files cut
        ):
            with self.subTest(cached=sorted(files)):
                plan, fit = self.fitted({CORE, *files}, max_requests=limit)
                self.assertEqual(kept(plan), expected)
                self.assertEqual(fit.rebuilt, frozenset())

    def test_the_architecture_over_a_bought_module_is_a_call(self) -> None:
        # core takes the overview along; util and a.py do not again
        plan, fit = self.fitted({OVERVIEW}, max_requests=4)
        self.assertEqual(kept(plan), ["architecture", "core", "util", "a.py"])
        self.assertEqual((cuts(fit.cut), fit.rebuilt),
                         ([(*B, REQUESTS), (*C, REQUESTS)], {OVERVIEW}))

    def test_the_architecture_over_modules_cached_or_cut_stays_free(self) -> None:
        everything = {OVERVIEW, CORE, UTIL, A, B, C}
        for cached, limit, expected in (
            (everything, 1, ["architecture", "core", "util", "a.py", "b.py", "c.py"]),
            ({OVERVIEW}, 1, ["architecture", "a.py"]),   # each module cut
        ):
            with self.subTest(cached=sorted(cached)):
                plan, fit = self.fitted(cached, max_requests=limit)
                self.assertEqual(kept(plan), expected)
                self.assertEqual(fit.rebuilt, frozenset())

    def test_a_file_bought_rebuilds_its_module_and_the_architecture(self) -> None:
        # a.py takes core and the overview along: three requests
        plan, fit = self.fitted({OVERVIEW, CORE, UTIL}, max_requests=3)
        self.assertEqual(kept(plan), ["architecture", "core", "util", "a.py"])
        self.assertEqual((cuts(fit.cut), fit.rebuilt),
                         ([(*B, REQUESTS), (*C, REQUESTS)], {OVERVIEW, CORE}))
        plan, fit = self.fitted({OVERVIEW, CORE, UTIL}, max_requests=2)
        self.assertEqual(kept(plan), ["architecture", "core", "util"])
        self.assertEqual(fit.rebuilt, frozenset())

    def test_each_rebuild_is_spent_once_and_counted_against_the_cost(self) -> None:
        # a.py takes core and the overview along, 0.10; b.py then fits, 0.14;
        # c.py with util, 0.04 more, would pass the limit
        plan, fit = self.fitted({OVERVIEW, CORE, UTIL}, max_cost=0.14)
        self.assertEqual(kept(plan), ["architecture", "core", "util", "a.py", "b.py"])
        self.assertEqual((cuts(fit.cut), fit.rebuilt), ([(*C, COST)], {OVERVIEW, CORE}))

    def test_a_file_no_overview_sums_up_rebuilds_nothing(self) -> None:
        # The tool's directory is a single file, so it has no module prose.
        tool = FilePlan("extra/tool.py", 1.0, 1.0, Mode.PROSE, "ranked 7 of 7")
        lone = ModulePlan("extra", ("extra/tool.py",), 1.0, Mode.STUB, "single file")
        plan = shop_walk()
        plan.files.append(tool)
        plan.modules.append(lone)
        cached = {("architecture", ""), ("module", "shop")} | {
            ("file", item.relpath) for item in plan.files[:-1]}
        fit = fit_to_budget(plan, {}, max_requests=1, cached=cached)
        self.assertEqual(kept(plan)[-1], "extra/tool.py")
        self.assertEqual((fit.cut, fit.rebuilt), ({}, frozenset()))


class TestPlan(unittest.TestCase):
    def test_demote_each_kind(self) -> None:
        plan = priced()
        plan.demote("file", "b.py", "over budget")
        plan.demote("module", "util", "over budget")
        plan.demote("architecture", "", "over budget")
        self.assertEqual([p.relpath for p in plan.prose_files()], ["a.py", "c.py"])
        self.assertEqual([m.path for m in plan.prose_modules()], ["core"])
        self.assertEqual(plan.files[1].reason, "over budget")
        self.assertEqual(plan.architecture_reason, "over budget")
        self.assertEqual(plan.calls(), 3)

    def test_demote_rejects_unknown_items(self) -> None:
        plan = priced()
        with self.assertRaises(KeyError):
            plan.demote("file", "missing.py", "over budget")
        with self.assertRaises(KeyError):
            plan.demote("module", "missing", "over budget")
        with self.assertRaises(ValueError):
            plan.demote("symbol", "a.py", "over budget")


if __name__ == "__main__":
    unittest.main()
