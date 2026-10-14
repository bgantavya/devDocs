"""A run: scan, parse, plan, ask, compose, render — resumably and on budget.

`Pipeline.prepare` does everything that costs nothing: the scan, parsing,
the import graph, git history, the plan, the known-issue sweep and the
retrieval indexes that give each prompt its cross-file context. `price`
builds every prompt the plan implies, quotes it against the catalog and the
response cache, and cuts the plan to the budget. `estimate` stops there;
`run` goes on to send exactly what was priced, so an estimate describes the
run that would follow it.

Calls go out on a thread pool in three waves — files, then modules (which
read the files' purposes), then the architecture overview (which reads the
modules') — most valuable first, so a budget or quota stop cuts the least
useful work. Every paid answer is cached the moment it lands and every item
is recorded in the run ledger, so an interrupted run writes what it has,
stubs the rest, and resumes next time without paying twice.

Rendering comes last and costs nothing: one renderer per configured format
turns the composed documents into files.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import posixpath
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from devdocs import __version__
from devdocs.compose import Composer, Prose, docstring_summary, first_sentence
from devdocs.config import Config
from devdocs.context import StructuralContext
from devdocs.errors import (
    AuthError,
    BudgetExceeded,
    ConfigError,
    DevDocsError,
    QuotaExhausted,
)
from devdocs.graph import GraphBuilder, ProjectGraph
from devdocs.history import FileHistory, GitHistory
from devdocs.index.chunk import ChunkStore, chunk_file
from devdocs.index.db import Database
from devdocs.index.embed import Embeddings
from devdocs.index.ledger import ItemStatus, LedgerItem, RunLedger, config_fingerprint
from devdocs.index.search import LexicalIndex, VectorIndex
from devdocs.issues import Issue, find_issues
from devdocs.llm import Completion, Embedder
from devdocs.llm.catalog import assume, find
from devdocs.llm.client import CallEvent, LLMClient
from devdocs.llm.providers import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from devdocs.parse import Confidence, FileFacts, get_parser
from devdocs.plan import Plan, Planner, PlannerOptions, fit_to_budget
from devdocs.prompting import Prompt, PromptBuilder
from devdocs.render import (
    MANIFEST,
    OutputWriter,
    Renderer,
    WriteReport,
    generated_files,
    renderer_for,
)
from devdocs.render.model import DocSet, Layout, Mode, RunInfo
from devdocs.render.style import StyleGuard, acceptable, parse_reply
from devdocs.retrieve import Retriever
from devdocs.scan import Scanner, SourceFile

__all__ = [
    "Estimate",
    "Pipeline",
    "Prepared",
    "PricedItem",
    "RunSummary",
    "project_name",
]

_QUALITY = {
    "cheap": PlannerOptions(),
    "balanced": PlannerOptions(coverage=0.35, min_prose_files=40),
    "high": PlannerOptions(coverage=0.6, min_prose_files=60, include_documented=True),
}

# Semantic context may claim at most this share of either limit.
_EMBED_SHARE = 0.5

_SHOWN_WARNINGS = 10     # per renderer in the log; summaries keep them all
# What a format's warnings mean, and how to avoid them.
_WARNINGS = {
    "pdf": ("characters outside the built-in fonts were printed as '?'",
            "for all of Unicode, use --pdf-engine chrome or --pdf-engine weasyprint"),
}

_JS_GLOBALS = frozenset({
    "Array", "ArrayBuffer", "Boolean", "Date", "Error", "JSON", "Map", "Math",
    "Number", "Object", "Promise", "Proxy", "Reflect", "RegExp", "Set",
    "String", "Symbol", "TypeError", "URL", "WeakMap", "WeakSet", "console",
    "document", "fetch", "globalThis", "process", "require", "window",
    "undefined", "null", "true", "false", "this", "async", "await",
})


@dataclass
class Prepared:
    """Everything a run knows before spending anything."""

    root: Path
    files: dict[str, SourceFile]            # documentable files
    sources: dict[str, str]
    facts: dict[str, FileFacts]
    hashes: dict[str, str]
    graph: ProjectGraph
    plan: Plan
    issues: dict[str, list[Issue]]
    head: str
    skipped: dict[str, str]
    languages: dict[str, int]
    prompts: dict[tuple[str, str], Prompt] = field(default_factory=dict)
    retriever: Retriever | None = None


@dataclass(frozen=True)
class PricedItem:
    kind: str
    key: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float          # worst case: the full output cap is used
    cached: bool
    requests: int = 1


@dataclass
class Estimate:
    items: list[PricedItem] = field(default_factory=list)
    demoted: list[tuple[str, str]] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return sum(item.requests for item in self.items if not item.cached)

    @property
    def cached(self) -> int:
        return sum(1 for item in self.items if item.cached)

    @property
    def cost(self) -> float:
        return sum(item.cost for item in self.items if not item.cached)

    @property
    def input_tokens(self) -> int:
        return sum(item.input_tokens for item in self.items if not item.cached)

    @property
    def output_tokens(self) -> int:
        return sum(item.output_tokens for item in self.items if not item.cached)


@dataclass
class RunSummary:
    run_id: str = ""
    resumed: bool = False
    documents: int = 0
    prose: int = 0
    stubs: int = 0
    requests: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    stopped: str = ""        # "" | "budget" | "quota" | "interrupted"
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    written: WriteReport = field(default_factory=WriteReport)


def project_name(root: Path) -> str:
    """The project's own name for itself, from its manifest, else the folder."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
        name = (data.get("project") or {}).get("name") or (
            (data.get("tool") or {}).get("poetry") or {}
        ).get("name")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError):
        pass
    try:
        data = json.loads((root / "package.json").read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            return str(data["name"])
    except (OSError, ValueError):
        pass
    try:
        data = tomllib.loads((root / "Cargo.toml").read_text("utf-8"))
        name = (data.get("package") or {}).get("name")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError):
        pass
    return root.resolve().name


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


class Pipeline:
    """One configured run over one project.

    `client` is required for `price`, `estimate` and `run`; `prepare` and
    `write` need none. `log` receives one-line progress messages for the
    CLI. Without `renderers`, there is one per configured format, built now
    so that a missing PDF engine fails before any call. `warnings` describe
    the last write.
    """

    def __init__(
        self,
        config: Config,
        *,
        client: LLMClient | None = None,
        db: Database | None = None,
        renderers: Sequence[Renderer] | None = None,
        log: Callable[[str], None] | None = None,
        project: str = "",
        description: str = "",
    ) -> None:
        self.config = config
        self.client = client
        self.db = db
        self.root = Path(config.path).resolve()
        self.renderers = list(renderers) if renderers is not None else [
            renderer_for(fmt, split=config.pdf_split, page_size=config.page_size,
                         pdf_engine=config.pdf_engine)
            for fmt in dict.fromkeys(config.formats)
        ]
        self.warnings: list[str] = []
        self.log = log or (lambda message: None)
        self.project = project or project_name(self.root)
        self.description = description
        self.builder = PromptBuilder(project=self.project, description=description)

    # -- preparation (free) ------------------------------------------------

    def prepare(self) -> Prepared:
        started = time.monotonic()
        scan = Scanner(
            self.root, include=self.config.include, exclude=self.config.exclude
        ).scan()
        made = self._made()
        for item in scan.files:
            if item.relpath in made:
                scan.skipped[item.relpath] = "written by devdocs"
        scan.files = [f for f in scan.files if f.relpath not in made]
        coded = [f for f in scan.files if f.language]
        documentable = {f.relpath: f for f in coded if f.is_documentable}

        sources: dict[str, str] = {}
        facts: dict[str, FileFacts] = {}
        for item in coded:
            text = self._read(item)
            sources[item.relpath] = text
            parser = get_parser(item.language)
            if parser is None:
                continue
            try:
                facts[item.relpath] = parser.parse(item.path, text)
            except Exception as err:   # a parser bug must not end the run
                facts[item.relpath] = FileFacts(
                    path=item.path, language=item.language, loc=item.lines,
                    confidence=Confidence.FAILED,
                    notes=[f"parser error: {type(err).__name__}: {err}"],
                )

        graph = GraphBuilder(self.root, scan.files, facts, sources).build()
        git = GitHistory(self.root)
        history: Mapping[str, FileHistory] = git.recent() if git.available else {}
        options = _QUALITY.get(self.config.quality, PlannerOptions())
        plan = Planner(options).plan(list(documentable.values()), facts, graph, history)

        languages: dict[str, int] = {}
        for item in documentable.values():
            languages[item.language] = languages.get(item.language, 0) + 1

        prepared = Prepared(
            root=self.root,
            files=documentable,
            sources=sources,
            facts=facts,
            hashes={rel: _sha(sources.get(rel, "")) for rel in documentable},
            graph=graph,
            plan=plan,
            issues={
                rel: found
                for rel, item in documentable.items()
                if (found := find_issues(sources.get(rel, ""), item.language))
            },
            head=git.head() if git.available else "",
            skipped=scan.skipped,
            languages=languages,
        )
        prepared.retriever = self._retriever(prepared)
        self.log(
            f"scanned {len(scan.files)} files, {len(documentable)} documentable, "
            f"{plan.calls()} model calls planned ({time.monotonic() - started:.1f}s)"
        )
        return prepared

    def _made(self) -> set[str]:
        """What devdocs wrote inside the project, by relpath: the out dir's
        manifest and every file it lists, so no HTML site's scripts are taken
        for source to document. Hand-written files beside them are scanned as
        usual."""
        made: set[str] = set()
        out = self.out.resolve()
        if out.is_relative_to(self.root):
            base = out.relative_to(self.root).as_posix()
            base = "" if base == "." else base
            listed = generated_files(out)
            if listed:
                made.update(posixpath.join(base, path) for path in (MANIFEST, *listed))
        return made

    @staticmethod
    def _read(item: SourceFile) -> str:
        try:
            return item.path.read_text("utf-8", errors="replace")
        except OSError:
            return ""

    # -- retrieval ---------------------------------------------------------

    def _retriever(self, prepared: Prepared) -> Retriever | None:
        """Structural and lexical retrieval, both free. In semantic mode the
        chunks are also synced to the store, where a vector paid for once
        stays valid until its chunk changes, and the stored ones are used."""
        if self.config.context == "off":
            return None
        by_file = {
            rel: chunk_file(rel, prepared.sources.get(rel, ""), prepared.facts.get(rel))
            for rel in sorted(prepared.files)
        }
        chunks = [chunk for group in by_file.values() for chunk in group]
        retriever = Retriever(
            StructuralContext(prepared.graph, prepared.facts, prepared.sources),
            chunks=chunks,
            lexical=LexicalIndex(chunks),
        )
        embeddings = self._embeddings()
        if embeddings is not None:
            for rel, group in by_file.items():
                embeddings.store.replace(rel, group)
            embeddings.store.prune(by_file)
            self._attach(retriever, embeddings)
        return retriever

    def _embeddings(self) -> Embeddings | None:
        if self.config.context != "semantic" or self.db is None:
            return None
        info = find(EMBEDDING_MODEL) or assume(EMBEDDING_MODEL, "gemini")
        embeddings = Embeddings(
            ChunkStore(self.db), model=info, dims=EMBEDDING_DIMENSIONS
        )
        client = self.client
        if client is not None:
            embeddings.budget = client.budget
            if isinstance(client.model, Embedder):
                embeddings.embedder = client.model
                embeddings.dims = client.model.dimensions
                embeddings.limiter = client.limiter(info)
        return embeddings

    @staticmethod
    def _attach(retriever: Retriever, embeddings: Embeddings) -> None:
        ids, vectors = embeddings.vectors()
        if ids:
            retriever.vectors = VectorIndex(ids, vectors)
            retriever.vector_of = dict(zip(ids, vectors, strict=True))

    def _embedding_fits(self, requests: int, cost: float) -> bool:
        """Whether embedding leaves the documents most of each limit."""
        config = self.config
        if config.max_cost and cost > config.max_cost * _EMBED_SHARE:
            return False
        return not config.max_requests or requests <= config.max_requests * _EMBED_SHARE

    def _embed(self, prepared: Prepared) -> None:
        """Embed the chunks that lack a vector, if that fits the limits.

        A stop on budget or quota costs only semantic hits: the vectors
        stored so far are used and the run goes on.
        """
        embeddings = self._embeddings()
        if embeddings is None or prepared.retriever is None:
            return
        quote = embeddings.quote()
        if not quote.chunks:
            return
        if not self._embedding_fits(quote.requests, quote.cost):
            self.log(
                f"semantic context skipped: embedding {quote.chunks} chunks could "
                f"cost ${quote.cost:.4f}, over half the limits; raise --max-cost "
                "or use --context standard"
            )
            return
        try:
            done, spent = embeddings.update()
        except (BudgetExceeded, QuotaExhausted) as err:
            self.log(f"semantic context: {err.message}; using the vectors stored")
        else:
            self.log(f"embedded {done} chunks (${spent:.4f})")
        self._attach(prepared.retriever, embeddings)

    # -- prompts and prices ------------------------------------------------

    def _file_prompts(self, prepared: Prepared) -> Iterator[Prompt]:
        retriever = prepared.retriever
        for plan in prepared.plan.prose_files():
            facts = prepared.facts.get(plan.relpath)
            if facts is None:
                continue
            yield self.builder.file(
                plan, facts, prepared.sources.get(plan.relpath, ""),
                prepared.graph.file(plan.relpath),
                retriever.for_file(plan.relpath, facts) if retriever else (),
            )

    def _module_prompts(
        self, prepared: Prepared, summaries: Mapping[str, str]
    ) -> Iterator[Prompt]:
        retriever = prepared.retriever
        for plan in prepared.plan.prose_modules():
            yield self.builder.module(
                plan, prepared.graph, summaries, prepared.facts,
                retriever.for_module(plan.files, prepared.facts) if retriever else (),
            )

    def _architecture_prompt(
        self, prepared: Prepared, summaries: Mapping[str, str]
    ) -> Prompt | None:
        if prepared.plan.architecture is not Mode.PROSE:
            return None
        retriever = prepared.retriever
        return self.builder.architecture(
            prepared.graph, summaries, prepared.languages,
            retriever.for_architecture() if retriever else (),
        )

    def _docstring_summaries(self, prepared: Prepared) -> dict[str, str]:
        return {
            rel: first_sentence(docstring_summary(facts.module_docstring))
            for rel, facts in prepared.facts.items()
            if rel in prepared.files and facts.module_docstring
        }

    def price(self, prepared: Prepared, *, embedding: bool = True) -> Estimate:
        """Quote every planned call and cut the plan to the budget.

        Prompts are built as a run would build them. Module prompts carry the
        file purposes a run would have by then, read back from the cache where
        those answers exist and taken from docstrings where they do not yet;
        the architecture prompt likewise. So an estimate after a run is free
        exactly when the run would be. With `embedding`, the vectors semantic
        context still needs are quoted too, and their cost held back.
        """
        client = self._client()
        guard = StyleGuard(known=self._known(prepared))
        describe = {p.relpath: p.describe for p in prepared.plan.files}
        estimate = Estimate()
        costs: dict[tuple[str, str], float] = {}

        def quote(prompt: Prompt) -> Prose | None:
            result = client.quote(
                prompt.tier, prompt.system, prompt.user,
                max_output_tokens=prompt.max_output_tokens,
                identity=prompt.cache_text,
            )
            costs[(prompt.kind, prompt.key)] = 0.0 if result.cached else result.cost
            estimate.items.append(PricedItem(
                prompt.kind, prompt.key, result.model, result.input_tokens,
                result.output_tokens, result.cost, result.cached,
            ))
            prepared.prompts[(prompt.kind, prompt.key)] = prompt
            if not result.cached:
                return None
            allowed = describe.get(prompt.key, ()) if prompt.kind == "file" else ()
            return self._cached_prose(prompt, guard, allowed)

        summaries = self._docstring_summaries(prepared)
        for prompt in list(self._file_prompts(prepared)):
            prose = quote(prompt)
            if prose is not None and prose.reply.purpose:
                summaries[prompt.key] = first_sentence(prose.reply.purpose)
        module_summaries: dict[str, str] = {}
        for prompt in list(self._module_prompts(prepared, summaries)):
            prose = quote(prompt)
            if prose is not None and prose.reply.purpose:
                module_summaries[prompt.key] = first_sentence(prose.reply.purpose)
        overview = self._architecture_prompt(prepared, module_summaries)
        if overview is not None:
            quote(overview)

        embeddings = self._embeddings() if embedding else None
        if embeddings is not None:
            need = embeddings.quote()
            if need.chunks and self._embedding_fits(need.requests, need.cost):
                estimate.items.insert(0, PricedItem(
                    "embed", "", embeddings.model.id, need.tokens, 0, need.cost,
                    False, requests=need.requests,
                ))

        # What is already spent or held back for embedding is not the plan's.
        held = [i for i in estimate.items if i.kind == "embed"]
        held_cost = client.budget.spent + sum(i.cost for i in held)
        held_requests = client.budget.requests + sum(i.requests for i in held)
        max_cost = self.config.max_cost
        if max_cost:
            max_cost = max(max_cost - held_cost, 1e-9)
        cached = sum(1 for item in estimate.items if item.cached)
        limit = 0
        if self.config.max_requests:
            limit = max(self.config.max_requests - held_requests, 1) + cached
        estimate.demoted = fit_to_budget(
            prepared.plan, costs, max_cost=max_cost, max_requests=limit
        )
        if estimate.demoted:
            dropped = set(estimate.demoted)
            estimate.items = [
                i for i in estimate.items if (i.kind, i.key) not in dropped
            ]
            for kind_key in dropped:
                prepared.prompts.pop(kind_key, None)
        return estimate

    def estimate(self, prepared: Prepared) -> Estimate:
        return self.price(prepared)

    def _client(self) -> LLMClient:
        if self.client is None:
            raise ConfigError("no model client configured for this run")
        return self.client

    # -- the run -----------------------------------------------------------

    def run(self, prepared: Prepared, *, resume: bool = True) -> RunSummary:
        """Send the priced plan, then compose and write every document.

        Documents are written even when the run stops early on budget, quota
        or Ctrl-C: whatever was paid for is kept, the rest is stubbed, and
        the ledger leaves the run resumable.
        """
        client = self._client()
        self._embed(prepared)
        self.price(prepared, embedding=False)
        summary = RunSummary()
        ledger = RunLedger(self.db) if self.db is not None else None
        if ledger is not None:
            fingerprint = config_fingerprint(
                self.root, self.config.provider, self.config.quality,
                self.config.include, self.config.exclude,
            )
            summary.run_id, summary.resumed = ledger.start(fingerprint, resume=resume)
            ledger.plan(summary.run_id, [
                LedgerItem(key or kind, kind, _sha(p.system + p.cache_text))
                for (kind, key), p in sorted(prepared.prompts.items())
            ])

        guard = StyleGuard(known=self._known(prepared))
        prose: dict[tuple[str, str], Prose] = {}
        interrupted: KeyboardInterrupt | None = None
        hits = [0]
        previous = client.on_event

        def count(event: CallEvent) -> None:
            if event.kind == "cache-hit":
                hits[0] += 1
            if previous is not None:
                previous(event)

        client.on_event = count
        try:
            self._waves(prepared, prose, guard, summary, ledger)
        except KeyboardInterrupt as err:
            summary.stopped = "interrupted"
            interrupted = err
        finally:
            client.on_event = previous

        docset = self._compose(prepared, prose)
        budget = client.budget
        docset.run = RunInfo(
            generator=f"devdocs {__version__}",
            generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            git_commit=prepared.head,
            provider=self.config.provider,
            requests=budget.requests,
            cache_hits=hits[0],
            input_tokens=sum(p.input_tokens for p in prose.values()),
            output_tokens=sum(p.output_tokens for p in prose.values()),
            cost_usd=round(budget.spent, 6),
        )
        summary.written = self.write(docset)
        summary.warnings = list(self.warnings)
        summary.documents = len(docset.documents)
        summary.prose = sum(1 for d in docset.documents if d.meta.mode is Mode.PROSE)
        summary.stubs = summary.documents - summary.prose
        summary.requests = budget.requests
        summary.cache_hits = hits[0]
        summary.cost = budget.spent
        summary.input_tokens = docset.run.input_tokens
        summary.output_tokens = docset.run.output_tokens
        summary.degraded = sorted(
            rel for rel, facts in prepared.facts.items()
            if rel in prepared.files
            and facts.confidence in (Confidence.DEGRADED, Confidence.FAILED)
        )
        if ledger is not None and summary.run_id:
            complete = not summary.stopped and not summary.failed
            ledger.finish(summary.run_id, "finished" if complete else "interrupted")
        if interrupted is not None:
            raise interrupted
        return summary

    def _waves(
        self,
        prepared: Prepared,
        prose: dict[tuple[str, str], Prose],
        guard: StyleGuard,
        summary: RunSummary,
        ledger: RunLedger | None,
    ) -> None:
        files = [p for p in self._file_prompts(prepared)
                 if ("file", p.key) in prepared.prompts]
        self._wave(files, prose, guard, prepared, summary, ledger)

        summaries = self._docstring_summaries(prepared)
        for (kind, key), result in prose.items():
            if kind == "file" and result.reply.purpose:
                summaries[key] = first_sentence(result.reply.purpose)
        modules = [p for p in self._module_prompts(prepared, summaries)
                   if ("module", p.key) in prepared.prompts]
        self._wave(modules, prose, guard, prepared, summary, ledger)

        module_summaries = {
            key: first_sentence(result.reply.purpose)
            for (kind, key), result in prose.items()
            if kind == "module" and result.reply.purpose
        }
        overview = self._architecture_prompt(prepared, module_summaries)
        if overview is not None and ("architecture", "") in prepared.prompts:
            self._wave([overview], prose, guard, prepared, summary, ledger)

    def _wave(
        self,
        prompts: Sequence[Prompt],
        prose: dict[tuple[str, str], Prose],
        guard: StyleGuard,
        prepared: Prepared,
        summary: RunSummary,
        ledger: RunLedger | None,
    ) -> None:
        """Send one wave of calls, most valuable first, stopping cleanly on a
        budget or quota stop and recording each outcome as it lands."""
        if not prompts or summary.stopped:
            return
        describe = {p.relpath: p.describe for p in prepared.plan.files}
        workers = max(1, min(self.config.concurrency, len(prompts)))
        pool = ThreadPoolExecutor(max_workers=workers)
        futures: dict[Future[Prose | None], Prompt] = {}
        pending = list(prompts)
        try:
            while pending or futures:
                while pending and len(futures) < workers and not summary.stopped:
                    prompt = pending.pop(0)
                    allowed = describe.get(prompt.key, ())
                    if prompt.kind != "file":
                        allowed = ()
                    futures[pool.submit(self._ask, prompt, guard, allowed)] = prompt
                if not futures:
                    break
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda f: futures[f].key):
                    self._settle(future, futures.pop(future), prose, summary, ledger)
                if summary.stopped:
                    pending.clear()
        finally:
            # In-flight calls finish and are cached; queued ones never start.
            pool.shutdown(wait=True, cancel_futures=True)

    def _settle(
        self,
        future: Future[Prose | None],
        prompt: Prompt,
        prose: dict[tuple[str, str], Prose],
        summary: RunSummary,
        ledger: RunLedger | None,
    ) -> None:
        source = prompt.key or prompt.kind
        try:
            result = future.result()
        except BudgetExceeded as err:
            summary.stopped = summary.stopped or "budget"
            self.log(f"stopped: {err.message}")
            return
        except QuotaExhausted as err:
            summary.stopped = summary.stopped or "quota"
            self.log(f"stopped: {err.message}")
            return
        except (AuthError, ConfigError):
            raise
        except DevDocsError as err:
            summary.failed.append((prompt.kind, prompt.key, err.message))
            if ledger is not None and summary.run_id:
                ledger.mark(summary.run_id, source, prompt.kind, ItemStatus.FAILED,
                            error=err.message)
            self.log(f"failed {prompt.kind} {source}: {err.message}")
            return

        if result is None:
            summary.failed.append((prompt.kind, prompt.key, "no usable reply"))
            if ledger is not None and summary.run_id:
                ledger.mark(summary.run_id, source, prompt.kind, ItemStatus.FAILED,
                            error="no usable reply")
            return
        prose[(prompt.kind, prompt.key)] = result
        if ledger is not None and summary.run_id:
            ledger.mark(summary.run_id, source, prompt.kind, ItemStatus.DONE)

    def _ask(
        self, prompt: Prompt, guard: StyleGuard, allowed: tuple[str, ...]
    ) -> Prose | None:
        """One call. The reply is parsed and guarded before it is accepted, so
        an unusable answer escalates instead of being cached."""
        completion = self._client().complete(
            prompt.tier, prompt.system, prompt.user,
            max_output_tokens=prompt.max_output_tokens,
            tag=prompt.key or prompt.kind,
            accept=self._acceptor(prompt, guard, allowed),
            identity=prompt.cache_text,
        )
        return self._prose(prompt, guard, allowed, completion)

    def _cached_prose(
        self, prompt: Prompt, guard: StyleGuard, allowed: tuple[str, ...]
    ) -> Prose | None:
        """The prose `_ask` would return from the cache alone, without sending."""
        completion = self._client().cached(
            prompt.tier, prompt.system, prompt.user,
            max_output_tokens=prompt.max_output_tokens,
            accept=self._acceptor(prompt, guard, allowed),
            identity=prompt.cache_text,
        )
        return self._prose(prompt, guard, allowed, completion)

    @staticmethod
    def _acceptor(
        prompt: Prompt, guard: StyleGuard, allowed: tuple[str, ...]
    ) -> Callable[[str], bool]:
        def accept(text: str) -> bool:
            reply = guard.apply(parse_reply(text, prompt.kind), prompt.kind,
                                allowed_symbols=allowed)
            return acceptable(reply, prompt.kind)

        return accept

    @staticmethod
    def _prose(
        prompt: Prompt,
        guard: StyleGuard,
        allowed: tuple[str, ...],
        completion: Completion | None,
    ) -> Prose | None:
        if completion is None or not completion.ok or completion.text is None:
            return None
        reply = guard.apply(parse_reply(completion.text, prompt.kind), prompt.kind,
                            allowed_symbols=allowed)
        if not acceptable(reply, prompt.kind):
            return None
        return Prose(
            reply=reply,
            model=completion.model,
            tier=prompt.tier.value,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
        )

    def _known(self, prepared: Prepared) -> Callable[[str], bool]:
        """Identifiers a model may cite: everything the project defines or
        imports, file names, and the languages' builtins."""
        names: set[str] = set(dir(builtins)) | _JS_GLOBALS
        for relpath, facts in prepared.facts.items():
            base = posixpath.basename(relpath)
            names.update((relpath, base, base.rpartition(".")[0]))
            for symbol in facts.all_symbols:
                names.add(symbol.name)
            for symbol in facts.symbols:
                for child in symbol.children:
                    names.add(f"{symbol.name}.{child.name}")
            for module in facts.imports:
                for part in module.replace("::", ".").replace("/", ".").split("."):
                    if part:
                        names.add(part)
                names.add(module)
        return names.__contains__

    # -- output ------------------------------------------------------------

    def _compose(
        self, prepared: Prepared, prose: Mapping[tuple[str, str], Prose]
    ) -> DocSet:
        modules = [m.path for m in prepared.plan.modules]
        composer = Composer(
            project=self.project,
            files=prepared.files,
            facts=prepared.facts,
            graph=prepared.graph,
            hashes=prepared.hashes,
            modules=modules,
            layout=Layout(per_file=self.config.per_file),
        )
        docset = DocSet(project=self.project, description=self.description)
        file_summaries: dict[str, str] = {}
        for plan in sorted(prepared.plan.files, key=lambda p: p.relpath):
            document = composer.file(
                plan, prose.get(("file", plan.relpath)),
                prepared.issues.get(plan.relpath, ()),
            )
            docset.add(document)
            file_summaries[plan.relpath] = document.summary

        module_summaries: dict[str, str] = {}
        for module in sorted(prepared.plan.modules, key=lambda m: m.path):
            document = composer.module(
                module, prose.get(("module", module.path)), file_summaries
            )
            docset.add(document)
            module_summaries[module.path] = document.summary

        if prepared.files:
            docset.add(composer.architecture(
                prose.get(("architecture", "")), module_summaries, prepared.languages
            ))
        return docset

    @property
    def out(self) -> Path:
        """The output directory: `config.out`, under the root when relative."""
        return self.root / self.config.out

    def write(self, docset: DocSet) -> WriteReport:
        """Render `docset` in every format and write the files to `out`.

        Rendering never calls a model. What a renderer could not show is
        logged and kept in `warnings`.
        """
        files = self._render(docset)
        return OutputWriter(self.out).write(files, docset)

    def _render(self, docset: DocSet) -> dict[str, bytes]:
        """Every format's files; refills `warnings`."""
        files: dict[str, bytes] = {}
        self.warnings = []
        for renderer in self.renderers:
            files.update(renderer.render(docset))
            self.warnings += self._logged_warnings(renderer)
        return files

    def _logged_warnings(self, renderer: Renderer) -> list[str]:
        """`renderer`'s warnings from its last render, logged in one message:
        what they mean, the first few, and how to avoid them."""
        warnings = [str(warning) for warning in getattr(renderer, "warnings", ())]
        if not warnings:
            return []
        meaning, remedy = _WARNINGS.get(renderer.format, ("warnings", ""))
        lines = [f"{renderer.format}: {meaning} ({len(warnings)}):"]
        lines += [f"  {warning}" for warning in warnings[:_SHOWN_WARNINGS]]
        if len(warnings) > _SHOWN_WARNINGS:
            lines.append(f"  and {len(warnings) - _SHOWN_WARNINGS} more")
        if remedy:
            lines.append(f"  {remedy}")
        self.log("\n".join(lines))
        return warnings
