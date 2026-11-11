# devdocs

Documentation from source code: exact facts from parsers, model prose only where it adds something, on a hard budget. No dependencies.

[![CI](https://img.shields.io/github/actions/workflow/status/bgantavya/devDocs/publish.yml?branch=v4&label=CI)](https://github.com/bgantavya/devDocs/actions/workflows/publish.yml?query=branch%3Av4)
[![PyPI](https://img.shields.io/pypi/v/devDocs)](https://pypi.org/project/devDocs/)
[![Python versions](https://img.shields.io/pypi/pyversions/devDocs)](https://pypi.org/project/devDocs/)
[![License](https://img.shields.io/github/license/bgantavya/devDocs)](https://github.com/bgantavya/devDocs/blob/v4/LICENSE)

devdocs is a command-line tool that writes the documentation of a code base: an overview of the architecture, a page for each module (each directory of source files), and a page for each source file other than tests and vendored or generated code. Parsers supply the facts. A model, Google Gemini or Anthropic Claude, writes prose only where it adds something, within a hard budget.

## Why devdocs

- **Facts come from parsers.** Parsers for Python, JavaScript, TypeScript, Go, Rust and Java read each file's public symbols, signatures, docstrings and imports, and the pages print signatures as the source writes them. The model writes prose around these facts; it never supplies them.
- **Prose only where it adds something.** devdocs ranks the files and gives prose to those where it adds most. A file its own docstrings already document, or one that ranks low, gets a page from facts alone, called a stub, at no cost. `devdocs estimate --explain` shows each file's plan and the reason for it.
- **A hard budget, priced first.** A run spends at most $0.50 unless you set another limit; a limit of 0 means no limit, not zero spend. Each request reserves its worst-case price before it is sent, so a run stays within the limit, and `devdocs estimate` prices a run before you pay for anything.
- **Cached and resumable.** Every usable answer is kept in `.devdocs/` in the project, and reused for free while its prompt and model stay the same ([costs.md](https://github.com/bgantavya/devDocs/blob/v4/docs/costs.md#what-a-re-run-pays-for)). A stopped run resumes where it stopped.
- **Markdown, HTML and PDF.** Markdown pages, a static HTML site that needs no server, and PDF from a built-in engine, or from an installed Chrome, Chromium, Edge or WeasyPrint for all of Unicode.
- **Gemini or Claude.** Gemini is the default; Claude is one setting away.
- **No dependencies.** Python 3.11 or newer and its standard library, nothing else. devdocs also installs from npm, and runs as a container image.

## Quick start

Install devdocs with [pipx](https://pipx.pypa.io/):

```sh
pipx install "devDocs>=4"
```

devdocs needs Python 3.11 or newer. If pipx runs on an older Python, name a newer one, as in `pipx install --python python3.13 "devDocs>=4"`. If you have no Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) downloads one as it installs devdocs: `uv tool install --python 3.13 "devDocs>=4"`. [The install guide](https://github.com/bgantavya/devDocs/blob/v4/docs/install.md) covers every method, and upgrading and removing.

Check the setup with `devdocs doctor --offline`. It sends nothing, and marks the API key `!!` until you set one.

Get an API key from [Google AI Studio](https://aistudio.google.com/apikey) for Gemini, or from the [Anthropic Console](https://console.anthropic.com/) for Claude. In bash or zsh, run the line for your provider, paste the key and press Enter. `read -s` keeps the key off the screen and out of your shell history.

```sh
read -rs GEMINI_API_KEY && export GEMINI_API_KEY
```

For Claude, the line also selects the provider:

```sh
read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY DEVDOCS_PROVIDER=anthropic
```

In PowerShell, use `$env:GEMINI_API_KEY = 'your-key'`, or for Claude `$env:ANTHROPIC_API_KEY = 'your-key'; $env:DEVDOCS_PROVIDER = 'anthropic'`, with your key in place of `your-key`.

Then, at the root of your project, write a starter `devdocs.toml`:

```sh
devdocs init
```

For Claude, run `devdocs init --provider anthropic` instead, so the file selects Claude too. `init` also adds `.devdocs/`, where devdocs keeps what it buys, to your `.gitignore`; it creates no `.gitignore` if you have none.

If your project already has a `docs/` directory, change the file's `out = "docs"` line to a directory of devdocs' own, such as `out = "api-docs"`, before you go on. devdocs replaces a file it did not write only when you pass `--force`: without it, a run refuses when such a file is in its way, and otherwise writes its pages among yours.

Price the run, then make it:

```sh
devdocs estimate
devdocs generate
```

`estimate` sends nothing. `generate` sends the provider your file paths and the source of the files it writes prose for, with lines from related files; [SECURITY.md](https://github.com/bgantavya/devDocs/blob/v4/SECURITY.md#generated-documentation-quotes-your-code) says what goes out. It writes the documentation to `docs/`, or to the directory you set in `out`. Start reading at its `README.md`, the home page.

For a site you can open in a browser, run `devdocs export --format html` and open `docs/index.html`; `devdocs export --format pdf` makes a PDF book. `export` renders the last run again, with no key and no cost. The next `generate` removes exported files in any format it does not write, since they describe an older run. To keep a format, give it to `generate` too, as in `devdocs generate --format markdown,html`, or set `formats` in `devdocs.toml`.

`generate` also creates `.devdocs/` in the project root, which holds every usable answer and the record of each run. Keep it, so later runs reuse those answers, but keep it out of version control. Without a `.gitignore`, or with a `devdocs.toml` you wrote yourself, add the line `.devdocs/` to `.gitignore` by hand: `init --force` would replace your `devdocs.toml`.

## A sample run

On a small project, a `shop` package of six Python files, with one test file, a `pyproject.toml` and the `devdocs.toml` that `init` wrote, `estimate` prints:

```text
scanned 9 files, 6 documentable, 8 model calls planned (0.0s)
6 files: 6 with prose, 0 from facts alone
8 model calls (1 architecture, 6 files, 1 module), 0 already cached
at most 16,222 input / 3,794 output tokens, at most $0.0214
```

`generate` then writes:

```text
docs/
├── README.md          home page
├── architecture.md    architecture overview
├── index.json         manifest: the run's documents and files
└── shop/
    ├── index.md       module page
    ├── __init__.py.md
    ├── cart.py.md
    ├── cli.py.md
    ├── money.py.md
    ├── orders.py.md
    └── tax.py.md
```

Each file page gives the facts: the file's module, language and line count, whether it is an entry point, the files and tests that import it, its public symbols with their exact signatures and docstrings, its dependencies, and the TODO, FIXME and similar notes in its comments. Where the plan gives a file prose, the model adds a summary, a sentence for each public name the source leaves undocumented, and notes on behaviour the code does not make obvious.

## Costs

- By default, file pages get their prose from the provider's cheapest models, such as `gemini-2.5-flash-lite` or `claude-haiku-5-5`, and the module summaries and the architecture overview from stronger ones. `--quality balanced` or `high` plans prose for more files and sends more of the work to stronger models, so it costs more. Under the default limit, `high` can end with fewer files with prose than `cheap`; `estimate` shows how many get prose before you pay.
- Files that need no prose get none. Every usable answer is cached under its prompt, so a run after an edit pays only for the prompts the edit changed: the edited files, files that gained or lost an importer or a test, and the module summaries and the architecture overview above them when what they sum up has changed.
- A run is capped at $0.50 by default. A plan whose worst-case price is higher is cut to fit before anything is sent, and what is cut becomes a stub. Set `--max-cost` to change the limit, and `--max-requests` to cap the requests. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend.
- `devdocs estimate` prices the plan at its worst case, offline and free. It assumes your key reaches devdocs' first-choice models, and leaves out retries and escalations to stronger models; the limits cap those all the same.
- By default devdocs keeps to Gemini's free-tier rate limits and models, so a run can pause for up to a minute at a time, printing nothing, while a model's per-minute limit frees up ([troubleshooting.md](https://github.com/bgantavya/devDocs/blob/v4/docs/troubleshooting.md#a-run-pauses-with-no-output)); pass `--paid` with a paid key. Free tier or not, devdocs prices every call at the paid rates in its catalog: it does not read the vendor's bill.
- Google now serves the 2.5 models only to projects that used them before, so with a key from a new project devdocs uses the 3.x models, which cost more per token than `estimate` assumes. On the free tier such a key reaches no architecture-tier model, so a module-tier model writes the overview, and at `--quality high` every page. The progress lines of `generate` name the model behind each call, and [models.md](https://github.com/bgantavya/devDocs/blob/v4/docs/models.md#gemini-models) explains the routing.

[costs.md](https://github.com/bgantavya/devDocs/blob/v4/docs/costs.md) explains what a run costs and how to keep it low.

## Commands

| Command | What it does |
|---|---|
| `devdocs generate [path]` | Write docs for a project. |
| `devdocs estimate [path]` | Price a run, send nothing. |
| `devdocs export [path]` | Render the last run again, in other formats; no key, no cost. |
| `devdocs doctor [path]` | Check that a run can work. |
| `devdocs init [path]` | Write a starter `devdocs.toml`. |
| `devdocs models` | List the models devdocs knows for the provider, their prices, and which your key can use. |
| `devdocs cache {stats,clear} [path]` | Inspect or clear the response cache. |

`devdocs <command> --help` lists a command's options. [commands.md](https://github.com/bgantavya/devDocs/blob/v4/docs/commands.md) documents every flag and exit code.

## Configuration

Settings come from flags, `DEVDOCS_*` environment variables and a `devdocs.toml` file, in that order of precedence. A minimal file, in the project root:

```toml
[devdocs]
path = "."             # the project root: this file's directory
out = "docs"           # where the docs go, under the project root
provider = "gemini"
quality = "cheap"
max_cost = 0.50        # USD per run; 0 means no limit, not zero spend
formats = ["markdown", "html"]
```

With `path = "."`, devdocs documents the whole project from any directory in it; without `path`, the project is the directory you run devdocs in. `devdocs init` writes a commented starter file, without `path`. [configuration.md](https://github.com/bgantavya/devDocs/blob/v4/docs/configuration.md) lists every setting.

## CI, Docker and upgrading

- **CI:** devdocs runs unattended, with its key from a secret, a budget and a cached `.devdocs/`, so a re-run reuses the answers already bought. See [ci.md](https://github.com/bgantavya/devDocs/blob/v4/docs/ci.md).
- **Docker:** `docker run --rm -it -u "$(id -u):$(id -g)" -v "$PWD:/work" -e GEMINI_API_KEY ghcr.io/bgantavya/devdocs:4.0.0 generate` documents the current directory. See [the install guide](https://github.com/bgantavya/devDocs/blob/v4/docs/install.md#docker).
- **From 3.x:** version 4 is a rewrite, and its command is `devdocs`, in lower case. Uninstall 3.x first: on macOS and Windows its `devDocs` command can answer for `devdocs`. A 3.x command line that uses a flag only 3.x had, such as `-p` or `--output`, still runs, translated, with a notice; a bare `devdocs` prints the help. See [migration.md](https://github.com/bgantavya/devDocs/blob/v4/docs/migration.md).

## Documentation

[The documentation index](https://github.com/bgantavya/devDocs/blob/v4/docs/README.md) lists every page with the question it answers.

- [Install](https://github.com/bgantavya/devDocs/blob/v4/docs/install.md): every way to install, upgrade and remove devdocs.
- [Commands](https://github.com/bgantavya/devDocs/blob/v4/docs/commands.md): every command, flag and exit code, and how a run stops.
- [Configuration](https://github.com/bgantavya/devDocs/blob/v4/docs/configuration.md): `devdocs.toml`, environment variables and the API key.
- [Models](https://github.com/bgantavya/devDocs/blob/v4/docs/models.md): providers, keys and how models are chosen.
- [Costs](https://github.com/bgantavya/devDocs/blob/v4/docs/costs.md): what a run costs, the budget, and `estimate`.
- [Context](https://github.com/bgantavya/devDocs/blob/v4/docs/context.md): the cross-file context each prompt gets.
- [Output](https://github.com/bgantavya/devDocs/blob/v4/docs/output.md): what a run writes, the formats, the files devdocs never replaces, and `export`.
- [Troubleshooting](https://github.com/bgantavya/devDocs/blob/v4/docs/troubleshooting.md): `doctor`, then problems and fixes.
- [Migration](https://github.com/bgantavya/devDocs/blob/v4/docs/migration.md): moving from 3.x.
- [CI](https://github.com/bgantavya/devDocs/blob/v4/docs/ci.md): unattended runs, GitHub Actions and Docker.
- [Architecture](https://github.com/bgantavya/devDocs/blob/v4/docs/architecture.md): how devdocs works inside, for contributors.
- [Changelog](https://github.com/bgantavya/devDocs/blob/v4/CHANGELOG.md): what changed in each release.

## Contributing, security and license

- [CONTRIBUTING.md](https://github.com/bgantavya/devDocs/blob/v4/CONTRIBUTING.md) covers the development setup, the checks a change must pass, and how to send a pull request.
- [SECURITY.md](https://github.com/bgantavya/devDocs/blob/v4/SECURITY.md) says what devdocs sends to the provider, and how to report a vulnerability.
- Everyone taking part follows the [code of conduct](https://github.com/bgantavya/devDocs/blob/v4/CODE_OF_CONDUCT.md).
- devdocs is released under the [MIT License](https://github.com/bgantavya/devDocs/blob/v4/LICENSE).
- Report a bug, a wrong generated page or a feature request in [the issue tracker](https://github.com/bgantavya/devDocs/issues).
