"""v3 compatibility: the old flags, translated into a `devdocs generate` call.

devDocs 3 was one script driven by flags; v4 has subcommands. Rather than
keep the v3 implementation alive, an invocation that looks like v3 is
rewritten into its v4 equivalent and runs through the same pipeline, after
one deprecation notice. A command line led by a v4 command is never
rewritten, and arguments v3 lacked are passed on rather than dropped, so a
line meant for v4 cannot turn into a paid run over the current directory.
`--per-file` keeps the v3 output layout.

Three flags change meaning, deliberately. `--include` and `--exclude` were
inverted in v3 (an include landed on the skip list); they now do what their
names say. `--overwrite` wrote a README.md into every directory; v4 never
touches files it did not generate, so the flag is accepted and ignored, as
are `--authors` and `--keywords`, which have no v4 use.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

__all__ = ["V3_ONLY_FLAGS", "is_v3_invocation", "main", "notice", "translate"]

# Flags only v3 had: their presence identifies a v3 command line.
V3_ONLY_FLAGS = frozenset({
    "-p", "--path", "--output", "--overwrite", "--authors", "--keywords",
})


def _v3_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)  # -h is v4's
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


def _read(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """v3's reading of `argv`, and the arguments it does not know. A line it
    cannot read exits 2, as in v3."""
    return _v3_parser().parse_known_args(list(argv))


def _is_v4(argv: Sequence[str]) -> bool:
    """True when `argv` starts with a v4 command, as no v3 line can: v3 took
    no positional arguments. The commands are `devdocs.cli`'s own."""
    from devdocs.cli import COMMANDS  # not at the top: cli imports this module

    return bool(argv) and argv[0] in COMMANDS


def _v3_reads(argv: Sequence[str]) -> bool:
    """True when v3's parser takes any of `argv` for an option of its own,
    in full or abbreviated."""
    _, unknown = _read(argv)
    return len(unknown) < len(argv)


def is_v3_invocation(argv: Sequence[str]) -> bool:
    """True when `argv` has no v4 subcommand and uses a flag only v3 had."""
    return not _is_v4(argv) and any(
        arg.split("=", 1)[0] in V3_ONLY_FLAGS for arg in argv
    )


def translate(argv: Sequence[str]) -> list[str]:
    """The v4 arguments equivalent to a v3 command line. A v4 command line
    comes back as it is; arguments v3 lacked follow the translation, for v4
    to apply or reject."""
    if _is_v4(argv):
        return list(argv)
    args, rest = _read(argv)
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
    return [*translated, *rest]


def notice(argv: Sequence[str]) -> str:
    """The deprecation notice for the v3 command line `argv`: the v4 command
    it runs."""
    return ("devdocs: v3-style flags are deprecated; running "
            f"`devdocs {' '.join(translate(argv))}`")


def main(argv: Sequence[str] | None = None) -> int:
    """The v3 entry point, kept for callers that import it directly. A line
    v3 would have read at least in part (an empty one, or one with any of
    v3's options) runs translated, after the notice; any other line goes to
    `cli.main` as it is."""
    from devdocs import cli  # not at the top: cli imports this module

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (not _is_v4(arguments) and _v3_reads(arguments)):
        print(notice(arguments), file=sys.stderr)
        arguments = translate(arguments)
    return cli.main(arguments)
