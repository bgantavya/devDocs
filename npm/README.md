# @bgantavya/devdocs

devdocs writes the documentation of a code base, as Markdown, an HTML site or PDF. Parsers supply the exact facts, and a model, Google Gemini or Anthropic Claude, writes prose only where it adds something, on a hard cost budget.

This package runs the devdocs command line from npm. It bundles the devdocs Python source and runs it with your own Python, so it installs no Python packages and has no npm dependencies. The same tool is on PyPI as [devDocs](https://pypi.org/project/devDocs/).

## Requirements

- **Node.js** 20.17 or a later 20.x, or 22.9 or newer.
- **Python 3.11 or newer.** The package bundles no interpreter; the section on how Python is found, below, explains which one it runs.
- **An API key** for the commands that call a model: `GEMINI_API_KEY` for Gemini, the default provider, or `ANTHROPIC_API_KEY` with `DEVDOCS_PROVIDER=anthropic` for Claude. `estimate`, `export`, `init` and `cache` need none.

## Install and run

Run it without installing:

```sh
npx @bgantavya/devdocs estimate
npx @bgantavya/devdocs generate
```

Or install it globally:

```sh
npm install -g @bgantavya/devdocs
devdocs --version
devdocs doctor --offline
```

`estimate` prices a run for the current directory and sends nothing. `generate` writes the documentation, into `docs/` by default. `devdocs doctor --offline` checks the setup without sending anything: Python, SQLite, TLS, the PDF engines, the configuration and the API key.

When the package is installed, its install script checks for Python 3.11 or newer and fails the install if it finds none. Set `DEVDOCS_SKIP_PYTHON_CHECK=1` to skip that check, for example in an image that adds Python afterwards. When install scripts do not run, as with `--ignore-scripts`, nothing is checked at install time; the `devdocs` command looks for Python each time it starts in any case.

Upgrade with `npm install -g @bgantavya/devdocs@latest`, and remove it with `npm uninstall -g @bgantavya/devdocs`.

## How Python is found

If `DEVDOCS_PYTHON` is set and not empty, it is the only interpreter tried, as a command name or a path, with no fallback. Otherwise the first of these that reports Python 3.11 or newer runs devdocs:

- macOS and Linux: `python3.15`, `python3.14`, `python3.13`, `python3.12`, `python3.11`, then `python3`, then `python`;
- Windows: `py -3.15` down to `py -3.11`, then `py -3`, then `python3.15` down to `python3.11`, then `python`, then `python3`.

On Windows a bare command name is looked up only in the absolute folders on PATH, never in the current directory. A Python newer than 3.15 is found through `python3`, `python`, `py -3` or `DEVDOCS_PYTHON`.

When none is found, devdocs does not start:

```text
devdocs: Python 3.11 or newer is required, and none was found on PATH.
Install it from https://www.python.org/downloads/ or with `uv python install 3.13`,
or set DEVDOCS_PYTHON to the path of a Python 3.11+ interpreter.
```

When `DEVDOCS_PYTHON` is set, the message names it instead and says it is older than 3.11 or does not run.

The launcher runs `python -P -m devdocs` with your arguments. It puts the bundled package first on `PYTHONPATH`, keeping only the absolute entries of your own value, and sets `PYTHONDONTWRITEBYTECODE=1` unless you set it. Everything else in your environment, API keys and `DEVDOCS_*` settings included, reaches devdocs unchanged. The [configuration reference](https://github.com/bgantavya/devDocs/blob/v4/docs/configuration.md#the-npm-launcher) lists the variables the launcher reads and sets.

Provider `anthropic-sdk` needs Anthropic's SDK, which this package does not install: it works only if the Python that runs devdocs can import `anthropic`. Provider `anthropic` reaches Claude with nothing extra.

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

A run is capped at $0.50 by default. `--max-cost` changes the cap and `--max-requests` limits the requests; `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend. `devdocs estimate` prices the planned calls at their worst case, and sends nothing. The [costs guide](https://github.com/bgantavya/devDocs/blob/v4/docs/costs.md) explains the budget.

`devdocs <command> --help` lists a command's options. Settings come from flags, from `DEVDOCS_*` environment variables such as `DEVDOCS_PROVIDER=anthropic`, and from a `devdocs.toml` file. The [commands reference](https://github.com/bgantavya/devDocs/blob/v4/docs/commands.md) and the [configuration reference](https://github.com/bgantavya/devDocs/blob/v4/docs/configuration.md) cover them all.

## Exit codes and signals

The launcher exits with devdocs' own exit code, unchanged: 0 on success, 130 after a stop by SIGINT, 143 after a stop by SIGTERM, and the others in the [exit code table](https://github.com/bgantavya/devDocs/blob/v4/docs/commands.md#exit-codes). It adds two cases of its own:

- **1** when it cannot start devdocs: no Python 3.11 or newer, a bundle missing from the package, a package folder whose path holds the PATH separator (`:`, or `;` on Windows), or a Python that fails to start. It prints a `devdocs:` line that says which. devdocs itself also exits 1 in some cases, such as an internal error or a blocking problem that `doctor` found; the table lists them.
- **128 plus the signal's number** when devdocs is ended by a signal before it can exit by itself, such as 137 for SIGKILL.

The launcher always passes SIGTERM on to devdocs. It passes SIGINT on only when it has no controlling terminal and is not on Windows, since a terminal's Ctrl-C already reaches devdocs directly. Once `generate` has begun buying answers, the first signal stops it gracefully: requests already sent finish and are cached, every page is written, and the next run resumes. The [CI guide](https://github.com/bgantavya/devDocs/blob/v4/docs/ci.md#timeouts-and-cancellation) explains what a stop keeps.

## More

- [Installing devdocs](https://github.com/bgantavya/devDocs/blob/v4/docs/install.md), in every way the project supports.
- [The devdocs README](https://github.com/bgantavya/devDocs/blob/v4/README.md), with a quick start and the documentation index.
- [Running devdocs in CI](https://github.com/bgantavya/devDocs/blob/v4/docs/ci.md).
- Report problems at <https://github.com/bgantavya/devDocs/issues>.
