# Running devdocs in CI

devdocs runs unattended as it runs at a terminal: it never prompts, takes its key from the environment, stops at a budget, prints JSON on request and reports what happened in its exit status. This page shows how to run it in CI so that each run pays only for what changed: [Keeping .devdocs/ between runs](#keeping-devdocs-between-runs) explains how.

## What a CI run needs

- devdocs, installed by any method in [install.md](install.md), or the container image.
- An API key, from the CI system's secrets.
- The project's `devdocs.toml`, committed, so CI and your own runs use the same settings. See [configuration.md](configuration.md).
- The `.devdocs/` directory from the previous run, restored from a cache.

## Keys

Give devdocs its key as an environment variable, filled from a secret:

- Gemini (the default provider): `GEMINI_API_KEY`, or `GOOGLE_API_KEY`;
- Claude: `ANTHROPIC_API_KEY`, with `DEVDOCS_PROVIDER=anthropic` or `provider = "anthropic"` in `devdocs.toml`;
- either provider: `DEVDOCS_API_KEY`, which devdocs tries first.

Never pass `--api-key` in CI: a command line can end up in logs and process listings. devdocs does not store the key, and `devdocs doctor` reports only where it found one. [configuration.md](configuration.md#the-api-key) gives the full lookup order.

Without a key, `generate` exits 2 with `no API key for provider 'gemini'` and creates nothing. This is what happens where secrets are not available, as on pull requests from forks. `estimate` needs no key, so it runs there.

## Budget

Every run is capped, even with no setting: `max_cost` defaults to $0.50. Set the limits in `devdocs.toml` (`max_cost`, `max_requests`) or in the job's environment (`DEVDOCS_MAX_COST`, `DEVDOCS_MAX_REQUESTS`), so that `estimate` and `generate` see the same ones. `max_requests` counts every request, retries included, and has no limit by default. `0` means no limit for either, not zero spend. [configuration.md](configuration.md#settings) lists both settings.

- The limit is per run. A run that resumes a stopped one may spend up to the limit again, so a scheduled job can spend up to the limit each time it runs.
- devdocs fits its plan to the limits before it sends anything. What does not fit gets a page from facts alone, and `generate` still exits 0. It prints no line about the cut; those pages only count among the summary's `from facts alone`. `devdocs estimate` prints `N items left as stubs to fit --max-cost $0.5`, and `estimate --json` lists them in `demoted`.
- Exit 7 means a run outgrew its plan, for example through retries, and stopped at a limit partway through.
- With a paid Gemini key, set `DEVDOCS_FREE_TIER=false` (or pass `--paid`). By default devdocs paces requests to Gemini's free-tier limits and uses only the models of the free tier. On the free tier a large first run can stop on the daily quota, exit 10, and finish over the next days; `devdocs estimate` says `free tier: up to N days of daily quota` when it may.

[costs.md](costs.md) explains the budget and how to keep runs cheap.

## Reading the result

`devdocs generate --json` prints one JSON object on stdout when the run ends. The fields a CI job usually reads:

- `stopped`: `""`, or `budget`, `quota` or `interrupted` when the run stopped early;
- `failed`: the items and formats that failed, each as `[kind, key, error]`;
- `documents`, `prose` and `stubs`: how many documents were written, how many of them have prose, and how many come from facts alone;
- `requests`, `cache_hits` and `cost`: what the run sent and spent, in US dollars;
- `resumed`: whether the run resumed one that stopped early.

Progress goes to stderr, and `-q` drops it; warnings and errors stay. An error is never JSON: it is a `devdocs: error:` line on stderr, or `devdocs: internal error:` for a bug, with nothing on stdout. [commands.md](commands.md#generate) describes the whole output of `generate` and every JSON field.

### Exit codes to act on

- **0**: done.
- **7** (budget) and **10** (quota): the run stopped early. Every page was written, those it had no answer for from facts alone, and the next run resumes. Treat these as warnings, and keep the cache. A quota already spent when the run began gives a 10 too: the run buys nothing and writes every page from the cache or from facts alone. Only when the provider refuses even to list its models, for quota or billing, does a 10 come before any page is written.
- **4**: at least one item or output format failed, and `failed` says which; or listing the provider's models failed, on a network failure or a server error. The next run tries again, and answers already received are cached. A 4 whose message is `request header 'x-goog-api-key' has a line break or a character HTTP headers cannot carry` (`'x-api-key'` with provider `anthropic`) is a key with stray characters, refused before any connection: retrying does not help. Set the secret again from a clean copy; [troubleshooting.md](troubleshooting.md#the-key-holds-stray-characters) explains the message.
- **130** and **143**: stopped by SIGINT or SIGTERM. See [Timeouts and cancellation](#timeouts-and-cancellation).
- **Any other code**: an error stopped devdocs, such as a missing or rejected key (2 or 3: check the secret). Fail the job. The error line devdocs printed on stderr says what went wrong, and [commands.md](commands.md#exit-codes) explains every code.

## Keeping .devdocs/ between runs

`.devdocs/` sits in the project root and holds all of devdocs' state:

- the response cache: every answer bought, keyed by the provider, the model and the prompt, which holds the source it documents;
- the run ledger, so a stopped run resumes;
- the vectors of semantic context, when `context = "semantic"`;
- the requests made per model per day, for free-tier pacing.

With it restored, an unchanged project costs nothing to document again, even in a fresh checkout at another path, with no output directory: the run writes every page from the cache. After an edit, a run buys again each answer whose prompt the edit changed: for the edited files, files that gained or lost an importer or a test, and the module summaries and the architecture overview above them when what they sum up has changed. [costs.md](costs.md#what-a-re-run-pays-for) lists every change that buys an answer again. The directory holds no API key.

Cache the whole `.devdocs/` directory, not only `devdocs.db`. A run stopped at once can leave `devdocs.db-wal` and `devdocs.db-shm` beside it, and the answers in them count. Save it after every run, failed and cancelled ones included, since the answers a run bought before it failed are in it. The output directory needs no cache: commit it, upload it, or let the next run write it again from the cache.

Pin the devdocs version you install. A new release can change the prompts, and a changed prompt is a cache miss. A cache saved by a newer release can also have a newer schema, which an older devdocs refuses with exit 9 (`is schema vN, this devdocs understands vM`): upgrade devdocs, or delete `.devdocs/`, which buys every answer again.

devdocs ranks files partly by their git history over the last 90 days. A shallow clone, such as `actions/checkout`'s default of one commit, can rank them differently from a full clone, so fetch the full history if CI and local runs should plan alike.

If the output directory holds a file devdocs did not write at a path it would write, such as a hand-written `docs/README.md`, `generate` refuses before it buys anything (exit 2). Give devdocs a directory of its own with `out` in `devdocs.toml`. A committed output directory is fine: its `index.json` records which files devdocs wrote.

## GitHub Actions

This workflow documents the project on every push to `main` and uploads the result. It takes the key from the `GEMINI_API_KEY` secret and caches `.devdocs/` from run to run.

```yaml
name: Docs

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: docs-${{ github.ref }}
  cancel-in-progress: false

jobs:
  docs:
    runs-on: ubuntu-latest
    timeout-minutes: 40
    env:
      DEVDOCS_MAX_COST: "0.50"
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false

      - uses: actions/setup-python@v7.1.0
        with:
          python-version: "3.13"

      - name: Install devdocs
        run: python -m pip install "devDocs==4.0.0"

      - name: Restore the devdocs state
        uses: actions/cache/restore@v6.1.0
        with:
          path: .devdocs
          key: devdocs-${{ github.run_id }}-${{ github.run_attempt }}
          restore-keys: devdocs-

      - name: Price the run
        run: devdocs estimate

      - name: Generate
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: |
          status=0
          timeout --signal=TERM --kill-after=150s --preserve-status 30m \
            devdocs generate --json > "$RUNNER_TEMP/devdocs.json" || status=$?
          if [ -s "$RUNNER_TEMP/devdocs.json" ]; then
            jq -r '"\(.documents) documents, \(.stubs) from facts alone; \(.requests) model calls, \(.cache_hits) cache hits, $\(.cost)"' \
              "$RUNNER_TEMP/devdocs.json" >> "$GITHUB_STEP_SUMMARY"
          fi
          case "$status" in
            0) ;;
            7|10) echo "::warning::devdocs stopped early (exit $status); the next run resumes" ;;
            *) exit "$status" ;;
          esac

      - name: Save the devdocs state
        if: always()
        uses: actions/cache/save@v6.1.0
        with:
          path: .devdocs
          key: devdocs-${{ github.run_id }}-${{ github.run_attempt }}

      - uses: actions/upload-artifact@v7.0.2
        with:
          name: docs
          path: docs/
```

How it works:

- **Concurrency.** Runs on one branch take turns, so a run starts from the state the one before it saved.
- **The key** is visible to the Generate step only.
- **The cache.** A GitHub cache entry cannot be overwritten, so each run saves under a key of its own, and `restore-keys` restores the newest one. `if: always()` saves it after a failure or a cancellation too. When no entry is left, the next run buys every answer again, up to its limit.
- **Estimate first.** The price appears in the log before anything is bought. It needs no key.
- **The exit status.** The Generate step passes on 7 and 10 as warnings and fails on any other code but 0. Add `4` to the warning case if a partial run should still upload its pages.
- **`timeout`** stops devdocs with SIGTERM after 30 minutes and allows 150 s for the graceful stop before SIGKILL; `--preserve-status` makes the step report devdocs' own 143. Keep `timeout-minutes` above the total, so that devdocs' stop comes first.
- **Versions.** Each action is pinned to a release. Pin them by commit SHA for a stricter guarantee, as this repository's own workflows do.

To use Claude, replace the key line with `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}`, and set the provider where `estimate` sees it too: `provider = "anthropic"` in `devdocs.toml`, or `DEVDOCS_PROVIDER: anthropic` in the job's `env`, beside `DEVDOCS_MAX_COST`.

### Committing the output

To keep the documentation in the repository instead of uploading it, replace the upload step with a commit. The job then needs `contents: write`, and the checkout step must keep its credentials: remove `persist-credentials: false`.

```yaml
      - name: Commit the docs
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add docs
          git diff --cached --quiet || git commit -m "docs: regenerate with devdocs"
          git push
```

A push made with the workflow's `GITHUB_TOKEN` does not start another workflow run, so the commit does not trigger the job again. The committed `docs/index.json` tells the next run which files are its own.

### Estimating pull requests

A pull request can show what it would cost, with no key, from the state the last push saved:

```yaml
name: Docs estimate

on:
  pull_request:

permissions:
  contents: read

jobs:
  estimate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-python@v7.1.0
        with:
          python-version: "3.13"
      - run: python -m pip install "devDocs==4.0.0"
      - uses: actions/cache/restore@v6.1.0
        with:
          path: .devdocs
          key: devdocs-${{ github.run_id }}
          restore-keys: devdocs-
      - run: devdocs estimate --explain
```

`estimate` reads the cache and changes nothing on disk. Its `model calls` line counts the planned calls the cache cannot answer, and the answers `already cached`. The count is a worst case: after an edit it includes the module summary and the overview above the edited file, which a run often finds cached ([costs.md](costs.md#what-at-most-means) explains why). `--explain` lists each file's plan and the reason for it.

## Docker in CI

The image needs no Python on the runner. Drop the setup-python and install steps, and run devdocs through the image. In the Generate step, `docker run` takes the place of `devdocs`, behind the same `timeout`:

```sh
timeout --signal=TERM --kill-after=150s --preserve-status 30m \
  docker run --rm --init -u "$(id -u):$(id -g)" -v "$PWD:/work" \
    -e GEMINI_API_KEY -e DEVDOCS_MAX_COST \
    ghcr.io/bgantavya/devdocs:4.0.0 generate --json > "$RUNNER_TEMP/devdocs.json" || status=$?
```

and the estimate step runs `docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/work" -e DEVDOCS_MAX_COST ghcr.io/bgantavya/devdocs:4.0.0 estimate`. The cache and upload steps stay as they are.

- `-u` runs devdocs as the runner's user, so it can write `.devdocs/` and `docs/` in the checkout, and the cache and upload steps can read them.
- `-e NAME` with no value passes a variable from the step's environment. Pass every `DEVDOCS_*` setting you use the same way.
- Leave out `-it`: a CI job has no terminal.
- `docker run` passes the signals it receives, such as the SIGTERM from `timeout`, on to the container. `--init` runs an init process as PID 1, which passes them on to devdocs. Without it devdocs is PID 1, and a SIGTERM that arrives before devdocs has set its handler is dropped.
- `docker run` exits with devdocs' own status, so the `case` statement works unchanged.
- The image has no git, so files are ranked without their history, and it has only the built-in PDF engine. [install.md](install.md#docker) lists what else it leaves out.

## Timeouts and cancellation

devdocs treats SIGTERM, which `kill`, `timeout` and a service manager's stop send, as it treats Ctrl-C (SIGINT). [commands.md](commands.md#how-a-run-stops) describes the whole behaviour; what matters in CI is this:

- **The first signal while the run is buying answers stops it gracefully.** Nothing new is sent, nothing is retried, and requests already on the wire finish, their answers cached. Each read or write on a connection times out after 120 s. Every page is written, from the cache or from facts alone, the summary is printed with `"stopped": "interrupted"`, and devdocs exits 143 after SIGTERM, 130 after SIGINT.
- **Allow 150 s before a forced kill**, as `timeout --kill-after=150s` does in the workflow above. A SIGKILL, or a second signal a second or more after the first, stops devdocs at once: nothing more is written, but the answers received stay cached.
- **Save `.devdocs/` after a cancelled job too.** The next run resumes the stopped one, prints `resumed run <id>` (with `--json`, `"resumed": true`), and buys only what is missing.
- A signal that arrives before the run starts buying, as while devdocs lists the models or scans the project, stops it at once, with nothing bought.

`-q` hides the `stopping:` line that a graceful stop prints; the warnings stay.

### Containers

The image's stop signal is SIGINT, so `docker stop` takes the graceful path. Allow it 150 s instead of Docker's default 10 s, with `docker stop -t 150` or `docker run --stop-timeout 150`; `compose.yaml` already does. [Containers and the npm launcher](commands.md#containers-and-the-npm-launcher) has the details.

## Publishing the HTML site

With `html` among the formats, the output directory is a static site: `index.html`, a page per document, `assets/` and a `.nojekyll` file. Every link is relative and nothing is loaded from elsewhere, so it works from any static host, from a subdirectory, or opened from disk. [output.md](output.md) describes the site.

To publish it with GitHub Pages, set the repository's Pages source to GitHub Actions. In the workflow above, add `--format markdown,html` to the `devdocs generate` command, or set `formats = ["markdown", "html"]` in `devdocs.toml`, and upload the directory as the Pages artifact after the save step:

```yaml
      - uses: actions/upload-pages-artifact@v5.0.0
        with:
          path: docs/
```

Then deploy it from a second job:

```yaml
  deploy:
    needs: docs
    runs-on: ubuntu-latest
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v5.0.1
```

If the Markdown output is committed with its `index.json`, a job with no key can make the site: `devdocs export --format html` renders the last run again in HTML, for free. Publishing from a branch instead also works: the `.nojekyll` file keeps GitHub Pages from hiding pages whose names start with `_`, such as `__init__.py.html`.
