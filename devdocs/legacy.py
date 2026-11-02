"""v3 compatibility: the old flags, translated into a `devdocs generate` call.

devDocs 3 was one script driven by flags; v4 has subcommands. Rather than
keep the v3 implementation alive, an invocation that looks like v3 is
rewritten into its v4 equivalent and runs through the same pipeline, after
one deprecation notice. A command line led by a v4 command is never
rewritten, and arguments v3 lacked are passed on rather than dropped, so a
line meant for v4 cannot turn into a paid run over the current directory.

The pages go where v3 put them, in its per-file layout: under `--output` in
the current directory, at the project's path from there, so `-p app` writes
docs/app/ and leaves app/docs/ alone. Nothing is forced, by `--overwrite` or
otherwise. v4 never replaces a file it has no record of writing, and v3 kept
no record, so over a v3 tree the run stops before it buys anything and names
the pages; `--force` then takes them over. Forcing every run would also
replace hand-written files at paths only v4 writes (architecture.md,
index.md, index.json), which v3 never touched.

Three flags change meaning, deliberately. `--include` and `--exclude` were
inverted in v3 (an include landed on the skip list); they now do what their
names say. `--overwrite` wrote each README page (the project summary, and
that of any file or folder with README in its name) over the README.md of
the source directory being read, instead of into the output directory; v4
writes only inside its output directory, so the flag is accepted and no
longer applies. `--authors` and `--keywords` are accepted and ignored, having
no v4 use. An empty `-p` or `--output`, on which v3 crashed before writing a
page, exits 2. So does a line whose output lands on the project root or above
it (`-p . --output .`), where v3 mixed its pages with the project's own files:
config refuses such an out.

A `~` in `-p` or `--output` means what it means to config, where no shell
expanded it, as none does in `--path=~/app`.

An API key that a flag names on the command line, however it is spelled, or
that an assignment names as a shell would, is masked in the command the notice
shows and in every argparse error, v3's and v4's alike, both parsers being
a `KeyMaskingParser`. A key nothing names looks like any other argument, and
is shown like one.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, NoReturn

__all__ = [
    "V3_ONLY_FLAGS",
    "KeyMaskingParser",
    "holds_key",
    "is_v3_invocation",
    "main",
    "notice",
    "translate",
]

# Flags only v3 had: their presence identifies a v3 command line.
V3_ONLY_FLAGS = frozenset({
    "-p", "--path", "--output", "--overwrite", "--authors", "--keywords",
})

# How a flag meant for a key ends, its dashes and case aside; see `_names_key`.
_KEY_FLAGS = ("key", "keys", "token", "tokens", "secret", "secrets")


def _directory(value: str) -> str:
    """A `-p` or `--output` value. v3 crashed on an empty one; v4 would read
    it as the current directory, and document it or write over it."""
    if not value:
        raise argparse.ArgumentTypeError("expected a directory, got an empty string")
    return value


class KeyMaskingParser(argparse.ArgumentParser):
    """An argparse parser whose errors show no API key the line names. Each
    key `_masked` finds in the arguments it parsed is masked where a message has
    it: in the unrecognised arguments it lists, an ambiguous `--a=KEY`
    (--authors or --api-key in v3) it names, or a value it refuses, as
    `devdocs --api-key KEY generate` makes KEY the command. Its subparsers
    are of this class too."""

    _arguments: Sequence[str] = ()

    def parse_known_args(
        self, args: Iterable[str] | None = None, namespace: Any = None
    ) -> tuple[Any, list[str]]:
        """Parse as argparse does, keeping the arguments for `error`."""
        self._arguments = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(self._arguments, namespace)

    def error(self, message: str) -> NoReturn:
        """Exit 2 as argparse does, every key in `message` masked."""
        super().error(_conceal(message, self._arguments))


def _v3_parser() -> argparse.ArgumentParser:
    parser = KeyMaskingParser(prog="devdocs", add_help=False)  # -h is v4's
    parser.add_argument("-p", "--path", default=".", type=_directory)
    parser.add_argument("--name")
    parser.add_argument("--description")
    parser.add_argument("--authors")
    parser.add_argument("--keywords")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--output", default="docs", type=_directory)
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


def _place(path: str) -> str:
    """Where v3 put project `path`'s pages in its output directory: '' for
    the current directory, else the path from there. v3 cut the current
    directory's text out of the project's own, so a path outside it keeps
    its full name: `-p /srv/app` wrote docs/srv/app."""
    path = os.path.expanduser(path)
    # v3's getcwd() inside the project: the real path, but as typed on Windows
    inside = os.path.abspath(path) if os.name == "nt" else os.path.realpath(path)
    return inside.replace(os.getcwd(), "").lstrip("\\/").replace("\\", "/")


def translate(argv: Sequence[str]) -> list[str]:
    """The v4 arguments equivalent to a v3 command line. A v4 command line
    comes back as it is; arguments v3 lacked follow the translation, for v4
    to apply or reject. `--out` is v3's output directory, absolute unless the
    project is `.`, since v4 reads it from the project and v3 from here."""
    if _is_v4(argv):
        return list(argv)
    args, rest = _read(argv)
    out = args.output
    if args.path != ".":
        out = str(Path.cwd() / os.path.expanduser(args.output) / _place(args.path))
    translated = ["generate", args.path, "--per-file", "--out", out]
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


def _flat(arg: str) -> str:
    """`arg` in lower case, its letters and digits alone."""
    return "".join(filter(str.isalnum, arg)).lower()


def _names_key(arg: str) -> bool:
    """True for a flag a key may follow: `--api-key`, in full or abbreviated
    as argparse accepts it, and as it is mistyped or misremembered, in any
    case, with any dashes, `_` or `.`: `-api_key`, `--GEMINI_API_KEY`,
    `--token`. Masking one more value in a message costs nothing."""
    flat = _flat(arg)
    return (arg.startswith("-") and len(arg) > 2 and bool(flat)
            and ("apikey".startswith(flat) or flat.endswith(_KEY_FLAGS)))


def _hidden(arg: str) -> str:
    """`arg`, its key masked if it is `--api-key=KEY` in any spelling, or
    `GEMINI_API_KEY=KEY`, as a line meant for a shell has it."""
    name, equals, _ = arg.partition("=")
    named = _names_key(name) or _flat(name).endswith(_KEY_FLAGS)
    return f"{name}=***" if equals and named else arg


def holds_key(path: str) -> bool:
    """True for a project path that can only be a key gone astray, as `--`
    makes `--api-key=KEY` one, and a line meant for a shell `GEMINI_API_KEY=KEY`.
    `./key=1` is a path, as is a directory named `key=1`."""
    name = path.partition("=")[0]
    return (_hidden(path) != path and Path(name).name == name
            and not os.path.isdir(path))


def _masked(arguments: Sequence[str]) -> list[str]:
    """`arguments` with every API key as ***: after '=', or apart from its
    flag, even by a `--`, which argparse would refuse but still list."""
    masked: list[str] = []
    key_next = False
    for arg in arguments:
        if key_next and arg == "--":
            masked.append(arg)
            continue
        masked.append("***" if key_next else _hidden(arg))
        key_next = _names_key(arg)
    return masked


def _conceal(message: str, arguments: Sequence[str]) -> str:
    """`message`, an argparse error, with each argument that `_masked` masks
    in `arguments` masked here too: quoted, as argparse quotes a value it
    refuses, or whole between spaces, as it lists unrecognised arguments."""
    pairs = zip(arguments, _masked(arguments), strict=True)
    hidden = {arg: shown for arg, shown in pairs if arg and arg != shown}
    if not hidden:
        return message
    for arg, shown in hidden.items():
        message = message.replace(repr(arg), repr(shown))
    words = "|".join(map(re.escape, sorted(hidden, key=len, reverse=True)))
    return re.sub(rf"(?<!\S)(?:{words})(?!\S)",
                  lambda match: hidden[match.group()], message)


def notice(argv: Sequence[str]) -> str:
    """The deprecation notice for the v3 command line `argv`: the v4 command
    it runs, API key masked, and what becomes of pages already there."""
    args, _ = _read(argv)
    command = shlex.join(_masked(translate(argv)))
    docs = os.path.join(args.output, _place(args.path), "")
    dropped = "--overwrite no longer applies; " if args.overwrite else ""
    return (
        f"devdocs: v3-style flags are deprecated; running `devdocs {command}`\n"
        f"devdocs: {dropped}v4 never replaces a page it has no record of "
        f"writing: if {docs} still holds v3 pages, add --force once to take "
        "them over"
    )


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
