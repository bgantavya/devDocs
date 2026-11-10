# Troubleshooting

Start with `devdocs doctor`. It checks the interpreter, the PDF engines, the configuration, the key and the provider, and says what blocks a run. Then find the message you see below. Each entry gives the message, its cause and the fix. The entries are grouped by the kind of problem, with the exit code each one gives; [commands.md](commands.md#exit-codes) lists every exit code.

## Run devdocs doctor

```sh
devdocs doctor --offline
```

`doctor` prints one line per check on stdout, marked `ok` or `!!`. It needs no key and changes nothing on disk. With `--offline`, or without a key, it makes no network request. Without `--offline` and with a key, it also asks the provider for the models the key can use. `--json` prints the same findings, except the stale-prices warning, as one JSON document, which never holds the key; [commands.md](commands.md#doctor) lists its fields.

Real output on a machine with no key set. The values depend on the machine:

```text
ok python                 3.13.15
ok syntax                 4/4 constructs
ok sqlite                 3.53.1
ok sqlite-threads         3
                         connections shareable
ok tls                    OpenSSL 3.5.8 25 Aug 2026
ok certificates           128 CA certificates
ok pdf-engines            builtin, chrome (/Applications/Google Chrome.app/Contents/MacOS/Google Chrome)

ok config               provider=gemini quality=cheap context=standard max_cost=$0.5 max_requests=no limit formats=markdown
!! api key              no API key for provider 'gemini'
  hint: pass --api-key or set one of: $DEVDOCS_API_KEY, $GEMINI_API_KEY, $GOOGLE_API_KEY
```

`doctor` exits 0 when nothing blocks a run, 1 when something does, and 2 when the configuration is invalid. What each finding means:

| Line | Meaning | Fix |
|---|---|---|
| `ok syntax 2/4 constructs`, with `cannot parse: pep695-generics (3.12+), pep701-fstrings (3.12+)` | Python 3.11 cannot parse that newer syntax. Files that use it get fewer facts or none, as [Partial symbol tables](#partial-symbol-tables) describes. It does not block a run. | Run devdocs on Python 3.12 or newer if your code uses that syntax. |
| `!! sqlite-threads` | This Python's SQLite is built without thread support. | Use a Python build with a thread-safe SQLite. |
| `!! certificates 0 CA certificates` | Python trusts no certificate authority, so every HTTPS request fails. | See [TLS certificate cannot be verified](#tls-certificate-cannot-be-verified). |
| `!! config` | The configuration is invalid. `doctor` stops here and exits 2. | See [Configuration refusals](#configuration-refusals). |
| `!! pdf engine` | `pdf` is a configured format, but the configured engine is not installed. | See [PDF engine not installed](#pdf-engine-not-installed). |
| `!! prices` | The catalog's prices were checked more than 180 days ago, so the costs devdocs reports may be out of date. It does not block a run. | Upgrade devdocs, and check the vendor's pricing page. |
| `!! anthropic-sdk` | The provider is `anthropic-sdk`, and the `anthropic` package cannot be imported or has no client. | See [The anthropic SDK is missing](#the-anthropic-sdk-is-missing). |
| `!! api key` | No key was found. | See [No API key](#no-api-key). |
| `!! provider` | The provider's model listing failed: a rejected key, a rate limit, a billing stop or a network failure. The line carries the error and its hint. | Find the error under [Keys](#keys), [Network, TLS and proxies](#network-tls-and-proxies) or [Rate limits and quotas](#rate-limits-and-quotas). |

## Reading an error

A failure that ends a command prints one line on stderr, and a hint on the next line when there is one:

```text
devdocs: error: <message>
  hint: <hint>
```

- A line that starts with `usage:` comes from the argument parser: a misspelled flag, a missing value or an unknown command. It exits 2. When a command's flag refuses its value, the error line names the command, as in `devdocs estimate: error: argument --quality: invalid choice: 'best' (choose from ...)`. An unknown flag gives `devdocs: error: unrecognized arguments: ...`, whatever the command.
- A line that starts with `warning:` reports something the command went on past, or what a stop left behind. The exit code says whether the command failed. `-q` hides progress lines but never warnings.
- `devdocs: internal error: <type>: <message>` is a bug in devdocs, and exits 1. Run the command again with `DEVDOCS_DEBUG=1` set to print the traceback, and [report it](#reporting-a-bug).
- `--json` never turns an error into JSON. When an error ends a command, stdout is empty and the message is on stderr. The one exception is a configuration error in `doctor --json`. A `generate` that ends with failed items, or with `stopped early`, still prints its result, as JSON with `--json`.

## Keys

### No API key

```text
devdocs: error: no API key for provider 'gemini'
  hint: pass --api-key or set one of: $DEVDOCS_API_KEY, $GEMINI_API_KEY, $GOOGLE_API_KEY
```

Exit 2. `generate` and `models` need a key, `doctor` reports a missing one, and devdocs never prompts for one. It looks for `--api-key`, then the provider's variables in the order the hint names them, then `api_key` in `devdocs.toml`. Besides `DEVDOCS_API_KEY`, which serves either provider, each provider reads only its own variables: `GEMINI_API_KEY` does not serve `--provider anthropic`, whose hint names `$DEVDOCS_API_KEY` and `$ANTHROPIC_API_KEY`.

Fix: set the provider's variable in your environment, or as a secret in CI, then check that devdocs finds it with `devdocs doctor --offline`, which prints `ok api key              from env GEMINI_API_KEY`. [configuration.md](configuration.md#the-api-key) has the full order, and [models.md](models.md#get-an-api-key) says where to get a key. This refusal comes before devdocs creates anything on disk.

### The key is rejected

```text
devdocs: error: Gemini rejected the API key: <the provider's message>
  hint: check GEMINI_API_KEY; get one at aistudio.google.com
```

For Claude the message is `Anthropic rejected the request: <the provider's message>`, with the hint `check ANTHROPIC_API_KEY and the key's workspace access`.

Exit 3. The provider answered 401 or 403, or Gemini said the key is not valid. `generate` asks the provider for its model list before it scans the project, so a bad key fails once, at the start, though it may leave an empty `.devdocs/devdocs.db`. The hint names the vendor's usual variable even when the key came from elsewhere.

Fix: run `devdocs doctor --offline` to see where the key comes from (`from flag --api-key`, `from env ...`, or `from file <path>`), and replace it there. `doctor` without `--offline` reports a rejected key as `!! provider` and exits 1, not 3.

### The key holds stray characters

```text
devdocs: error: request header 'x-goog-api-key' has a line break or a character HTTP headers cannot carry
  hint: if it holds the API key, check the key for stray whitespace or pasted characters
```

The header is `x-api-key` with provider `anthropic`. Exit 4. The key holds a character that an HTTP header cannot carry: a control character other than a tab, such as a line break, or a character outside Latin-1, such as a curly quote from a paste. devdocs refuses it before it opens any connection, and does not print the key. Spaces and line breaks around a key are trimmed; this is a character inside it. With provider `anthropic-sdk`, Anthropic's SDK sends the key, and devdocs makes no such check.

Fix: set the key again from a clean copy.

### estimate refuses --api-key

```text
devdocs: error: unrecognized arguments: --api-key
```

Exit 2, after the usage line. `estimate` sends nothing and needs no key, so it does not take `--api-key`. A key that the message lists shows as `***`, as in `unrecognized arguments: --api-key ***`. Drop the flag.

### The anthropic SDK is missing

```text
devdocs: error: provider 'anthropic-sdk' needs the anthropic package
  hint: pip install 'devdocs[anthropic]', or use provider = 'anthropic'
```

Exit 2. Provider `anthropic-sdk` sends its requests through Anthropic's own SDK, an optional extra that the installer scripts, the npm package and the container image do not install. `doctor` reports it on its `!! anthropic-sdk` line, as `cannot import anthropic: ModuleNotFoundError: No module named 'anthropic'`.

Fix: install the extra ([install.md](install.md#the-anthropic-extra)), or use provider `anthropic`, which talks to the same API with no SDK.

## Network, TLS and proxies

These errors exit 4 when they happen while `generate` or `models` lists the models, which is not retried. During a run, devdocs retries a failed request; see [Items fail after retries](#items-fail-after-retries). In the messages, `<host>` is `generativelanguage.googleapis.com` for Gemini and `api.anthropic.com` for Claude. With provider `anthropic-sdk`, Anthropic's SDK makes the requests, and a network failure reads `network error: <the SDK's message>`.

### The provider cannot be reached

```text
devdocs: error: network error talking to <host>: <reason>
```

The connection failed: no network, a DNS failure, a firewall, or a timeout. Each connect, read and write on a connection times out after 120 seconds; there is no setting for that.

Fix: check that the machine can reach `<host>` over HTTPS. Behind a proxy, set `HTTPS_PROXY` and, for hosts to reach directly, `NO_PROXY`; [configuration.md](configuration.md#other-variables-devdocs-reads) describes both. When no proxy variable is set, devdocs uses the system's proxy settings on macOS and Windows.

### TLS certificate cannot be verified

```text
devdocs: error: TLS certificate for <host> could not be verified: <reason>
  hint: run `devdocs doctor`; on macOS python.org builds, run 'Install Certificates.command' or set SSL_CERT_FILE
```

Python does not trust the certificate the server, or a proxy that inspects TLS, presented. `doctor` shows `!! certificates 0 CA certificates` when Python trusts no certificate authority at all. This failure is never retried.

Fix: on macOS with Python from python.org, run `Install Certificates.command` from that Python's folder in Applications. Otherwise point `SSL_CERT_FILE` at a CA bundle that includes your organization's certificate, if a proxy inspects TLS.

### Proxy errors

| Message | Cause | Fix |
|---|---|---|
| `the proxy would not connect to <host>: <reason>` | The proxy refused the CONNECT request, as for missing or wrong credentials. | Check the proxy and its credentials in `HTTPS_PROXY`, or exempt the host with `NO_PROXY`. |
| `<scheme> proxies are not supported, only HTTP CONNECT proxies` | The proxy URL names another scheme, such as `socks5`. | Use an HTTP proxy that supports CONNECT. |
| `the proxy URL has no host`, `the proxy URL has a malformed host or port` | `HTTPS_PROXY` cannot be read as a URL. | Fix the variable. |
| `<host> sent a reply that is not valid HTTP/1.1: ...` | Something between devdocs and the provider answered with something other than HTTP/1.1. | Check `HTTPS_PROXY`: a proxy or middlebox may be in the way. |

A proxy error never quotes the proxy URL, which may hold a password.

### Items fail after retries

```text
failed file shop/cart.py: <error>
```

Exit 4 at the end of the run. During a run, devdocs retries a request that failed for a reason that may pass (a network error, a 429 or a server error), up to 4 attempts in all. A `retry` progress line shows each wait. When the server asks for a wait longer than 120 seconds, the item fails at once. The run goes on, writes every page, the failed ones from facts alone, and lists each failure in its result. `no usable reply` in place of the error means that no model gave a usable answer, as when the vendor blocked or refused the request.

Fix: run `devdocs generate` again. It resumes, takes every answer already bought from the cache, and asks only for what is missing.

## Rate limits and quotas

### Rate limited

Exit 5 when the model listing gets a plain 429, which `generate` and `models` do not retry. Wait a minute and run again.

During a run, a 429 is retried after the delay the server gives, or after a short backoff:

```text
  retry     gemini-2.5-flash-lite    shop/cart.py  (#1 in 30.0s: <the provider's message>)
```

By default devdocs paces requests to Gemini's free-tier limits. With a paid key, pass `--paid` or set `DEVDOCS_FREE_TIER=false`; see [models.md](models.md#free-tier-pacing-and---paid).

### A run pauses with no output

During `generate`, the progress lines stop for up to a minute at a time, then go on. The run is not stuck. Under free-tier pacing, the default, devdocs holds each Gemini model to its free requests per minute: 15 for the file-tier models, 10 for the module-tier models and 5 for `gemini-2.5-pro`, as the [catalog](models.md#gemini-models) lists. When a model has used its limit in the last 60 seconds, devdocs waits for a slot and prints nothing while it waits. Claude models are paced the same way, at 50 requests per minute each, with or without `--paid`.

Fix: let the run go on. With a paid Gemini key, pass `--paid`, which paces to the paid limits. A Ctrl-C ends the wait at once and stops the run gracefully, as [Interrupted runs](#interrupted-runs) describes.

### Daily quota used up

```text
stopped early (quota); run again to resume
```

Exit 10. A model whose daily quota is spent (`Gemini daily quota exhausted: ...`) is set aside until the provider's quota day turns, and another model takes over. Under free-tier pacing, devdocs also counts each model's requests per day, across the project's runs, and sets a model aside at its free limit with `reached this model's limit of <n> requests per day`. When no model is left, the run stops: it writes every page, the rest from facts alone, and keeps every answer it bought.

Fix: run `devdocs generate` again once the quota resets, or with a paid key. It resumes where it stopped. [models.md](models.md#daily-quotas) explains the quota day.

### Spend cap or billing

The messages are `Gemini spend cap reached: ...`, `Gemini billing: ...`, `Anthropic spend limit reached: ...` or `Anthropic billing: ...`. Each holds for the whole account, so devdocs tries no other model. During a run, it sends nothing more and prints `stopped: <message>`; requests already sent finish, every page is written, the rest from facts alone, and the result says `stopped early (quota); run again to resume`. If it happens while the models are listed, the command ends there, and the hint reads ``fix the account's billing, then run `devdocs generate` again (it resumes where it stopped)``. Either way the exit code is 10.

Fix: raise the account's spend limit or settle its billing in the vendor's console, then run again.

### No model the key can use

```text
devdocs: error: no gemini model available for file work
  hint: run `devdocs doctor` to see which models your key can use
```

Exit 2, usually before any model call. The key's model listing holds no catalogued model for this tier of work (`file`, `module` or `architecture`): none of the tier where `--quality` starts it, and none of a tier above. Under free-tier pacing on Gemini, only models with a free tier count, and a lower tier's model can serve a higher tier, so there this error means the key's listing holds no catalogued model with a free tier at all.

Fix: run `devdocs models` to see which catalogued models the key can reach. With a paid key, pass `--paid`, so that models without a free tier count too. If the error came with `--paid`, under which no lower tier's model stands in, run without it, or pin a model the key lists with `--model`. [models.md](models.md#the-model-catalog) lists the catalog.

### Every model answered 404

```text
devdocs: error: every file-tier model was unavailable
  hint: the key may not have access to any catalogued model
```

Exit 2. Every model devdocs tried for the tier answered 404, so each was retired; a `retire` progress line shows each one. The usual cause is a mistyped `--model` pin: the pinned model is the only one tried, and it is not checked against the listing. The run stops before it writes any page. Answers it bought before the stop are cached.

Fix: check the id with `devdocs models`, then correct or drop `--model`, and run again. [models.md](models.md#the-models-command) explains the listing.

## The budget

By default every run has a cost ceiling: `--max-cost` is 0.5 US dollars. `--max-requests` has no limit by default. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend. Answers the cache already holds cost nothing and count against neither limit.

### Pages from facts alone, exit 0

```text
8 documents (3 with prose, 5 from facts alone) in docs
```

Pages from facts alone are often by design: devdocs buys prose only where it adds most, as [costs.md](costs.md#what-gets-prose) explains, and `devdocs estimate --explain` gives each file's plan and the reason for it. The reason `parse failed` means the file's parser could not read it; see [Partial symbol tables](#partial-symbol-tables). The other cause is the budget. The plan did not fit the limits, so `generate` bought only what fit and wrote the rest from facts alone. It prints no line about the cut. `devdocs estimate` with the same settings shows it, for example `5 items left as stubs to fit --max-cost $0.001`.

Fix, for a cut: raise `--max-cost`, or run again: the answers already bought are cached and free, so each run's budget goes to what is still missing. [costs.md](costs.md#budget-guards) explains the guards.

### Budget stop

A run with `--max-requests 8` whose first call was rate limited and retried printed this progress line on stderr:

```text
stopped: request limit of 8 reached
```

Its result, on stdout, ended with:

```text
stopped early (budget); run again to resume
```

Exit 7. The run reached a limit partway, as when retries take more requests than the plan priced. For `--max-cost` the progress line reads `stopped: next call (up to $<x>) would pass the $<limit> limit; $<spent> spent so far`. `-q` hides these progress lines, not the result. Every page is written, the rest from facts alone.

Fix: run `devdocs generate` again to buy the rest, or raise the limit.

## Refusals to write

`generate` checks its output before the first model call, so most of these refusals cost nothing, though they may leave an empty `.devdocs/devdocs.db`. Each exits 2. [output.md](output.md#never-replacing-your-files) explains the rules.

### Files devdocs has no record of writing

```text
devdocs: error: refusing to replace 1 file in /path/to/shop/docs that devdocs has no record of writing: shop/money.py.md
  hint: move it away or choose another --out; --force replaces it
```

A file the manifest does not list sits where devdocs would write a page: a hand-written page, an old 3.x tree, or another tool's `index.json`.

Fix: move the file away, choose another `--out`, or pass `--force` to replace it. Coming from 3.x, see [migration.md](migration.md#your-existing-docs-tree).

### A directory is in the way

```text
devdocs: error: cannot write 1 file in /path/to/shop/docs: a directory is in the way of README.md
  hint: move it away or choose another --out
```

A directory has the name of a file devdocs writes. `--force` does not remove directories. Move it, or choose another `--out`.

### Cannot write

The message is `cannot write <file> in <out>: <reason>`, or `cannot write to <out>: <reason>` when the output directory itself, or a directory above it, is at fault. The hint says what to do:

| Hint | Cause |
|---|---|
| `devdocs needs write access to <directory>` | The directory is not writable by you. |
| `a file or directory is in the way: move it away or choose another --out` | `--out` names an existing file, or a path through one. |
| `choose an --out on a file system devdocs can write to` | The file system is read-only. |
| `free some space on the disk, then run again` | The disk is full, or a quota is reached. |
| `shorten the source file's name, or choose an --out with a shorter path` | A page's name is longer than the file system allows. When the output directory's own path is too long, the hint is `choose an --out with a shorter path`. |
| ``rename the source file it documents, then run `devdocs generate` again; or choose an --out on a file system that can name it`` | The file system cannot hold the page's name, and the reason reads `the file system cannot name it (...)`. For a source file whose name is not UTF-8, which macOS refuses, the hint reads `rename the source file it documents so its name is UTF-8, ...`. |
| `<file> is locked or read-only: close any program that has it open, or clear its read-only or immutable flag, then run again` | Another program holds a page open, or the file is flagged read-only or immutable. |
| `check that devdocs can write to <out>, or choose another --out` | Any other failure. The reason in the message says what it was. |

Real examples, each refused before any model call:

```text
devdocs: error: cannot write index.json in /path/to/shop/docs: Permission denied
  hint: devdocs needs write access to /path/to/shop/docs
```

```text
devdocs: error: cannot write index.json in /path/to/shop/afile: Not a directory
  hint: a file or directory is in the way: move it away or choose another --out
```

A disk that fills up, or a file another program locks, can still fail the write midway, after the paid part. The pages renamed by then are new, the others as they were, and the manifest lists everything the write may have made, so the next run sorts it out.

### Refused after paying

If a file appears at an output path while the run is buying prose, the write is refused at the end, after paying. The hint then adds what running again costs, for example:

```text
  hint: move it away or choose another --out; --force replaces it; the answers are cached, so running again costs nothing
```

Fix the cause and run again: the answers bought are cached.

### Export finds no manifest

```text
devdocs: error: no generated documentation in /path/to/shop/docs: index.json is missing
  hint: run `devdocs generate` first, or point --out at the directory it wrote
```

Exit 2. `export` renders from the manifest that `generate` wrote. Run `generate` first, or pass the `--out` it used. [output.md](output.md#export) lists the other messages for a manifest it cannot read.

## Configuration refusals

devdocs checks the flags, the `DEVDOCS_*` variables and `devdocs.toml` as a command starts, before it creates anything. A value it refuses ends the command with exit 2, and `doctor` reports it on its `!! config` line. Many messages come with a hint that says what to do, as here:

```text
devdocs: error: unknown format(s): md
  hint: choose from: html, markdown, pdf
```

[configuration.md](configuration.md#validation) lists every message with its hint and cause. These common ones have no hint:

| Message | Fix |
|---|---|
| `project root <root> does not exist` | The message gives the path devdocs tried. Correct the path argument, `DEVDOCS_PATH` or `path` in `devdocs.toml`, whichever gave it. |
| `<file> is not valid TOML: <reason>` | Fix the line the reason names. A string needs quotes: `out = "docs"`, not `out = docs`. |
| `DEVDOCS_<NAME> must be true or false`, `DEVDOCS_<NAME> must be a number` | Give the variable a value of its type; [Setting variables](configuration.md#setting-variables) lists the values each type takes. |

## Interrupted runs

The first Ctrl-C (SIGINT) or SIGTERM during the paid part of `generate` stops it gracefully:

```text
stopping: requests already sent finish and are kept, then the pages are written; interrupt again to stop at once
```

Nothing new is sent. Requests already sent finish and are cached. Every page is written, with the answers received and any that an earlier run paid for, the rest from facts alone, and the result says `stopped early (interrupted); run again to resume`. The exit code is 130 after SIGINT and 143 after SIGTERM. `-q` hides the `stopping:` line, but not the result.

A second signal, a second or more after the first, stops at once:

```text
warning: stopped at once; the answers received are cached, so the next run writes their pages for free
```

No more pages are written, and the exit code is 128 plus the second signal's number. A stop before the paid part, while devdocs lists the models, scans the project or checks the output, ends at once with `warning: interrupted; nothing was bought and no documents were written`.

Fix: run `devdocs generate` again with the same settings. It prints `resumed run <id>`, takes every cached answer for free, and buys only what is missing. `--no-resume` starts a new run record but still uses the cache. To buy every answer again, empty the cache with `devdocs cache clear`. [commands.md](commands.md#how-a-run-stops) describes every case, and [ci.md](ci.md#timeouts-and-cancellation) covers cancelled CI jobs and containers.

A run with a failed item, or a failed format, also prints `resumed run <id>` next time. That run wrote every page, a failed item's from facts alone, and the next run buys only what is missing.

## Partial symbol tables

```text
partial symbol tables: shop/broken.py, shop/js/widget.js, shop/unclosed.py
```

Exit 0: a partial symbol table does not fail the run. The result of `generate` includes this line when a parser could read a file only in part, or not at all. The usual causes are a syntax error, syntax newer than the Python that runs devdocs (Python 3.12 syntax on 3.11, for example), and a string or comment left unterminated. Each such page says `Symbol table may be incomplete`, and may miss symbols. A Python page also lacks the file's docstrings. A file its parser could not read at all gets no prose, and `devdocs estimate --explain` gives it the reason `parse failed`. The line names up to 10 files; with `--json`, `degraded` lists them all, as [commands.md](commands.md#output) describes.

Fix: correct the syntax, or run devdocs on a Python that reads it. On Python 3.11, the `syntax` line of `devdocs doctor --offline` names the 3.12 constructs that Python cannot parse.

## The state database

`generate` keeps its response cache and run ledger in `<project>/.devdocs/devdocs.db`. When that file cannot be used, the command exits 9. The hint says why:

| Hint | Fix |
|---|---|
| `another devdocs process is using the index; run again once it ends` | Wait for the other run on this project to finish. |
| `the index is damaged: delete <project>/.devdocs/ to rebuild the index` | Delete the directory. The next run pays again for answers the cache held. |
| `devdocs needs write access to <project>/.devdocs` | Fix the directory's permissions. |
| `the disk is full; free some space and run again` | Free some space. |
| `upgrade devdocs, or delete <project>/.devdocs/ to rebuild the index` | A newer devdocs wrote the file. Upgrade, or delete the directory. |

## PDF

### Characters printed as ?

```text
pdf: characters outside the built-in fonts were printed as '?' (2):
  U+2192 RIGHTWARDS ARROW in the overview
  U+2713 CHECK MARK in the overview
  for all of Unicode, use --pdf-engine chrome or --pdf-engine weasyprint
```

The built-in engine uses the standard PDF fonts, which cover only WinAnsi (Windows code page 1252): ASCII, Latin-1 letters and common punctuation. Any other character prints as `?`, and devdocs lists each one with the chapter it is in. The run still succeeds, and the result line adds `2 rendering warnings; --json lists them all`. `-q` hides the list, as a progress line, but not that result line.

Fix: print with an engine that covers all of Unicode, from the manifest, without a model:

```sh
devdocs export --format pdf --pdf-engine weasyprint
```

or with `--pdf-engine chrome`. Or keep those characters out of the text the named chapter shows, such as a docstring or the project description. [output.md](output.md#characters-outside-the-built-in-fonts) lists what the built-in fonts cover.

### PDF engine not installed

```text
devdocs: error: the weasyprint PDF engine is not installed
  hint: install it or use --pdf-engine builtin; `devdocs doctor` lists the engines it finds
```

Exit 2, before any model call. devdocs finds `weasyprint` on `PATH` only, and `chrome` on `PATH` or in the browsers' usual install folders. The container image includes neither.

Fix: install WeasyPrint, or Chrome, Chromium or Edge, and check that `devdocs doctor --offline` lists it; or use the built-in engine.

### The PDF engine fails

```text
failed format pdf: weasyprint could not print the book (exit status 3): fake weasyprint: cannot load fonts
  hint: run /path/to/weasyprint by hand to see why, or use the builtin PDF engine; `devdocs export --format pdf --pdf-engine weasyprint` makes it from the manifest, with no key
```

That is real output from a stand-in `weasyprint` that always fails. `generate` writes the other formats and the manifest, then exits 4. An engine that takes longer than 120 seconds fails with `<engine> did not finish printing within 120 seconds`.

Fix: run the engine by hand to see its full error. Once it works, run the `export` command the hint names. It makes the PDF from the manifest, without a key or a model. It repeats the project path, `--out`, `--pdf-engine`, `--page-size` and `--split` or `--no-split` where the command line gave them. Settings from `devdocs.toml` and `DEVDOCS_*` variables apply to `export` as they did to the run: without `--pdf-engine`, it uses the configured engine, which is the built-in one unless `pdf_engine` or `DEVDOCS_PDF_ENGINE` says otherwise. From `export`, an engine failure exits 1.

## Windows notes

- **Support.** CI runs the tests and the installer on Windows in jobs marked experimental, which may fail without failing the build, so Windows is less tested than Linux and macOS; see [install.md](install.md#requirements).
- **Stopping.** Ctrl-C stops a run as described in [Interrupted runs](#interrupted-runs). While devdocs waits for answers, it wakes every half second so that a Ctrl-C is not held up.
- **Files held open.** Windows does not let devdocs replace a file that another program holds open, as some PDF viewers hold the book. The write then fails with the hint `<file> is locked or read-only: close any program that has it open, ...`. Close the program and run again; the answers bought are cached.
- **Names that differ only in case.** Windows and macOS store `Cart.py.md` and `cart.py.md` as one file. devdocs never gives two pages such names, on any system: the later one is `cart.py-2.md`, so docs written on Linux keep every page on Windows. [output.md](output.md#what-each-file-is-for) has the rule.
- **Long paths.** A deep source tree under a deep `--out` can produce a path Windows will not create. Choose an `--out` with a shorter path.
- **PDF engines.** The `chrome` engine also finds Chrome, Chromium and Microsoft Edge in their install folders under `%PROGRAMFILES%`, `%PROGRAMFILES(X86)%` and `%LOCALAPPDATA%`. `weasyprint` must be on `PATH`. `devdocs doctor --offline` lists what it found.
- **The `next:` line of `devdocs init`** writes the path with forward slashes, and quotes it in single quotes when it needs quoting. PowerShell and Git Bash read it as written; cmd.exe does not.
- **Console output.** A character the console cannot show, as in a file name, is printed as a backslash escape rather than failing the command.
- **The free-tier quota day** turns at midnight US Pacific time. A Python without the time zone database, as on a bare Windows install, uses a fixed UTC-8, which is an hour off in summer; it matters only within an hour of the reset.

## Reporting a bug

Report a bug with the [bug report form](https://github.com/bgantavya/devDocs/issues/new?template=bug_report.yml). Include:

- the output of `devdocs --version`, how you installed devdocs, and your operating system;
- the output of `devdocs doctor --offline`, which names where the key comes from, never the key;
- the commands you ran, with any key removed;
- what devdocs printed on stdout and stderr, and its exit status;
- for an internal error, also the traceback that the same command prints with `DEVDOCS_DEBUG=1` set.

For a page devdocs wrote that is wrong or unclear, use the [generated docs form](https://github.com/bgantavya/devDocs/issues/new?template=generated_docs.yml) instead.

devdocs never prints the key it uses, but a command line, your shell history, environment and CI logs may hold one: check what you paste. Report a security problem privately, as [SECURITY.md](../SECURITY.md) describes, not in a public issue.
