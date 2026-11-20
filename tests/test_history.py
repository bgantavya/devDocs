"""GitHistory: how git is run, how its output is read, and how it fails.

A fake runner plays git for the parsing and failure cases. One test drives a
real git binary against a throwaway repository, isolated from the developer's
own git configuration, so the command line is proven against the real tool.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from devdocs.history import FileHistory, GitHistory

SHA1 = "3f786850e387550fdab836ed7e6dc881de23001b"
SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
PREFIX = ["git", "-C", "repo", "-c", "core.quotePath=false"]
LOG = ["log", "--since=90.days", "--no-merges", "--relative", "--name-only",
       "--format=%x1e%ae"]

Reply = tuple[int, str] | BaseException


class FakeGit:
    """Answers each git command from a table keyed by the command line after
    the fixed prefix; an exception in the table is raised instead."""

    def __init__(self, replies: Mapping[str, Reply]) -> None:
        self.replies = dict(replies)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self, args: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), options))
        reply = self.replies[" ".join(args[len(PREFIX) :])]
        if isinstance(reply, BaseException):
            raise reply
        code, stdout = reply
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")


def history(replies: Mapping[str, Reply]) -> tuple[GitHistory, FakeGit]:
    fake = FakeGit(replies)
    return GitHistory(Path("repo"), runner=fake), fake


class CommandTest(unittest.TestCase):
    def test_git_runs_under_root_with_fixed_options(self) -> None:
        fake = FakeGit({"rev-parse --is-inside-work-tree": (0, "true\n")})
        git = GitHistory(Path("repo"), git="/opt/git", timeout=5.0, runner=fake)
        self.assertTrue(git.available)
        self.assertEqual(
            fake.calls,
            [
                (
                    ["/opt/git", *PREFIX[1:], "rev-parse", "--is-inside-work-tree"],
                    {
                        "capture_output": True,
                        "text": True,
                        "encoding": "utf-8",
                        "errors": "replace",
                        "timeout": 5.0,
                        "stdin": subprocess.DEVNULL,
                        "check": False,
                    },
                )
            ],
        )

    def test_available_is_asked_once(self) -> None:
        git, fake = history({"rev-parse --is-inside-work-tree": (0, "true\n")})
        self.assertTrue(git.available)
        self.assertTrue(git.available)
        self.assertEqual(len(fake.calls), 1)

    def test_not_available_outside_a_work_tree(self) -> None:
        for reply in [(0, "false\n"), (128, ""), (0, "")]:
            git, _ = history({"rev-parse --is-inside-work-tree": reply})
            self.assertFalse(git.available, reply)

    def test_head_is_a_full_sha_or_empty(self) -> None:
        cases: list[tuple[Reply, str]] = [
            ((0, SHA1 + "\n"), SHA1),
            ((0, SHA256 + "\n"), SHA256),
            ((128, "HEAD\n"), ""),             # unborn HEAD: no commit yet
            ((0, "not-a-sha\n"), ""),
            ((0, SHA1[:12] + "\n"), ""),
        ]
        for reply, expected in cases:
            git, _ = history({"rev-parse HEAD": reply})
            self.assertEqual(git.head(), expected, reply)


class RecentTest(unittest.TestCase):
    def test_counts_commits_and_distinct_authors_per_path(self) -> None:
        output = (
            "\x1eada@example.com\n\nsrc/app.py\nREADME.md\n"
            "\x1eBob@Example.com\n\nsrc/app.py\nsrc/util.py\n"
            "\x1ebob@example.com\n\nsrc/app.py\n"
        )
        git, fake = history({" ".join(LOG): (0, output)})
        found = git.recent()
        self.assertEqual(
            found,
            {
                "README.md": FileHistory(commits=1, authors=1),
                "src/app.py": FileHistory(commits=3, authors=2),
                "src/util.py": FileHistory(commits=1, authors=1),
            },
        )
        self.assertEqual(list(found), ["README.md", "src/app.py", "src/util.py"])
        self.assertEqual(fake.calls[0][0], [*PREFIX, *LOG])

    def test_the_window_is_whole_days_never_negative(self) -> None:
        for days, since in [(7, "--since=7.days"), (-5, "--since=0.days")]:
            command = " ".join(["log", since, *LOG[2:]])
            git, fake = history({command: (0, "")})
            self.assertEqual(git.recent(days), {})
            self.assertEqual(fake.calls[0][0][len(PREFIX) + 1], since)

    def test_paths_git_still_quotes_are_unquoted(self) -> None:
        output = (
            "\x1eada@example.com\n\n"
            '"docs/na\\303\\257ve.md"\n'
            '"say \\"hi\\".md"\n'
            '"tab\\there.txt"\n'
            '"back\\\\slash.txt"\n'
            "docs/ü.md\n"
            '"odd\\qescape"\n'
        )
        git, _ = history({" ".join(LOG): (0, output)})
        self.assertEqual(
            sorted(git.recent()),
            sorted(
                [
                    "docs/naïve.md",
                    'say "hi".md',
                    "tab\there.txt",
                    "back\\slash.txt",
                    "docs/ü.md",
                    "oddqescape",
                ]
            ),
        )

    def test_a_path_listed_twice_in_one_commit_counts_once(self) -> None:
        output = "\x1eada@example.com\n\na.py\na.py\n\x1eada@example.com\n\n"
        git, _ = history({" ".join(LOG): (0, output)})
        self.assertEqual(git.recent(), {"a.py": FileHistory(commits=1, authors=1)})

    def test_text_before_the_first_record_is_ignored(self) -> None:
        output = "warning: something\n\x1eada@example.com\n\na.py\n"
        git, _ = history({" ".join(LOG): (0, output)})
        self.assertEqual(git.recent(), {"a.py": FileHistory(commits=1, authors=1)})


class FailureTest(unittest.TestCase):
    def test_a_missing_slow_or_failing_git_gives_empty_answers(self) -> None:
        failures: list[Reply] = [
            FileNotFoundError("git"),
            PermissionError("denied"),
            subprocess.TimeoutExpired(["git"], 30.0),
            (128, "fatal: not a git repository\n"),
        ]
        for failure in failures:
            replies = {
                "rev-parse --is-inside-work-tree": failure,
                "rev-parse HEAD": failure,
                " ".join(LOG): failure,
            }
            git, _ = history(replies)
            self.assertFalse(git.available, failure)
            self.assertEqual(git.head(), "", failure)
            self.assertEqual(git.recent(), {}, failure)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RealGitTest(unittest.TestCase):
    """A throwaway repository; HOME and git config point into it, so the
    developer's own settings (signing, hooks, templates) cannot leak in."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        home = self.base / "home"
        home.mkdir()
        config = home / "gitconfig"
        config.write_text("", encoding="utf-8")
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.env.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home),
            GIT_CONFIG_GLOBAL=str(config),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CEILING_DIRECTORIES=str(self.base),
        )
        self.root = self.base / "repo"
        self.root.mkdir()

    def runner(
        self, args: list[str], **options: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, env=self.env, **options)

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env, capture_output=True, text=True, check=True,
        )
        return done.stdout

    def commit(self, files: Mapping[str, str], name: str, email: str) -> None:
        for relpath, text in files.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git(
            "-c", f"user.name={name}", "-c", f"user.email={email}",
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"work by {name}",
        )

    def test_counts_commits_and_authors_in_a_real_repository(self) -> None:
        self.git("init", "-q")
        self.commit({"src/app.py": "print(1)\n", "README.md": "# app\n"},
                    "Ada", "ada@example.com")
        self.commit({"src/app.py": "print(2)\n", "src/util.py": "x = 1\n"},
                    "Bob", "bob@example.com")

        git = GitHistory(self.root, runner=self.runner)
        self.assertTrue(git.available)
        self.assertEqual(git.head(), self.git("rev-parse", "HEAD").strip())
        self.assertEqual(
            git.recent(),
            {
                "README.md": FileHistory(commits=1, authors=1),
                "src/app.py": FileHistory(commits=2, authors=2),
                "src/util.py": FileHistory(commits=1, authors=1),
            },
        )
        inner = GitHistory(self.root / "src", runner=self.runner)
        self.assertEqual(
            inner.recent(),
            {
                "app.py": FileHistory(commits=2, authors=2),
                "util.py": FileHistory(commits=1, authors=1),
            },
        )

    def test_an_unborn_head_and_a_plain_directory_give_empty_answers(self) -> None:
        self.git("init", "-q")
        fresh = GitHistory(self.root, runner=self.runner)
        self.assertTrue(fresh.available)
        self.assertEqual((fresh.head(), fresh.recent()), ("", {}))
        plain = GitHistory(self.base / "home", runner=self.runner)
        self.assertFalse(plain.available)
        self.assertEqual((plain.head(), plain.recent()), ("", {}))


if __name__ == "__main__":
    unittest.main()
