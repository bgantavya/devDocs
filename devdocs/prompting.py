"""Building the three prompts a run sends: file, module, architecture.

The system prompts are data, in `devdocs/prompts/*.md`, so wording can be
reviewed and changed without touching code. They are byte-identical across
calls of the same kind, which is the stable prefix providers cache. Everything
that varies — the facts and the source — goes in the user prompt after it.

The model is given facts, not asked to rediscover them: signatures, imports,
dependents and entry points come from the parser and the graph, so its job
shrinks to what code cannot say. Source is included up to a token budget;
beyond it, a skeleton keeps every signature and docstring and trims bodies.

The skeleton works on lines, not syntax, so one routine serves every language
the parsers cover. Each line is assigned to the innermost symbol around it —
as lead-in (decorators, doc comments), header, docstring, body or closing
brace — or to the top-level code between symbols, and each line records the
least body allowance that keeps it. One pass then renders any allowance, and
a binary search finds the largest that fits.

A prompt is a cache key, so the same project state must give the same bytes:
every list is sorted and capped, and nothing machine-specific (absolute paths,
times, environment) is ever included.
"""

from __future__ import annotations

import functools
import heapq
import posixpath
import re
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources

from devdocs.compose import docstring_summary, first_sentence
from devdocs.context import Snippet
from devdocs.graph import FileNode, ProjectGraph, is_stdlib
from devdocs.llm.budget import estimate_tokens
from devdocs.llm.catalog import Tier
from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind, count_lines
from devdocs.plan import FilePlan, ModulePlan

__all__ = ["Prompt", "PromptBuilder", "excerpt", "system_prompt"]

_KINDS = ("file", "module", "architecture")
_ROOT = "(project root)"
_NO_SYMBOLS = "(none — write no SYMBOL lines)"

# Most entries a prompt lists before "and N more".
_FILES = 100
_DEPENDENTS = 8
_TESTED_BY = 4
_IMPORTS = 12
_EXPORTS = 20
_DEFINES = 6
_INTERNAL_EDGES = 30
_MODULE_LINKS = 12
_SUBMODULES = 20
_COMPONENT_EDGES = 40
_ENTRY_POINTS = 15
_PACKAGES = 15

_HEADER_LINES = 60       # kept above the first declaration
_LICENCE_WINDOW = 40     # a licence header starts within this many lines
_MEMBER_LINES = 8        # field lines of a class or type kept as declarations
_LONG_LINE = 400         # a trimmed excerpt clips longer lines
_DECL_LINES = 40         # longest declaration header followed
_DECORATOR_REACH = 40    # farthest above its target a decorator may open
_HEAD_SHARE = 0.7        # of a head-and-tail excerpt
_NEVER = sys.maxsize     # need of a line no allowance keeps

# Line roles within a skeleton.
_HEAD, _GAP, _LEAD, _DECL, _DOC, _BODY, _CLOSE = range(7)

_CONTAINERS = frozenset({SymbolKind.CLASS, SymbolKind.INTERFACE, SymbolKind.TYPE})
_HASH_COMMENTS = frozenset({"python", "shell", "ruby", "elixir"})
_LICENCE = re.compile(r"copyright|licen[cs]e|spdx-license-identifier", re.IGNORECASE)
_DOCSTRING = re.compile(r"[rRuU]?(\"\"\"|'''|\"|')")
_CONTENT = re.compile(r"[^\W_]")
_BACKTICKS = re.compile(r"`+")


@dataclass(frozen=True)
class Prompt:
    """One model call, ready to price or send."""

    kind: str                 # "file" | "module" | "architecture"
    key: str                  # relpath, module directory, or ""
    tier: Tier
    system: str
    user: str
    max_output_tokens: int
    identity: str = ""        # `user` without retrieved context, if it has any

    @property
    def cache_text(self) -> str:
        """What a cached answer is keyed on: the prompt minus retrieved
        context, so an edit to a neighbour does not buy this answer again."""
        return self.identity or self.user


def system_prompt(kind: str) -> str:
    """The packaged system prompt for `kind`, read once and cached.

    The file's leading HTML comment is a note for maintainers and is never
    sent. Raises ValueError for a kind other than file, module or architecture.
    """
    if kind not in _KINDS:
        expected = ", ".join(_KINDS)
        raise ValueError(f"unknown prompt kind {kind!r}; expected one of {expected}")
    return _load_prompt(kind)


@functools.cache
def _load_prompt(kind: str) -> str:
    resource = resources.files("devdocs.prompts") / f"{kind}.md"
    text = resource.read_text(encoding="utf-8").replace("\r\n", "\n").lstrip()
    if text.startswith("<!--"):
        end = text.find("-->")
        if end >= 0:
            text = text[end + 3 :]
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def excerpt(source: str, facts: FileFacts, budget_tokens: int) -> tuple[str, bool]:
    """Source trimmed to `budget_tokens`; the bool is True when it was cut.

    Normalised first (LF line ends, no trailing whitespace, three or more
    blank lines collapsed to one, a leading licence header dropped). If that
    does not fit: a skeleton with every declaration and as much of each body
    as fits, then with docstrings cut to their first line, then without class
    fields, then the first declarations that fit; with no symbols or a failed
    parse, the head and tail. Deterministic and never raises; the result fits
    any budget of one token or more.
    """
    numbers, lines = _normalise(source, facts.language)
    text = "\n".join(lines)
    if not text:
        return "", False
    if estimate_tokens(text) <= budget_tokens:
        return text, False
    if facts.confidence is not Confidence.FAILED:
        fitted = _Skeleton(numbers, lines, facts).fit(budget_tokens)
        if fitted is not None:
            return fitted, True
    return _head_and_tail(lines, budget_tokens), True


class PromptBuilder:
    """Builds the prompts of one project from its plan, facts and graph."""

    def __init__(
        self, *, project: str, description: str = "", source_tokens: int = 6000
    ) -> None:
        self.project = project
        self.description = description
        self.source_tokens = source_tokens
        self._parents: tuple[ProjectGraph, int, dict[str, list[str]]] | None = None

    def file(
        self,
        plan: FilePlan,
        facts: FileFacts,
        source: str,
        node: FileNode | None,
        context: Sequence[Snippet] = (),
    ) -> Prompt:
        """The prompt for one file: its place in the graph, the symbols to
        describe, retrieved context, and its source trimmed to `source_tokens`."""
        lines = self._project()
        loc = facts.loc or count_lines(source)
        shape = ", ".join(
            part for part in (facts.language, _plural(loc, "line")) if part
        )
        lines.append(f"File: {plan.relpath} ({shape})")
        if node is not None:
            lines += _listed("Imported by", sorted(set(node.dependents)), _DEPENDENTS)
            lines += _listed("Tested by", sorted(set(node.tested_by)), _TESTED_BY)
            lines += _listed("Imports", sorted(set(node.internal)), _IMPORTS)
            lines += _listed("External", sorted(set(node.external)), _IMPORTS)
            if node.entry_point.strip():
                lines.append(f"Entry point: {_one_line(node.entry_point)}")
        lines += _listed("Exports", list(dict.fromkeys(facts.exports)), _EXPORTS)
        if facts.confidence is not Confidence.EXACT:
            notes = [_one_line(note) for note in facts.notes if note.strip()]
            lines.append("; ".join([f"Parse: {facts.confidence.value}", *notes]))
        lines.append(f"Describe: {', '.join(plan.describe) or _NO_SYMBOLS}")
        at = len(lines)

        text, cut = excerpt(source, facts, self.source_tokens)
        fence = _fence(text)
        lines += [
            "",
            "Source (excerpt; bodies trimmed to fit):" if cut else "Source:",
            fence + facts.language,
            *([text] if text else []),
            fence,
        ]
        reference = _context(
            "How other files use this one, and what it calls (for its role; "
            "do not describe them):", context,
        )
        return Prompt(
            kind="file",
            key=plan.relpath,
            tier=Tier.FILE,
            system=system_prompt("file"),
            user="\n".join([*lines[:at], *reference, *lines[at:]]),
            identity="\n".join(lines) if reference else "",
            max_output_tokens=min(1400, max(256, 200 + 45 * len(plan.describe))),
        )

    def module(
        self,
        plan: ModulePlan,
        graph: ProjectGraph,
        summaries: Mapping[str, str],
        facts: Mapping[str, FileFacts],
        context: Sequence[Snippet] = (),
    ) -> Prompt:
        """`summaries` maps each file to its one-sentence purpose, if known.
        Past 100 files, the most imported are listed and the rest counted."""
        lines = self._project()
        lines.append(f"Module: {_name(plan.path)}")
        shown = _most_imported(plan.files, graph, _FILES)
        rows = [
            _file_entry(relpath, summaries.get(relpath, ""), facts.get(relpath))
            for relpath in shown
        ]
        if len(plan.files) > len(shown):
            rows.append(f"and {len(plan.files) - len(shown)} more")
        lines += _block("Files:", rows, len(rows))

        members = set(plan.files)
        edges: set[tuple[str, str]] = set()
        entries: list[str] = []
        for relpath in plan.files:
            node = graph.file(relpath)
            if node is None:
                continue
            base = posixpath.basename(relpath)
            edges.update(
                (base, posixpath.basename(target))
                for target in node.internal
                if target in members and target != relpath
            )
            if node.entry_point.strip():
                entries.append(f"{base} — {_one_line(node.entry_point)}")
        imports = [f"{source} -> {target}" for source, target in sorted(edges)]
        lines += _block("Internal imports:", imports, _INTERNAL_EDGES)

        module = graph.modules.get(plan.path)
        if module is not None:
            for label, paths in (("Depends on", module.depends_on),
                                 ("Used by", module.used_by)):
                others = sorted(set(paths) - {plan.path})
                lines += _listed(label, [_name(path) for path in others], _MODULE_LINKS)
        lines += _block("Entry points:", entries, _ENTRY_POINTS)
        lines += _listed("Submodules", self._submodules(graph).get(plan.path, []),
                         _SUBMODULES)
        reference = _context(
            "Related code elsewhere (reference only; do not describe it):", context
        )
        return Prompt(
            kind="module",
            key=plan.path,
            tier=Tier.MODULE,
            system=system_prompt("module"),
            user="\n".join([*lines, *reference]),
            identity="\n".join(lines) if reference else "",
            max_output_tokens=600,
        )

    def architecture(
        self,
        graph: ProjectGraph,
        summaries: Mapping[str, str],
        languages: Mapping[str, int],
        context: Sequence[Snippet] = (),
    ) -> Prompt:
        """`summaries` maps each module to its one-sentence purpose, if known;
        `languages` counts documentable files per language."""
        lines = self._project()
        counted = sorted(
            ((count, language) for language, count in languages.items() if count > 0),
            key=lambda item: (-item[0], item[1]),
        )
        if counted:
            lines.append("Languages: " + ", ".join(
                f"{language} ({_plural(count, 'file')})" for count, language in counted
            ))

        component = graph.components(12)
        sizes: dict[str, int] = {}
        for node in graph.files.values():
            name = component.get(node.module)
            if name is not None:
                sizes[name] = sizes.get(name, 0) + 1
        rows: list[str] = []
        for name in sorted(set(component.values())):
            summary = _one_line(summaries.get(name, ""))
            size = _plural(sizes.get(name, 0), "file")
            rows.append(f"{_name(name)}: {_clause(summary)}; {size}" if summary
                        else f"{_name(name)}: {size}")
        lines += _block("Components:", rows, len(rows))

        weights: dict[tuple[str, str], int] = {}
        for (source, target), count in graph.module_edges().items():
            pair = (component.get(source, source), component.get(target, target))
            if pair[0] != pair[1]:
                weights[pair] = weights.get(pair, 0) + count
        ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
        lines += _block(
            "Component dependencies:",
            [f"{_name(source)} -> {_name(target)} ({_plural(count, 'import')})"
             for (source, target), count in ranked],
            _COMPONENT_EDGES,
        )
        lines += _block(
            "Entry points:",
            [f"{node.relpath} — {_one_line(node.entry_point)}"
             for node in graph.entry_points()],
            _ENTRY_POINTS,
        )
        packages = _packages(graph)[:_PACKAGES]
        if packages:
            lines.append(f"External packages: {', '.join(packages)}")
        reference: list[str] = []
        for snippet in context:
            fence = _fence(snippet.text)
            reference += ["", f"Entry point code ({snippet.source}:{snippet.line}; "
                          "FLOW follows its order):", fence, snippet.text, fence]
        return Prompt(
            kind="architecture",
            key="",
            tier=Tier.ARCHITECTURE,
            system=system_prompt("architecture"),
            user="\n".join([*lines, *reference]),
            identity="\n".join(lines) if reference else "",
            max_output_tokens=1500,
        )

    def _project(self) -> list[str]:
        parts = [text for text in (_one_line(self.project), _one_line(self.description))
                 if text]
        return [f"Project: {' — '.join(parts)}"] if parts else []

    def _submodules(self, graph: ProjectGraph) -> dict[str, list[str]]:
        """Each module's nearest modules below it, indexed once per graph.

        Nearest rather than direct children, so a module under a directory
        holding no files of its own (`packages/x/src`) still has a parent.
        """
        cached = self._parents
        if cached is None or cached[0] is not graph or cached[1] != len(graph.modules):
            index: dict[str, list[str]] = {}
            for path in sorted(graph.modules):
                if not path:
                    continue
                parent = posixpath.dirname(path)
                while parent and parent not in graph.modules:
                    parent = posixpath.dirname(parent)
                index.setdefault(parent, []).append(path)
            cached = self._parents = (graph, len(graph.modules), index)
        return cached[2]


# -- prompt text helpers --------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _name(path: str) -> str:
    return path or _ROOT


def _clause(sentence: str) -> str:
    """A summary sentence about to be followed by "; ...", without its stop."""
    return sentence.removesuffix(".")


def _listing(items: Sequence[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    rest = len(items) - limit
    return f"{shown} and {rest} more" if rest > 0 else shown


def _listed(label: str, items: Sequence[str], limit: int) -> list[str]:
    """A `label: a, b and N more` line, or nothing for no items."""
    return [f"{label}: {_listing(items, limit)}"] if items else []


def _block(title: str, rows: Sequence[str], limit: int) -> list[str]:
    """A title line over `- row` lines, or nothing for no rows."""
    if not rows:
        return []
    block = [title, *(f"- {row}" for row in rows[:limit])]
    if len(rows) > limit:
        block.append(f"- and {len(rows) - limit} more")
    return block


def _context(title: str, snippets: Sequence[Snippet]) -> list[str]:
    """Retrieved lines as `- path:line kind name: code`, or nothing. Marked
    as reference so the reply describes this file, not its neighbours."""
    if not snippets:
        return []
    rows = []
    for snippet in snippets:
        label = f"{snippet.kind} {snippet.symbol}".strip()
        rows.append(f"- {snippet.source}:{snippet.line} {label}: {snippet.text}")
    return ["", title, *rows]


def _fence(text: str) -> str:
    """A code fence longer than any backtick run inside `text`."""
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def _file_entry(relpath: str, summary: str, facts: FileFacts | None) -> str:
    """A module's file row: its purpose, then its main public names."""
    text = _one_line(summary)
    if not text and facts is not None:
        text = first_sentence(docstring_summary(facts.module_docstring))
    text = text or "(no summary)"
    base = posixpath.basename(relpath)
    names: list[str] = []
    if facts is not None:
        ordered = sorted(
            facts.public_symbols,
            key=lambda symbol: (symbol.kind is SymbolKind.CONSTANT, symbol.line),
        )
        names = list(dict.fromkeys(symbol.name for symbol in ordered))
    if not names:
        return f"{base}: {text}"
    return f"{base}: {_clause(text)}; defines: {_listing(names, _DEFINES)}"


def _most_imported(
    relpaths: Sequence[str], graph: ProjectGraph, limit: int
) -> list[str]:
    """At most `limit` of `relpaths`, in their own order; the most imported
    are the ones kept, ties going to the earlier path."""
    if len(relpaths) <= limit:
        return list(relpaths)

    def rank(relpath: str) -> tuple[int, str]:
        node = graph.file(relpath)
        return (-node.fan_in if node is not None else 0, relpath)

    kept = set(heapq.nsmallest(limit, relpaths, key=rank))
    return [relpath for relpath in relpaths if relpath in kept]


def _packages(graph: ProjectGraph) -> list[str]:
    """Non-stdlib external packages by importing files, most first, then by name."""
    counts: dict[str, int] = {}
    for node in graph.files.values():
        names = {name for name in node.external
                 if name and not is_stdlib(node.language, name)}
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ranked]


# -- excerpt: normalising --------------------------------------------------------


def _normalise(source: str, language: str) -> tuple[list[int], list[str]]:
    """The cleaned lines, each paired with its 1-based line number in `source`.

    Lines break where the language's parser breaks them, so symbol line
    numbers stay valid: for Python that includes a lone carriage return.
    """
    content = source.removeprefix("\ufeff").replace("\r\n", "\n")
    if language == "python":
        content = content.replace("\r", "\n")
    raw = [line.rstrip() for line in content.split("\n")]
    start, stop = _licence(raw, language) or (0, 0)
    numbers: list[int] = []
    lines: list[str] = []
    blanks: list[int] = []
    for index, text in enumerate(raw):
        if start <= index < stop:
            continue
        if not text:
            blanks.append(index + 1)
            continue
        if lines:
            for number in blanks if len(blanks) < 3 else blanks[:1]:
                numbers.append(number)
                lines.append("")
        blanks = []
        numbers.append(index + 1)
        lines.append(text)
    return numbers, lines


def _licence(lines: list[str], language: str) -> tuple[int, int] | None:
    """[start, stop) of the licence comment heading the file, if any.

    Only blank lines, a shebang and other comments may precede it, and it
    starts within the first 40 lines. Docstrings, Rust `//!` and `///` doc
    comments and a Go package comment are documentation, never a licence.
    """
    hashed = language in _HASH_COMMENTS
    index = 0
    while index < min(len(lines), _LICENCE_WINDOW):
        text = lines[index].lstrip()
        if not text or (index == 0 and text.startswith("#!")):
            index += 1
            continue
        stop = _comment_block(lines, index, hashed)
        if stop < 0:
            return None
        if _LICENCE.search("\n".join(lines[index:stop])):
            package_doc = (language == "go" and stop < len(lines)
                           and lines[stop].startswith("package "))
            return None if package_doc else (index, stop)
        index = stop
    return None


def _comment_block(lines: list[str], start: int, hashed: bool) -> int:
    """End (exclusive) of the plain comment block opening at `start`, or -1."""
    text = lines[start].lstrip()
    if hashed:
        if not text.startswith("#"):
            return -1
        stop = start
        while stop < len(lines) and lines[stop].lstrip().startswith("#"):
            stop += 1
        return stop
    if text.startswith("/*"):
        offset = lines[start].index("/*") + 2
        for stop in range(start, len(lines)):
            line = lines[stop]
            if "*/" in line[offset if stop == start else 0 :]:
                return stop + 1 if line.endswith("*/") else -1   # code after it
        return -1
    if text.startswith("//") and not text.startswith(("///", "//!")):
        stop = start
        while stop < len(lines):
            current = lines[stop].lstrip()
            if not current.startswith("//") or current.startswith(("///", "//!")):
                break
            stop += 1
        return stop
    return -1


# -- excerpt: trimming ------------------------------------------------------------


@dataclass
class _Site:
    """One declaration in the normalised lines; every index is 0-based."""

    depth: int
    container: bool               # class, interface or type: its fields declare
    decl: int
    decl_end: int
    end: int                      # last line; -1 until resolved
    doc: tuple[int, int] | None   # Python docstring lines, inclusive
    lead: int = 0                 # first decorator or doc-comment line above
    close: bool = False           # `end` is the body's closing brace

    @property
    def header_end(self) -> int:
        return self.doc[1] if self.doc is not None else self.decl_end


class _Skeleton:
    """A file's lines, each assigned to a declaration or to top-level code.

    `levels` holds, for each detail level, the least body allowance that keeps
    each line: with docs in full; with docstrings and doc comments cut to
    their first line; and with class fields no longer kept as declarations.
    """

    def __init__(self, numbers: list[int], lines: list[str], facts: FileFacts) -> None:
        self.lines = lines
        self.python = facts.language == "python"
        self.hashed = facts.language in _HASH_COMMENTS
        self.sites = self._locate(numbers, facts)
        self._leads()
        self._ends()
        self.owner = self._paint()
        self.levels: tuple[tuple[list[int], int], ...] = ()
        if self.sites:
            full, brief, bare = self._levels()
            self.levels = ((full, 2), (brief, 2), (bare, 1))

    def fit(self, budget: int) -> str | None:
        """The fullest skeleton within `budget`, or None when not even the
        header region and a count of the declarations fit."""
        for need, slack in self.levels:
            text = self._widest(need, slack, budget)
            if text is not None:
                return text
        return self._first(budget) if self.sites else None

    def _widest(self, need: list[int], slack: int, budget: int) -> str | None:
        """The rendering at the largest allowance within `budget`, if any fits."""

        def fits(allowance: int) -> bool:
            text = "\n".join(self._render(need, allowance, slack))
            return estimate_tokens(text) <= budget

        if not fits(0):
            return None
        widest = max((value for value in need if value < _NEVER), default=0)
        return "\n".join(self._render(need, _largest(widest, fits), slack))

    def _first(self, budget: int) -> str | None:
        """The barest rendering of the first declarations that fit, closed by
        a count of the ones left out."""
        need, slack = self.levels[-1]

        def render(kept: int) -> str:
            out = self._render(need, 0, slack, stop=self.sites[kept].lead)
            while out and not out[-1]:
                out.pop()
            out.append(f"... ({_plural(len(self.sites) - kept, 'more declaration')}"
                       " omitted)")
            return "\n".join(out)

        def fits(kept: int) -> bool:
            return estimate_tokens(render(kept)) <= budget

        if not fits(0):
            return None
        return render(_largest(len(self.sites) - 1, fits))

    def _render(
        self, need: list[int], allowance: int, slack: int, stop: int | None = None
    ) -> list[str]:
        """The lines `allowance` keeps; each run of the rest becomes a marker."""
        lines, owner = self.lines, self.owner
        limit = len(lines) if stop is None else stop
        out: list[str] = []
        line = 0
        while line < limit:
            if need[line] <= allowance:
                out.append(_clip(lines[line]))
                line += 1
                continue
            end = line + 1
            while end < limit and owner[end] == owner[line] and need[end] > allowance:
                end += 1
            _omit(lines[line:end], out, slack)
            line = end
        return out

    # -- locating declarations --------------------------------------------------

    def _locate(self, numbers: list[int], facts: FileFacts) -> list[_Site]:
        """A site per symbol whose line survived normalisation, in line order.

        A symbol on a line the source does not have is skipped, and of two
        symbols on one line the first is kept: facts and source may disagree.
        """
        count = len(self.lines)
        exact = facts.confidence is Confidence.EXACT
        found: dict[int, tuple[Symbol, int]] = {}
        stack = [(symbol, 0) for symbol in reversed(facts.symbols)]
        while stack:
            symbol, depth = stack.pop()
            stack.extend((child, depth + 1) for child in reversed(symbol.children))
            index = bisect_left(numbers, symbol.line)
            if symbol.line > 0 and index < count and numbers[index] == symbol.line:
                found.setdefault(index, (symbol, depth))

        decls = sorted(found)
        sites: list[_Site] = []
        for position, decl in enumerate(decls):
            symbol, depth = found[decl]
            limit = decls[position + 1] - 1 if position + 1 < len(decls) else count - 1
            end = -1
            if symbol.end_line >= symbol.line:
                end = limit = bisect_right(numbers, symbol.end_line) - 1
            decl_end = self._header(
                decl, min(limit, decl + _DECL_LINES - 1), symbol.signature
            )
            doc = None
            wants_doc = bool(symbol.docstring) or not exact
            if self.python and symbol.kind is not SymbolKind.CONSTANT and wants_doc:
                doc = self._docstring(decl_end, limit)
            container = symbol.kind in _CONTAINERS
            sites.append(_Site(depth, container, decl, decl_end, end, doc))
        return sites

    def _header(self, decl: int, stop: int, signature: str) -> int:
        """Last line of a declaration's header: where its signature ends.

        The signature is the source with whitespace collapsed, so counting
        non-space characters line by line finds the line it ends on.
        """
        want = len("".join(signature.split()))
        seen = 0
        last = decl
        for line in range(decl, stop + 1):
            seen += len("".join(self.lines[line].split()))
            if seen >= want:
                last = line
                break
        if not self.python and last < stop and self.lines[last + 1].strip() == "{":
            last += 1
        return last

    def _docstring(self, header_end: int, limit: int) -> tuple[int, int] | None:
        """The lines of a string literal opening a Python body, if there is one."""
        start = header_end + 1
        while start <= limit and not self.lines[start]:
            start += 1
        if start > limit:
            return None
        text = self.lines[start].lstrip()
        match = _DOCSTRING.match(text)
        if match is None:
            return None
        quote = match.group(1)
        if len(quote) == 1 or quote in text[match.end() :]:
            return start, start
        for end in range(start + 1, limit + 1):
            if quote in self.lines[end]:
                return start, end
        return None

    def _leads(self) -> None:
        """Extend each site up over its decorators and doc comments, never into
        the header or body of an earlier symbol."""
        ended: list[int] = []
        closed = header = -1
        for site in self.sites:
            while ended and ended[0] < site.decl:
                closed = max(closed, heapq.heappop(ended))
            site.lead = self._lead_in(site.decl, max(closed, header) + 1)
            header = max(header, site.header_end)
            if site.end >= 0:
                heapq.heappush(ended, site.end)

    def _lead_in(self, decl: int, floor: int) -> int:
        lines = self.lines
        indent = _indent(lines[decl])
        start = decl
        line = decl - 1
        while line >= floor:
            text = lines[line].strip()
            if not text:
                break
            aligned = _indent(lines[line]) <= indent or text.startswith("*")
            if aligned and (self._comment(text) or text.startswith(("@", "#["))):
                start = line
                line -= 1
                continue
            opener = self._decorator(line, floor, indent)
            if opener < 0:
                break
            start = opener
            line = opener - 1
        return start

    def _decorator(self, line: int, floor: int, indent: int) -> int:
        """The line a multi-line decorator ending at `line` opens on, or -1."""
        for above in range(line - 1, max(floor, line - _DECORATOR_REACH) - 1, -1):
            text = self.lines[above]
            stripped = text.strip()
            if not stripped:
                return -1
            if stripped.startswith(("@", "#[")) and _indent(text) == indent:
                return above
        return -1

    def _ends(self) -> None:
        """Resolve an unknown end to just before the next symbol at the same or
        a shallower depth, less trailing lines that are blank or indented less
        than the declaration (an enclosing block's), and mark ends that are a
        closing brace."""
        sites = self.sites
        following = [-1] * len(sites)
        stack: list[int] = []
        for position in range(len(sites) - 1, -1, -1):
            while stack and sites[stack[-1]].depth > sites[position].depth:
                stack.pop()
            following[position] = stack[-1] if stack else -1
            stack.append(position)

        for position, site in enumerate(sites):
            if site.end < 0:
                after = following[position]
                end = sites[after].lead - 1 if after >= 0 else len(self.lines) - 1
                indent = _indent(self.lines[site.decl])
                while end > site.header_end and (
                    not self.lines[end] or _indent(self.lines[end]) < indent
                ):
                    end -= 1
                site.end = end
            site.end = max(site.end, site.header_end)
            site.close = (
                not self.python
                and site.end > site.header_end
                and self.lines[site.end].lstrip().startswith("}")
            )

    # -- classifying lines ------------------------------------------------------

    def _paint(self) -> list[int]:
        """The innermost site around each line, or -1 for top-level code."""
        sites = self.sites
        owner = [-1] * len(self.lines)
        order = sorted(range(len(sites)), key=lambda i: (sites[i].lead, -sites[i].end))
        active: list[int] = []
        cursor = 0
        for line in range(len(self.lines)):
            while cursor < len(order) and sites[order[cursor]].lead <= line:
                active.append(order[cursor])
                cursor += 1
            while active and sites[active[-1]].end < line:
                active.pop()
            if active:
                owner[line] = active[-1]
        return owner

    def _role(self, line: int) -> int:
        index = self.owner[line]
        if index < 0:
            return _HEAD if line < self.sites[0].lead else _GAP
        site = self.sites[index]
        if line < site.decl:
            return _LEAD
        if line <= site.decl_end:
            return _DECL
        if site.doc is not None and site.doc[0] <= line <= site.doc[1]:
            return _DOC
        if line == site.end and site.close:
            return _CLOSE
        return _BODY

    def _levels(self) -> tuple[list[int], list[int], list[int]]:
        """Per line, the least allowance that keeps it, at each detail level.

        Body lines are ranked within their symbol and top-level lines within
        their run; a method needs twice its class's allowance per line.
        """
        full: list[int] = []
        brief: list[int] = []
        bare: list[int] = []
        ranked: dict[int, int] = {}
        opened: set[tuple[int, int]] = set()   # docs past their first content line
        run = 0
        for line, text in enumerate(self.lines):
            role = self._role(line)
            index = self.owner[line]
            if role == _HEAD:
                most = least = plain = 0 if line < _HEADER_LINES else _NEVER
            elif role in (_DECL, _CLOSE):
                most = least = plain = 0
            elif role in (_LEAD, _DOC):
                stripped = text.strip()
                prose = role == _DOC or self._comment(stripped)
                most = 0
                least = plain = _NEVER if prose and (index, role) in opened else 0
                if prose and _CONTENT.search(stripped):
                    opened.add((index, role))
            elif role == _GAP:
                if self.owner[line - 1] >= 0:
                    run = 0
                most = least = plain = run + 1
                run += bool(text)
            else:
                site = self.sites[index]
                rank = ranked.get(index, 0) + 1
                if text:
                    ranked[index] = rank
                plain = rank << site.depth
                member = max(rank - _MEMBER_LINES, 0) << site.depth
                most = least = member if site.container else plain
            full.append(most)
            brief.append(least)
            bare.append(plain)
        return full, brief, bare

    def _comment(self, text: str) -> bool:
        if self.hashed:
            return text.startswith("#")
        return text.startswith(("//", "/*", "*"))


def _omit(run: list[str], out: list[str], slack: int) -> None:
    """Add `run` to `out` as one marker at its indentation.

    Trailing blank lines stay, as separators. A run no longer than its
    marker stays as it is, as does a lone line up to `slack` markers long:
    replacing one short line with a marker saves nothing and loses it.
    """
    first, last = 0, len(run)
    while first < last and not run[first]:
        first += 1
    if first == last:
        out.extend(run)
        return
    while not run[last - 1]:
        last -= 1
    core = run[first:last]
    indent = min((line[: _indent(line)] for line in core if line), key=len)
    marker = indent + _omitted(last)
    size = sum(len(line) + 1 for line in core)
    room = len(marker) + 1
    if size <= room or (len(core) == 1 and size <= slack * room):
        out.extend(run[:last])
    else:
        out.append(marker)
    out.extend(run[last:])


def _head_and_tail(lines: list[str], budget: int) -> str:
    """The first and last lines around one marker, 70% of the room to the head.

    Lines are clipped first, so a minified line cannot fill the whole budget.
    """
    clipped = [_clip(line) for line in lines]
    room = _char_limit(budget) - len(_omitted(len(lines))) - 1
    head_room = int(room * _HEAD_SHARE)
    head = _fill(clipped, head_room)
    tail = _fill(reversed(clipped[head:]), room - head_room)
    while True:
        omitted = len(clipped) - head - tail
        middle = [_omitted(omitted)] if omitted else []
        text = "\n".join([*clipped[:head], *middle, *clipped[len(clipped) - tail :]])
        if estimate_tokens(text) <= budget:
            return text
        if not head and not tail:
            return ""
        if tail:
            tail -= 1
        else:
            head -= 1


def _fill(lines: Iterable[str], room: int) -> int:
    """How many of `lines`, from the first, fit in `room` characters."""
    used = count = 0
    for line in lines:
        used += len(line) + 1
        if used > room:
            break
        count += 1
    return count


def _char_limit(budget: int) -> int:
    """The most characters `estimate_tokens` lets into `budget` tokens."""
    return _largest(
        max(budget, 0) * 8 + 8, lambda size: estimate_tokens("x" * size) <= budget
    )


def _largest(high: int, accepts: Callable[[int], bool]) -> int:
    """The largest value in [0, high] that `accepts`, by binary search: it
    accepts 0, and stops accepting as values grow."""
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if accepts(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _clip(line: str) -> str:
    if len(line) <= _LONG_LINE:
        return line
    return f"{line[:_LONG_LINE]} ... ({len(line) - _LONG_LINE} characters omitted)"


def _omitted(count: int) -> str:
    return f"... ({_plural(count, 'line')} omitted)"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())
