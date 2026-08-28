# @bgantavya/devdocs

devdocs writes Markdown documentation for a code base with an LLM (Gemini or
Claude) on a hard cost budget. This package runs the devdocs command line from
npm: it bundles the devdocs Python source and runs it with your own Python, so
it installs no Python packages and has no npm dependencies.

## Requirements

- **Python 3.11 or newer.** The wrapper tries `python3.15` down to
  `python3.11`, then `python3` and `python`. On Windows it tries the `py`
  launcher first, and looks only in the folders on PATH, never in the current
  directory. To choose an interpreter, set `DEVDOCS_PYTHON` to its path.
  To get Python, see [python.org](https://www.python.org/downloads/) or run
  `uv python install 3.13`.
- **Node.js** 20.17 or a later 20.x, or 22.9 or newer.
- **An API key** for a run that calls a model: `GEMINI_API_KEY` for Gemini
  (the default provider), or `ANTHROPIC_API_KEY` with
  `DEVDOCS_PROVIDER=anthropic` for Claude.

## Run without installing

```sh
npx @bgantavya/devdocs estimate   # price a run for the current directory; sends nothing
npx @bgantavya/devdocs generate   # write the docs, into docs/ by default
```

## Install globally

```sh
npm install -g @bgantavya/devdocs
devdocs --version
devdocs doctor --offline          # check the local setup
```

npm 10 and 11 run a check during the install and stop it when no Python 3.11+
is found; npm 11 also warns that the check is not covered by `allowScripts`.
npm 12 and later run install scripts only for packages you allow, for example
with `npm install -g --allow-scripts=@bgantavya/devdocs @bgantavya/devdocs` or
an `allowScripts` entry in a project's `package.json`; otherwise devdocs makes
the same check when it starts. Set `DEVDOCS_SKIP_PYTHON_CHECK=1` to skip the
install-time check, for example in an image that installs Python afterwards.

## Commands

| Command                             | What it does                             |
| ----------------------------------- | ---------------------------------------- |
| `devdocs generate [path]`           | write the docs, into `docs/` by default  |
| `devdocs estimate [path]`           | price a run; sends nothing               |
| `devdocs doctor [--offline] [path]` | check that a run can work                |
| `devdocs init [path]`               | write a starter `devdocs.toml`           |
| `devdocs models`                    | list the models your key can use         |
| `devdocs cache stats\|clear [path]` | inspect or clear the response cache      |

`devdocs --help` and `devdocs <command> --help` list every command and option.
Any setting can also be given as an environment variable named
`DEVDOCS_<NAME>`, for example `DEVDOCS_PROVIDER=anthropic`.

The main exit codes: 0 success, 1 a blocking problem or an internal error,
2 a configuration error, 3 an authentication failure, 4 a partial failure,
7 stopped at the budget, 10 the provider quota is used up, 130 interrupted.

The same tool is published on PyPI as `devDocs`. Report problems at
<https://github.com/bgantavya/devDocs/issues>.
