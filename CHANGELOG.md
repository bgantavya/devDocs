# Changelog

All notable changes to devdocs are recorded in this file, newest first. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The exit codes are part of devdocs' public contract; in the Python package, only `devdocs.__version__` is.

This file sums up what changed for users. From 4.0.0, the full notes of each release, generated from its commits, are on [GitHub Releases](https://github.com/bgantavya/devDocs/releases). The PyPI distribution is `devDocs` in every version; the command is `devDocs` up to 3.0.0 and `devdocs` from 4.0.0. The entries for 3.0.0 and earlier are reconstructed from the git history.

## [Unreleased]

devdocs 4.0.0 is a rewrite of the 3.x line from the ground up. It needs Python 3.11 or later and installs no dependencies. Parsers supply the exact facts, such as signatures, structure and docstrings, and a model, Google Gemini or Anthropic Claude, writes prose only where it adds something, within a cost limit checked before every call. It writes Markdown, an HTML site and PDF, caches the answers it pays for, and resumes a run that stopped. It ships on PyPI as `devDocs`, on npm as `@bgantavya/devdocs`, and as the container image `ghcr.io/bgantavya/devdocs`.

Upgrading from 3.x is a breaking change: the command, its command line, what gets documented and the pages written all differ. Breaking changes are marked **Breaking** below, and [docs/migration.md](docs/migration.md) explains each one and what to do.

### Added

- Commands: `devdocs generate` writes the documentation; `estimate` prices a run without sending anything; `export` renders the last run again in other formats, with no key and no model call; `doctor` checks that a run can work; `init` writes a starter `devdocs.toml`; `models` lists the models devdocs knows for the provider, with their prices and whether your key can use each; and `cache stats` and `cache clear` show and empty the response cache. Every command but `init` takes `--json`. [docs/commands.md](docs/commands.md) documents them all.
- Providers: Google Gemini, the default, and Anthropic Claude, both reached through the standard library's HTTP client, plus `anthropic-sdk`, which uses Anthropic's official SDK from the optional `anthropic` extra.
- Configuration in `devdocs.toml`, looked for in the project and then in each directory above it, stopping at the repository root and before the home directory, and in `DEVDOCS_*` environment variables. A flag beats a variable, which beats the file, which beats the default. See [docs/configuration.md](docs/configuration.md).
- Parsers for Python, JavaScript, TypeScript, Go, Rust and Java supply each file's symbols and their members, with signatures copied verbatim from the source, and the file's imports, exports and docstrings. Files in the other languages the scanner recognises, such as C, Ruby or SQL, get a page from facts alone, without a symbol table.
- An import graph across those languages gives each page its dependencies, the files that use it and the tests that import it, and finds the project's entry points.
- Known issues, collected from TODO, FIXME, XXX, HACK and BUG comments.
- A planner that gives model prose only to the files, directories and overview where it adds most. Below `--quality high`, a file whose docstrings already document it gets a page from facts alone. `--quality cheap`, the default, `balanced` or `high` sets how much prose, from how strong a model.
- Cost limits: `--max-cost`, 0.50 USD per run by default, and `--max-requests`, with no limit by default. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend. The plan is cut to fit before anything is sent, so work that does not fit gets a page from facts alone; each call reserves its worst-case cost before it goes out. [docs/costs.md](docs/costs.md) explains what a run costs.
- Free-tier pacing by default: Gemini requests are paced to each model's free-tier rate limits, and a Gemini model without a free tier is called only if `--model` pins it. `--paid` paces to paid-tier limits and lets every model be called. Claude has no free tier and is always paced at its paid limits. A model whose daily quota is spent hands its work to another; when none is left, the run stops with exit 10, to resume once the quota resets.
- A response cache in `<project>/.devdocs/devdocs.db`. Each usable answer is stored under its model and prompt and reused for free, so a re-run with the same settings buys prose again only for prompts that an edit changed, not for the whole project. Under Gemini free-tier pacing, a tier that no free model the key can reach serves is served on loan from a lower tier, and those answers are cached under the tier they served, so they are reused too. [docs/costs.md](docs/costs.md#what-a-re-run-pays-for) lists what a re-run pays for.
- Cross-file context in each prompt: `--context standard`, the default, adds exact snippets from the import graph and, where those are few, related code found by text search, with no extra request, though the snippets add input tokens; `semantic`, with provider `gemini` only, also searches Gemini embeddings, priced with the run and held to half of each limit; `off` adds none.
- Output formats, chosen with `--format` and combinable:
  - Markdown, the default: a page per source file and per directory that holds source files, an architecture overview and a `README.md` index, every page but the index with YAML front matter. `--per-file` keeps v3's `docs/<dir>/<name>.md` layout.
  - HTML: a static site.
  - PDF: one bound book, or, with the built-in engine, one file per document with `--split`, on A4 or Letter (`--page-size`). The built-in engine needs nothing installed and prints characters outside its fonts as `?`, reporting each; `--pdf-engine chrome` or `weasyprint` prints all of Unicode.
- Diagrams of the dependencies within a directory and between the project's main components: Mermaid in Markdown, drawn as SVG in HTML and as vector graphics in PDF.
- A manifest, `index.json`, in the output directory: every document of the run, in document schema 1.0.0, and every file written. `export` renders from it.
- A format that fails to render, such as a PDF whose external engine fails, costs only itself: the other formats and the manifest are still written, the run exits 4, and the hint names the `devdocs export` command that makes the format later from the manifest, with no key.
- Safe writing. devdocs never replaces or deletes a file it has no record of writing: such a file where a page goes stops the run before the first model call, and `--force` takes it over. Writes are atomic, unchanged files are left alone, and the pages of removed sources are deleted. Files that a devdocs manifest lists are never documented as source.
- Resumable runs. A run stopped in its paid part, by its budget, a spent quota, or a first Ctrl-C or SIGTERM, still writes every page, with the prose it has and the rest from facts, and the next run reuses every answer bought.
- A graceful stop. The first Ctrl-C or SIGTERM during the paid part of `generate` sends nothing new and retries nothing, lets the requests already sent finish and keeps their answers, and writes the pages; a second signal a second or more later stops at once. The exit status is 130 after SIGINT and 143 after SIGTERM.
- Stable exit codes, so scripts and CI can tell a bad key from a budget stop from a spent quota. See [docs/commands.md](docs/commands.md#exit-codes).
- `devdocs --version`.
- The npm package `@bgantavya/devdocs`, which runs the bundled devdocs with a Python 3.11 or newer found on the machine, or the one `DEVDOCS_PYTHON` names.
- The container image `ghcr.io/bgantavya/devdocs`, which runs as a non-root user, and a compose service that runs it on the current directory.
- Installer scripts for macOS and Linux (`scripts/install.sh`) and Windows (`scripts/install.ps1`), which install with uv, fetching uv first when it is missing, else pipx, else pip, then run `devdocs doctor --offline`. [docs/install.md](docs/install.md) lists every install method.

### Changed

- **Breaking:** The command is `devdocs`, in lowercase, and the Python package is `devdocs`: the `devDocs` command and `import devDocs` no longer exist. The PyPI distribution is still `devDocs`, so upgrading it installs 4.0.0.
- **Breaking:** Python 3.11 or later is required. 3.x declared Python 3.8 and needed 3.9.
- **Breaking:** A bare `devdocs` prints its help and exits 0, where v3 documented the current directory. Run `devdocs generate`.
- **Breaking:** The API key is never asked for. Pass `--api-key`, set `DEVDOCS_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY` (`DEVDOCS_API_KEY` or `ANTHROPIC_API_KEY` for Claude), or set `api_key` in `devdocs.toml`. Without a key, `generate` exits 2, and its hint names the flag and the variables it reads.
- **Breaking:** v3 command lines are deprecated. A line that starts with no v4 command and has `-p`, `--path`, `--output`, `--overwrite`, `--authors` or `--keywords` is translated to `devdocs generate` in v3's page layout and v3's output location, after a notice on stderr. A line with only `--name`, `--description`, `--include` or `--exclude` is not read as v3, and exits 2.
- **Breaking:** `--include` and `--exclude` do what their names say; in v3 each did the other's job. `--exclude` takes `.gitignore`-style patterns. `--include` takes literal paths, names or path endings, not globs, and brings back source files that the ignore rules or the size limit leave out.
- **Breaking:** What is documented. The project's `.gitignore` rules and a built-in list of tool, dependency and build directories decide what is skipped, where v3 skipped any name that contained one of 39 fixed strings, such as `util`, `lib` or `test`. Source files in a recognised language are documented, `__init__.py` included; tests, vendored and generated code, configuration, docs and other files that are not source are not. Files over 1,000,000 bytes are skipped, and symbolic links are not followed.
- **Breaking:** The pages. Each page states exact facts and adds short prose where it helps, instead of a model-written README per file. A file's page is named after the whole file name (`shop/cart.py.md`) unless `--per-file` is given; a directory's page is `<dir>/index.md`, not `<dir>/<dir>.md`; `README.md` is an index of the pages, not a project README; `architecture.md` and `index.json` are new; and the file, directory and architecture pages carry YAML front matter.
- **Breaking:** The output directory is set with `--out` and read relative to the project, where v3's `--output` was relative to the current directory. A translated v3 line still writes where v3 did, to `<current directory>/<output>/<project path>`.
- **Breaking:** An output directory that is the project root, or above it, is refused with exit 2. On a v3 line, `-p . --output .` is such a case.
- **Breaking:** The first run over pages that v3 wrote stops before any model call, with exit 2, and names the pages it would replace. Add `--force` once to take them over. v3 pages at paths that v4 does not write are left where they are.
- **Breaking:** A run spends at most 0.50 USD unless you set another limit with `--max-cost` or `max_cost`. `--max-cost 0` means no limit, not zero spend.
- **Breaking:** The exit status reports what happened. v3 exited 0 even when its run failed.
- **Breaking:** devdocs keeps its state, such as the response cache and the run ledger, in `<project>/.devdocs/`, which `generate` creates. `devdocs init` adds `.devdocs/` to an existing `.gitignore`.
- Each kind of work goes to one model, chosen per tier in a catalog of models and prices; another model is asked only when that one is out of reach, as after a 404 or a spent daily quota, or when its answer is unusable. A call that meets a rate limit, a server error or a network failure is retried with backoff. `--model` pins one model for every call. v3 tried eight Gemini models in turn, starting with the most expensive, and moved on after any error.
- The project name comes from `--name` or the `name` setting, else from `pyproject.toml`, `package.json` or `Cargo.toml`, else the folder name. v3's placeholders, "My Project" and "No description provided.", are gone.
- Progress goes to stderr and results to stdout, without emoji; `-q` hides the progress lines.

### Removed

- **Breaking:** The interactive prompt for the API key.
- The runtime dependencies `google-genai` and `httpx`: devdocs runs on the standard library alone.
- **Breaking:** The effect of `--overwrite`, `--authors` and `--keywords`. A v3 line may still pass them, and they are ignored. devdocs never writes the project's own `README.md`, which v3 replaced with `--overwrite`.

### Fixed

These are defects of 3.0.0 that 4.0.0 fixes.

- A file or directory is no longer skipped because its name contains a word such as `out`, `lib`, `util`, `env`, `bin` or `debug`, as v3 skipped `routes.py`, `layout.tsx` and `src/utils/`, and `__init__.py` files are read.
- The name of the output directory no longer hides source files: with `--output site`, v3 skipped every name containing `site`, such as `website.py`.
- A folder that can be entered but not listed, or a broken symbolic link, no longer ends the walk. v3 lost its place in the tree there, wrote no more pages and still reported success.
- A rejected API key fails once, with exit 3, instead of costing eight requests for every file and folder.
- A generation that fails is reported and sets the exit status. v3 wrote a page saying `Failed to generate documentation` and printed its success line.
- Sources whose pages would share a name, such as `cart.py` and `cart.js` in one directory, or names that differ only in case, each get their own page.
- A missing project directory is an error, exit 2, before anything is created. v3 created the output directory, asked for the key, and exited 0.
- The package ships the MIT license text. 3.0.0 declared the MIT license but had no `LICENSE` file.

### Security

- devdocs no longer echoes the API key: v3's prompt showed it as it was pasted. devdocs never writes the key to disk, sends it only in a request header (`x-goog-api-key`, `x-api-key`), never in a URL, and `doctor` reports where it came from, never the key.
- Argument errors and the v3 notice show `***` for an API key that the command line names: after `--api-key` in any case, punctuation or abbreviation, after another flag for a key, token or secret, or in an assignment such as `GEMINI_API_KEY=...`. A project path that can only be a key is refused, masked. A bare key that no flag or assignment names is printed like any other argument.
- Providers `gemini` and `anthropic` refuse a key that holds a line break or another character an HTTP header cannot carry, before any connection and without printing it.
- Providers `gemini` and `anthropic` verify certificates and require TLS 1.2 or later.
- Every page escapes the text it takes from source code and model replies, or sets it as code, so a docstring or a reply cannot reshape a page or add script to it. The HTML site's Content-Security-Policy allows only stylesheets and scripts from the site's own origin, and no page carries inline script.
- No shared demo key ships. The Gemini demo key that has sat in the repository's `secret` file since 1.x was revoked by Google, and no code reads it.
- The container image runs as a non-root user; the compose service adds a read-only root file system, drops every capability and sets `no-new-privileges`.
- Releases are built by GitHub Actions from their tag, with build provenance: trusted publishing with attestations on PyPI, provenance on npm, and provenance and an SBOM for the container image.

## [3.0.0] - 2026-04-01

### Changed

- Tries eight Gemini models in turn, from `gemini-3.1-pro-preview` down to `gemini-2.0-flash-lite`, moving to the next after any error.
- The code is no longer minified and has type hints. The `devDocs` command runs `devDocs.cli:main`.
- The license is declared as the SPDX expression `MIT`.

## [2.0.7] - 2025-08-06

### Fixed

- The `devDocs` command runs again. From 2.0.3 to 2.0.6 its entry point named the wrong function, so it failed with a `TypeError`; `python -m devDocs.cli` worked.

## [2.0.6] - 2025-08-06

### Changed

- The version number only.

## [2.0.5] - 2025-08-06

### Changed

- The version number only.

## [2.0.3] - 2025-08-06

### Changed

- Falls back across four Gemini models, `gemini-2.0-flash-lite`, `gemini-2.0-flash`, `gemini-2.5-flash-lite` and `gemini-2.5-flash`, when a call fails, and no longer waits a second before each call.
- The `devDocs` command fails with a `TypeError` in this version, fixed in 2.0.7.

## [1.0.3] - 2025-07-26

### Changed

- The version number only.

## [1.0.2] - 2025-07-26

### Changed

- Package metadata only.

## [1.0.1] - 2025-07-26

### Added

- The `devDocs` command, as the repository's first commit has it. It walks a project and writes a Markdown README for each file and folder that its name filters keep, and one for the project, with Google Gemini (`gemini-2.0-flash-lite`), waiting a second before each call and asking for the API key at a prompt. Its flags are `-p`/`--path`, `--name`, `--description`, `--authors`, `--keywords`, `--overwrite`, `--output`, `--exclude` and `--include`. It declares Python 3.8 or later.

[Unreleased]: https://github.com/bgantavya/devDocs/compare/v3.0.0...v4
[3.0.0]: https://github.com/bgantavya/devDocs/compare/v2.0.7...v3.0.0
[2.0.7]: https://github.com/bgantavya/devDocs/compare/v2.0.6...v2.0.7
[2.0.6]: https://github.com/bgantavya/devDocs/compare/03d2b15879b092e0469b7f21abd9181d0c417d22...v2.0.6
[2.0.5]: https://github.com/bgantavya/devDocs/compare/v2.0.3...03d2b15879b092e0469b7f21abd9181d0c417d22
[2.0.3]: https://github.com/bgantavya/devDocs/compare/v1.0.3...v2.0.3
[1.0.3]: https://github.com/bgantavya/devDocs/compare/02e4d0b5fbabd38277b5b1f1c177e2c48c95276f...v1.0.3
[1.0.2]: https://github.com/bgantavya/devDocs/compare/ba57599ea0df78d253e3eaf24c751f4bbc6a3748...02e4d0b5fbabd38277b5b1f1c177e2c48c95276f
[1.0.1]: https://github.com/bgantavya/devDocs/tree/ba57599ea0df78d253e3eaf24c751f4bbc6a3748
