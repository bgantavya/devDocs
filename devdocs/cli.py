"""The `devdocs` command line.

    devdocs generate [PATH]   write docs for a project
    devdocs estimate [PATH]   price a run without sending anything
    devdocs export [PATH]     render the last run again, in other formats, free
    devdocs doctor            check interpreter, configuration, key, provider
    devdocs init [PATH]       write a starter devdocs.toml
    devdocs models            models your key can use, with tier and price
    devdocs cache stats|clear the local response cache

Every failure maps to a stable exit code from `devdocs.errors`, so CI can tell
a bad key (3) from a budget stop (7) from a spent daily quota (10). Progress
goes to stderr and results to stdout; `--json` makes stdout machine-readable.
A v3-style invocation is translated by `devdocs.legacy`, with one notice; a
command line that starts with a v4 command never is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from devdocs import __version__
from devdocs.config import CONFIG_FILENAME, Config, load_config, resolve_api_key
from devdocs.errors import AuthError, ConfigError, DevDocsError, exit_code_for
from devdocs.index.cache import ResponseCache
from devdocs.index.db import Database
from devdocs.index.ledger import DailyUsage
from devdocs.llm.budget import Budget, Router
from devdocs.llm.catalog import CATALOG, PRICES_CHECKED, Tier, catalogued
from devdocs.llm.client import CallEvent, LLMClient
from devdocs.llm.factory import PROVIDERS, OfflineModel, make_provider
from devdocs.render import PDF_ENGINES

__all__ = ["build_parser", "main"]

COMMANDS = ("generate", "estimate", "export", "doctor", "init", "models", "cache")
STATE_DIR = ".devdocs"
_LISTED = 10             # written files an export names; --json lists them all


class _Output:
    """stderr for progress, stdout for results; `--json` and `--quiet` aware."""

    def __init__(self, *, as_json: bool = False, quiet: bool = False,
                 stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def info(self, message: str) -> None:
        if not self.quiet:
            print(message, file=self.stderr, flush=True)

    def warn(self, message: str) -> None:
        print(f"warning: {message}", file=self.stderr, flush=True)

    def result(self, text: str, data: dict[str, Any]) -> None:
        if self.as_json:
            print(json.dumps(data, indent=2, default=str), file=self.stdout)
        else:
            print(text, file=self.stdout)

    def event(self, event: CallEvent) -> None:
        if self.quiet:
            return
        cost = f"  ${event.cost:.5f}" if event.cost else ""
        detail = f"  ({event.detail})" if event.detail and event.kind != "call" else ""
        self.info(f"  {event.kind:<9} {event.model:<24} {event.tag}{cost}{detail}")


# -- arguments -------------------------------------------------------------------


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _output_options(parser: argparse.ArgumentParser) -> None:
    """The project, where its documents go, and in which formats."""
    parser.add_argument("path", nargs="?", default=None,
                        help="project root (default: current directory)")
    parser.add_argument("--out", help="output directory, relative to the project")
    parser.add_argument("--format", action="append", type=_csv, default=None,
                        dest="formats", metavar="FORMAT",
                        help="markdown, html or pdf; repeat or comma-separate "
                             "(default: markdown)")
    parser.add_argument("--pdf-engine", choices=PDF_ENGINES,
                        help="builtin (default), or chrome or weasyprint "
                             "for all of Unicode")
    parser.add_argument("--page-size", metavar="SIZE",
                        help="PDF paper: A4 (default) or Letter")
    parser.add_argument("--split", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="one PDF per document instead of one book")


def _force_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true",
                        help="replace files in the output directory that "
                             "devdocs has no record of writing")


def _run_options(parser: argparse.ArgumentParser) -> None:
    _output_options(parser)
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--model", help="use this one model for every call")
    parser.add_argument("--quality", choices=("cheap", "balanced", "high"))
    parser.add_argument("--context", choices=("off", "standard", "semantic"),
                        help="retrieved cross-file context (default: standard, free)")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="stop before spending more than this (0: no limit)")
    parser.add_argument("--max-requests", type=int, metavar="N")
    parser.add_argument("--concurrency", type=int, metavar="N")
    parser.add_argument("--include", action="append", type=_csv, default=None,
                        metavar="PATTERN", help="force paths in, despite ignores")
    parser.add_argument("--exclude", action="append", type=_csv, default=None,
                        metavar="PATTERN", help="extra gitignore-style excludes")
    parser.add_argument("--per-file", action="store_true", default=None,
                        help="v3 layout: docs/<dir>/<name>.md")
    parser.add_argument("--paid", action="store_true",
                        help="pace to paid-tier rate limits instead of free-tier")
    parser.add_argument("--api-key", default="", help="else $GEMINI_API_KEY etc.")
    parser.add_argument("--name", help="project name for the docs")
    parser.add_argument("--description", help="one-line project description")
    parser.add_argument("--json", action="store_true", help="JSON on stdout")
    parser.add_argument("-q", "--quiet", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devdocs",
        description="Documentation from source code: exact facts, cheap prose.",
    )
    parser.add_argument("--version", action="version", version=f"devdocs {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    generate = commands.add_parser("generate", help="write docs for a project")
    _run_options(generate)
    _force_option(generate)
    generate.add_argument("--no-resume", action="store_true",
                          help="start a fresh run instead of resuming")

    estimate = commands.add_parser("estimate", help="price a run, send nothing")
    _run_options(estimate)
    estimate.add_argument("--explain", action="store_true",
                          help="list every file with its plan and reason")

    export = commands.add_parser(
        "export", help="render the last run again, in other formats; no key, no cost"
    )
    _output_options(export)
    _force_option(export)
    export.add_argument("--json", action="store_true", help="JSON on stdout")
    export.add_argument("-q", "--quiet", action="store_true")

    doctor = commands.add_parser("doctor", help="check that a run can work")
    doctor.add_argument("path", nargs="?", default=None)
    doctor.add_argument("--provider", choices=PROVIDERS)
    doctor.add_argument("--api-key", default="")
    doctor.add_argument("--offline", action="store_true",
                        help="skip the provider check")
    doctor.add_argument("--json", action="store_true")

    init = commands.add_parser("init", help=f"write a starter {CONFIG_FILENAME}")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--provider", choices=PROVIDERS, default="gemini")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")

    models = commands.add_parser("models", help="models your key can use")
    models.add_argument("--provider", choices=PROVIDERS)
    models.add_argument("--api-key", default="")
    models.add_argument("--json", action="store_true")

    cache = commands.add_parser("cache", help="inspect or clear the response cache")
    cache.add_argument("action", choices=("stats", "clear"))
    cache.add_argument("path", nargs="?", default=None)
    cache.add_argument("--json", action="store_true")
    return parser


def _flatten(values: list[list[str]] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    return tuple(item for group in values for item in group)


def _config(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {
        "path": getattr(args, "path", None),
        "out": getattr(args, "out", None),
        "provider": getattr(args, "provider", None),
        "model": getattr(args, "model", None),
        "quality": getattr(args, "quality", None),
        "context": getattr(args, "context", None),
        "max_cost": getattr(args, "max_cost", None),
        "max_requests": getattr(args, "max_requests", None),
        "concurrency": getattr(args, "concurrency", None),
        "include": _flatten(getattr(args, "include", None)),
        "exclude": _flatten(getattr(args, "exclude", None)),
        "formats": _flatten(getattr(args, "formats", None)),
        "pdf_engine": getattr(args, "pdf_engine", None),
        "pdf_split": getattr(args, "split", None),
        "page_size": getattr(args, "page_size", None),
        "per_file": getattr(args, "per_file", None),
        "name": getattr(args, "name", None),
        "description": getattr(args, "description", None),
    }
    if getattr(args, "paid", False):
        overrides["free_tier"] = False
    config = load_config(overrides)
    flags = _pdf_flags(config)
    if flags and "pdf" not in config.formats:
        raise ConfigError(
            f"{', '.join(flags)} {'applies' if len(flags) == 1 else 'apply'} only "
            f"to PDF output, which is not among the formats: "
            f"{', '.join(config.formats)}",
            hint="add --format pdf",
        )
    return config


def _pdf_flags(config: Config) -> list[str]:
    """The PDF options given on the command line, as typed. The file and the
    environment may set them for every PDF a project makes; a flag is meant
    for this one."""
    named = {"pdf_engine": "--pdf-engine", "page_size": "--page-size",
             "pdf_split": "--split" if config.pdf_split else "--no-split"}
    return [flag for name, flag in named.items() if config.origin_of(name) == "flag"]


def _state(config: Config) -> Database:
    return Database(Path(config.path).resolve() / STATE_DIR / "devdocs.db")


def _key(config: Config, explicit: str, out: _Output) -> str:
    from devdocs.demo import DEMO_KEY

    demo = DEMO_KEY if config.provider == "gemini" and DEMO_KEY else None
    key, source = resolve_api_key(config, explicit=explicit, demo_key=demo)
    config.key_source = source
    if source.shared:
        out.warn(
            "using the shared demo key; its free quota is shared by everyone. "
            "Set GEMINI_API_KEY to use your own."
        )
    return key


def _client(
    config: Config, db: Database, out: _Output, *, key: str | None
) -> LLMClient:
    """The model client. With `key=None` it is offline: it can quote prices
    and read the cache, and refuses to send anything."""
    model = make_provider(config.provider, key) if key is not None else OfflineModel(
        "anthropic" if config.provider.startswith("anthropic") else config.provider
    )
    return LLMClient(
        model,
        router=Router(model.provider, quality=config.quality,
                      pinned=config.model or None, free_only=config.free_tier),
        budget=Budget(config.max_cost, config.max_requests),
        cache=ResponseCache(db),
        usage=DailyUsage(db),
        free_tier=config.free_tier,
        on_event=out.event,
    )


# -- commands --------------------------------------------------------------------


def _generate(args: argparse.Namespace, out: _Output) -> int:
    from devdocs.generate import Pipeline

    config = _config(args)
    key = _key(config, args.api_key, out)
    with _state(config) as db:
        client = _client(config, db, out, key=key)
        pipeline = Pipeline(config, client=client, db=db, log=out.info,
                            project=config.name, description=config.description)
        available = client.discover()
        out.info(f"{config.provider}: {len(available)} models available to this key")
        prepared = pipeline.prepare()
        summary = pipeline.run(prepared, resume=not args.no_resume, force=args.force)

    lines = [
        f"{summary.documents} documents ({summary.prose} with prose, "
        f"{summary.stubs} from facts alone) in {config.out}",
        f"{summary.requests} model calls, {summary.cache_hits} cache hits, "
        f"{summary.input_tokens:,} input / {summary.output_tokens:,} output tokens, "
        f"${summary.cost:.4f}",
    ]
    if summary.resumed:
        lines.append(f"resumed run {summary.run_id}")
    if summary.stopped:
        lines.append(f"stopped early ({summary.stopped}); run again to resume")
    for kind, item, error in summary.failed:
        lines.append(f"failed {kind} {item or '(project)'}: {error}")
    if summary.degraded:
        lines.append(f"partial symbol tables: {', '.join(summary.degraded[:10])}")
    lines.extend(_warnings(summary.warnings))
    data = asdict(summary)
    out.result("\n".join(lines), data)

    if summary.stopped == "budget":
        return 7
    if summary.stopped == "quota":
        return 10
    return 4 if summary.failed else 0


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _warnings(warnings: Sequence[str]) -> list[str]:
    """A result line counting rendering warnings, which the log details."""
    if not warnings:
        return []
    return [f"{_count(len(warnings), 'rendering warning')}; --json lists them all"]


def _export(args: argparse.Namespace, out: _Output) -> int:
    from devdocs.generate import Pipeline

    config = _config(args)
    summary = Pipeline(config, log=out.info).export(force=args.force)
    report = summary.written
    run = f" (run of {summary.generated_at})" if summary.generated_at else ""
    lines = [
        f"{_count(summary.documents, 'document')}{run} as "
        f"{', '.join(summary.formats)} in {config.out}: "
        f"{_count(len(report.written), 'file')} written, "
        f"{len(report.unchanged)} unchanged",
    ]
    if len(report.written) <= _LISTED:
        lines += [f"  {Path(config.out, path).as_posix()}" for path in report.written]
    lines.extend(_warnings(summary.warnings))
    out.result("\n".join(lines), asdict(summary))
    return 0


def _estimate(args: argparse.Namespace, out: _Output) -> int:
    from devdocs.generate import Pipeline

    config = _config(args)
    with _state(config) as db:
        client = _client(config, db, out, key=None)
        pipeline = Pipeline(config, client=client, db=db, log=out.info,
                            project=config.name, description=config.description)
        prepared = pipeline.prepare()
        estimate = pipeline.estimate(prepared)

    plan = prepared.plan
    by_kind: dict[str, int] = {}
    for item in estimate.items:
        if not item.cached:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + item.requests
    lines = [
        f"{len(plan.files)} files: {len(plan.prose_files())} with prose, "
        f"{len(plan.files) - len(plan.prose_files())} from facts alone",
        f"{estimate.requests} model calls "
        f"({', '.join(f'{n} {k}' for k, n in sorted(by_kind.items())) or 'none'}), "
        f"{estimate.cached} already cached",
        f"at most {estimate.input_tokens:,} input / {estimate.output_tokens:,} output "
        f"tokens, at most ${estimate.cost:.4f}",
    ]
    if config.free_tier and estimate.requests:
        days = _free_days(config, estimate.requests)
        if days > 1:
            lines.append(f"free tier: about {days} days of daily quota; "
                         "runs resume where they stop")
    if estimate.demoted:
        lines.append(f"{len(estimate.demoted)} items left as stubs to fit "
                     f"--max-cost ${config.max_cost:g}")
    if args.explain:
        lines.append("")
        for entry in plan.files:
            lines.append(f"  {entry.mode.value:<5} {entry.score:7.2f}  {entry.relpath}"
                         f"  ({entry.reason})")
    out.result("\n".join(lines), {
        "files": len(plan.files),
        "prose_files": len(plan.prose_files()),
        "requests": estimate.requests,
        "cached": estimate.cached,
        "input_tokens": estimate.input_tokens,
        "output_tokens": estimate.output_tokens,
        "max_cost_usd": round(estimate.cost, 6),
        "demoted": [list(item) for item in estimate.demoted],
        "items": [asdict(item) for item in estimate.items],
    })
    return 0


def _free_days(config: Config, requests: int) -> int:
    file_models = [m for m in CATALOG
                   if m.provider == config.provider and m.tier is Tier.FILE]
    daily = file_models[0].free_rpd if file_models else None
    if not daily:
        return 0
    return -(-requests // daily)


def _doctor(args: argparse.Namespace, out: _Output) -> int:
    from devdocs.capabilities import Capability, probe_all
    from devdocs.render.pdf.external import available_engines

    report = probe_all()
    engines = available_engines()   # builtin always among them
    found = ", ".join(name if name == "builtin" else f"{name} ({path})"
                      for name, path in engines.items())
    lines = [str(report), str(Capability("pdf-engines", True, found)), ""]
    data: dict[str, Any] = {"capabilities": [asdict(c) for c in report.capabilities],
                            "pdf_engines": engines}
    blocking = not report.ok

    try:
        config = _config(args)
    except ConfigError as err:
        lines.append(f"!! config               {err}")
        out.result("\n".join(lines), {**data, "config": str(err)})
        return 2
    lines.append(f"ok config               provider={config.provider} "
                 f"quality={config.quality} context={config.context} "
                 f"max_cost=${config.max_cost:g} formats={','.join(config.formats)}")
    data["formats"] = list(config.formats)
    data["pdf_engine"] = {"configured": config.pdf_engine,
                          "path": engines.get(config.pdf_engine)}
    if "pdf" in config.formats and config.pdf_engine not in engines:
        lines.append(f"!! pdf engine           {config.pdf_engine} is configured "
                     "but not installed; PDF output will fail")
        blocking = True
    age = (date.today() - date.fromisoformat(PRICES_CHECKED)).days
    if age > 180:
        lines.append(f"!! prices               checked {PRICES_CHECKED}, {age} days "
                     "ago; verify against the vendor's pricing page")

    try:
        key = _key(config, args.api_key, out)
        source = config.key_source
        where = f"{source.origin} {source.detail}".strip() if source else "?"
        lines.append(f"ok api key              from {where}"
                     + (" (shared demo key)" if source and source.shared else ""))
    except ConfigError as err:
        lines.append(f"!! api key              {err.message}")
        key = ""
        blocking = True

    if key and not args.offline:
        try:
            model = make_provider(config.provider, key)
            available = set(model.list_models())
            reachable = catalogued(available)
            known = [m.id for m in CATALOG
                     if m.provider == model.provider and m.id in reachable]
            lines.append(f"ok provider             {len(available)} models; "
                         f"catalogued: {', '.join(known) or 'none'}")
            data["models"] = sorted(available)
        except DevDocsError as err:
            lines.append(f"!! provider             {err}")
            blocking = True

    out.result("\n".join(lines), data)
    return 1 if blocking else 0


_TEMPLATE = """\
# devdocs configuration. Command-line flags override this file.
[devdocs]
provider = "{provider}"       # gemini | anthropic | anthropic-sdk
quality = "cheap"            # cheap | balanced | high
# context = "standard"       # off | standard | semantic (Gemini embeddings)
max_cost = 0.50              # USD ceiling per run; 0 disables it
out = "docs"
# formats = ["markdown"]     # any of markdown, html, pdf
# pdf_engine = "builtin"     # builtin, or chrome | weasyprint for all of Unicode
# pdf_split = false          # one PDF per document instead of one book
# page_size = "A4"           # A4 | Letter
# model = ""                 # pin one model for every call
# include = ["scripts"]      # force paths in despite ignores
# exclude = ["examples/"]    # extra gitignore-style excludes
# free_tier = true           # pace requests to free-tier limits
"""


def _init(args: argparse.Namespace, out: _Output) -> int:
    root = Path(args.path).resolve()
    target = root / CONFIG_FILENAME
    if target.exists() and not args.force:
        raise ConfigError(f"{target} already exists", hint="pass --force to replace it")
    target.write_text(_TEMPLATE.format(provider=args.provider), "utf-8")
    out.info(f"wrote {target}")

    ignore = root / ".gitignore"
    if ignore.is_file():
        text = ignore.read_text("utf-8", errors="replace")
        if f"{STATE_DIR}/" not in text.split():
            joiner = "" if text.endswith("\n") or not text else "\n"
            ignore.write_text(f"{text}{joiner}{STATE_DIR}/\n", "utf-8")
            out.info(f"added {STATE_DIR}/ to .gitignore (local cache and run ledger)")
    out.result(f"next: devdocs estimate {args.path}", {"config": str(target)})
    return 0


def _models(args: argparse.Namespace, out: _Output) -> int:
    config = _config(args)
    key = _key(config, args.api_key, out)
    model = make_provider(config.provider, key)
    available = set(model.list_models())
    reachable = catalogued(available)
    rows: list[dict[str, Any]] = []
    for info in CATALOG:
        if info.provider != model.provider:
            continue
        rows.append({
            "id": info.id, "tier": info.tier.value, "available": info.id in reachable,
            "input_per_mtok": info.input_per_mtok,
            "output_per_mtok": info.output_per_mtok,
            "free_rpm": info.free_rpm, "free_rpd": info.free_rpd,
        })
    lines = [f"{'model':<26} {'tier':<13} {'access':<7} {'$/Mtok in':>9} {'out':>7}"]
    for row in rows:
        lines.append(
            f"{row['id']:<26} {row['tier']:<13} "
            f"{'yes' if row['available'] else 'no':<7} "
            f"{row['input_per_mtok']:>9.2f} {row['output_per_mtok']:>7.2f}"
        )
    others = len([m for m in available if not catalogued({m})])
    lines.append(f"(+{others} other models; pin one with --model)")
    out.result("\n".join(lines), {"catalogued": rows, "available": sorted(available)})
    return 0


def _cache(args: argparse.Namespace, out: _Output) -> int:
    config = load_config({"path": args.path})
    with _state(config) as db:
        cache = ResponseCache(db)
        if args.action == "clear":
            removed = cache.clear()
            out.result(f"removed {removed} cached responses", {"removed": removed})
            return 0
        entries, cost = cache.stats()
    out.result(
        f"{entries} cached responses, bought for ${cost:.4f} in total",
        {"entries": entries, "cost_usd": cost},
    )
    return 0


_HANDLERS = {
    "generate": _generate,
    "estimate": _estimate,
    "export": _export,
    "doctor": _doctor,
    "init": _init,
    "models": _models,
    "cache": _cache,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the `devdocs` script. Returns the process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    from devdocs.legacy import is_v3_invocation, notice, translate

    if is_v3_invocation(arguments):
        print(notice(arguments), file=sys.stderr)
        arguments = translate(arguments)

    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 0

    out = _Output(as_json=getattr(args, "json", False),
                  quiet=getattr(args, "quiet", False))
    try:
        return _HANDLERS[args.command](args, out)
    except KeyboardInterrupt:
        out.warn("interrupted; documents written so far are kept, run again to resume")
        return 130
    except AuthError as err:
        out.warn(str(err))
        return err.exit_code
    except DevDocsError as err:
        print(f"devdocs: {err}", file=sys.stderr)
        return err.exit_code
    except Exception as err:   # a bug: say so plainly, never as a clean stop
        print(f"devdocs: internal error: {type(err).__name__}: {err}", file=sys.stderr)
        if os.environ.get("DEVDOCS_DEBUG"):
            raise
        return exit_code_for(err)
