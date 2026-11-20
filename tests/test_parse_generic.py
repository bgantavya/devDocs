"""The spec-driven scanner, per language, against fixture files.

Fixtures live in tests/fixtures/parse/ as real source files so they can be
opened, edited, and compared with what a language's own tooling reports.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind, get_parser
from devdocs.scan import LANGUAGES

FIXTURES = Path(__file__).parent / "fixtures" / "parse"


def parse_fixture(name: str) -> FileFacts:
    path = FIXTURES / name
    parser = get_parser(LANGUAGES[path.suffix])
    assert parser is not None
    return parser.parse(path, path.read_text(encoding="utf-8"))


def parse_text(source: str, suffix: str) -> FileFacts:
    parser = get_parser(LANGUAGES[suffix])
    assert parser is not None
    return parser.parse(Path("inline" + suffix), source)


class TestLines(unittest.TestCase):
    def test_lines_are_counted_whatever_ends_the_file(self) -> None:
        # Regression: a final newline counted as one more line.
        for source, loc in {"": 0, "\n": 1, "x := 1": 1, "x := 1\n": 1,
                            "x := 1\n\n": 2, "a()\nb()": 2, "a()\nb()\n": 2,
                            "a()\r\nb()\r\n": 2}.items():
            with self.subTest(source=source):
                self.assertEqual(parse_text(source, ".go").loc, loc)

    def test_a_scan_that_fails_still_counts_them(self) -> None:
        parser = get_parser("go")
        assert parser is not None
        failing = mock.patch("devdocs.parse.generic.mask", side_effect=RecursionError)
        with failing:
            facts = parser.parse(Path("inline.go"), "a()\nb()\n")
        self.assertIs(facts.confidence, Confidence.FAILED)
        self.assertEqual(facts.loc, 2)

    def test_what_nothing_closes_ends_on_the_last_line(self) -> None:
        # Regression: it ended one past the last line of a file with a final newline.
        for source, suffix, spans in (
            ("func Run() {\n\tx()\n", ".go", {"Run": (1, 2)}),
            ("func Run() {\n\tx()", ".go", {"Run": (1, 2)}),
            ("func Run() {\n\tx()\n\n", ".go", {"Run": (1, 3)}),
            ("func Run(\n", ".go", {"Run": (1, 1)}),
            ("pub struct S {\n    a: u8,\n", ".rs", {"S": (1, 2)}),
            ("function f() {\n  x();\n", ".js", {"f": (1, 2)}),
            ("export class C {\n  m() {\n    y();\n", ".ts",
             {"C": (1, 3), "m": (2, 3)}),
            ("public class A {\n  int x;\n", ".java", {"A": (1, 2)}),
        ):
            with self.subTest(source=source):
                facts = parse_text(source, suffix)
                self.assertEqual({symbol.name: (symbol.line, symbol.end_line)
                                  for top in facts.symbols for symbol in top.walk()},
                                 spans)

    def test_a_closed_block_still_ends_on_its_brace(self) -> None:
        facts = parse_text("func Run() {\n\tx()\n}\n\nvar V = 1\n", ".go")
        self.assertEqual(index(facts)["Run"].end_line, 3)
        self.assertEqual(index(facts)["V"].end_line, 5)

    def test_no_cut_of_a_file_puts_a_symbol_past_its_end(self) -> None:
        for suffix, whole in {
            ".go": ("func Run() {\n\tx()\n}\n\ntype T struct {\n\tA int\n}\n\n"
                    "var V = 1\nfunc Many(\n\ta int,\n) {\n}\n"),
            ".rs": "pub fn f() {\n    x();\n}\n\npub struct S {\n    a: u8,\n}\n",
            ".ts": ("export class C {\n  m() {\n    y();\n  }\n}\n"
                    "export const g = () => {\n  z();\n};\n"
                    "export const h = (a: number) =>\n  a + 1;\n"
                    "export type T = {\n  a: number;\n};\n"),
            ".java": "public class A {\n  public void m() {\n    y();\n  }\n}\n",
        }.items():
            for cut in range(len(whole) + 1):
                source = whole[:cut]
                facts = parse_text(source, suffix)
                for top in facts.symbols:
                    for symbol in top.walk():
                        self.assertLessEqual(symbol.end_line, facts.loc,
                                             f"{symbol.name} in {source!r}")


def index(facts: FileFacts) -> dict[str, Symbol]:
    return {symbol.name: symbol for symbol in facts.symbols}


class TestTypeScript(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = parse_fixture("checkout.tsx")
        self.top = index(self.facts)

    def test_language_follows_suffix(self) -> None:
        self.assertEqual(self.facts.language, "typescript")
        self.assertEqual(parse_text("function f() {}\n", ".js").language, "javascript")

    def test_confidence_is_heuristic(self) -> None:
        self.assertIs(self.facts.confidence, Confidence.HEURISTIC)

    def test_imports_include_multiline_and_dynamic_but_not_commented(self) -> None:
        self.assertEqual(
            set(self.facts.imports), {"react", "../types", "./money", "./lazy"}
        )

    def test_file_level_doc(self) -> None:
        self.assertEqual(self.facts.module_docstring, "Checkout page and helpers.")

    def test_declaration_kinds(self) -> None:
        self.assertIs(self.top["CheckoutProps"].kind, SymbolKind.INTERFACE)
        self.assertIs(self.top["Status"].kind, SymbolKind.TYPE)
        self.assertIs(self.top["Checkout"].kind, SymbolKind.FUNCTION)
        self.assertIs(self.top["CheckoutPage"].kind, SymbolKind.FUNCTION)
        self.assertIs(self.top["PaymentService"].kind, SymbolKind.CLASS)

    def test_export_visibility(self) -> None:
        self.assertTrue(self.top["Checkout"].is_public)
        self.assertFalse(self.top["MAX_RETRIES"].is_public)
        # Exported only through `export { helper as publicHelper }`.
        self.assertTrue(self.top["helper"].is_public)

    def test_export_list_does_not_hide_inline_exports(self) -> None:
        public = {s.name for s in self.facts.public_symbols}
        self.assertLessEqual({"helper", "Checkout", "PaymentService"}, public)

    def test_class_members(self) -> None:
        members = {m.name: m for m in self.top["PaymentService"].children}
        self.assertEqual(set(members), {"charge", "#internal", "create", "handle"})
        self.assertTrue(members["charge"].is_async)
        self.assertTrue(members["handle"].is_async)
        self.assertFalse(members["#internal"].is_public)

    def test_bases_and_docs(self) -> None:
        self.assertEqual(self.top["PaymentService"].bases, ("Base", "Payer"))
        self.assertEqual(self.top["CheckoutProps"].bases, ("BaseProps", "Themed"))
        self.assertEqual(self.top["CheckoutProps"].docstring, "Props for the checkout.")

    def test_arrow_signature_ends_at_arrow(self) -> None:
        self.assertTrue(self.top["Checkout"].signature.endswith("=>"))
        self.assertEqual(self.top["Status"].signature, "export type Status")

    def test_script_without_exports_is_all_public(self) -> None:
        facts = parse_text("function a() {}\nfunction b() {}\n", ".js")
        self.assertTrue(all(s.is_public for s in facts.symbols))

    def test_commonjs_exports(self) -> None:
        facts = parse_text(
            "function a() {}\nfunction b() {}\nmodule.exports = { a };\n", ".js"
        )
        self.assertEqual({s.name for s in facts.public_symbols}, {"a"})

    def test_overloads_are_deduplicated(self) -> None:
        facts = parse_text(
            "export function f(a: string): void;\n"
            "export function f(a: any) { return a; }\n",
            ".ts",
        )
        self.assertEqual([s.name for s in facts.symbols], ["f"])


class TestGo(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = parse_fixture("store.go")
        self.top = index(self.facts)

    def test_imports_from_block_and_single(self) -> None:
        self.assertEqual(
            set(self.facts.imports), {"context", "fmt", "github.com/acme/db"}
        )

    def test_package_doc(self) -> None:
        self.assertEqual(self.facts.module_docstring, "Package store persists carts.")

    def test_receiver_methods_attach_to_their_type(self) -> None:
        methods = {m.name for m in self.top["Store"].children}
        self.assertEqual(methods, {"Get", "flush"})

    def test_capitalisation_decides_visibility(self) -> None:
        self.assertTrue(self.top["MaxItems"].is_public)
        self.assertFalse(self.top["minItems"].is_public)
        flush = next(m for m in self.top["Store"].children if m.name == "flush")
        self.assertFalse(flush.is_public)

    def test_interface_members_and_generics(self) -> None:
        self.assertEqual(
            {m.name for m in self.top["Reader"].children}, {"Read", "close"}
        )
        self.assertEqual(
            self.top["New"].signature, "func New[T any](conn *db.Conn) *Store"
        )

    def test_declaration_without_semicolon_ends_at_newline(self) -> None:
        self.assertEqual(self.top["ID"].signature, "type ID string")

    def test_method_for_type_in_another_file_stays_top_level(self) -> None:
        facts = parse_text("package p\n\nfunc (s *Elsewhere) Do() {}\n", ".go")
        self.assertEqual([s.name for s in facts.symbols], ["Do"])
        self.assertIs(facts.symbols[0].kind, SymbolKind.METHOD)


class TestRust(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = parse_fixture("cart.rs")
        self.top = index(self.facts)

    def test_use_paths(self) -> None:
        self.assertEqual(
            set(self.facts.imports),
            {"std::collections", "crate::money::Price", "serde::Serialize"},
        )

    def test_inner_doc(self) -> None:
        self.assertEqual(self.facts.module_docstring, "Cart engine.\nHandles pricing.")

    def test_impl_members_attach_to_struct(self) -> None:
        methods = {m.name for m in self.top["Cart"].children}
        # `new` must survive the keyword filter: it is Rust's constructor idiom.
        self.assertEqual(methods, {"new", "internal", "price"})

    def test_multiline_where_clause_keeps_members(self) -> None:
        price = [m for m in self.top["Cart"].children if m.name == "price"]
        self.assertEqual(len(price), 1)

    def test_visibility(self) -> None:
        children = {m.name: m for m in self.top["Cart"].children}
        self.assertTrue(children["new"].is_public)
        self.assertFalse(children["internal"].is_public)
        self.assertTrue(children["price"].is_public)   # via `impl Priced for`
        self.assertFalse(self.top["private_helper"].is_public)
        self.assertTrue(self.top["checkout"].is_public)   # pub(crate)

    def test_attribute_between_doc_and_item(self) -> None:
        self.assertEqual(self.top["Cart"].docstring, "A shopping cart.")

    def test_lifetimes_survive_into_signature(self) -> None:
        self.assertEqual(self.top["Cart"].signature, "pub struct Cart<'a>")
        self.assertTrue(self.top["checkout"].is_async)


class TestJava(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = parse_fixture("Cart.java")
        self.top = index(self.facts)

    def test_imports_strip_wildcards(self) -> None:
        self.assertEqual(
            set(self.facts.imports), {"java.util.List", "java.util.Collections"}
        )

    def test_class_members(self) -> None:
        members = {m.name: m for m in self.top["Cart"].children}
        self.assertEqual(set(members), {"MAX_ITEMS", "Cart", "total", "map"})
        self.assertIs(members["MAX_ITEMS"].kind, SymbolKind.CONSTANT)

    def test_constructor_keeps_its_visibility(self) -> None:
        constructor = next(m for m in self.top["Cart"].children if m.name == "Cart")
        self.assertTrue(constructor.is_public)
        self.assertEqual(constructor.signature, "public Cart(List<Item> items)")

    def test_text_block_does_not_yield_a_fake_method(self) -> None:
        names = {m.name for m in self.top["Cart"].children}
        self.assertNotIn("fake", names)

    def test_javadoc_stops_at_tags_and_skips_annotations(self) -> None:
        self.assertEqual(self.top["Cart"].docstring, "A cart of items.")

    def test_interface_members_are_implicitly_public(self) -> None:
        self.assertFalse(self.top["Priced"].is_public)
        self.assertTrue(all(m.is_public for m in self.top["Priced"].children))


class TestRobustness(unittest.TestCase):
    def test_unterminated_template_downgrades_confidence(self) -> None:
        facts = parse_text("export function a() {}\nconst s = `never closed\n", ".ts")
        self.assertIs(facts.confidence, Confidence.DEGRADED)
        self.assertTrue(facts.notes)

    def test_stray_closing_brace_does_not_hide_later_declarations(self) -> None:
        facts = parse_text("}\nexport function after() {}\n", ".ts")
        self.assertEqual([s.name for s in facts.symbols], ["after"])

    def test_code_inside_function_bodies_is_not_top_level(self) -> None:
        source = (
            "export function outer() {\n"
            "  const inner = () => 1;\n"
            "  class Local {}\n"
            "}\n"
        )
        facts = parse_text(source, ".ts")
        self.assertEqual([s.name for s in facts.symbols], ["outer"])

    def test_empty_source(self) -> None:
        for suffix in (".ts", ".go", ".rs", ".java"):
            facts = parse_text("", suffix)
            self.assertEqual(facts.symbols, ())
            self.assertTrue(facts.is_usable)


class TestHeaders(unittest.TestCase):
    """Regressions from review: where a declaration's header ends."""

    def sig(self, source: str, suffix: str, name: str) -> Symbol:
        found = {s.name: s for s in parse_text(source, suffix).all_symbols}
        return found[name]

    def test_shifts_and_comparisons_are_not_generics(self) -> None:
        go = "package x\n\nconst MaxSize = 1 << 20\n\nfunc Later() {}\n"
        self.assertEqual(self.sig(go, ".go", "MaxSize").signature,
                         "const MaxSize = 1 << 20")
        ts = ("export const isMobile = window.innerWidth < 768\n"
              "export const FLAG_READ = 1 << 0\n"
              "export function later() {}\n")
        mobile = self.sig(ts, ".ts", "isMobile")
        self.assertEqual((mobile.signature, mobile.end_line),
                         ("export const isMobile = window.innerWidth < 768", 1))
        self.assertEqual(self.sig(ts, ".ts", "FLAG_READ").signature,
                         "export const FLAG_READ = 1 << 0")

    def test_go_channel_and_inline_types_in_results(self) -> None:
        go = ("package x\n\n"
              "func (w *Watcher) Events() <-chan Event {\n\treturn w.ch\n}\n\n"
              "func Load(path string) map[string]interface{} {\n\treturn nil\n}\n")
        events = self.sig(go, ".go", "Events")
        self.assertEqual((events.signature, events.end_line),
                         ("func (w *Watcher) Events() <-chan Event", 5))
        load = self.sig(go, ".go", "Load")
        self.assertEqual((load.signature, load.end_line),
                         ("func Load(path string) map[string]interface{}", 9))

    def test_function_and_object_types_in_annotations(self) -> None:
        ts = ("export function onChange(cb: (v: string) => void): () => void {\n"
              "  return () => {};\n"
              "}\n"
              "export function shape(): { a: string; b: number } {\n"
              "  return { a: '', b: 0 };\n"
              "}\n")
        change = self.sig(ts, ".ts", "onChange")
        self.assertEqual(
            (change.signature, change.end_line),
            ("export function onChange(cb: (v: string) => void): () => void", 3),
        )
        shape = self.sig(ts, ".ts", "shape")
        self.assertEqual(
            (shape.signature, shape.end_line),
            ("export function shape(): { a: string; b: number }", 6),
        )

    def test_comments_inside_a_header_are_left_out(self) -> None:
        ts = ("export function createServer(\n"
              "  port: number, // listen here\n"
              '  host = "0.0.0.0", /* all interfaces */\n'
              "): Server {\n  return s;\n}\n")
        self.assertEqual(self.sig(ts, ".ts", "createServer").signature,
                         'export function createServer( port: number, '
                         'host = "0.0.0.0", ): Server')

    def test_rust_where_clause_belongs_to_the_header(self) -> None:
        rust = ("pub fn process<T>(items: &[T]) -> Vec<T>\n"
                "where\n    T: Clone + Debug,\n{\n    items.to_vec()\n}\n")
        process = self.sig(rust, ".rs", "process")
        self.assertEqual(process.signature,
                         "pub fn process<T>(items: &[T]) -> Vec<T> where "
                         "T: Clone + Debug,")
        self.assertEqual(process.end_line, 6)

    def test_wrapped_components_stop_before_their_body(self) -> None:
        ts = ("export const Card = memo(function Card({ title }: { title: string }) {\n"
              "  return <div>{title}</div>;\n});\n"
              "export const PUT = withAuth(async (req: NextRequest) => {\n"
              "  return ok();\n});\n")
        self.assertEqual(
            self.sig(ts, ".tsx", "Card").signature,
            "export const Card = memo(function Card({ title }: { title: string })",
        )
        self.assertEqual(self.sig(ts, ".tsx", "PUT").signature,
                         "export const PUT = withAuth(async (req: NextRequest) =>")


class TestStatements(unittest.TestCase):
    """Regressions from review: continuation lines are never declarations."""

    def test_java_wrapped_initialiser_is_not_a_method(self) -> None:
        java = ("public class View {\n"
                "    private final Map<String, Foo> cache =\n"
                "        new HashMap<>(\n"
                "            computeSize());\n"
                "    private final Foo foo = make(\n"
                "        new Foo(1, 2));\n"
                "    public String render() { return \"\"; }\n"
                "}\n")
        view = index(parse_text(java, ".java"))["View"]
        self.assertEqual([(c.name, c.kind.value) for c in view.children],
                         [("render", "method")])

    def test_ts_field_initialiser_calls_are_not_methods(self) -> None:
        ts = ("export class UserStore {\n"
              "  readonly selected = combineLatest([\n"
              "    this.users$,\n"
              "  ]).pipe(\n"
              "    map(([users]) => users[0]),\n"
              "  );\n"
              "  load(id: string): Observable<User> {\n"
              "    return this.http.get(id);\n"
              "  }\n"
              "}\n")
        store = index(parse_text(ts, ".ts"))["UserStore"]
        self.assertEqual([(c.name, c.signature) for c in store.children],
                         [("load", "load(id: string): Observable<User>")])

    def test_go_groups_track_parentheses(self) -> None:
        go = ("package x\n\nvar (\n"
              "\tclient = http.Client{\n\t\tTimeout: 5 * time.Second,\n\t}\n"
              "\tErrNotFound = errors.New(\n\t\t\"not found\",\n\t)\n"
              "\tAll = []string{\n\t\t\"a\",\n\t}\n"
              "\tTotal = First +\n\t\tSecond\n"
              ")\n")
        names = [s.name for s in parse_text(go, ".go").symbols]
        self.assertEqual(names, ["client", "ErrNotFound", "All", "Total"])

    def test_go_type_group_items_have_bodies(self) -> None:
        go = ("package x\n\ntype (\n"
              "\tRequest struct {\n\t\tURL string\n\t}\n"
              "\tDoer interface {\n\t\tDo(r *Request) error\n\t}\n"
              ")\n")
        found = index(parse_text(go, ".go"))
        request, doer = found["Request"], found["Doer"]
        self.assertEqual((request.signature, request.end_line), ("Request struct", 6))
        self.assertEqual((doer.signature, doer.end_line), ("Doer interface", 9))
        self.assertEqual([c.name for c in doer.children], ["Do"])


class TestShapes(unittest.TestCase):
    """Regressions from review: generics, impl blocks, nesting, visibility."""

    def test_bases_skip_the_declarations_own_type_parameters(self) -> None:
        java = ("public class SortedBox<T extends Comparable<T>> extends Container\n"
                "        implements Sized {\n}\n"
                "class Node<T extends Comparable<T>> {\n}\n")
        found = index(parse_text(java, ".java"))
        self.assertEqual(found["SortedBox"].bases, ("Container", "Sized"))
        self.assertEqual(found["Node"].bases, ())
        ts = ("export class Store extends BaseStore<State> implements Disposable {}\n"
              "export class Repository<T extends Entity> {}\n"
              "export class Admin extends Mixin(User, Timestamped) {}\n")
        found = index(parse_text(ts, ".ts"))
        self.assertEqual(found["Store"].bases, ("BaseStore", "Disposable"))
        self.assertEqual(found["Repository"].bases, ())
        self.assertEqual(found["Admin"].bases, ("Mixin(User, Timestamped)",))

    def test_nested_type_parameters_keep_the_declaration(self) -> None:
        go = ("package x\n\n"
              "func Index[S ~[]E, E comparable](s S, v E) int { return 0 }\n"
              "func Keys[M ~map[K]V, K comparable, V any](m M) []K { return nil }\n")
        self.assertEqual([s.name for s in parse_text(go, ".go").symbols],
                         ["Index", "Keys"])
        java = ("public class Util {\n"
                "    public static <T extends Comparable<? super T>> void sort("
                "List<T> list) {}\n"
                "    public static <T extends Comparable<T>> T max(List<T> xs) {\n"
                "        return null;\n    }\n}\n")
        util = index(parse_text(java, ".java"))["Util"]
        self.assertEqual([c.name for c in util.children], ["sort", "max"])
        ts = "export type Maybe<T = string> = T | null;\n"
        self.assertEqual([s.name for s in parse_text(ts, ".ts").symbols], ["Maybe"])

    def test_rust_impl_headers_attach_to_the_self_type(self) -> None:
        rust = ("pub struct Bytes(Vec<u8>);\n"
                "impl<'a> From<&'a [u8]> for Bytes {\n"
                "    fn from(s: &'a [u8]) -> Self { Bytes(s.to_vec()) }\n}\n"
                "pub struct Lines<'a>(&'a str);\n"
                "impl<'a, T: AsRef<str>> Lines<'a> {\n"
                "    pub fn count_lines(&self) -> usize { 0 }\n}\n")
        found = index(parse_text(rust, ".rs"))
        self.assertEqual([(c.name, c.is_public) for c in found["Bytes"].children],
                         [("from", True)])
        self.assertEqual([c.name for c in found["Lines"].children], ["count_lines"])
        self.assertNotIn("from", found)

    def test_java_enum_constants_and_nested_classes(self) -> None:
        java = ("public enum Planet {\n"
                "    MERCURY(3.303e+23, 2.4397e6),\n"
                "    VENUS(4.869e+24, 6.0518e6);\n"
                "    private final double mass;\n"
                "    Planet(double mass, double radius) { this.mass = mass; }\n"
                "    double mass() { return mass; }\n"
                "}\n"
                "public class User {\n"
                "    public static class Builder {\n"
                "        public Builder id(String id) { return this; }\n"
                "        public User build() { return new User(); }\n"
                "    }\n"
                "}\n")
        found = index(parse_text(java, ".java"))
        planet = [(c.name, c.kind.value, c.signature) for c in found["Planet"].children]
        self.assertEqual(planet[:2], [
            ("MERCURY", "constant", "MERCURY(3.303e+23, 2.4397e6)"),
            ("VENUS", "constant", "VENUS(4.869e+24, 6.0518e6)"),
        ])
        self.assertEqual([n for n, k, _ in planet if k == "method"], ["Planet", "mass"])
        builder = found["User"].children[0]
        self.assertEqual([c.name for c in builder.children], ["id", "build"])

    def test_private_interface_methods_are_private(self) -> None:
        java = ("public interface Greeter {\n"
                "    String name();\n"
                "    default String greet() { return prefix() + name(); }\n"
                "    private String prefix() { return \"Hi \"; }\n"
                "}\n")
        greeter = index(parse_text(java, ".java"))["Greeter"]
        self.assertEqual({c.name: c.is_public for c in greeter.children},
                         {"name": True, "greet": True, "prefix": False})

    def test_keywords_are_never_names(self) -> None:
        ts = ("export default class extends Base {}\n"
              "export interface Ctor { new (x: string): Thing; size(): number }\n"
              "functionRegistry.register(a);\n")
        names = [s.name for s in parse_text(ts, ".ts").all_symbols]
        self.assertNotIn("extends", names)
        self.assertNotIn("new", names)
        self.assertNotIn("functionRegistry", names)

    def test_modules_without_named_exports_keep_helpers_private(self) -> None:
        ts = ("import { defineConfig } from 'vite';\n"
              "function manualChunks(id) { return id; }\n"
              "export default defineConfig({ manualChunks });\n")
        facts = parse_text(ts, ".ts")
        self.assertEqual(facts.public_symbols, ())

    def test_type_only_export_specifiers_count(self) -> None:
        ts = ("interface ButtonProps { size: number }\n"
              "function Button(p: ButtonProps) { return null; }\n"
              "export { type ButtonProps, Button };\n")
        facts = parse_text(ts, ".ts")
        self.assertEqual(facts.exports, ("ButtonProps", "Button"))


if __name__ == "__main__":
    unittest.main()
