# Configuration

devdocs takes its settings from command-line flags, environment variables, the project's `devdocs.toml` file and its built-in defaults. This page covers how the file is found, every setting, the order in which the sources apply, every environment variable, how the API key is found, and the messages devdocs gives for a value it refuses. [commands.md](commands.md) lists which command takes which flag.

## Precedence

A flag beats an environment variable, which beats `devdocs.toml`, which beats the built-in default:

| Order | Source | Example |
|---|---|---|
| 1 | Flag | `--max-cost 1` |
| 2 | Environment variable | `DEVDOCS_MAX_COST=1` |
| 3 | `devdocs.toml` | `max_cost = 1` |
| 4 | Default | `0.50` |

- A flag you do not give leaves the lower sources in effect.
- Values never merge across sources. A list from a higher source replaces the lower one whole: with `exclude = ["examples/", "scripts/"]` in the file and `--exclude benchmarks/` on the command line, the run's `exclude` is `benchmarks/` alone, and `examples/` and `scripts/` are no longer excluded.
- The API key has an order of its own; see [The API key](#the-api-key).
- `generate`, `estimate`, `export`, `doctor` and `models` read all four sources. `cache` reads the file, the environment and its `path` argument. `init` reads only its own flags and `$DEVDOCS_PATH`.

## devdocs.toml

### Where devdocs looks for it

devdocs reads at most one file, named `devdocs.toml`. It reads no other configuration file: there is no `[tool.devdocs]` table in `pyproject.toml`, no user-wide file and no flag to name a file.

The search starts at:

1. the command's `path` argument, if you give one;
2. else `$DEVDOCS_PATH`, if it is set and not blank;
3. else the current directory.

`models`, which has no `path` argument, starts at `$DEVDOCS_PATH` or the current directory. The start is checked as a project root before any file is read, so a mistyped path is reported as missing, not hidden by a `devdocs.toml` found above it.

From the start, devdocs walks up through the parent directories. In each one:

- if it holds a `devdocs.toml` file, that file is used and the search ends;
- if it holds `.git`, a directory or a file as in worktrees and submodules, the search ends there, with no file;
- the home directory ends the search before it is looked at, unless the search started there. A `devdocs.toml` in your home directory is therefore read only when the search starts at home: when you run devdocs there, or name home as the `path` argument or in `$DEVDOCS_PATH`. This holds even if home is a git repository. A home directory that does not exist, such as the container image's `/nonexistent`, ends nothing.

Outside any repository and outside home, the walk goes up to the filesystem root, and the first `devdocs.toml` on the way applies.

A `devdocs.toml` that devdocs cannot read is an error. A symbolic link named `devdocs.toml` that leads nowhere is ignored, as if it were absent.

### Relative paths

- A relative `path` in the file is read from the directory that holds the file. A relative `path` from the command line or `$DEVDOCS_PATH` is read from the current directory.
- A relative `out`, from any source, is read from the project root.
- A leading `~` in `path` or `out` is expanded to your home directory, from any source. No other setting expands `~`.

For example, with this layout:

```text
shop/
├── .git/
├── devdocs.toml
└── src/
    └── shop/
```

Running devdocs from `shop/src/shop` uses `shop/devdocs.toml`. If the file has no `path`, the project root is the current directory, `shop/src/shop`, and the output goes to `shop/src/shop/docs`. With `path = "."` in the file, the project root is `shop`, the file's own directory, wherever you run devdocs inside it.

### Format

The file is TOML. Put the settings in a `[devdocs]` table, or at the top level of a file that has no `[devdocs]` table.

- With a `[devdocs]` table, keys and tables outside it are ignored.
- Without one, every top-level key must be a setting, so another table such as `[tool]` is refused as an unknown option.
- `-` and `_` are interchangeable in key names: `max-cost` is `max_cost`. If a file holds both spellings of one key, the later one wins.
- An unknown key is an error, and the message lists the valid keys.

The value types:

| Type | Accepted in the file |
|---|---|
| path (`path`, `out`) | A string, not empty or blank, without a NUL character. |
| list (`include`, `exclude`, `formats`) | An array, or one string of comma-separated items. Array items are taken as text exactly as written, spaces included; items of a string are trimmed, and empty ones dropped. |
| boolean | `true` or `false`. A string such as `"yes"` is refused. |
| integer (`max_requests`, `concurrency`) | A TOML integer. `2.0` is refused. |
| number (`max_cost`) | A TOML integer or float. |
| string (the other settings) | Any value, converted to text, then checked against the setting's choices. |

### Annotated example

A file with every setting. The values shown are the defaults, except `path`, `exclude`, `formats`, `name` and `description`. Writing `path = "."` is not the same as leaving `path` out: it makes this file's directory the project root, wherever below it you run devdocs.

```toml
# devdocs.toml: this project's devdocs settings.
# Flags and DEVDOCS_* variables override what is set here.
[devdocs]

# Where the project and its docs are
path = "."                    # project root, read from this file's directory
out = "docs"                  # output directory, under the project root

# Who writes the prose
provider = "gemini"           # gemini | anthropic | anthropic-sdk
model = ""                    # one model for every call; "" lets devdocs choose
quality = "cheap"             # cheap | balanced | high
context = "standard"          # off | standard | semantic (Gemini only, paid)

# Budget and pacing
max_cost = 0.50               # US dollars per run; 0 means no limit
max_requests = 0              # requests per run, retries included; 0 means no limit
concurrency = 4               # requests to a model at one time, at least 1
free_tier = true              # pace to free-tier limits; false is what --paid sets

# Which files
include = []                  # source to take in despite ignores; literal text, not globs
exclude = ["examples/"]       # more files to leave out, as .gitignore lines

# Output formats
formats = ["markdown", "html"]  # any of markdown, html, pdf
pdf_engine = "builtin"        # builtin | chrome | weasyprint
pdf_split = false             # one PDF per document; built-in engine only
page_size = "A4"              # A4 | Letter
per_file = false              # v3 page layout: docs/<dir>/<name>.md

# What the docs call the project
name = "shop"                 # "" takes it from pyproject.toml, package.json, Cargo.toml or the folder
description = "A small online shop."  # one line under the name

# api_key = ""                # possible, but keep keys out of files you commit
```

`devdocs init` writes a shorter starter file; see [commands.md](commands.md#init).

## Settings

Every setting except `api_key` has an environment variable named `DEVDOCS_` and the key in upper case. `api_key` has `--api-key` and the key variables instead, which come before the file; see [The API key](#the-api-key).

| Key | Type | Default | Flag | Environment variable | Meaning | Validation |
|---|---|---|---|---|---|---|
| `path` | path | the current directory | `path` argument | `DEVDOCS_PATH` | The project root. | Not empty, no NUL; an existing directory devdocs can list. |
| `out` | path | `"docs"` | `--out` | `DEVDOCS_OUT` | The output directory, relative to the project root. | Not empty, no NUL; not the project root or a directory above it. |
| `provider` | string | `"gemini"` | `--provider` | `DEVDOCS_PROVIDER` | Whose models write the prose. `anthropic` uses devdocs' own HTTP client; `anthropic-sdk` uses Anthropic's SDK, from the `anthropic` extra. | `gemini`, `anthropic` or `anthropic-sdk`, in lower case. |
| `model` | string | `""` | `--model` | `DEVDOCS_MODEL` | One model for every call. `""` lets devdocs choose per task. | Any id. A leading `models/` and surrounding spaces are dropped. |
| `quality` | string | `"cheap"` | `--quality` | `DEVDOCS_QUALITY` | How much prose, from how strong a model. | `cheap`, `balanced` or `high`. |
| `context` | string | `"standard"` | `--context` | `DEVDOCS_CONTEXT` | Cross-file context in prompts. `standard` sends no extra request but adds input tokens; `semantic` adds paid embeddings. | `off`, `standard` or `semantic`. `semantic` needs provider `gemini`. |
| `include` | list | `[]` | `--include` | `DEVDOCS_INCLUDE` | Source files to take in that ignores, `exclude` or the 1,000,000-byte size limit leave out. Each item is literal text in any case, not a glob: a path from the project root, which also reaches inside an ignored directory; a file or directory name at any depth; or the end of a path such as `.sql`. Tests, vendored and generated code and files that are not source stay out. | A list. |
| `exclude` | list | `[]` | `--exclude` | `DEVDOCS_EXCLUDE` | Files to leave out, each item read as a line of a `.gitignore`, on top of the project's own ignore rules. What `include` takes in stays. | A list. |
| `max_cost` | number | `0.50` | `--max-cost` | `DEVDOCS_MAX_COST` | The most a run may spend, in US dollars. `0` means no limit, not zero spend. | Finite, and 0 or more. |
| `max_requests` | integer | `0` | `--max-requests` | `DEVDOCS_MAX_REQUESTS` | The most requests a run may send, retries included. `0` means no limit. | 0 or more. |
| `concurrency` | integer | `4` | `--concurrency` | `DEVDOCS_CONCURRENCY` | Requests to a model at one time. | 1 or more. |
| `free_tier` | boolean | `true` | `--paid` sets `false` | `DEVDOCS_FREE_TIER` | Paces requests to free-tier rate limits and, for Gemini, uses free-tier models only. It changes nothing for Anthropic, which has no free tier. | `true` or `false`. |
| `per_file` | boolean | `false` | `--per-file` sets `true` | `DEVDOCS_PER_FILE` | Uses the v3 page layout, `docs/<dir>/<name>.md`. | `true` or `false`. |
| `formats` | list | `["markdown"]` | `--format` | `DEVDOCS_FORMATS` | The formats to render. | Not empty; `markdown`, `html` or `pdf`, in lower case. |
| `pdf_engine` | string | `"builtin"` | `--pdf-engine` | `DEVDOCS_PDF_ENGINE` | What prints the PDF. `chrome` and `weasyprint` cover all of Unicode. | `builtin`, `chrome` or `weasyprint`. With PDF output, an external engine must be installed. |
| `pdf_split` | boolean | `false` | `--split`, `--no-split` | `DEVDOCS_PDF_SPLIT` | One PDF per document instead of one book. | `true` or `false`. With PDF output, only the built-in engine splits. |
| `page_size` | string | `"A4"` | `--page-size` | `DEVDOCS_PAGE_SIZE` | The PDF paper size. | `A4` or `Letter`, in any letter case. |
| `name` | string | `""` | `--name` | `DEVDOCS_NAME` | The project name in the docs. `""` takes it from `pyproject.toml`, `package.json` or `Cargo.toml`, else the folder name. | Any text. |
| `description` | string | `""` | `--description` | `DEVDOCS_DESCRIPTION` | A one-line description of the project. | Any text. |
| `api_key` | string | `""` | `--api-key`, on `generate`, `doctor` and `models` | `DEVDOCS_API_KEY` and the provider's key variables | The API key, as a last resort: the flag and the variables come first. See [The API key](#the-api-key). | Any text. |

Notes:

- `name` and `description` open every prompt, so they are part of each cached answer's identity. Changing either one makes the earlier answers unusable, and the next run pays for them again.
- The project root, `provider`, `quality`, `include` and `exclude` decide which unfinished run a `generate` resumes. Changing one starts a new run but does not clear the cache; [costs.md](costs.md#what-a-re-run-pays-for) says which answers a run buys again.
- An `out` inside a source directory is allowed. devdocs still never replaces a file it has no record of writing, unless you pass `--force`.
- A model the catalog does not know is priced at its vendor's highest rates, so the budget is never understated. A pinned model is used even if it is outside the free tier.
- Duplicate formats render once.
- The PDF settings in the file or the environment are accepted without PDF output. Their flags are not: see [commands.md](commands.md#output-options).
- No flag sets `free_tier` back to `true` or `per_file` back to `false`. To override the file, use `DEVDOCS_FREE_TIER=true` or `DEVDOCS_PER_FILE=false`.

[models.md](models.md), [costs.md](costs.md), [context.md](context.md) and [output.md](output.md) explain what these settings do to a run.

## Environment variables

### Setting variables

Each setting except `api_key` has a variable, listed in the [Settings](#settings) table: `DEVDOCS_PATH`, `DEVDOCS_OUT`, `DEVDOCS_PROVIDER`, `DEVDOCS_MODEL`, `DEVDOCS_QUALITY`, `DEVDOCS_CONTEXT`, `DEVDOCS_INCLUDE`, `DEVDOCS_EXCLUDE`, `DEVDOCS_MAX_COST`, `DEVDOCS_MAX_REQUESTS`, `DEVDOCS_CONCURRENCY`, `DEVDOCS_FREE_TIER`, `DEVDOCS_PER_FILE`, `DEVDOCS_FORMATS`, `DEVDOCS_PDF_ENGINE`, `DEVDOCS_PDF_SPLIT`, `DEVDOCS_PAGE_SIZE`, `DEVDOCS_NAME` and `DEVDOCS_DESCRIPTION`.

How their values are read:

- Surrounding spaces are removed, and a blank value counts as unset.
- Booleans take `1`, `true`, `yes` or `on`, and `0`, `false`, `no` or `off`, in any letter case.
- Integer variables take a whole number: `2.0` and `1e3` are refused. `DEVDOCS_MAX_COST` takes a number, such as `0.25`.
- Lists are comma-separated, and empty items are dropped: `DEVDOCS_FORMATS=markdown,pdf`.
- `DEVDOCS_PATH` and `DEVDOCS_OUT` are checked like their flags, with `~` expanded. A relative `DEVDOCS_PATH` is read from the current directory.

`DEVDOCS_PATH` also says where the search for `devdocs.toml` starts when no `path` argument is given, and which project `models` and `init` use.

For example:

```bash
DEVDOCS_QUALITY=balanced DEVDOCS_MAX_COST=1 devdocs estimate
```

### API key variables

| Variable | Read for | Order |
|---|---|---|
| `DEVDOCS_API_KEY` | every provider | first |
| `GEMINI_API_KEY` | `gemini` | after `DEVDOCS_API_KEY` |
| `GOOGLE_API_KEY` | `gemini` | after `GEMINI_API_KEY` |
| `ANTHROPIC_API_KEY` | `anthropic`, `anthropic-sdk` | after `DEVDOCS_API_KEY` |

[The API key](#the-api-key) describes the whole order.

### Other variables devdocs reads

| Variable | Effect |
|---|---|
| `DEVDOCS_DEBUG` | Any non-empty value, `0` included, makes an internal error print its traceback after the `devdocs: internal error:` line. The exit code is 1 either way. It changes nothing for other errors. |
| `HOME` | Where a leading `~` in `path` or `out` expands, and the home directory that ends the `devdocs.toml` search. On macOS, browsers for the `chrome` PDF engine are also looked for in `~/Applications`. On Windows, Python takes the home directory from `USERPROFILE`. |
| `PATH` | Where `git`, `weasyprint` and a Chrome, Chromium or Edge browser are found. |
| `HTTPS_PROXY`, `https_proxy` | An HTTP proxy for the provider requests, reached with HTTP CONNECT. A URL without a scheme is taken as `http://`. An `https://` URL is accepted, but devdocs still talks to the proxy without TLS: the scheme only sets the default port, 443. Any other scheme is refused. A user name and password in the URL are sent to the proxy. The lower-case form wins when both are set. On macOS and Windows, the system proxy settings apply when no proxy variable is set. |
| `NO_PROXY`, `no_proxy` | Hosts to reach without the proxy. |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | The CA certificates that TLS trusts. |
| `TMPDIR`, `TEMP`, `TMP` | Where an external PDF engine prints. |
| `PROGRAMFILES`, `PROGRAMFILES(X86)`, `LOCALAPPDATA` | On Windows, where devdocs looks for Chrome, Chromium or Edge. |

The proxy rows apply to devdocs' own HTTP client, which providers `gemini` and `anthropic` use. The provider endpoints are HTTPS, so `HTTP_PROXY` is not used for them, and `ALL_PROXY` is never used. SOCKS proxies are not supported. An error about a proxy never quotes its URL, which may hold a password.

With `provider = "anthropic-sdk"`, Anthropic's SDK sends the requests through its own HTTP client. devdocs passes it the key it found and the 120-second timeout, and turns off the SDK's own retries, since devdocs retries. Any other environment setting, such as a proxy, is the SDK's to read, not devdocs'.

### The npm launcher

The `@bgantavya/devdocs` package runs devdocs with a Python it finds on your machine. It passes your whole environment to devdocs and reads or sets these:

| Variable | Effect |
|---|---|
| `DEVDOCS_PYTHON` | The only interpreter the launcher tries, when set and not empty: a command name or a path. If it is older than 3.11 or does not run, the launcher exits 1, with no fallback. Otherwise the launcher searches for one, as [install.md](install.md#npm) describes. |
| `PYTHONPATH` | Set by the launcher: the bundled devdocs first, then the absolute entries of your own value. |
| `PYTHONDONTWRITEBYTECODE` | Set to `1`, unless you set it yourself. |

The package's install script also reads `DEVDOCS_SKIP_PYTHON_CHECK`, at install time only; [install.md](install.md#npm) describes it.

### The installer scripts

`scripts/install.sh` and `scripts/install.ps1` read `DEVDOCS_VERSION`, `DEVDOCS_SOURCE`, `DEVDOCS_PYTHON_VERSION` and `DEVDOCS_NO_UV_INSTALL`, at install time only. [install.md](install.md#installer-scripts) describes them and the installers.

### The container image and compose.yaml

The image `ghcr.io/bgantavya/devdocs` sets `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`, and runs as user 10001, whose home is `/nonexistent`, in `/work`. It reads no variable of its own: pass devdocs' variables with `-e`, such as `-e GEMINI_API_KEY`.

`compose.yaml` reads these on the host:

| Variable | Effect |
|---|---|
| `PWD` | The directory mounted at `/work`. Without it, the directory that holds `compose.yaml`. |
| `DEVDOCS_UID`, `DEVDOCS_GID` | The user and group the container runs as, so that `docs/` and `.devdocs/` belong to you. The default is `1000` for both. |

It passes into the container, by name, the four API key variables, the 19 setting variables and `DEVDOCS_DEBUG`. A variable your shell does not set stays unset in the container. `DEVDOCS_PATH` and `DEVDOCS_OUT` are read inside the container, so keep them relative and inside the mounted directory. compose passes no proxy variables. [install.md](install.md) and [ci.md](ci.md) show how to run the image.

### Names devdocs does not read

devdocs itself reads none of these, though they are easy to expect:

- `DEVDOCS_PAID`: use `DEVDOCS_FREE_TIER=false`.
- `DEVDOCS_FORMAT` and `DEVDOCS_SPLIT`: the names are `DEVDOCS_FORMATS` and `DEVDOCS_PDF_SPLIT`.
- `DEVDOCS_CONFIG`: devdocs reads no other configuration file.
- `DEVDOCS_FORCE`, `DEVDOCS_NO_RESUME`, `DEVDOCS_JSON`, `DEVDOCS_QUIET`, `DEVDOCS_EXPLAIN` and `DEVDOCS_OFFLINE`: these are flags only.
- `DEVDOCS_KEY`, `CLAUDE_API_KEY` and `ANTHROPIC_AUTH_TOKEN`: see the key variables above.

No setting, and no variable that devdocs reads, changes the provider endpoints, the 120-second network timeout, the retry policy, or how a run stops. With `anthropic-sdk`, the SDK's own environment settings still apply, as described above.

## The API key

`generate` and `models` need a key, and `doctor` checks for one. devdocs takes the first of these that holds a non-blank value, with surrounding spaces removed:

1. `--api-key`, on `generate`, `doctor` and `models`;
2. the provider's environment variables, in order:
   - `gemini`: `DEVDOCS_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`;
   - `anthropic` and `anthropic-sdk`: `DEVDOCS_API_KEY`, `ANTHROPIC_API_KEY`;
3. `api_key` in `devdocs.toml`.

`DEVDOCS_API_KEY` and `api_key` go to whichever provider is selected. A key in the other vendor's variable is never used: with only `GEMINI_API_KEY` set, `--provider anthropic` has no key.

Without a key, `generate` and `models` exit 2 before they create anything, and `doctor` reports it and exits 1:

```text
devdocs: error: no API key for provider 'gemini'
  hint: pass --api-key or set one of: $DEVDOCS_API_KEY, $GEMINI_API_KEY, $GOOGLE_API_KEY
```

For Anthropic the hint reads `pass --api-key or set one of: $DEVDOCS_API_KEY, $ANTHROPIC_API_KEY`. devdocs never prompts for a key, in a terminal or not, and 4.0.0 ships no shared demo key.

Prefer an environment variable or a CI secret. A key in `devdocs.toml` works, but keep that file out of version control, and a key on the command line can end up in your shell history.

### The key is never printed

- devdocs never writes the key to its output, its `--json`, the manifest, the cache or the run ledger.
- `doctor` reports where the key came from, never the key: `from flag --api-key`, `from env GEMINI_API_KEY`, or `from file /home/me/shop/devdocs.toml`. In `--json` this is the `api_key` object, which [commands.md](commands.md#doctor) describes.
- Every argument error masks a key that the command line names. A key is named when it follows, or is attached with `=` to, `--api-key` in full or abbreviated, or any flag whose name ends in `key`, `keys`, `token`, `tokens`, `secret` or `secrets`, in any letter case and with any dashes, underscores or dots. A shell-style `GEMINI_API_KEY=...` argument is masked too. The key shows as `***`: `devdocs estimate . --api-key KEY` prints `devdocs: error: unrecognized arguments: --api-key ***`.
- A project path that can only be a key gone astray, as in `devdocs generate -- --api-key=...`, is refused with exit 2 and the key masked: `devdocs: error: argument path: expected a directory, got --api-key=***`.
- The notice for a v3 command line masks the key the same way.
- A key that nothing names, such as a bare argument, looks like any other argument and is shown like one.
- With providers `gemini` and `anthropic`, devdocs' own HTTP client sends the key in a request header (`x-goog-api-key` for Gemini, `x-api-key` for Anthropic), never in a URL. A key holding a line break, any other control character but tab, or a character outside Latin-1 is refused before any connection, without being quoted: `request header 'x-goog-api-key' has a line break or a character HTTP headers cannot carry`.

A key the provider rejects ends the command with exit 3, and `doctor` reports it with exit 1. The messages are `Gemini rejected the API key: ...` with the hint `check GEMINI_API_KEY; get one at aistudio.google.com`, and `Anthropic rejected the request: ...` with the hint `check ANTHROPIC_API_KEY and the key's workspace access`. [models.md](models.md) explains how to get a key, and [troubleshooting.md](troubleshooting.md) what to do when one fails.

## Validation

devdocs checks the configuration before a command creates anything, and reports the first problem it finds. Each problem exits 2 and prints `devdocs: error: <message>`, with a `  hint:` line when there is one; a usage error from the command line itself prints the usage first, as [Values](#values) shows. `doctor` prints a problem with the configuration as its `!! config` line instead, then exits 2.

The checks run in this order:

1. the command line itself: unknown flags, fixed choices and numbers, as described under [Values](#values);
2. the start of the `devdocs.toml` search, as a project root;
3. the file's values, as they are read;
4. the environment variables;
5. the flags' values;
6. the settings' values and combinations;
7. the final project root, and `out` against it;
8. PDF flags given without PDF output.

In the messages below, `<root>` stands for the absolute project root and `<file>` for the absolute path of `devdocs.toml`.

### Paths

| Message | Hint | Cause |
|---|---|---|
| `path is empty` | `use "." for the current directory, or in devdocs.toml for its own` | An empty or blank `path` argument, or `path` in the file. A blank `DEVDOCS_PATH` counts as unset instead. |
| `out is empty` | `give the docs a directory of their own, such as docs (the default)` | An empty or blank `--out` or `out`. A blank `DEVDOCS_OUT` counts as unset instead. |
| `path must be a string`, `out must be a string` | none | A value in the file that is not a string. |
| `path contains a NUL character`, `out contains a NUL character` | none | A NUL character in the value. |
| `project root <root> does not exist` | none | The project directory is missing, or the current directory was removed. |
| `project root <root> is not a directory` | `pass the directory that holds it, <parent>` | The path names a file. |
| `cannot open project root <root>: <reason>` | none | devdocs cannot list or enter the directory, or the path is a symbolic link loop. |
| `out is the project root, <root>` | `give the docs a directory of their own, such as docs (the default)` | `out` resolves to the project root, as `.`, `./` or `src/..` do. |
| `out <dir> contains the project root, <root>` | `give the docs a directory of their own, such as docs (the default)` | `out` resolves to a directory above the project root, as `..` or `/` do. |

devdocs compares these directories by identity, so another spelling, a symbolic link or another letter case on a case-insensitive disk is caught too.

### The file

| Message | Hint | Cause |
|---|---|---|
| `<file> is not valid TOML: <reason>` | none | A TOML syntax error, a file that is not UTF-8, nesting too deep, or a number too long. |
| `cannot read <file>: <reason>` | none | devdocs may not read the file. |
| `<file>: [devdocs] must be a table` | none | A top-level `devdocs` key that is not a table. |
| `<file>: unknown option '<key>'` | `valid options: api_key, concurrency, context, description, exclude, formats, free_tier, include, max_cost, max_requests, model, name, out, page_size, path, pdf_engine, pdf_split, per_file, provider, quality` | A misspelled key, or a table other than `[devdocs]` in a file without one. |
| `<key> must be a list or comma-separated string` | none | `include`, `exclude` or `formats` of another type. |
| `<key> must be true or false` | none | A boolean setting with another value, such as `"yes"`. |
| `<key> must be an integer` | none | `max_requests` or `concurrency` that is not an integer, such as `2.0`. |
| `<key> must be a number` | none | `max_cost` that is not a number, such as `"1"`. |

### Environment values

| Message | Cause |
|---|---|
| `DEVDOCS_<NAME> must be true or false` | A boolean variable with another value, such as `DEVDOCS_FREE_TIER=maybe`. |
| `DEVDOCS_<NAME> must be a number` | A numeric variable that does not parse, such as `DEVDOCS_MAX_COST=lots` or `DEVDOCS_MAX_REQUESTS=1e3`. |

### Values

The flags with fixed choices (`--provider`, `--quality`, `--context`, `--pdf-engine`) and the numeric flags are checked by the command line itself. It prints the command's usage and, for example, `devdocs estimate: error: argument --quality: invalid choice: 'best' (choose from 'cheap', 'balanced', 'high')`; the exact wording depends on the Python version. The same values from the file or the environment, and the values of the other flags, give these messages:

| Message | Hint |
|---|---|
| `unknown provider '<value>'` | `choose one of: anthropic, anthropic-sdk, gemini` |
| `unknown quality '<value>'` | `choose one of: balanced, cheap, high` |
| `unknown context '<value>'` | `choose one of: off, semantic, standard` |
| `no output format configured` | `choose one or more of: markdown, html, pdf` |
| `unknown format(s): <names>` | `choose from: html, markdown, pdf` |
| `unknown PDF engine '<value>'` | `choose one of: builtin, chrome, weasyprint` |
| `unknown page size '<value>'` | `choose one of: A4, Letter` |
| `max_cost must be a number of dollars, not nan` (or `inf`) | `use 0 for no cost ceiling` |
| `max_cost cannot be negative` | none |
| `max_requests cannot be negative` | `use 0 for no request limit` |
| `concurrency must be at least 1` | none |

So `--format HTML` gives `unknown format(s): HTML`, `--page-size a3` gives `unknown page size 'a3'`, and `--max-cost -1` gives `max_cost cannot be negative`.

### Combinations

| Message | Hint | Cause |
|---|---|---|
| `semantic context needs Gemini embeddings` | `use context = "standard" with Anthropic, or provider = "gemini"` | `context = "semantic"` with an Anthropic provider. |
| `the chrome PDF engine prints one book and cannot split it` (or `weasyprint`) | `split with the builtin engine (--pdf-engine builtin), or print one book (--no-split, or pdf_split = false)` | `pdf_split = true` with an external engine and PDF output. |
| `--split applies only to PDF output, which is not among the formats: markdown` | `add --format pdf` | A PDF flag on the command line without PDF output. With several flags it reads `--page-size, --split apply only to PDF output, ...`. |
| `the weasyprint PDF engine is not installed` (or `chrome`) | ``install it or use --pdf-engine builtin; `devdocs doctor` lists the engines it finds`` | PDF output with an external engine that devdocs cannot find. This is checked when the command builds its renderers, after the other checks; in `generate`, after `.devdocs/devdocs.db` is opened. |

A missing API key is refused after the configuration is checked and before the PDF engine is; see [The API key](#the-api-key). [troubleshooting.md](troubleshooting.md) covers the errors that come later in a run.
