# Contributing to devdocs

Thank you for helping. This page covers setting up a checkout, the checks every change must pass, the project's rules, commit messages, pull requests and, for maintainers, releases. [docs/architecture.md](docs/architecture.md) explains how the code fits together, and is worth reading before a large change.

- Report a bug or ask for a feature with the [issue forms](https://github.com/bgantavya/devDocs/issues/new/choose).
- Report a vulnerability privately, as [SECURITY.md](SECURITY.md) describes, never in a public issue.
- Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need:

- git;
- Python 3.11 or newer;
- [uv](https://docs.astral.sh/uv/), which installs the locked development tools;
- GNU make 3.81 or newer, for the Makefile targets;
- Node.js, only for the npm wrapper's tests and `make npm`: CI uses Node 22, and the package supports 20.17 or a later 20.x release, or 22.9 and later;
- Docker, only to build the container image.

Clone the repository, then create the virtual environment:

```sh
git clone --branch v4 https://github.com/bgantavya/devDocs.git
cd devDocs
make dev
```

`make dev` runs `uv sync --locked --extra dev`: it creates `.venv` with devdocs installed from the checkout and the development tools at the versions `uv.lock` pins. `uv run devdocs --help` then runs the devdocs of your checkout. The targets that run the tests, ruff, mypy, coverage, bandit or pip-audit go through `uv run --locked --extra dev`, so you get those versions too. Override `UV`, `PYTHON` or `IMAGE` on the command line, as in `make test UV=/path/to/uv`. Only `make docker` uses the other two: `PYTHON` (default `python3`) to read the version, and `IMAGE` (default `devdocs:dev`) as the image's name.

| Target | What it does |
|---|---|
| `make help` | Lists the targets. It is the default. |
| `make dev` | Creates `.venv` with the package and the locked dev tools. |
| `make test` | Runs the unit tests. |
| `make lint` | Lints the package, the tests and the scripts with ruff. |
| `make typecheck` | Type-checks the package and the release script with `mypy --strict`. |
| `make coverage` | Runs the tests under coverage, and fails below the configured floor. |
| `make security` | Runs bandit on the package and pip-audit on every locked extra. pip-audit needs the network. |
| `make check` | Runs `lint`, `typecheck` and `test`. |
| `make build` | Builds the sdist and the wheel into `dist/`. |
| `make npm` | Packs the npm wrapper into `npm/*.tgz`; its prepack step bundles the package. |
| `make docker` | Builds the container image `devdocs:dev` with version labels. |
| `make clean` | Removes build products and caches. |

The test suite needs Python 3.11 or newer and nothing else installed, which is how CI runs it. Run it from the repository root, with any such interpreter as `python`; `uv run python` uses the one in `.venv`. The four commands run the whole suite, one module, one test, and the tests whose names contain a pattern:

```sh
python -m unittest discover -s tests
python -m unittest tests.test_scan
python -m unittest tests.test_generate.TestRun.test_full_run_writes_every_document
python -m unittest discover -s tests -k test_full_run
```

The root matters: the test modules import shared helpers from each other, as `tests.test_generate` and so on. The npm wrapper has its own tests:

```sh
cd npm
npm test
```

In the checkout, devdocs reads the repository's own `devdocs.toml`: `devdocs generate` with no path, run from any directory of the checkout, documents the repository into `site/` and keeps its state in `.devdocs/`, both of which git ignores. `docs/` holds the hand-written documentation, and the run leaves it alone.

## The gates

Every pull request, and every push to a branch other than `v4` and `main`, runs `.github/workflows/ci.yml`. A push to `v4` or `main` runs it through the Release workflow, before anything is published. A change must pass all of these:

| CI job | What it runs |
|---|---|
| Lint | `uv run ruff check devdocs tests scripts`, on Python 3.13. |
| Types | `uv run mypy --strict devdocs scripts/release.py`, on Python 3.13. |
| Test | `python -m unittest discover -s tests`, with nothing installed, on Ubuntu and macOS with Python 3.11, 3.12, 3.13, 3.14 and 3.15. Windows runs Python 3.11 and 3.15 as experimental jobs, which may fail without failing CI. |
| Coverage | `uv run coverage run -m unittest discover -s tests`, then `uv run coverage report`, which fails below the floor in `pyproject.toml`: `fail_under = 85`, with branch coverage on. |
| Security | gitleaks 8.30.1 over the history of `HEAD`, with `.gitleaks.toml`; `uv run bandit -r devdocs -ll`; pip-audit over `uv export --locked --all-extras`, so the optional `anthropic` SDK is audited too. |
| Commit subjects | `python scripts/release.py lint-commits "origin/${BASE_REF}..HEAD" "^origin/${DEFAULT_BRANCH}"`, for pull requests only: the commits a pull request adds, less those the default branch already has. |
| Package | `uv build`; a check that the wheel holds every package-data file and declares no runtime dependency; `python scripts/release.py check`; then, on Node 22, `npm test`, `npm pack --dry-run` and `node npm/bin/devdocs.js --version`. |
| Container | Builds the image, runs `--version`, then `init` and `estimate` on a mounted project as uid 1001 with a read-only root, and checks that `devdocs.toml` belongs to uid 1001 and that no `.devdocs/` was made. |
| Installers | `scripts/install.sh` on Ubuntu and macOS, and `scripts/install.ps1` on Windows as an experimental job, installing from the checkout, then `devdocs --version`. |

The locked tools are ruff 0.17.3, mypy 2.4.1, bandit 1.9.4, pip-audit 2.10.1 and coverage 7.17.0. Another version of ruff or mypy can report differently, so check with `make lint` and `make typecheck` rather than a tool installed elsewhere. Before you push, run:

```sh
make check
make coverage
cd npm && npm test
```

gitleaks has no Makefile target. If you have gitleaks 8.30.1, this is the scan CI makes:

```sh
gitleaks git --config .gitleaks.toml --log-opts=HEAD --redact --no-banner --verbose .
```

## Project rules

- **No runtime dependencies.** `dependencies = []` in `pyproject.toml` stays empty, and CI fails a wheel that declares one. devdocs is built on the standard library alone; the one runtime extra, `anthropic`, backs a provider that has a standard-library alternative. Development tools belong in the `dev` extra.
- **Python 3.11 or newer.** CI tests 3.11 to 3.15. Write for 3.11, the oldest supported version: syntax from later versions, such as PEP 695 type parameters, does not parse there. ruff targets `py311` and mypy checks as Python 3.11.
- **Tests use no network and no model API.** Never call a provider, and never put a real API key in a test, a fixture or an example; gitleaks scans the whole history. Each outside dependency has a fake:
  - the model: `TaggedModel` in `tests/test_generate.py`, which answers every prompt kind in the tagged reply format, and `Scripted` in `tests/test_client.py`, which scripts each model's answer or error;
  - HTTP: the `Recorded` transport in `tests/test_providers.py`, which replays canned responses, and a real server on 127.0.0.1 in `tests/test_http.py`, with a self-signed certificate that is test data only;
  - the Anthropic SDK: a stand-in `anthropic` module in `tests/test_anthropic_sdk.py`;
  - git: `FakeGit` in `tests/test_history.py`; time: `FakeClock` in `tests/test_budget.py`;
  - the environment: pass `env=` to `load_config` and `resolve_api_key`, and wrap command-line tests in `clean_env()` from `tests/test_cli.py`, rather than changing `os.environ`.
- **The standard library's `unittest`.** No pytest. Build projects in temporary directories, type test methods `-> None`, and skip with a reason where a platform lacks a feature, such as signals on Windows or POSIX permissions.
- **Comments only where needed, and telegraphic.** A comment says what a line cannot, in a few words: `# a parser bug must not end the run`. The why of a design belongs in the module, class or function docstring, where the existing modules keep it.
- **Typing.** `mypy --strict` covers `devdocs` and `scripts/release.py`, so every function needs annotations. The package ships `py.typed`. The tests are linted by ruff but not type-checked.
- **Exit codes are a public contract.** Never change or reuse one; 8 stays unused. Raise an existing `DevDocsError` subclass where one fits. A failure that needs a code of its own gets a new subclass in `devdocs/errors.py` with an unused code, listed in the module docstring and added to `ALL` and the expected codes in `tests/test_errors.py`.
- **Settings.** `load_config` reads every `Config` field, so a new field is at once a `devdocs.toml` key and a `DEVDOCS_<NAME>` variable; a flag for it also needs an entry in `_config` in `devdocs/cli.py`. Pass the variable on in the `environment` list of `compose.yaml`, which `npm/test/repository.test.js` checks, and document the setting in [docs/configuration.md](docs/configuration.md).
- **Never show a key.** devdocs reports where a key came from, never the key. Keep it that way in every message, log line and `--json` field.
- **Documentation.** A change users can see updates the page that is the home of that fact: flags and exit codes in [docs/commands.md](docs/commands.md), settings and environment variables in [docs/configuration.md](docs/configuration.md), install methods in [docs/install.md](docs/install.md).

## Commit messages

Every commit subject is a [Conventional Commit](https://www.conventionalcommits.org/en/v1.0.0/):

```text
type(scope)!: description
```

- `type` is one of `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`, `style` and `revert`, in lowercase.
- `(scope)` is optional, with no spaces or parentheses inside, such as `(cli)`, `(pdf)` or `(legacy)`.
- `!` is optional and marks a breaking change. So does a body line that starts `BREAKING CHANGE:` or `BREAKING-CHANGE:`.
- The description follows `: ` and starts with a non-space character.

Keep commits small and their messages short. The subject says what the change does for its reader, as in `fix(legacy): the v3 notice masks the API key`; a short body says why, when that is not obvious. CI's commit job lints every commit a pull request adds, merge commits aside. To run the same check on your branch before you push:

```sh
python scripts/release.py lint-commits origin/v4..HEAD
```

Commit types choose the next release:

| Commit | Release it asks for |
|---|---|
| `feat` | minor |
| `fix`, `perf` | patch |
| `!` after the type or scope, or a breaking footer | major |
| `chore(release)` | none, ever |
| any other type | none |

Subjects become the release notes: `scripts/release.py notes` lists breaking changes, features, fixes and performance changes from them, so write them for users.

While `__version__` in `devdocs/__init__.py` is a pre-release (`.devN`, `aN`, `bN` or `rcN`), every release is held, and work can merge without shipping. With `__version__` at `4.0.0.dev0`, `next` prints no version, and says why on standard error:

```sh
python scripts/release.py next
```

```text
warning: 4.0.0.dev0 is a pre-release, so nothing ships until `bump 4.0.0` makes it final
```

## Pull requests

1. Open the pull request against `v4`, the branch releases are made from.
2. Give it a conventional-commit title, and fill in the template: what changes and why, and how you tested it.
3. Add tests for the change. A fix comes with a test that fails without it.
4. Update the documentation where users see the change, and add an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md), in the section that fits (Added, Changed, Deprecated, Removed, Fixed or Security). Mark a breaking change as such, and say what a user must do.
5. Wait for CI. Every job must pass, except the experimental Windows jobs.
6. `.github/CODEOWNERS` assigns every path to the maintainer, so GitHub asks them to review each pull request.

## Releases

This section is for maintainers.

### The version

The version lives in one place, `__version__` in `devdocs/__init__.py`, in PEP 440 form: `X.Y.Z`, optionally followed by `.devN`, `aN`, `bN` or `rcN`. `npm/package.json` carries the same version in its semver spelling (`4.0.0.dev0` is `4.0.0-dev.0`). Releases are the tags `vX.Y.Z` merged into the branch; other tags, such as `devDocs-v3.0.0`, are ignored.

`scripts/release.py` uses the standard library only. Run it from the repository root. It works on the repository that holds it, or on the one `--root PATH` names:

| Command | What it does |
|---|---|
| `python scripts/release.py current` | Prints `__version__`. |
| `python scripts/release.py check` | Exits 1 unless `npm/package.json` matches `__version__`. |
| `python scripts/release.py next [--json]` | Prints the release due now, or nothing when none is. `--json` prints `release`, `version`, `tag`, `previous` and `bump` as one object. |
| `python scripts/release.py bump VERSION` | Writes `VERSION` to `devdocs/__init__.py` and `npm/package.json`, both or neither. |
| `python scripts/release.py lint-commits REV...` | Lists the commits in the revisions, given as `git log` takes them, whose subject is not conventional, and exits 1 if there are any. |
| `python scripts/release.py notes RANGE` | Prints Markdown release notes for `RANGE`. |

It exits 0 on success, 1 for a failed check or a git or file error, and 2 for bad usage, including an invalid version for `bump`.

`next` decides what ships:

- a pre-release `__version__` holds every release (`bump` is `held`);
- a final `__version__` above the newest tag ships as it is (`pending`);
- otherwise the newest tag is bumped by the highest kind among the commits since it (`bump` is `major`, `minor` or `patch`), or nothing ships (`none`). A version at or below the newest tag is never proposed.

### Shipping 4.0.0

1. Run `python scripts/release.py bump 4.0.0`, and rename `[Unreleased]` in `CHANGELOG.md` to `[4.0.0]` with the date, adding a new empty `[Unreleased]` above it and updating the link references at the bottom.
2. Commit both with a `chore(release): v4.0.0` subject, and push to `v4`.
3. The Release workflow finds `4.0.0` final and untagged, tags that commit `v4.0.0`, and publishes it.

Until `v4.0.0` exists, the newest `vX.Y.Z` tag merged into `v4` is an old one from the 1.x or 2.x line, which `next --json` reports as `"previous"`, so the notes of 4.0.0 are made from that tag to `v4.0.0`. The 2.x and 3.x commits in that range are not conventional and are left out, so the notes list the rewrite's changes. To start them at 3.0.0, tag `v3.0.0` on the commit that `devDocs-v3.0.0` names before the release.

### What the Release workflow does

`.github/workflows/publish.yml` runs on every push to `v4` or `main`, and when started by hand, one run at a time. Only a run on `v4` or `main` can tag and publish:

1. **CI** runs `ci.yml`. Its commit-subject job runs for pull requests only, so it is skipped here.
2. **Plan** runs `python scripts/release.py next --json`. When no release is due, the run ends here.
3. **Tag**, as `github-actions[bot]`, runs `bump` with the planned version, commits `chore(release): vX.Y.Z` if that changed anything, makes an annotated tag `vX.Y.Z`, and pushes the commit and the tag together. Every later job that needs the source checks out that tag.
4. **Build** makes the sdist and the wheel once, with `SOURCE_DATE_EPOCH` set from the tagged commit, a CycloneDX SBOM and a build-provenance attestation.
5. **PyPI** publishes the sdist and the wheel through trusted publishing, with PEP 740 attestations.
6. **npm** publishes `@bgantavya/devdocs` from the tag with npm 11.21.1, `npm publish --provenance --access public`.
7. **GHCR** builds `ghcr.io/bgantavya/devdocs` for `linux/amd64` and `linux/arm64`, tagged `X.Y.Z`, `X.Y` and `latest`, with provenance and an SBOM, and attests it.
8. **GitHub release** writes the notes with `release.py notes PREVIOUS..vX.Y.Z` and creates the release with the sdist, the wheel and the SBOM. It runs only once PyPI, npm and GHCR have all succeeded.

Pushing a tag yourself triggers nothing, and `next` then counts that version as released, so the workflow never publishes it: leave release tags to the Tag job. A pre-release is never published anywhere.

### What the registries need

| Registry | Package | Credentials |
|---|---|---|
| PyPI | `devDocs` | A trusted publisher for this repository and the workflow file `publish.yml`, with no environment. No token. |
| npm | `@bgantavya/devdocs` | The `NPM_TOKEN` repository secret, for the first publish only; after that, a trusted publisher set up on npmjs.com for this repository and `publish.yml`, which needs npm 11.5.1 or later and no token. |
| GHCR | `ghcr.io/bgantavya/devdocs` | The workflow's own `GITHUB_TOKEN`, with `packages: write`. |
| GitHub Releases | the sdist, the wheel and the SBOM | The workflow's own token, with `contents: write`. |

If branch protection on `v4` or `main` blocks the bot's push, allow `github-actions` to bypass it. On a personal account, a newly created container package may start private: after the first release, check that `ghcr.io/bgantavya/devdocs` is public.

Dependabot (`.github/dependabot.yml`) proposes weekly grouped updates for the pinned actions, `uv.lock`, the npm wrapper and the base image, only to versions at least a week old. Their subjects start `ci(deps):`, `build(deps):` or `build(deps-dev):`, types that never trigger a release. It reads its configuration from the default branch only, so it starts once `v4` is the repository's default branch. The gitleaks version and checksum in `ci.yml`, and the binfmt image in `publish.yml`, are pinned by hand: bump them yourself.
