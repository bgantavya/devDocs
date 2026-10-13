"""The output writer: atomic writes, quiet re-runs, and pruning that only ever
touches files devdocs itself generated; and the registry of formats."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs.errors import ConfigError
from devdocs.render import (
    FORMATS,
    MANIFEST,
    PAGE_SIZES,
    PDF_ENGINES,
    OutputWriter,
    Renderer,
    WriteReport,
    renderer_for,
)
from devdocs.render.html import HtmlRenderer
from devdocs.render.markdown import MarkdownRenderer
from devdocs.render.model import DocSet, RunInfo
from devdocs.render.pdf import PdfRenderer, external, layout
from tests.test_markdown import shop


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
        with failing, self.assertRaises(OSError):
            self.write({"a.md": b"new\n"})
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
        self.assertEqual(len(lengths), 2)
        self.assertLessEqual(max(lengths), 255)

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
        self.assertEqual(len(opened), 3)   # a.md taken, a.md retried, index.json
        self.assertNotEqual(opened[0][0], opened[1][0])
        exclusive = os.O_CREAT | os.O_EXCL
        self.assertTrue(all(flags & exclusive == exclusive for _, flags in opened))
        self.assertEqual(sorted(self.tree()), ["a.md", "index.json"])

    def test_a_directory_in_the_way_raises_and_cleans_up(self) -> None:
        (self.out / "a.md").mkdir(parents=True)
        with self.assertRaises(OSError):
            self.write({"a.md": b"A\n"})
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
        listed = {"schema_version": "1.0.0", "files": ["old.md"]}
        cases = {
            "not json": b"{oops",
            "not utf-8": b"\x80{}",
            "not an object": json.dumps(["old.md"]).encode(),
            "foreign": json.dumps({"files": ["old.md"]}).encode(),
            "files not a list": json.dumps({**listed, "files": "old.md"}).encode(),
            "too deep": b"[" * 100_000 + b"]" * 100_000,
            "a directory": None,
        }
        for name, manifest in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.out, ignore_errors=True)
                self.plant("old.md")
                if manifest is None:
                    (self.out / MANIFEST).mkdir()
                    with self.assertRaises(OSError):
                        self.write({"new.md": b"N\n"})
                else:
                    self.plant(MANIFEST, manifest)
                    self.assertEqual(self.write({"new.md": b"N\n"}).removed, [])
                self.assertTrue((self.out / "old.md").is_file())

    def test_a_manifest_entry_naming_no_possible_file_is_skipped(self) -> None:
        self.plant("old.md")
        self.plant(MANIFEST, json.dumps({"schema_version": "1.0.0", "files": [
            "\ud800.md", "old.md",
        ]}).encode())
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
        self.plant(MANIFEST, json.dumps({"schema_version": "1.0.0", "files": [
            "../outside.md", str(outside), "dir.md", "link.md", "jump/x.md",
            "missing.md", "a\\b.md", 42, None, "listed.md",
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

    def test_a_file_that_cannot_be_removed_is_skipped(self) -> None:
        self.write({"a.md": b"A\n", "b.md": b"B\n"})
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            report = self.write({"b.md": b"B\n"})
        self.assertEqual(report.removed, [])
        self.assertTrue((self.out / "a.md").is_file())


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
