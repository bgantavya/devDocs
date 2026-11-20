"""The masker's one invariant is alignment: same length, same newlines."""

from __future__ import annotations

import random
import time
import unittest
from pathlib import Path

from devdocs.issues import comment_syntax
from devdocs.parse import get_parser
from devdocs.parse.languages import SPECS
from devdocs.parse.masking import Masked, Syntax, mask

JS = Syntax(template_literals=True, regex_literals=True)
RUST = Syntax(
    nested_block_comments=True, quotes='"', multiline_strings=True, rust_literals=True
)
GO = Syntax(raw_backticks=True)
JAVA = Syntax(text_blocks=True)
LUA = Syntax(line_comments=("--",), block_comment=("--[[", "]]"))


def blanked(source: str, fragment: str) -> str:
    """`source` with `fragment` (which must occur once) overwritten by spaces."""
    assert source.count(fragment) == 1, fragment
    spaces = "".join(c if c in "\r\n" else " " for c in fragment)
    return source.replace(fragment, spaces)


def span(source: str, fragment: str) -> tuple[int, int]:
    start = source.index(fragment)
    return start, start + len(fragment)


class MaskCase(unittest.TestCase):
    def masked(self, source: str, syntax: Syntax) -> str:
        result = mask(source, syntax)
        self.assertEqual(len(result.text), len(source), "length must be preserved")
        self.assertEqual(
            result.text.count("\n"), source.count("\n"), "newlines must be preserved"
        )
        return result.text

    def assert_hidden(self, source: str, syntax: Syntax, *hidden: str) -> None:
        text = self.masked(source, syntax)
        for fragment in hidden:
            self.assertNotIn(fragment, text)

    def assert_kept(self, source: str, syntax: Syntax, *kept: str) -> None:
        text = self.masked(source, syntax)
        for fragment in kept:
            self.assertIn(fragment, text)

    def assert_aligned(self, source: str, result: Masked) -> None:
        """Only blanks change, line breaks stay, comment spans are ordered and blank."""
        self.assertEqual(len(result.text), len(source))
        for before, after in zip(source, result.text, strict=True):
            if before != after:
                self.assertNotIn(before, "\r\n")
                self.assertEqual(after, " ")
        previous = 0
        for start, end in result.comments:
            self.assertTrue(previous <= start < end <= len(source), result.comments)
            self.assertEqual(result.text[start:end].strip(), "")
            previous = end


class TestComments(MaskCase):
    def test_line_and_block_comments(self) -> None:
        source = "a // class Hidden\n/* class Hidden2 */ class Real {}"
        self.assert_hidden(source, JS, "Hidden", "Hidden2")
        self.assert_kept(source, JS, "class Real")

    def test_block_comment_keeps_line_structure(self) -> None:
        text = self.masked("/*\nline\n*/x", JS)
        self.assertEqual(text.split("\n")[2].strip(), "x")

    def test_nested_rust_block_comment(self) -> None:
        source = "/* a /* fn fake() */ still */ fn real() {}"
        self.assert_hidden(source, RUST, "fake", "still")

    def test_unterminated_block_comment_is_counted(self) -> None:
        self.assertEqual(mask("x /* never", JS).unterminated, 1)

    def test_hashbang_line_is_a_comment(self) -> None:
        source = "#!/usr/bin/env node\nconst x = /a/;\n"
        result = mask(source, JS)
        self.assertEqual(result.text, " " * 19 + "\nconst x = / /;\n")
        self.assertEqual(result.comments, ((0, 19),))

    def test_hashbang_is_code_outside_js(self) -> None:
        self.assertEqual(mask("#![allow(x)]\nfn f() {}", RUST).comments, ())


class TestCommentSpans(MaskCase):
    """`Masked.comments`: where each comment was, for searching comments alone."""

    def test_line_and_block_spans_in_source_order(self) -> None:
        self.assertEqual(mask("a // x\n/* y */ b", JS).comments, ((2, 6), (7, 14)))

    def test_line_comment_ends_before_crlf(self) -> None:
        result = mask("a // x\r\nb", JS)
        self.assertEqual(result.comments, ((2, 6),))
        self.assertEqual(result.text, "a     \r\nb")

    def test_unterminated_block_comment_runs_to_the_end(self) -> None:
        self.assertEqual(mask("x /* never", JS).comments, ((2, 10),))

    def test_rust_nested_comment_is_one_span(self) -> None:
        source = "/* a /* b */ c */ fn x() {}"
        self.assertEqual(mask(source, RUST).comments, ((0, 17),))

    def test_rust_doc_comments(self) -> None:
        self.assertEqual(
            mask("/// doc\n//! inner\nfn x() {}", RUST).comments, ((0, 7), (8, 17))
        )

    def test_comment_text_inside_literals_is_not_a_comment(self) -> None:
        cases = [
            (JS, 's = "// a /* b"; t = `// c ${"/* d"}`; r = /\\/\\/ e/; // yes'),
            (JS, "x = <p>// f /* g https://x.io</p>; // yes"),
            (RUST, "let s = \"// a /* b\"; let r = r#\"// c\"#; let c = '/'; // yes"),
            (GO, 's := `// a` + "/* b" + string(\'/\') // yes'),
            (JAVA, "String s = \"\"\"\n// a\n\"\"\"; char c = '/'; // yes"),
        ]
        for syntax, source in cases:
            with self.subTest(source=source):
                comments = mask(source, syntax).comments
                self.assertEqual(comments, (span(source, "// yes"),))

    def test_comments_inside_jsx_tags_and_expressions(self) -> None:
        source = 'x = <div // a\n  b="c" /* d */>{/* e */}</div>;'
        result = mask(source, JS)
        self.assertEqual(
            result.comments,
            (span(source, "// a"), span(source, "/* d */"), span(source, "/* e */")),
        )
        self.assertEqual(result.text, 'x = <div     \n  b=" "        >{       }</div>;')

    def test_comment_inside_template_interpolation(self) -> None:
        source = "s = `${x /* TODO */}`;"
        self.assertEqual(mask(source, JS).comments, (span(source, "/* TODO */"),))

    def test_longer_block_opener_wins(self) -> None:
        source = "--[[ a\nb ]] x -- c"
        self.assertEqual(mask(source, LUA).comments, ((0, 11), (14, 18)))


class TestStrings(MaskCase):
    def test_contents_hidden_delimiters_kept(self) -> None:
        text = self.masked('x = "class Fake"', JS)
        self.assertNotIn("Fake", text)
        self.assertEqual(text.count('"'), 2)

    def test_escaped_quote_does_not_end_string(self) -> None:
        self.assert_hidden(r'x = "a \" class Fake"; class Real {}', JS, "Fake")

    def test_url_inside_string_is_not_a_comment(self) -> None:
        self.assert_kept('u = "http://x.com"; class Real {}', JS, "class Real")

    def test_jsx_apostrophe_is_not_a_string(self) -> None:
        # Regression: masking to end of line hid the brace and broke depth.
        source = "const C = () => <p>Don't {x}</p>;\nclass Real {}"
        self.assert_kept(source, JS, "{x}", "class Real")

    def test_java_text_block(self) -> None:
        self.assert_hidden('s = """\n class Fake {\n"""; class Real {}', JAVA, "Fake")

    def test_go_raw_string(self) -> None:
        self.assert_hidden("s := `func fake() {\n}`\nfunc Real() {}", GO, "fake")

    def test_line_continuation_in_crlf_file(self) -> None:
        # Regression: the escape skipped `\` and `\r`, then `\n` rejected the string.
        lf = "const tpl = 'function fake() {\\\n';\nexport function real() {}\n"
        expected = "const tpl = '" + " " * 18 + "\n';\nexport function real() {}\n"
        self.assertEqual(self.masked(lf, JS), expected)
        crlf = lf.replace("\n", "\r\n")
        self.assertEqual(self.masked(crlf, JS), expected.replace("\n", "\r\n"))


class TestTemplates(MaskCase):
    def test_nested_template_in_interpolation(self) -> None:
        source = "s = `a ${f(`inner } { ${z}`)} b`; class Real {}"
        self.assert_hidden(source, JS, "inner")
        self.assert_kept(source, JS, "class Real")

    def test_unterminated_template_is_counted(self) -> None:
        self.assertEqual(mask("s = `never", JS).unterminated, 1)

    def test_jsx_inside_template_is_blanked_with_it(self) -> None:
        source = "s = `a ${cond && <p>Don't {`x${y}`}</p>} b`; class Real {}"
        body = "a ${cond && <p>Don't {`x${y}`}</p>} b"
        self.assertEqual(self.masked(source, JS), blanked(source, body))


class TestRegexLiterals(MaskCase):
    def test_regex_containing_a_quote(self) -> None:
        self.assert_kept("const r = /\"[a-z]'/g; class Real {}", JS, "class Real")

    def test_division_is_not_a_regex(self) -> None:
        self.assert_kept("const a = b / c / d;", JS, "b / c / d")

    def test_regex_after_return_keyword(self) -> None:
        self.assert_hidden("return /class Fake/.test(s)", JS, "Fake")

    def test_jsx_closing_tag_is_not_a_regex(self) -> None:
        source = "return <div>x</div>;\nclass Real {}"
        self.assert_kept(source, JS, "</div>", "class Real")

    def test_regex_as_arrow_body(self) -> None:
        # Regression: `>` of `=>` was no regex preceder, so `\{` counted as a brace.
        cases = {
            "const opensBlock = (line) => /\\{\\s*$/.test(line);":
                "const opensBlock = (line) => /      /.test(line);",
            "const ext = urls.filter((u) => /^https?:\\/\\//.test(u));":
                "const ext = urls.filter((u) => /            /.test(u));",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.masked(source, JS), expected)

    def test_division_after_postfix_operand(self) -> None:
        # Regression: `x!`, `i++` and `obj.return` before `/` opened a regex
        # that ended on the first `/` of the trailing comment.
        for source in (
            "const half = size! / 2; // e.g. {w: 10 -> 5",
            "const avg = total++ / n; // see {",
            "const left = count-- / n; // see {",
            "const r = step.return / 2; // {",
        ):
            with self.subTest(source=source):
                comment = source.index("//")
                expected = source[:comment] + " " * (len(source) - comment)
                self.assertEqual(self.masked(source, JS), expected)

    def test_regex_after_prefix_bang_and_keywords(self) -> None:
        cases = {
            "if (!/^a/.test(s)) {}": "if (!/  /.test(s)) {}",
            "export default /a{2}/;": "export default /    /;",
            "x = y instanceof /a/.constructor;": "x = y instanceof / /.constructor;",
            "return typeof /}/;": "return typeof / /;",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.masked(source, JS), expected)

    def test_regex_starting_with_greater_than(self) -> None:
        source = 's = s.replace(/>/g, "&gt;");'
        self.assertEqual(self.masked(source, JS), 's = s.replace(/ /g, "    ");')

    def test_regex_followed_directly_by_division(self) -> None:
        source = "const r = /\\d+//gi.test(x); // {"
        self.assertEqual(self.masked(source, JS), "const r = /   //gi.test(x);     ")


class TestJsx(MaskCase):
    """JSX content is text: neither code, comment nor string."""

    def test_self_closing_tag_after_expression(self) -> None:
        # Regression: `}` before `/>` opened a regex up to the next `/`.
        sources = [
            "const field = (\n  <label>\n"
            "    <input value={value} /> {err && <span>{err}</span>}\n"
            "  </label>\n);\n",
            "  return <div>{error && <Alert message={error} />}</div>;\n",
            "    return <button><Icon name={icon} /> {label && <span>{label}</span>}"
            "</button>;",
            "x; <Icon name={icon} /> {label && <span>{label}</span>}",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.masked(source, JS), source)

    def test_apostrophe_in_text_does_not_pair_with_a_later_quote(self) -> None:
        # Regression: `Don't` paired with `'signup.cta'`, hiding the `{`.
        source = "    return <p>Don't have an account? {t('signup.cta')}</p>;"
        expected = "    return <p>Don't have an account? {t('          ')}</p>;"
        self.assertEqual(self.masked(source, JS), expected)

    def test_text_that_looks_like_code_is_left_alone(self) -> None:
        # Regression: a glob's `/*` opened a comment, a URL's `//` hid the line.
        for text in ("Don't", 'say "hi', "https://x.io/a", "**/*.ts", "a // b",
                     "/* open", "`tick", "${x}", "a > b", "return /re/"):
            with self.subTest(text=text):
                source = f"  return <p>{text} {{x('y')}}</p>;"
                expected = f"  return <p>{text} {{x(' ')}}</p>;"
                self.assertEqual(self.masked(source, JS), expected)

    def test_glob_in_text_does_not_swallow_code(self) -> None:
        source = (
            "export function IgnoreHelp() {\n"
            "  return <p>Patterns such as <code>**/*.test.ts</code> are skipped.</p>;\n"
            "}\n/** Saves the settings. */\nexport function save() {}\n"
        )
        result = mask(source, JS)
        self.assertEqual(result.text, blanked(source, "/** Saves the settings. */"))
        self.assertEqual(result.comments, (span(source, "/** Saves the settings. */"),))

    def test_url_in_text_keeps_the_brace_after_it(self) -> None:
        source = (
            "    return <p>Docs: https://example.com/docs {links.length > 0 && (\n"
            "      <ul>{links.map((l) => <li key={l}>{l}</li>)}</ul>\n"
            "    )}</p>;\n"
        )
        self.assertEqual(self.masked(source, JS), source)

    def test_attribute_strings_have_no_escapes_and_may_span_lines(self) -> None:
        source = "x = <a title=\"C:\\\" b='x\ny' />;"
        self.assertEqual(self.masked(source, JS), "x = <a title=\"   \" b=' \n ' />;")

    def test_generic_component_with_object_type_argument(self) -> None:
        source = 'x = <Table<{ a: 1 }> title="a > b" />;'
        expected = 'x = <Table<{ a: 1 }> title="     " />;'
        self.assertEqual(self.masked(source, JS), expected)

    def test_type_parameters_are_not_jsx(self) -> None:
        # A `<T>` component later in the file must not turn generics into JSX.
        later = "\nconst C = () => <T>hi</T>;\n"
        heads = (
            "<T,>(v: T)", "<T = string>(v: T)", "<T extends { a: string }>(v: T)",
            "<const T,>(v: T)", "<T extends keyof X>(v: T)",
        )
        for head in heads:
            with self.subTest(head=head):
                source = f"const f = {head} => v; // it's {{" + later
                self.assertEqual(self.masked(source, JS), blanked(source, "// it's {"))

    def test_type_assertion_and_comparison_stay_code(self) -> None:
        cases = [
            "const el = <HTMLElement>document.body; // it's {",
            "if (a < b && c > d) { x = 1; } // it's {",
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.masked(source, JS), blanked(source, "// it's {"))
        source = 'if (a < b && c > d) { x = "</a>"; }'
        self.assertEqual(self.masked(source, JS), 'if (a < b && c > d) { x = "    "; }')


class TestRust(MaskCase):
    def test_lifetimes_stay_as_code(self) -> None:
        source = "fn f<'a>(x: &'a str) -> &'a str { x }"
        self.assert_kept(source, RUST, "fn f<'a>(x: &'a str)")

    def test_char_literal_brace_is_hidden(self) -> None:
        text = self.masked("let c = '}'; fn g() {}", RUST)
        self.assertEqual(text.count("}"), 1)

    def test_escaped_char_literal(self) -> None:
        self.assert_kept(r"let c = '\n'; fn g() {}", RUST, "fn g()")

    def test_escaped_quote_char_literal(self) -> None:
        # Regression: `'\''` closed at the escaped quote, desyncing what followed.
        self.assertEqual(self.masked("let q = '\\'';", RUST), "let q = '  ';")
        self.assertEqual(
            self.masked("const QUOTES: [char; 2] = ['\\'','\"'];", RUST),
            "const QUOTES: [char; 2] = ['  ',' '];",
        )

    def test_raw_string_with_hashes(self) -> None:
        self.assert_hidden('let s = r#"fn fake() { "#; fn real() {}', RUST, "fake")

    def test_raw_c_string(self) -> None:
        self.assertEqual(
            self.masked('let s = cr"C:\\"; fn real() {}', RUST),
            'let s = cr"   "; fn real() {}',
        )

    def test_byte_string(self) -> None:
        self.assert_hidden('let b = b"fn fake"; fn real() {}', RUST, "fake")

    def test_r_inside_identifier_is_not_raw(self) -> None:
        self.assert_kept('let bar = "x"; fn real() {}', RUST, "let bar", "fn real")

    def test_multiline_string(self) -> None:
        self.assert_hidden('let s = "fn\nfake()"; fn real() {}', RUST, "fake")


def _syntaxes() -> list[Syntax]:
    """Every syntax in use: the scanner's languages and the comment-only ones."""
    found = [spec.syntax for spec in SPECS]
    found += [comment_syntax(name) for name in ("shell", "lua", "sql", "unknown")]
    return found


class TestLinearTime(unittest.TestCase):
    """200k characters of the worst input for a syntax take well under a second.

    Quadratic behaviour takes minutes at this size, so the generous limit
    cannot flake on a slow machine yet still catches it.
    """

    SIZE = 200_000
    LIMIT = 2.0

    def run_input(self, syntax: Syntax, text: str) -> None:
        start = time.perf_counter()
        result = mask(text, syntax)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result.text), len(text))
        self.assertEqual(result.text.count("\n"), text.count("\n"))
        self.assertLess(elapsed, self.LIMIT)

    def repeat(self, unit: str) -> str:
        return (unit * (self.SIZE // len(unit) + 1))[: self.SIZE]

    def test_common_traps_in_every_syntax(self) -> None:
        for syntax in _syntaxes():
            for unit in ("'", '"', "/*", "\\", "x\n"):
                with self.subTest(syntax=syntax, unit=unit):
                    self.run_input(syntax, self.repeat(unit))

    def test_language_specific_traps(self) -> None:
        by_language = {spec.languages[0]: spec.syntax for spec in SPECS}
        k = self.SIZE // 5
        cases = {
            "javascript": [
                self.repeat("`${"),                        # templates never closed
                "`${" * k + "}`" * k,                      # templates closed, deeply
                self.repeat("(/["), self.repeat("=/\\"),    # regexes missing their end
                "x = (<a>t</a>); y = (<a>" + "</" + " " * self.SIZE,
                self.repeat("(<a<"), self.repeat("x=(<a b='"),
                self.repeat("(<a>{"), self.repeat("=<T extends "),
            ],
            "rust": [self.repeat("r#"), self.repeat("'\\"), self.repeat("br##\"x\"#")],
            "go": [self.repeat("`"), self.repeat("'\\'")],
            "java": [self.repeat('"""'), self.repeat('"""\\')],
        }
        for language, texts in cases.items():
            for text in texts:
                with self.subTest(language=language, start=text[:12]):
                    self.run_input(by_language[language], text)


class TestInvariants(MaskCase):
    """Random sources never raise and keep every position aligned."""

    TOKENS = (
        "x", " ", "\n", "\r\n", "{", "}", "(", ")", "=", "=>", "<", ">", "/", "//",
        "/*", "*/", "\\", "'", '"', "`", "${", "<p>", "</p>", "<a b='", "/>",
        "return ", "#!", "r#\"", "'a", "--", "--[[", "]]", '"""', "!", "++",
    )

    def test_random_token_soup(self) -> None:
        rng = random.Random(20261101)
        for syntax in _syntaxes():
            for _ in range(150):
                size = rng.randint(0, 60)
                source = "".join(rng.choice(self.TOKENS) for _ in range(size))
                with self.subTest(syntax=syntax, source=source):
                    self.assert_aligned(source, mask(source, syntax))


def _symbols(language: str, name: str, source: str) -> list[tuple[str, int, int]]:
    parser = get_parser(language)
    assert parser is not None
    facts = parser.parse(Path(name), source)
    return [(symbol.name, symbol.line, symbol.end_line) for symbol in facts.symbols]


class TestDownstream(unittest.TestCase):
    """The review's repros, end to end: masking decides what counts as top level."""

    def names(self, language: str, name: str, source: str) -> list[str]:
        return [symbol for symbol, _, _ in _symbols(language, name, source)]

    def test_self_closing_tags_keep_brace_depth(self) -> None:
        form = (
            "export function Form({ err, value }: Props) {\n"
            "  const field = (\n"
            "    <label>\n"
            "      <input value={value} /> {err && <span>{err}</span>}\n"
            "    </label>\n"
            "  );\n"
            "  const validate = (v: string) => v.length > 0;\n"
            "  function submit() {}\n"
            "  return field;\n"
            "}\n"
        )
        self.assertEqual(_symbols("typescript", "Form.tsx", form), [("Form", 1, 10)])
        alerts = (
            "export function Alerts({ error }: Props) {\n"
            "  return <div>{error && <Alert message={error} />}</div>;\n"
            "}\n\n"
            "export function Footer() {\n"
            "  return <footer />;\n"
            "}\n"
        )
        self.assertEqual(
            self.names("typescript", "Alerts.tsx", alerts), ["Alerts", "Footer"]
        )

    def test_regex_arrow_body_keeps_later_declarations(self) -> None:
        blocks = (
            "const opensBlock = (line) => /\\{\\s*$/.test(line);\n\n"
            "export function parse(src) {\n"
            "  return src.split('\\n').filter(opensBlock);\n"
            "}\n\n"
            "export class Parser {}\n"
        )
        self.assertEqual(
            self.names("javascript", "blocks.js", blocks),
            ["opensBlock", "parse", "Parser"],
        )
        links = (
            "export function collect(urls) {\n"
            "  const external = urls.filter((u) => /^https?:\\/\\//.test(u))"
            ".map((u) => {\n"
            "    return u.toLowerCase();\n"
            "  });\n"
            "  const isLocal = (u) => !external.includes(u);\n"
            "  return urls.filter(isLocal);\n"
            "}\n"
        )
        self.assertEqual(_symbols("javascript", "links.js", links), [("collect", 1, 7)])

    def test_jsx_text_keeps_locals_local(self) -> None:
        signup = (
            "export function Signup({ busy }) {\n"
            "  const renderHint = () => {\n"
            "    return <p>Don't have an account? {t('signup.cta')}</p>;\n"
            "  };\n"
            "  const handleSubmit = async (event) => {\n"
            "    event.preventDefault();\n"
            "  };\n"
            "  return <form onSubmit={handleSubmit}>{renderHint()}</form>;\n"
            "}\n"
        )
        self.assertEqual(
            _symbols("javascript", "Signup.jsx", signup), [("Signup", 1, 9)]
        )
        toolbar = (
            "export function Toolbar({ icon, label }) {\n"
            "  const renderButton = () => {\n"
            "    return <button><Icon name={icon} /> {label && <span>{label}</span>}"
            "</button>;\n"
            "  };\n"
            "  const handleClick = () => {\n"
            "    console.log('clicked');\n"
            "  };\n"
            "  return <div onClick={handleClick}>{renderButton()}</div>;\n"
            "}\n"
        )
        self.assertEqual(self.names("javascript", "Toolbar.jsx", toolbar), ["Toolbar"])
        footer = (
            "export function Footer({ links }) {\n"
            "  const renderLinks = () => {\n"
            "    return <p>Docs: https://example.com/docs {links.length > 0 && (\n"
            "      <ul>{links.map((l) => <li key={l}>{l}</li>)}</ul>\n"
            "    )}</p>;\n"
            "  };\n"
            "  const handleClick = () => {};\n"
            "  return <footer onClick={handleClick}>{renderLinks()}</footer>;\n"
            "}\n"
        )
        self.assertEqual(self.names("javascript", "Footer.jsx", footer), ["Footer"])
        help_ = (
            "export function IgnoreHelp() {\n"
            "  return <p>Patterns such as <code>**/*.test.ts</code> are skipped.</p>;\n"
            "}\n\n"
            "export function Settings() {\n"
            "  return <form />;\n"
            "}\n\n"
            "/** Saves the settings. */\n"
            "export function save() {}\n"
        )
        self.assertEqual(
            self.names("javascript", "Help.jsx", help_),
            ["IgnoreHelp", "Settings", "save"],
        )

    def test_trailing_comment_after_division_stays_a_comment(self) -> None:
        half = (
            "const half = size! / 2; // e.g. {w: 10} -> 5, or { when odd\n"
            "export function helper() {}\n"
        )
        self.assertEqual(self.names("typescript", "half.ts", half), ["helper"])

    def test_crlf_continuation_hides_string_braces(self) -> None:
        source = (
            "const tpl = 'function fake() {\\\r\n';\r\n"
            "export function real() {}\r\n"
        )
        self.assertEqual(self.names("javascript", "t.js", source), ["real"])

    def test_rust_escaped_quote_char_keeps_strings_in_step(self) -> None:
        quotes = (
            "const QUOTES: [char; 2] = ['\\'','\"'];\n"
            "pub fn real() {}\n"
            "pub const HELP: &str = \"\n"
            "pub fn fake() {}\n"
            "\";\n"
        )
        self.assertEqual(
            self.names("rust", "quotes.rs", quotes), ["QUOTES", "real", "HELP"]
        )


if __name__ == "__main__":
    unittest.main()
