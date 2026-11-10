# Providers and models

devdocs writes prose with the models of one provider: Google Gemini, the default, or Anthropic Claude. Parsers supply the facts on every page. A model writes only the prose the planner asks for, under a cost ceiling; [costs.md](costs.md) explains what that costs.

This page covers API keys, choosing a provider, and how devdocs chooses a model for each document.

## Providers

| `provider` | Models | Connection | Key variable |
|---|---|---|---|
| `gemini` (default) | Google Gemini | built-in HTTP client | `GEMINI_API_KEY` |
| `anthropic` | Anthropic Claude | built-in HTTP client | `ANTHROPIC_API_KEY` |
| `anthropic-sdk` | Anthropic Claude | Anthropic's Python SDK, from the [`anthropic` extra](#the-anthropic-extra) | `ANTHROPIC_API_KEY` |

`DEVDOCS_API_KEY` also works for every provider, and Gemini also reads `GOOGLE_API_KEY`. [configuration.md](configuration.md#the-api-key) gives the full order in which devdocs looks for a key.

The built-in client's endpoints are fixed: `https://generativelanguage.googleapis.com/v1beta` for `gemini` and `https://api.anthropic.com/v1` for `anthropic`. devdocs has no setting that changes them. With `anthropic-sdk`, the SDK chooses the endpoint from its own settings; see [The anthropic extra](#the-anthropic-extra).

## Get an API key

devdocs never prompts for a key, and no demo key ships with it. Set a key before you run `generate` or `models`. `estimate`, `export`, `init` and `cache` need no key.

### Google Gemini

1. Create a key in Google AI Studio: <https://aistudio.google.com/apikey>.
2. Put it in the environment. In bash or zsh, run this line, paste the key and press Enter. `read -s` keeps the key off the screen and out of your shell history:

   ```sh
   read -rs GEMINI_API_KEY && export GEMINI_API_KEY
   ```

Gemini is the default provider, so you need nothing else. devdocs paces its requests to Gemini's free-tier limits by default; see [Free-tier pacing and --paid](#free-tier-pacing-and---paid).

### Anthropic Claude

1. Create a key in the Anthropic Console: <https://console.anthropic.com/>.
2. Put it in the environment and select the provider. In bash or zsh, run this line, paste the key and press Enter:

   ```sh
   read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY DEVDOCS_PROVIDER=anthropic
   ```

Claude has no free tier: every request is billed.

devdocs never infers the provider from the key you set. With only `ANTHROPIC_API_KEY` set and no provider selected, the provider is still `gemini`, and a run stops with exit 2:

```text
devdocs: error: no API key for provider 'gemini'
  hint: pass --api-key or set one of: $DEVDOCS_API_KEY, $GEMINI_API_KEY, $GOOGLE_API_KEY
```

`DEVDOCS_API_KEY` goes to whichever provider is selected. A Claude key in it is sent to Gemini unless you select `anthropic` or `anthropic-sdk`.

### Check the key

```sh
devdocs doctor --offline
```

This reports where the key comes from without sending anything. It never prints the key:

```text
ok api key              from env GEMINI_API_KEY
```

Without `--offline`, `doctor` also asks the provider for the key's model listing, and generates nothing. A key the provider rejects stops a run with exit 3. [troubleshooting.md](troubleshooting.md#keys) covers both.

## Select a provider

The provider comes from the first of these that is set:

1. the `--provider` flag;
2. the `DEVDOCS_PROVIDER` environment variable;
3. `provider` in `devdocs.toml`;
4. the default, `gemini`.

```toml
[devdocs]
provider = "anthropic"
```

`devdocs init --provider anthropic` writes that line into a new `devdocs.toml`. Semantic context needs Gemini embeddings, so a Claude provider refuses `context = "semantic"`; see [context.md](context.md).

## How a model is chosen

### Tiers

Each prompt has a tier:

- `file`: the prose of one source file's page;
- `module`: the summary of a directory;
- `architecture`: the project overview.

A fourth tier, `embed`, serves [semantic context](context.md#semantic-context).

The catalog lists models for each tier in order of preference. The first model of a tier that your key can reach serves that tier; the others are stand-ins, used only when it is out of reach ([The model catalog](#the-model-catalog) lists them all). At the default quality:

| Tier | Gemini | Claude |
|---|---|---|
| `file` | `gemini-2.5-flash-lite` | `claude-haiku-5-5` |
| `module` | `gemini-2.5-flash` | `claude-sonnet-5-5` |
| `architecture` | `gemini-2.5-pro` | `claude-opus-5-5` |

Before it plans, `generate` asks the provider which models the key can use and prints `<provider>: <N> models available to this key`. It routes only to catalogued models in that listing. If the listing leaves a tier with no model to call, from that tier or one above it, `generate` stops with exit 2 before it buys anything: `no <provider> model available for <tier> work`. The listing request does not count against `--max-requests`. `estimate` sends nothing, so it assumes the first choices in the table above are reachable.

### Quality levels

`--quality` (setting `quality`) is `cheap` (the default), `balanced` or `high`. It sets the tier each kind of prompt starts on, called its starting tier:

| Quality | File pages | Module summaries | Overview |
|---|---|---|---|
| `cheap` | file tier | module tier | architecture tier |
| `balanced` | module tier | module tier | architecture tier |
| `high` | architecture tier | architecture tier | architecture tier |

At `high`, every call goes to the architecture-tier model: `gemini-2.5-pro` or `claude-opus-5-5` when the key can reach it. Quality also sets how many files get prose; [costs.md](costs.md#what-gets-prose) gives the numbers and explains how files are ranked.

Each page records the model that wrote its prose (`model`) and the kind of prompt (`tier`) in its metadata. A file page written at `high` therefore shows `tier: "file"` with an architecture-tier model.

### Escalation

For each prompt, devdocs works in this order:

1. If any model of the prompt's starting tier, or of a tier above it, has already answered the identical prompt, devdocs uses that stored answer. It costs nothing.
2. Otherwise it calls the first model of the starting tier.
3. If the answer is unusable, it calls the model of the next tier up: file, then module, then architecture. An answer is unusable when it is empty, ends for a reason other than a normal stop or the length cap, or fails devdocs' style check (no purpose or overview line survives cleaning). Each tier has one model at a time, so escalation asks at most one model per tier: a file prompt at `cheap` gets at most three answers.
4. A safety block or a refusal is not escalated: a stronger model from the same vendor applies the same policy. The page is built from facts alone, and the run reports `failed file <path>: no usable reply` and exits 4.

Every call and every escalation counts against the budget. Because of step 1, an answer bought from a stronger model also serves a later run that starts lower: a `cheap` run reuses every answer of a `high` run, while a `high` run reuses only what an architecture-tier model wrote, such as the overview.

A model that answers 404 is retired for the rest of the run, with a `retire` progress line, and the next stand-in of its tier serves from then on. A model whose daily quota is spent is set aside the same way, with a `quota` progress line, until the quota day turns; see [Daily quotas](#daily-quotas).

## Pin one model with --model

`--model ID` (setting `model`) sends every call to that one model, under exactly the id you give. A leading `models/` is dropped.

- There is no escalation: the pinned model is the only one called, even under free-tier pacing. Only its own earlier answers are reused from the cache. Quality still sets how many files get prose.
- The pin is not checked against the key's listing or the provider. A Claude id with provider `gemini` is sent to Gemini.
- A mistyped id fails as soon as it is called. The provider answers 404, devdocs retires the model, and with no model left the run stops with exit 2 before it writes any page:

  ```text
  devdocs: error: every file-tier model was unavailable
    hint: the key may not have access to any catalogued model
  ```

- An id in the catalog, or a snapshot of one, is priced and parameterised by that entry. A snapshot is a catalogued id followed by a date (`claude-haiku-4-5-20251001`), a three-digit version (`gemini-2.5-flash-001`), or `-preview` or `-exp` with an optional date. Matching is case-sensitive.
- Any other id is priced at the most its vendor charges: $10 input and $50 output per million tokens for Anthropic, $4 and $21 for Gemini, with cache reads billed as fresh input. It is sent no optional parameters: no temperature, effort or thinking setting.
- The pinned model is paced at its own limits. A model the catalog does not know is paced at the provider's lowest paid rate: 150 requests per minute on Gemini, 50 on Claude.
- `claude-fable-5-1` is in the catalog for pinning only. devdocs never routes to it.

On a copy of this repository, pinning the architecture model for everything gives this estimate:

```text
$ devdocs estimate -q --model gemini-2.5-pro
60 files: 15 with prose, 45 from facts alone
24 model calls (1 architecture, 15 files, 8 modules), 0 already cached
at most 283,484 input / 13,563 output tokens, at most $0.4900
10 items left as stubs to fit --max-cost $0.5
```

The default ceiling of $0.50 cut 10 files that the default routing would have paid for. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend; [costs.md](costs.md#budget-guards) explains the cut.

## Free-tier pacing and --paid

By default devdocs paces requests to each model's free-tier limits (`free_tier = true`):

- **Gemini.** Each model is held to the free requests per minute and per day in the [catalog](#gemini-models). Models without a free tier, `gemini-3.1-pro-preview` and `gemini-4-argon`, are left out of routing; only a pin calls them. When no free model of a tier is reachable, the strongest free model below it serves that tier.
- **Claude.** Claude has no free tier. Every Claude model is paced at 50 requests per minute, with or without `--paid`.

A lower-tier model that serves a tier this way answers on loan. The cache keeps such an answer under the tier it served, apart from the model's answers for its own tier: later runs find it, `--paid` ones included, and a run at `--quality high` never takes a `cheap` run's page for its own. While `gemini-2.5-pro` is out of reach, a module-tier model writes the overview this way, and at `--quality high` every page.

`--paid` (or `free_tier = false` in `devdocs.toml`, or `DEVDOCS_FREE_TIER=false`) paces Gemini models to their paid per-minute rates, with no daily cap, and lets devdocs call every Gemini model. No flag turns free-tier pacing back on; `DEVDOCS_FREE_TIER=true` overrides a file's `free_tier = false`.

Pacing is per model and per process, over a sliding 60-second window. When a model's window is full, devdocs waits, and prints nothing while it waits.

Google no longer publishes Gemini's free-tier limits; AI Studio shows them per project. The catalog's figures only pace requests: the provider's rate-limit response decides. devdocs retries a rate-limited request, four attempts in all, and waits as long as the server asks, up to 120 seconds. A longer wait fails that one item. [troubleshooting.md](troubleshooting.md#rate-limits-and-quotas) covers rate limits and quotas.

### Daily quotas

devdocs counts each model's answered requests per quota day in `.devdocs/devdocs.db`, so a second run on the same day knows what the first one used. The quota day runs from midnight to midnight US Pacific time, as Gemini's daily quotas do.

When a model reaches the catalog's daily limit under free-tier pacing, or the provider reports its daily quota spent, devdocs stops calling that model until the quota day turns. Its work moves to the next model the key can use: a stand-in of the same tier, else the model of the next tier up, which costs more per token. Under free-tier pacing, a tier with no free model left is served from a lower tier, as [described above](#free-tier-pacing-and---paid).

When no model is left, the run stops. It writes every page, the rest as stubs, says `stopped early (quota); run again to resume`, and exits 10. Run it again after the reset: it resumes and reuses every answer already bought, those given on loan included.

An account-wide billing problem stops the run at once, with exit 10: a Gemini prepay balance at zero or a monthly spend cap, or an Anthropic spend limit or billing error.

`estimate` says how many days of free-tier quota a plan may need; see [costs.md](costs.md#the-free-tier-line).

## The model catalog

The catalog is a table in the code, `devdocs/llm/catalog.py`. Its prices are the vendors' paid rates, in USD per million tokens, and were checked on 2026-11-02. `devdocs doctor` warns once that date is more than 180 days old. devdocs computes what a call cost from these prices and the usage the provider reports, under free-tier pacing too; it does not read the vendor's bill. Check the vendors' pricing pages before you rely on a figure.

Within a tier, the first model is the first choice and the others are stand-ins. A stand-in serves when the first choice is not in the key's listing, answers 404, has spent its daily quota, or, on Gemini under free-tier pacing, has no free tier.

### Gemini models

| Model | Tier | Input | Output | Free tier, requests per minute / day | Paid pacing, requests per minute | Notes |
|---|---|---|---|---|---|---|
| `gemini-2.5-flash-lite` | file | 0.10 | 0.40 | 15 / 1,000 | 4,000 | first choice |
| `gemini-3.1-flash-lite` | file | 0.25 | 1.50 | 15 / 1,000 | 4,000 | |
| `gemini-3.5-flash-lite` | file | 0.30 | 2.50 | 15 / 1,000 | 4,000 | |
| `gemini-2.5-flash` | module | 0.30 | 2.50 | 10 / 250 | 1,000 | first choice |
| `gemini-3.8-flash` | module | 0.75 | 3.75 | 10 / 250 | 1,000 | 1.50 / 7.50 from 2027-01-01 |
| `gemini-3.6-flash` | module | 0.75 | 3.75 | 10 / 250 | 1,000 | 1.50 / 7.50 from 2027-01-01 |
| `gemini-2.5-pro` | architecture | 1.25 | 10.00 | 5 / 100 | 150 | first choice; 2.50 / 15.00 for a prompt over 200,000 tokens |
| `gemini-3.1-pro-preview` | architecture | 2.00 | 12.00 | none | 150 | 4.00 / 18.00 for a prompt over 200,000 tokens |
| `gemini-4-argon` | architecture | 2.00 | 10.00 | none | 150 | 4.00 / 20.00 from 2026-12-01 |
| `gemini-embedding-001` | embed | 0.15 | none | 100 / 1,000 | 3,000 | [semantic context](context.md#semantic-context) only |

Google now serves the 2.5 models only to projects that used them before. Other projects get a 404, and devdocs moves on to the 3.x stand-ins, which cost more per token than the 2.5 models that `estimate` prices. Under free-tier pacing, a lower-tier model then writes the overview on loan; see [Free-tier pacing and --paid](#free-tier-pacing-and---paid). A model missing from the key's listing is never tried. A listed model that answers 404 is retired for that run only, so the next run tries it again.

### Claude models

| Model | Tier | Input | Output | Notes |
|---|---|---|---|---|
| `claude-haiku-5-5` | file | 0.10 | 0.50 | first choice; 0.50 / 2.50 for a prompt over 100,000 tokens |
| `claude-haiku-4-5` | file | 1.00 | 5.00 | |
| `claude-sonnet-5-5` | module | 2.00 | 10.00 | first choice; server-side fallbacks |
| `claude-sonnet-5` | module | 2.00 | 10.00 | |
| `claude-opus-5-5` | architecture | 4.00 | 20.00 | first choice; server-side fallbacks |
| `claude-opus-5` | architecture | 5.00 | 25.00 | server-side fallbacks |
| `claude-fable-5-1` | architecture | 10.00 | 50.00 | pin only, never routed; server-side fallbacks |

Every Claude model is paced at 50 requests per minute.

### How the rates apply

- A prompt over a model's long-prompt threshold bills every token at the long-prompt rates.
- An announced price change applies from its date, by the UTC calendar.
- Cache reads bill at 0.1 times the input rate, 0.05 times on `gemini-4-argon` and `claude-opus-5-5`, and 0.025 times on `claude-fable-5-1`. On Claude, writes to the prompt cache bill at 1.25 times the input rate.
- Claude models marked "server-side fallbacks" let Anthropic answer a declined request with another model, and Anthropic can bill the declined attempt as well. devdocs books the dearer of the requested and the serving model's price. Worst-case prices, in `estimate` and in the budget, count such a call twice.

### Request parameters

devdocs sends each model only the parameters it accepts. Temperature 0.2 goes only to the Gemini 2.5 models. Effort `low` goes to the Claude models that accept it. Thinking is turned off, or set as low as the catalog records the model allows. None of these is configurable.

### What the catalog is not

The catalog is static. The key's listing only filters it: it never adds a model or a price. A listed model the catalog does not know is used only when you pin it, at the vendor's highest rates.

devdocs 4.0.0 has no public plugin API. For contributors working in the repository, the `ChatModel` protocol is the extension point for providers: adding a provider, or a model to the catalog, is a change to the code. [architecture.md](architecture.md#add-a-model-provider) shows where.

## The models command

`devdocs models` compares the provider's catalog models with the models the provider lists for your key. It needs a key and generates nothing:

```sh
devdocs models
```

The `access` column is `yes` for each catalog model the listing includes; a dated Claude id, such as `claude-haiku-4-5-20251001`, counts for its catalog id. `generate` asks for the same listing before it plans, and routes only to models marked `yes`; a pin is not checked against it. `models` shows only the base prices: the long-prompt rates, announced changes and cache rates are in [the catalog](#the-model-catalog). [commands.md](commands.md#models) describes the output, `--json` and the flags.

## The anthropic extra

devdocs reaches Claude through its own HTTP client, with `provider = "anthropic"`, and needs no other package. To use Anthropic's official Python SDK instead:

1. Install devdocs with its `anthropic` extra, in the Python environment devdocs runs from. [install.md](install.md#the-anthropic-extra) gives the commands.
2. Select the provider: `--provider anthropic-sdk`, `DEVDOCS_PROVIDER=anthropic-sdk`, or `provider = "anthropic-sdk"` in `devdocs.toml`. Installing the extra alone changes nothing: with `provider = "anthropic"`, devdocs uses its own client even when the SDK is installed.
3. Check that the SDK imports:

   ```sh
   devdocs doctor --offline --provider anthropic-sdk
   ```

   Without the package, `doctor` reports it and exits 1:

   ```text
   !! anthropic-sdk          cannot import anthropic: ModuleNotFoundError: No module named 'anthropic'
                            pip install 'devdocs[anthropic]', or use provider = 'anthropic'
   ```

Without the package, `generate` and `models` stop with exit 2 before they send any request:

```text
devdocs: error: provider 'anthropic-sdk' needs the anthropic package
  hint: pip install 'devdocs[anthropic]', or use provider = 'anthropic'
```

pip reads `devdocs` and `devDocs` as the same name.

`estimate` sends nothing, so it works without the SDK and prices `anthropic-sdk` as `anthropic`.

The SDK provider builds the same requests and reads the responses and errors the same way as `anthropic`. The differences:

- The SDK's own retries are off, because devdocs retries.
- It never sends a temperature. No catalogued Claude model receives one from `anthropic` either.
- The SDK applies its own environment settings.

Both Claude providers share cached answers, so switching between them buys nothing again. A run under one does not resume an interrupted run under the other.

The Docker image installs no extras, so it refuses `anthropic-sdk`; use `anthropic` there. The npm package runs your own Python, so `anthropic-sdk` works through it only when that Python has the `anthropic` package installed.
