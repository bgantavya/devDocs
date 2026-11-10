# Context and retrieval

A prompt can carry context from other files: how the file is used, what it builds on, and related code elsewhere in the project. Context helps the model describe a file's role. It lives only in the prompts, and never appears in the documents.

`--context` chooses how much context the prompts get: `off`, `standard` (the default) or `semantic`. It is also the `context` setting in `devdocs.toml` and the `DEVDOCS_CONTEXT` variable; see [configuration.md](configuration.md#settings). Of the commands, only `generate` and `estimate` take the flag.

## The modes

| Mode | What the prompts get | Extra requests | Providers |
|---|---|---|---|
| `off` | the parsed facts and the source, nothing retrieved | none | all |
| `standard` (default) | how other files use the file and what it calls; related code found by keyword search; the entry points' code for the overview | none, only more input tokens | all |
| `semantic` | everything in `standard`, with related code also ranked by Gemini embeddings | embedding batches, for code without a stored vector | `gemini` only |

## What every prompt carries

In every mode, `off` included, a prompt carries the facts that the parsers and the import graph supply:

- a file prompt: the project, the file's path, language and line count, the files that import it and test it, its imports and external packages, its entry point and exports, and the names to describe, then its source;
- a module prompt: its files with their purposes, the imports between them, the modules it depends on and is used by, its entry points and submodules;
- the overview prompt: the languages, the components and their dependencies, the entry points and the external packages.

These facts are not retrieved context: `--context off` does not remove them.

A file prompt carries the file's source, without any licence comment at its head, up to about 6,000 tokens, counted at 3.5 characters a token. A longer source is cut to fit:

- to a skeleton that keeps every declaration, with as much of each body as fits and, if need be, docstrings cut to their first line;
- when not even the declarations fit, to as many of the first declarations as fit, followed by `... (<N> more declarations omitted)`;
- when the parser found no declarations in the file, to its first and last lines, around `... (<N> lines omitted)`.

## Standard context

### File prompts

This is the prompt for `shop/cart.py` in a small project, as devdocs builds it in standard mode. The block between the `Describe:` line and `Source:` is the context:

````text
Project: shop
File: shop/cart.py (python, 9 lines)
Imported by: shop/cli.py, shop/orders.py
Tested by: tests/test_cart.py
Imports: shop/money.py
Describe: Cart, Cart.add, Cart.total

How other files use this one, and what it calls (for its role; do not describe them):
- shop/cli.py:6 uses Cart: Cart()
- shop/orders.py:6 uses Cart: def __init__(self, cart: Cart) -> None:
- shop/money.py:3 defines Money: class Money(int) — An amount in cents.

Source:
```python
from shop.money import Money


class Cart:
    def add(self, sku: str, price: Money) -> None:
        self.items.append((sku, price))

    def total(self) -> Money:
        return sum(p for _, p in self.items)
```
````

With `--context off`, the same prompt has no such block. Each row has one of three kinds:

- `uses`: a line in a file that imports this one, where one of this file's public names is used. Comments and strings are masked out, so a name in a docstring or a log message is not a use, and import lines are skipped. Importers that are not tests come first. At most 8 lines are kept: 2 per importer and 2 per name. Names shorter than 3 characters are not searched.
- `defines`: the signature of a project symbol this file uses from the files it imports, with the first paragraph of its docstring. At most 12.
- `related`: related code from elsewhere, added only when fewer than 3 `uses` and `defines` rows fit. Up to 3 chunks are added, never from the file itself or from a file that an import joins to it, in either direction, tests included.

The code in each row is cut to 160 characters. The block must fit in about 700 tokens, counted at 3.5 characters a token. A row that does not fit is skipped, so a shorter one after it can still be used.

On a copy of this repository, 24 of the 25 file prompts had at least 3 exact rows. Only `devdocs/demo.py` got related code.

### Module prompts

A module prompt ends with up to 4 chunks of related code, under the heading `Related code elsewhere (reference only; do not describe it):`. They come from outside the module and from no file that an import joins to it. Module prompts always look for related code, within about 600 tokens. On the copy of this repository, each of the 8 module prompts got 4 rows.

### The overview

The overview prompt ends with the code of the project's entry points, each under a heading such as `Entry point code (shop/cli.py:4; FLOW follows its order):`.

- It takes the first 3 entry points, in path order.
- In each, it takes the first top-level function named `main`, `run`, `cli`, `app` or `handler`, looked for in that order, for up to 40 lines. Without one, it takes the first 40 lines of the file.
- The bodies must fit in about 1,500 tokens.

The overview never gets related code, in any mode.

### How related code is found

Related code is code the import graph cannot link, such as a request handler and the client code that calls its route. It comes from an index of the project's code, built in memory on each run:

- Each file is split into chunks: one per top-level declaration, plus a header chunk with the module docstring and the lines before the first declaration. A class over 60 lines is split into one chunk per member. A chunk holds at most 60 lines and 2,400 characters. Constants get no chunk of their own.
- Only documentable files are indexed: source files in a language devdocs knows. Tests, documentation, and generated and vendored code are never returned as related code. Tests can still appear in `uses` rows.
- Standard mode ranks the chunks by keyword search (BM25). The query is the file's public names and its module docstring; for a module, those of all its files. Matches count only when the best one scores at least 2.0, and then only those scoring at least half the best.
- A `related` row quotes the first line of its chunk, skipping decorators and comments. For a header chunk, that is usually the first line of the module docstring.

## Semantic context

### What it adds

Semantic context needs Gemini embeddings. With a Claude provider, devdocs refuses it with exit 2:

```text
devdocs: error: semantic context needs Gemini embeddings
  hint: use context = "standard" with Anthropic, or provider = "gemini"
```

Semantic mode ranks related code by embedding similarity as well as by keywords, and merges the two rankings. A file's query is the mean of its own stored chunk vectors, and a module's is the mean over all its files' chunks, so retrieval sends no request per prompt. A similarity match counts only when it is within 10% of the best one.

Only the `related` rows change: those of module prompts, and those of file prompts whose exact context is thin. The `uses` and `defines` rows and the overview are the same as in standard mode. A file whose chunks have no stored vector, because it is new or because embedding was skipped or failed, gets exactly its standard prompt.

### Embeddings

- The model is `gemini-embedding-001`, at 768 dimensions, with 100 chunks per request. None of this is configurable, and `--model` pins only the model that writes prose.
- A run embeds only the chunks that have no vector from this model at this size. A chunk's id is a hash of its path, name and text, so unchanged code keeps its vector, even when it moves within its file. An edit or a rename gives the changed chunks new ids.
- The batches go out one at a time, after devdocs has checked that it can write its output and before it buys any prose. `--concurrency` does not apply to them.
- They are paced like any request: on the free tier, 100 a minute and 1,000 a day, counted across runs; with `--paid`, 3,000 a minute and no daily cap.
- A failed batch is retried like any call, four attempts in all. Each retry shows as a `retry` progress line with the tag `embeddings`.
- The run logs `embedded <N> chunks ($<cost>)`. `-q` hides that line.

### Cost and cap

Embedding costs $0.15 per million input tokens. The endpoint reports no usage, so devdocs reserves and books each batch at its worst case: one token per byte of its chunks' text, plus 64. The cost devdocs reports for embedding therefore overstates what you pay.

The first semantic run embeds the whole code base. On the copy of this repository, `estimate --context semantic` priced 14 batches, at most 941,462 tokens and $0.1412, which raised the worst case from $0.0920 to $0.2332. Later runs embed only what changed. After a one-line edit to one function of a small project whose answers and vectors were all stored, `estimate --context semantic` printed this; its one embedding batch holds the edited function's chunk alone:

```text
6 files: 6 with prose, 0 from facts alone
4 model calls (1 architecture, 1 embed, 1 file, 1 module), 5 already cached
at most 6,263 input / 2,356 output tokens, at most $0.0199
```

Embedding may take at most half of each limit:

- It runs only if its worst-case cost is at most half of `--max-cost`, and its batches number at most half of `--max-requests`. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend, so a limit at 0 puts no cap on embedding.
- Otherwise the run skips embedding altogether and carries on with the vectors already stored:

  ```text
  semantic context skipped: embedding 11 chunks could take 1 request and $0.0002, over half of --max-requests; raise that or use --context standard
  ```

- `estimate` gives no notice of the skip: it leaves the embedding out of its price. On the copy of this repository, `devdocs estimate --context semantic --max-cost 0.2` prices the same as standard context, because $0.1412 is more than half of $0.20.
- Embedding never sends more than half of `--max-requests`, retries included.

Embedding comes out of the same budget, before the plan is cut to fit, so under a binding limit semantic context buys fewer pages with prose. On the copy of this repository, 14 batches leave 16 of 30 requests for prose:

```text
$ devdocs estimate -q --context semantic --max-requests 30
60 files: 7 with prose, 53 from facts alone
30 model calls (1 architecture, 14 embeds, 7 files, 8 modules), 0 already cached
at most 1,096,955 input / 10,760 output tokens, at most $0.1973
18 items left as stubs to fit --max-requests 30
```

[costs.md](costs.md#budget-guards) explains the limits.

### When embedding fails

- A budget limit, a quota or a stop request in the middle of embedding logs `semantic context: <reason>; using the vectors stored`. The vectors stored so far are kept, and the run goes on.
- Any other failure that the retries do not cure, such as a rate limit, a server or network error, or a rejected key, ends embedding for this run: no later batch is tried. devdocs prints one warning, even under `--quiet`: `warning: semantic context: embedding failed (<reason>); files without vectors get standard context, and the next run tries again`. The pages are written, the run counts as finished, and with no other failure devdocs exits 0. With `--json`, `semantic_fallback` gives the reason.

## The index on disk

- `off` stores nothing and builds no index.
- `standard` builds its chunks and keyword index in memory on each run, and stores nothing.
- `semantic` stores the vectors in `.devdocs/devdocs.db` under the project root, in the `chunk` table: each chunk's id, path and start line, with its vector and the vector's size and model. The text is not stored: each run has it from its scan. Each vector takes 3,072 bytes, 768 four-byte numbers.

Only `generate` writes the vectors, after it has checked that it can write its output. `estimate`, and a run refused before it pays, leave them as they were.

Each semantic run replaces the stored chunks of the files it covers, keeping every vector whose chunk id survives, and drops the chunks of files that are gone from disk. A file the run only leaves out, with `--exclude` for example, keeps its vectors, so widening the selection again costs nothing.

`devdocs cache clear` deletes cached answers, not vectors, and no command lists or clears the vectors. To drop them, delete `.devdocs/`; that also drops the cached answers, the run ledger and the daily request counts.

Above 13,000 vectors, the search first compares the vectors' sign bits and keeps the nearest candidates, then ranks those exactly. devdocs uses no SQLite extension and no external index.

## Context, cost and the cache

Context costs input tokens, and estimates and budget reservations count them. On the copy of this repository:

| `--context` | Input tokens, at most | Cost, at most |
|---|---|---|
| `off` | 443,595 | $0.0831 |
| `standard` | 485,425 | $0.0920 |
| `semantic`, first run | 1,426,887, of which 941,462 to embed | $0.2332 |

Context is not part of an answer's cache key: an answer is stored under its prompt without the retrieved context. So:

- Switching modes never buys an answer again. After a semantic run of a small project, `estimate --context standard` and `estimate --context off` both printed `0 model calls (none), 8 already cached`.
- An answer bought under one mode stays in use under another until the file's own prompt changes. To have every page written again with the new context, run `devdocs cache clear` first. That drops every cached answer, so the next run pays for all of them.
- An edit elsewhere that changes only a file's context rows does not buy its page again. A change to the file's facts does: a new file that imports it changes its `Imported by:` line.
- The entry points' code in the overview prompt is context too, so a change to that code alone leaves the overview's cache key as it was.

Context is not part of how a run is resumed either: an interrupted run resumes under another mode.

## Choosing a mode

- **`standard`** suits most projects. It sends no extra requests, and on the copy of this repository it added about a tenth to the input tokens.
- **`off`** is the cheapest. Use it under a tight budget, or when the context rows add little. The prompts still carry every fact listed above.
- **`semantic`** ranks related code by embedding similarity as well as by keywords. It needs Gemini. Its first run pays to embed the whole code base, at most half of each limit; later runs pay only for changed code. It changes only the related rows of module prompts and of thin file prompts, so check with `estimate` that it fits your budget before you turn it on.
