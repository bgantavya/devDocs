"""How a project's files depend on each other, resolved from their imports.

Fan-in — how many project files import a file — is the strongest single
signal of what is worth documenting, and the module graph built from the same
edges is what the architecture document draws. Both come from resolving each
file's import strings to project files, per language, with no model involved.

Resolution is conservative: an import that cannot be resolved is treated as
external. That can under-count fan-in but never invents an edge, so every
dependency the docs show exists in the code. The exception is an import that
can only mean a project file (a relative import, a path under the project's
own Go module, a `crate::` path): unresolved, it is dropped rather than shown
as an outside package, which would be just as wrong.

Each language resolves the way its own toolchain does, from the files that
toolchain reads: package roots and pyproject scripts for Python, tsconfig
`paths` and package.json workspaces for JS/TS, go.mod module paths, Cargo
crates and the `mod` tree, Java packages. Every index is built once per run,
so resolving one import is a few dict lookups and a 20,000-file project
resolves in seconds.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devdocs.parse import FileFacts, Symbol, SymbolKind
from devdocs.parse.languages import GO, JAVA, RUST
from devdocs.parse.masking import mask
from devdocs.scan import Role, SourceFile

__all__ = [
    "FileNode",
    "GraphBuilder",
    "ModuleNode",
    "ProjectGraph",
    "is_stdlib",
]


@dataclass(frozen=True)
class FileNode:
    """One file's place in the graph. All paths are POSIX relpaths."""

    relpath: str
    language: str
    module: str                          # the file's directory; "" for the root
    internal: tuple[str, ...] = ()       # project files it imports
    external: tuple[str, ...] = ()       # outside packages it imports
    dependents: tuple[str, ...] = ()     # non-test project files importing it
    tested_by: tuple[str, ...] = ()      # test files importing it
    entry_point: str = ""                # why it is an entry point, or ""

    @property
    def fan_in(self) -> int:
        return len(self.dependents)


@dataclass(frozen=True)
class ModuleNode:
    """A directory holding graph files, and its links to other directories."""

    path: str                            # POSIX directory; "" for the root
    files: tuple[str, ...] = ()          # files directly inside, sorted
    depends_on: tuple[str, ...] = ()     # modules its files import from
    used_by: tuple[str, ...] = ()        # modules importing its files


@dataclass
class ProjectGraph:
    files: dict[str, FileNode] = field(default_factory=dict)
    modules: dict[str, ModuleNode] = field(default_factory=dict)

    def file(self, relpath: str) -> FileNode | None:
        return self.files.get(relpath)

    def module_edges(self) -> dict[tuple[str, str], int]:
        """(importing module, imported module) -> number of file-level edges."""
        counts: dict[tuple[str, str], int] = {}
        for node in self.files.values():
            for target in node.internal:
                imported = self.files.get(target)
                if imported is None or imported.module == node.module:
                    continue
                key = (node.module, imported.module)
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def entry_points(self) -> list[FileNode]:
        """Files with an `entry_point` reason, sorted by relpath."""
        return sorted(
            (node for node in self.files.values() if node.entry_point),
            key=lambda node: node.relpath,
        )

    def components(self, max_nodes: int = 12) -> dict[str, str]:
        """Map each module to the coarser component it is drawn as.

        Diagrams stay readable by collapsing modules into their ancestors at
        the deepest level that yields at most `max_nodes` components, after
        stripping a prefix every module shares (such as `src/pkg`).
        """
        paths = sorted(self.modules)
        if not paths:
            return {}
        split = {path: path.split("/") if path else [] for path in paths}
        shared = _common_prefix(list(split.values()))
        deepest = max(len(parts) for parts in split.values())

        best: dict[str, str] = {}
        for depth in range(1, max(deepest - len(shared), 1) + 1):
            keep = len(shared) + depth
            mapping = {path: "/".join(parts[:keep]) for path, parts in split.items()}
            if best and len(set(mapping.values())) > max_nodes:
                break
            best = mapping
        return best


def _common_prefix(paths: list[list[str]]) -> list[str]:
    """Directory segments shared by every path, excluding the last level."""
    if not paths:
        return []
    shortest = min(paths, key=len)
    prefix: list[str] = []
    for index, segment in enumerate(shortest):
        if all(path[index] == segment for path in paths):
            prefix.append(segment)
        else:
            break
    # Never swallow a whole module: one must stay distinguishable from another.
    if any(len(path) == len(prefix) for path in paths) and len(paths) > 1:
        prefix = prefix[:-1] if prefix else prefix
    return prefix


_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net", "os",
    "path", "perf_hooks", "process", "punycode", "querystring", "readline",
    "repl", "stream", "string_decoder", "sys", "test", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib",
})
_RUST_STD = frozenset({"std", "core", "alloc", "proc_macro", "test"})
_JAVA_STD = ("java.", "javax.", "jdk.", "sun.", "com.sun.")


def is_stdlib(language: str, name: str) -> bool:
    """True when an external import name belongs to the language's stdlib."""
    if language == "python":
        return name.split(".", 1)[0] in sys.stdlib_module_names
    if language in ("javascript", "typescript"):
        bare = name.removeprefix("node:").split("/", 1)[0]
        return name.startswith("node:") or bare in _NODE_BUILTINS
    if language == "go":
        return "." not in name.split("/", 1)[0]
    if language == "rust":
        return name.split("::", 1)[0] in _RUST_STD
    if language == "java":
        return name.startswith(_JAVA_STD)
    return False


class GraphBuilder:
    """Resolves imports for every file with a known language.

    `sources` maps relpath to file text, already read by the pipeline; it is
    used for facts the parsers do not record (Java packages and same-package
    references, Rust `mod` declarations and `use super` scopes, Python
    `__main__` guards, Go's `package main`). Config files such as
    pyproject.toml, package.json, tsconfig.json, go.mod and Cargo.toml are
    read from `root` directly.
    """

    def __init__(
        self,
        root: Path,
        files: Sequence[SourceFile],
        facts: Mapping[str, FileFacts],
        sources: Mapping[str, str],
    ) -> None:
        self.root = root
        self.files = files
        self.facts = facts
        self.sources = sources

    def build(self) -> ProjectGraph:
        """One node per file with a language, its imports resolved per language.

        Never raises on project content: malformed configs are skipped, and
        a file missing from `sources` or `facts` reads as empty.
        """
        nodes: dict[str, SourceFile] = {}
        for file in sorted(self.files, key=lambda item: item.relpath):
            if file.language and file.relpath not in nodes:
                nodes[file.relpath] = file
        project = _Project(self.root, nodes, self.files, self.facts, self.sources)
        families = {_FAMILIES.get(file.language, _Resolver) for file in nodes.values()}
        resolvers = {
            family: family(project)
            for family in sorted(families, key=lambda family: family.__name__)
        }

        internal: dict[str, set[str]] = {}
        external: dict[str, set[str]] = {}
        for relpath, file in nodes.items():
            rules = resolvers[_FAMILIES.get(file.language, _Resolver)]
            targets = set(rules.implicit(relpath))
            outside: set[str] = set()
            for spec in rules.imports(relpath):
                found = rules.resolve(relpath, spec)
                targets.update(found.internal)
                if found.external:
                    outside.add(found.external)
            targets.discard(relpath)
            internal[relpath] = targets
            external[relpath] = outside

        entries: dict[str, str] = {}
        for rules in resolvers.values():
            for relpath, reason in rules.entry_points().items():
                if nodes[relpath].role not in _NEVER_ENTRIES:
                    entries.setdefault(relpath, reason)
        return _assemble(nodes, internal, external, entries)


# -- shared machinery ------------------------------------------------------------

# Tests are run by a runner and vendored code is someone else's: neither is
# where a reader starts, even with a `__main__` guard.
_NEVER_ENTRIES = frozenset({Role.TEST, Role.VENDORED})
_CONFIG_LIMIT = 2_000_000    # bytes; a bigger "config" is not one worth parsing

_JSON_STRING = r'"(?:[^"\\\n]|\\.)*"'
_JSONC_COMMENTS = re.compile(rf"({_JSON_STRING})|//[^\n]*|/\*.*?\*/", re.S)
_JSONC_COMMAS = re.compile(rf"({_JSON_STRING})|,(?=\s*[}}\]])")


@dataclass(frozen=True)
class _Resolved:
    """Where one import leads: project files, an outside package, or neither
    (a project-relative import naming nothing the scan saw)."""

    internal: tuple[str, ...] = ()
    external: str = ""


_NOTHING = _Resolved()


def _read_text(path: Path) -> str | None:
    """A config file's text, or None when it is missing, odd or unreadable."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            data = handle.read(_CONFIG_LIMIT + 1)
    except (OSError, ValueError):
        return None
    if len(data) > _CONFIG_LIMIT:
        return None
    return data.decode("utf-8-sig", errors="replace")


def _json_object(text: str | None, *, jsonc: bool = False) -> dict[str, Any] | None:
    """The top-level object of a JSON document, or None.

    With `jsonc`, `//` and `/* */` comments and trailing commas are allowed,
    as in tsconfig.json; strings are matched first so `"@/*"` survives.
    """
    if text is None:
        return None
    if jsonc:
        text = _JSONC_COMMENTS.sub(lambda match: match.group(1) or " ", text)
        text = _JSONC_COMMAS.sub(lambda match: match.group(1) or "", text)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _toml(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except (ValueError, RecursionError):
        return None


def _table(data: Mapping[str, Any] | None, *keys: str) -> dict[str, Any]:
    """The nested table at `keys`, or {} when a level is missing or not a table."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _shared_dirs(left: str, right: str) -> int:
    """How many leading directories two relpaths have in common."""
    count = 0
    for mine, theirs in zip(
        left.split("/")[:-1], right.split("/")[:-1], strict=False
    ):
        if mine != theirs:
            break
        count += 1
    return count


def _closest(candidates: Iterable[str], importer: str) -> str:
    """The candidate nearest `importer` by shared directories, then the first
    in sorted order; never the importer itself; "" when none is left."""
    options = [candidate for candidate in candidates if candidate != importer]
    if len(options) < 2:
        return options[0] if options else ""
    return min(options, key=lambda option: (-_shared_dirs(option, importer), option))


class _Project:
    """What every resolver reads: nodes, their facts and text, and the disk."""

    def __init__(
        self,
        root: Path,
        nodes: dict[str, SourceFile],
        scanned: Sequence[SourceFile],
        facts: Mapping[str, FileFacts],
        sources: Mapping[str, str],
    ) -> None:
        self.root = root
        self.nodes = nodes
        self.paths = frozenset(nodes)
        self.facts = facts
        self.sources = sources
        self._named: dict[str, set[str]] = {}
        for file in scanned:
            self._named.setdefault(posixpath.basename(file.relpath), set()).add(
                file.relpath
            )
        self._exists: dict[str, bool] = {}
        self._texts: dict[str, str | None] = {}

    def of(self, *languages: str) -> list[str]:
        """Node relpaths in one of `languages`, sorted."""
        return [path for path, file in self.nodes.items() if file.language in languages]

    def source(self, relpath: str) -> str:
        text = self.sources.get(relpath)
        return text if isinstance(text, str) else ""

    def imports(self, relpath: str) -> list[str]:
        facts = self.facts.get(relpath)
        if facts is None:
            return []
        return [
            spec.strip()
            for spec in facts.imports
            if isinstance(spec, str) and spec.strip()
        ]

    def exists(self, relpath: str) -> bool:
        """True when `relpath` is a regular file on disk, scanned or not."""
        known = self._exists.get(relpath)
        if known is None:
            known = os.path.isfile(self.root / relpath)
            self._exists[relpath] = known
        return known

    def read(self, relpath: str) -> str | None:
        if relpath not in self._texts:
            self._texts[relpath] = _read_text(self.root / relpath)
        return self._texts[relpath]

    def configs(self, name: str, near: Iterable[str]) -> list[str]:
        """Relpaths of files called `name`, sorted: those the scan saw, plus
        any on disk at the root or in a directory holding or above `near`."""
        found = set(self._named.get(name, ()))
        directories = {""}
        for relpath in near:
            directory = posixpath.dirname(relpath)
            while directory not in directories:
                directories.add(directory)
                directory = posixpath.dirname(directory)
        for directory in directories:
            candidate = posixpath.join(directory, name)
            if candidate not in found and self.exists(candidate):
                found.add(candidate)
        return sorted(found)


class _Resolver:
    """One language family's rules, its indexes built once in `__init__`.

    The base serves languages with no rules of their own: every import is
    kept verbatim as external, except one that looks relative.
    """

    def __init__(self, project: _Project) -> None:
        self.project = project

    def imports(self, importer: str) -> list[str]:
        return self.project.imports(importer)

    def resolve(self, importer: str, spec: str) -> _Resolved:
        return _NOTHING if spec.startswith(".") else _Resolved(external=spec)

    def implicit(self, importer: str) -> list[str]:
        """Edges the language makes without an import statement."""
        return []

    def entry_points(self) -> dict[str, str]:
        """Relpath -> reason, first applicable reason per file."""
        return {}


# -- Python ----------------------------------------------------------------------

_PY_ROOTS = ("", "src", "lib", "python")
_MAIN_GUARD = re.compile(
    r"^if\s*\(?\s*(?:__name__\s*==\s*(['\"])__main__\1|(['\"])__main__\2\s*==\s*"
    r"__name__)\s*\)?\s*:",
    re.M,
)


def _dotted(path: str) -> str:
    """`a/b/c.py` -> `a.b.c` and `a/__init__.py` -> `a`; "" unless every part
    is an identifier, since nothing else can be imported."""
    parts = path.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return ""
    return ".".join(parts)


def _script_module(value: object) -> str:
    """The module of a console script: `pkg.mod:func [extra]` -> `pkg.mod`.

    Poetry's table form names it under `callable` or, for console scripts,
    `reference`.
    """
    if isinstance(value, dict):
        kind = value.get("type", "console")
        value = value.get("callable") or (
            value.get("reference") if kind == "console" else None
        )
    if not isinstance(value, str):
        return ""
    module = value.split(":", 1)[0].split("[", 1)[0].strip()
    return module if all(part.isidentifier() for part in module.split(".")) else ""


class _Python(_Resolver):
    """Python's import system, from the roots a checkout is usually run from.

    Each file is indexed under its dotted name relative to the repo root,
    src/, lib/, python/ and the directory above its topmost package, so flat,
    src and nested-project layouts all resolve without configuration. A file
    in no package anywhere else (scripts/, tests/) is importable by its bare
    name only from its own directory and below, as `sys.path[0]` allows, so
    `tools/logging.py` never captures the rest of the project's `logging`.
    """

    def __init__(self, project: _Project) -> None:
        super().__init__(project)
        self.files = [
            path for path in project.of("python") if path.endswith((".py", ".pyi"))
        ]
        self.packages: set[str] = set()      # top-level package names
        self._package_dirs: dict[str, bool] = {}
        names: dict[str, set[str]] = {}
        # bare name -> directory -> files outside any package
        self.loose: dict[str, dict[str, list[str]]] = {}
        for relpath in self.files:
            for name in self._names(relpath):
                names.setdefault(name, set()).add(relpath)
        self.index = {name: sorted(found) for name, found in names.items()}

    def _is_package(self, directory: str) -> bool:
        """True when `directory` has an `__init__.py`, even an empty one the
        scanner skipped."""
        known = self._package_dirs.get(directory)
        if known is None:
            init = posixpath.join(directory, "__init__.py")
            paths = self.project.paths
            known = init in paths or init + "i" in paths or self.project.exists(init)
            self._package_dirs[directory] = known
        return known

    def _names(self, relpath: str) -> set[str]:
        """The dotted names `relpath` is indexed under; a file in no package
        outside the fixed roots is also filed in `loose`."""
        directory = posixpath.dirname(relpath)
        top = directory
        while top and self._is_package(top):
            top = posixpath.dirname(top)
        roots = set(_PY_ROOTS)
        if top != directory:
            inside = directory[len(top) + 1 :] if top else directory
            self.packages.add(inside.split("/", 1)[0])
            roots.add(top)
        elif directory not in roots:
            name = _dotted(posixpath.basename(relpath))
            if name:
                self.loose.setdefault(name, {}).setdefault(directory, []).append(
                    relpath
                )
        names: set[str] = set()
        for base in roots:
            if base and not relpath.startswith(base + "/"):
                continue
            name = _dotted(relpath[len(base) + 1 :] if base else relpath)
            if name:
                names.add(name)
        return names

    def _module(self, name: str, importer: str) -> str | None:
        """The file a dotted name means to `importer`: a loose module in its
        directory or one above, as `sys.path[0]` finds it, else the nearest
        indexed one; "" when none is, None when it is the importer itself."""
        found: Sequence[str] = self.index.get(name, ())
        local = self.loose.get(name)
        if local is not None:
            directory = posixpath.dirname(importer)
            while directory not in local and directory:
                directory = posixpath.dirname(directory)
            found = local.get(directory, found)
        if not found:
            return ""
        return _closest(found, importer) or None

    def resolve(self, importer: str, spec: str) -> _Resolved:
        if spec.startswith("."):
            return self._relative(importer, spec)
        parts = spec.split(".")
        for size in range(len(parts), 0, -1):
            target = self._module(".".join(parts[:size]), importer)
            if target is None:
                return _NOTHING   # the importer naming itself
            if target:
                return _Resolved((target,))
        if parts[0] in self.packages:
            return _NOTHING   # the project's own package, its __init__ unscanned
        return _Resolved(external=parts[0])

    def _relative(self, importer: str, spec: str) -> _Resolved:
        """`.m.x` from the importer's package: the longest prefix naming a
        module, down to the package's own `__init__` (`from . import name`)."""
        rest = spec.lstrip(".")
        base = posixpath.dirname(importer)
        for _ in range(len(spec) - len(rest) - 1):
            if not base:
                return _NOTHING   # climbs above the project root
            base = posixpath.dirname(base)
        paths = self.project.paths
        parts = rest.split(".") if rest else []
        for size in range(len(parts), -1, -1):
            stem = posixpath.join(base, *parts[:size])
            candidates = (
                (stem + ".py", stem + ".pyi", stem + "/__init__.py")
                if size
                else (
                    posixpath.join(base, "__init__.py"),
                    posixpath.join(base, "__init__.pyi"),
                )
            )
            for candidate in candidates:
                if candidate in paths and candidate != importer:
                    return _Resolved((candidate,))
        return _NOTHING

    def entry_points(self) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for relpath in self.files:
            if posixpath.basename(relpath) == "__main__.py":
                reasons[relpath] = "runs with python -m"
        for manifest, script, module in self._scripts():
            target = _closest(self.index.get(module, ()), manifest)
            if target:
                reasons.setdefault(target, f"console script `{script}`")
        for relpath in self.files:
            if relpath not in reasons and _MAIN_GUARD.search(
                self.project.source(relpath)
            ):
                reasons[relpath] = "runs as a script"
        return reasons

    def _scripts(self) -> list[tuple[str, str, str]]:
        """(pyproject.toml relpath, script name, module) for every script."""
        found: list[tuple[str, str, str]] = []
        for manifest in self.project.configs("pyproject.toml", self.files):
            data = _toml(self.project.read(manifest))
            for table in (
                _table(data, "project", "scripts"),
                _table(data, "project", "gui-scripts"),
                _table(data, "tool", "poetry", "scripts"),
            ):
                for script in sorted(table):
                    module = _script_module(table[script])
                    if module:
                        found.append((manifest, script, module))
        return found


# -- JavaScript / TypeScript -----------------------------------------------------

_SCRIPT_LANGUAGES = ("javascript", "typescript", "vue", "svelte")
_SCRIPT_EXTENSIONS = (
    ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".vue", ".svelte",
)
# TS ESM convention: `./util.js` in source names `util.ts`.
_TS_TWINS = {
    ".js": (".ts", ".tsx", ".d.ts"), ".jsx": (".tsx",), ".mjs": (".mts",),
    ".cjs": (".cts",),
}
# `.`, `..` and a trailing slash name a directory: Node and tsc load its index.
_DIRECTORY_SPEC = re.compile(r"(?:^|/)\.{0,2}$")
_ENTRY_FIELDS = ("source", "module", "main", "types")
_ROUTE_EXTENSIONS = (".tsx", ".jsx", ".ts", ".js")
_SPECIAL_PAGES = frozenset({"_app", "_document", "_error"})


@dataclass(frozen=True, eq=False)
class _TsConfig:
    """What resolution uses from one tsconfig.json or jsconfig.json."""

    directory: str
    base: str | None                                     # baseUrl, as a relpath
    exact: Mapping[str, tuple[str, ...]]                 # `paths` without a `*`
    wildcards: tuple[tuple[str, str, tuple[str, ...]], ...]   # longest prefix first

    @property
    def anchor(self) -> str:
        """The directory `paths` targets are relative to."""
        return self.base if self.base is not None else self.directory


def _tsconfig(directory: str, text: str | None) -> _TsConfig | None:
    data = _json_object(text, jsonc=True)
    if data is None:
        return None
    options = _table(data, "compilerOptions")
    base_url = options.get("baseUrl")
    base = None
    if isinstance(base_url, str):
        base = posixpath.normpath(posixpath.join(directory, base_url))
        base = "" if base == "." else base
    exact: dict[str, tuple[str, ...]] = {}
    wildcards: list[tuple[str, str, tuple[str, ...]]] = []
    for pattern, values in _table(options, "paths").items():
        if not isinstance(values, list):
            continue
        targets = tuple(
            value for value in values if isinstance(value, str) and value.count("*") < 2
        )
        if "*" not in pattern:
            exact[pattern] = targets
        elif pattern.count("*") == 1:
            prefix, _, suffix = pattern.partition("*")
            wildcards.append((prefix, suffix, targets))
    wildcards.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
    return _TsConfig(directory, base, exact, tuple(wildcards))


def _npm_name(spec: str) -> str:
    """The package a bare specifier names: `@scope/pkg/x` -> `@scope/pkg`,
    `lodash/fp` -> `lodash`, `node:fs/promises` -> `node:fs`; URLs stay whole."""
    if "://" in spec:
        return spec
    scheme, colon, rest = spec.partition(":")
    if colon and scheme.isalpha() and rest:
        return f"{scheme}:{_npm_name(rest)}"
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def _exports(value: object) -> list[str]:
    """The targets package.json `exports` gives the package root (".")."""
    if isinstance(value, dict):
        if "." in value:
            value = value["."]
        elif any(str(key).startswith(".") for key in value):
            return []    # subpath exports only
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            value[key]
            for key in ("import", "require", "default")
            if isinstance(value.get(key), str)
        ]
    return []


class _Script(_Resolver):
    """Node and TypeScript resolution: relative paths probed for extensions and
    index files, tsconfig `paths`/`baseUrl`, and workspace packages."""

    def __init__(self, project: _Project) -> None:
        super().__init__(project)
        self.files = project.of(*_SCRIPT_LANGUAGES)
        ranked: dict[str, tuple[int, str]] = {}
        for relpath in self.files:
            for rank, extension in enumerate(_SCRIPT_EXTENSIONS):
                if relpath.endswith(extension):
                    stem = relpath[: -len(extension)]
                    entry = (rank, relpath)
                    ranked[stem] = min(ranked.get(stem, entry), entry)
        self._stems = {stem: relpath for stem, (_, relpath) in ranked.items()}

        self.manifests: dict[str, dict[str, Any]] = {}     # directory -> package.json
        for manifest in project.configs("package.json", self.files):
            data = _json_object(project.read(manifest))
            if data is not None:
                self.manifests[posixpath.dirname(manifest)] = data
        self.packages: dict[str, str] = {}    # workspace package name -> directory
        for directory, data in self.manifests.items():
            name = data.get("name")
            if directory and isinstance(name, str) and name:
                self.packages.setdefault(name, directory)

        self.configs: dict[str, _TsConfig] = {}
        for name in ("jsconfig.json", "tsconfig.json"):    # tsconfig wins a tie
            for path in project.configs(name, self.files):
                config = _tsconfig(posixpath.dirname(path), project.read(path))
                if config is not None:
                    self.configs[config.directory] = config
        self._nearest: dict[str, _TsConfig | None] = {}

    def _file(self, path: str, *, prefer_ts: bool = False) -> str:
        """The project file an import path names, probing as bundlers do:
        exact, TS twin, added extension, then `index` inside a directory."""
        path = posixpath.normpath(path) if path else "."
        if path == ".":
            path = ""
        elif path == ".." or path.startswith(("../", "/")):
            return ""
        stem, extension = posixpath.splitext(path)
        twins = [stem + twin for twin in _TS_TWINS.get(extension, ())]
        for candidate in [*twins, path] if prefer_ts else [path, *twins]:
            if candidate in self.project.paths:
                return candidate
        hit = self._stems.get(path, "") if path else ""
        return hit or self._stems.get(posixpath.join(path, "index"), "")

    def resolve(self, importer: str, spec: str) -> _Resolved:
        spec = spec.split("?", 1)[0]
        if not spec or spec.startswith("#"):
            return _NOTHING     # `#x` is a package.json subpath import: ours
        prefer_ts = self.project.nodes[importer].language == "typescript"
        if spec.startswith((".", "/")):
            start = posixpath.dirname(importer) if spec.startswith(".") else ""
            path = posixpath.join(start, spec.lstrip("/"))
            if _DIRECTORY_SPEC.search(spec):
                path = posixpath.join(path, "index")
            target = self._file(path, prefer_ts=prefer_ts)
            return _Resolved((target,)) if target else _NOTHING

        aliased = False
        config = self._config(importer)
        if config is not None:
            target, aliased = self._alias(config, spec, prefer_ts)
            if not target and config.base is not None:
                path = posixpath.join(config.base, spec)
                target = self._file(path, prefer_ts=prefer_ts)
            if target:
                return _Resolved((target,))
        package = _npm_name(spec)
        directory = self.packages.get(package)
        if directory is not None:
            inner = spec[len(package) :].lstrip("/")
            target = (
                self._file(posixpath.join(directory, inner), prefer_ts=prefer_ts)
                if inner
                else self._entry(directory)
            )
            return _Resolved((target,)) if target else _NOTHING
        if aliased or spec.startswith(("@/", "~")):
            return _NOTHING     # an alias, never a valid npm package name
        return _Resolved(external=package)

    def _config(self, importer: str) -> _TsConfig | None:
        """The nearest tsconfig/jsconfig at or above the importer's directory."""
        directory = posixpath.dirname(importer)
        missed: list[str] = []
        found: _TsConfig | None = None
        while True:
            if directory in self._nearest:
                found = self._nearest[directory]
                break
            found = self.configs.get(directory)
            if found is not None:
                break
            missed.append(directory)
            if not directory:
                break
            directory = posixpath.dirname(directory)
        for path in missed:
            self._nearest[path] = found
        return found

    def _alias(self, config: _TsConfig, spec: str, prefer_ts: bool) -> tuple[str, bool]:
        """(file or "", whether a project alias matched) through `paths`.

        Like tsc, an exact pattern beats a wildcard, and among wildcards the
        longest prefix wins. A bare `*` catch-all is not a project alias.
        """
        targets = config.exact.get(spec)
        owned = targets is not None
        star = ""
        if targets is None:
            for prefix, suffix, substitutes in config.wildcards:
                if (
                    len(spec) >= len(prefix) + len(suffix)
                    and spec.startswith(prefix)
                    and spec.endswith(suffix)
                ):
                    targets = substitutes
                    star = spec[len(prefix) : len(spec) - len(suffix)]
                    owned = bool(prefix or suffix)
                    break
        for target in targets or ():
            path = posixpath.join(config.anchor, target.replace("*", star))
            hit = self._file(path, prefer_ts=prefer_ts)
            if hit:
                return hit, True
        return "", owned

    def _entry(self, directory: str) -> str:
        """The file a bare workspace package name loads."""
        data = self.manifests.get(directory, {})
        fields = [data.get(key) for key in _ENTRY_FIELDS]
        for value in [*fields, *_exports(data.get("exports"))]:
            hit = self._from_manifest(directory, value)
            if hit:
                return hit
        for stem in ("src/index", "index"):
            hit = self._stems.get(posixpath.join(directory, stem), "")
            if hit:
                return hit
        return ""

    def _from_manifest(self, directory: str, value: object) -> str:
        """The file a package.json field names, or ""."""
        if not isinstance(value, str):
            return ""
        return self._file(posixpath.join(directory, value))

    def entry_points(self) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for directory, data in self.manifests.items():
            bins = data.get("bin")
            if isinstance(bins, str):
                # npm names a lone bin after the package, without its scope.
                name = data.get("name")
                command = name.rsplit("/", 1)[-1] if isinstance(name, str) else ""
                command = command or posixpath.splitext(posixpath.basename(bins))[0]
                bins = {command: bins}
            if not isinstance(bins, dict):
                continue
            for command in sorted(bins):
                hit = self._from_manifest(directory, bins[command])
                if hit:
                    reasons.setdefault(hit, f"command `{command}`")
        for directory, data in self.manifests.items():
            exports = _exports(data.get("exports"))
            for value in (data.get("main"), data.get("module"), *exports):
                hit = self._from_manifest(directory, value)
                if hit:
                    reasons.setdefault(hit, "package entry")
        for relpath in self.files:
            route = self._route(relpath)
            if route:
                reasons.setdefault(relpath, route)
        return reasons

    def _route(self, relpath: str) -> str:
        """Why a file is a Next.js route, or "": `app/` and `pages/` at the
        nearest package root, optionally inside `src/`."""
        root = posixpath.dirname(relpath)
        while root and root not in self.manifests:
            root = posixpath.dirname(root)
        tail = (relpath[len(root) + 1 :] if root else relpath).removeprefix("src/")
        stem, extension = posixpath.splitext(posixpath.basename(tail))
        if extension not in _ROUTE_EXTENSIONS:
            return ""
        if tail.startswith("app/"):
            if stem == "page":
                return "route page"
            handler = stem == "route" and extension in (".ts", ".js")
            return "route handler" if handler else ""
        if tail.startswith("pages/api/"):
            return "API route"
        if tail.startswith("pages/") and stem not in _SPECIAL_PAGES:
            return "route page"
        return ""


# -- Go --------------------------------------------------------------------------

_GO_MODULE = re.compile(r'^[ \t]*module[ \t]+"?([^\s"]+)"?', re.M)
_GO_MAIN_PACKAGE = re.compile(r"^package\s+main\b", re.M)
_GO_MAIN_FUNC = re.compile(r"^func\s+main\s*\(", re.M)


class _Go(_Resolver):
    """Go packages: an import path under a go.mod module path is a directory,
    and importing it depends on every non-test file there."""

    def __init__(self, project: _Project) -> None:
        super().__init__(project)
        self.files = project.of("go")
        self.packages: dict[str, list[str]] = {}
        for relpath in self.files:
            if not relpath.endswith("_test.go"):
                self.packages.setdefault(posixpath.dirname(relpath), []).append(relpath)
        self.modules: dict[str, str] = {}     # module path -> directory
        manifests = project.configs("go.mod", self.files)
        for manifest in sorted(manifests, key=lambda path: (path.count("/"), path)):
            match = _GO_MODULE.search(project.read(manifest) or "")
            if match:
                self.modules.setdefault(match.group(1), posixpath.dirname(manifest))

    def resolve(self, importer: str, spec: str) -> _Resolved:
        parts = spec.split("/")
        for size in range(len(parts), 0, -1):
            directory = self.modules.get("/".join(parts[:size]))
            if directory is not None:
                package = self.packages.get(posixpath.join(directory, *parts[size:]))
                return _Resolved(
                    tuple(path for path in package or () if path != importer)
                )
        return _Resolved(external="/".join(parts[:3]) if "." in parts[0] else spec)

    def entry_points(self) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for relpath in self.files:
            source = self.project.source(relpath)
            if not (_GO_MAIN_PACKAGE.search(source) and _GO_MAIN_FUNC.search(source)):
                continue     # cheap test first: masking costs a pass over the file
            masked = mask(source, GO.syntax).text
            if _GO_MAIN_PACKAGE.search(masked) and _GO_MAIN_FUNC.search(masked):
                reasons[relpath] = "program entry (`func main`)"
        return reasons


# -- Rust ------------------------------------------------------------------------

_RUST_CHILD = re.compile(r"\bmod\s+(?:r#)?[A-Za-z_]\w*\s*;")
_RUST_INLINE = re.compile(r"\bmod\s+(?:r#)?[A-Za-z_]\w*\s*\{")
_RUST_SUPER = re.compile(r"\buse\s+super\b")
_RUST_SCOPE = re.compile(
    r"\bmod\s+(?:r#)?(?P<mod>[A-Za-z_]\w*)\s*(?P<end>[;{])"
    r"|\buse\s+(?P<supers>(?:super\s*::\s*)+)"
    r"(?P<path>(?:r#)?[A-Za-z_]\w*(?:\s*::\s*(?:r#)?[A-Za-z_]\w*)*)?"
    r"|(?P<brace>[{}])"
)
_RUST_RELATIVE = frozenset({"crate", "$crate", "self", "super"})
_RUST_TARGET_DIRS = ("tests", "examples", "benches")
_ROOT_RANK = {"lib": 0, "bin": 1, "aux": 2}


@dataclass(frozen=True)
class _Crate:
    """One Cargo package: its name as written in paths, and its root files."""

    name: str
    directory: str
    lib: str = ""                         # library root relpath, or ""
    bins: frozenset[str] = frozenset()    # [[bin]] paths
    build: str = ""

    def main(self, paths: frozenset[str]) -> str:
        path = posixpath.join(self.directory, "src/main.rs")
        return path if path in paths else ""


@dataclass(frozen=True)
class _RustScan:
    """What the masked source says that the parser does not record."""

    children: tuple[tuple[str, ...], ...] = ()     # `mod x;`: inline path + x
    # Recorded `use super...` path -> the paths it means once the inline
    # modules around each use are discounted; "" means inside this file.
    supers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _crate(manifest: str, text: str | None, paths: frozenset[str]) -> _Crate | None:
    data = _toml(text)
    package = _table(data, "package")
    name = package.get("name")
    if not isinstance(name, str) or not name:
        return None   # a virtual workspace manifest, or malformed
    directory = posixpath.dirname(manifest)

    def local(value: object, default: str) -> str:
        path = value if isinstance(value, str) else default
        return posixpath.normpath(posixpath.join(directory, path)) if path else ""

    lib = local(_table(data, "lib").get("path"), "src/lib.rs")
    targets = data.get("bin") if isinstance(data, dict) else None
    bins = frozenset(
        local(target.get("path"), "")
        for target in (targets if isinstance(targets, list) else [])
        if isinstance(target, dict) and isinstance(target.get("path"), str)
    )
    build = package.get("build")
    return _Crate(
        name=name.replace("-", "_"),
        directory=directory,
        lib=lib if lib in paths else "",
        bins=bins,
        build=local(build, "build.rs") if build is not False else "",
    )


def _target_shape(parts: Sequence[str]) -> bool:
    """`x.rs` or `x/main.rs`: the layouts Cargo discovers a target from."""
    return len(parts) == 1 or (len(parts) == 2 and parts[1] == "main.rs")


def _root_kind(crate: _Crate, relpath: str) -> str:
    """"bin", "lib" or "aux" (tests, examples, benches, build script) when
    `relpath` is one of `crate`'s target roots; "" otherwise."""
    tail = relpath[len(crate.directory) + 1 :] if crate.directory else relpath
    parts = tail.split("/")
    if tail == "src/main.rs" or relpath in crate.bins or (
        parts[:2] == ["src", "bin"] and _target_shape(parts[2:])
    ):
        return "bin"
    if relpath == crate.lib:
        return "lib"
    if relpath == crate.build or (
        parts[0] in _RUST_TARGET_DIRS and _target_shape(parts[1:])
    ):
        return "aux"
    return ""


def _scan_rust(source: str) -> _RustScan:
    """`mod x;` declarations and `use super` paths, from masked source.

    A brace stack records which blocks are inline modules, so `mod a { mod
    b; }` names a/b.rs and the ubiquitous `mod tests { use super::*; }` is
    seen to mean the file itself, not its parent module.
    """
    stack: list[str] = []    # inline module name, or "" for any other block
    children: list[tuple[str, ...]] = []
    supers: dict[str, list[str]] = {}
    for match in _RUST_SCOPE.finditer(mask(source, RUST.syntax).text):
        name, brace = match.group("mod"), match.group("brace")
        if name is not None:
            if match.group("end") == "{":
                stack.append(name)
            else:
                children.append((*filter(None, stack), name))
        elif brace == "{":
            stack.append("")
        elif brace == "}":
            if stack:
                stack.pop()
        else:
            climbs = match.group("supers").count("super")
            rest = [part.strip() for part in (match.group("path") or "").split("::")]
            rest = [part for part in rest if part]
            inline = sum(1 for scope in stack if scope)
            written = "::".join(["super"] * climbs + rest)
            meant = "::".join(["super"] * (climbs - inline) + rest)
            supers.setdefault(written, []).append(meant if climbs > inline else "")
    return _RustScan(
        tuple(children), {path: tuple(found) for path, found in supers.items()}
    )


class _Rust(_Resolver):
    """Cargo crates and the module tree: `crate::`, `self::`, `super::`,
    workspace crate names, and `mod x;` declarations."""

    def __init__(self, project: _Project) -> None:
        super().__init__(project)
        self.files = project.of("rust")
        self.crates: dict[str, _Crate] = {}      # crate directory -> crate
        self.named: dict[str, _Crate] = {}       # name in paths -> crate
        for manifest in project.configs("Cargo.toml", self.files):
            crate = _crate(manifest, project.read(manifest), project.paths)
            if crate is not None:
                self.crates[crate.directory] = crate
                self.named.setdefault(crate.name, crate)
        self._owners: dict[str, _Crate | None] = {}
        self.roots: dict[str, str] = {}          # root file -> kind
        for relpath in self.files:
            crate = self._owner(posixpath.dirname(relpath))
            kind = _root_kind(crate, relpath) if crate is not None else ""
            if kind:
                self.roots[relpath] = kind
        self._root_dirs: dict[str, str] = {}     # directory -> its preferred root
        ranked = sorted((_ROOT_RANK[kind], path) for path, kind in self.roots.items())
        for _, relpath in ranked:
            self._root_dirs.setdefault(posixpath.dirname(relpath), relpath)
        self._scans: dict[str, _RustScan] = {}
        self._names: dict[str, frozenset[str]] = {}
        self._contexts: dict[str, tuple[str, tuple[str, ...]] | None] = {}

    def _owner(self, directory: str) -> _Crate | None:
        """The crate whose Cargo.toml is nearest at or above `directory`."""
        chain: list[str] = []
        crate: _Crate | None = None
        while True:
            if directory in self._owners:
                crate = self._owners[directory]
                break
            chain.append(directory)
            crate = self.crates.get(directory)
            if crate is not None or not directory:
                break
            directory = posixpath.dirname(directory)
        for path in chain:
            self._owners[path] = crate
        return crate

    def _scan(self, relpath: str) -> _RustScan:
        """The file's masked-source facts, masked only when they can matter:
        a `mod x;` to follow, or a `use super` an inline module may scope."""
        scan = self._scans.get(relpath)
        if scan is None:
            source = self.project.source(relpath)
            needed = _RUST_CHILD.search(source) or (
                _RUST_SUPER.search(source) and _RUST_INLINE.search(source)
            )
            scan = _scan_rust(source) if needed else _RustScan()
            self._scans[relpath] = scan
        return scan

    def _context(self, relpath: str) -> tuple[str, tuple[str, ...]] | None:
        """(the crate root a file hangs from, its module path under that root)."""
        if relpath not in self._contexts:
            self._contexts[relpath] = self._find_context(relpath)
        return self._contexts[relpath]

    def _find_context(self, relpath: str) -> tuple[str, tuple[str, ...]] | None:
        if relpath in self.roots:
            return relpath, ()
        crate = self._owner(posixpath.dirname(relpath))
        if crate is None:
            return None
        directory = posixpath.dirname(relpath)
        while directory not in self._root_dirs:
            if directory == crate.directory or not directory:
                return None
            directory = posixpath.dirname(directory)
        tail = relpath[len(directory) + 1 :] if directory else relpath
        parts = tail.removesuffix(".rs").split("/")
        if parts[-1] == "mod":
            parts.pop()
        return self._root_dirs[directory], tuple(parts)

    def _module_file(self, root: str, path: Sequence[str]) -> str:
        """`<root dir>/a/b.rs` or `<root dir>/a/b/mod.rs` for path [a, b]."""
        stem = posixpath.join(posixpath.dirname(root), *path)
        for candidate in (stem + ".rs", stem + "/mod.rs"):
            if candidate in self.project.paths:
                return candidate
        return ""

    def _lookup(self, root: str, base: tuple[str, ...], rest: Sequence[str]) -> str:
        """The deepest file along `base + rest`, down to the crate root: an
        item, or a module declared inline, lives in its nearest ancestor's
        file, so an empty rest is `base` itself."""
        path = (*base, *rest)
        for size in range(len(path), 0, -1):
            hit = self._module_file(root, path[:size])
            if hit:
                return hit
        return root

    def imports(self, importer: str) -> list[str]:
        specs = self.project.imports(importer)
        if not any(spec == "super" or spec.startswith("super::") for spec in specs):
            return specs
        supers = self._scan(importer).supers
        found: list[str] = []
        for spec in specs:
            if spec in supers:
                found.extend(path for path in supers[spec] if path)
            else:
                found.append(spec)
        return found

    def resolve(self, importer: str, spec: str) -> _Resolved:
        parts = [part.strip().removeprefix("r#") for part in spec.split("::")]
        if not parts[0]:
            parts = parts[1:]     # `::std::fmt`: an extern crate path
        head = parts[0] if parts else ""
        if not head:
            return _NOTHING
        if head in _RUST_RELATIVE or head in self._declared(importer):
            target = self._relative(importer, head, parts)
            if target is None:
                return _NOTHING
        else:
            crate = self.named.get(head)
            if crate is None:
                return _Resolved(external=head)
            root = crate.lib or crate.main(self.project.paths)
            target = self._lookup(root, (), parts[1:]) if root else ""
        return _Resolved((target,)) if target and target != importer else _NOTHING

    def _relative(self, importer: str, head: str, parts: list[str]) -> str | None:
        """The file a path relative to this crate names; None above its root.

        Besides `crate::`, `self::` and `super::`, Rust 2018 lets a path start
        with any name declared in the file, so `mod store; use store::Cart;`
        names the child module and `use Kind::*` an enum in this very file.
        """
        context = self._context(importer)
        if context is None:
            return None
        root, here = context
        if head in ("crate", "$crate"):
            return self._lookup(root, (), parts[1:])
        if head == "self":
            return self._lookup(root, here, parts[1:])
        if head != "super":
            return self._lookup(root, here, parts)
        climbs = 0
        while climbs < len(parts) and parts[climbs] == "super":
            climbs += 1
        if climbs > len(here):
            return None
        return self._lookup(root, here[: len(here) - climbs], parts[climbs:])

    def _declared(self, relpath: str) -> frozenset[str]:
        """Names the file declares at top level: modules, types, functions."""
        names = self._names.get(relpath)
        if names is None:
            facts = self.project.facts.get(relpath)
            names = frozenset(
                symbol.name for symbol in (facts.symbols if facts is not None else ())
            )
            self._names[relpath] = names
        return names

    def implicit(self, importer: str) -> list[str]:
        children = self._scan(importer).children
        if not children:
            return []
        name = posixpath.basename(importer)
        home = (
            posixpath.dirname(importer)
            if importer in self.roots
            or name == "mod.rs"
            or (name in ("lib.rs", "main.rs") and self._context(importer) is None)
            else importer.removesuffix(".rs")
        )
        found: list[str] = []
        for path in children:
            stem = posixpath.join(home, *path)
            found.extend(
                candidate
                for candidate in (stem + ".rs", stem + "/mod.rs")
                if candidate in self.project.paths
            )
        return found

    def entry_points(self) -> dict[str, str]:
        labels = {"bin": "binary entry", "lib": "library root"}
        return {
            relpath: labels[kind]
            for relpath, kind in sorted(self.roots.items())
            if kind in labels
        }


# -- Java ------------------------------------------------------------------------

_JAVA_PACKAGE = re.compile(
    r"^\s*(?:@[\w.]+(?:\s*\([^)]*\))?\s*)*package\s+([\w.]+)\s*;", re.M
)
_JAVA_WORD = re.compile(r"[A-Za-z_$][\w$]*")


def _class_name(relpath: str) -> str:
    """The top-level class a .java file declares, from its name; "" for
    package-info.java and other names no class can have."""
    stem = posixpath.basename(relpath).removesuffix(".java")
    return stem if _JAVA_WORD.fullmatch(stem) else ""


def _java_owner(parts: Sequence[str]) -> list[str]:
    """The package segments of a dotted name: those before the first one
    that is capitalised, which by convention starts the class name."""
    owner: list[str] = []
    for part in parts:
        if part[:1].isupper():
            break
        owner.append(part)
    return owner


def _is_java_main(symbol: Symbol) -> bool:
    signature = re.sub(r"\s+", " ", symbol.signature).replace(" (", "(")
    return symbol.kind is SymbolKind.METHOD and "static void main(" in signature


class _Java(_Resolver):
    """Java packages and classes: FQCN imports, wildcards, static and nested
    names, and the same-package references Java needs no import for.

    Each file is masked once, and only the identifiers that are some project
    class's name are kept, so references cost one pass per file however
    large a package is.
    """

    def __init__(self, project: _Project) -> None:
        super().__init__(project)
        self.files = project.of("java")
        names = {name for name in map(_class_name, self.files) if name}
        self.package: dict[str, str] = {}
        self.mentions: dict[str, frozenset[str]] = {}
        self.classes: dict[str, list[str]] = {}               # FQCN -> files
        self.members: dict[str, dict[str, list[str]]] = {}    # package -> class
        for relpath in self.files:
            masked = mask(project.source(relpath), JAVA.syntax).text
            match = _JAVA_PACKAGE.search(masked)
            package = match.group(1) if match else ""
            self.package[relpath] = package
            self.mentions[relpath] = frozenset(_JAVA_WORD.findall(masked)) & names
            name = _class_name(relpath)
            if name:
                fqcn = f"{package}.{name}" if package else name
                self.classes.setdefault(fqcn, []).append(relpath)
                package_members = self.members.setdefault(package, {})
                package_members.setdefault(name, []).append(relpath)

    def resolve(self, importer: str, spec: str) -> _Resolved:
        target = _closest(self.classes.get(spec, ()), importer)
        if target:
            return _Resolved((target,))
        members = self.members.get(spec)
        if members is not None:
            # A wildcard import links only the classes the file mentions.
            used = self.mentions.get(importer, frozenset())
            hits = [
                _closest(members[name], importer)
                for name in sorted(used)
                if name in members
            ]
            return _Resolved(tuple(filter(None, hits)))
        parts = spec.split(".")
        for size in range(len(parts) - 1, 0, -1):
            target = _closest(self.classes.get(".".join(parts[:size]), ()), importer)
            if target:
                return _Resolved((target,))
        owner = _java_owner(parts)
        if ".".join(owner) in self.members:
            return _NOTHING   # a project package; the class lives in another file
        return _Resolved(external=".".join((owner or parts[:1])[:3]))

    def implicit(self, importer: str) -> list[str]:
        """Same-package classes the file mentions, except a name it imports
        from elsewhere: a single-type import shadows the package's class."""
        package = self.package.get(importer, "")
        members = self.members.get(package, {})
        if not members:
            return []
        shadowed = {_class_name(importer)}
        for spec in self.project.imports(importer):
            owner, _, name = spec.rpartition(".")
            if owner != package:
                shadowed.add(name)
        hits = (
            _closest(members[name], importer)
            for name in sorted(self.mentions.get(importer, ()))
            if name not in shadowed and name in members
        )
        return [hit for hit in hits if hit]

    def entry_points(self) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for relpath in self.files:
            facts = self.project.facts.get(relpath)
            if facts is not None and any(map(_is_java_main, facts.all_symbols)):
                reasons[relpath] = "`main` method"
        return reasons


_FAMILIES: dict[str, type[_Resolver]] = {
    "python": _Python,
    **dict.fromkeys(_SCRIPT_LANGUAGES, _Script),
    "go": _Go,
    "rust": _Rust,
    "java": _Java,
}


def _assemble(
    nodes: Mapping[str, SourceFile],
    internal: Mapping[str, set[str]],
    external: Mapping[str, set[str]],
    entries: Mapping[str, str],
) -> ProjectGraph:
    """Invert the edges into dependents and tested_by, and group by module."""
    dependents: dict[str, list[str]] = {relpath: [] for relpath in nodes}
    tested_by: dict[str, list[str]] = {relpath: [] for relpath in nodes}
    for relpath, file in nodes.items():     # sorted, so every list stays sorted
        inbound = tested_by if file.role is Role.TEST else dependents
        for target in internal[relpath]:
            inbound[target].append(relpath)

    files: dict[str, FileNode] = {}
    for relpath, file in nodes.items():
        files[relpath] = FileNode(
            relpath=relpath,
            language=file.language,
            module=posixpath.dirname(relpath),
            internal=tuple(sorted(internal[relpath])),
            external=tuple(sorted(external[relpath])),
            dependents=tuple(dependents[relpath]),
            tested_by=tuple(tested_by[relpath]),
            entry_point=entries.get(relpath, ""),
        )

    members: dict[str, list[str]] = {}
    depends_on: dict[str, set[str]] = {}
    used_by: dict[str, set[str]] = {}
    for relpath, node in files.items():
        members.setdefault(node.module, []).append(relpath)
        for target in node.internal:
            other = files[target].module
            if other != node.module:
                depends_on.setdefault(node.module, set()).add(other)
                used_by.setdefault(other, set()).add(node.module)
    modules = {
        path: ModuleNode(
            path=path,
            files=tuple(members[path]),
            depends_on=tuple(sorted(depends_on.get(path, ()))),
            used_by=tuple(sorted(used_by.get(path, ()))),
        )
        for path in sorted(members)
    }
    return ProjectGraph(files=files, modules=modules)
