# Installing devdocs

devdocs is one command, `devdocs`, written in Python with no dependencies outside the standard library. This page covers every way to install it, check it, upgrade it and remove it.

## Requirements

- **Python 3.11 or newer.** CI tests 3.11, 3.12, 3.13, 3.14 and 3.15 on Linux and macOS. On Windows, CI runs 3.11 and 3.15, and the Windows installer script, in jobs marked experimental and allowed to fail.
  - On Python 3.11, `devdocs doctor` reports `2/4 constructs`: files that use Python 3.12 syntax (PEP 695 generics, PEP 701 f-strings) get fewer facts, or none.
- **Nothing else for the core.** devdocs reaches the providers over Python's own HTTP client and keeps its state in SQLite.
- **An API key**, for `generate` and `models`, and for the provider check of `doctor`. `estimate`, `export`, `init` and `cache` never need one. See [models.md](models.md) for the providers and keys.
- **Optional:** Chrome, Chromium, Edge or WeasyPrint for PDFs with all of Unicode, and git for ranking files by recent history. See [output.md](output.md).

## Choose a method

| Method | What you need | Notes |
|---|---|---|
| [pipx](#pipx) | pipx, and Python 3.11+ | The usual way to install a Python command line. |
| [uv](#uv) | uv | uv fetches a suitable Python if you have none. |
| [pip in a virtual environment](#pip-in-a-virtual-environment) | Python 3.11+ | When you manage environments yourself. |
| [Installer scripts](#installer-scripts) | curl (macOS, Linux), or PowerShell (Windows) | Picks uv, pipx or pip for you, then runs a check. |
| [npm](#npm) | Node.js and Python 3.11+ | Runs the bundled Python source with your Python. |
| [Docker](#docker) | Docker | No Python on the host. |
| [From source](#from-source) | git and Python 3.11+ | Any commit of the repository. |

The PyPI distribution is named `devDocs`; pip, pipx and uv treat the name case-insensitively, and the command is always `devdocs` in lower case. The commands below ask for `devDocs>=4`: on a Python older than 3.11, pip would otherwise fall back to an older release, such as 3.0.0 from the abandoned 3.x line, instead of failing.

## pipx

```sh
pipx install "devDocs>=4"
```

pipx uses its default Python, usually the one it runs on. If that is older than 3.11, name a newer one:

```sh
pipx install --python python3.13 "devDocs>=4"
```

Check it: `devdocs doctor --offline` (see [Checking the install](#checking-the-install)).

## uv

```sh
uv tool install --python 3.13 "devDocs>=4"
```

When Python 3.13 is missing, uv downloads one of its own. If the `devdocs` command is not found afterwards, run `uv tool update-shell` and open a new terminal.

Check it: `devdocs doctor --offline`.

## pip in a virtual environment

A virtual environment runs the Python that creates it, and devdocs needs 3.11 or newer. Run `python3 --version` first. If it prints an older version, use a newer interpreter in the first command below, such as `python3.13 -m venv .venv`.

On macOS and Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install "devDocs>=4"
```

On Windows, in PowerShell:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install "devDocs>=4"
```

If PowerShell refuses to run `Activate.ps1` because running scripts is disabled, allow local scripts for your account, as the Python documentation describes, and activate again:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

`devdocs` is then on PATH while the environment is active. devdocs leaves `.venv/` and `venv/` directories out of its scan, so the environment can live inside the project.

Check it: `devdocs doctor --offline`.

## Installer scripts

On macOS and Linux:

```sh
curl -LsSf https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.sh | sh
```

On Windows, in Windows PowerShell 5.1 or PowerShell 7:

```powershell
irm https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.ps1 | iex
```

Each script, in order:

1. Looks for uv on PATH and in the folders where Astral's installer puts it, and installs it with Astral's installer (`https://astral.sh/uv/install.sh`, or `install.ps1` on Windows) if there is none. It uses uv if it has it, else pipx if pipx is on PATH, else `pip install --user`.
2. Installs devdocs from PyPI, or from `DEVDOCS_SOURCE`: `uv tool install --python 3.13 …`, `pipx install --force …` or `pip install --user --upgrade …`. With pipx and pip it looks for a Python 3.11 or newer that is not a virtual environment.
3. Runs `devdocs --version`, then `devdocs doctor --offline` in the current directory, and prints the report.
4. Says how to put the command on PATH if it is not there yet, and warns if another `devdocs` comes first on PATH.
5. Prints a quick start: where to get a key, how to set it, then `devdocs estimate` and `devdocs generate`.

Its own messages start with `devdocs-install:`. Running it again upgrades or reinstalls. It never sets an API key and never edits a shell profile itself; Astral's uv installer, when it runs, may add uv's directory to your profile or user Path.

### Settings

The scripts take their settings from the environment. `install.sh` accepts only `-h` and `--help`; `install.ps1` takes no arguments.

| Variable | Meaning | Default |
|---|---|---|
| `DEVDOCS_VERSION` | The release to install, such as `4.0.0`. A leading `v` is dropped. Ignored, with a warning, when `DEVDOCS_SOURCE` is set. | the latest release (`devdocs>=4`) |
| `DEVDOCS_SOURCE` | Install from this instead of PyPI: a URL (anything containing `://`), or an existing path to a checkout, a wheel or an sdist. Credentials in a URL are masked in messages, and the pipx route refuses such a URL. | PyPI |
| `DEVDOCS_PYTHON_VERSION` | The Python to install devdocs with: 3.11 or newer. uv installs with this version, and downloads it if it is missing; pipx and pip look for it first (on Windows, only when you set it). | `3.13` |
| `DEVDOCS_NO_UV_INSTALL` | Exactly `1` means never install uv. A uv already present is still used; without one, pipx, then pip. | unset |

The scripts also read a few other variables, at install time only: `UV_INSTALL_DIR`, `XDG_BIN_HOME` and `XDG_DATA_HOME` when they look for uv, `PIPX_HOME` and `PIPX_BIN_DIR` when pipx does not report its folders, and `UV_NO_MODIFY_PATH` and `INSTALLER_NO_MODIFY_PATH` to word their PATH advice. `install.sh` downloads Astral's installer into `TMPDIR`, or `/tmp` when that is unset or empty.

Through a pipe, put the setting on `sh`, or set it in the session first on Windows:

```sh
curl -LsSf https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.sh | DEVDOCS_VERSION=4.0.0 sh
```

```powershell
$env:DEVDOCS_VERSION = '4.0.0'
irm https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.ps1 | iex
```

To read the help of `install.sh` through a pipe: `curl -LsSf https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.sh | sh -s -- --help`.

### Exit status

The scripts exit 0 when devdocs is installed and runs, whatever `doctor` reports: before you set a key, doctor marks the key `!!` and the script says so without failing. They exit 1 on an error, with a `devdocs-install: error:` line on stderr. `install.sh` exits 2 for an unknown argument. Piped into `iex`, `install.ps1` does not close your PowerShell session on an error: it sets `$LASTEXITCODE` to 1 and throws the error instead.

Check it: the script has already run `devdocs doctor --offline`. Open a new terminal if it asked you to, then run `devdocs --version`.

## npm

```sh
npm install -g @bgantavya/devdocs
```

Or run it without installing:

```sh
npx @bgantavya/devdocs estimate
```

The package `@bgantavya/devdocs` bundles the devdocs Python source and runs it with your own Python as `python -P -m devdocs`. It has no npm dependencies, installs no Python packages and bundles no interpreter. It needs:

- Node.js 20.17 or a later 20.x, or 22.9 or newer;
- Python 3.11 or newer.

**How Python is found.** If `DEVDOCS_PYTHON` is set and not empty, that command or path is the only one tried, with no fallback. Otherwise the first of these that reports Python 3.11 or newer is used:

- macOS and Linux: `python3.15`, `python3.14`, `python3.13`, `python3.12`, `python3.11`, `python3`, `python`;
- Windows: `py -3.15` down to `py -3.11`, then `py -3`, then `python3.15` down to `python3.11`, then `python`, then `python3`. A bare name is looked up only in the absolute folders on PATH, never in the current directory.

A Python newer than 3.15 is found through `python3`, `python`, `py -3` or `DEVDOCS_PYTHON`. [configuration.md](configuration.md#the-npm-launcher) describes `DEVDOCS_PYTHON` and the other variables the launcher reads or sets each time it runs.

**The install-time check.** The package's install script looks for Python the same way, `DEVDOCS_PYTHON` included, and fails the install when it finds no Python 3.11 or newer. It reads one variable of its own:

| Variable | Meaning | Default |
|---|---|---|
| `DEVDOCS_SKIP_PYTHON_CHECK` | Exactly `1` skips the check, for example in an image that adds Python later. | unset |

When install scripts do not run, as with `npm install --ignore-scripts`, nothing is checked at install time. Either way, the `devdocs` command looks for Python each time it starts, and prints the same message when it finds none.

**Exit codes and signals.** The launcher passes devdocs' exit code through, and adds its own for when it cannot start devdocs and for when a signal ends devdocs; [commands.md](commands.md#exit-codes) lists them with the rest, and [Containers and the npm launcher](commands.md#containers-and-the-npm-launcher) says which signals it passes on.

The npm package does not install the optional `anthropic` extra: provider `anthropic-sdk` works only if the Python it runs can already import `anthropic`. Provider `anthropic` needs nothing extra.

Check it: `devdocs doctor --offline`.

## Docker

The image is `ghcr.io/bgantavya/devdocs`, for `linux/amd64` and `linux/arm64`. Each release is tagged with its version (`4.0.0`), its minor line (`4.0`) and `latest`. There is no major-only tag such as `4`.

Run it on the current directory, mounted at `/work`:

```sh
docker run --rm -it -u "$(id -u):$(id -g)" -v "$PWD:/work" ghcr.io/bgantavya/devdocs:4.0.0 estimate
docker run --rm -it -u "$(id -u):$(id -g)" -v "$PWD:/work" -e GEMINI_API_KEY ghcr.io/bgantavya/devdocs:4.0.0 generate
```

- `-u "$(id -u):$(id -g)"` runs devdocs as you, so `docs/` and `.devdocs/` belong to you. Without it the image runs as uid 10001.
- `-e GEMINI_API_KEY` with no value passes the variable from your shell, so the key is not on the command line. Pass any `DEVDOCS_*` setting the same way.
- With no command, the image prints `devdocs --help`.
- Only the mounted project outlives the container. Keep `--out`, `DEVDOCS_OUT` and `DEVDOCS_PATH` relative, inside it. Most paths outside `/work`, `~` included, cannot be written there, so devdocs refuses them; what it writes under `/tmp` is lost when the container exits.

The image is devdocs installed on `python:3.13-slim`, with nothing else added:

- no external PDF engine, so only `--pdf-engine builtin` works;
- no `anthropic` extra, so provider `anthropic-sdk` is refused; use provider `anthropic`, which reaches the same API;
- no git, so files are ranked without their recent history.

To stop a run gracefully, allow `docker stop` 150 s instead of Docker's default 10 s: `docker stop -t 150`, or `docker run --stop-timeout 150`. [Containers and the npm launcher](commands.md#containers-and-the-npm-launcher) explains the stop.

Check it:

```sh
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/work" -e GEMINI_API_KEY ghcr.io/bgantavya/devdocs:4.0.0 doctor --offline
```

### Compose

The repository's `compose.yaml` runs the image's `latest` tag against the current directory, with a read-only root file system, a tmpfs at `/tmp`, every capability dropped and `no-new-privileges`. From a checkout:

```sh
export DEVDOCS_UID=$(id -u) DEVDOCS_GID=$(id -g)
docker compose -f path/to/devDocs/compose.yaml run --rm devdocs estimate
```

- `DEVDOCS_UID` and `DEVDOCS_GID` (default `1000`) set the user, so the files belong to you. [configuration.md](configuration.md#the-container-image-and-composeyaml) lists what compose reads and passes on.
- The API key variables and every `DEVDOCS_*` setting pass through from your shell; one you leave unset stays unset, so the project's `devdocs.toml` still applies.
- The default command is `generate`, a paid run. Name the command you want.
- The image is pulled from ghcr.io, or built from the checkout if that fails.
- Stopping sends SIGINT and allows 150 s before SIGKILL.
- PowerShell and cmd do not export `PWD`, so there the mounted directory is the one that holds `compose.yaml`.

## From source

On macOS and Linux, into a virtual environment in the checkout, with a `python3` that is 3.11 or newer:

```sh
git clone --branch v4 https://github.com/bgantavya/devDocs.git
cd devDocs
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

The other methods can install the checkout too: `uv tool install --python 3.13 .`, `pipx install .`, or `DEVDOCS_SOURCE=. sh scripts/install.sh`. On Windows, run the installer from the checkout in PowerShell:

```powershell
$env:DEVDOCS_SOURCE = '.'
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

From the root of a checkout, `python3 -m devdocs` runs devdocs without installing it, when `python3` is 3.11 or newer. For a development setup, see [CONTRIBUTING.md](../CONTRIBUTING.md).

Check it: `devdocs doctor --offline`.

## The anthropic extra

devdocs reaches Claude over its own HTTP client with provider `anthropic`. Provider `anthropic-sdk` uses Anthropic's Python SDK instead, which you install as the `anthropic` extra:

```sh
pipx install "devDocs[anthropic]>=4"
uv tool install --python 3.13 "devDocs[anthropic]>=4"
python -m pip install "devDocs[anthropic]>=4"
```

The installer scripts, the npm package and the image do not install it. `devdocs doctor --provider anthropic-sdk --offline` checks that the SDK imports.

## Checking the install

```sh
devdocs --version
devdocs doctor --offline
```

`devdocs --version` prints `devdocs <version>`. `doctor --offline` checks Python, SQLite, TLS and the CA certificates, lists the PDF engines it finds, then reads the configuration of the current directory and looks for an API key. It sends nothing. Each check prints a line that starts with `ok` or `!!`, and indented lines under it add a detail or a hint. It exits 0 when nothing blocks a run, 1 when something does (before you set a key, that is the key), and 2 when the configuration is wrong.

Once a key is set, `devdocs doctor` without `--offline` also asks the provider which models the key can use. That request costs nothing. [troubleshooting.md](troubleshooting.md) explains each check and what to do when one fails.

## Upgrading and removing

| Installed with | Upgrade | Remove |
|---|---|---|
| pipx | `pipx upgrade devdocs` | `pipx uninstall devdocs` |
| uv | `uv tool upgrade devdocs` | `uv tool uninstall devdocs` |
| pip in a virtual environment | `python -m pip install --upgrade "devDocs>=4"` | `python -m pip uninstall devDocs`, or delete the environment |
| an installer script | run it again | as for the tool that its `installing devdocs from ...` line names: uv or pipx as above, or for `pip --user`, `<python> -m pip uninstall devDocs` with the Python that line names |
| npm | `npm install -g @bgantavya/devdocs@latest` | `npm uninstall -g @bgantavya/devdocs` |
| Docker | `docker pull ghcr.io/bgantavya/devdocs:latest`, or use a newer tag | `docker image rm ghcr.io/bgantavya/devdocs:4.0.0` |

To install one release, name it: `"devDocs==4.0.0"` for pipx, uv and pip, `DEVDOCS_VERSION=4.0.0` for the installer scripts, `@bgantavya/devdocs@4.0.0` for npm, and the `4.0.0` tag for Docker.

A new release can change the prompts devdocs sends. A changed prompt misses the response cache, so the first run after an upgrade may buy some answers again; `devdocs estimate` shows how many.

Removing devdocs leaves each project's files alone: `devdocs.toml`, the `.devdocs/` state directory (the response cache and the run ledger) and the generated documentation. Delete them yourself if you no longer want them.

### Coming from 3.x

devdocs 3.x installed a command named `devDocs`. On macOS and Windows, whose file systems ignore case by default, it can shadow the new `devdocs`. Remove it first, with the pip that installed it:

```sh
python3 -m pip uninstall devDocs
```

If `devdocs --version` does not print a 4.x version, another install comes first on PATH. [Upgrade step by step](migration.md#upgrade-step-by-step) shows how to find which one runs, and [migration.md](migration.md) covers the rest of the move from 3.x.
