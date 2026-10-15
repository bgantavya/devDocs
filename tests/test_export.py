"""`devdocs export` end to end: a project is generated with the scripted
model, then rendered again in other formats from its manifest alone, with no
key and no model, keeping every file the run wrote."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from devdocs import cli
from devdocs.config import Config
from devdocs.errors import ConfigError, DevDocsError
from devdocs.generate import Pipeline
from devdocs.render import MANIFEST
from devdocs.render.pdf import external
from devdocs.render.pdf.check import PdfFile, check
from tests.test_cli import CliCase, clean_env
from tests.test_html import check_links


def tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by POSIX path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ExportCase(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.docs = self.root / "docs"

    def generate(self, *argv: str) -> None:
        """A run with the scripted model; remembers what it wrote."""
        code, _, err = self.run_cli("generate", str(self.root), "--api-key", "k",
                                    "--quiet", *argv)
        self.assertEqual(code, 0, err)
        self.generated = tree(self.docs)
        self.calls = len(self.model.calls)

    def export(self, *argv: str) -> tuple[int, str, str]:
        """`devdocs export` with no key anywhere and no way to make a model."""
        out, err = io.StringIO(), io.StringIO()
        refuse = mock.patch.object(cli, "make_provider",
                                   side_effect=AssertionError("export made a model"))
        with clean_env(), refuse, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["export", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def manifest(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.docs / MANIFEST).read_bytes())
        return data


class TestExport(ExportCase):
    def setUp(self) -> None:
        super().setUp()
        self.generate()

    def test_html_and_pdf_join_the_markdown_pages(self) -> None:
        code, out, _ = self.export("--format", "html,pdf", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.model.calls), self.calls)
        files = tree(self.docs)
        for path, data in self.generated.items():
            if path != MANIFEST:
                self.assertEqual(files[path], data, path)
        self.assertEqual(check(files["shop.pdf"]), [])
        self.assertIn("index.html", files)
        self.assertEqual(check_links(files), [])

        manifest, before = self.manifest(), json.loads(self.generated[MANIFEST])
        self.assertEqual(manifest["files"], sorted(set(files) - {MANIFEST}))
        self.assertEqual({k: v for k, v in manifest.items() if k != "files"},
                         {k: v for k, v in before.items() if k != "files"})

        summary = json.loads(out)
        self.assertEqual(summary["formats"], ["html", "pdf"])
        self.assertEqual(summary["documents"], len(before["documents"]))
        self.assertEqual(summary["generated_at"], before["run"]["generated_at"])
        self.assertIn("shop.pdf", summary["written"]["written"])
        self.assertEqual(summary["written"]["removed"], [])
        self.assertEqual(summary["warnings"], [])

    def test_split_pdfs_on_letter_paper(self) -> None:
        code, _, _ = self.export("--format", "pdf", "--split", "--page-size", "Letter")
        self.assertEqual(code, 0)
        split = {p: d for p, d in tree(self.docs).items() if p.startswith("pdf/")}
        self.assertIn("pdf/index.pdf", split)
        self.assertIn("pdf/shop/index.pdf", split)
        for path, data in split.items():
            with self.subTest(path):
                self.assertEqual(check(data), [])
        cart = PdfFile(split["pdf/shop/cart.py.pdf"])
        self.assertEqual(cart.page(0)["MediaBox"], [0, 0, 612, 792])
        self.assertEqual(sorted(split), [p for p in self.manifest()["files"]
                                         if p.startswith("pdf/")])

    def test_the_result_names_what_was_written(self) -> None:
        code, out, err = self.export("--format", "pdf")
        self.assertEqual(code, 0)
        run = json.loads(self.generated[MANIFEST])["run"]["generated_at"]
        self.assertEqual(out.splitlines(), [
            f"6 documents (run of {run}) as pdf in docs: 2 files written, 0 unchanged",
            "  docs/index.json",
            "  docs/shop.pdf",
        ])
        self.assertIn("read 6 documents from", err)
        (self.docs / "shop.pdf").unlink()
        _, out, _ = self.export("--format", "pdf", "--quiet")
        self.assertEqual(out.splitlines(), [
            f"6 documents (run of {run}) as pdf in docs: 1 file written, 1 unchanged",
            "  docs/shop.pdf",
        ])

    def test_exporting_again_rewrites_nothing(self) -> None:
        self.export("--format", "pdf")
        code, out, _ = self.export("--format", "pdf", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)["written"]
        self.assertEqual((report["written"], report["unchanged"]),
                         ([], ["index.json", "shop.pdf"]))

    def test_the_configured_formats_are_the_default(self) -> None:
        (self.root / "devdocs.toml").write_text('formats = ["html"]\n', "utf-8")
        code, out, _ = self.export()
        self.assertEqual(code, 0)
        self.assertTrue((self.docs / "index.html").is_file())
        self.assertFalse((self.docs / "shop.pdf").exists())
        self.assertIn(" as html in docs", out)

    def test_a_site_written_by_flag_survives_an_export(self) -> None:
        """What rendering the configured formats with the requested ones would
        lose: a format that was asked for by flag, not configured."""
        self.generate("--format", "html")
        site = tree(self.docs)
        self.assertNotIn("README.md", site)
        code, _, _ = self.export("--format", "pdf")
        self.assertEqual(code, 0)
        files = tree(self.docs)
        self.assertEqual({p: files[p] for p in site if p != MANIFEST},
                         {p: d for p, d in site.items() if p != MANIFEST})
        self.assertIn("shop.pdf", files)
        self.assertNotIn("README.md", files)

    def test_the_next_generate_prunes_exports_with_its_other_stale_files(self) -> None:
        self.export("--format", "html,pdf")
        self.generate()
        self.assertFalse((self.docs / "shop.pdf").exists())
        self.assertFalse((self.docs / "index.html").exists())
        self.assertIn("README.md", self.generated)

    def test_out_names_the_directory_read_and_added_to(self) -> None:
        self.generate("--out", "manual")
        code, _, _ = self.export("--out", "manual", "--format", "pdf")
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "manual" / "shop.pdf").is_file())
        self.assertTrue((self.root / "manual" / "README.md").is_file())

    def test_hand_written_site_files_are_never_replaced_or_pruned(self) -> None:
        # Regression: export replaced a hand-written GitHub Pages landing page,
        # listed it and a matching .nojekyll, and the next generate deleted both.
        landing = b"<!doctype html><title>Our landing page</title>\n"
        (self.docs / "index.html").write_bytes(landing)
        (self.docs / ".nojekyll").write_bytes(b"")
        code, _, err = self.export("--format", "html")
        self.assertEqual(code, 2)
        self.assertIn("has no record of writing: index.html", err)
        self.assertEqual(tree(self.docs), {**self.generated, "index.html": landing,
                                           ".nojekyll": b""})

        (self.docs / "index.html").unlink()
        code, out, _ = self.export("--format", "html", "--json")
        self.assertEqual(code, 0)
        self.assertIn(".nojekyll", json.loads(out)["written"]["unchanged"])
        self.assertNotIn(".nojekyll", self.manifest()["files"])
        self.generate()
        self.assertFalse((self.docs / "index.html").exists())
        self.assertTrue((self.docs / ".nojekyll").is_file())

    def test_force_replaces_a_hand_written_page(self) -> None:
        (self.docs / "index.html").write_bytes(b"<p>Ours.</p>\n")
        code, _, _ = self.export("--format", "html", "--force")
        self.assertEqual(code, 0)
        self.assertIn(b"Generated by devdocs", (self.docs / "index.html").read_bytes())
        self.assertIn("index.html", self.manifest()["files"])

    def test_a_newer_schemas_record_survives_an_export(self) -> None:
        # Regression: the manifest kept the newer label but lost its fields.
        newer = self.manifest()
        newer["schema_version"] = "1.1.0"
        newer["sources_root"] = "src"
        newer["documents"][0]["owners"] = ["team-a"]
        newer["run"]["embedding_model"] = "gemini-embedding-001"
        (self.docs / MANIFEST).write_text(json.dumps(newer), "utf-8")
        code, _, _ = self.export("--format", "html")
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual({k: v for k, v in manifest.items() if k != "files"},
                         {k: v for k, v in newer.items() if k != "files"})
        self.assertIn("index.html", manifest["files"])

    def test_pdf_options_need_pdf_output(self) -> None:
        for flags in (["--pdf-engine", "chrome", "--page-size", "letter"], ["--split"]):
            with self.subTest(flags):
                code, _, err = self.export(*flags)
                self.assertEqual(code, 2)
                self.assertIn("only to PDF output, which is not among the formats: "
                              "markdown", err)
        self.assertEqual(tree(self.docs), self.generated)

    def test_a_failing_engine_still_writes_the_other_formats(self) -> None:
        broken = DevDocsError("chrome could not print the book (exit status 1)")
        with mock.patch.object(external, "find", return_value="/bin/chrome"), \
                mock.patch.object(external, "render", side_effect=broken):
            code, _, err = self.export("--format", "html,pdf", "--pdf-engine", "chrome")
        self.assertEqual(code, 1)
        self.assertIn("chrome could not print the book", err)
        self.assertTrue((self.docs / "index.html").is_file())
        self.assertFalse((self.docs / "shop.pdf").exists())
        self.assertIn("index.html", self.manifest()["files"])

    def test_characters_the_pdf_fonts_lack_are_reported(self) -> None:
        self.generate("--description", "Sells books in 中文.")
        code, out, err = self.export("--format", "pdf")
        self.assertEqual(code, 0)
        self.assertIn("pdf: characters outside the built-in fonts were printed as "
                      "'?' (2):", err)
        self.assertIn("U+4E2D CJK UNIFIED IDEOGRAPH-4E2D in the overview", err)
        self.assertIn("use --pdf-engine chrome or --pdf-engine weasyprint", err)
        self.assertIn("2 rendering warnings", out)
        code, out, err = self.export("--format", "pdf", "--quiet")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 rendering warnings; --json lists them all", out)


class TestRefusals(ExportCase):
    def test_without_a_run_there_is_nothing_to_export(self) -> None:
        code, _, err = self.export("--format", "pdf")
        self.assertEqual(code, 2)
        self.assertIn("index.json is missing", err)
        self.assertIn("run `devdocs generate` first", err)
        self.assertFalse(self.docs.exists())

    def test_an_unreadable_manifest_is_refused(self) -> None:
        self.generate()
        newer = self.manifest()
        newer["schema_version"] = "2.0.0"
        for name, data in (("newer", json.dumps(newer).encode()), ("corrupt", b"{")):
            with self.subTest(name):
                (self.docs / MANIFEST).write_bytes(data)
                code, _, err = self.export("--format", "pdf")
                self.assertEqual(code, 2)
                self.assertIn("newer devdocs" if name == "newer" else "cannot read",
                              err)
                self.assertFalse((self.docs / "shop.pdf").exists())

    def test_a_bad_format_or_a_missing_engine_writes_nothing(self) -> None:
        self.generate()
        code, _, err = self.export("--format", "docx")
        self.assertEqual(code, 2)
        self.assertIn("unknown format", err)
        with mock.patch.object(external, "find", return_value=None):
            code, _, err = self.export("--format", "pdf", "--pdf-engine", "weasyprint")
        self.assertEqual(code, 2)
        self.assertIn("weasyprint PDF engine is not installed", err)
        self.assertEqual(tree(self.docs), self.generated)


class TestPipelineExport(ExportCase):
    def test_the_configuration_is_all_it_needs(self) -> None:
        self.generate()
        summary = Pipeline(Config(path=self.root, formats=("pdf",))).export()
        self.assertEqual((summary.formats, summary.documents), (["pdf"], 6))
        self.assertEqual(summary.written.written, ["index.json", "shop.pdf"])
        self.assertEqual(summary.out, str(self.docs.resolve()))

    def test_no_manifest_is_a_configuration_error(self) -> None:
        with self.assertRaises(ConfigError):
            Pipeline(Config(path=self.root)).export()


if __name__ == "__main__":
    unittest.main()
