"""Deciding which documents are worth paying a model to write.

Every documentable file gets a document; the question is only whether its
prose comes from a model (PROSE) or entirely from parsed facts (STUB, zero
tokens). The planner answers it with two deterministic numbers:

- `score` — how significant the file is: fan-in, public surface, entry
  points, size, recent activity; tests, generated and vendored code sink.
- `need` — how much a model would add: a file whose module docstring and
  every public symbol are already documented needs nothing a stub cannot say.

Prose goes to the files with the highest `score * need` up to a coverage
cut, to the busiest modules and to the architecture overview. When a budget
cannot pay for all of it, the overview is kept first, then modules, then
files by value: each overview frames many files, so the least valuable file
prose is the first to go. Buying what a cached overview sums up asks that
overview again, so under a tight request limit a module can be cut while
files below it are bought. This is where most of a run's savings come from:
measured on a 579-file corpus, planning cut cost by 95% against documenting
every file.
"""

from __future__ import annotations

import heapq
import math
import posixpath
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from devdocs.graph import FileNode, ModuleNode, ProjectGraph
from devdocs.history import FileHistory
from devdocs.parse import Confidence, FileFacts, Symbol, SymbolKind
from devdocs.render.model import Mode
from devdocs.scan import Role, SourceFile

__all__ = [
    "FilePlan",
    "Fit",
    "ModulePlan",
    "Plan",
    "Planner",
    "PlannerOptions",
    "Signals",
    "fit_to_budget",
    "need",
    "score",
    "signals_for",
]

_PURPOSE_CHARS = 80          # module docstring long enough to state a purpose
_NEED_FLOOR = 0.25           # least need of a file with any gap
_API_DUNDERS = frozenset({"__init__", "__call__"})
_DESCRIBE_LIMIT = 25
_MODULE_TOP_FILES = 5        # file scores summed into a module's score
_MODULE_FLOOR = 8            # eligible modules that always get prose
_MODULE_SHARE = 0.25         # share of eligible modules beyond the floor
_BUDGET_SLACK = 1e-12        # float noise in a sum that lands on the limit
_INCLUDED = f" (documented in source; included at need {_NEED_FLOOR})"


@dataclass(frozen=True)
class Signals:
    """The deterministic inputs to `score`."""

    fan_in: int = 0
    public_symbols: int = 0
    has_exports: bool = False        # explicit export list (__all__, export {})
    is_entry_point: bool = False
    loc: int = 0
    docstring_chars: int = 0         # length of the module docstring
    commits: int = 0                 # commits in the recent-history window
    authors: int = 0
    is_private_module: bool = False  # name starts with "_" (not __init__/__main__)
    is_test: bool = False
    is_generated: bool = False
    is_vendored: bool = False


def score(signals: Signals) -> float:
    """Significance of a file. Higher means more worth a reader's attention.

    A weighted sum of log-damped counts and 0/1 flags, so no single count can
    dominate, rounded to 4 decimals so rankings never turn on float noise.
    """
    s = signals
    total = (
        3.0 * _log1p(s.fan_in)
        + 1.5 * _log1p(s.public_symbols)
        + 2.0 * s.has_exports
        + 1.5 * s.is_entry_point
        + 0.8 * _log1p(s.loc / 100)
        + 1.0 * (s.docstring_chars > 200)
        + 1.2 * _log1p(s.commits)
        + 1.0 * min(max(s.authors, 0), 3)
        - 2.0 * (s.public_symbols <= 0)
        - 2.0 * s.is_private_module
        - 3.0 * s.is_test
        - 4.0 * s.is_generated
        - 5.0 * s.is_vendored
    )
    return _round(total)


def need(facts: FileFacts) -> float:
    """How much model prose would add, from 0.0 (nothing) to 1.0.

    A missing purpose (module docstring under 80 characters) adds 0.4 and the
    undocumented share of the API up to 0.6; any gap counts for at least 0.25.
    A failed parse needs nothing: there is no symbol table to ground prose in.
    """
    if facts.confidence is Confidence.FAILED:
        return 0.0
    api = _api(facts)
    missing = sum(1 for _, symbol in api if not symbol.docstring.strip())
    share = missing / len(api) if api else 0.0
    purposeless = len(facts.module_docstring.strip()) < _PURPOSE_CHARS
    if not purposeless and not missing:
        return 0.0
    return _round(max(0.4 * purposeless + 0.6 * share, _NEED_FLOOR))


def signals_for(
    file: SourceFile,
    facts: FileFacts | None,
    node: FileNode | None,
    history: FileHistory | None,
) -> Signals:
    """Gather one file's `Signals` from its scan entry, facts, node and history.

    Any of the last three may be missing and then contributes zeros; `loc`
    falls back to the scanner's line count when the parser recorded none.
    """
    facts = facts if facts is not None else FileFacts(file.path, file.language)
    history = history if history is not None else FileHistory()
    stem = file.stem
    return Signals(
        fan_in=node.fan_in if node is not None else 0,
        public_symbols=len(facts.public_symbols),
        has_exports=bool(facts.exports),
        is_entry_point=node is not None and bool(node.entry_point),
        loc=facts.loc or file.lines,
        docstring_chars=len(facts.module_docstring.strip()),
        commits=history.commits,
        authors=history.authors,
        is_private_module=stem.startswith("_")
        and stem not in ("__init__", "__main__"),
        is_test=file.role is Role.TEST,
        is_generated=file.role is Role.GENERATED,
        is_vendored=file.role is Role.VENDORED,
    )


@dataclass(frozen=True)
class FilePlan:
    """The decision for one file, with the numbers behind it."""

    relpath: str
    score: float
    need: float
    mode: Mode
    reason: str                       # why PROSE or STUB, shown by `estimate`
    describe: tuple[str, ...] = ()    # undocumented public qualnames to describe

    @property
    def value(self) -> float:
        return self.score * self.need


@dataclass(frozen=True)
class ModulePlan:
    """The decision for one directory's overview document."""

    path: str                         # directory; "" for the root
    files: tuple[str, ...]
    score: float
    mode: Mode
    reason: str


@dataclass
class Plan:
    """Every decision of one run, each list in priority order."""

    files: list[FilePlan] = field(default_factory=list)
    modules: list[ModulePlan] = field(default_factory=list)
    architecture: Mode = Mode.STUB
    architecture_reason: str = ""

    def prose_files(self) -> list[FilePlan]:
        return [p for p in self.files if p.mode is Mode.PROSE]

    def prose_modules(self) -> list[ModulePlan]:
        return [p for p in self.modules if p.mode is Mode.PROSE]

    def calls(self) -> int:
        """Model calls the plan implies, before any cache hits."""
        architecture = 1 if self.architecture is Mode.PROSE else 0
        return len(self.prose_files()) + len(self.prose_modules()) + architecture

    def demote(self, kind: str, key: str, reason: str) -> None:
        """Turn one PROSE item into a STUB, e.g. when the budget cannot fit it."""
        if kind == "architecture":
            self.architecture = Mode.STUB
            self.architecture_reason = reason
        elif kind == "file":
            index = next((i for i, p in enumerate(self.files) if p.relpath == key), -1)
            if index < 0:
                raise KeyError(f"file {key!r} is not in the plan")
            self.files[index] = replace(
                self.files[index], mode=Mode.STUB, reason=reason
            )
        elif kind == "module":
            index = next((i for i, p in enumerate(self.modules) if p.path == key), -1)
            if index < 0:
                raise KeyError(f"module {key!r} is not in the plan")
            self.modules[index] = replace(
                self.modules[index], mode=Mode.STUB, reason=reason
            )
        else:
            raise ValueError(f"unknown plan item kind {kind!r}")


@dataclass(frozen=True)
class PlannerOptions:
    """How much prose a run buys, before any budget is applied."""

    coverage: float = 0.2            # share of files given prose beyond the floor
    min_prose_files: int = 25        # projects this small: prose wherever needed
    max_prose_files: int | None = None
    module_min_files: int = 2        # a directory needs this many to get prose
    max_prose_modules: int | None = None
    architecture: bool = True
    include_documented: bool = False # also give prose to fully documented files


class Planner:
    """Decides PROSE or STUB for every file, every directory and the overview.

    Pure and deterministic: the same inputs always give the same plan, so the
    `estimate` command and the real run agree on exactly what is paid for.
    """

    def __init__(self, options: PlannerOptions | None = None) -> None:
        self.options = options or PlannerOptions()

    def plan(
        self,
        files: Sequence[SourceFile],
        facts: Mapping[str, FileFacts],
        graph: ProjectGraph,
        history: Mapping[str, FileHistory],
    ) -> Plan:
        """Plan the documentable files among `files`; the rest are left out.

        `facts` and `history` are keyed by relpath. A file with no facts has
        no parser for its language and gets a stub, as does a failed parse.
        """
        candidates: dict[str, SourceFile] = {}
        for file in files:
            if file.is_documentable:
                candidates.setdefault(file.relpath, file)
        planned = self._files(candidates, facts, graph, history)
        modules = self._modules(planned, graph)
        architecture, reason = self._architecture(len(modules), len(candidates))
        return Plan(planned, modules, architecture, reason)

    def _files(
        self,
        candidates: Mapping[str, SourceFile],
        facts: Mapping[str, FileFacts],
        graph: ProjectGraph,
        history: Mapping[str, FileHistory],
    ) -> list[FilePlan]:
        """One FilePlan per candidate, ranked by value, score, then relpath.

        With `include_documented`, a fully documented file is ranked at the
        floor need instead of being stubbed, and its reason says so.
        """
        options = self.options
        settled: list[FilePlan] = []
        ranked: list[FilePlan] = []
        included: set[str] = set()
        for relpath, file in candidates.items():
            known = facts.get(relpath)
            signals = signals_for(
                file, known, graph.file(relpath), history.get(relpath)
            )
            significance = score(signals)
            if known is None:
                reason = f"no parser for {file.language}"
                settled.append(FilePlan(relpath, significance, 0.0, Mode.STUB, reason))
                continue
            if known.confidence is Confidence.FAILED:
                settled.append(
                    FilePlan(relpath, significance, 0.0, Mode.STUB, "parse failed")
                )
                continue
            wanted = need(known)
            if wanted == 0.0:
                if not options.include_documented:
                    reason = "fully documented in source"
                    settled.append(
                        FilePlan(relpath, significance, 0.0, Mode.STUB, reason)
                    )
                    continue
                wanted = _NEED_FLOOR
                included.add(relpath)
            ranked.append(
                FilePlan(relpath, significance, wanted, Mode.STUB, "", _describe(known))
            )

        ranked.sort(key=_file_order)
        total = len(ranked)
        cut = _cut(
            total, options.min_prose_files, options.coverage, options.max_prose_files
        )
        for index, item in enumerate(ranked):
            if index < cut:
                mode, reason = Mode.PROSE, f"ranked {index + 1:,} of {total:,}"
            else:
                mode, reason = Mode.STUB, "below coverage cut"
            if item.relpath in included:
                reason += _INCLUDED
            ranked[index] = replace(item, mode=mode, reason=reason)
        return sorted(settled + ranked, key=_file_order)

    def _modules(
        self, planned: Sequence[FilePlan], graph: ProjectGraph
    ) -> list[ModulePlan]:
        """One ModulePlan per directory holding a candidate, in one ranking.

        A file belongs to its graph node's module, or to its directory when
        the graph has no node for it.
        """
        scores = {item.relpath: item.score for item in planned}
        groups: dict[str, list[str]] = {}
        for relpath in scores:
            node = graph.file(relpath)
            path = node.module if node is not None else posixpath.dirname(relpath)
            groups.setdefault(path, []).append(relpath)

        modules = sorted(
            (
                ModulePlan(
                    path,
                    tuple(sorted(members)),
                    _module_score(members, scores, graph.modules.get(path)),
                    Mode.STUB,
                    "",
                )
                for path, members in groups.items()
            ),
            key=lambda module: (-module.score, module.path),
        )
        minimum = self.options.module_min_files
        eligible = [module for module in modules if len(module.files) >= minimum]
        rank = {module.path: index for index, module in enumerate(eligible)}
        cut = _cut(
            len(rank), _MODULE_FLOOR, _MODULE_SHARE, self.options.max_prose_modules
        )

        result: list[ModulePlan] = []
        for module in modules:
            index = rank.get(module.path)
            if index is None:
                count = len(module.files)
                reason = "single file" if count == 1 else f"fewer than {minimum} files"
                result.append(replace(module, reason=reason))
            elif index < cut:
                reason = f"ranked {index + 1:,} of {len(rank):,}"
                result.append(replace(module, mode=Mode.PROSE, reason=reason))
            else:
                result.append(replace(module, reason="below module cut"))
        return result

    def _architecture(self, modules: int, files: int) -> tuple[Mode, str]:
        """An overview is worth writing once there is structure to describe."""
        if not self.options.architecture:
            return Mode.STUB, "disabled"
        if modules >= 2 or files >= 5:
            return Mode.PROSE, "project overview"
        return Mode.STUB, "project too small"


@dataclass(frozen=True)
class Fit:
    """What `fit_to_budget` did to a plan."""

    cut: dict[tuple[str, str], str]           # in walk order: the limit that cut it
    rebuilt: frozenset[tuple[str, str]]       # cached overviews the run asks again


def fit_to_budget(
    plan: Plan,
    costs: Mapping[tuple[str, str], float],
    *,
    max_cost: float = 0.0,
    max_requests: int = 0,
    cached: Collection[tuple[str, str]] = frozenset(),
) -> Fit:
    """Demote PROSE items until the plan fits the limits.

    `costs` maps (kind, key) — ("file", relpath), ("module", dir) or
    ("architecture", "") — to the estimated USD of that item's call. A limit
    of 0 is no limit. Items are kept greedily in priority order: the
    architecture overview, then modules by score, then files by value. An
    item missing from `costs` is free but still counts as a request, and a cut
    item does not stop the walk: a later, cheaper one may still fit. Each cut
    is named by the limit its calls would pass, "max_requests" if both, as
    `Budget` checks that first.

    A `cached` item makes no call, so it is free and never cut. But an
    overview's prompt carries the purposes it sums up, a module's of its files
    and the architecture's of its modules: buying one rebuilds the overview,
    which the run then asks again. So an item is kept only if its call fits
    with those of the cached overviews it rebuilds, which become calls at
    their `costs`; else it is cut and they stay free: an overview is walked
    before what it sums up, and is never cut for it. Every file of a module
    and every module count as summed up, which may over-count but never
    under-counts. So a module under a cached architecture takes two requests
    and a file under a cut module one: with one request, no module is bought
    once the architecture is cached.
    """
    walk: list[tuple[str, str]] = []
    above: dict[tuple[str, str], tuple[str, str]] = {}   # the overview summing it up
    overview = ("architecture", "")
    if plan.architecture is Mode.PROSE:
        walk.append(overview)
    for module in plan.prose_modules():
        node = ("module", module.path)
        walk.append(node)
        if plan.architecture is Mode.PROSE:
            above[node] = overview
        for path in module.files:
            above[("file", path)] = node
    walk.extend(("file", item.relpath) for item in plan.prose_files())

    spent = 0.0
    requests = 0
    cut: dict[tuple[str, str], str] = {}
    rebuilt: set[tuple[str, str]] = set()
    for item in walk:
        if item in cached:
            continue
        calls = [item]
        upper = above.get(item)
        while upper is not None and upper in cached and upper not in rebuilt:
            calls.append(upper)
            upper = above.get(upper)
        cost = sum(costs.get(call, 0.0) for call in calls)
        if max_requests and requests + len(calls) > max_requests:
            cut[item] = "max_requests"
        elif max_cost and spent + cost > max_cost + _BUDGET_SLACK:
            cut[item] = "max_cost"
        else:
            spent += cost
            requests += len(calls)
            rebuilt.update(calls[1:])
    _demote_all(plan, cut, "over budget")
    return Fit(cut, frozenset(rebuilt))


def _round(value: float) -> float:
    return round(value, 4) + 0.0   # + 0.0 turns -0.0 into 0.0


def _log1p(count: float) -> float:
    return math.log1p(max(count, 0.0))   # a negative count reads as none


def _is_api_member(child: Symbol) -> bool:
    """Public by flag and by name; of the dunders, only __init__ and __call__."""
    return child.is_public and (
        not child.name.startswith("_") or child.name in _API_DUNDERS
    )


def _api(facts: FileFacts) -> list[tuple[str, Symbol]]:
    """The file's API as (qualname, symbol) pairs.

    The public symbols, each class or interface among them followed by its
    public members as `Class.member`. One level only: a member's own members
    are not API. Every definition is an entry, so a bare property setter or
    overload stub counts even when its namesake is documented.
    """
    api: list[tuple[str, Symbol]] = []
    for symbol in facts.public_symbols:
        api.append((symbol.name, symbol))
        if symbol.kind in (SymbolKind.CLASS, SymbolKind.INTERFACE):
            api.extend(
                (f"{symbol.name}.{child.name}", child)
                for child in symbol.children
                if _is_api_member(child)
            )
    return api


def _describe(facts: FileFacts) -> tuple[str, ...]:
    """Undocumented API qualnames in source order, each once, at most 25."""
    missing = sorted(
        (symbol.line, name)
        for name, symbol in _api(facts)
        if not symbol.docstring.strip()
    )
    names = dict.fromkeys(name for _, name in missing)   # overloads share a name
    return tuple(names)[:_DESCRIBE_LIMIT]


def _file_order(item: FilePlan) -> tuple[float, float, str]:
    """Value desc, then score desc, then relpath asc.

    Value is compared at 8 decimals, where the product of a 4-decimal score
    and a 4-decimal need is exact, so values equal on paper tie and fall
    through to score instead of splitting on float noise (3.0 * 0.3 < 0.9).
    """
    return (-round(item.value, 8), -item.score, item.relpath)


def _cut(count: int, floor: int, share: float, cap: int | None) -> int:
    """How many of `count` ranked items get prose.

    All of them up to `floor`; beyond it `share` of them, never fewer than
    `floor`; never more than `cap` when one is set.
    """
    if count <= floor:
        chosen = count
    else:
        fraction = min(share, 1.0) if share >= 0.0 else 0.0   # NaN reads as 0
        exact = round(fraction * count, 9)   # 0.07 * 100 is 7.000000000000001
        chosen = max(floor, math.ceil(exact))
    if cap is not None:
        chosen = min(chosen, cap)
    return max(0, min(chosen, count))


def _module_score(
    members: Sequence[str], scores: Mapping[str, float], node: ModuleNode | None
) -> float:
    """Sum of a directory's best file scores, plus one per module importing it."""
    best = heapq.nlargest(
        _MODULE_TOP_FILES, (max(scores[relpath], 0.0) for relpath in members)
    )
    used_by = len(node.used_by) if node is not None else 0
    return _round(sum(best) + 1.0 * used_by)


def _demote_all(plan: Plan, items: Iterable[tuple[str, str]], reason: str) -> None:
    """`Plan.demote` for many items in one pass over each list.

    Demoting one at a time rescans the plan per item, which turns cutting a
    20,000-file plan down to budget into seconds of quadratic work.
    """
    chosen = set(items)
    if ("architecture", "") in chosen:
        plan.demote("architecture", "", reason)
    for index, module in enumerate(plan.modules):
        if module.mode is Mode.PROSE and ("module", module.path) in chosen:
            plan.modules[index] = replace(module, mode=Mode.STUB, reason=reason)
    for index, item in enumerate(plan.files):
        if item.mode is Mode.PROSE and ("file", item.relpath) in chosen:
            plan.files[index] = replace(item, mode=Mode.STUB, reason=reason)
