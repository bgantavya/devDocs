"""Import resolution per language, entry points, and the shape of the graph.

Every test builds a tiny realistic project in a temporary directory: config
files are written to disk, where the builder reads them; sources are handed
over the way the pipeline hands them; and facts come from the real parsers,
unless a test pins a file's import strings to exercise one rule exactly.
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import tempfile
import unittest
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from devdocs.graph import FileNode, GraphBuilder, ModuleNode, ProjectGraph, is_stdlib
from devdocs.parse import FileFacts, get_parser
from devdocs.scan import LANGUAGES, Role, SourceFile

_TEST_DIRS = frozenset({"test", "tests", "__tests__"})

Prepared = tuple[list[SourceFile], dict[str, FileFacts], dict[str, str]]

SHOP: dict[str, str] = {
    "README.md": "# Shop\n",
    "pyproject.toml": '[project]\nname = "shop"\n',
    "shop/__init__.py": '"""Shop."""\n',
    "shop/cart.py": "from shop.money import Money\n",
    "shop/money.py": "class Money(int): ...\n",
    "shop/api/routes.py": (
        "from shop.cart import Cart\nfrom shop.money import Money\n"
    ),
    "tests/test_cart.py": (
        "from shop.cart import Cart\nfrom shop.cart import Cart as Again\n"
    ),
    "scripts/build.rb": "require 'json'\n",
}


def _role(relpath: str, language: str) -> Role:
    """The scanner's rule in miniature: vendored and test code by path."""
    *directories, name = relpath.split("/")
    if "vendor" in directories:
        return Role.VENDORED
    if _TEST_DIRS.intersection(directories) or name.startswith("test_"):
        return Role.TEST
    if name.endswith(("_test.go", ".test.ts")):
        return Role.TEST
    return Role.SOURCE if language else Role.CONFIG


def entries(graph: ProjectGraph) -> dict[str, str]:
    return {node.relpath: node.entry_point for node in graph.entry_points()}


def node(relpath: str, *internal: str) -> FileNode:
    return FileNode(relpath, "python", posixpath.dirname(relpath), internal=internal)


class GraphCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def prepare(
        self,
        files: Mapping[str, str],
        *,
        imports: Mapping[str, Sequence[str]] | None = None,
        roles: Mapping[str, Role] | None = None,
        unscanned: Iterable[str] = (),
    ) -> Prepared:
        """Write `files` and return what the pipeline would hand the builder.

        Files in `unscanned` exist on disk only, like the empty __init__.py
        or the go.mod a scan skips. A file listed in `imports` gets exactly
        those import strings; every other file goes through its parser.
        """
        pinned = imports or {}
        hidden = set(unscanned)
        scanned: list[SourceFile] = []
        facts: dict[str, FileFacts] = {}
        sources: dict[str, str] = {}
        for relpath, text in files.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if relpath in hidden:
                continue
            language = LANGUAGES.get(posixpath.splitext(relpath)[1].lower(), "")
            role = (roles or {}).get(relpath) or _role(relpath, language)
            scanned.append(
                SourceFile(path, relpath, language, role, len(text), text.count("\n"))
            )
            if not language:
                continue
            sources[relpath] = text
            parser = get_parser(language)
            if relpath in pinned:
                facts[relpath] = FileFacts(
                    path, language, imports=tuple(pinned[relpath])
                )
            elif parser is not None:
                facts[relpath] = parser.parse(path, text)
        return scanned, facts, sources

    def build(
        self,
        files: Mapping[str, str],
        *,
        imports: Mapping[str, Sequence[str]] | None = None,
        roles: Mapping[str, Role] | None = None,
        unscanned: Iterable[str] = (),
    ) -> ProjectGraph:
        scanned, facts, sources = self.prepare(
            files, imports=imports, roles=roles, unscanned=unscanned
        )
        return GraphBuilder(self.root, scanned, facts, sources).build()

    def assertLinks(
        self,
        graph: ProjectGraph,
        relpath: str,
        internal: Sequence[str],
        external: Sequence[str] = (),
    ) -> None:
        found = graph.files[relpath]
        self.assertEqual(
            (found.internal, found.external),
            (tuple(internal), tuple(external)),
            relpath,
        )


class PythonTest(GraphCase):
    def test_absolute_imports_take_the_longest_prefix_naming_a_module(self) -> None:
        graph = self.build(
            {
                "shop/__init__.py": 'VERSION = "1.0"\n',
                "shop/cart.py": "from shop.money import Money\n",
                "shop/money.py": "class Money(int): ...\n",
                "shop/payments/__init__.py": "from .stripe import charge\n",
                "shop/payments/stripe.py": "import requests\n\ndef charge(): ...\n",
                "app.py": "import shop\n",
            },
            imports={
                "app.py": [
                    "shop.cart.Cart",
                    "shop.payments",
                    "shop.payments.stripe.charge",
                    "shop.VERSION",
                    "shop.missing.Thing",
                ],
            },
        )
        self.assertLinks(
            graph,
            "app.py",
            [
                "shop/__init__.py",
                "shop/cart.py",
                "shop/payments/__init__.py",
                "shop/payments/stripe.py",
            ],
        )
        self.assertLinks(graph, "shop/cart.py", ["shop/money.py"])
        self.assertLinks(
            graph, "shop/payments/__init__.py", ["shop/payments/stripe.py"]
        )
        self.assertLinks(graph, "shop/payments/stripe.py", [], ["requests"])

    def test_outside_packages_are_external_by_their_top_level_name(self) -> None:
        graph = self.build(
            {
                "shop/__init__.py": "",
                "shop/cart.py": (
                    "import os.path\n"
                    "import requests.adapters\n"
                    "from yaml import safe_load\n"
                    "import shop.gone\n"
                    "from shop import money\n"
                ),
                "shop/money.py": "class Money(int): ...\n",
            },
            unscanned=["shop/__init__.py"],
        )
        # shop.gone names the project's own package, so it is not external.
        self.assertLinks(
            graph, "shop/cart.py", ["shop/money.py"], ["os", "requests", "yaml"]
        )

    def test_an_unscanned_empty_init_still_marks_a_package_root(self) -> None:
        graph = self.build(
            {
                "backend/manage.py": "from shop.money import Money\n",
                "backend/shop/__init__.py": "",
                "backend/shop/money.py": "class Money(int): ...\n",
            },
            unscanned=["backend/shop/__init__.py"],
        )
        self.assertLinks(graph, "backend/manage.py", ["backend/shop/money.py"])

    def test_src_and_nested_project_layouts_resolve_to_the_nearest_file(self) -> None:
        graph = self.build(
            {
                "src/acme/__init__.py": '"""Acme."""\n',
                "src/acme/core.py": "def run(): ...\n",
                "services/api/app/__init__.py": '"""API."""\n',
                "services/api/app/models.py": "class User: ...\n",
                "services/api/main.py": "from app.models import User\n",
                "services/worker/app/__init__.py": '"""Worker."""\n',
                "services/worker/app/models.py": "class Job: ...\n",
                "services/worker/run.py": "import app.models\n",
                "tests/test_core.py": (
                    "from acme.core import run\nfrom app.models import User\n"
                ),
            }
        )
        self.assertLinks(graph, "services/api/main.py", ["services/api/app/models.py"])
        self.assertLinks(
            graph, "services/worker/run.py", ["services/worker/app/models.py"]
        )
        # No directory in common with either app: the first in sorted order.
        self.assertLinks(
            graph,
            "tests/test_core.py",
            ["services/api/app/models.py", "src/acme/core.py"],
        )
        core = graph.files["src/acme/core.py"]
        self.assertEqual(core.tested_by, ("tests/test_core.py",))

    def test_relative_imports_climb_from_the_importing_package(self) -> None:
        graph = self.build(
            {
                "pkg/__init__.py": 'VERSION = "2"\n',
                "pkg/b.py": "def helper(): ...\n",
                "pkg/sub/__init__.py": "from .d import thing\n",
                "pkg/sub/c.py": (
                    "from ..b import helper\n"
                    "from . import d\n"
                    "from .. import VERSION\n"
                    "from . import *\n"
                    "from .... import too_far\n"
                ),
                "pkg/sub/d.py": "thing = 1\n",
                "scripts/run.py": "from .gone import x\n",
            }
        )
        # `from .. import VERSION` names no module, so it is pkg/__init__.py.
        self.assertLinks(
            graph,
            "pkg/sub/c.py",
            ["pkg/__init__.py", "pkg/b.py", "pkg/sub/__init__.py", "pkg/sub/d.py"],
        )
        self.assertLinks(graph, "pkg/sub/__init__.py", ["pkg/sub/d.py"])
        self.assertLinks(graph, "scripts/run.py", [])

    def test_a_loose_module_is_seen_only_from_its_directory_and_below(self) -> None:
        graph = self.build(
            {
                "tools/logging.py": "def log(): ...\n",
                "tools/helpers.py": "def bump(): ...\n",
                "tools/release.py": "import logging\nimport helpers\n",
                "tools/run-all.py": "import logging\n",
                "tools/sub/bump.py": "import logging\n",
                "src/app.py": "import logging\nimport helpers\n",
                "tests/helpers.py": "def make(): ...\n",
                "tests/unit/test_a.py": "from helpers import make\n",
            }
        )
        self.assertLinks(
            graph, "tools/release.py", ["tools/helpers.py", "tools/logging.py"]
        )
        self.assertLinks(graph, "tools/run-all.py", ["tools/logging.py"])
        self.assertLinks(graph, "tools/sub/bump.py", ["tools/logging.py"])
        self.assertLinks(graph, "src/app.py", [], ["helpers", "logging"])
        self.assertLinks(graph, "tests/unit/test_a.py", ["tests/helpers.py"])

    def test_stubs_resolve_and_a_module_beats_its_stub(self) -> None:
        graph = self.build(
            {
                "pkg/__init__.py": "x = 1\n",
                "pkg/fast.pyi": "def f() -> int: ...\n",
                "pkg/both.py": "def g(): ...\n",
                "pkg/both.pyi": "def g() -> None: ...\n",
                "pkg/use.py": "from .both import g\nfrom .fast import f\n",
                "main.py": "import pkg.fast\nimport pkg.both\n",
            }
        )
        self.assertLinks(graph, "main.py", ["pkg/both.py", "pkg/fast.pyi"])
        self.assertLinks(graph, "pkg/use.py", ["pkg/both.py", "pkg/fast.pyi"])

    def test_a_module_naming_itself_is_neither_internal_nor_external(self) -> None:
        graph = self.build(
            {
                "single.py": "import single\n",
                "pkg/__init__.py": "x = 1\n",
                "pkg/mod.py": "from pkg.mod import thing\n",
            }
        )
        self.assertLinks(graph, "single.py", [])
        self.assertLinks(graph, "pkg/mod.py", [])

    def test_entry_points_in_order_of_precedence(self) -> None:
        pyproject = (
            '[project]\nname = "shop"\n\n'
            "[project.scripts]\n"
            'shop = "shop.cli:main"\n'
            'shop-admin = "shop.admin:run [extra]"\n\n'
            "[project.gui-scripts]\n"
            'shop-gui = "shop.gui:start"\n\n'
            "[tool.poetry.scripts]\n"
            'legacy = "shop.legacy:main"\n'
            'table = { callable = "shop.table:main" }\n'
            'dunder = "shop.__main__:main"\n'
            'broken = "not a module:main"\n'
            'ghost = "shop.ghost:main"\n'
            'shell = { reference = "bin/run.sh", type = "file" }\n'
            "number = 42\n"
        )
        graph = self.build(
            {
                "pyproject.toml": pyproject,
                "shop/__init__.py": 'VERSION = "1"\n',
                "shop/__main__.py": "from shop.cli import main\n\nmain()\n",
                "shop/cli.py": (
                    "def main(): ...\n\n\nif __name__ == '__main__':\n    main()\n"
                ),
                "shop/admin.py": "def run(): ...\n",
                "shop/gui.py": "def start(): ...\n",
                "shop/legacy.py": "def main(): ...\n",
                "shop/table.py": "def main(): ...\n",
                "tools/report.py": (
                    'def main(): ...\n\n\nif "__main__" == __name__:\n    main()\n'
                ),
                "tools/seed.py": 'if __name__ == "__main__":\n    pass\n',
                "tools/nested.py": (
                    'def f():\n    if __name__ == "__main__":\n        pass\n'
                ),
                "tests/test_cli.py": (
                    "import unittest\n\n"
                    'if __name__ == "__main__":\n    unittest.main()\n'
                ),
            }
        )
        self.assertEqual(
            entries(graph),
            {
                "shop/__main__.py": "runs with python -m",
                "shop/admin.py": "console script `shop-admin`",
                "shop/cli.py": "console script `shop`",
                "shop/gui.py": "console script `shop-gui`",
                "shop/legacy.py": "console script `legacy`",
                "shop/table.py": "console script `table`",
                "tools/report.py": "runs as a script",
                "tools/seed.py": "runs as a script",
            },
        )


class ScriptTest(GraphCase):
    def test_relative_specifiers_probe_files_extensions_and_index(self) -> None:
        graph = self.build(
            {
                "web/src/index.ts": "export {}\n",
                "web/src/old.js": "module.exports = {}\n",
                "web/src/util.ts": "export const u = 1\n",
                "web/src/components/index.tsx": "export {}\n",
                "web/src/legacy.ts": "export const l = 1\n",
                "web/src/legacy.js": "exports.l = 1\n",
                "web/src/types.d.ts": "export type T = 1\n",
                "web/src/Button.vue": "<template><button /></template>\n",
                "web/src/worker.ts": "export {}\n",
                "web/src/data.json": "{}\n",
                "main.ts": "export {}\n",
            },
            imports={
                "main.ts": ["../escape", "./web/src/util"],
                "web/src/index.ts": [
                    "./util",
                    "./components",
                    "./legacy.js",
                    "./types",
                    "./Button.vue",
                    "./worker?worker",
                    "./data.json",
                    "./styles.css?inline",
                    "../README.md",
                ],
                "web/src/old.js": ["./legacy.js", "./util"],
            },
        )
        # TypeScript reads `./legacy.js` as its TS twin; plain JS keeps the file.
        self.assertLinks(
            graph,
            "web/src/index.ts",
            [
                "web/src/Button.vue",
                "web/src/components/index.tsx",
                "web/src/legacy.ts",
                "web/src/types.d.ts",
                "web/src/util.ts",
                "web/src/worker.ts",
            ],
        )
        self.assertLinks(
            graph, "web/src/old.js", ["web/src/legacy.js", "web/src/util.ts"]
        )
        self.assertLinks(graph, "main.ts", ["web/src/util.ts"])

    def test_dot_specifiers_and_trailing_slashes_name_directories(self) -> None:
        graph = self.build(
            {
                "src/lib.ts": "export const flat = 1\n",
                "src/lib/index.ts": "export const dir = 1\n",
                "src/lib/core/x.ts": "export {}\n",
            },
            imports={"src/lib/core/x.ts": ["..", "../", "../../lib", "./"]},
        )
        self.assertLinks(graph, "src/lib/core/x.ts", ["src/lib.ts", "src/lib/index.ts"])

    def test_tsconfig_paths_and_base_url_from_the_nearest_config(self) -> None:
        tsconfig = (
            "{\n"
            "  // Aliases for the whole repository.\n"
            '  "$schema": "https://json.schemastore.org/tsconfig",\n'
            '  "compilerOptions": {\n'
            '    "baseUrl": "src", /* targets are relative to src */\n'
            '    "paths": {\n'
            '      "@/*": ["*"],\n'
            '      "@config": ["config/index.ts"],\n'
            '      "@ui/*": ["../packages/ui/src/*"],\n'
            '      "*": ["types/*"],\n'
            '      "bad": "not a list",\n'
            '      "**/deep/*": ["deep/*"],\n'
            "    },\n"
            "  },\n"
            "}\n"
        )
        graph = self.build(
            {
                "tsconfig.json": tsconfig,
                "src/app.ts": "export {}\n",
                "src/lib/api.ts": "export {}\n",
                "src/config/index.ts": "export {}\n",
                "packages/ui/src/button.tsx": "export {}\n",
                "admin/tsconfig.json": (
                    '{"compilerOptions": {"paths": {"@/*": ["./app/*"]}}}'
                ),
                "admin/app/main.ts": "export {}\n",
                "admin/app/lib/auth.ts": "export {}\n",
            },
            imports={
                "src/app.ts": [
                    "@/lib/api",
                    "@config",
                    "lib/api",
                    "@ui/button",
                    "@/missing",
                    "react",
                    "lodash/fp",
                ],
                "admin/app/main.ts": ["@/lib/auth", "@config"],
            },
            unscanned=["tsconfig.json"],
        )
        self.assertLinks(
            graph,
            "src/app.ts",
            ["packages/ui/src/button.tsx", "src/config/index.ts", "src/lib/api.ts"],
            ["lodash", "react"],
        )
        # The nearest config has no `@config`, so the root one does not apply.
        self.assertLinks(
            graph, "admin/app/main.ts", ["admin/app/lib/auth.ts"], ["@config"]
        )

    def test_jsconfig_is_read_and_tsconfig_wins_in_the_same_directory(self) -> None:
        graph = self.build(
            {
                "legacy/jsconfig.json": '{"compilerOptions": {"baseUrl": "."}}',
                "legacy/main.js": "export {}\n",
                "legacy/utils/date.js": "export {}\n",
                "both/jsconfig.json": (
                    '{"compilerOptions": {"paths": {"@/*": ["./js/*"]}}}'
                ),
                "both/tsconfig.json": (
                    '{"compilerOptions": {"paths": {"@/*": ["./ts/*"]}}}'
                ),
                "both/main.ts": "export {}\n",
                "both/js/x.ts": "export {}\n",
                "both/ts/x.ts": "export {}\n",
            },
            imports={
                "legacy/main.js": ["utils/date", "moment"],
                "both/main.ts": ["@/x"],
            },
        )
        self.assertLinks(graph, "legacy/main.js", ["legacy/utils/date.js"], ["moment"])
        self.assertLinks(graph, "both/main.ts", ["both/ts/x.ts"])

    def test_workspace_packages_resolve_to_their_entry_or_inside(self) -> None:
        graph = self.build(
            {
                "package.json": '{"name": "monorepo", "workspaces": ["packages/*"]}',
                "packages/ui/package.json": (
                    '{"name": "@acme/ui", "main": "dist/index.js",'
                    ' "source": "src/index.ts"}'
                ),
                "packages/ui/src/index.ts": "export {}\n",
                "packages/ui/src/button.tsx": "export {}\n",
                "packages/utils/package.json": (
                    '{"name": "utils", "exports": {".": {"import": "./lib/index.mjs",'
                    ' "require": "./lib/index.cjs"}}}'
                ),
                "packages/utils/lib/index.mjs": "export {}\n",
                "packages/utils/lib/index.cjs": "module.exports = {}\n",
                "packages/core/package.json": '{"name": "@acme/core"}',
                "packages/core/src/index.ts": "export {}\n",
                "packages/legacy/package.json": (
                    '{"name": "legacy-lib", "main": "gone.js"}'
                ),
                "packages/legacy/index.js": "module.exports = {}\n",
                "packages/feature/package.json": (
                    '{"name": "feature", "exports": {"./x": "./x.js"}}'
                ),
                "packages/feature/src/index.ts": "export {}\n",
                "packages/empty/package.json": '{"name": "@acme/empty"}',
                "apps/web/main.ts": "export {}\n",
            },
            imports={
                "apps/web/main.ts": [
                    "@acme/ui",
                    "@acme/ui/src/button",
                    "utils",
                    "@acme/core",
                    "legacy-lib",
                    "feature",
                    "@acme/empty",
                    "@acme/ui/gone",
                    "monorepo",
                    "react",
                ],
            },
        )
        # What a workspace package lacks is dropped, never external; the
        # root package.json maps no name.
        self.assertLinks(
            graph,
            "apps/web/main.ts",
            [
                "packages/core/src/index.ts",
                "packages/feature/src/index.ts",
                "packages/legacy/index.js",
                "packages/ui/src/button.tsx",
                "packages/ui/src/index.ts",
                "packages/utils/lib/index.mjs",
            ],
            ["monorepo", "react"],
        )

    def test_outside_packages_are_external_by_package_name(self) -> None:
        graph = self.build(
            {"app.ts": "export {}\n"},
            imports={
                "app.ts": [
                    "react",
                    "react-dom/client",
                    "@scope/pkg/sub/path",
                    "node:fs/promises",
                    "node:path",
                    "lodash/fp",
                    "~/components/x",
                    "@/lib/y",
                    "#internal/z",
                    "https://esm.sh/preact",
                ],
            },
        )
        # `~/`, `@/` and `#` are project aliases, never npm packages.
        self.assertLinks(
            graph,
            "app.ts",
            [],
            [
                "@scope/pkg",
                "https://esm.sh/preact",
                "lodash",
                "node:fs",
                "node:path",
                "react",
                "react-dom",
            ],
        )

    def test_entry_points_from_manifests_and_routes(self) -> None:
        graph = self.build(
            {
                "package.json": (
                    '{"name": "shopctl", "bin": "./bin/shopctl.js",'
                    ' "main": "./lib/index.js", "module": "./lib/index.mjs"}'
                ),
                "bin/shopctl.js": "#!/usr/bin/env node\n",
                "lib/index.js": "module.exports = {}\n",
                "lib/index.mjs": "export {}\n",
                "packages/cli/package.json": (
                    '{"name": "@acme/cli", "bin": {"acme": "./src/main.ts",'
                    ' "acme-dev": "./src/dev.ts"}, "exports": "./src/index.ts"}'
                ),
                "packages/cli/src/main.ts": "export {}\n",
                "packages/cli/src/dev.ts": "export {}\n",
                "packages/cli/src/index.ts": "export {}\n",
                "packages/tool/package.json": (
                    '{"name": "@acme/tool", "bin": "./run.js", "main": "./run.js"}'
                ),
                "packages/tool/run.js": "#!/usr/bin/env node\n",
                "packages/ghost/package.json": (
                    '{"name": "ghost", "bin": {"ghost": "./missing.js"}}'
                ),
                "apps/site/package.json": '{"name": "site", "private": true}',
                "apps/site/app/page.tsx": "export default function Page() {}\n",
                "apps/site/app/blog/[slug]/page.tsx": "export {}\n",
                "apps/site/app/api/users/route.ts": "export async function GET() {}\n",
                "apps/site/app/layout.tsx": "export {}\n",
                "apps/site/app/route.tsx": "export {}\n",
                "apps/blog/package.json": '{"name": "blog"}',
                "apps/blog/src/pages/index.tsx": "export {}\n",
                "apps/blog/src/pages/_app.tsx": "export {}\n",
                "apps/blog/src/pages/api/hello.ts": "export {}\n",
                "apps/blog/src/components/Nav.tsx": "export {}\n",
            }
        )
        # A lone bin is named after the package without its scope, as npm does.
        self.assertEqual(
            entries(graph),
            {
                "apps/blog/src/pages/api/hello.ts": "API route",
                "apps/blog/src/pages/index.tsx": "route page",
                "apps/site/app/api/users/route.ts": "route handler",
                "apps/site/app/blog/[slug]/page.tsx": "route page",
                "apps/site/app/page.tsx": "route page",
                "bin/shopctl.js": "command `shopctl`",
                "lib/index.js": "package entry",
                "lib/index.mjs": "package entry",
                "packages/cli/src/dev.ts": "command `acme-dev`",
                "packages/cli/src/index.ts": "package entry",
                "packages/cli/src/main.ts": "command `acme`",
                "packages/tool/run.js": "command `tool`",
            },
        )

    def test_parsed_imports_resolve_end_to_end(self) -> None:
        graph = self.build(
            {
                "src/index.ts": (
                    "import { b } from './lib/b.js'\n"
                    "import React from 'react'\n"
                    "export * from './lib/c'\n"
                    "const lazy = () => import('./lib/lazy')\n"
                    "// import ghost from './lib/ghost'\n"
                ),
                "src/lib/b.ts": "export const b = 1\n",
                "src/lib/c.tsx": "export const c = 1\n",
                "src/lib/lazy.ts": "export default 1\n",
                "src/lib/ghost.ts": "export default 1\n",
                "src/index.test.ts": "import { b } from './lib/b'\n",
            }
        )
        self.assertLinks(
            graph,
            "src/index.ts",
            ["src/lib/b.ts", "src/lib/c.tsx", "src/lib/lazy.ts"],
            ["react"],
        )
        self.assertEqual(graph.files["src/lib/b.ts"].dependents, ("src/index.ts",))
        self.assertEqual(graph.files["src/lib/b.ts"].tested_by, ("src/index.test.ts",))


class GoTest(GraphCase):
    def test_imports_under_the_module_path_link_every_non_test_file(self) -> None:
        graph = self.build(
            {
                "go.mod": (
                    "module github.com/acme/shop\n\ngo 1.22\n\n"
                    "require github.com/lib/pq v1.10.9\n"
                ),
                "cmd/shop/main.go": (
                    "package main\n\n"
                    "import (\n"
                    '\t"fmt"\n'
                    '\t"net/http"\n\n'
                    '\t"github.com/acme/shop/internal/store"\n'
                    '\t"github.com/acme/shop/internal/gone"\n'
                    '\t_ "github.com/lib/pq"\n'
                    '\t"golang.org/x/sync/errgroup"\n'
                    ")\n\n"
                    "func main() {\n\tfmt.Println(store.Open())\n}\n"
                ),
                "internal/store/store.go": (
                    "package store\n\nfunc Open() int { return 1 }\n"
                ),
                "internal/store/cache.go": (
                    "package store\n\nvar cache = map[string]int{}\n"
                ),
                "internal/store/store_test.go": (
                    "package store_test\n\n"
                    'import (\n\t"testing"\n\n'
                    '\t"github.com/acme/shop/internal/store"\n)\n\n'
                    "func TestOpen(t *testing.T) { store.Open() }\n"
                ),
            },
            unscanned=["go.mod"],
        )
        store = ["internal/store/cache.go", "internal/store/store.go"]
        self.assertLinks(
            graph,
            "cmd/shop/main.go",
            store,
            ["fmt", "github.com/lib/pq", "golang.org/x/sync", "net/http"],
        )
        self.assertLinks(graph, "internal/store/store_test.go", store, ["testing"])
        found = graph.files["internal/store/store.go"]
        self.assertEqual(found.dependents, ("cmd/shop/main.go",))
        self.assertEqual(found.tested_by, ("internal/store/store_test.go",))

    def test_the_longest_module_path_wins(self) -> None:
        graph = self.build(
            {
                "go.mod": "module example.com/mono\n",
                "modules/tools/go.mod": 'module "example.com/mono/tools"\n',
                "modules/tools/gen/gen.go": "package gen\n",
                "tools/gen/gen.go": "package gen\n",
                "lib/lib.go": "package lib\n",
                "app/app.go": "package app\n",
            },
            imports={
                "app/app.go": ["example.com/mono/tools/gen", "example.com/mono/lib"]
            },
            unscanned=["go.mod", "modules/tools/go.mod"],
        )
        self.assertLinks(
            graph, "app/app.go", ["lib/lib.go", "modules/tools/gen/gen.go"]
        )

    def test_entry_point_needs_package_main_and_func_main_in_code(self) -> None:
        graph = self.build(
            {
                "cmd/a/main.go": "package main\n\nfunc main() {}\n",
                "cmd/b/main.go": "package main\n\nfunc run() {}\n",
                "cmd/c/doc.go": "package main\n\nvar usage = `\nfunc main() {\n`\n",
                "cmd/d/main.go": (
                    "/*\npackage main\n*/\npackage tool\n\nfunc main() {}\n"
                ),
                "cmd/e/main_test.go": "package main\n\nfunc main() {}\n",
                "lib/x.go": "package lib\n\nfunc main() {}\n",
                "vendor/github.com/x/tool/main.go": "package main\n\nfunc main() {}\n",
            }
        )
        self.assertEqual(
            entries(graph), {"cmd/a/main.go": "program entry (`func main`)"}
        )


class RustTest(GraphCase):
    def test_crate_self_super_and_mod_paths(self) -> None:
        graph = self.build(
            {
                "Cargo.toml": '[package]\nname = "shop-core"\nversion = "0.1.0"\n',
                "src/lib.rs": (
                    "pub mod store;\nmod money;\n\n"
                    "use crate::money::Price;\n"
                    "use serde::Serialize;\n"
                    "use std::collections::HashMap;\n"
                    "use ::std::fmt;\n"
                ),
                "src/money.rs": (
                    "pub struct Price;\n\n"
                    "#[cfg(test)]\nmod tests {\n    use super::*;\n}\n"
                ),
                "src/store/mod.rs": (
                    "pub mod cart;\n\nuse super::money::Price;\n\n"
                    "fn total() -> u32 {\n    if true { 1 } else { 2 }\n}\n"
                ),
                "src/store/cart.rs": (
                    "mod line;\n\n"
                    "use super::super::money::Price;\n"
                    "use super::Inventory;\n"
                    "use crate::store;\n"
                    "use self::line::Line;\n"
                    "use self::Cart as Basket;\n\n"
                    "pub struct Cart;\n"
                ),
                "src/store/cart/line.rs": "pub struct Line;\n",
                "src/main.rs": "use shop_core::store::cart::Cart;\n\nfn main() {}\n",
            }
        )
        self.assertLinks(
            graph, "src/lib.rs", ["src/money.rs", "src/store/mod.rs"], ["serde", "std"]
        )
        # `use super::*` inside `mod tests` means the file itself.
        self.assertLinks(graph, "src/money.rs", [])
        self.assertLinks(
            graph, "src/store/mod.rs", ["src/money.rs", "src/store/cart.rs"]
        )
        self.assertLinks(
            graph,
            "src/store/cart.rs",
            ["src/money.rs", "src/store/cart/line.rs", "src/store/mod.rs"],
        )
        self.assertLinks(graph, "src/main.rs", ["src/store/cart.rs"])
        self.assertEqual(
            entries(graph),
            {"src/lib.rs": "library root", "src/main.rs": "binary entry"},
        )

    def test_workspace_crates_targets_and_test_modules(self) -> None:
        graph = self.build(
            {
                "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
                "crates/model/Cargo.toml": (
                    '[package]\nname = "shop-model"\n\n[lib]\npath = "src/model.rs"\n'
                ),
                "crates/model/src/model.rs": "pub mod user;\n",
                "crates/model/src/user.rs": "pub struct User;\n",
                "crates/api/Cargo.toml": (
                    '[package]\nname = "api"\n\n'
                    '[[bin]]\nname = "gen"\npath = "tools/gen.rs"\n'
                ),
                "crates/api/src/main.rs": (
                    "use shop_model::user::User;\n"
                    "use shop_model::Thing;\n\n"
                    "fn main() {}\n"
                ),
                "crates/api/src/bin/migrate.rs": "fn main() {}\n",
                "crates/api/tools/gen.rs": "fn main() {}\n",
                "crates/api/build.rs": "fn main() {}\n",
                "crates/api/tests/smoke.rs": "mod common;\n\nuse api::*;\n",
                "crates/api/tests/common/mod.rs": "pub fn setup() {}\n",
            }
        )
        self.assertLinks(
            graph,
            "crates/api/src/main.rs",
            ["crates/model/src/model.rs", "crates/model/src/user.rs"],
        )
        self.assertLinks(
            graph, "crates/model/src/model.rs", ["crates/model/src/user.rs"]
        )
        self.assertLinks(
            graph,
            "crates/api/tests/smoke.rs",
            ["crates/api/src/main.rs", "crates/api/tests/common/mod.rs"],
        )
        api = graph.files["crates/api/src/main.rs"]
        self.assertEqual(api.tested_by, ("crates/api/tests/smoke.rs",))
        self.assertEqual(
            entries(graph),
            {
                "crates/api/src/bin/migrate.rs": "binary entry",
                "crates/api/src/main.rs": "binary entry",
                "crates/api/tools/gen.rs": "binary entry",
                "crates/model/src/model.rs": "library root",
            },
        )

    def test_mod_declarations_are_read_from_code_only(self) -> None:
        graph = self.build(
            {
                "Cargo.toml": '[package]\nname = "app"\n',
                "src/lib.rs": (
                    "// mod ghost;\n"
                    'const NOTE: &str = "mod phantom;";\n'
                    "/* mod hidden; */\n"
                    "mod store;\n"
                    "mod outer {\n    mod nested;\n}\n"
                    "use store::Cart;\n"
                ),
                "src/ghost.rs": "pub struct Ghost;\n",
                "src/phantom.rs": "pub struct Phantom;\n",
                "src/hidden.rs": "pub struct Hidden;\n",
                "src/store.rs": "pub struct Cart;\n",
                "src/outer/nested.rs": "use super::Thing;\n\npub fn f() {}\n",
            }
        )
        self.assertLinks(graph, "src/lib.rs", ["src/outer/nested.rs", "src/store.rs"])
        # `outer` is inline in lib.rs, so that is where `super::Thing` lives.
        self.assertLinks(graph, "src/outer/nested.rs", ["src/lib.rs"])

    def test_crate_relative_paths_that_lead_nowhere_are_dropped(self) -> None:
        graph = self.build(
            {
                "Cargo.toml": '[package]\nname = "app"\n',
                "src/lib.rs": "pub fn f() {}\n",
                "scripts/tool.rs": "fn main() {}\n",
            },
            imports={
                "src/lib.rs": ["super::x"],
                "scripts/tool.rs": ["crate::util", "self::y", "regex::Regex"],
            },
        )
        self.assertLinks(graph, "src/lib.rs", [])
        self.assertLinks(graph, "scripts/tool.rs", [], ["regex"])


class JavaTest(GraphCase):
    BASE = "src/main/java/com/acme"

    def java(
        self, package: str, name: str, imports: str = "", members: str = ""
    ) -> tuple[str, str]:
        """(relpath, source) of a one-class file in `com.acme.<package>`."""
        relpath = f"{self.BASE}/{package}/{name}.java"
        header = f"package com.acme.{package};\n\n{imports}"
        return relpath, f"{header}public class {name} {{\n{members}}}\n"

    def test_imports_wildcards_static_members_and_same_package(self) -> None:
        cart = (
            "// Copyright Acme\n"
            "/*\npackage com.fake;\n*/\n"
            "package com.acme.store;\n\n"
            "import com.acme.util.Strings;\n"
            "import com.acme.util.*;\n"
            "import static com.acme.util.Numbers.round;\n"
            "import com.acme.store.Gone;\n"
            "import org.springframework.boot.SpringApplication;\n"
            "import org.junit.*;\n"
            "import java.util.List;\n\n"
            "public class Cart {\n"
            "    private Item item;          // a Price is never stored\n"
            '    private String label = "Discount";\n'
            "    private Joiner joiner;\n"
            "    public static class Line {}\n"
            "}\n"
        )
        files = dict(
            [
                (f"{self.BASE}/store/Cart.java", cart),
                self.java("store", "Item"),
                self.java("store", "Price"),
                self.java("store", "Discount"),
                self.java("util", "Strings"),
                self.java("util", "Numbers"),
                self.java("util", "Joiner"),
                self.java("util", "Unused"),
                self.java("web", "Api", "import com.acme.store.Cart.Line;\n\n"),
                (
                    f"{self.BASE}/store/package-info.java",
                    "@Deprecated\npackage com.acme.store;\n",
                ),
                (f"{self.BASE}/empty/package-info.java", "package com.acme.empty;\n"),
            ]
        )
        graph = self.build(files)
        # Price is only in a comment and Discount only in a string; a
        # wildcard import links the classes the file actually mentions.
        self.assertLinks(
            graph,
            f"{self.BASE}/store/Cart.java",
            [
                f"{self.BASE}/store/Item.java",
                f"{self.BASE}/util/Joiner.java",
                f"{self.BASE}/util/Numbers.java",
                f"{self.BASE}/util/Strings.java",
            ],
            ["java.util", "org.junit", "org.springframework.boot"],
        )
        self.assertLinks(
            graph, f"{self.BASE}/web/Api.java", [f"{self.BASE}/store/Cart.java"]
        )

    def test_same_package_tests_link_and_imports_shadow_package_classes(self) -> None:
        field = "    private Logger log;\n"
        files = dict(
            [
                self.java("log", "Logger"),
                self.java("log", "Service", "import org.slf4j.Logger;\n\n", field),
                self.java("log", "Audit", members=field),
                (
                    "src/test/java/com/acme/log/AuditTest.java",
                    "package com.acme.log;\n\n"
                    "class AuditTest {\n    Audit audit = new Audit();\n}\n",
                ),
            ]
        )
        graph = self.build(files)
        self.assertLinks(graph, f"{self.BASE}/log/Service.java", [], ["org.slf4j"])
        self.assertLinks(
            graph, f"{self.BASE}/log/Audit.java", [f"{self.BASE}/log/Logger.java"]
        )
        self.assertEqual(
            graph.files[f"{self.BASE}/log/Audit.java"].tested_by,
            ("src/test/java/com/acme/log/AuditTest.java",),
        )

    def test_a_static_main_method_is_an_entry_point(self) -> None:
        graph = self.build(
            {
                f"{self.BASE}/App.java": (
                    "package com.acme;\n\npublic class App {\n"
                    "    public static void main(String[] args) {}\n}\n"
                ),
                f"{self.BASE}/Lib.java": (
                    "package com.acme;\n\npublic class Lib {\n"
                    "    // public static void main(String[] args) {}\n"
                    "    public void main() {}\n}\n"
                ),
            }
        )
        self.assertEqual(entries(graph), {f"{self.BASE}/App.java": "`main` method"})


class GraphShapeTest(GraphCase):
    def test_nodes_dependents_tested_by_and_modules(self) -> None:
        graph = self.build(SHOP)
        self.assertEqual(
            {path: found.module for path, found in graph.files.items()},
            {
                "scripts/build.rb": "scripts",
                "shop/__init__.py": "shop",
                "shop/api/routes.py": "shop/api",
                "shop/cart.py": "shop",
                "shop/money.py": "shop",
                "tests/test_cart.py": "tests",
            },
        )
        self.assertEqual(
            graph.files["shop/cart.py"],
            FileNode(
                relpath="shop/cart.py",
                language="python",
                module="shop",
                internal=("shop/money.py",),
                dependents=("shop/api/routes.py",),
                tested_by=("tests/test_cart.py",),
            ),
        )
        self.assertEqual(graph.files["shop/cart.py"].fan_in, 1)
        money = graph.files["shop/money.py"]
        self.assertEqual(money.dependents, ("shop/api/routes.py", "shop/cart.py"))
        self.assertEqual(
            graph.modules,
            {
                "scripts": ModuleNode("scripts", ("scripts/build.rb",)),
                "shop": ModuleNode(
                    "shop",
                    ("shop/__init__.py", "shop/cart.py", "shop/money.py"),
                    used_by=("shop/api", "tests"),
                ),
                "shop/api": ModuleNode(
                    "shop/api", ("shop/api/routes.py",), depends_on=("shop",)
                ),
                "tests": ModuleNode(
                    "tests", ("tests/test_cart.py",), depends_on=("shop",)
                ),
            },
        )
        self.assertEqual(
            graph.module_edges(), {("shop/api", "shop"): 2, ("tests", "shop"): 1}
        )

    def test_the_graph_does_not_depend_on_input_order(self) -> None:
        scanned, facts, sources = self.prepare(SHOP)
        forward = GraphBuilder(self.root, scanned, facts, sources).build()
        backward = GraphBuilder(self.root, scanned[::-1], facts, sources).build()
        self.assertEqual(forward, backward)
        self.assertEqual(list(forward.files), sorted(forward.files))
        self.assertEqual(list(forward.modules), list(backward.modules))

    def test_files_missing_facts_or_sources_read_as_empty(self) -> None:
        scanned, _, _ = self.prepare(
            {
                "Cargo.toml": '[package]\nname = "app"\n',
                "src/lib.rs": "mod a;\n",
                "src/a.rs": "pub fn f() {}\n",
                "App.java": "package com.acme;\npublic class App {}\n",
                "main.go": "package main\nfunc main() {}\n",
                "tool.py": 'if __name__ == "__main__":\n    pass\n',
            }
        )
        graph = GraphBuilder(self.root, scanned, {}, {}).build()
        self.assertEqual(
            sorted(graph.files),
            ["App.java", "main.go", "src/a.rs", "src/lib.rs", "tool.py"],
        )
        self.assertEqual([found.internal for found in graph.files.values()], [()] * 5)
        # Rust roots come from Cargo.toml, not from source text.
        self.assertEqual(entries(graph), {"src/lib.rs": "library root"})

    def test_languages_without_rules_keep_imports_as_external(self) -> None:
        graph = self.build(
            {"lib/build.rb": "require 'json'\n"},
            imports={"lib/build.rb": ["json", "./helper", "../up"]},
        )
        self.assertLinks(graph, "lib/build.rb", [], ["json"])

    def test_test_and_vendored_files_are_never_entry_points(self) -> None:
        graph = self.build(
            {
                "vendor/tool/__main__.py": "print(1)\n",
                "tests/__main__.py": "print(1)\n",
                "app/__main__.py": "print(1)\n",
            }
        )
        self.assertEqual(entries(graph), {"app/__main__.py": "runs with python -m"})


class RobustnessTest(GraphCase):
    def test_malformed_configs_are_ignored(self) -> None:
        files = {
            "package.json": '{"name": "web", "bin": ',
            "tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["src/*"]}',
            "Cargo.toml": "[package\nname = ",
            "pyproject.toml": "[project.scripts\nshop = ",
            "web/main.ts": "export {}\n",
            "web/src/x.ts": "export {}\n",
            "svc/main.go": "package main\n\nfunc main() {}\n",
            "src/lib.rs": "pub fn f() {}\n",
            "tool.py": 'if __name__ == "__main__":\n    pass\n',
        }
        scanned, facts, sources = self.prepare(
            files,
            imports={
                "web/main.ts": ["@/x", "react"],
                "svc/main.go": ["github.com/acme/svc/store"],
                "src/lib.rs": ["crate::x", "serde::Serialize"],
            },
        )
        (self.root / "svc/go.mod").write_bytes(b"\xff\xfe\x00module\n")
        (self.root / "go.mod").write_text("module\n", encoding="utf-8")
        (self.root / "web/package.json").mkdir()     # a directory, not a file
        graph = GraphBuilder(self.root, scanned, facts, sources).build()
        self.assertLinks(graph, "web/main.ts", [], ["react"])
        self.assertLinks(graph, "svc/main.go", [], ["github.com/acme/svc"])
        self.assertLinks(graph, "src/lib.rs", [], ["serde"])
        self.assertEqual(
            entries(graph),
            {
                "svc/main.go": "program entry (`func main`)",
                "tool.py": "runs as a script",
            },
        )

    def test_odd_names_symlink_loops_and_unreadable_entries_do_not_crash(self) -> None:
        files = {
            "weird name.py": "x = 1\n",
            "ünïcode.py": "y = 1\n",
            "a-b.py": "z = 1\n",
            ".hidden/mod.py": "w = 1\n",
            "main.py": "import ünïcode\n",
            "web/app.ts": "export {}\n",
        }
        scanned, facts, sources = self.prepare(
            files, imports={"web/app.ts": ["./x", "left-pad"]}
        )
        with contextlib.suppress(OSError, NotImplementedError):  # no symlinks here
            os.symlink("tsconfig.json", self.root / "web" / "tsconfig.json")
        ghost = SourceFile(self.root / "ghost.py", "ghost.py", "python", Role.SOURCE, 1)
        graph = GraphBuilder(self.root, [*scanned, ghost], facts, sources).build()
        self.assertLinks(graph, "main.py", ["ünïcode.py"])
        self.assertLinks(graph, "web/app.ts", [], ["left-pad"])
        self.assertIn("ghost.py", graph.files)
        self.assertIn("weird name.py", graph.files)


class HelperTest(unittest.TestCase):
    def test_module_edges_count_file_links_between_modules(self) -> None:
        nodes = [
            node("a/x.py", "b/y.py", "a/z.py"),
            node("a/z.py", "b/y.py"),
            node("b/y.py", "gone.py"),
        ]
        graph = ProjectGraph(files={found.relpath: found for found in nodes})
        self.assertEqual(graph.module_edges(), {("a", "b"): 2})
        self.assertIsNone(graph.file("gone.py"))
        self.assertEqual(graph.file("a/z.py"), nodes[1])

    def test_components_collapse_to_the_deepest_level_that_fits(self) -> None:
        paths = ["src/pkg", "src/pkg/api", "src/pkg/db", "src/pkg/db/models"]
        graph = ProjectGraph(modules={path: ModuleNode(path) for path in paths})
        self.assertEqual(
            graph.components(max_nodes=3),
            {
                "src/pkg": "src/pkg",
                "src/pkg/api": "src/pkg/api",
                "src/pkg/db": "src/pkg/db",
                "src/pkg/db/models": "src/pkg/db",
            },
        )
        self.assertEqual(graph.components(max_nodes=1), dict.fromkeys(paths, "src/pkg"))
        self.assertEqual(ProjectGraph().components(), {})
        root_only = ProjectGraph(modules={"": ModuleNode("")})
        self.assertEqual(root_only.components(), {"": ""})
        apart = ProjectGraph(modules={p: ModuleNode(p) for p in ("api", "web/app")})
        self.assertEqual(apart.components(), {"api": "api", "web/app": "web/app"})

    def test_is_stdlib_per_language(self) -> None:
        cases = [
            ("python", "os", True),
            ("python", "os.path", True),
            ("python", "requests", False),
            ("javascript", "fs", True),
            ("javascript", "fs/promises", True),
            ("typescript", "node:test", True),
            ("typescript", "react", False),
            ("go", "net/http", True),
            ("go", "github.com/lib/pq", False),
            ("rust", "std", True),
            ("rust", "core", True),
            ("rust", "serde", False),
            ("java", "java.util", True),
            ("java", "javax.annotation", True),
            ("java", "org.junit", False),
            ("ruby", "json", False),
        ]
        for language, name, expected in cases:
            self.assertIs(is_stdlib(language, name), expected, (language, name))


if __name__ == "__main__":
    unittest.main()
