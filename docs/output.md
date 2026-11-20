# Output

`devdocs generate` writes its documentation into one directory, `docs` under the project unless you choose another. This page describes what goes in that directory, what each format looks like, and the rules devdocs follows when it writes:

- It never replaces a file it has no record of writing, unless you pass `--force`.
- It removes only files it wrote itself.
- It can render a finished run again, in another format, without a key or a model.

[commands.md](commands.md) lists every flag named here, and [configuration.md](configuration.md) the matching `devdocs.toml` keys and environment variables.

## Where output goes

- The output directory is `--out`, `docs` by default. A relative `--out` is read from the project, not from the current directory: `devdocs generate app` writes `app/docs/`. An absolute `--out` is used as given, and a leading `~` is expanded.
- devdocs refuses an `--out` that is empty, that is the project root, or that contains the project root. It refuses before it creates anything. An `--out` inside a source directory is allowed.
- `generate` and `export` write only inside the output directory. `generate` also keeps its state, such as the response cache and the run ledger, in `<project>/.devdocs/devdocs.db`. An external PDF engine prints in a temporary directory, which is removed afterwards.
- devdocs never documents its own output. The scan skips every file that a devdocs manifest lists, wherever that manifest is in the project.

## Formats

devdocs writes `markdown` (the default), `html` and `pdf`. Choose with `--format`, repeated or comma-separated:

```sh
devdocs generate --format markdown,html,pdf
```

Every format is rendered from the same documents, so the formats say the same things in the same order. They never share a file name. The manifest, `index.json`, is written whichever formats you choose.

`--pdf-engine`, `--page-size` and `--split` apply only to PDF. On the command line they need `pdf` among the formats; [commands.md](commands.md#output-options) has the rule and its error.

## The output tree

The trees below come from real runs on a small project named `shop`: six Python files in `shop/`, one test in `tests/` and a `pyproject.toml`. A run makes eight documents: one per source file, one for the `shop/` module and the architecture overview. Test files get no page.

`devdocs generate` (Markdown):

```text
README.md
architecture.md
index.json
shop/__init__.py.md
shop/cart.py.md
shop/cli.py.md
shop/index.md
shop/money.py.md
shop/orders.py.md
shop/tax.py.md
```

`devdocs generate --format html`:

```text
.nojekyll
architecture.html
assets/filter.js
assets/pages.js
assets/style.css
index.html
index.json
shop/__init__.py.html
shop/cart.py.html
shop/cli.py.html
shop/index.html
shop/money.py.html
shop/orders.py.html
shop/tax.py.html
```

`devdocs generate --format pdf`:

```text
index.json
shop.pdf
```

`devdocs generate --format pdf --split`:

```text
index.json
pdf/architecture.pdf
pdf/index.pdf
pdf/shop/__init__.py.pdf
pdf/shop/cart.py.pdf
pdf/shop/cli.py.pdf
pdf/shop/index.pdf
pdf/shop/money.py.pdf
pdf/shop/orders.py.pdf
pdf/shop/tax.py.pdf
```

With `--format markdown,html,pdf` the directory holds the Markdown, HTML and PDF book files together, with one `index.json`.

## What each file is for

| File | Format | Holds |
|---|---|---|
| `README.md` | markdown | The home page: the project name and description, a link to the architecture page, and tables of modules and files with a one-line summary each. |
| `architecture.md` | markdown | The architecture overview of the whole project. |
| `<dir>/index.md` | markdown | One page per module, that is, per directory that holds documented files. |
| `<path>.md` | markdown | One page per source file, named after the file with its extension kept: `shop/cart.py.md`. |
| `index.md` | markdown | The root module's page, when the project root itself holds documented files. |
| `index.json` | all | The manifest: every document, the record of the run, and the list of files devdocs wrote. |
| `index.html` | html | The site's home page. |
| `architecture.html`, `<dir>/index.html`, `<path>.html` | html | The site's pages, one per document. |
| `root.html` | html | The root module's page, since `index.html` is the home page. |
| `assets/style.css` | html | The stylesheet. |
| `assets/filter.js`, `assets/pages.js` | html | The page filter, and its list of every page's title, path and summary. |
| `.nojekyll` | html | An empty file. It stops GitHub Pages from hiding pages whose names start with `_`, such as `__init__.py.html`. |
| `<project>.pdf` | pdf | The bound book, named after the project: accents and other non-ASCII characters dropped, the rest in lower case with each run of characters other than letters and digits turned into `-`, and no `-` at either end, as in `shop.pdf`, or `cafe-shop.pdf` for `Café Shop`. When nothing is left, it is `documentation.pdf`. |
| `pdf/index.pdf` | pdf, split | The overview that `README.md` holds. |
| `pdf/<path>.pdf` | pdf, split | One file per document: `pdf/architecture.pdf`, `pdf/shop/index.pdf`, `pdf/shop/cart.py.pdf`. |
| `pdf/root.pdf` | pdf, split | The root module's file, since `pdf/index.pdf` is the overview. |

Two documents never share a page. When two would get the same name, compared without regard to case or Unicode normalization, the later one in reading order gets `-2`, the next `-3`, and so on: `Cart.py.md` and `cart.py-2.md`. The home page's name is reserved: `README.md`, `index.html`, and `pdf/index.pdf` when split. The manifest records each document's path without such a suffix.

The reading order is the same everywhere (home tables, book, contents, bookmarks and manifest): the architecture first, then the modules by directory, then the files by path.

### Markdown pages

A document's page opens with YAML front matter, then the title, the lead paragraph, a facts line and the sections. This is the page for `shop/money.py` from a run that bought no prose, so nothing on it was written by a model:

````markdown
---
id: "file:shop/money.py"
kind: "file"
title: "shop/money.py"
source: "shop/money.py"
source_hash: "76b4810075a0fb878970216284b0f1a8b8d9fbfe7150afff4ed4ab0cf0db7540"
language: "python"
mode: "stub"
schema: "1.0.0"
---

# shop/money.py

Money as integer cents, so totals never drift.

**Module:** [shop](index.md) · Python · 4 lines · **Used by:** [shop/cart.py](cart.py.md), [shop/orders.py](orders.py.md), [shop/tax.py](tax.py.md)

## Public surface

<a id="sym-Money"></a>
### `Money`

```python
class Money(int)
```

An amount in cents.

Bases: `int`
````

- **Front matter** holds, in this order: `id`, `kind`, `title`, `source`, `source_hash`, `language`, `mode` (`prose` or `stub`), `model`, `tier`, `input_tokens`, `output_tokens`, `confidence` (only when the parse was not exact) and `schema`. Empty values and zero counts are left out.
- **The facts line** of a file's page gives the module, the language, the line count, the entry point, the files that use this one (`Used by`) and the tests that cover it (`Tested by`). A module's page gives its parent module, its file count, its submodules and the modules it depends on and is used by. Link lists show 8 entries, then `N more`. `Symbol table may be incomplete` means the file's parser failed, so the symbols listed may not be all of them.
- **Pages carry no date, cost or run statistics.** The token counts in the front matter are those of the answer that wrote the document, so a re-run that finds the answer cached does not change them. An unchanged source renders to the same bytes, and a re-run changes only what changed.
- **Links** are relative paths between pages, so the pages read the same on a code host, in a static site or from disk.
- **The home page**, `README.md`, has no front matter. It ends with `Generated by devdocs; edit the source, not these pages.`

Each kind of document has its sections in a fixed order. The first is the lead, shown without a heading, and a section with nothing to say is left out:

| Document | Sections |
|---|---|
| File | Purpose, Public surface, Behaviour, Dependencies, Known issues |
| Module | Purpose, Components, Relationships, Entry points, Notes |
| Architecture | Overview, Component map, Data flow, Entry points, Extension points, Operational notes |

- **Public surface** lists the public symbols with their signatures copied from the source. A symbol's summary is the first paragraph of its docstring. On a page with prose, the model is asked for a sentence for each public symbol that has no docstring, up to 25 per file.
- **Dependencies** lists the project files this one imports, as links (`Internal`), then third-party packages (`External`) and standard-library modules (`Standard library`).
- **Known issues** lists the `TODO`, `FIXME`, `XXX`, `HACK` and `BUG` comments with their line numbers, at most 25 per file.

## The manifest

`index.json` sits at the root of the output directory. Every `generate` writes it, whatever the formats, and `export` adds to its list of files. It serves three purposes:

- It is devdocs' record of the files it wrote, which the [never-replace rule](#never-replacing-your-files) and [pruning](#re-runs-and-pruning) depend on.
- It is what [export](#export) renders from.
- It is a stable interface for your own tools. Its `schema_version` follows semantic versioning: an additive change raises the minor version, a change that would break a reader raises the major one. devdocs 4.0.0 writes `1.0.0`.

The file is JSON in UTF-8, indented by two spaces. It is not a search index; the HTML site's search data is `assets/pages.js`.

| Key | Holds |
|---|---|
| `schema_version` | `"1.0.0"`. |
| `project` | The project name: `--name` or the `name` setting, else the `name` in `pyproject.toml` (`[project]` or `[tool.poetry]`), `package.json` or `Cargo.toml`, else the directory's name. |
| `description` | `--description` or the `description` setting; `""` otherwise. |
| `documents` | Every document, in reading order. |
| `run` | The run that wrote the documents. |
| `files` | The files devdocs has a record of writing there, which it may replace or prune: those of the last `generate`, and any an `export` added since. Sorted paths, relative to the output directory. `index.json` itself is not listed. |

Each entry of `documents` has these keys:

| Key | Holds |
|---|---|
| `id` | `file:<path>`, `module:<dir>` (`module:` for the root module) or `architecture`. |
| `kind` | `file`, `module` or `architecture`. |
| `title` | The file's path; the module's directory with a trailing `/`, or the project name for the root module; `Architecture`. |
| `path` | The output path without an extension: `shop/cart.py`, `shop/index`, `architecture`. |
| `summary` | One sentence, as the index tables show it. |
| `sections` | The sections, each with `key`, `title`, `text` (paragraphs), `items`, `symbols`, `diagram` (Mermaid source, or `""`) and `ordered` (true for numbered steps). The facts line is the section with key `facts`. |
| `meta` | Where the document came from, below. |

- An item has `text`, `links` and `line` (0 for none). A link has `text`, `target` (a document id, or `""` for plain text) and `anchor`.
- A symbol has `id` (`<path>#<qualname>`), `name`, `qualname`, `kind` (`module`, `class`, `function`, `method`, `constant`, `type` or `interface`), `signature` (verbatim source), `summary`, `summary_source` (`docstring`, `model` or `""`), `line`, `end_line`, `is_async`, `decorators`, `bases` and `members`.
- `meta` has `mode` (`prose` or `stub`), `source`, `source_hash` (in hex: the SHA-256 of a file's source; for a module or the architecture, a SHA-256 over the hashes of the files it covers), `language`, `model` and `tier` (empty for a stub), `input_tokens` and `output_tokens` (those of the answer that wrote this document), `confidence` (`exact`, `heuristic`, `degraded` or `failed`) and `notes`.

The `run` object has these keys:

| Key | Holds |
|---|---|
| `generator` | `devdocs` and its version. |
| `generated_at` | When the run wrote its documents: UTC, ISO 8601, to the second. |
| `git_commit` | The full hash of the project's `HEAD`, or `""` outside a git repository or without git. |
| `provider` | The provider the run used. |
| `requests` | Requests the run sent, retries and embedding batches included. |
| `cache_hits` | Answers the run took from the response cache. |
| `input_tokens`, `output_tokens` | The tokens of every document with prose, cached ones included. |
| `cost_usd` | What this run spent, in US dollars. |

Here is the document for `shop/money.py` from the same run that made the Markdown page above, its `facts` section left out:

```json
{
  "id": "file:shop/money.py",
  "kind": "file",
  "title": "shop/money.py",
  "path": "shop/money.py",
  "summary": "Money as integer cents, so totals never drift.",
  "sections": [
    {
      "key": "purpose",
      "title": "Purpose",
      "text": [
        "Money as integer cents, so totals never drift."
      ],
      "items": [],
      "symbols": [],
      "diagram": "",
      "ordered": false
    },
    {
      "key": "public-surface",
      "title": "Public surface",
      "text": [],
      "items": [],
      "symbols": [
        {
          "id": "shop/money.py#Money",
          "name": "Money",
          "qualname": "Money",
          "kind": "class",
          "signature": "class Money(int)",
          "summary": "An amount in cents.",
          "summary_source": "docstring",
          "line": 3,
          "end_line": 4,
          "is_async": false,
          "decorators": [],
          "bases": [
            "int"
          ],
          "members": []
        }
      ],
      "diagram": "",
      "ordered": false
    }
  ],
  "meta": {
    "mode": "stub",
    "source": "shop/money.py",
    "source_hash": "76b4810075a0fb878970216284b0f1a8b8d9fbfe7150afff4ed4ab0cf0db7540",
    "language": "python",
    "model": "",
    "tier": "",
    "input_tokens": 0,
    "output_tokens": 0,
    "confidence": "exact",
    "notes": []
  }
}
```

The manifest does not record the formats, PDF options, layout or other settings of the run, nor whether the run was stopped. The run ledger in `.devdocs/` keeps how each run ended.

devdocs takes an `index.json` for its own when it is a JSON object with `schema_version` and lists under `documents` and `files`. Any other `index.json` in the output directory, a damaged one included, counts as a file devdocs did not write.

## Layouts

The default layout mirrors the source tree and keeps each file's extension: `shop/cart.py` becomes `shop/cart.py.md`. So `cart.py` and `cart.ts` in one directory never collide.

The per-file layout is the one devdocs 3 used. It drops the extension: `shop/cart.py` becomes `shop/cart.md`, `shop/cart.html` and, split, `pdf/shop/cart.pdf`. Turn it on with `--per-file` on `generate` or `estimate`, with `per_file = true` in `devdocs.toml`, or with `DEVDOCS_PER_FILE=true`. A v3-style command line uses it too; see [migration.md](migration.md).

In the per-file layout a file keeps its extension when the shorter name would collide, compared without regard to case or Unicode normalization:

- with another file's name, as `cart.py` and `cart.ts`;
- with a module page, as `shop/index.py` beside `shop/index.md`;
- with a top-level name devdocs uses: `README`, `architecture`, `index` or `root`.

`devdocs generate --per-file`, on the `shop` project plus `shop/index.py`, `shop/cart.ts` and a root-level `README.py`:

```text
README.md
README.py.md
architecture.md
index.json
index.md
shop/__init__.md
shop/cart.py.md
shop/cart.ts.md
shop/cli.md
shop/index.md
shop/index.py.md
shop/money.md
shop/orders.md
shop/tax.md
```

`README.py` at the root makes a root module, whose page is `index.md` (`root.html` in HTML, `pdf/root.pdf` when split). Module pages, `architecture.md`, `README.md` and `index.json` are named the same in both layouts. `export` keeps the layout of the run it renders.

## HTML

### The site

`--format html` writes a static site: `index.html`, one page per document, `assets/` and `.nojekyll`. Every link is relative, and the site loads nothing from elsewhere: no fonts, images or scripts from another host.

- **A page** has a masthead with the project name and a `Menu` link, breadcrumbs, the title, a provenance line, the lead, the facts, an "On this page" list and the sections. The provenance line reads `Written from source by devdocs; prose by <model>.`, or `Written from source by devdocs; no model prose.` for a stub.
- **The sidebar** lists Home, Architecture and every module. Only the current page's module is opened to list its files.
- **The filter** (`assets/filter.js`) adds a `Filter pages` box to the masthead. It lists the pages whose title and summary hold every word you type, up to 30. Enter opens the first, and Escape clears the box. Without JavaScript the box is absent and the rest of the site works.
- **The layout** is one column on a phone. The sidebar becomes a column from 52rem wide, and "On this page" from 80rem. The site follows the system's dark mode, and its print styles leave out the navigation.
- **Safety:** a Content-Security-Policy allows only the site's own stylesheet and scripts. No page has inline script, a style attribute or an event handler, and every string from the source or a model is escaped.
- **Code blocks** are not syntax-highlighted.
- **Provenance** is in `<meta name="devdocs:…">` tags on each page: `devdocs:id`, `devdocs:mode`, `devdocs:model`, `devdocs:source_hash` and the like. The generator and git commit are named on the home page only, so a run at a new commit rewrites the home page and the pages whose documents changed, not the whole site.

### Viewing offline

Open `docs/index.html` in a browser. The pages and their links work from disk, with no server. The filter's index, `assets/pages.js`, is a script rather than JSON because browsers refuse to fetch files for a page opened from disk. A browser that does not run the scripts shows the site without the filter box.

### Publishing the site

Copy the output directory as it is to any static host. The links are relative, so the site works at any path. On GitHub Pages, publish the directory with its `.nojekyll` file. [ci.md](ci.md#publishing-the-html-site) has a GitHub Actions workflow that builds and publishes it.

### The HTML book

For the `chrome` and `weasyprint` engines, devdocs renders the overview and every document into one self-contained HTML page, the book, and has the engine print it to PDF. It writes that page to a temporary directory, never to the output directory, so no option keeps it; `--format html` always writes the site.

## PDF

### The built-in engine

The default engine, `builtin`, needs nothing installed. It writes one bound book, `<project>.pdf`:

1. A cover with the project name, the description, the generator and the first 12 characters of the git commit. It has no date.
2. Contents, with page numbers and links.
3. The overview, as `README.md` gives it.
4. The architecture, then each module, then each file, each from a new page.

Every page but the cover has a running head (the project and the chapter) and a folio, `Page N of M`. Bookmarks list every document with its sections beneath it, and the reader opens with the bookmarks shown.

- The book is deterministic: the same documents give the same bytes. A re-run of an unchanged project rewrites no PDF, except the book after a new git commit, which its cover names. Split files do not name the commit.
- It uses the standard PDF fonts, Helvetica for text and Courier for code, which every PDF reader has, so no font is embedded.
- It has no images, no syntax highlighting and no tagged-PDF structure. Pages are portrait.

### Characters outside the built-in fonts

The standard fonts cover WinAnsi, which is Windows code page 1252: ASCII, Latin-1 letters such as `é`, `ü`, `ñ` and `ß`, and marks such as `€`, `–`, `—`, `…`, `“`, `”`, `•` and `©`. A character outside it, such as `→`, `≤`, `−` (U+2212), `Ω`, `中`, `✓` or an emoji, prints as `?`. devdocs never drops one silently. It reports each character once per chapter, the first 10 on stderr:

```text
pdf: characters outside the built-in fonts were printed as '?' (2):
  U+2192 RIGHTWARDS ARROW in the overview
  U+2713 CHECK MARK in the overview
  for all of Unicode, use --pdf-engine chrome or --pdf-engine weasyprint
```

The result line then adds `2 rendering warnings; --json lists them all`, and `--json` lists every one under `warnings`. `-q` hides the block on stderr but not the result line. Bookmarks and the document's title can hold any character.

### External engines

| Engine | Runs | Found |
|---|---|---|
| `chrome` | Chrome, Chromium or Microsoft Edge, headless, with `--print-to-pdf` | On `PATH` as `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`, `microsoft-edge`, `microsoft-edge-stable`, `msedge` or `chrome`; then in the usual install locations on macOS, Windows and Linux |
| `weasyprint` | WeasyPrint's command line | On `PATH` only |

Both print the HTML book, with all of Unicode. Neither is a dependency of devdocs, and no setting names a path to one. `devdocs doctor` lists the engines it finds. The container image has neither, so only `builtin` works there.

- **One book only.** An external engine cannot split; `--split` with one exits 2: `the chrome PDF engine prints one book and cannot split it`.
- **Not reproducible.** Both tools stamp their own dates and identifiers, so every run rewrites the book.
- **A missing engine** stops the run before any model call, with exit 2: `the weasyprint PDF engine is not installed`.
- **A failing engine** has 120 seconds to print. If it fails, `generate` still writes the other formats and the manifest, reports `failed format pdf: ...` and exits 4. Its hint names the `devdocs export` command that makes the PDF from the manifest, without a key, with the project path, `--out`, `--pdf-engine`, `--page-size` and `--split` the command line gave. From `export`, the same failure exits 1.

### Page sizes

`--page-size` takes `A4`, the default, or `Letter`, in any case: `--page-size letter` works. Pages are always portrait. An external engine gets the size as a CSS `@page` rule.

### One file per document

`--split` writes one PDF per document under `pdf/`, plus `pdf/index.pdf` for the overview; the root module's file is `pdf/root.pdf`. Split files have no cover or contents page. Each has its own bookmarks, running head and folio. A link to another document opens the sibling file at the right place; a reader that does not follow links between files shows such a link as plain text. Only the built-in engine splits.

## Diagrams

devdocs draws two kinds of diagram, both from the import graph:

- **Relationships**, on a module page: the module's files that import one another, and those imports. The diagram is left out when the files import nothing from each other, or when the module has more than 20 files.
- **Component map**, on the architecture page: the components and the imports between them. A component is a module, or the directory several modules are grouped under, at the deepest level of the tree that gives 12 components or fewer. Grouping stops at the first level below the directories all modules share, such as `src/`, so more than 12 directories at that level all show. The diagram is left out when there is only one component.

How each format shows them:

- **Markdown:** a fenced `mermaid` block, which GitHub, GitLab and most documentation sites draw.
- **HTML:** an inline SVG that devdocs lays out itself, so it needs no Mermaid script and works offline. Its colours follow the light or dark theme, and it carries a text description of its boxes and arrows.
- **PDF:** vector drawing, scaled to fit the page and never split across pages.

The labels are file and directory names. A long label is shortened in the middle.

## Never replacing your files

devdocs replaces or deletes only files that its manifest lists. If a file the manifest does not list sits where an output goes, and holds different bytes, the whole write is refused before anything is written:

```text
devdocs: error: refusing to replace 1 file in /path/to/shop/docs that devdocs has no record of writing: shop/money.py.md
  hint: move it away or choose another --out; --force replaces it
```

The exit code is 2. The message names the first five files, then `and N more`.

`generate` makes this check before its first model call. It composes the documents with the prose the cache already holds, renders them in every format, and checks each file the write would make, without writing anything. So a refused run buys nothing, though it may leave an empty `.devdocs/devdocs.db`. The same check refuses, before paying, a directory in the way of an output, an `--out` that is an existing file, an output directory devdocs cannot write, and a file name the file system cannot hold.

You can:

- move the file away;
- choose another `--out`;
- pass `--force`, which replaces those files and lists them in the manifest, so later runs replace and prune them as usual.

`--force` does not remove a directory in the way, get past a file-system error, or allow an `--out` on the project root.

Some cases to know:

- **Your own files at other paths** in the output directory are never touched or pruned. A hand-written `docs/NOTES.md` stays.
- **A page devdocs wrote and you edited** is still devdocs' file. The next run replaces it without asking, so keep notes at a path devdocs does not write.
- **A file that already holds exactly the output** is left alone and not listed, so it is never pruned later. A hand-written empty `.nojekyll` is such a file.
- **With no manifest at all,** on a first run or after `index.json` was deleted, such an identical file is taken as devdocs' own and listed.
- **A foreign or damaged `index.json`** counts as a file devdocs did not write, and the refusal names it first.
- **A directory in the way** cannot be replaced, `--force` or not:

  ```text
  devdocs: error: cannot write 1 file in /path/to/shop/docs: a directory is in the way of README.md
    hint: move it away or choose another --out
  ```

- **A file that appears during the paid part** is refused when the run writes, after paying. The hint then says what running again costs, for example `the answers are cached, so running again costs nothing`.

[troubleshooting.md](troubleshooting.md#refusals-to-write) covers the other write failures: permissions, read-only disks, full disks and names a file system cannot hold.

## Re-runs and pruning

- **Atomic writes.** Each file is written to a temporary file beside it, `.<name>.<pid>.<n>.tmp`, then renamed into place, so a reader never sees half a page. A process killed outright leaves at most one temporary file. The manifest is written first, listing every file the write may make, and again at the end, so a write that dies midway still records every file it made.
- **Unchanged files are not rewritten,** so their timestamps stay and diffs stay quiet. The output is deterministic, so a re-run of an unchanged project rewrites only `index.json`, whose `run` changes. With HTML, a run at a new git commit also rewrites `index.html`; with the built-in engine's PDF book, `<project>.pdf`. An external PDF engine's book is rewritten on every run.
- **Pruning.** `generate` deletes the files the previous manifest lists that this run does not produce, then the directories that leaves empty. It deletes only regular files inside the output directory.
- **Switching formats or layouts** works the same way: the next `generate` writes the new set and removes what only the old one had.
- **A failed format costs only itself.** If one format fails to render, as an external PDF engine can, `generate` writes the other formats and the manifest and prunes nothing that time, so the failed format's earlier files stay and stay listed.

Deleting `shop/tax.py` and running `devdocs generate --json` again removed its page, and rewrote the pages that changed with it: `README.md` and `shop/index.md`, which listed it; `shop/money.py.md`, whose `Used by` line named it; and `architecture.md`, whose `source_hash` covers every file. The `written` object of the result:

```json
{
  "written": [
    "README.md",
    "architecture.md",
    "index.json",
    "shop/index.md",
    "shop/money.py.md"
  ],
  "unchanged": [
    "shop/__init__.py.md",
    "shop/cart.py.md",
    "shop/cli.py.md",
    "shop/orders.py.md"
  ],
  "removed": [
    "shop/tax.py.md"
  ]
}
```

## Export

`devdocs export` renders the last run's documents again, from `index.json`. It needs no key, sends nothing, scans nothing and opens no database. Use it to add a format after a run, to make a PDF that an external engine failed to print, or to try another page size:

```sh
devdocs export --format html
```

```text
8 documents (run of 2026-11-10T07:20:25+00:00) as html in docs: 14 files written, 0 unchanged
```

When it writes 10 files or fewer, it lists them:

```text
8 documents (run of 2026-11-10T07:20:25+00:00) as pdf in docs: 2 files written, 0 unchanged
  docs/index.json
  docs/shop.pdf
```

- **Formats:** `--format` if given, else the configured formats, else `markdown`. A plain `devdocs export` re-renders the configured formats.
- **It adds files and never prunes.** Every file the manifest listed stays, and stays listed. The manifest keeps its record of the run exactly as it was; only its list of files grows. So a site from `generate --format html` survives `export --format pdf`.
- **The next `generate` prunes exported files** it does not write itself, since they describe an older run. To keep a format, include it in `generate`'s formats.
- **The layout, project name and description** come from the manifest. `export` has no `--per-file`, `--name` or `--description`.
- **The never-replace rule and `--force`** apply as for `generate`. `export` has no check of its own ahead of the write; the write refuses before it writes anything.
- **An unchanged export rewrites nothing**, except the output of an external PDF engine, which differs every time.
- **A failing format:** the other formats are written first, then the command fails with that format's error.

`export` needs a manifest it can read. Otherwise it exits 2 with one of these, where `<out>` is the output directory's full path:

| Message | Cause |
|---|---|
| `no generated documentation in <out>: index.json is missing` | No `generate` has written to this output directory. Run it first, or point `--out` at the directory it wrote. |
| `cannot read <out>/index.json: <reason>` | The file is not valid JSON, cannot be read, or holds documents this devdocs cannot read. |
| `<out>/index.json is not a devdocs manifest` | Another tool's `index.json`. |
| `<out>/index.json was written by a newer devdocs, in document schema 2.0.0; this one reads schema 1.x` | A devdocs with a newer major schema wrote it. Upgrade devdocs, or run `generate` again. |

The schema version in the last message is whatever the manifest holds. A manifest edited by hand can also give `<out>/index.json: document '<id>' appears twice`, `<out>/index.json: document '<id>' has the unsafe path '<path>'` or `no devdocs manifest in <out> to add files to`. For any of them, run `devdocs generate` to write the manifest again.

## Prose style rules

devdocs enforces a style on the text a model writes. The prompts ask for it, and a guard applies the rules below to every reply before devdocs uses or caches it. The rules apply only to model replies: signatures, docstrings, file names and every other parsed fact appear as the source has them.

A model answers in tagged lines, not in Markdown: `PURPOSE`, `SYMBOL`, `NOTE` for a file; `PURPOSE`, `RELATION`, `NOTE` for a module; `OVERVIEW`, `FLOW`, `EXTEND`, `NOTE` for the architecture. A reply left without a purpose, or an overview, once these rules apply is not used or cached; devdocs then asks the next, stronger model, if there is one. When no model gives a usable reply, the page is built from facts alone and the run reports `failed <kind> <item>: no usable reply`, which makes `generate` exit 4 ([commands.md](commands.md#exit-codes)).

**Length.**

| Text | Limit |
|---|---|
| File purpose | 3 sentences, 600 characters |
| Module purpose | 4 sentences, 800 characters |
| Architecture overview | 6 sentences, 1,200 characters |
| Notes | 5 per file; 4 per module or architecture |
| Module relations | 6 |
| Data-flow steps | 8 |
| Extension points | 4 |
| Each note, relation, step, extension point and symbol sentence | 1 sentence, 240 characters |

Extra sentences are dropped. A first sentence over the limit is cut at a word, a dangling word such as `the`, `of` or `and` is removed, and a full stop is added. Duplicate items are dropped.

**Cleaning.**

- Text is normalized to Unicode NFC. Emoji are removed, and so are the other characters of Unicode's "other symbol" category, such as `©`, `™` and `✓`, except `°`. Math and currency signs such as `→` and `€` stay. Private-use characters and invisible format and control characters are removed too.
- Markdown and HTML are removed: headings, list and quote markers, images, bold and italic markers and stray backticks. A link keeps its text. Names such as `__init__` stay as they are.
- These openers are removed: `In summary,`, `In conclusion,`, `Overall,`, `Essentially,`, `Basically,`, `Simply put,`, `Note that`, `Please note that`, `It is worth noting that`, `It is important to note that`.
- `This file provides …` becomes `Provides …`, and likewise for module, class, function and similar nouns, and for verbs such as `defines`, `implements` or `handles`. `This module is responsible for …` becomes `Responsible for …`, and `This class is a …` becomes `A …`.
- `utilize`, `utilise` and `leverage` become `use`, in any form; `in order to` becomes `to`.
- These adjectives are removed, with any intensifier before them such as `very` or `highly`, where they qualify a noun: `robust`, `seamless`, `powerful`, `cutting-edge`, `state-of-the-art`, `comprehensive`, `elegant`, `effortless`, `world-class`, `best-in-class`, `next-generation`, `intuitive`, `sleek`, `blazing-fast`. In a predicate, as in `the parser is robust`, they stay. The adverbs `seamlessly`, `elegantly` and `effortlessly` are removed.

**Sentences removed.** These rules apply to a purpose, an overview, and each note, relation, step and extension point. A symbol's sentence is not checked for them.

- Hedged sentences: those with `probably`, `likely`, `presumably`, `possibly`, `perhaps`, `it seems`, `seems to`, `appears to`, `might be`, `may be`, `I think`, `I believe` or `unclear`.
- Sentences about the prompt: those with `as an AI`, `provided code`, `provided source`, `code snippet`, `the given file`, `based on the source` or `from the code above`.

**Grounding.** A name in a code span must be something the project defines or imports, a file name, a Python builtin, a common JavaScript global, or a keyword such as `self` or `null`. Literals pass: numbers, quoted strings, paths, flags, upper-case constants and file names. An ungrounded span in a purpose or symbol sentence loses its backticks and the sentence stays. In a note, relation, step or extension point, it removes the whole item. A model's sentence about a symbol is kept only for a symbol devdocs asked about.

devdocs does not report what these rules removed.
