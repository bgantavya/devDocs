"""A declarative scanner for brace-delimited languages.

Rather than one hand-written parser per language, each language is described
by a `LanguageSpec`: its lexical syntax, the declaration patterns it uses at top
level, the member patterns inside class-like bodies, and how it spells imports.
`GenericParser` turns any spec into a `Parser`. Adding a language means writing
a spec in `languages.py`, not a parser.

The scan runs over masked text (see `masking.py`), so nothing is ever matched
inside a comment or string, while offsets still index the real source. Brace
depth is tracked per line to tell top-level declarations from members and
from code inside function bodies, and a declaration must start a statement:
a line continuing the expression above it (a wrapped initialiser, a call's
arguments) is never read as one. Containers nest, so a class inside a class
keeps its members.

Headers are read with the brackets of types in mind: `<` opens a generic
only where a generic can stand (after a name, or a modifier in Java's
`public <T>`), never in `1 << 20` or Go's `<-chan`; a `{` after `:`, `|` or
`(` is an object type, not the body; `=>` ends a header only after an
initialiser `=`, being otherwise part of a function type. Comments inside a
multi-line header are left out of the signature.

Output confidence is HEURISTIC: every name reported genuinely appears as a
declaration, but unusual forms can be missed. That is the right trade for a
zero-dependency tool; tree-sitter would be exact but is a native dependency.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind, count_lines
from devdocs.parse.masking import Masked, Syntax, mask
from devdocs.scan import LANGUAGES

__all__ = ["Decl", "GenericParser", "LanguageSpec"]

Visibility = Callable[[re.Match[str], re.Match[str] | None], bool]

_MAX_SIGNATURE = 400
_MAX_DOC = 2000
_MAX_HEADER_WAIT = 6   # lines a container may wait for its opening brace
_HAS_BASES = frozenset({SymbolKind.CLASS, SymbolKind.INTERFACE})

# A header line followed by one of these continues on the next line rather
# than ending there, e.g. `class A` newline `extends B {`.
_CONTINUATIONS = ("{", "extends", "implements", "where", "throws", ":", "=",
                  "->", "|", "&", ".", ")", "]", ">", ",")


def always_public(match: re.Match[str], parent: re.Match[str] | None) -> bool:
    return True


def _strip(text: str) -> str:
    return text.strip()


@dataclass(frozen=True)
class Decl:
    """One declaration shape, matched against a masked line.

    `name` is the regex group holding the declared name. A Decl with
    `members` is a container: lines one brace deeper inside its body are tried
    against those member patterns. `kind=None` marks an anonymous container,
    such as a Rust `impl` block, whose members attach to the type named by the
    `attach` group instead of to a symbol of their own.
    """

    regex: re.Pattern[str]
    kind: SymbolKind | None
    public: Visibility = always_public
    members: tuple[Decl, ...] = ()
    name: str = "name"
    attach: str | None = None
    # Computes the attach target from the header line when a regex group
    # cannot (Rust impl headers nest generics and references).
    attach_fn: Callable[[str], str] | None = None
    # Matches items of a comma-separated list, such as Java enum constants,
    # so a line after a trailing comma still starts one.
    listed: bool = False


@dataclass(frozen=True)
class GroupBlock:
    """A parenthesised declaration group, such as Go's `const ( ... )`."""

    opener: re.Pattern[str]
    item: re.Pattern[str]
    kind: SymbolKind
    public: Visibility = always_public
    # An item whose `shape` group names a body (Go's `T struct {`) takes the
    # kind and member patterns listed here.
    shapes: tuple[tuple[str, SymbolKind, tuple[Decl, ...]], ...] = ()


@dataclass(frozen=True)
class LanguageSpec:
    """Everything the engine needs to know about one language."""

    languages: tuple[str, ...]
    syntax: Syntax
    declarations: tuple[Decl, ...]
    imports: tuple[re.Pattern[str], ...] = ()
    import_block: re.Pattern[str] | None = None
    import_item: re.Pattern[str] | None = None
    exports: tuple[re.Pattern[str], ...] = ()
    groups: tuple[GroupBlock, ...] = ()
    doc_style: str = "block"          # block | line | triple-slash
    module_doc: str = "leading-block" # leading-block | go-package | inner | ""
    exclude_names: frozenset[str] = frozenset()
    normalise_import: Callable[[str], str] = _strip
    # A JS file with no export of any kind is a script, not a module: every
    # top-level definition is its surface, so none should read as private.
    script_fallback_public: bool = False


@dataclass
class _Pending:
    """A symbol under construction; frozen into `Symbol` at the end."""

    name: str
    kind: SymbolKind
    line: int
    end_line: int
    signature: str
    docstring: str
    public: bool
    is_async: bool
    bases: tuple[str, ...] = ()
    children: list[_Pending] = field(default_factory=list)

    def freeze(self) -> Symbol:
        return Symbol(
            name=self.name,
            kind=self.kind,
            line=self.line,
            end_line=self.end_line,
            signature=self.signature,
            docstring=self.docstring,
            bases=self.bases,
            children=tuple(child.freeze() for child in self.children),
            is_public=self.public,
            is_async=self.is_async,
        )


@dataclass
class _Container:
    decl: Decl
    match: re.Match[str]
    depth: int
    owner: _Pending | None
    attach_name: str | None
    opened: bool = False
    waited: int = 0
    members: list[_Pending] = field(default_factory=list)


class GenericParser:
    """A `Parser` driven entirely by a `LanguageSpec`."""

    def __init__(self, spec: LanguageSpec) -> None:
        self.spec = spec
        self.languages: tuple[str, ...] = spec.languages

    def parse(self, path: Path, source: str) -> FileFacts:
        # One spec serves several languages (JS and TS), so label by suffix.
        language = LANGUAGES.get(path.suffix.lower(), "")
        if language not in self.spec.languages:
            language = self.spec.languages[0]
        loc = count_lines(source)
        try:
            masked = mask(source, self.spec.syntax)
            return self._scan(path, source, masked, language, loc)
        except (RecursionError, re.error) as err:   # never let one file end a run
            return FileFacts(
                path=path,
                language=language,
                loc=loc,
                confidence=Confidence.FAILED,
                notes=[f"scanner failed: {type(err).__name__}: {err}"],
            )

    # -- scan ---------------------------------------------------------------

    def _scan(
        self, path: Path, source: str, masked: Masked, language: str, loc: int
    ) -> FileFacts:
        text = masked.text
        src_lines = source.split("\n")
        mask_lines = text.split("\n")
        depths = _line_depths(mask_lines)
        offsets = _line_offsets(mask_lines)
        comments = _Comments(masked.comments)

        top: list[_Pending] = []
        detached: list[tuple[str, _Pending]] = []
        stack: list[_Container] = []
        group: GroupBlock | None = None
        group_parens = 0
        group_line = -1

        def pending(decl: Decl, match: re.Match[str], index: int,
                    parent: re.Match[str] | None, kind: SymbolKind | None = None,
                    ) -> _Pending | None:
            return self._pending(
                decl, match, index, offsets, source, text, src_lines, comments,
                parent=parent, is_member=parent is not None, kind=kind,
            )

        for index, line in enumerate(mask_lines):
            depth = depths[index]

            while stack and self._closed(stack[-1], depth, line):
                self._pop(stack.pop(), detached)

            if stack and depth == stack[-1].depth + 1:
                container = stack[-1]
                member = self._match(container.decl.members, line, container.match)
                if member is not None and (
                    member[0].listed or _starts_statement(mask_lines, index)
                ):
                    decl, match = member
                    found = pending(decl, match, index, container.match)
                    if found is not None:
                        container.members.append(found)
                        if decl.members:
                            stack.append(self._open(decl, match, depth, found, line))
                continue

            if group is not None:
                inside = group_parens
                group_parens += line.count("(") - line.count(")")
                if group_parens <= 0:
                    group = None
                    continue
                item = group.item.match(line)
                if (
                    item is not None and inside == 1 and depth == 0
                    and (_previous_code(mask_lines, index) == group_line
                         or _starts_statement(mask_lines, index))
                ):
                    self._group_item(group, item, index, pending, top, stack, line)
                continue

            if depth != 0:
                continue

            opener = next((g for g in self.spec.groups if g.opener.match(line)), None)
            if opener is not None:
                group = opener
                group_parens = line.count("(") - line.count(")")
                group_line = index
                continue

            found_decl = self._match(self.spec.declarations, line, None)
            if found_decl is None or not _starts_statement(mask_lines, index):
                continue
            decl, match = found_decl

            # A new top-level declaration ends any header still waiting for
            # its body, so a stale container cannot capture this one's members.
            while stack and not stack[-1].opened:
                self._pop(stack.pop(), detached)

            target = None
            if decl.attach_fn is not None:
                target = decl.attach_fn(line[match.start() :]) or None
            elif decl.attach:
                target = match.group(decl.attach) or None

            owner: _Pending | None = None
            if decl.kind is not None:
                owner = pending(decl, match, index, None)
                if owner is None:
                    continue
                if target:
                    detached.append((target, owner))
                else:
                    top.append(owner)

            if decl.members:
                container = self._open(decl, match, depth, owner, line)
                container.attach_name = target if owner is None else None
                stack.append(container)

        while stack:
            self._pop(stack.pop(), detached)

        self._attach(top, detached)
        symbols = _dedupe(top)
        imports = self._imports(source, text)
        exports = self._exports(source, text)

        if exports:
            named = set(exports)
            for symbol in symbols:
                symbol.public = symbol.public or symbol.name in named
            # Merge inline `export function f` with any `export { g }` list, or
            # public_symbols would filter the inline ones out.
            exports = tuple(dict.fromkeys(
                [*exports, *(s.name for s in symbols if s.public)]
            ))
        elif (
            self.spec.script_fallback_public
            and not any(s.public for s in symbols)
            and not _MODULE_SYNTAX.search(text)
        ):
            for symbol in symbols:
                symbol.public = True

        confidence = Confidence.HEURISTIC
        notes: list[str] = []
        if masked.unterminated:
            confidence = Confidence.DEGRADED
            notes.append(
                f"{masked.unterminated} unterminated comment/string; "
                "symbols after it may be missing"
            )

        return FileFacts(
            path=path,
            language=language,
            symbols=tuple(symbol.freeze() for symbol in symbols),
            imports=imports,
            exports=exports,
            module_docstring=self._module_doc(src_lines),
            loc=loc,
            confidence=confidence,
            notes=notes,
        )

    @staticmethod
    def _open(
        decl: Decl, match: re.Match[str], depth: int, owner: _Pending | None, line: str
    ) -> _Container:
        container = _Container(decl, match, depth, owner, None)
        if "{" in line[match.start() :]:
            container.opened = True
        return container

    def _group_item(
        self,
        group: GroupBlock,
        item: re.Match[str],
        index: int,
        pending: Callable[..., _Pending | None],
        top: list[_Pending],
        stack: list[_Container],
        line: str,
    ) -> None:
        """One item of a declaration group, with a body if its shape has one."""
        shape = item.groupdict().get("shape") or ""
        kind: SymbolKind = group.kind
        members: tuple[Decl, ...] = ()
        for name, shaped_kind, shaped_members in group.shapes:
            if name == shape:
                kind, members = shaped_kind, shaped_members
        decl = Decl(group.item, kind, group.public, members)
        found = pending(decl, item, index, None, kind)
        if found is None:
            return
        found.public = group.public(item, None)
        top.append(found)
        if members:
            stack.append(self._open(decl, item, 0, found, line))

    @staticmethod
    def _closed(container: _Container, depth: int, line: str) -> bool:
        """True once the container's body has opened and we are back outside it.

        A header may run over several lines before its `{` — a Rust `where`
        clause, a wrapped `extends` list — so an unopened container waits a
        few lines. A `;` first means it had no body at all.
        """
        if not container.opened:
            if "{" in line:
                container.opened = True
                return False
            container.waited += 1
            return ";" in line or container.waited > _MAX_HEADER_WAIT
        return depth <= container.depth

    @staticmethod
    def _pop(container: _Container, detached: list[tuple[str, _Pending]]) -> None:
        if container.owner is not None:
            container.owner.children.extend(container.members)
        elif container.attach_name:
            for member in container.members:
                detached.append((container.attach_name, member))

    @staticmethod
    def _attach(top: list[_Pending], detached: list[tuple[str, _Pending]]) -> None:
        """Hang Go receiver methods and Rust impl members off their type.

        When the type lives in another file there is nothing to hang them on,
        so they stay top-level rather than being dropped.
        """
        by_name = {p.name: p for p in top if p.kind in (
            SymbolKind.CLASS, SymbolKind.INTERFACE, SymbolKind.TYPE)}
        for type_name, member in detached:
            base = type_name.split("::")[-1].split("<")[0].lstrip("*&")
            owner = by_name.get(base)
            member.kind = SymbolKind.METHOD
            if owner is not None:
                owner.children.append(member)
            else:
                top.append(member)

    def _match(
        self, decls: tuple[Decl, ...], line: str, parent: re.Match[str] | None
    ) -> tuple[Decl, re.Match[str]] | None:
        for decl in decls:
            match = decl.regex.match(line)
            if match is None:
                continue
            name = match.groupdict().get(decl.name)
            if decl.kind is not None and (not name or name in self.spec.exclude_names):
                continue
            return decl, match
        return None

    def _pending(
        self,
        decl: Decl,
        match: re.Match[str],
        index: int,
        offsets: list[int],
        source: str,
        masked: str,
        src_lines: list[str],
        comments: _Comments,
        *,
        parent: re.Match[str] | None,
        is_member: bool,
        kind: SymbolKind | None = None,
    ) -> _Pending | None:
        kind = kind or decl.kind
        assert kind is not None
        name = match.group(decl.name)
        start = offsets[index] + match.start()
        signature, end_offset = _signature(source, masked, start, kind, comments)
        end_line = _end_line(masked, start, end_offset)
        if is_member and kind is SymbolKind.FUNCTION:
            kind = SymbolKind.METHOD
        if kind is SymbolKind.CONSTANT:
            signature = signature.rstrip(",; ")
        # `async` may precede the name (`async charge(`) or follow it
        # (`handle = async (e) =>`), so check every word except the name.
        words = re.findall(r"[A-Za-z_$][\w$]*", match.group(0))
        if name in words:
            words.remove(name)

        return _Pending(
            name=name,
            kind=kind,
            line=index + 1,
            end_line=end_line,
            signature=signature,
            docstring=_docstring(src_lines, index, self.spec.doc_style),
            public=decl.public(match, parent),
            is_async="async" in words,
            bases=_bases(signature) if kind in _HAS_BASES else (),
        )

    # -- imports / exports / module doc -------------------------------------

    def _imports(self, source: str, masked: str) -> tuple[str, ...]:
        """Import targets, kept only where the keyword is real code.

        Patterns run over the source (the mask hides the quoted module paths),
        and each hit is checked against the mask: if the first character was
        blanked, the match came from a comment or string and is discarded.
        """
        found: set[str] = set()
        for pattern in self.spec.imports:
            for match in pattern.finditer(source):
                if _is_code(masked, source, match.start()):
                    found.add(self.spec.normalise_import(match.group("module")))

        block, item = self.spec.import_block, self.spec.import_item
        if block is not None and item is not None:
            for match in block.finditer(source):
                if not _is_code(masked, source, match.start()):
                    continue
                for entry in item.finditer(match.group("block")):
                    found.add(self.spec.normalise_import(entry.group("module")))

        return tuple(sorted(value for value in found if value))

    def _exports(self, source: str, masked: str) -> tuple[str, ...]:
        names: list[str] = []
        for pattern in self.spec.exports:
            for match in pattern.finditer(source):
                if not _is_code(masked, source, match.start()):
                    continue
                groups = match.groupdict()
                if groups.get("names"):
                    for part in groups["names"].split(","):
                        local = part.strip().split(" as ")[0].split(":")[-1].strip()
                        local = re.sub(r"^type\s+", "", local)
                        if re.fullmatch(r"[A-Za-z_$][\w$]*", local):
                            names.append(local)
                elif groups.get("name"):
                    names.append(groups["name"])
        return tuple(dict.fromkeys(names))

    def _module_doc(self, lines: list[str]) -> str:
        style = self.spec.module_doc
        if style == "inner":
            collected = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("//!"):
                    collected.append(stripped[3:].strip())
                elif stripped and not stripped.startswith("#!"):
                    break
            return "\n".join(collected).strip()[:_MAX_DOC]

        if style == "go-package":
            for index, line in enumerate(lines):
                if line.startswith("package "):
                    return _docstring(lines, index, "line")
            return ""

        if style == "leading-block":
            first = next((i for i, line in enumerate(lines) if line.strip()), None)
            if first is None or not lines[first].lstrip().startswith("/*"):
                return ""
            end = next(
                (i for i in range(first, len(lines)) if "*/" in lines[i]), None
            )
            if end is None:
                return ""
            block = lines[first : end + 1]
            raw = "\n".join(block).lower()
            if "copyright" in raw or "license" in raw:
                return ""   # a licence header describes the terms, not the module
            following = lines[end + 1] if end + 1 < len(lines) else ""
            # Tags are checked on the raw block: cleaning strips them.
            tagged = any(tag in raw for tag in ("@file", "@module", "@fileoverview"))
            return _clean_block(block) if tagged or not following.strip() else ""

        return ""


# -- helpers ----------------------------------------------------------------


def _line_depths(lines: list[str]) -> list[int]:
    """Brace depth at the start of each masked line, never below zero.

    Clamping at zero makes a stray `}` self-healing instead of shifting every
    later line to the wrong depth.
    """
    depths: list[int] = []
    depth = 0
    for line in lines:
        depths.append(depth)
        for char in line:
            if char == "{":
                depth += 1
            elif char == "}" and depth > 0:
                depth -= 1
    return depths


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    total = 0
    for line in lines:
        offsets.append(total)
        total += len(line) + 1
    return offsets


def _is_code(masked: str, source: str, position: int) -> bool:
    """True when the first non-space char at `position` survived masking."""
    while position < len(source) and source[position] in " \t\r\n":
        position += 1
    return position < len(source) and masked[position] == source[position]


class _Comments:
    """Comment spans from the masker, for cutting comments out of headers."""

    def __init__(self, spans: Sequence[tuple[int, int]]) -> None:
        self.spans = sorted(spans)
        self.ends = [end for _, end in self.spans]

    def strip(self, source: str, start: int, end: int) -> str:
        """`source[start:end]` with every comment in it replaced by a space."""
        first = bisect.bisect_right(self.ends, start)
        parts: list[str] = []
        cursor = start
        for span_start, span_end in self.spans[first:]:
            if span_start >= end:
                break
            if span_start > cursor:
                parts.append(source[cursor:span_start])
            parts.append(" ")
            cursor = max(cursor, span_end)
        if cursor < end:
            parts.append(source[cursor:end])
        return "".join(parts)


_MODULE_SYNTAX = re.compile(r"^\s*(?:import|export)\b", re.M)
# A parameter list, with an optional return type, right before a `{`.
_FUNCTION_BODY = re.compile(r"\)\s*(?::\s*[\w$.<>\[\]| ,]+)?\s*$")

# A line ending in one of these carries its expression onto the next line.
_CONTINUED = ("=", "(", "[", ",", ".", "+", "-", "*", "/", "%", "&", "|", "^",
              "!", "~", "?", ":", "<")
# A line starting with one of these continues the one above.
_CONTINUING = (".", "?", ":", "+", "&&", "||", "|", "&", "=>", "->", ",", ")",
               "]")
# Modifiers after which a `<` opens Java's method type parameters.
_ANGLE_WORDS = frozenset({
    "public", "protected", "private", "static", "final", "abstract", "default",
    "synchronized", "native", "strictfp",
})
_TYPE_OPENERS = (":", "|", "&", "<", ",", "(")


def _previous_code(lines: list[str], index: int) -> int:
    """Index of the nearest non-blank masked line above `index`, or -1."""
    cursor = index - 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    return cursor


def _starts_statement(lines: list[str], index: int) -> bool:
    """False when masked line `index` continues an expression from above.

    A declaration starts a statement: the line before it ends one (`;`, a
    brace, or a complete expression where semicolons are optional), or is an
    annotation. Blank lines, and comments (blank once masked), are skipped.
    """
    current = lines[index].lstrip()
    if current.startswith(_CONTINUING):
        return False
    cursor = _previous_code(lines, index)
    if cursor < 0:
        return True
    previous = lines[cursor].rstrip()
    if previous.lstrip().startswith("@"):
        return True
    if previous.endswith(("=>", "->")):
        return False
    if previous.endswith(("++", "--", ">")):
        return True
    return not previous.endswith(_CONTINUED)


def _word_before(masked: str, start: int, index: int) -> str:
    match = re.search(r"([A-Za-z_]\w*)\s*$", masked[start:index])
    return match.group(1) if match else ""


def _opens_generic(masked: str, start: int, index: int) -> bool:
    """Whether the `<` at `index` opens type arguments rather than comparing.

    It does straight after a name (`Map<`, `fn f<`), at the start of the
    header, or after a Java modifier (`public <T>`); never before `<`, `=`
    or `-` (`<<`, `<=`, Go's `<-chan`), and never after a space and an
    operand (`a < b`).
    """
    following = masked[index + 1 : index + 2]
    if following in ("<", "=", "-"):
        return False
    if index == start:
        return True
    previous = masked[index - 1]
    if previous.isalnum() or previous in "_$":
        return True
    return previous in " \t" and _word_before(masked, start, index) in _ANGLE_WORDS


def _type_brace(masked: str, start: int, index: int, kind: SymbolKind) -> bool:
    """Whether a `{` in a header opens a type (`{ a: string }`, Go's
    `interface{}`) rather than the declaration's body."""
    gap = masked[start:index]
    if gap.rstrip(" \t").endswith("\n"):
        return False      # a brace opening a line is a body, as rustfmt puts it
    before = gap.rstrip()
    if before.endswith(_TYPE_OPENERS):
        return True
    word = _word_before(masked, start, index)
    callable_ = kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
    return callable_ and word in ("interface", "struct")


def _signature(
    source: str,
    masked: str,
    start: int,
    kind: SymbolKind,
    comments: _Comments | None = None,
) -> tuple[str, int]:
    """Take a declaration's header verbatim, up to where its body begins.

    Brackets are tracked so nothing inside a parameter list, a generic or an
    object type ends the header early. `=>` and `->` are consumed whole, and
    an `=>` ends the header only once an initialiser `=` has been seen: before
    that it belongs to a function type.
    """
    paren = bracket = angle = brace = 0
    initialiser = False
    limit = min(len(masked), start + _MAX_SIGNATURE)
    end = limit
    index = start

    while index < limit:
        char = masked[index]
        pair = masked[index : index + 2]
        at_rest = paren == 0 and bracket == 0 and brace == 0

        if pair in ("=>", "->"):
            if pair == "=>" and initialiser and angle == 0:
                end = index + 2
                break
            index += 2
            continue
        if pair in ("<=", ">=", "==", "!=", "<<"):
            index += 2
            continue
        if pair == ">>" and angle:
            angle = max(0, angle - 2)
            index += 2
            continue

        # Languages without semicolons (Go, and JS relying on ASI) end a
        # declaration at the newline unless the next line plainly continues.
        if (
            char == "\n"
            and at_rest
            and angle == 0
            and (kind is SymbolKind.CONSTANT or not _continues(masked, start, index))
        ):
            end = index
            break
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "<" and at_rest and _opens_generic(masked, start, index):
            angle += 1
        elif char == ">" and at_rest and angle:
            angle -= 1
        elif char == "{":
            if brace:
                brace += 1
            elif at_rest and angle == 0:
                if not initialiser and _type_brace(masked, start, index, kind):
                    brace += 1
                else:
                    end = index
                    break
            elif initialiser and _FUNCTION_BODY.search(masked, start, index):
                end = index   # a function expression's body, inside a call
                break
        elif char == "}" and brace:
            brace -= 1
        elif at_rest and angle == 0:
            if char == ";":
                end = index
                break
            if char == "=":
                if kind is SymbolKind.TYPE:
                    end = index
                    break
                initialiser = True
        index += 1

    raw = comments.strip(source, start, end) if comments else source[start:end]
    text = " ".join(raw.split())
    return text.rstrip(" :"), end


def _continues(masked: str, start: int, newline: int) -> bool:
    """Whether a header broken at `newline` carries on onto the next line."""
    before = masked[start:newline].rstrip()
    if before.endswith(("(", ",", "<", "[", "=", "|", "&", ":", "->", "=>")):
        return True
    if re.search(r"\bwhere$", before):
        return True   # a Rust where clause runs on to the body
    rest = masked[newline + 1 :].lstrip(" \t")
    return rest.startswith(_CONTINUATIONS)


def _end_line(masked: str, start: int, header_end: int) -> int:
    """Line of the brace closing the body that follows the header; of the
    header's last character when no body follows; the file's last line when
    nothing closes the body.

    The body may sit after `=>` (arrow functions) or `=` (type aliases), so
    those are stepped over before looking for the opening brace.
    """
    line = masked.count("\n", 0, start) + 1
    position = header_end
    stepped = masked[header_end - 2 : header_end] == "=>"
    if masked[position : position + 1] == "=":
        position += 1
        stepped = True
    if stepped:
        while position < len(masked) and masked[position] in " \t\r\n":
            position += 1

    if position >= len(masked) or masked[position] != "{":
        return masked.count("\n", 0, header_end - 1) + 1 if header_end > start else line
    depth = 0
    for index in range(position, len(masked)):
        char = masked[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return masked.count("\n", 0, index) + 1
    return count_lines(masked)


def skip_angles(text: str) -> str:
    """`text` after a leading balanced `<...>` (a declaration's type parameters)."""
    stripped = text.lstrip()
    if not stripped.startswith("<"):
        return text
    depth = 0
    for index, char in enumerate(stripped):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0:
                return stripped[index + 1 :]
    return ""


def _split_top(text: str) -> list[str]:
    """Comma-separated parts at the top level of `<>` and `()`."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char in "<(":
            depth += 1
        elif char in ">)":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def _bases(signature: str) -> tuple[str, ...]:
    """Supertypes named after `extends` / `implements` in a class header.

    The declaration's own type parameters come off first: the bound in
    `class Box<T extends Comparable<T>>` is not a supertype. Type arguments
    are dropped from each base, while a call (`Mixin(User, Timestamped)`)
    stays whole.
    """
    head = re.search(r"\b(?:class|interface|struct|record|trait|enum)\s+[\w$]+",
                     signature)
    rest = skip_angles(signature[head.end() :]) if head else signature
    found: list[str] = []
    for keyword in ("extends", "implements"):
        pattern = rf"\b{keyword}\s+(.+?)(?=\s+(?:extends|implements)\b|$)"
        match = re.search(pattern, rest)
        if match is None:
            continue
        for part in _split_top(match.group(1)):
            name = part.strip().rstrip("{").strip()
            if "(" not in name:
                name = re.sub(r"<.*$", "", name).strip()
            if name:
                found.append(name)
    return tuple(found)


def _clean_block(lines: list[str]) -> str:
    """Strip `/** * */` framing and keep the description, not the tags.

    `@file`, `@fileoverview` and `@module` introduce the description itself,
    so their text is kept; any other tag marks where the description ends.
    """
    cleaned: list[str] = []
    for raw in lines:
        line = raw.strip()
        line = re.sub(r"^/\*\*?", "", line)
        line = re.sub(r"\*/$", "", line)
        line = re.sub(r"^\*\s?", "", line.strip()).rstrip()
        described = re.match(r"^@(?:file|fileoverview|module)\b\s*(.*)$", line)
        if described:
            line = described.group(1)
        elif line.startswith("@") and any(cleaned):
            break
        elif line.startswith("@"):
            continue
        cleaned.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()[:_MAX_DOC]


def _docstring(lines: list[str], index: int, style: str) -> str:
    """The doc comment directly above line `index`, in the given style.

    Decorator, annotation and attribute lines between the comment and the
    declaration are skipped, since that is where they conventionally sit.
    """
    cursor = index - 1
    while cursor >= 0 and re.match(r"^\s*(@\w|#\[)", lines[cursor]):
        cursor -= 1
    if cursor < 0:
        return ""

    if style == "block":
        if not lines[cursor].strip().endswith("*/"):
            return ""
        top = cursor
        while top >= 0 and "/*" not in lines[top]:
            top -= 1
        if top < 0 or "/**" not in lines[top]:
            return ""   # plain /* */ above code is usually commented-out code
        return _clean_block(lines[top : cursor + 1])

    marker = "///" if style == "triple-slash" else "//"
    collected: list[str] = []
    while cursor >= 0:
        stripped = lines[cursor].strip()
        if not stripped.startswith(marker):
            break
        if marker == "//" and stripped.startswith("///"):
            break
        collected.append(stripped[len(marker) :].strip())
        cursor -= 1
    return "\n".join(reversed(collected)).strip()[:_MAX_DOC]


def _dedupe(symbols: list[_Pending]) -> list[_Pending]:
    """Keep the first declaration of each name: TS overloads repeat it."""
    seen: set[tuple[str, SymbolKind]] = set()
    kept: list[_Pending] = []
    for symbol in symbols:
        key = (symbol.name, symbol.kind)
        if key in seen:
            continue
        seen.add(key)
        kept.append(symbol)
    return kept
