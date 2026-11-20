"""What git knows about a project: recent activity per file, and HEAD.

Files that change often and are touched by several people are where readers
need docs most, so recent activity feeds the planner's score. Git is optional:
without it, or outside a work tree, every query returns an empty answer and
the planner scores on static facts alone.

Git runs as a subprocess rather than through a library, which keeps the
zero-dependency promise and makes every failure mode (missing binary, not a
repository, a hung command) look the same: no answer. `core.quotePath=false`
keeps non-ASCII paths readable; the few paths git still quotes are unquoted
here, so history keys match the scanner's relpaths.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["FileHistory", "GitHistory"]

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")   # SHA-1, or SHA-256 repos
_RECORD = "\x1e"
_OCTAL = re.compile(r"[0-3][0-7]{2}")                  # one byte, as git writes it
_ESCAPES = {
    "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A, "v": 0x0B, "f": 0x0C, "r": 0x0D,
    '"': 0x22, "\\": 0x5C,
}


@dataclass(frozen=True)
class FileHistory:
    commits: int = 0      # commits touching the file in the window
    authors: int = 0      # distinct author emails in the window


class GitHistory:
    """Read-only queries against the repository containing `root`.

    `runner` is `subprocess.run` in production and a fake in tests. Every
    call has a timeout; a slow or failing git yields an empty answer, never
    an exception, because history only refines the plan.
    """

    def __init__(
        self,
        root: Path,
        *,
        git: str = "git",
        timeout: float = 30.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self.root = root
        self.git = git
        self.timeout = timeout
        self.runner = runner
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        """True when git runs and `root` is inside a work tree."""
        if self._available is None:
            result = self._run("rev-parse", "--is-inside-work-tree")
            self._available = result is not None and result.stdout.strip() == "true"
        return self._available

    def head(self) -> str:
        """The full HEAD commit sha, or "" when unavailable."""
        result = self._run("rev-parse", "HEAD")
        sha = result.stdout.strip() if result is not None else ""
        return sha if _SHA.fullmatch(sha) else ""

    def recent(self, days: int = 90) -> dict[str, FileHistory]:
        """Per-file activity over the last `days`, keyed by path under `root`.

        Merges are skipped: they repeat changes already counted on the
        branch that made them. Author emails are compared case-insensitively.
        """
        result = self._run(
            "log",
            f"--since={max(int(days), 0)}.days",
            "--no-merges",
            "--relative",
            "--name-only",
            "--format=%x1e%ae",
        )
        if result is None:
            return {}
        commits: dict[str, int] = {}
        authors: dict[str, set[str]] = {}
        # Each record is "<email>\n\n<path>\n<path>\n"; text before the first
        # separator is not a record.
        for record in result.stdout.split(_RECORD)[1:]:
            email, *lines = record.split("\n")
            author = email.strip().lower()
            for path in {_unquote(line) for line in lines if line}:
                commits[path] = commits.get(path, 0) + 1
                authors.setdefault(path, set()).add(author)
        return {
            path: FileHistory(commits=commits[path], authors=len(authors[path]))
            for path in sorted(commits)
        }

    def _run(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        """One git command under `root`; None if git is missing, slow or fails."""
        try:
            result = self.runner(
                [self.git, "-C", str(self.root), "-c", "core.quotePath=false", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return result if result.returncode == 0 else None


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path holding quotes, backslashes or
    control characters; any other path is returned unchanged."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    body = path[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 == len(body):
            out += char.encode("utf-8")
            index += 1
            continue
        if _OCTAL.fullmatch(body, index + 1, index + 4):
            out.append(int(body[index + 1 : index + 4], 8))
            index += 4
            continue
        escaped = body[index + 1]
        code = _ESCAPES.get(escaped)
        out += bytes([code]) if code is not None else escaped.encode("utf-8")
        index += 2
    return out.decode("utf-8", errors="replace")
