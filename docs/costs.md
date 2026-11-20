# Costs

devdocs pays a model only for prose, plus embeddings when you turn on semantic context, and it caps what one run may spend. Three facts matter most:

- Every run has a cost ceiling: $0.50 by default, set with `--max-cost`. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend.
- `devdocs estimate` prices a run before you pay for it. It needs no key and sends nothing.
- A re-run pays only for what changed. Every answer is cached in `.devdocs/` and reused for free; see [What a re-run pays for](#what-a-re-run-pays-for).

This page explains where the money goes, how the budget guards work, how to read `estimate`, and how to keep runs cheap.

## What a run sends to a model

Every documentable file gets a page. For each one, the planner decides whether a model writes its prose or the page is built from parsed facts alone: signatures, members, dependencies, entry points and known issues. A page built from facts alone is called a stub, and it costs zero tokens. The planner is deterministic, so `estimate` and `generate` plan the same calls.

### What gets prose

Files are ranked by value: significance times need.

- **Significance** rises with the number of files that import the file, its public symbols, an explicit export list, being an entry point, its size, a module docstring over 200 characters, and its git commits and authors in the last 90 days. It falls for a file with no public symbols and for a private module, such as `_util.py`.
- **Need** is how much a model would add. A file whose module docstring has at least 80 characters, and whose every public symbol and public class member has a docstring, needs nothing: it becomes a stub, `fully documented in source`. Any gap gives a need of at least 0.25.

The quality level sets how many of the ranked files get prose:

| Quality | Files with prose |
|---|---|
| `cheap` (default) | every ranked file when there are at most 25; else 20% of them, at least 25 |
| `balanced` | every ranked file when there are at most 40; else 35% of them, at least 40 |
| `high` | every ranked file when there are at most 60; else 60% of them, at least 60; fully documented files are ranked too |

The rest of the plan is the same at every quality:

- Files in a language devdocs has no parser for, and files that fail to parse, are stubs.
- Each file prompt asks the model to describe only the file's undocumented names, at most 25 of them.
- Each directory with at least two documentable files is ranked as a module. The top 8 get a summary, or the top quarter when that is more.
- The project gets one architecture overview when its documentable files span at least two directories, or number at least five.

[models.md](models.md#quality-levels) explains how quality also chooses stronger models. `devdocs estimate --explain` shows the plan and the reason for every file; see [--explain](#--explain).

### Routing and output caps

At the default quality, each prompt goes first to the cheapest model of its tier: a file page to the file-tier model, a module summary to the module-tier model, the overview to the architecture-tier model. A stronger model is called only when the cheaper one's answer is unusable, at most once per tier; [models.md](models.md#escalation) gives the rules.

Each call's output is capped, so its cost has a bound:

| Prompt | Output cap, in tokens |
|---|---|
| file | 200 plus 45 per name to describe, between 256 and 1,400 |
| module | 600 |
| architecture | 1,500 |

A file prompt carries its source up to about 6,000 tokens; a longer source is cut to fit, as [context.md](context.md#what-every-prompt-carries) describes. Thinking is turned off, or kept as low as each model allows. None of these limits is configurable.

## The cache and resume

### What a re-run pays for

Every usable answer is stored in `.devdocs/devdocs.db` under the project root the moment it is paid for, so a crash or a Ctrl-C keeps what was bought. The cache belongs to the project, not to the user, and nothing expires from it.

An answer is stored under a key made of the provider, the model, the system prompt, the prompt without its retrieved context, the output cap, the temperature and the effort. An unchanged file builds an identical prompt, so its answer is free. On a project whose answers are all cached, `estimate` prints:

```text
6 files: 6 with prose, 0 from facts alone
0 model calls (none), 8 already cached
at most 0 input / 0 output tokens, at most $0.0000
```

A document is bought again when its prompt changes:

- the file's source changes;
- a fact in its prompt changes: the project's name or description, the line count, the files that import it or test it, its imports, exports or entry point, or the names to describe;
- other models must answer it: another provider, a higher quality, which starts on a stronger tier, or a pinned model;
- a devdocs release changes the system prompts.

On the Gemini free tier, a prompt whose starting tier has no free model the key can reach is answered on loan by the strongest free model below that tier ([models.md](models.md#free-tier-pacing-and---paid)). A prompt's starting tier is the one its quality level sets ([models.md](models.md#quality-levels)). The key of an answer on loan also names the tier it served, so later runs reuse it, `--paid` ones included, and `estimate` counts it as cached.

The architecture tier has one free model, `gemini-2.5-pro`. Google serves the 2.5 models only to projects that used them before, so a key from any other project, a new one included, cannot reach it ([models.md](models.md#gemini-models)). With such a key, a module-tier model writes the overview on loan, and at `--quality high` every page.

A module summary carries the purposes of its files, and the overview carries those of the modules. Buying a file can therefore change its module's prompt and the overview's. When the new purposes match the old ones, those two are still cache hits.

These leave every cache key as it was, so they buy nothing again: `--paid`, `--context`, `--max-cost`, `--max-requests`, `--concurrency`, `--format`, `--per-file` and `--no-resume`.

`devdocs cache stats` shows how many answers the cache holds and what they cost; `devdocs cache clear` deletes them all. There is no way to clear one file's answers. See [commands.md](commands.md#cache).

### Resuming an interrupted run

A run that stops early, on its budget, on a quota or on Ctrl-C, or that leaves an item failed, stays open in the project's run ledger. The next run with the same project root, provider, quality, `include` and `exclude` reopens it and prints `resumed run <id>`. Like any run, it pays nothing for the answers the cache can serve; see [What a re-run pays for](#what-a-re-run-pays-for).

- `--no-resume` starts a new run record, but it still uses the cache. To buy unchanged answers again, run `devdocs cache clear` or delete `.devdocs/`.
- The first Ctrl-C in the paid part of a run sends nothing new: calls already sent finish and are kept, every page is written, and devdocs exits 130. [commands.md](commands.md#how-a-run-stops) describes stopping.
- The budget does not carry over. Each run, resumed or not, starts with its full ceiling.

## The cost of context

Each prompt can carry context from other files; [context.md](context.md) explains the modes. Standard context, the default, sends no extra requests and costs only input tokens. Semantic context first embeds the code; later runs embed only what changed, and embedding may take at most half of each limit. Changing the mode never buys a cached answer again. [context.md](context.md#context-cost-and-the-cache) gives what each mode cost on a copy of this repository.

## Budget guards

- `--max-cost USD` (setting `max_cost`) stops a run before it spends more than this many US dollars. The default is 0.50.
- `--max-requests N` (setting `max_requests`) stops a run before it sends more than N requests to a model. The default is 0.

`--max-cost 0` and `--max-requests 0` turn that guard off; they do not mean zero spend. Both guards apply to each run on its own: nothing carries over between runs. [commands.md](commands.md#run-options) lists the flags, and [configuration.md](configuration.md#settings) the settings and their environment variables.

### What counts

- Before each call is sent, devdocs reserves its worst case: one token per byte of the system prompt and the prompt, plus 64 for the message framing, priced as a write to the prompt cache (1.25 times the input rate on Claude), plus the full output cap. A call to a Claude model with server-side fallbacks is reserved twice over: Anthropic can answer a declined request with another model and bill the declined attempt as well ([models.md](models.md#how-the-rates-apply)). The call goes out only if the money already spent, plus what other calls in flight hold, plus this reservation fits under `--max-cost`. Calls in flight together therefore cannot overshoot the ceiling.
- After the call, its real cost replaces the reservation: the usage the provider reports, priced from the [catalog](models.md#the-model-catalog). That is usually well below the reservation, so a run spends less than its ceiling. Embedding batches are the exception: the endpoint reports no usage, so each is booked at its worst case.
- Free tier or not, every call is priced at the catalog's rates, which are the vendors' paid rates. So `estimate` and the cost in `generate`'s summary show, and `--max-cost` limits, what the calls cost at paid rates. devdocs does not read the vendor's bill.
- `--max-requests` counts every request sent to a model: retries, escalations, failed attempts and embedding batches included.
- A request that fails releases its money but still counts as a request.
- A cached answer costs nothing and counts against neither guard. Nor does the model listing `generate` asks for before it plans.

### Before the run: the plan is cut to fit

devdocs prices every planned call and keeps what fits, in priority order: the architecture overview, then module summaries by rank, then files by value. An item that does not fit becomes a stub, and the walk goes on, so a later, cheaper item may still fit.

A file bought under a cached module summary also asks that summary again, and the overview too if it is cached. The file is kept only if those calls fit as well. So under `--max-requests 1` or `2` such files can stay stubs, while a limit of 3 or more always lets a run make progress.

`estimate` reports the cut and the limit that made it:

```text
29 items left as stubs to fit --max-cost $0.01
```

`generate` cuts the plan the same way but prints no line about it. Run `estimate` first to see the cut. Cutting the plan is not a stop: a run whose plan was cut to fit still exits 0.

Each run starts with its full ceiling, and the cache keeps what earlier runs bought. Running again with the same ceiling therefore buys the next items the last run cut, until what is left does not fit the ceiling even on its own.

Semantic context may take at most half of each limit for embedding, or it is skipped; see [context.md](context.md#cost-and-cap).

### During the run: a stop at the limit

Retries and escalations are not in the plan. When one of them would pass a guard, the run stops on its budget:

- no new prompt is sent; the prompts already in progress finish, and their answers are cached;
- later waves are skipped: the waves are files, then module summaries, then the overview, so a stop among the files leaves the summaries and the overview as stubs;
- every page is written, the rest from facts alone;
- stderr shows the reason, such as `stopped: request limit of 8 reached` or `stopped: next call (up to $<cost>) would pass the $<limit> limit; $<spent> spent so far`;
- the result says `stopped early (budget); run again to resume`, and devdocs exits 7.

The next run resumes and buys the rest. A quota stop works the same way and exits 10; see [models.md](models.md#daily-quotas). [commands.md](commands.md#exit-codes) lists every exit code.

## estimate

```sh
devdocs estimate
```

`estimate` plans and prices a run as `generate` would, then stops. It needs no key, sends nothing, and writes nothing: it does not even create `.devdocs/`. It takes the same run options as `generate`, so you can price any variant, plus `--explain`. It does not take `--api-key`.

On a copy of this repository, without its git history:

```text
$ devdocs estimate
scanned 144 files, 60 documentable, 34 model calls planned (0.3s)
60 files: 25 with prose, 35 from facts alone
34 model calls (1 architecture, 25 files, 8 modules), 0 already cached
at most 485,425 input / 17,948 output tokens, at most $0.0920
```

Line by line:

1. `scanned …` goes to stderr, and `-q` hides it. It counts the calls planned before the budget cut.
2. The files and how many get prose, after the cut.
3. The calls the run would make, by kind, and the answers already cached.
4. The worst-case tokens and cost of those calls.

Two more lines appear when they apply: [the free-tier line](#the-free-tier-line), and the cut line of [Before the run](#before-the-run-the-plan-is-cut-to-fit).

### What "at most" means

The figure is a worst case for the planned calls:

- each byte of a prompt counts as a token, which is 2.5 to 4 times a typical count;
- input is priced as a write to the prompt cache, and output at its full cap;
- each call is priced on the first model its tier would use. `estimate` does not ask which models your key can reach. A key that cannot reach that model usually gets a stand-in, another model of the same tier ([models.md](models.md#the-model-catalog)), and pays its rates: for a Gemini file page, `gemini-3.1-flash-lite` at $0.25 and $1.50 per million tokens instead of `gemini-2.5-flash-lite` at $0.10 and $0.40;
- escalations and retries are not included.

After you edit a file, `estimate` prices the file's module summary and the overview as calls too, because their prompts carry the file's new purpose, which only the run learns. A run that gets the same purpose back finds them cached. After a one-line edit to one file of a fully cached project:

```text
6 files: 6 with prose, 0 from facts alone
3 model calls (1 architecture, 1 file, 1 module), 5 already cached
at most 6,037 input / 2,356 output tokens, at most $0.0199
```

### --explain

`--explain` adds one line per planned file: its plan, significance score, path and reason. On the same copy of this repository, the first lines and some of the rest:

```text
  prose   11.33  devdocs/parse/languages.py  (ranked 1 of 52)
  prose   14.88  devdocs/render/model.py  (ranked 2 of 52)
  prose   11.97  devdocs/render/pdf/layout.py  (ranked 3 of 52)
  stub     7.50  devdocs/llm/factory.py  (below coverage cut)
  stub    13.06  devdocs/render/text.py  (fully documented in source)
  stub    -0.84  scripts/install.sh  (no parser for shell)
```

Files are ranked by value, significance times need, not by significance alone. The reasons are:

| Reason | Meaning |
|---|---|
| `ranked <i> of <n>` | prose: within the coverage cut |
| `below coverage cut` | stub: ranked, but outside the cut for this quality |
| `fully documented in source` | stub: nothing for a model to add |
| `no parser for <language>` | stub: devdocs has no parser for the language |
| `parse failed` | stub: the parser could not read the file |
| `over budget` | stub: cut to fit `--max-cost` or `--max-requests` |

At `high`, a fully documented file is ranked anyway, and its reason ends `(documented in source; included at need 0.25)`. Module summaries and the overview are not listed.

### --json

`devdocs estimate --json` prints the same figures as one JSON object. Among its keys, `worst_case_cost_usd` is the plan's worst case, `cost_limit_usd` and `request_limit` are the guards (`null` for no limit), `demoted` and `demoted_by` list what the cut removed and which limit cut it, and `items` gives each priced item that was kept, cached or not, with its model, tokens and cost. [commands.md](commands.md#estimate) gives the complete shape.

### Other settings

`estimate` takes every run option, so you can compare before you choose. On the same copy of this repository:

| Flags | Files with prose | Model calls | Cost, at most | Cut to fit |
|---|---|---|---|---|
| none | 25 | 34 | $0.0920 | none |
| `--provider anthropic` | 25 | 34 | $0.4090 | none |
| `--quality balanced` | 40 | 49 | $0.2871 | none |
| `--quality high` | 16 | 25 | $0.4998 | 43 items, `--max-cost $0.5` |
| `--quality high --max-cost 0` | 59 | 68 | $1.4618 | none |
| `--provider anthropic --quality balanced` | 2 | 11 | $0.4769 | 38 items, `--max-cost $0.5` |
| `--context off` | 25 | 34 | $0.0831 | none |
| `--context semantic` | 25 | 48, 14 of them embedding batches | $0.2332 | none |
| `--max-requests 20` | 11 | 20 | $0.0612 | 14 items, `--max-requests 20` |
| `--max-cost 0.01` | 2 | 5 | $0.0099 | 29 items, `--max-cost $0.01` |

At `high`, every call goes to the architecture-tier model, so the default ceiling buys fewer files than at `cheap`. With Claude, the module summaries cost the most: `claude-sonnet-5-5` has server-side fallbacks, so each of its calls is priced twice over. The prices come from the catalog, checked on 2026-11-02, and these figures change as the code base does.

### The free-tier line

Under free-tier pacing, `estimate` adds a line when the plan needs more than one day of the planned models' daily limits. On a generated project of 1,200 one-function files in 30 directories:

```text
$ devdocs estimate --quality high --max-cost 0
scanned 1,201 files, 1,200 documentable, 729 model calls planned (0.2s)
1,200 files: 720 with prose, 480 from facts alone
729 model calls (1 architecture, 720 files, 8 modules), 0 already cached
at most 1,412,696 input / 190,620 output tokens, at most $3.6721
free tier: up to 8 days of daily quota, fewer if other models can take over; runs resume where they stop
```

At `high`, every call goes to `gemini-2.5-pro`, whose free daily limit in the catalog is 100 requests. The figure is the worst case: when one model's quota is spent, others the key can reach take over. The line never appears for Claude, which has no free tier, nor when a planned model has no free daily limit, nor with `--paid`.

## Recipes

### The cheapest run

1. Run `devdocs estimate`, and `devdocs estimate --explain` to see which files get prose.
2. Keep the defaults: `--quality cheap` and `--context standard`. `--context off` saves the retrieved-context tokens, at the price of prompts that know less about each file's role.
3. Document your code. A file with a module docstring of 80 characters or more, and a docstring on every public symbol and public class member, gets a stub, at zero tokens.
4. Leave out what nobody needs documented with `--exclude`.
5. Keep `.devdocs/` between runs, and do not clear the cache to refresh the docs: unchanged files cost nothing.
6. Set a ceiling that suits you, for example `--max-cost 0.10`.

### CI runs

- Keep `.devdocs/` between runs, so each run pays only for what changed. [ci.md](ci.md) shows how to cache it.
- Take the key from a CI secret, through an environment variable. Never pass `--api-key` in CI.
- Set `--max-cost`, and `--max-requests` if you want a request ceiling too, in the workflow or in `devdocs.toml`.
- Act on the exit status. 7 is a budget stop and 10 a quota stop: the pages are written, and the next run resumes. [commands.md](commands.md#exit-codes) lists the rest.
- Check the price before you pay for it. This prints the plan's worst case in dollars:

  ```sh
  devdocs estimate --json -q | python3 -c 'import json, sys; print(json.load(sys.stdin)["worst_case_cost_usd"])'
  ```

- With a paid Gemini key, add `--paid`, so devdocs paces to paid rate limits instead of the free tier's.

### Large repositories

- Expect the default $0.50 ceiling to cut the plan, and read the cut line of `estimate`. Raise `--max-cost`, or run again: each run buys the next items the last one cut.
- On a free tier, read the free-tier line and plan for several days. A run that spends a model's daily quota moves to another model; when none is left, it stops with exit 10 and resumes on the next run.
- `--quality balanced` and `high` multiply the calls on stronger models. On the copy of this repository, `--quality high --max-cost 0` costs at most $1.4618 against $0.0920 at `cheap`.
- Semantic context embeds the whole code base on its first run; see [context.md](context.md#cost-and-cap).
