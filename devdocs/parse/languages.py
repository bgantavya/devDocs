"""Language specs for the generic scanner: JS/TS, Go, Rust, Java.

Each spec is data, not code. To support another brace-delimited language,
write a `LanguageSpec` here and add it to `SPECS`; the engine in `generic.py`
does the rest. Order within `declarations` matters: the first pattern to match
a line wins, so more specific shapes come before general ones.

Visibility follows each language's own rule: JS/TS exports, Go capitalisation,
Rust `pub`, Java `public` (plus interface members, which are implicitly public).
"""

from __future__ import annotations

import re

from devdocs.parse import SymbolKind, register
from devdocs.parse.generic import (
    Decl,
    GenericParser,
    GroupBlock,
    LanguageSpec,
    always_public,
    skip_angles,
)
from devdocs.parse.masking import Syntax

__all__ = ["GO", "JAVA", "JAVASCRIPT", "RUST", "SPECS"]

K = SymbolKind

# Control-flow words that shape-match a method declaration but never are one.
# `new` and `delete` are deliberately absent: `new` is the idiomatic Rust
# constructor and `delete` a common Java/JS method name.
_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    "await", "yield", "super", "else", "do", "try", "finally", "throw",
    "case", "synchronized",
})


def _match_group(match: re.Match[str], name: str) -> str:
    return match.groupdict().get(name) or ""


# -- JavaScript / TypeScript -------------------------------------------------

_JS_ID = r"[A-Za-z_$][\w$]*"
# Type parameters, nesting once: `<T extends Base<U> = Default>`.
_JS_TYPEPARAMS = r"(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>)"
# Names that read as declarations in some shapes but never are one: an
# anonymous `export default class extends X`, an interface's `new (...)`.
_JS_NOT_NAMES = frozenset({"extends", "implements", "new"})
_EXPORT = r"(?P<export>export\s+(?:default\s+)?)?"
_DECLARE = r"(?:declare\s+)?"
_JS_MODS = (
    r"(?P<mods>(?:(?:public|private|protected|static|async|readonly|override|"
    r"abstract|declare|get|set|accessor)\s+)*)"
)


def _js_exported(match: re.Match[str], parent: re.Match[str] | None) -> bool:
    return bool(_match_group(match, "export"))


def _js_member_public(match: re.Match[str], parent: re.Match[str] | None) -> bool:
    mods = _match_group(match, "mods").split()
    name = match.group("name")
    hidden = "private" in mods or "protected" in mods
    return not name.startswith("#") and not hidden


_JS_MEMBERS = (
    Decl(
        re.compile(
            rf"^\s*{_JS_MODS}\*?\s*(?P<name>#?{_JS_ID})\??\s*(?:<[^>]*>)?\s*\("
        ),
        K.METHOD,
        _js_member_public,
    ),
    # Class-property arrow functions: `handle = async (e) => { ... }`.
    Decl(
        re.compile(
            rf"^\s*{_JS_MODS}(?P<name>#?{_JS_ID})\s*(?::[^=]+)?=\s*(?:async\s+)?"
            rf"(?:\([^)]*\)|{_JS_ID})\s*(?::[^=]+?)?\s*=>"
        ),
        K.METHOD,
        _js_member_public,
    ),
)

_TS_INTERFACE_MEMBERS = (
    Decl(
        re.compile(rf"^\s*(?:readonly\s+)?(?P<name>{_JS_ID})\??\s*(?:<[^>]*>)?\s*\("),
        K.METHOD,
        always_public,
    ),
)

JAVASCRIPT = LanguageSpec(
    languages=("javascript", "typescript"),
    syntax=Syntax(template_literals=True, regex_literals=True),
    declarations=(
        Decl(
            re.compile(
                rf"^\s*{_EXPORT}{_DECLARE}(?:async\s+)?function\b\s*\*?\s*"
                rf"(?P<name>{_JS_ID})"
            ),
            K.FUNCTION,
            _js_exported,
        ),
        Decl(
            re.compile(
                rf"^\s*{_EXPORT}{_DECLARE}(?:abstract\s+)?class\s+(?P<name>{_JS_ID})"
            ),
            K.CLASS,
            _js_exported,
            members=_JS_MEMBERS,
        ),
        Decl(
            re.compile(rf"^\s*{_EXPORT}{_DECLARE}interface\s+(?P<name>{_JS_ID})"),
            K.INTERFACE,
            _js_exported,
            members=_TS_INTERFACE_MEMBERS,
        ),
        Decl(
            re.compile(
                rf"^\s*{_EXPORT}{_DECLARE}type\s+(?P<name>{_JS_ID})\s*"
                rf"{_JS_TYPEPARAMS}?\s*="
            ),
            K.TYPE,
            _js_exported,
        ),
        Decl(
            re.compile(
                rf"^\s*{_EXPORT}{_DECLARE}(?:const\s+)?enum\s+(?P<name>{_JS_ID})"
            ),
            K.TYPE,
            _js_exported,
        ),
        # `const f = (...) =>`, `const f = async x =>`, `const f = function`.
        Decl(
            re.compile(
                rf"^\s*{_EXPORT}{_DECLARE}(?:const|let|var)\s+(?P<name>{_JS_ID})\s*"
                rf"(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|(?:<[^>]*>\s*)?"
                rf"\([^)]*\)\s*(?::[^=]+?)?\s*=>|{_JS_ID}\s*=>)"
            ),
            K.FUNCTION,
            _js_exported,
        ),
        # Arrow functions whose parameters wrap onto following lines.
        Decl(
            re.compile(
                rf"^\s*{_EXPORT}{_DECLARE}(?:const|let|var)\s+(?P<name>{_JS_ID})\s*"
                rf"(?::[^=]+)?=\s*(?:async\s+)?\(\s*$"
            ),
            K.FUNCTION,
            _js_exported,
        ),
        Decl(
            re.compile(
                rf"^\s*(?P<export>export\s+){_DECLARE}(?:const|let|var)\s+(?P<name>{_JS_ID})"
            ),
            K.CONSTANT,
            _js_exported,
        ),
        Decl(
            re.compile(
                r"^\s*(?P<export>)(?:const|let|var)\s+(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?="
            ),
            K.CONSTANT,
            _js_exported,
        ),
    ),
    imports=(
        re.compile(
            r"^\s*import\s+(?:type\s+)?(?:[^;'\"]*?\s+from\s+)?['\"](?P<module>[^'\"\n]+)['\"]",
            re.M,
        ),
        re.compile(
            r"^\s*export\s+(?:type\s+)?(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+"
            r"['\"](?P<module>[^'\"\n]+)['\"]",
            re.M,
        ),
        re.compile(r"\brequire\(\s*['\"](?P<module>[^'\"\n]+)['\"]\s*\)"),
        re.compile(r"\bimport\(\s*['\"](?P<module>[^'\"\n]+)['\"]\s*\)"),
    ),
    exports=(
        re.compile(r"^\s*export\s*\{(?P<names>[^}]*)\}(?!\s*from)", re.M),
        re.compile(r"^\s*export\s+default\s+(?P<name>[A-Za-z_$][\w$]*)\s*;?\s*$", re.M),
        re.compile(r"\bmodule\.exports\s*=\s*\{(?P<names>[^}]*)\}"),
        re.compile(
            r"\bmodule\.exports\s*=\s*(?P<name>[A-Za-z_$][\w$]*)\s*;?\s*$", re.M
        ),
        re.compile(r"\b(?:module\.)?exports\.(?P<name>[A-Za-z_$][\w$]*)\s*="),
    ),
    doc_style="block",
    module_doc="leading-block",
    exclude_names=_KEYWORDS | _JS_NOT_NAMES,
    script_fallback_public=True,
)


# -- Go ------------------------------------------------------------------------

_GO_ID = r"[A-Za-z_]\w*"
# Type parameters, nesting once: `[S ~[]E, E any]`, `[M ~map[K]V]`.
_GO_TYPEPARAMS = r"(?:\[(?:[^\[\]]|\[[^\[\]]*\])*\])?"


def _go_public(match: re.Match[str], parent: re.Match[str] | None) -> bool:
    return match.group("name")[:1].isupper()


_GO_INTERFACE_MEMBERS = (
    Decl(re.compile(rf"^\s+(?P<name>{_GO_ID})\s*\("), K.METHOD, _go_public),
)

GO = LanguageSpec(
    languages=("go",),
    syntax=Syntax(raw_backticks=True),
    declarations=(
        # Methods: `func (r *Reader) Read(`; attached to Reader after the scan.
        Decl(
            re.compile(
                rf"^func\s+\(\s*(?:{_GO_ID}\s+)?\*?(?P<recv>{_GO_ID}){_GO_TYPEPARAMS}\s*\)"
                rf"\s*(?P<name>{_GO_ID})\s*{_GO_TYPEPARAMS}\s*\("
            ),
            K.FUNCTION,
            _go_public,
            attach="recv",
        ),
        Decl(
            re.compile(rf"^func\s+(?P<name>{_GO_ID})\s*{_GO_TYPEPARAMS}\s*\("),
            K.FUNCTION,
            _go_public,
        ),
        Decl(
            re.compile(rf"^type\s+(?P<name>{_GO_ID}){_GO_TYPEPARAMS}\s+struct\b"),
            K.CLASS,
            _go_public,
        ),
        Decl(
            re.compile(rf"^type\s+(?P<name>{_GO_ID}){_GO_TYPEPARAMS}\s+interface\b"),
            K.INTERFACE,
            _go_public,
            members=_GO_INTERFACE_MEMBERS,
        ),
        Decl(re.compile(rf"^type\s+(?P<name>{_GO_ID})\b"), K.TYPE, _go_public),
        Decl(
            re.compile(rf"^(?:const|var)\s+(?P<name>{_GO_ID})\b"),
            K.CONSTANT,
            _go_public,
        ),
    ),
    groups=(
        GroupBlock(
            re.compile(r"^(?:const|var)\s*\(\s*$"),
            re.compile(rf"^\s+(?P<name>{_GO_ID})\b"),
            K.CONSTANT,
            _go_public,
        ),
        GroupBlock(
            re.compile(r"^type\s*\(\s*$"),
            re.compile(
                rf"^\s+(?P<name>{_GO_ID}){_GO_TYPEPARAMS}"
                r"(?:\s+(?P<shape>struct|interface)\b)?"
            ),
            K.TYPE,
            _go_public,
            shapes=(
                ("struct", K.CLASS, ()),
                ("interface", K.INTERFACE, _GO_INTERFACE_MEMBERS),
            ),
        ),
    ),
    imports=(re.compile(r'^import\s+(?:[\w.]+\s+)?"(?P<module>[^"]+)"', re.M),),
    import_block=re.compile(r"^import\s*\((?P<block>.*?)^\)", re.M | re.S),
    import_item=re.compile(r'"(?P<module>[^"]+)"'),
    doc_style="line",
    module_doc="go-package",
    exclude_names=_KEYWORDS,
)


# -- Rust ------------------------------------------------------------------------

_RS_VIS = r"(?P<vis>pub(?:\s*\([^)]*\))?\s+)?"
_RS_FN = (
    r"(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(?:extern\s+\"[^\"]*\"\s+)?fn\s+(?P<name>[A-Za-z_]\w*)"
)


def _rust_impl_parts(header: str) -> tuple[str, str]:
    """(trait, self type) of an `impl` header, as written.

    Type parameters, nested generics and references are handled in code:
    `impl<T: A<B>> Trait<X> for Vec<Y>`, `impl<'a> Foo for &'a T`.
    """
    rest = re.sub(r"^\s*(?:unsafe\s+)?impl\b", "", header)
    rest = skip_angles(rest)
    rest = rest.split("{", 1)[0].split(" where ", 1)[0]
    depth = 0
    for index in range(len(rest)):
        char = rest[index]
        if char in "<(":
            depth += 1
        elif char in ">)":
            depth -= 1
        elif depth == 0 and re.match(r"\sfor\s", rest[index:]):
            return rest[:index].strip(), rest[index + 4 :].strip()
    return "", rest.strip()


def _rust_impl_target(header: str) -> str:
    """The type an impl block's members belong to: `Vec` for
    `impl<T> Trait for Vec<T>`, `T` for `impl Foo for &'a mut T`."""
    _, self_type = _rust_impl_parts(header)
    self_type = re.sub(r"^(?:&\s*(?:'\w+\s+)?(?:mut\s+)?|dyn\s+|mut\s+)+", "",
                       self_type)
    base = re.match(r"[\w:]+", self_type)
    return base.group(0).split("::")[-1] if base else ""


def _rust_public(match: re.Match[str], parent: re.Match[str] | None) -> bool:
    """`pub` makes an item public; trait items and trait-impl items are
    reachable through the trait, so they count as public too."""
    if _match_group(match, "vis"):
        return True
    if parent is None:
        return False
    header = parent.string[parent.start() :]
    if re.match(r"\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:unsafe\s+)?trait\b", header):
        return True
    trait, _ = _rust_impl_parts(header)
    return bool(trait)


def _rust_import(text: str) -> str:
    return text.strip().rstrip(":").strip()


_RS_MEMBERS = (Decl(re.compile(rf"^\s*{_RS_VIS}{_RS_FN}"), K.METHOD, _rust_public),)

RUST = LanguageSpec(
    languages=("rust",),
    syntax=Syntax(
        nested_block_comments=True,
        quotes='"',
        multiline_strings=True,
        rust_literals=True,
    ),
    declarations=(
        Decl(re.compile(rf"^\s*{_RS_VIS}{_RS_FN}"), K.FUNCTION, _rust_public),
        Decl(
            re.compile(rf"^\s*{_RS_VIS}struct\s+(?P<name>[A-Za-z_]\w*)"),
            K.CLASS,
            _rust_public,
        ),
        Decl(
            re.compile(rf"^\s*{_RS_VIS}enum\s+(?P<name>[A-Za-z_]\w*)"),
            K.TYPE,
            _rust_public,
        ),
        Decl(
            re.compile(rf"^\s*{_RS_VIS}(?:unsafe\s+)?trait\s+(?P<name>[A-Za-z_]\w*)"),
            K.INTERFACE,
            _rust_public,
            members=_RS_MEMBERS,
        ),
        Decl(
            re.compile(rf"^\s*{_RS_VIS}type\s+(?P<name>[A-Za-z_]\w*)"),
            K.TYPE,
            _rust_public,
        ),
        Decl(
            re.compile(
                rf"^\s*{_RS_VIS}(?:const|static)\s+(?:mut\s+)?(?P<name>[A-Z_][A-Z0-9_]*)\b"
            ),
            K.CONSTANT,
            _rust_public,
        ),
        Decl(
            re.compile(rf"^\s*{_RS_VIS}mod\s+(?P<name>[A-Za-z_]\w*)"),
            K.MODULE,
            _rust_public,
        ),
        Decl(
            re.compile(r"^\s*(?:#\[macro_export\]\s*)?macro_rules!\s*(?P<name>\w+)"),
            K.FUNCTION,
            always_public,
        ),
        # `impl Foo` / `impl<T> Trait for Foo<T>`: members attach to Foo.
        Decl(
            re.compile(r"^\s*(?:unsafe\s+)?impl\b"),
            None,
            members=_RS_MEMBERS,
            attach_fn=_rust_impl_target,
        ),
    ),
    imports=(
        # `use a::b;`, `use a::b as c;`, `use a::{b, c};` and `use a::*;`
        # all record `a`/`a::b`: the path, not the names pulled from it.
        re.compile(
            r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?use\s+(?P<module>[^;{]+?)"
            r"(?:::\{.*|::\*|\s+as\s+\w+)?\s*;?\s*$",
            re.M,
        ),
        re.compile(r"^\s*extern\s+crate\s+(?P<module>\w+)", re.M),
    ),
    doc_style="triple-slash",
    module_doc="inner",
    exclude_names=_KEYWORDS,
    normalise_import=_rust_import,
)


# -- Java ------------------------------------------------------------------------

_JAVA_ANNOT = r"(?:@[\w.]+(?:\([^)]*\))?\s+)*"
_JAVA_MODS = (
    r"(?P<mods>(?:(?:public|protected|private|abstract|final|static|sealed|"
    r"non-sealed|strictfp|synchronized|native|default|transient|volatile)\s+)*)"
)


def _java_public(match: re.Match[str], parent: re.Match[str] | None) -> bool:
    """`public`, or a member of an interface, which Java makes public unless
    it is declared `private` (allowed since Java 9)."""
    mods = _match_group(match, "mods").split()
    if "public" in mods:
        return True
    if parent is None or "private" in mods:
        return False
    return re.search(r"\binterface\b", parent.group(0)) is not None


def _java_import(text: str) -> str:
    return text.strip().removesuffix(".*")


# Type parameters, nesting twice: `<T extends Comparable<? super T>>`.
_JAVA_TYPEPARAMS = r"(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>\s+)"

_JAVA_FIELDS_AND_METHODS = (
    # Constructors first: `public Foo(` would otherwise read as a method whose
    # return type is `public`, losing its visibility.
    Decl(
        re.compile(rf"^\s*{_JAVA_ANNOT}{_JAVA_MODS}(?P<name>[A-Z][\w$]*)\s*\("),
        K.METHOD,
        _java_public,
    ),
    Decl(
        re.compile(
            rf"^\s*{_JAVA_ANNOT}{_JAVA_MODS}{_JAVA_TYPEPARAMS}?"
            rf"[\w.$]+(?:<[^()]*?>)?(?:\[\])*\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("
        ),
        K.METHOD,
        _java_public,
    ),
    Decl(
        re.compile(
            r"^\s*(?P<mods>(?:(?:public|protected|private)\s+)?)"
            r"(?:static\s+final|final\s+static)\s+[\w.<>\[\], ?]+?\s+"
            r"(?P<name>[A-Z][A-Z0-9_]*)\s*="
        ),
        K.CONSTANT,
        _java_public,
    ),
)

_JAVA_NESTED = re.compile(
    rf"^\s*{_JAVA_ANNOT}{_JAVA_MODS}(?:class|interface|enum|record)\s+(?P<name>\w+)"
)

# A nested type keeps its own members, two levels deep (a Builder inside a
# class inside a class); deeper nesting is rare enough to leave flat.
_JAVA_MEMBERS = (
    *_JAVA_FIELDS_AND_METHODS,
    Decl(
        _JAVA_NESTED,
        K.CLASS,
        _java_public,
        members=(
            *_JAVA_FIELDS_AND_METHODS,
            Decl(_JAVA_NESTED, K.CLASS, _java_public,
                 members=_JAVA_FIELDS_AND_METHODS),
        ),
    ),
)

# Enum constants, `MERCURY(3.303e+23, 2.4397e6),`, come before the members.
_JAVA_ENUM_MEMBERS = (
    Decl(
        re.compile(rf"^\s*{_JAVA_ANNOT}(?P<name>[A-Z][A-Z0-9_]*)\s*(?=[(,;{{]|$)"),
        K.CONSTANT,
        always_public,
        listed=True,
    ),
    *_JAVA_MEMBERS,
)

JAVA = LanguageSpec(
    languages=("java",),
    syntax=Syntax(text_blocks=True),
    declarations=(
        Decl(
            re.compile(rf"^\s*{_JAVA_ANNOT}{_JAVA_MODS}(?:class|record)\s+(?P<name>\w+)"),
            K.CLASS,
            _java_public,
            members=_JAVA_MEMBERS,
        ),
        Decl(
            re.compile(rf"^\s*{_JAVA_ANNOT}{_JAVA_MODS}@?interface\s+(?P<name>\w+)"),
            K.INTERFACE,
            _java_public,
            members=_JAVA_MEMBERS,
        ),
        Decl(
            re.compile(rf"^\s*{_JAVA_ANNOT}{_JAVA_MODS}enum\s+(?P<name>\w+)"),
            K.TYPE,
            _java_public,
            members=_JAVA_ENUM_MEMBERS,
        ),
    ),
    imports=(
        re.compile(r"^\s*import\s+(?:static\s+)?(?P<module>[\w.*]+)\s*;", re.M),
    ),
    doc_style="block",
    module_doc="",
    exclude_names=_KEYWORDS,
    normalise_import=_java_import,
)


SPECS: tuple[LanguageSpec, ...] = (JAVASCRIPT, GO, RUST, JAVA)

for _spec in SPECS:
    register(GenericParser(_spec), replace=False)
