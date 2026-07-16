"""v3 compatibility: the old flags, translated into a `devdocs generate` call.

devDocs 3 was one script driven by flags; v4 has subcommands. Rather than
keep the v3 implementation alive, an invocation that looks like v3 is
rewritten into its v4 equivalent and runs through the same pipeline, after
one deprecation notice. `--per-file` keeps the v3 output layout.

Three flags change meaning, deliberately. `--include` and `--exclude` were
inverted in v3 (an include landed on the skip list); they now do what their
names say. `--overwrite` wrote a README.md into every directory; v4 never
touches files it did not generate, so the flag is accepted and ignored, as
are `--authors` and `--keywords`, which have no v4 use.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

__all__ = ["V3_ONLY_FLAGS", "is_v3_invocation", "main", "translate"]

# Flags only v3 had: their presence identifies a v3 command line.
V3_ONLY_FLAGS = frozenset({
    "-p", "--path", "--output", "--overwrite", "--authors", "--keywords",
})

_V4_COMMANDS = frozenset({
    "generate", "estimate", "doctor", "init", "models", "cache",
})


def _v3_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--path", default=".")
    parser.add_argument("--name")
    parser.add_argument("--description")
    parser.add_argument("--authors")
    parser.add_argument("--keywords")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--output", default="docs")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--include", default="")
    parser.add_argument("--api-key", default="")
    return parser


def is_v3_invocation(argv: Sequence[str]) -> bool:
    """True when `argv` has no v4 subcommand and uses a flag only v3 had."""
    if not argv or argv[0] in _V4_COMMANDS:
        return False
    return any(arg.split("=", 1)[0] in V3_ONLY_FLAGS for arg in argv)


def translate(argv: Sequence[str]) -> list[str]:
    """The v4 arguments equivalent to a v3 command line."""
    args, _ = _v3_parser().parse_known_args(list(argv))
    translated = ["generate", args.path, "--per-file", "--out", args.output]
    if args.include:
        translated += ["--include", args.include]
    if args.exclude:
        translated += ["--exclude", args.exclude]
    if args.api_key:
        translated += ["--api-key", args.api_key]
    if args.name and args.name != "My Project":   # v3's placeholder default
        translated += ["--name", args.name]
    if args.description and args.description != "No description provided.":
        translated += ["--description", args.description]
    return translated


def main() -> int:
    """The v3 entry point, kept for callers that import it directly."""
    import sys

    from devdocs.cli import main as cli_main

    argv = sys.argv[1:]
    if not is_v3_invocation(argv) and (not argv or argv[0] not in _V4_COMMANDS):
        argv = translate(argv)
    return cli_main(argv)
