"""The output writer: atomic writes, quiet re-runs, and pruning that only ever
touches files devdocs itself generated; reading a run back from its
manifest; and the registry of formats."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs import cli
from devdocs.errors import ConfigError
from devdocs.render import (
    FORMATS,
    MANIFEST,
    PAGE_SIZES,
    PDF_ENGINES,
    OutputWriter,
    Renderer,
    WriteReport,
    generated_files,
    read_manifest,
    renderer_for,
)
from devdocs.render.html import HtmlRenderer
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import DocKind, DocSet, Document, RunInfo, Section
from devdocs.render.pdf import PdfRenderer, external, layout
from tests.test_markdown import shop

ODD = b"caf\xe9".decode("utf-8", "surrogateescape")   # a Linux name, as os.walk has it


@contextlib.contextmanager
def unnameable(word: str) -> Iterator[None]:
    """A file system that cannot name a path holding `word`, as macOS cannot
    name bytes that are not UTF-8: making a file or directory there, or
    renaming one to it, fails with EILSEQ, while a lookup finds nothing."""

    def refusing(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            for arg in args:
                if isinstance(arg, (str, os.PathLike)) and word in os.fspath(arg):
                    raise OSError(errno.EILSEQ, os.strerror(errno.EILSEQ),
                                  os.fspath(arg))
            return real(*args, **kwargs)
        return call

    with contextlib.ExitStack() as stack:
        for name in ("open", "mkdir", "replace"):
            stack.enter_context(mock.patch(f"devdocs.render.os.{name}",
                                           side_effect=refusing(getattr(os, name))))
        yield


class WriterCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.out = self.base / "docs"
        self.docset = DocSet(project="demo")

    def write(self, files: dict[str, bytes]) -> WriteReport:
        return OutputWriter(self.out).write(files, self.docset)

    def tree(self) -> dict[str, bytes]:
        """Every file under the out dir, hidden ones included, by POSIX path."""
        return {
            path.relative_to(self.out).as_posix(): path.read_bytes()
            for path in sorted(self.out.rglob("*"))
            if path.is_file()
        }

    def dirs(self) -> list[str]:
        return sorted(
            path.relative_to(self.out).as_posix()
            for path in self.out.rglob("*")
            if path.is_dir()
        )

    def manifest(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.out / MANIFEST).read_bytes())
        return data

    def plant(self, relative: str, data: bytes = b"hand-written\n") -> Path:
        path = self.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class TestWriting(WriterCase):
    def test_first_write_creates_out_and_parents(self) -> None:
        report = self.write({"b.md": b"B\n", "a/x.md": b"X\n"})
        self.assertEqual(report, WriteReport(written=["a/x.md", "b.md", "index.json"]))
        tree = self.tree()
        self.assertEqual(sorted(tree), ["a/x.md", "b.md", "index.json"])
        self.assertEqual((tree["a/x.md"], tree["b.md"]), (b"X\n", b"B\n"))

    def test_manifest_is_the_docset_plus_the_files_it_lists(self) -> None:
        self.docset = DocSet(
            project="café",
            run=RunInfo(generator="devdocs 4.0.0",
                        generated_at="2026-11-01T09:30:00+00:00", requests=3),
        )
        self.write({"b.md": b"B\n", "a/x.md": b"X\n", MANIFEST: b"replaced"})
        expected = self.docset.to_dict(include_run=True)
        expected["files"] = ["a/x.md", "b.md"]
        text = json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
        self.assertEqual((self.out / MANIFEST).read_bytes(), text.encode("utf-8"))
        self.assertIn('"project": "café"', text)
        self.assertEqual(self.manifest()["run"]["requests"], 3)

    def test_undecodable_text_falls_back_to_an_ascii_manifest(self) -> None:
        self.docset = DocSet(project="bad\udcff café")
        self.write({"a.md": b"A\n"})
        raw = (self.out / MANIFEST).read_bytes()
        self.assertIn(b'"project": "bad\\udcff caf\\u00e9"', raw)
        self.assertEqual(self.manifest()["project"], "bad\udcff café")

    def test_unchanged_bytes_are_not_rewritten(self) -> None:
        files = {"a.md": b"A\n", "sub/b.md": b"B\n"}
        self.write(files)
        old = 1_000_000_000
        for relative in ("a.md", "sub/b.md", MANIFEST):
            os.utime(self.out / relative, (old, old))
        report = self.write(files)
        unchanged = ["a.md", "index.json", "sub/b.md"]
        self.assertEqual(report, WriteReport(unchanged=unchanged))
        for relative in ("a.md", "sub/b.md", MANIFEST):
            self.assertEqual((self.out / relative).stat().st_mtime, old)

    def test_changed_bytes_are_replaced_without_leaving_temp_files(self) -> None:
        self.write({"same.md": b"x\n", "size.md": b"short\n", "text.md": b"old\n"})
        report = self.write({"same.md": b"x\n", "size.md": b"longer\n",
                             "text.md": b"new\n"})
        self.assertEqual(report, WriteReport(
            written=["size.md", "text.md"], unchanged=["index.json", "same.md"],
        ))
        tree = self.tree()
        self.assertEqual(sorted(tree), ["index.json", "same.md", "size.md", "text.md"])
        self.assertEqual((tree["size.md"], tree["text.md"]), (b"longer\n", b"new\n"))

    def test_a_failed_write_keeps_the_old_file_and_cleans_up(self) -> None:
        self.write({"a.md": b"old\n"})
        failing = mock.patch("devdocs.render.os.replace", side_effect=OSError("full"))
        with failing, self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"new\n"})
        self.assertEqual(caught.exception.message,
                         f"cannot write a.md in {self.out}: full")
        tree = self.tree()
        self.assertEqual(sorted(tree), ["a.md", "index.json"])
        self.assertEqual(tree["a.md"], b"old\n")

    def test_temp_names_fit_the_limit_when_the_target_name_barely_does(self) -> None:
        name = "é" * 120 + ".md"   # 243 bytes as UTF-8; 255 is the usual cap
        lengths: list[int] = []
        real_open = os.open

        def measured(path: Any, flags: int, mode: int = 0o777) -> int:
            lengths.append(len(os.fsencode(os.path.basename(path))))
            return real_open(path, flags, mode)

        with mock.patch("devdocs.render.os.open", side_effect=measured):
            report = self.write({name: b"E\n"})
        self.assertEqual(report.written, sorted([name, MANIFEST]))
        self.assertEqual((self.out / name).read_bytes(), b"E\n")
        self.assertEqual(len(lengths), 4)   # each probed, then written
        self.assertLessEqual(max(lengths), 255)

    def test_a_temp_name_too_long_where_the_name_fits_is_cut(self) -> None:
        """eCryptfs caps names at 143 bytes and Windows whole paths, so the
        target's name may fit where a temp name holding all of it does not.
        This one fits the cap exactly, so no temp name holding it does,
        whatever the process id."""
        name = "p" * 140 + ".md"
        real_open = os.open

        def capped(path: Any, flags: int, mode: int = 0o777) -> int:
            if len(os.fsencode(os.path.basename(path))) > 143:
                raise OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG), path)
            return real_open(path, flags, mode)

        with mock.patch("devdocs.render.os.open", side_effect=capped):
            report = self.write({name: b"P\n"})
        self.assertEqual(report.written, sorted([name, MANIFEST]))
        self.assertEqual(sorted(self.tree()), sorted([name, MANIFEST]))

    def test_temp_files_never_open_an_existing_name(self) -> None:
        opened: list[tuple[str, int]] = []
        real_open = os.open

        def taken_once(path: Any, flags: int, mode: int = 0o777) -> int:
            opened.append((os.fspath(path), flags))
            if len(opened) == 1:
                raise FileExistsError(path)
            return real_open(path, flags, mode)

        with mock.patch("devdocs.render.os.open", side_effect=taken_once):
            self.write({"a.md": b"A\n"})
        # probes: index.json taken, retried; a.md; then each written
        self.assertEqual(len(opened), 5)
        self.assertNotEqual(opened[0][0], opened[1][0])
        exclusive = os.O_CREAT | os.O_EXCL
        self.assertTrue(all(flags & exclusive == exclusive for _, flags in opened))
        self.assertEqual(sorted(self.tree()), ["a.md", "index.json"])

    def test_a_directory_in_the_way_is_refused_before_anything_is_written(
        self,
    ) -> None:
        (self.out / "a.md").mkdir(parents=True)
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(ConfigError) as caught:
                OutputWriter(self.out).write({"a.md": b"A\n", "b.md": b"B\n"},
                                             self.docset, force=force)
            self.assertIn("a directory", caught.exception.message)
            self.assertTrue(caught.exception.message.endswith(" a.md"))
            self.assertEqual(self.tree(), {})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_new_files_get_the_permissions_of_a_plain_write(self) -> None:
        previous = os.umask(0o022)
        try:
            self.write({"a.md": b"A\n"})
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE((self.out / "a.md").stat().st_mode), 0o644)


class TestValidation(WriterCase):
    def test_unsafe_paths_are_rejected_before_anything_is_written(self) -> None:
        unsafe = [
            "../x.md", "a/../../x.md", "a/../b.md", "/etc/x.md", "a\\b.md", "C:x.md",
            "c:/x.md", "", "a//b.md", "./a.md", "a/./b.md", "a/", "a\x00.md",
        ]
        for path in unsafe:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.write({"ok.md": b"x", path: b"x"})
            self.assertFalse(self.out.exists())

    @unittest.skipIf(os.name == "nt", "Windows file names may hold lone surrogates")
    def test_a_name_the_file_system_cannot_encode_is_rejected_up_front(self) -> None:
        with self.assertRaises(ValueError):
            self.write({"a.md": b"A\n", "z\ud800.md": b"Z\n"})
        self.assertFalse(self.out.exists())

    def test_a_path_another_output_needs_as_a_directory_is_rejected(self) -> None:
        for files in ({"a.md": b"", "a.md/b.md": b""}, {"index.json/x.md": b""}):
            with self.subTest(files=sorted(files)), self.assertRaises(ValueError):
                self.write(files)
        self.assertFalse(self.out.exists())


class TestPruning(WriterCase):
    def test_stale_generated_files_go_and_hand_written_ones_stay(self) -> None:
        self.write({"a.md": b"A\n", "old/gone.md": b"G\n", "keep/x.md": b"X\n"})
        self.plant("notes.md")
        self.plant("keep/mine.md")
        (self.out / "empty").mkdir()
        report = self.write({"a.md": b"A\n"})
        self.assertEqual(report, WriteReport(
            written=["index.json"], unchanged=["a.md"],
            removed=["keep/x.md", "old/gone.md"],
        ))
        self.assertEqual(sorted(self.tree()),
                         ["a.md", "index.json", "keep/mine.md", "notes.md"])
        self.assertEqual(self.dirs(), ["empty", "keep"])
        self.assertEqual(self.manifest()["files"], ["a.md"])

    def test_emptied_directories_go_up_to_the_first_one_still_in_use(self) -> None:
        self.write({"a/b/c/d.md": b"D\n", "x/y/z.md": b"Z\n"})
        self.plant("a/side.md")
        report = self.write({})
        self.assertEqual(report.removed, ["a/b/c/d.md", "x/y/z.md"])
        self.assertEqual(self.dirs(), ["a"])
        self.assertEqual(sorted(self.tree()), ["a/side.md", "index.json"])
        self.assertEqual(self.manifest()["files"], [])

    def test_without_a_manifest_nothing_is_pruned(self) -> None:
        self.plant("old.md")
        report = self.write({"new.md": b"N\n"})
        self.assertEqual(report.removed, [])
        self.assertEqual(sorted(self.tree()), ["index.json", "new.md", "old.md"])

    def test_a_corrupt_or_foreign_manifest_is_not_trusted(self) -> None:
        """Nothing it lists is pruned, and as a file devdocs cannot tell it
        wrote, only `force` replaces it."""
        listed = {"schema_version": "1.0.0", "documents": [], "files": ["old.md"]}
        cases = {
            "not json": b"{oops",
            "not utf-8": b"\x80{}",
            "not an object": json.dumps(["old.md"]).encode(),
            "foreign": json.dumps({"files": ["old.md"]}).encode(),
            "no documents": json.dumps({**listed, "documents": None}).encode(),
            "files not a list": json.dumps({**listed, "files": "old.md"}).encode(),
            "too deep": b"[" * 100_000 + b"]" * 100_000,
        }
        for name, manifest in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.out, ignore_errors=True)
                self.plant("old.md")
                self.plant(MANIFEST, manifest)
                with self.assertRaises(ConfigError) as caught:
                    self.write({"new.md": b"N\n"})
                self.assertTrue(caught.exception.message.endswith(": index.json"))
                self.assertEqual(self.tree(), {"index.json": manifest,
                                               "old.md": b"hand-written\n"})
                report = OutputWriter(self.out).write({"new.md": b"N\n"},
                                                      self.docset, force=True)
                self.assertEqual(report.removed, [])
                self.assertTrue((self.out / "old.md").is_file())
                self.assertEqual(self.manifest()["files"], ["new.md"])
        with self.subTest("a directory"):
            shutil.rmtree(self.out, ignore_errors=True)
            (self.out / MANIFEST).mkdir(parents=True)
            with self.assertRaises(ConfigError):
                OutputWriter(self.out).write({"new.md": b"N\n"}, self.docset,
                                             force=True)
            self.assertEqual(self.tree(), {})

    def test_a_manifest_entry_naming_no_possible_file_is_skipped(self) -> None:
        self.plant("old.md")
        self.plant(MANIFEST, json.dumps({"schema_version": "1.0.0", "documents": [],
                                         "files": ["\ud800.md", "old.md"]}).encode())
        report = self.write({"new.md": b"N\n"})
        self.assertEqual(report.removed, ["old.md"])
        self.assertEqual(self.manifest()["files"], ["new.md"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_only_listed_regular_files_inside_out_are_removed(self) -> None:
        outside = self.base / "outside.md"
        outside.write_bytes(b"precious\n")
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.md").write_bytes(b"precious\n")
        self.plant("listed.md")
        (self.out / "dir.md").mkdir()
        (self.out / "link.md").symlink_to(outside)
        (self.out / "jump").symlink_to(elsewhere, target_is_directory=True)
        self.plant(MANIFEST, json.dumps({"schema_version": "1.0.0", "documents": [],
                                         "files": [
            "../outside.md", str(outside), "dir.md", "link.md", "jump/x.md",
            "missing.md", "a\\b.md", 42, None, "listed.md", "index.json",
        ]}).encode())
        report = self.write({})
        self.assertEqual(report.removed, ["listed.md"])
        self.assertEqual(outside.read_bytes(), b"precious\n")
        self.assertEqual((elsewhere / "x.md").read_bytes(), b"precious\n")
        self.assertTrue((self.out / "dir.md").is_dir())
        self.assertTrue((self.out / "link.md").is_symlink())

    @unittest.skipUnless(hasattr(os, "link"), "needs hard links")
    def test_a_stale_name_for_an_output_file_is_kept(self) -> None:
        """A case-insensitive disk makes `Page.md` and `page.md` one file; a
        hard link reproduces that sameness on any disk."""
        self.write({"alias.md": b"P\n"})
        os.link(self.out / "alias.md", self.out / "page.md")
        report = self.write({"page.md": b"P\n"})
        self.assertEqual(report, WriteReport(written=["index.json"],
                                             unchanged=["page.md"]))
        self.assertTrue((self.out / "alias.md").is_file())
        self.assertEqual(self.manifest()["files"], ["page.md"])

    def test_a_file_that_cannot_be_removed_is_skipped(self) -> None:
        self.write({"a.md": b"A\n", "b.md": b"B\n"})
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            report = self.write({"b.md": b"B\n"})
        self.assertEqual(report.removed, [])
        self.assertTrue((self.out / "a.md").is_file())


class TestKeeping(WriterCase):
    def keep(self, files: dict[str, bytes]) -> WriteReport:
        return OutputWriter(self.out).write(files, self.docset, prune=False)

    def test_earlier_files_stay_and_stay_listed(self) -> None:
        self.write({"a.md": b"A\n", "sub/b.md": b"B\n"})
        report = self.keep({"book.pdf": b"%PDF\n", "a.md": b"A\n"})
        self.assertEqual(report, WriteReport(written=["book.pdf", "index.json"],
                                             unchanged=["a.md"]))
        self.assertEqual(sorted(self.tree()),
                         ["a.md", "book.pdf", "index.json", "sub/b.md"])
        self.assertEqual(self.manifest()["files"], ["a.md", "book.pdf", "sub/b.md"])

    def test_only_files_still_there_stay_listed(self) -> None:
        self.write({"a.md": b"A\n", "gone.md": b"G\n", "moved.md": b"M\n"})
        (self.out / "gone.md").unlink()
        (self.out / "moved.md").unlink()
        (self.out / "moved.md").mkdir()
        self.keep({"book.pdf": b"%PDF\n"})
        self.assertEqual(self.manifest()["files"], ["a.md", "book.pdf"])

    def test_a_later_pruning_write_still_removes_what_was_kept(self) -> None:
        self.write({"a.md": b"A\n"})
        self.keep({"book.pdf": b"%PDF\n"})
        report = self.write({"a.md": b"A\n"})
        self.assertEqual(report.removed, ["book.pdf"])
        self.assertEqual(sorted(self.tree()), ["a.md", "index.json"])


class TestConflicts(WriterCase):
    """A file devdocs did not write, where an output goes, is never replaced
    or later pruned without `force`."""

    def force(self, files: dict[str, bytes]) -> WriteReport:
        return OutputWriter(self.out).write(files, self.docset, force=True)

    def assert_refused(self, files: dict[str, bytes], *paths: str) -> None:
        before = self.tree()
        with self.assertRaises(ConfigError) as caught:
            self.write(files)
        self.assertIn("devdocs has no record of writing", caught.exception.message)
        self.assertTrue(caught.exception.message.endswith(": " + ", ".join(paths)))
        self.assertIn("--force", caught.exception.hint or "")
        self.assertEqual(self.tree(), before)

    def test_a_first_write_refuses_hand_written_files_before_writing(self) -> None:
        self.plant("index.html", b"<h1>Our landing page</h1>\n")
        self.plant("assets/style.css", b"/* our theme */\n")
        site = {"index.html": b"<p>site</p>\n", "assets/style.css": b"p {}\n",
                "page.html": b"<p>page</p>\n"}
        self.assert_refused(site, "assets/style.css", "index.html")

    def test_a_file_the_manifest_does_not_list_is_refused(self) -> None:
        self.write({"a.md": b"A\n"})
        self.plant("README.md", b"# Our docs\n")
        self.assert_refused({"a.md": b"A\n", "README.md": b"# Home\n"}, "README.md")

    def test_force_replaces_them_and_lists_them_as_generated(self) -> None:
        self.write({"a.md": b"A\n"})
        self.plant("README.md", b"# Our docs\n")
        report = self.force({"a.md": b"A\n", "README.md": b"# Home\n"})
        self.assertEqual(report.written, ["README.md", "index.json"])
        self.assertEqual(self.manifest()["files"], ["README.md", "a.md"])
        self.assertEqual(self.write({"a.md": b"A\n"}).removed, ["README.md"])

    def test_a_file_already_right_is_left_alone_and_never_pruned(self) -> None:
        self.write({"a.md": b"A\n"})
        self.plant(".nojekyll", b"")
        report = self.write({"a.md": b"A\n", ".nojekyll": b""})
        self.assertEqual(report, WriteReport(unchanged=[".nojekyll", "a.md",
                                                        "index.json"]))
        self.assertEqual(self.manifest()["files"], ["a.md"])
        self.assertEqual(self.force({"a.md": b"A\n", ".nojekyll": b""}).written, [])
        self.assertEqual(self.write({"a.md": b"A\n"}).removed, [])
        self.assertTrue((self.out / ".nojekyll").is_file())

    def test_without_a_manifest_identical_files_are_taken_as_generated(self) -> None:
        """What a lost manifest leaves: the pages, exactly as they would be
        written again, so nothing to refuse and nothing to lose."""
        self.plant("a.md", b"A\n")
        report = self.write({"a.md": b"A\n", "b.md": b"B\n"})
        self.assertEqual(report, WriteReport(written=["b.md", "index.json"],
                                             unchanged=["a.md"]))
        self.assertEqual(self.manifest()["files"], ["a.md", "b.md"])
        self.assertEqual(self.write({"b.md": b"B\n"}).removed, ["a.md"])

    def test_a_write_that_dies_midway_leaves_a_record_of_its_files(self) -> None:
        """A rename failing, as over a file Windows holds open: the files
        renamed by then stay, the rest are as they were, and the manifest,
        renamed first, lists them all. Regression: the hint asked for write
        access to a directory devdocs could write."""
        self.write({"a.md": b"A\n"})
        real_replace = os.replace

        def locked_at_c(source: Any, target: Any) -> None:
            if os.fspath(target).endswith("c.md"):
                raise PermissionError(errno.EACCES, "locked", os.fspath(source),
                                      None, os.fspath(target))
            real_replace(source, target)

        with mock.patch("devdocs.render.os.replace", side_effect=locked_at_c), \
                self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n", "b.md": b"B\n", "c.md": b"C\n", "d.md": b"D\n"})
        self.assertEqual(caught.exception.message,
                         f"cannot write c.md in {self.out}: locked")
        self.assertEqual(caught.exception.hint, (
            f"{self.out / 'c.md'} is locked or read-only: close any program that "
            "has it open, or clear its read-only or immutable flag, then run again"))
        self.assertEqual(sorted(self.tree()), ["a.md", "b.md", "index.json"])
        self.assertEqual(self.manifest()["files"], ["a.md", "b.md", "c.md", "d.md"])
        report = self.write({"a.md": b"A\n", "b.md": b"B2\n"})
        self.assertEqual(report.written, ["b.md", "index.json"])
        self.assertEqual(self.manifest()["files"], ["a.md", "b.md"])

    @unittest.skipUnless(hasattr(os, "link"), "needs hard links")
    def test_another_name_for_a_listed_file_is_not_a_conflict(self) -> None:
        """A source renamed only in case: a case-insensitive disk finds the
        old page under the new name; a hard link stands in for that here."""
        self.write({"Page.md": b"old\n"})
        os.link(self.out / "Page.md", self.out / "page-alias.md")
        report = self.write({"page-alias.md": b"new\n"})
        self.assertEqual(report.written, ["index.json", "page-alias.md"])
        self.assertEqual(self.manifest()["files"], ["page-alias.md"])

    def test_the_error_names_the_first_few_files_and_counts_the_rest(self) -> None:
        names = [f"p{n}.md" for n in range(8)]
        for name in names:
            self.plant(name)
        with self.assertRaises(ConfigError) as caught:
            self.write(dict.fromkeys(names, b"page\n"))
        self.assertTrue(caught.exception.message.startswith(
            "refusing to replace 8 files in "))
        self.assertTrue(caught.exception.message.endswith(
            ": p0.md, p1.md, p2.md, p3.md, p4.md and 3 more"))


class TestFailures(WriterCase):
    """A write the file system fails is a ConfigError that names the file
    and says what to do, never a bare OSError; most fail before anything
    has changed."""

    def test_a_name_the_file_system_cannot_hold_changes_nothing(self) -> None:
        """Regression: on macOS a page named for a Linux file holding a byte
        that is not UTF-8 ended the run as an internal error, after earlier
        pages and the manifest had been replaced. The byte sits past the
        first 40 characters, where an older temp name did not reach."""
        self.write({"a.md": b"A\n", "sub/b.md": b"B\n"})
        before, dirs = self.tree(), self.dirs()
        for name in (f"{'x' * 50}{ODD}.py.md", f"new/{ODD}/x.py.md"):
            with self.subTest(name=name):
                with unnameable(ODD), self.assertRaises(ConfigError) as caught:
                    self.write({"a.md": b"A2\n", name: b"X\n", "z.md": b"Z\n"})
                error = caught.exception
                self.assertEqual(error.message, (
                    f"cannot write {name} in {self.out}: the file system cannot "
                    f"name it ({os.strerror(errno.EILSEQ)})"))
                self.assertEqual(error.hint, (
                    "rename the source file it documents so its name is UTF-8, "
                    "then run `devdocs generate` again; or choose an --out on a "
                    "file system that can name it"))
                self.assertEqual(error.exit_code, 2)
                self.assertEqual((self.tree(), self.dirs()), (before, dirs))

    def test_a_name_this_file_system_refuses_writes_nothing(self) -> None:
        probe = self.base / f"{ODD}.probe"
        try:
            os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        except OSError:
            pass
        else:
            self.skipTest("this file system names any bytes, as Linux does")
        with self.assertRaises(ConfigError) as caught:
            self.write({"README.md": b"R\n", f"{ODD}.py.md": b"C\n"})
        self.assertIn(f"cannot write {ODD}.py.md in ", caught.exception.message)
        self.assertFalse(self.out.exists())
        self.out = self.base / f"docs-{ODD}"
        with self.assertRaises(ConfigError) as caught:
            self.write({"README.md": b"R\n"})
        self.assertEqual(caught.exception.hint, "choose an --out whose path is UTF-8")

    def test_an_out_the_file_system_cannot_name_is_named(self) -> None:
        """Regression: where the out dir's path, not a page's, holds bytes
        that are not UTF-8, the hint said to rename a source file."""
        for out in (self.base / f"docs-{ODD}", self.base / ODD / "docs"):
            with self.subTest(out=out):
                self.out = out
                with unnameable(ODD), self.assertRaises(ConfigError) as caught:
                    self.write({"a.md": b"A\n"})
                self.assertEqual(caught.exception.message, (
                    f"cannot write to {out}: the file system cannot name it "
                    f"({os.strerror(errno.EILSEQ)})"))
                self.assertEqual(caught.exception.hint,
                                 "choose an --out whose path is UTF-8")
                self.assertEqual(os.listdir(self.base), [])

    @unittest.skipIf(os.name == "nt", "Windows has its own limits and errors")
    def test_an_out_whose_path_is_too_long_is_named(self) -> None:
        """Regression: the hint said to shorten a source file's name."""
        self.out = self.base / ("d" * 300) / "docs"
        with self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n"})
        self.assertEqual(caught.exception.message, (
            f"cannot write to {self.out}: {os.strerror(errno.ENAMETOOLONG)}"))
        self.assertEqual(caught.exception.hint, "choose an --out with a shorter path")

    @unittest.skipIf(os.name == "nt", "Windows has its own limits and errors")
    def test_a_name_too_long_is_found_before_anything_is_written(self) -> None:
        """Looked up where the out dir exists, else once the write has made
        its directory; either way before any rename. Directories made for it
        go again, innermost first."""
        name = "x" * 300 + ".md"
        for where, first in ((name, True), (name, False), (f"new/{name}", False),
                             (f"new/sub/{name}", False)):
            with self.subTest(where=where, first=first):
                shutil.rmtree(self.out, ignore_errors=True)
                if not first:
                    self.write({"a.md": b"A\n"})
                before, dirs = self.tree(), self.dirs()
                with self.assertRaises(ConfigError) as caught:
                    self.write({"a.md": b"A2\n", where: b"X\n"})
                self.assertEqual(caught.exception.message, (
                    f"cannot write {where} in {self.out}: "
                    f"{os.strerror(errno.ENAMETOOLONG)}"))
                self.assertIn("shorten the source file's name",
                              caught.exception.hint or "")
                if first:
                    self.assertFalse(self.out.exists())
                else:
                    self.assertEqual((self.tree(), self.dirs()), (before, dirs))

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_devdocs_may_not_write_is_named(self) -> None:
        self.write({"a.md": b"A\n"})
        before = self.tree()
        os.chmod(self.out, 0o555)
        self.addCleanup(os.chmod, self.out, 0o755)
        with self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A2\n", "b.md": b"B\n"})
        self.assertTrue(caught.exception.message.startswith(
            f"cannot write index.json in {self.out}: "))
        self.assertEqual(caught.exception.hint,
                         f"devdocs needs write access to {self.out}")
        self.assertEqual(self.tree(), before)

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_the_directory_out_cannot_be_made_in_is_named(self) -> None:
        site = self.base / "site"
        site.mkdir()
        os.chmod(site, 0o555)
        self.addCleanup(os.chmod, site, 0o755)
        self.out = site / "docs"
        with self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n"})
        self.assertEqual(caught.exception.message,
                         f"cannot write to {self.out}: {os.strerror(errno.EACCES)}")
        self.assertEqual(caught.exception.hint, f"devdocs needs write access to {site}")

    @unittest.skipUnless(hasattr(os, "chflags"), "BSD file flags")
    def test_a_file_flagged_immutable_is_named_with_what_to_do(self) -> None:
        """Its rename fails midway. Regression: the hint asked for write
        access to the out dir, which devdocs had."""
        self.write({"a.md": b"A\n", "b.md": b"B\n"})
        target = self.out / "b.md"
        try:
            os.chflags(target, stat.UF_IMMUTABLE)
        except OSError:
            self.skipTest("this file system keeps no file flags")
        self.addCleanup(os.chflags, target, 0)
        pages = {"a.md": b"A2\n", "b.md": b"B2\n"}
        with self.assertRaises(ConfigError) as caught:
            self.write(pages)
        self.assertEqual(caught.exception.message,
                         f"cannot write b.md in {self.out}: {os.strerror(errno.EPERM)}")
        self.assertEqual(caught.exception.hint, (
            f"{target} is locked or read-only: close any program that has it "
            "open, or clear its read-only or immutable flag, then run again"))
        os.chflags(target, 0)
        self.assertEqual(self.write(pages).written, ["b.md"])

    def test_only_one_temp_file_is_there_at_a_time(self) -> None:
        """Regression: every temp file was written before any was renamed,
        so a rewrite needed room on the disk for all its files at once, and
        a process killed midway left them all behind."""
        self.write({f"p{n}.md": b"old\n" for n in range(5)})
        counts: list[int] = []
        real_fsync = os.fsync

        def counting(handle: int) -> None:
            counts.append(sum(path.suffix == ".tmp" for path in self.out.iterdir()))
            real_fsync(handle)

        self.docset = DocSet(project="two")
        with mock.patch("devdocs.render.os.fsync", side_effect=counting):
            report = self.write({f"p{n}.md": b"new\n" for n in range(5)})
        self.assertEqual(len(report.written), 6)
        self.assertEqual(counts, [1] * 6)

    def test_a_disk_that_fills_midway_leaves_a_record_of_its_files(self) -> None:
        """The files renamed by then are new and the rest as they were, no
        temp file stays, and the manifest, renamed first, holds the new
        record and lists them all; the next write finishes the job."""
        self.write({"a.md": b"A\n", "b.md": b"B\n", "c.md": b"C\n"})
        real_fsync = os.fsync

        def full_at_c(handle: int) -> None:
            if any(path.name.startswith(".c.md.") for path in self.out.iterdir()):
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real_fsync(handle)

        self.docset = DocSet(project="two")
        pages = {"a.md": b"A2\n", "b.md": b"B2\n", "c.md": b"C2\n"}
        with mock.patch("devdocs.render.os.fsync", side_effect=full_at_c), \
                self.assertRaises(ConfigError) as caught:
            self.write(pages)
        self.assertEqual(caught.exception.message, (
            f"cannot write c.md in {self.out}: {os.strerror(errno.ENOSPC)}"))
        self.assertEqual(caught.exception.hint,
                         "free some space on the disk, then run again")
        tree = self.tree()
        self.assertEqual(sorted(tree), ["a.md", "b.md", "c.md", MANIFEST])
        self.assertEqual((tree["a.md"], tree["b.md"], tree["c.md"]),
                         (b"A2\n", b"B2\n", b"C\n"))
        self.assertEqual(read_manifest(self.out).project, "two")
        self.assertEqual(self.manifest()["files"], ["a.md", "b.md", "c.md"])
        self.assertEqual(self.write(pages), WriteReport(
            written=["c.md"], unchanged=["a.md", "b.md", MANIFEST]))

    def test_each_failure_names_the_file_and_what_to_do(self) -> None:
        """Making the temp file fails for most causes; a directory that
        appears where a page goes fails its rename."""
        self.write({"a.md": b"A\n"})
        before = self.tree()
        cases = [
            ("open", errno.EACCES, f"devdocs needs write access to {self.out}"),
            ("open", errno.EPERM, f"devdocs needs write access to {self.out}"),
            ("open", errno.EROFS,
             "choose an --out on a file system devdocs can write to"),
            ("open", errno.ENOSPC, "free some space on the disk, then run again"),
            ("open", errno.EIO,
             f"check that devdocs can write to {self.out}, or choose another --out"),
            ("replace", errno.EISDIR,
             "a file or directory is in the way: move it away or choose another --out"),
        ]
        for call, code, hint in cases:
            with self.subTest(call=call, errno=errno.errorcode[code]):
                failing = mock.patch(f"devdocs.render.os.{call}",
                                     side_effect=OSError(code, os.strerror(code)))
                with failing, self.assertRaises(ConfigError) as caught:
                    self.write({"a.md": b"A2\n"})
                reason = os.strerror(code)
                self.assertEqual(caught.exception.message,
                                 f"cannot write a.md in {self.out}: {reason}")
                self.assertEqual(caught.exception.hint, hint)
                self.assertEqual(caught.exception.exit_code, 2)
                self.assertEqual(self.tree(), before)

    def test_a_manifest_that_fails_after_pruning_leaves_one_still_read(self) -> None:
        """The manifest written ahead still lists the pruned file; the next
        write finds it gone and lists only what is there."""
        self.write({"a.md": b"A\n", "old.md": b"O\n"})
        real_open = os.open

        def no_manifest(path: Any, flags: int, mode: int = 0o777) -> int:
            if os.path.basename(path).startswith(f".{MANIFEST}."):
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), path)
            return real_open(path, flags, mode)

        with mock.patch("devdocs.render.os.open", side_effect=no_manifest), \
                self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n"})
        self.assertTrue(caught.exception.message.startswith(
            f"cannot write {MANIFEST} in {self.out}: "))
        self.assertEqual(sorted(self.tree()), ["a.md", "index.json"])
        self.assertEqual(self.manifest()["files"], ["a.md", "old.md"])
        self.assertEqual(self.write({"a.md": b"A\n"}),
                         WriteReport(written=["index.json"], unchanged=["a.md"]))
        self.assertEqual(self.manifest()["files"], ["a.md"])

    def test_export_reports_it_instead_of_an_internal_error(self) -> None:
        """Regression: `devdocs export` ended with "internal error: OSError:
        [Errno 92] Illegal byte sequence" and exit 1."""
        root = self.base / "proj"
        docs = root / "docs"
        docset = DocSet(project="proj")
        docset.add(Document(f"file:{ODD}.py", DocKind.FILE, f"{ODD}.py", f"{ODD}.py",
                            sections=(Section("purpose", "Purpose", text=("Hi.",)),)))
        OutputWriter(docs).write({}, docset)
        before = (docs / MANIFEST).read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        environ = {k: v for k, v in os.environ.items() if not k.startswith("DEVDOCS_")}
        with mock.patch.dict(os.environ, environ, clear=True), unnameable(ODD), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["export", str(root), "--format", "markdown"])
        self.assertEqual(code, 2)
        self.assertNotIn("internal error", stderr.getvalue())
        self.assertIn(f"devdocs: cannot write {ODD}.py.md in ", stderr.getvalue())
        self.assertIn(": the file system cannot name it (", stderr.getvalue())
        self.assertEqual(sorted(path.name for path in docs.iterdir()), [MANIFEST])
        self.assertEqual((docs / MANIFEST).read_bytes(), before)


class TestChecking(WriterCase):
    """`check` is `write` up to its probes: it refuses what `write` would,
    with the same error, and changes nothing."""

    def check(self, files: dict[str, bytes], *, force: bool = False) -> None:
        OutputWriter(self.out).check(files, self.docset, force=force)

    def assert_refused_alike(self, files: dict[str, bytes]) -> ConfigError:
        """The error `check` raises for `files`, once it is the one `write`
        raises next, and once the check is seen to change nothing."""
        before = (self.out.exists(), self.tree(), self.dirs())
        with self.assertRaises(ConfigError) as checked:
            self.check(files)
        self.assertEqual((self.out.exists(), self.tree(), self.dirs()), before)
        with self.assertRaises(ConfigError) as written:
            self.write(files)
        error = checked.exception
        self.assertEqual((error.message, error.hint),
                         (written.exception.message, written.exception.hint))
        return error

    def test_a_file_devdocs_did_not_write(self) -> None:
        self.write({"a.md": b"A\n"})
        self.plant("README.md")
        error = self.assert_refused_alike({"a.md": b"A2\n", "README.md": b"R\n"})
        self.assertIn("devdocs has no record of writing: README.md", error.message)
        self.check({"a.md": b"A2\n", "README.md": b"R\n"}, force=True)
        self.assertEqual(self.tree()["README.md"], b"hand-written\n")

    def test_a_manifest_devdocs_cannot_read(self) -> None:
        self.plant(MANIFEST, b'{"another": "tool"}\n')
        self.assert_refused_alike({"a.md": b"A\n"})

    def test_a_directory_in_the_way(self) -> None:
        (self.out / "a.md").mkdir(parents=True)
        self.assert_refused_alike({"a.md": b"A\n"})

    def test_a_name_the_file_system_cannot_hold(self) -> None:
        for name in (f"{ODD}.py.md", f"new/{ODD}/x.py.md"):
            with self.subTest(name=name), unnameable(ODD):
                self.assert_refused_alike({"a.md": b"A\n", name: b"X\n"})
        self.assertFalse(self.out.exists())

    @unittest.skipIf(os.name == "nt", "Windows has its own limits and errors")
    def test_a_name_too_long(self) -> None:
        self.write({"a.md": b"A\n"})
        self.assert_refused_alike({"a.md": b"A\n", f"new/{'x' * 300}.md": b"X\n"})

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_an_out_dir_devdocs_cannot_write(self) -> None:
        self.write({"a.md": b"A\n"})
        os.chmod(self.out, 0o555)
        self.addCleanup(os.chmod, self.out, 0o755)
        error = self.assert_refused_alike({"a.md": b"A2\n"})
        self.assertEqual(error.hint, f"devdocs needs write access to {self.out}")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_an_out_dir_that_cannot_be_made(self) -> None:
        site = self.base / "site"
        site.mkdir()
        os.chmod(site, 0o555)
        self.addCleanup(os.chmod, site, 0o755)
        self.out = site / "docs"
        self.assert_refused_alike({"a.md": b"A\n"})

    def test_a_check_that_passes_changes_nothing(self) -> None:
        self.check({"a.md": b"A\n", "sub/deep/b.md": b"B\n"})
        self.assertFalse(self.out.exists())
        self.write({"a.md": b"A\n"})
        before = (self.tree(), self.dirs())
        self.check({"a.md": b"A2\n", "new/deep/c.md": b"C\n"})
        self.assertEqual((self.tree(), self.dirs()), before)

    def test_a_check_cut_short_leaves_nothing_behind(self) -> None:
        # As a Ctrl-C does, with a probe's temp file made and a directory too.
        real_close = os.close
        closed: list[int] = []

        def interrupted(handle: int) -> None:
            real_close(handle)
            closed.append(handle)
            if len(closed) == 3:
                raise KeyboardInterrupt

        with mock.patch("devdocs.render.os.close", side_effect=interrupted), \
                self.assertRaises(KeyboardInterrupt):
            self.check({"a.md": b"A\n", "sub/b.md": b"B\n", "sub/c.md": b"C\n"})
        self.assertEqual(len(closed), 3)
        self.assertFalse(self.out.exists())


class TestOutAsGiven(WriterCase):
    """Every file call goes to what `out` names once its missing directories
    are made, the check's as much as the write's, and every message keeps
    `out` as given."""

    def setUp(self) -> None:
        super().setUp()
        self.given = self.out / "new" / ".."

    def test_a_directory_not_made_yet_hides_no_file(self) -> None:
        """Regression: `new/..` led nowhere until `new` was made, so the look
        found nothing, and the rename, by then with `new` made, replaced the
        hand-written README.md without `force`."""
        self.plant("README.md")
        writer = OutputWriter(self.given)
        for act in (writer.check, writer.write):
            with self.subTest(act=act.__name__):
                with self.assertRaises(ConfigError) as caught:
                    act({"README.md": b"R\n"}, self.docset)
                self.assertEqual(caught.exception.message, (
                    f"refusing to replace 1 file in {self.given} that devdocs has "
                    "no record of writing: README.md"))
                self.assertEqual(self.tree(), {"README.md": b"hand-written\n"})
                self.assertEqual(self.dirs(), [])

    def test_a_relative_out_is_looked_through_and_named_as_given_too(self) -> None:
        self.plant("README.md")
        given = Path("docs", "new", "..")
        with contextlib.chdir(self.base), self.assertRaises(ConfigError) as caught:
            OutputWriter(given).write({"README.md": b"R\n"}, self.docset)
        self.assertEqual(caught.exception.message, (
            f"refusing to replace 1 file in {given} that devdocs has no record of "
            "writing: README.md"))
        self.assertEqual(self.tree(), {"README.md": b"hand-written\n"})

    def test_adding_refuses_it_too(self) -> None:
        self.write({"a.md": b"A\n"})
        self.plant("README.md")
        with self.assertRaises(ConfigError) as caught:
            OutputWriter(self.given).add({"README.md": b"R\n"})
        self.assertIn("no record of writing: README.md", caught.exception.message)
        self.assertEqual(self.tree()["README.md"], b"hand-written\n")
        self.assertEqual(self.dirs(), [])

    def test_force_replaces_it_and_no_directory_is_made_on_the_way(self) -> None:
        self.plant("README.md")
        OutputWriter(self.given).write({"README.md": b"R\n"}, self.docset, force=True)
        self.assertEqual(self.tree()["README.md"], b"R\n")
        self.assertEqual(self.dirs(), [])

    def test_directories_past_it_are_made_and_it_is_not(self) -> None:
        OutputWriter(self.given / "deep" / "er").write({"a.md": b"A\n"}, self.docset)
        self.assertEqual(self.dirs(), ["deep", "deep/er"])
        self.assertEqual(sorted(self.tree()),
                         ["deep/er/a.md", f"deep/er/{MANIFEST}"])

    def test_a_check_that_passes_makes_nothing_there(self) -> None:
        OutputWriter(self.given / "deep").check({"sub/a.md": b"A\n"}, self.docset)
        self.assertFalse(self.out.exists())

    def test_the_manifest_is_found_there(self) -> None:
        """Regression: a run spelling `out` so found no record of the run
        before it, so took its pages for files it never wrote, and `export`
        said nothing was generated."""
        self.write({"a.md": b"A\n", "old.md": b"O\n"})
        self.assertEqual(generated_files(self.given), {"a.md", "old.md"})
        self.assertEqual(read_manifest(self.given).project, "demo")
        report = OutputWriter(self.given).write({"a.md": b"A2\n"}, self.docset)
        self.assertEqual(report, WriteReport(written=["a.md", MANIFEST],
                                             removed=["old.md"]))
        report = OutputWriter(self.given).add({"b.md": b"B\n"})
        self.assertEqual(report.written, ["b.md", MANIFEST])
        self.assertEqual(self.manifest()["files"], ["a.md", "b.md"])
        self.assertEqual(self.dirs(), [])

    def test_a_missing_manifest_is_named_as_given(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            read_manifest(self.given)
        self.assertEqual(
            caught.exception.message,
            f"no generated documentation in {self.given}: {MANIFEST} is missing")
        with self.assertRaises(ConfigError) as caught:
            OutputWriter(self.given).add({"a.md": b"A\n"})
        self.assertEqual(caught.exception.message,
                         f"no devdocs manifest in {self.given} to add files to")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_above_it_is_named_by_its_shortest_spelling(self) -> None:
        site = self.base / "site"
        site.mkdir()
        os.chmod(site, 0o555)
        self.addCleanup(os.chmod, site, 0o755)
        given = site / "new" / ".." / "docs"
        with self.assertRaises(ConfigError) as caught:
            OutputWriter(given).write({"a.md": b"A\n"}, self.docset)
        self.assertEqual(caught.exception.message,
                         f"cannot write to {given}: {os.strerror(errno.EACCES)}")
        self.assertEqual(caught.exception.hint, f"devdocs needs write access to {site}")

    @unittest.skipIf(os.name == "nt", "Windows has its own limits and errors")
    def test_a_name_too_long_past_it_is_the_outs_to_shorten(self) -> None:
        """The kernel stops at `new`, so only the real path meets the long
        name; it is still the out's, not a source file's, to shorten."""
        given = self.base / "new" / ".." / ("d" * 300) / "docs"
        writer = OutputWriter(given)
        for act in (writer.check, writer.write):
            with self.subTest(act=act.__name__):
                with self.assertRaises(ConfigError) as caught:
                    act({"a.md": b"A\n"}, self.docset)
                self.assertEqual(caught.exception.message, (
                    f"cannot write to {given}: {os.strerror(errno.ENAMETOOLONG)}"))
                self.assertEqual(caught.exception.hint,
                                 "choose an --out with a shorter path")
                self.assertEqual(os.listdir(self.base), [])

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_inside_it_is_named_from_it(self) -> None:
        self.write({"sub/a.md": b"A\n"})
        os.chmod(self.out / "sub", 0o555)
        self.addCleanup(os.chmod, self.out / "sub", 0o755)
        with self.assertRaises(ConfigError) as caught:
            OutputWriter(self.given).write({"sub/a.md": b"A2\n"}, self.docset)
        self.assertEqual(
            caught.exception.message,
            f"cannot write sub/a.md in {self.given}: {os.strerror(errno.EACCES)}")
        self.assertEqual(caught.exception.hint,
                         f"devdocs needs write access to {self.given / 'sub'}")

    def test_a_locked_file_is_named_from_it(self) -> None:
        self.write({"a.md": b"A\n"})

        def locked(source: Any, target: Any) -> None:
            raise PermissionError(errno.EACCES, "locked", os.fspath(source), None,
                                  os.fspath(target))

        with mock.patch("devdocs.render.os.replace", side_effect=locked), \
                self.assertRaises(ConfigError) as caught:
            OutputWriter(self.given).write({"a.md": b"A2\n"}, self.docset)
        self.assertEqual(caught.exception.message,
                         f"cannot write a.md in {self.given}: locked")
        self.assertEqual(caught.exception.hint, (
            f"{self.given / 'a.md'} is locked or read-only: close any program that "
            "has it open, or clear its read-only or immutable flag, then run again"))

    @unittest.skipIf(os.name == "nt", "reading a directory is EACCES on Windows")
    def test_a_manifest_it_cannot_read_is_named_once_as_given(self) -> None:
        """Regression: the OSError's own text added the real path, a second
        spelling of one file."""
        (self.out / MANIFEST).mkdir(parents=True)
        with self.assertRaises(ConfigError) as caught:
            read_manifest(self.given)
        self.assertEqual(
            caught.exception.message,
            f"cannot read {self.given / MANIFEST}: {os.strerror(errno.EISDIR)}")

    def test_a_read_error_with_no_reason_of_its_own_is_shown_whole(self) -> None:
        self.write({"a.md": b"A\n"})
        with mock.patch("devdocs.render.json.loads", side_effect=OSError("boom")), \
                self.assertRaises(ConfigError) as caught:
            read_manifest(self.given)
        self.assertEqual(caught.exception.message,
                         f"cannot read {self.given / MANIFEST}: boom")


@unittest.skipIf(os.name == "nt", "Windows reads `..` before it looks at anything")
class TestOutTheKernelRefuses(WriterCase):
    """`os.path.realpath` reads `afile/..` as the directory holding `afile`,
    but the kernel refuses that spelling, and no directory made could change
    its mind. So it stays refused, by the check and `force` alike, as it was
    before writes went through the real path. Regression: it ran, and wrote."""

    def setUp(self) -> None:
        super().setUp()
        self.afile = self.base / "afile"
        self.afile.write_bytes(b"x\n")
        self.given = self.afile / ".." / "docs"

    def assert_write_refused(self, given: Path, reason: int, hint: str = "") -> None:
        """The check and the write refuse `given`, for `reason`, naming it."""
        writer = OutputWriter(given)
        for act in (writer.check, writer.write):
            for force in (False, True):
                with self.subTest(act=act.__name__, force=force):
                    with self.assertRaises(ConfigError) as caught:
                        act({"a.md": b"A\n"}, self.docset, force=force)
                    self.assertEqual(caught.exception.message,
                                     f"cannot write to {given}: {os.strerror(reason)}")
                    if hint:
                        self.assertEqual(caught.exception.hint, hint)

    def test_a_regular_file_in_the_way(self) -> None:
        self.assert_write_refused(self.given, errno.ENOTDIR, "a file or directory is "
                                  "in the way: move it away or choose another --out")
        self.assertFalse(self.out.exists())

    def test_it_writes_nothing_where_it_would_have_landed(self) -> None:
        self.plant("afile")
        self.plant("README.md")
        before = self.tree()
        self.assert_write_refused(self.out / "afile" / "..", errno.ENOTDIR)
        self.assertEqual(self.tree(), before)

    def test_a_link_to_a_regular_file_in_the_way(self) -> None:
        link = self.base / "link"
        try:
            link.symlink_to(self.afile)
        except (OSError, NotImplementedError) as err:
            self.skipTest(f"symlinks unavailable: {err}")
        self.assert_write_refused(link / ".." / "docs", errno.ENOTDIR)
        self.assertFalse(self.out.exists())

    def test_nothing_is_read_through_it_either(self) -> None:
        self.write({"a.md": b"A\n"})
        before = self.tree()
        self.assertEqual(generated_files(self.given), set())
        with self.assertRaises(ConfigError) as caught:
            read_manifest(self.given)
        self.assertEqual(
            caught.exception.message,
            f"cannot read {self.given / MANIFEST}: {os.strerror(errno.ENOTDIR)}")
        with self.assertRaises(ConfigError):
            OutputWriter(self.given).add({"b.md": b"B\n"})
        self.assertEqual(self.tree(), before)

    def test_a_name_too_long_to_make(self) -> None:
        given = self.base / ("x" * 300) / ".." / "docs"
        self.assert_write_refused(given, errno.ENAMETOOLONG,
                                  "choose an --out with a shorter path")
        self.assertFalse(self.out.exists())

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_it_may_not_pass_through(self) -> None:
        shut = self.base / "shut"
        shut.mkdir()
        os.chmod(shut, 0)
        self.addCleanup(os.chmod, shut, 0o755)
        self.assert_write_refused(shut / ".." / "docs", errno.EACCES)
        self.assertFalse(self.out.exists())

    def test_a_directory_not_made_yet_still_leads_past_a_file(self) -> None:
        """The kernel stops at `new`, so never gets to the file; the real path
        is then all there is to go by, and it makes no `new`. A limit, not a
        promise: nothing it writes escapes the guard on `docs`."""
        given = self.base / "new" / ".." / "afile" / ".." / "docs"
        self.plant("README.md")
        with self.assertRaises(ConfigError) as caught:
            OutputWriter(given).write({"README.md": b"R\n"}, self.docset)
        self.assertIn("no record of writing: README.md", caught.exception.message)
        OutputWriter(given).write({"a.md": b"A\n"}, self.docset)
        self.assertEqual(self.tree()["a.md"], b"A\n")
        self.assertFalse((self.base / "new").exists())


@unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
class TestOutThroughALink(WriterCase):
    """An `out` that is a link leads where it leads, even to a directory not
    made yet, once a file in the way; messages name the link."""

    def setUp(self) -> None:
        super().setUp()
        self.real = self.base / "real"
        self.out = self.base / "link"
        self.out.symlink_to(self.real, target_is_directory=True)

    def test_a_link_to_a_directory_not_made_yet_leads_to_it(self) -> None:
        self.write({"a.md": b"A\n"})
        self.assertTrue(self.out.is_symlink())
        self.assertEqual((self.real / "a.md").read_bytes(), b"A\n")

    def test_what_is_behind_a_link_is_refused_and_named_through_it(self) -> None:
        self.real.mkdir()
        (self.real / "README.md").write_bytes(b"hand-written\n")
        with self.assertRaises(ConfigError) as caught:
            self.write({"README.md": b"R\n"})
        self.assertEqual(caught.exception.message, (
            f"refusing to replace 1 file in {self.out} that devdocs has no record "
            "of writing: README.md"))
        self.assertEqual((self.real / "README.md").read_bytes(), b"hand-written\n")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_behind_a_link_is_named_through_it(self) -> None:
        self.real.mkdir()
        os.chmod(self.real, 0o555)
        self.addCleanup(os.chmod, self.real, 0o755)
        with self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n"})
        self.assertEqual(
            caught.exception.message,
            f"cannot write index.json in {self.out}: {os.strerror(errno.EACCES)}")
        self.assertEqual(caught.exception.hint,
                         f"devdocs needs write access to {self.out}")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0,
                     "POSIX permission bits, which root ignores")
    def test_a_directory_only_a_link_leads_to_is_named_itself(self) -> None:
        """The directory above the link's target has no name through `out`."""
        self.real.mkdir()
        target = self.real / "docs"
        self.out.unlink()
        self.out.symlink_to(target, target_is_directory=True)
        os.chmod(self.real, 0o555)
        self.addCleanup(os.chmod, self.real, 0o755)
        with self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n"})
        self.assertEqual(caught.exception.message,
                         f"cannot write to {self.out}: {os.strerror(errno.EACCES)}")
        self.assertEqual(caught.exception.hint,
                         f"devdocs needs write access to {self.real.resolve()}")

    def test_a_link_loop_is_named_as_any_other_failure(self) -> None:
        # Path.resolve raises RuntimeError for one before 3.13.
        self.out.unlink()
        self.out.symlink_to(self.out)
        with self.assertRaises(ConfigError) as caught:
            self.write({"a.md": b"A\n"})
        self.assertEqual(
            caught.exception.message,
            f"cannot write index.json in {self.out}: {os.strerror(errno.ELOOP)}")
        with self.assertRaises(ConfigError):
            OutputWriter(self.out).check({"a.md": b"A\n"}, self.docset)
        with self.assertRaises(ConfigError) as caught:
            read_manifest(self.out)
        self.assertEqual(
            caught.exception.message,
            f"cannot read {self.out / MANIFEST}: {os.strerror(errno.ELOOP)}")
        self.assertEqual(generated_files(self.out), set())

    def test_a_manifest_it_cannot_read_is_named_through_it(self) -> None:
        # Regression: as for `new/..`, the OSError's text added the real path.
        (self.real / MANIFEST).mkdir(parents=True)
        with self.assertRaises(ConfigError) as caught:
            read_manifest(self.out)
        self.assertEqual(
            caught.exception.message,
            f"cannot read {self.out / MANIFEST}: {os.strerror(errno.EISDIR)}")


class TestAdding(WriterCase):
    """`add`, as export uses it: new files join the last write's, whose
    record is kept exactly as it was."""

    def test_files_join_the_record_which_is_kept(self) -> None:
        self.docset = shop()
        self.write({"a.md": b"A\n"})
        before = self.manifest()
        report = OutputWriter(self.out).add({"shop.pdf": b"%PDF\n"})
        self.assertEqual(report, WriteReport(written=["index.json", "shop.pdf"]))
        self.assertEqual(self.manifest(), {**before, "files": ["a.md", "shop.pdf"]})

    def test_a_newer_schemas_fields_and_label_survive(self) -> None:
        self.write({"a.md": b"A\n"})
        newer = self.manifest()
        newer["schema_version"] = "1.9.0"
        newer["sources_root"] = "src"
        newer["run"] = {"embedding_model": "e"}
        self.plant(MANIFEST, json.dumps(newer).encode())
        OutputWriter(self.out).add({"b.md": b"B\n"})
        self.assertEqual(self.manifest(), {**newer, "files": ["a.md", "b.md"]})

    def test_adding_refuses_what_writing_refuses(self) -> None:
        self.write({"a.md": b"A\n"})
        self.plant("index.html", b"<h1>Ours</h1>\n")
        with self.assertRaises(ConfigError):
            OutputWriter(self.out).add({"index.html": b"<p>site</p>\n"})
        self.assertEqual(self.manifest()["files"], ["a.md"])
        OutputWriter(self.out).add({"index.html": b"<p>site</p>\n"}, force=True)
        self.assertEqual(self.manifest()["files"], ["a.md", "index.html"])

    def test_there_must_be_a_manifest_to_add_to(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            OutputWriter(self.out).add({"b.md": b"B\n"})
        self.assertIn("devdocs generate", caught.exception.hint or "")
        self.assertFalse(self.out.exists())


class TestReadingTheManifest(WriterCase):
    def test_the_docset_comes_back_as_written(self) -> None:
        docset = shop()
        docset.run = RunInfo(generator="devdocs 4.0.0",
                             generated_at="2026-11-02T10:00:00+00:00", requests=3)
        OutputWriter(self.out).write({"a.md": b"A\n"}, docset)
        again = read_manifest(self.out)
        self.assertEqual(again.to_dict(), docset.to_dict())
        self.assertEqual(again.run, docset.run)

    def assert_refused(self, manifest: bytes | None, *words: str) -> ConfigError:
        shutil.rmtree(self.out, ignore_errors=True)
        if manifest is not None:
            self.plant(MANIFEST, manifest)
        with self.assertRaises(ConfigError) as caught:
            read_manifest(self.out)
        for word in words:
            self.assertIn(word, str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 2)
        return caught.exception

    def test_a_missing_manifest_says_to_generate_first(self) -> None:
        refused = self.assert_refused(None, "index.json is missing")
        self.assertIn("devdocs generate", refused.hint or "")

    def test_an_unreadable_or_foreign_manifest_is_refused(self) -> None:
        documents = [{"id": "x", "kind": "file", "title": "x"}]   # no path
        cases: dict[str, tuple[bytes, tuple[str, ...]]] = {
            "not json": (b"{oops", ("cannot read",)),
            "too deep": (b"[" * 100_000 + b"]" * 100_000, ()),   # 3.15 parses it
            "not an object": (b"[]", ("not a devdocs manifest",)),
            "foreign": (b'{"files": []}', ("not a devdocs manifest",)),
            "older": (b'{"schema_version": "0.9.0"}', ("not readable",)),
            "malformed": (json.dumps({"schema_version": "1.0.0",
                                      "documents": documents}).encode(), ("path",)),
            "not a list": (b'{"schema_version": "1.0.0", "documents": 7}',
                           ("cannot read",)),
        }
        for name, (manifest, words) in cases.items():
            with self.subTest(name):
                refused = self.assert_refused(manifest, *words)
                self.assertIn("devdocs generate", refused.hint or "")
        with self.subTest("a directory"):
            shutil.rmtree(self.out, ignore_errors=True)
            (self.out / MANIFEST).mkdir(parents=True)
            with self.assertRaises(ConfigError):
                read_manifest(self.out)

    def test_a_newer_schema_is_refused_by_name(self) -> None:
        data = shop().to_dict()
        data["schema_version"] = "2.0.0"
        refused = self.assert_refused(json.dumps(data).encode(), "newer devdocs",
                                      "2.0.0")
        self.assertIn("upgrade devdocs", refused.hint or "")

    def test_a_newer_minor_version_is_read(self) -> None:
        data = shop().to_dict()
        data["schema_version"] = "1.9.0"
        self.plant(MANIFEST, json.dumps(data).encode())
        self.assertEqual(len(read_manifest(self.out).documents), len(data["documents"]))

    def test_documents_that_would_write_outside_out_or_twice_are_refused(self) -> None:
        good = shop()
        first = good.documents[0]
        for name, documents in (
            ("unsafe", [replace(first, path="../../escape")]),
            ("empty", [replace(first, path="")]),
            ("twice", [first, replace(first, path="elsewhere")]),
        ):
            with self.subTest(name):
                data = DocSet(project="demo", documents=documents).to_dict()
                self.assert_refused(json.dumps(data).encode(), repr(first.id))


class TestRegistry(unittest.TestCase):
    def test_each_format_has_its_renderer(self) -> None:
        for fmt, kind in (("markdown", MarkdownRenderer), ("html", HtmlRenderer),
                          ("pdf", PdfRenderer)):
            with self.subTest(fmt):
                renderer = renderer_for(fmt)
                self.assertIsInstance(renderer, kind)
                self.assertIsInstance(renderer, Renderer)
                self.assertEqual(renderer.format, fmt)
        self.assertEqual(FORMATS, ("markdown", "html", "pdf"))

    def test_html_is_the_site(self) -> None:
        files = renderer_for("html").render(shop())
        self.assertIn("index.html", files)
        self.assertNotIn("book.html", files)

    def test_pdf_options_reach_the_renderer(self) -> None:
        renderer = renderer_for("pdf", split=True, page_size="Letter")
        assert isinstance(renderer, PdfRenderer)
        self.assertEqual((renderer.split, renderer.page_size, renderer.engine),
                         (True, "Letter", "builtin"))
        with mock.patch.object(external, "find", return_value="/bin/chrome"):
            renderer = renderer_for("pdf", pdf_engine="chrome")
        assert isinstance(renderer, PdfRenderer)
        self.assertEqual(renderer.engine, "chrome")

    def test_unknown_formats_and_options_are_configuration_errors(self) -> None:
        for fmt, page_size, engine in (("docx", "A4", "builtin"),
                                       ("pdf", "A5", "builtin"),
                                       ("pdf", "A4", "pandoc")):
            with self.subTest(fmt=fmt, page_size=page_size, engine=engine), \
                    self.assertRaises(ConfigError):
                renderer_for(fmt, page_size=page_size, pdf_engine=engine)

    def test_a_missing_external_engine_fails_when_the_renderer_is_made(self) -> None:
        with mock.patch.object(external, "find", return_value=None), \
                self.assertRaises(ConfigError) as caught:
            renderer_for("pdf", pdf_engine="weasyprint")
        self.assertIn("weasyprint PDF engine is not installed", str(caught.exception))
        with mock.patch.object(external, "find", side_effect=AssertionError):
            renderer_for("pdf")   # builtin needs nothing installed

    def test_the_pdf_choices_are_the_pdf_renderers_own(self) -> None:
        self.assertEqual(PDF_ENGINES, external.ENGINES)
        self.assertEqual(PAGE_SIZES, tuple(layout.PAGE_SIZES))


if __name__ == "__main__":
    unittest.main()
