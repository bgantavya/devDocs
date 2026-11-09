# Commands

This page is the reference for every `devdocs` command, flag and exit code. Each command has its arguments, flags, examples and output. Then come the options several commands share, how a v3 command line is handled, the exit codes, and how a run stops. Settings that a flag shares with `devdocs.toml` and the environment are described in [configuration.md](configuration.md).

```text
devdocs [-h] [--version] command ...
```

| Command | What it does | Needs a key | Contacts the provider |
|---|---|---|---|
| [`generate`](#generate) | Writes the documentation. | Yes | Yes |
| [`estimate`](#estimate) | Prices a run and sends nothing. | No | No |
| [`export`](#export) | Renders the last run again, in other formats. | No | No |
| [`doctor`](#doctor) | Checks that a run can work. | No | Only to test a key it found, and never with `--offline` |
| [`init`](#init) | Writes a starter `devdocs.toml`. | No | No |
| [`models`](#models) | Lists devdocs' catalog models for the provider, and which your key can use. | Yes | Yes |
| [`cache`](#cache) | Shows or clears the response cache. | No | No |

| Flag | Effect |
|---|---|
| `-h`, `--help` | Prints help and exits 0. Every command also takes it: `devdocs generate --help`. |
| `--version` | Prints `devdocs <version>` and exits 0. It goes before the command: `devdocs generate --version` is an error. |

`devdocs` with no arguments prints the help and exits 0.

Long options can be shortened to any unambiguous prefix, as in `--max-c` for `--max-cost`. An ambiguous prefix, such as `--p` on `estimate`, is an error.

## generate

```text
devdocs generate [path] [output options] [run options]
                 [--api-key API_KEY] [--force] [--no-resume] [--json] [-q]
```

`generate` writes the documentation of a project into its output directory, `docs` under the project root by default. Parsers supply the facts. A model writes prose only for the items the plan selects, within the budget.

What it does, in order:

1. It reads the configuration and checks the project root and the output directory. A problem exits 2 before anything is created.
2. It finds the API key, as [configuration.md](configuration.md#the-api-key) describes. Without one it exits 2, again before anything is created.
3. It opens the project's state database, `.devdocs/devdocs.db`, and creates it if needed; no other command creates it. The database holds the response cache, the run ledger, the daily request counts and, with semantic context, the embedding vectors. It then starts the provider's client and builds its renderers, so provider `anthropic-sdk` without the `anthropic` package, or a configured PDF engine that is not installed, fails here.
4. It asks the provider which models the key can use. The listing is free, but it is a request, and it is not retried. A rejected key exits 3.
5. It scans, parses and plans the project.
6. It checks the output before it buys anything. It refuses, with exit 2, what the write would refuse: a file devdocs has no record of writing at a path where it writes one of its own files, such as a page or the manifest `index.json` (unless `--force` is given), a directory in the way of one of its files, an output directory it cannot write, an `--out` that names a file, or a file name the file system cannot hold. Your other files in the output directory are left alone, and devdocs writes its files beside them. No model call has been made at this point.
7. With `--context semantic`, it first embeds the code that has no stored vector yet; [context.md](context.md) explains when it skips that. It cuts the plan to fit `--max-cost` and `--max-requests`, where 0 means no limit, not zero spend, then sends the calls in three waves: files, then module summaries, then the architecture overview. An answer taken from the response cache costs nothing and counts against neither limit.
8. It writes every document and prints a summary.

A failure at steps 3 to 6 can leave behind a `.devdocs/devdocs.db` that holds no data. A refusal at step 6 looks like this, with nothing bought:

```text
devdocs: error: refusing to replace 1 file in /home/me/shop/docs that devdocs has no record of writing: README.md
  hint: move it away or choose another --out; --force replaces it
```

The items that the budget cut become pages built from the facts alone, called stubs. `generate` does not list them: run [`estimate`](#estimate) first to see the cut. [costs.md](costs.md) explains the budget and the cache.

By default a run resumes the newest unfinished run that has the same project root, provider, quality, include and exclude settings, and its summary says `resumed run <id>`. `--no-resume` starts a new run record instead. Either way, a cached answer is reused for free while its prompt and model stay the same; [costs.md](costs.md#what-a-re-run-pays-for) lists what buys one again.

### Arguments

| Argument | Default |
|---|---|
| `path` | The project root. Without it, `$DEVDOCS_PATH`, else `path` in `devdocs.toml`, else the current directory. |

### Flags

`generate` takes the [output options](#output-options) and the [run options](#run-options), and these:

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--api-key` | `API_KEY` | none | The API key for this run. See [`--api-key`](#--api-key). | `api_key` and the key variables; see [the API key](configuration.md#the-api-key) |
| `--force` | none | off | Replaces files devdocs has no record of writing at the paths where it writes its own files. Your other files in the output directory are left alone. | none |
| `--no-resume` | none | off | Starts a new run record instead of resuming the last unfinished one. Cached answers are still reused. | none |
| `--json` | none | off | Prints the summary as one JSON document on stdout. See [`--json` and `-q`](#--json-and--q). | none |
| `-q`, `--quiet` | none | off | Hides the progress lines on stderr, the details of rendering warnings included. Lines that start with `warning:` and errors still show. | none |

### Examples

These assume a key in the environment, such as `GEMINI_API_KEY`.

```bash
devdocs generate
devdocs generate . --format markdown,html
devdocs generate --quality balanced --max-cost 1
devdocs generate --max-cost 0
devdocs generate --json -q > run.json
```

`--max-cost 0` means no cost ceiling, not zero spend. `--max-requests 0`, the default, likewise means no request limit.

### Output

Progress goes to stderr, and `-q` hides it. This is a first run on an 8-file project, made with a scripted test model, so its token counts and costs do not reflect a real provider:

```text
gemini: 3 models available to this key
scanned 8 files, 6 documentable, 8 model calls planned (0.0s)
  call      gemini-2.5-flash-lite    shop/cart.py  $0.00007
  call      gemini-2.5-flash-lite    shop/tax.py  $0.00007
  call      gemini-2.5-flash-lite    shop/__init__.py  $0.00007
  call      gemini-2.5-flash-lite    shop/cli.py  $0.00007
  call      gemini-2.5-flash-lite    shop/orders.py  $0.00007
  call      gemini-2.5-flash-lite    shop/money.py  $0.00007
  call      gemini-2.5-flash         shop  $0.00030
  call      gemini-2.5-pro           architecture  $0.00122
```

Each event line names what happened, the model and the item:

| Event | Meaning |
|---|---|
| `call` | The model answered. The line adds the cost, when there is one. |
| `cache-hit` | The answer came from the response cache, for free. |
| `retry` | A request failed and is sent again after a wait. The detail gives the attempt, the wait and the error. |
| `escalate` | The prompt goes to another model, because the one the detail names gave an unusable answer, was retired or had its quota spent. [models.md](models.md#escalation) explains the order. |
| `retire` | The model answered 404, and the run calls it no more. The next run tries it again. |
| `quota` | The model's daily quota is spent, and it is set aside until the quota day turns; see [models.md](models.md#daily-quotas). |

The details of `retry`, `escalate`, `retire` and `quota` lines are in parentheses. Other stderr lines:

- `stopping: ...` after a first stop signal; see [How a run stops](#how-a-run-stops).
- `stopped: <reason>` when the budget or the provider's quota ends the run early, such as `stopped: request limit of N reached`.
- `failed <kind> <item>: <error>` when an item's call ends in an error. The summary lists every failure, including items that got no usable reply.
- The semantic context lines, described in [context.md](context.md).
- The rendering warnings, one block per format, such as characters the built-in PDF fonts cannot print.

`-q` hides all of these except lines that start with `warning:`.

The summary goes to stdout:

```text
8 documents (8 with prose, 0 from facts alone) in docs
8 model calls, 0 cache hits, 4,000 input / 480 output tokens, $0.0020
```

"model calls" counts every request sent: retries, failed attempts and embedding batches included. The token counts add up the prose used, cached answers included. These lines follow when they apply, in this order:

- `resumed run <id>`
- `stopped early (<reason>); run again to resume`, where the reason is `budget`, `quota` or `interrupted`
- `failed <kind> <item>: <error>`, one per failure; `<item>` is `(project)` for the architecture overview
- `partial symbol tables: <files>`, naming up to 10 files that their parser could read only in part, or not at all
- `1 rendering warning; --json lists it`, or `N rendering warnings; --json lists them all`

The usual causes of a partial symbol table are a syntax error, syntax newer than the Python that runs devdocs, such as Python 3.12 syntax on 3.11, and a string or comment left unterminated. The page of such a file says `Symbol table may be incomplete`, and may miss symbols. A file its parser could not read at all gets no prose, and `estimate --explain` gives it the reason `parse failed`. Fix the syntax, or run devdocs on a Python that reads it.

With `--json`, stdout holds one JSON document. This one is from a second run of the same scripted test, which found every answer cached:

```json
{
  "run_id": "58853df7f556",
  "resumed": false,
  "documents": 8,
  "prose": 8,
  "stubs": 0,
  "requests": 0,
  "cache_hits": 8,
  "input_tokens": 4000,
  "output_tokens": 480,
  "cost": 0.0,
  "stopped": "",
  "failed": [],
  "degraded": [],
  "warnings": [],
  "written": {
    "written": [
      "index.json"
    ],
    "unchanged": [
      "README.md",
      "architecture.md",
      "shop/__init__.py.md",
      "shop/cart.py.md",
      "shop/cli.py.md",
      "shop/index.md",
      "shop/money.py.md",
      "shop/orders.py.md",
      "shop/tax.py.md"
    ],
    "removed": []
  },
  "semantic_fallback": ""
}
```

| Field | Meaning |
|---|---|
| `run_id` | The run's id in the ledger, 12 hexadecimal characters. |
| `resumed` | `true` when the run resumed an unfinished one. |
| `documents`, `prose`, `stubs` | Documents written; those with prose; those built from facts alone. |
| `requests` | Requests sent to a model, counted as for "model calls". |
| `cache_hits` | Answers taken from the response cache. |
| `input_tokens`, `output_tokens` | Tokens of the prose used, cached answers included. |
| `cost` | US dollars this run spent, not rounded. |
| `stopped` | `""`, `"budget"`, `"quota"` or `"interrupted"`. |
| `failed` | A list of `[kind, key, error]`. `kind` is `file`, `module`, `architecture` or `format`. `key` is `""` for the architecture overview and the format's name for a format. A format's error ends with a hint naming the `devdocs export` command that makes that format later, with no key; that command repeats the path, `--out` and PDF options this run's command line gave. |
| `degraded` | Every file whose symbol table is partial, as the `partial symbol tables` line describes. |
| `warnings` | The rendering warnings. |
| `written` | `written`, `unchanged` and `removed`: paths relative to the output directory. |
| `semantic_fallback` | `""`, or why embedding failed when the run fell back to standard context. |

`--json` does not turn errors into JSON. On an error stdout is empty and stderr holds the message. A stopped run is not an error: it prints its summary.

### Exit status

`generate` exits with the first of these that applies:

1. `130` or `143` if a stop signal arrived, even when nothing was left to cut;
2. `7` if the budget stopped the run partway;
3. `10` if the provider's quota stopped it;
4. `4` if any item or format failed;
5. `0` otherwise, also when the plan was cut to fit the budget before the first call.

Errors that end the command early exit 1, 2, 3, 4, 5, 6, 9 or 10. See [Exit codes](#exit-codes).

## estimate

```text
devdocs estimate [path] [output options] [run options] [--explain] [--json] [-q]
```

`estimate` prices the run that `generate` would make with the same options. It needs no key and makes no network request. It changes nothing stored: it reads `.devdocs/devdocs.db`, when there is one, to find the answers already cached, and never creates it.

It checks the configuration as `generate` does, the PDF options and engine included.

The figures are upper bounds for the planned calls. Every byte of a prompt counts as an input token, every call is priced at its full output cap, and each call is priced on the first model its tier would use. Retries and escalations are not included. A key that reaches only a tier's stand-in models pays their rates, which are usually higher. `--max-cost` caps the run's spending all the same, unless it is 0, which means no limit, not zero spend. [costs.md](costs.md#what-at-most-means) explains. An answer the run would take from the cache costs nothing. After a file changes, the estimate also counts its module summary and the architecture overview as calls, since a run asks them again; the run may find them in the cache and make fewer calls.

`--format`, the PDF options, `--concurrency` and `--per-file` are accepted but do not change the price. `estimate` does not take `--api-key`.

### Arguments

| Argument | Default |
|---|---|
| `path` | The project root. Without it, `$DEVDOCS_PATH`, else `path` in `devdocs.toml`, else the current directory. |

### Flags

`estimate` takes the [output options](#output-options) and the [run options](#run-options), and these:

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--explain` | none | off | Lists every planned file with its plan and the reason for it. | none |
| `--json` | none | off | Prints the estimate as one JSON document on stdout. | none |
| `-q`, `--quiet` | none | off | Hides the progress line on stderr. | none |

### Examples

```bash
devdocs estimate
devdocs estimate --explain
devdocs estimate --quality high
devdocs estimate --max-cost 0.0007 --json -q
```

### Output

stderr carries the `scanned ...` line, which `-q` hides. stdout carries the estimate. Real output for an 8-file project with nothing cached:

```text
6 files: 6 with prose, 0 from facts alone
8 model calls (1 architecture, 6 files, 1 module), 0 already cached
at most 16,222 input / 3,794 output tokens, at most $0.0214
```

The second line counts the calls by kind, and says `(none)` when every answer is cached. These lines follow when they apply:

- `free tier: up to N days of daily quota, fewer if other models can take over; runs resume where they stop`, on the free tier, when the plan needs more than one day of the models' daily quotas.
- `N items left as stubs to fit --max-cost $X` (`1 item left as a stub` for one), with `--max-requests N` in place of the cost, or both joined by `and`: the items the budget cut, and the limit that cut them. A limit of 0 means no limit, not zero spend, and cuts nothing.

With `--explain`, a blank line follows, then one line per planned file with its mode (`prose` or `stub`), its significance score, its path and the reason:

```text

  prose    4.40  shop/cart.py  (ranked 1 of 6)
  prose    2.62  shop/cli.py  (ranked 2 of 6)
  prose    5.23  shop/money.py  (ranked 3 of 6)
  prose    1.12  shop/orders.py  (ranked 4 of 6)
  prose    1.08  shop/tax.py  (ranked 5 of 6)
  prose   -1.99  shop/__init__.py  (ranked 6 of 6)
```

The lines are sorted by value, which is significance times need, so a file with a higher score can come later, as `shop/money.py` does here. [costs.md](costs.md#what-gets-prose) explains significance and need, and [costs.md](costs.md#--explain) lists the reasons.

With `--json`, from `devdocs estimate --max-cost 0.0007 --json -q`, with `demoted` and `items` shortened and `demoted` folded onto one line:

```json
{
  "files": 6,
  "prose_files": 2,
  "requests": 2,
  "cached": 0,
  "input_tokens": 4282,
  "output_tokens": 591,
  "worst_case_cost_usd": 0.000665,
  "cost_limit_usd": 0.0007,
  "request_limit": null,
  "demoted": [["architecture", ""], ["module", "shop"], ["file", "shop/money.py"]],
  "demoted_by": ["max_cost"],
  "items": [
    {
      "kind": "file",
      "key": "shop/cart.py",
      "model": "gemini-2.5-flash-lite",
      "input_tokens": 2262,
      "output_tokens": 335,
      "cost": 0.00036020000000000003,
      "cached": false,
      "requests": 1
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `files`, `prose_files` | Files planned; those with prose after the budget cut. |
| `requests` | Requests the uncached items need. |
| `cached` | Items whose answer is cached. |
| `input_tokens`, `output_tokens` | Upper bounds for the uncached items. |
| `worst_case_cost_usd` | Upper bound of the cost, rounded to 6 decimals. |
| `cost_limit_usd` | The `max_cost` in force, or `null` for no limit. |
| `request_limit` | The `max_requests` in force, or `null` for no limit. |
| `demoted` | The `[kind, key]` pairs the budget cut to stubs. |
| `demoted_by` | The limits that made the cut: `"max_cost"`, `"max_requests"`, both or none. |
| `items` | Every priced item that was kept: `kind` (`file`, `module`, `architecture`, or `embed` with semantic context), `key`, `model`, `input_tokens`, `output_tokens`, `cost` (not rounded), `cached` and `requests`. |

### Exit status

0 on success. 2 for a configuration error, 9 for a state database it cannot read, 130 or 143 on a stop.

## export

```text
devdocs export [path] [output options] [--force] [--json] [-q]
```

`export` renders the documents of the last `generate` again, from the manifest `index.json` in the output directory, in the formats configured or given with `--format`. It needs no key, calls no model, scans nothing and opens no state database. It only adds files: the manifest keeps the run's record, and its list of files grows. [output.md](output.md) describes the formats.

### Arguments

| Argument | Default |
|---|---|
| `path` | The project root. Without it, `$DEVDOCS_PATH`, else `path` in `devdocs.toml`, else the current directory. |

### Flags

`export` takes the [output options](#output-options), and these:

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--force` | none | off | Replaces files devdocs has no record of writing at the paths where this export writes its files. Your other files in the output directory are left alone. | none |
| `--json` | none | off | Prints the result as one JSON document on stdout. | none |
| `-q`, `--quiet` | none | off | Hides the progress lines on stderr. | none |

### Examples

```bash
devdocs export --format pdf
devdocs export --format html
devdocs export --format pdf --split
devdocs export --format pdf --json
```

### Output

stderr carries `read 8 documents from /home/me/shop/docs/index.json` and any rendering warnings; `-q` hides both. stdout:

```text
8 documents (run of 2026-11-09T10:56:57+00:00) as pdf in docs: 2 files written, 0 unchanged
  docs/index.json
  docs/shop.pdf
```

Each file written is listed on its own line when there are 10 or fewer. A last line counts the rendering warnings, if there were any.

With `--json`, from a second `--format pdf` export, which found both files unchanged:

```json
{
  "documents": 8,
  "formats": [
    "pdf"
  ],
  "out": "/home/me/shop/docs",
  "generated_at": "2026-11-09T10:56:57+00:00",
  "warnings": [],
  "written": {
    "written": [],
    "unchanged": [
      "index.json",
      "shop.pdf"
    ],
    "removed": []
  }
}
```

`out` is the absolute output directory. `generated_at` is when the exported run wrote its documents. `written` covers the manifest and the files this export rendered, and its `removed` is always empty.

Without a manifest, `export` exits 2:

```text
devdocs: error: no generated documentation in /home/me/shop/manual: index.json is missing
  hint: run `devdocs generate` first, or point --out at the directory it wrote
```

### Exit status

0 on success. 2 for a configuration error, a missing or unreadable manifest, or a write refusal. 1 when an external PDF engine fails or times out; the other formats are written first. 130 or 143 on a stop.

## doctor

```text
devdocs doctor [path] [--provider PROVIDER] [--api-key API_KEY] [--offline] [--json]
```

`doctor` checks that a run can work and prints one line per check on stdout, marked `ok` or `!!`. It needs no key. Without a key, or with `--offline`, it makes no network request. [troubleshooting.md](troubleshooting.md) explains what to do about each finding.

It checks, in order:

1. The interpreter: `python` (3.11 or newer), `syntax`, `sqlite`, `sqlite-threads`, `tls` and `certificates`. The `syntax` line notes newer Python syntax this interpreter cannot parse, and never blocks. `sqlite-threads` shows Python's `sqlite3.threadsafety` level, from 0 to 3: 0 blocks, and 3 means connections can be shared between threads.
2. `pdf-engines`: the PDF engines it finds. `builtin` is always among them.
3. `config`: the project's configuration, read as `generate` reads it. It shows the provider, quality, context, `max_cost`, `max_requests` and formats, with `no limit` for a limit of 0. A configuration error is reported here, and `doctor` exits 2 at once.
4. `pdf engine`: when `pdf` is a format and the configured engine is not installed.
5. `prices`: a warning, which does not block, when the catalog's prices were checked more than 180 days ago. It is in the text report only: the `--json` document has no key for it.
6. `anthropic-sdk`: with provider `anthropic-sdk` only, whether the `anthropic` package imports and has its client. This check also runs with `--offline`.
7. `api key`: where the key comes from (`from flag --api-key`, `from env GEMINI_API_KEY`, `from file /home/me/shop/devdocs.toml`). The key itself is never printed.
8. `provider`: with a key, and without `--offline`, it asks the provider for its model listing and reports how many models the key can use and which of them devdocs' catalog knows. It is skipped when the `anthropic` SDK is not usable.

### Arguments

| Argument | Default |
|---|---|
| `path` | The project root. Without it, `$DEVDOCS_PATH`, else `path` in `devdocs.toml`, else the current directory. |

### Flags

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--provider` | `gemini`, `anthropic`, `anthropic-sdk` | `gemini` | The provider to check. | `provider`, `DEVDOCS_PROVIDER` |
| `--api-key` | `API_KEY` | none | The key to check. See [`--api-key`](#--api-key). | `api_key` and the key variables; see [the API key](configuration.md#the-api-key) |
| `--offline` | none | off | Skips the provider check, the only step that uses the network. | none |
| `--json` | none | off | Prints the report as one JSON document on stdout. | none |

`doctor` has no `-q`.

### Examples

```bash
devdocs doctor --offline
devdocs doctor
devdocs doctor --provider anthropic --json
```

### Output

Real output of `devdocs doctor --offline` with no key, which exits 1:

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

With `--json`, the keys are, in order:

| Key | Content |
|---|---|
| `capabilities` | A list of `{"name", "ok", "value", "detail"}`, one per interpreter check. |
| `pdf_engines` | `{engine: executable}`. `builtin` maps to the Python interpreter. |
| `formats` | The configured formats. |
| `pdf_engine` | `{"configured": <engine>, "path": <executable or null>}`. `null` means the configured engine is not installed. |
| `anthropic_sdk` | With provider `anthropic-sdk` only: `{"name", "ok", "value", "detail"}`. |
| `api_key` | `{"found": <bool>, "origin": "flag" \| "env" \| "file" \| null, "where": <flag, variable or file path> \| null, "shared": <bool>}`. It never holds the key. `shared` is true only for a shared demo key; 4.0.0 ships none, so it is always `false`. |
| `models` | The model ids the key can use, when the provider check succeeded. |
| `provider_check` | `{"provider", "status": "ok" \| "failed" \| "skipped", "detail"}`. A skip's detail is `--offline`, `no API key` or `the anthropic SDK is not usable`. |

On a configuration error, the JSON holds only `capabilities`, `pdf_engines` and `config`, the error text with its hint, and `doctor` exits 2. It is the only error that ends a command and still prints JSON.

### Exit status

| Code | When |
|---|---|
| 0 | Nothing blocks a run. A stale-prices warning does not block. |
| 1 | Something blocks a run: a failed interpreter check, a configured PDF engine that is missing, an unusable `anthropic` SDK, no key, or a failed provider check. A key the provider rejects gives 1 here, not 3. |
| 2 | A configuration error. |
| 130, 143 | A stop. |

## init

```text
devdocs init [path] [--provider PROVIDER] [--force]
```

`init` writes a starter `devdocs.toml` in the project root. It reads no configuration: it never reads an existing `devdocs.toml` or any `DEVDOCS_*` variable but `DEVDOCS_PATH`, and it checks nothing but the path. It does not store a key and asks no questions.

If the project root has a `.gitignore` that does not list `.devdocs/`, `init` appends that line, in the file's own line ending, and changes nothing else. It takes `.devdocs/` as listed when it appears anywhere in the file as a word of its own, even in a comment. It creates no `.gitignore`. A `.gitignore` that is a symbolic link, or not a regular file, is refused with exit 2 after `devdocs.toml` is written; git does not read a `.gitignore` that is a link.

### Arguments

| Argument | Default |
|---|---|
| `path` | The project root. Without it, `$DEVDOCS_PATH`, else the current directory. |

### Flags

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--provider` | `gemini`, `anthropic`, `anthropic-sdk` | `gemini` | The provider the file names. Only this flag sets it. | none |
| `--force` | none | off | Overwrites an existing `devdocs.toml`. This differs from the `--force` of `generate` and `export`. | none |

`init` has no `--json` and no `-q`.

### Examples

```bash
devdocs init
devdocs init app --provider anthropic
devdocs init --force
```

### Output

stderr reports what was written, and stdout gives the next command, ready to paste:

```text
wrote /home/me/app/devdocs.toml
added .devdocs/ to .gitignore (local cache and run ledger)
next: devdocs estimate app
```

The `next:` line quotes a path that needs quoting, puts `--` before one that starts with `-`, uses the value of `DEVDOCS_PATH` when no path was given, and is plain `next: devdocs estimate` when there is neither. On Windows the path is written with forward slashes.

The file it writes, with `--provider gemini`:

```toml
# devdocs configuration. Command-line flags override this file.
[devdocs]
provider = "gemini"       # gemini | anthropic | anthropic-sdk
quality = "cheap"            # cheap | balanced | high
# context = "standard"       # off | standard | semantic (Gemini embeddings)
max_cost = 0.50              # USD ceiling per run; 0 disables it
out = "docs"
# formats = ["markdown"]     # any of markdown, html, pdf
# pdf_engine = "builtin"     # builtin, or chrome | weasyprint for all of Unicode
# pdf_split = false          # one PDF per document instead of one book
# page_size = "A4"           # A4 | Letter
# model = ""                 # pin one model for every call
# include = ["scripts"]      # take source in despite ignores
# exclude = ["examples/"]    # extra gitignore-style excludes
# free_tier = true           # pace requests to free-tier limits
```

[configuration.md](configuration.md#annotated-example) has an example with every setting.

`init` refuses these with exit 2:

| Message | Hint |
|---|---|
| `<root>/devdocs.toml already exists` | `pass --force to replace it` |
| `cannot write <root>/devdocs.toml: it links to <target>, which leads nowhere` | `remove the link, or make the file it names and pass --force` |
| `cannot write <root>/devdocs.toml: <reason>` | none |
| `cannot add .devdocs/ to <root>/.gitignore: <reason>` | `<root>/devdocs.toml is written; add .devdocs/ to .gitignore yourself` |

For `.gitignore`, the reason is `it is a symbolic link, which git does not read`, `it is not a regular file`, or the system's own error. The project root is checked as for every command; [configuration.md](configuration.md#paths) lists those messages.

### Exit status

0 on success. 2 for a bad project root, an existing `devdocs.toml` without `--force`, a `devdocs.toml` that is a symbolic link leading nowhere (refused even with `--force`), a write that fails, or a `.gitignore` it cannot add the entry to. 130 or 143 on a stop.

## models

```text
devdocs models [--provider PROVIDER] [--api-key API_KEY] [--json]
```

`models` lists every model in devdocs' catalog for the provider, and whether your key can use it, with its tier and price. It needs a key and asks the provider for its model listing. It has no path argument: it looks for `devdocs.toml` from `$DEVDOCS_PATH`, else from the current directory. [models.md](models.md) explains tiers and how models are chosen.

### Flags

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--provider` | `gemini`, `anthropic`, `anthropic-sdk` | `gemini` | The provider to list. `anthropic-sdk` lists the Anthropic models. | `provider`, `DEVDOCS_PROVIDER` |
| `--api-key` | `API_KEY` | none | The key to list with. See [`--api-key`](#--api-key). | `api_key` and the key variables; see [the API key](configuration.md#the-api-key) |
| `--json` | none | off | Prints the listing as one JSON document on stdout. | none |

`models` has no `-q`.

### Examples

```bash
devdocs models
devdocs models --provider anthropic --json
```

### Output

stdout holds a table with one row per catalog model. The columns are `model`, `tier` (`file`, `module`, `architecture` or `embed`), `access` (`yes` when the key's listing includes the model, by its id or a dated snapshot of it), and the price in US dollars per million tokens, `$/Mtok in` and `out`. A last line, `(+N other models; pin one with --model)`, counts the listed ids that match no catalog model.

With `--json`, stdout holds one object with two keys:

| Key | Content |
|---|---|
| `catalogued` | One object per catalog model: `id`, `tier`, `available` (a boolean), `input_per_mtok` and `output_per_mtok` (US dollars per million tokens), `free_rpm` and `free_rpd` (the free tier's requests per minute and per day, or `null` for a model with no free tier). |
| `available` | The sorted model ids the provider listed for the key. |

### Exit status

0 on success. 2 for no key, a configuration error, or `anthropic-sdk` without the `anthropic` package. 3 when the provider rejects the key, 4 for a network failure or a server error, 5 for a rate limit, 6 for a 404, 10 for a spent quota, a spend cap or a billing problem. 130 or 143 on a stop.

## cache

```text
devdocs cache {stats,clear} [path] [--json]
```

`cache` works on the response cache in `.devdocs/devdocs.db`:

- `stats` counts the cached responses and what they cost when they were bought.
- `clear` deletes the cached responses. It leaves the run ledger, the semantic vectors and the daily usage counts as they are. The next `generate` pays for every answer again.

`cache` never creates `.devdocs/`. It reads `devdocs.toml` and the `DEVDOCS_*` variables to find the project, and stops with exit 2 when they are invalid. [costs.md](costs.md) explains what the cache saves.

### Arguments

| Argument | Default |
|---|---|
| `stats` or `clear` | Required. |
| `path` | The project root. Without it, `$DEVDOCS_PATH`, else `path` in `devdocs.toml`, else the current directory. |

### Flags

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--json` | none | off | Prints the result as one JSON document on stdout. | none |

`cache` has no `-q`.

### Examples

```bash
devdocs cache stats
devdocs cache stats --json
devdocs cache clear
```

### Output

| Command | Text | JSON |
|---|---|---|
| `cache stats` | `8 cached responses, bought for $0.0020 in total` | `{"entries": 8, "cost_usd": 0.001969}` |
| `cache clear` | `removed 8 cached responses` | `{"removed": 8}` |
| `cache stats`, no state yet | `0 cached responses, bought for $0.0000 in total` | `{"entries": 0, "cost_usd": 0.0}` |
| `cache clear`, no state yet | `nothing to clear: /home/me/shop has no cache yet` | `{"removed": 0}` |

The table shows each JSON object on one line; `--json` prints it indented. `cost_usd` is not rounded.

### Exit status

0 on success, also with no state. 2 for a configuration error, 9 for a state database it cannot read or write, 130 or 143 on a stop.

## Options several commands share

### Output options

`generate`, `estimate` and `export` take these. [output.md](output.md) describes the formats, the PDF engines and the output tree.

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `path` | directory | see below | The project root. | `path`, `DEVDOCS_PATH` |
| `--out` | directory | `docs` | The output directory, relative to the project root, not to the current directory. An absolute path stays absolute, and a leading `~` is expanded. It cannot be the project root or a directory above it. | `out`, `DEVDOCS_OUT` |
| `--format` | `markdown`, `html`, `pdf` | `markdown` | The formats to render. Repeat the flag or separate names with commas. Names are case-sensitive. | `formats`, `DEVDOCS_FORMATS` |
| `--pdf-engine` | `builtin`, `chrome`, `weasyprint` | `builtin` | What prints the PDF. `chrome` (Chrome, Chromium or Edge) and `weasyprint` cover all of Unicode, and must be installed. | `pdf_engine`, `DEVDOCS_PDF_ENGINE` |
| `--page-size` | `A4`, `Letter` | `A4` | The PDF paper size, in any letter case. | `page_size`, `DEVDOCS_PAGE_SIZE` |
| `--split`, `--no-split` | none | no split | One PDF per document instead of one book. Only the built-in engine splits. The last of the two flags wins. | `pdf_split`, `DEVDOCS_PDF_SPLIT` |

Without `path`, the project root is `$DEVDOCS_PATH`, else `path` in `devdocs.toml`, else the current directory.

On the command line, `--pdf-engine`, `--page-size`, `--split` and `--no-split` need `pdf` among the formats. Otherwise the command exits 2:

```text
devdocs: error: --split applies only to PDF output, which is not among the formats: markdown
  hint: add --format pdf
```

The same settings in `devdocs.toml` or the environment are accepted without PDF output, so a project can keep its PDF preferences there.

### Run options

`generate` and `estimate` take these. [configuration.md](configuration.md#settings) gives each setting's validation, and [models.md](models.md), [costs.md](costs.md) and [context.md](context.md) explain what they do.

| Flag | Value | Default | Effect | Setting |
|---|---|---|---|---|
| `--provider` | `gemini`, `anthropic`, `anthropic-sdk` | `gemini` | Whose models write the prose. `anthropic-sdk` needs the `anthropic` extra. | `provider`, `DEVDOCS_PROVIDER` |
| `--model` | model id | none | Uses this one model for every call. A model the catalog does not know is priced at its vendor's highest rates. | `model`, `DEVDOCS_MODEL` |
| `--quality` | `cheap`, `balanced`, `high` | `cheap` | How much prose, from how strong a model. | `quality`, `DEVDOCS_QUALITY` |
| `--context` | `off`, `standard`, `semantic` | `standard` | Cross-file context in each prompt. `standard` sends no extra request, but adds input tokens. `semantic` adds paid Gemini embeddings and needs provider `gemini`. | `context`, `DEVDOCS_CONTEXT` |
| `--max-cost` | US dollars | `0.5` | Stops before spending more than this. `0` means no limit, not zero spend. | `max_cost`, `DEVDOCS_MAX_COST` |
| `--max-requests` | integer | `0` | Stops before sending more requests than this, retries included. `0`, the default, means no limit. | `max_requests`, `DEVDOCS_MAX_REQUESTS` |
| `--concurrency` | integer, at least 1 | `4` | Requests to a model at one time. | `concurrency`, `DEVDOCS_CONCURRENCY` |
| `--include` | `PATH` | none | Takes in source files that ignores, `--exclude` or the 1,000,000-byte size limit leave out. Literal text in any case, not a glob. Repeat or comma-separate. | `include`, `DEVDOCS_INCLUDE` |
| `--exclude` | `PATTERN` | none | Leaves files out, each pattern read as a line of a `.gitignore`. Repeat or comma-separate. What `--include` takes in stays. | `exclude`, `DEVDOCS_EXCLUDE` |
| `--per-file` | none | off | Uses the v3 layout, `docs/<dir>/<name>.md`. | `per_file`, `DEVDOCS_PER_FILE` |
| `--paid` | none | free tier | Paces to paid-tier rate limits, and lets Gemini use models outside its free tier. | `free_tier = false`, `DEVDOCS_FREE_TIER=false` |
| `--name` | text | the project's own name | The project name in the docs. | `name`, `DEVDOCS_NAME` |
| `--description` | text | none | A one-line project description. | `description`, `DEVDOCS_DESCRIPTION` |

No flag undoes `--paid` or `--per-file`. When `devdocs.toml` sets `free_tier = false` or `per_file = true`, override it with `DEVDOCS_FREE_TIER=true` or `DEVDOCS_PER_FILE=false`.

### `--api-key`

`generate`, `doctor` and `models` take `--api-key API_KEY`. It overrides every other source of a key, as [configuration.md](configuration.md#the-api-key) describes. A key typed on the command line can end up in your shell history, so prefer an environment variable, and use a secret in CI. `estimate`, `export`, `init` and `cache` need no key and refuse the flag with exit 2. The error never shows the key: `devdocs estimate . --api-key KEY` prints `devdocs: error: unrecognized arguments: --api-key ***`.

### `--provider`

`generate`, `estimate`, `doctor` and `models` read the provider like any setting: flag, then `DEVDOCS_PROVIDER`, then `devdocs.toml`, then `gemini`. `init` takes only its flag, which defaults to `gemini`.

### `--force`

On `generate` and `export`, `--force` replaces files devdocs has no record of writing at the paths where it writes its own files. Without it, devdocs refuses to replace them. With or without it, your other files in the output directory are left alone. [output.md](output.md) explains the manifest and the rule. On `init`, `--force` overwrites an existing `devdocs.toml`.

### `--json` and `-q`

stdout carries only the result: text lines, or one JSON document with `--json`. stderr carries everything else: progress, warnings and errors.

| Command | `--json` | `-q`, `--quiet` |
|---|---|---|
| `generate` | yes | yes |
| `estimate` | yes | yes |
| `export` | yes | yes |
| `doctor` | yes | no |
| `init` | no | no |
| `models` | yes | no |
| `cache` | yes | no |

- `--json` without `-q` still writes progress to stderr; stdout stays clean.
- `-q` hides progress lines only. Lines that start with `warning:` and errors still show.
- `--json` does not turn errors into JSON. The one exception is `doctor`'s configuration error.

An error that ends a command prints `devdocs: error: <message>` on stderr, and a second line, `  hint: <what to do>`, when there is a hint. A usage error prints the usage first and exits 2. A value that a command's flag refuses names the command, as in `devdocs estimate: error: argument --quality: invalid choice: ...`; an unknown flag gives `devdocs: error: unrecognized arguments: ...`. An unexpected failure prints `devdocs: internal error: <type>: <message>` and exits 1; set `DEVDOCS_DEBUG=1` to see its traceback.

## v3 command lines

devdocs 3 was one command driven by flags. A command line that does not start with a v4 command and uses a flag only v3 had (`-p`, `--path`, `--output`, `--overwrite`, `--authors` or `--keywords`, written in full, with or without `=value`) is translated into `devdocs generate <path> --per-file --out <out>` and run, with the pages where v3 put them. A two-line notice on stderr shows the command it runs, with any key masked. It appears even with `-q`:

```text
devdocs: v3-style flags are deprecated; running `devdocs generate . --per-file --out docs --api-key '***' -q`
devdocs: v4 never replaces a page it has no record of writing: if docs/ still holds v3 pages, add --force once to take them over
```

A command line that starts with a v4 command is never translated: `devdocs generate --path x` is an error, because v4 takes the project as the positional `path`.

[migration.md](migration.md#v3-command-lines) shows how each argument of a v3 line is translated, and [Your existing docs tree](migration.md#your-existing-docs-tree) what a run does over the pages v3 wrote.

## Exit codes

The exit codes are stable, so scripts and CI can act on them.

| Code | Meaning | Typical cause | What to do |
|---|---|---|---|
| 0 | Success | The command did its work. Also `--help`, `--version`, and `doctor` with nothing blocking. A `generate` whose plan was cut to fit the budget also exits 0. | Nothing. |
| 1 | Unexpected error, or `doctor` found a blocking problem | `doctor`: a blocking `!!` line. `export`: an external PDF engine failed or timed out. Otherwise a bug, printed as `devdocs: internal error: ...`. | Fix what `doctor` marks. For an engine, run it by hand or use `--pdf-engine builtin`. For an internal error, run again with `DEVDOCS_DEBUG=1` and report the traceback. |
| 2 | Configuration or usage error | A bad flag or value, an invalid `devdocs.toml` or `DEVDOCS_*` variable, no API key, a missing project root, an `out` that is the project root, PDF options without PDF output, a PDF engine that is not installed, provider `anthropic-sdk` without the `anthropic` package, `export` without a manifest, a refusal to replace files devdocs did not write, `init` over an existing file, or no model left to call, as when the key reaches no catalogued model or a pinned `--model` does not exist. | Read the message and its hint. [configuration.md](configuration.md#validation) lists the configuration messages. Retrying unchanged does not help. |
| 3 | The provider rejected the key | A wrong or revoked key, or one without access, that the provider rejects. `doctor` reports this with exit 1 instead. | Check the key and the variable it comes from; `devdocs doctor` shows the source. |
| 4 | Provider or network failure, or failed items | `generate`: at least one item or format failed; the rest was written. Otherwise a network failure or a server error while listing models, or a key that holds a line break or another character an HTTP header cannot carry, which devdocs refuses before any connection. | Run again: answers already received are cached. Check the network, proxy and TLS settings with `devdocs doctor`. For a key with stray characters, set the key again from a clean copy; retrying does not help. |
| 5 | Rate limited | A 429 from the provider's model listing, which is not retried. Inside a run, a rate-limited call is retried. | Wait, then run again. |
| 6 | Model not found | A 404 from the provider's model listing. Inside a run, a missing model is dropped and the next one is tried. | Run `devdocs doctor`, which asks for the listing again. A mistyped `--model` does not give 6: the run drops it and, with no model left, exits 2. |
| 7 | Budget stop | `generate` reached `--max-cost` or `--max-requests` partway through the run, as when retries use more requests than were planned. The rest became stubs. | Run again to buy the rest, or raise the limit; 0 means no limit, not zero spend. |
| 8 | Unused | Never returned. | Nothing. |
| 9 | State database error | `.devdocs/devdocs.db` cannot be created, read or written, is damaged, is locked by another devdocs, or has a newer schema. | Follow the hint: wait for the other process, free disk space, fix permissions, upgrade devdocs, or delete `.devdocs/` to rebuild it. |
| 10 | Quota or billing stop | The provider's daily quota is spent, or a spend cap or billing problem. | Run `devdocs generate` again once the quota resets, or use a paid key; it resumes where it stopped. For a spend cap or a billing problem, fix the account's limit or billing first. |
| 130 | Stopped by SIGINT | Ctrl-C, `docker stop`, or `docker compose stop`. | Run the command again. A `generate` resumes, and its cached answers are free. |
| 143 | Stopped by SIGTERM | `kill`, `timeout`, or a service manager stopping the process. | Run the command again. A `generate` resumes, and its cached answers are free. |

After a graceful stop, the code is 128 plus the first signal's number. After a hard stop by a second signal, it is 128 plus the second signal's number, so SIGINT followed by SIGTERM exits 143.

The npm launcher passes devdocs' exit code through, and exits with 128 plus the signal's number when devdocs was ended by a signal. It exits 1 itself when it cannot start devdocs: no Python 3.11 or newer, its bundled package missing, a package folder whose path holds the PATH separator (`:`, or `;` on Windows), or a Python that fails to start. A `devdocs:` line says which. [install.md](install.md#npm) explains how it finds Python.

## How a run stops

devdocs handles SIGINT (Ctrl-C) and SIGTERM in every command. Nothing configures this behaviour. A signal that was already ignored when devdocs started, as for a background job in a non-interactive shell, stays ignored.

### Outside the paid part

Every command but `generate`, and `generate` before it starts paying (while it reads the configuration, lists the models, scans the project and checks the output), stops at once on the first signal. It prints what the stop left behind and exits 130 after SIGINT or 143 after SIGTERM:

| Command | Message |
|---|---|
| `generate`, before paying | `warning: interrupted; nothing was bought and no documents were written` |
| `estimate` | `warning: interrupted; nothing was sent, so nothing was spent` |
| `export` | ``warning: interrupted; each file written so far is whole; export again for the rest, for free`` |
| `doctor` | `warning: interrupted; the checks did not finish; nothing was changed` |
| `models` | `warning: interrupted; nothing was changed` |
| `cache stats` | `warning: interrupted; the cache is as it was` |
| `cache clear` | ``warning: interrupted; the cache is empty or as it was; `devdocs cache stats` tells which`` |
| `init`, before it writes | `warning: interrupted; nothing was written` |
| `init`, once it writes | `warning: interrupted; its files may be unfinished; run it again with --force to finish them` |

If a stop ends `generate` at once after its paid part began, the message is `warning: interrupted; documents written so far are kept, run again to resume`.

### In the paid part of generate

The paid part begins once the output check has passed, just before the first embedding or model call, and ends when the last page is written. The first signal there stops the run gracefully:

- It prints `stopping: requests already sent finish and are kept, then the pages are written; interrupt again to stop at once`. This is a progress line, so `-q` hides it.
- No new request starts. Nothing is retried or escalated, and waits for a backoff, a `Retry-After` or a rate limit end at once.
- Requests already sent finish. Each read or write on their connection times out after 120 seconds. Their answers are paid for and cached.
- The remaining items still use the cache: an answer an earlier run paid for still makes its page. The others become stubs.
- Every page is written, and the run is recorded as interrupted.
- The summary is printed, with `stopped early (interrupted); run again to resume`, or `"stopped": "interrupted"` with `--json`.
- The exit code is 130 after SIGINT and 143 after SIGTERM.

A second signal within 1 second of the first counts as the same request and is ignored. A signal sent to a process group can reach devdocs twice, once directly and once through the npm launcher.

A later second signal stops at once:

- It prints `warning: stopped at once; the answers received are cached, so the next run writes their pages for free`. `-q` does not hide it.
- The process exits immediately with 128 plus that signal's number. Requests still on the wire are abandoned, and no more pages are written.

### The next run

The next `generate` with the same settings resumes the stopped run and prints `resumed run <id>`. It uses every answer already cached for free, including those that arrived before the stop, and buys only what is missing. In a test with a scripted model, a run stopped while one call was on the wire and another waited to retry: it wrote 8 documents, 1 with prose, and the next run made 7 model calls and found 1 cache hit.

### Containers and the npm launcher

The image's `STOPSIGNAL` and compose's `stop_signal` are SIGINT, so `docker stop` and `docker compose stop` take the graceful path and devdocs exits 130. Allow at least 150 seconds before the container is killed, for the calls on the wire: `docker stop -t 150`, or `docker run --stop-timeout 150`. compose sets `stop_grace_period: 150s`. Docker's default is 10 seconds. SIGTERM also stops devdocs gracefully, with exit 143, but the kernel drops a SIGTERM sent to process 1 in a container before devdocs has installed its handler.

The npm launcher always passes SIGTERM on to devdocs. It passes SIGINT on only when it has no controlling terminal and is not on Windows, since a terminal's Ctrl-C reaches devdocs directly. [ci.md](ci.md) covers cancelled CI jobs.
