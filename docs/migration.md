# Migrating from 3.x

devdocs 4 is a rewrite of the 3.x line, not an update of it. Most v3 command lines still run: devdocs translates them into the v4 command and prints a notice. This page covers what changed, how the old flags map to the new ones, what happens to the documentation v3 wrote, and how to upgrade step by step.

## What changed and why

- **Facts from parsers, prose from a model only where it adds something.** v3 sent each file's whole source to Gemini and asked for a full README per file and per folder. v4 parses the code for its signatures, structure, docstrings, dependencies and tests, writes those facts itself, and asks a model only for short prose, the most valuable files first. Signatures come from the source, not from a model.
- **A cost ceiling on every run.** A run never spends more than `--max-cost`, 0.5 US dollars by default; `--max-requests` can cap its requests too. `--max-cost 0` and `--max-requests 0` mean no limit, not zero spend. [costs.md](costs.md) explains what a run costs.
- **A cache, and runs that resume.** Every usable answer is cached in `<project>/.devdocs/`, and a stopped run resumes where it stopped. A re-run pays only for what changed ([costs.md](costs.md#what-a-re-run-pays-for)). v3 paid for every file again on every run.
- **Commands.** `devdocs generate` writes the docs. `estimate` prices a run without sending anything, `export` renders the last run in another format, `doctor` checks the setup, and `init` writes a starter `devdocs.toml`. `models` lists the models devdocs knows for the provider, their prices, and which your key can use. `cache` shows or clears the response cache. [commands.md](commands.md) covers each one.
- **No key prompt.** v3 asked for a Gemini key on every run. v4 reads the key from the environment, a flag or `devdocs.toml`.
- **Gemini or Claude.** `--provider anthropic` uses Anthropic's Claude models; see [models.md](models.md).
- **Markdown, HTML and PDF.** v3 wrote Markdown only.
- **Your files are safe.** v4 writes its pages only inside its output directory, and never replaces a file it has no record of writing unless you pass `--force`. v3's `--overwrite` wrote README pages into the source tree.
- **Exit codes.** v3 exited 0 even when its run failed. v4's exit codes say what happened, so scripts and CI can act on them; see [commands.md](commands.md#exit-codes).
- **Python 3.11 or newer, and no dependencies.** See [Python and dependencies](#python-and-dependencies).

## Commands, flags and settings

| v3 | v4 | Setting in `devdocs.toml`, environment variable |
|---|---|---|
| `devDocs` | `devdocs generate` | |
| `-p PATH`, `--path PATH` | `devdocs generate PATH` | `path`, `DEVDOCS_PATH` |
| `--output DIR` | `--out DIR` | `out`, `DEVDOCS_OUT` |
| `--name NAME` | `--name NAME` | `name`, `DEVDOCS_NAME` |
| `--description TEXT` | `--description TEXT` | `description`, `DEVDOCS_DESCRIPTION` |
| `--include ITEMS` | `--include PATH` | `include`, `DEVDOCS_INCLUDE` |
| `--exclude ITEMS` | `--exclude PATTERN` | `exclude`, `DEVDOCS_EXCLUDE` |
| `--overwrite` | none | |
| `--authors`, `--keywords` | none | |
| the key, typed at a prompt | `--api-key`, or the variable `DEVDOCS_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `api_key` |
| the page layout | `--per-file` | `per_file`, `DEVDOCS_PER_FILE` |

v3 had no configuration file and no settings variables. In v4 a flag overrides a variable, which overrides `devdocs.toml`; [configuration.md](configuration.md) describes every setting.

What behaves differently:

- **The command is `devdocs`, in lower case.** v3 installed `devDocs`. A bare `devdocs` prints its help and exits 0, where v3 started a run over the current directory.
- **`--out` is read from the project.** v3 read `--output` from the current directory. `devdocs generate app --out docs` writes `app/docs/`.
- **`--name`** defaults to the name in `pyproject.toml`, `package.json` or `Cargo.toml`, else the directory's name, instead of `My Project`. **`--description`** defaults to nothing, instead of `No description provided.`
- **`--include` and `--exclude` do what their names say.** v3 had them the other way round: an `--include` item joined its skip list, and an `--exclude` item left it. In v4, `--exclude` leaves out what matches, read as a `.gitignore` line, and `--include` takes in source files that ignores, `--exclude` or the size limit leave out. Both take several values, repeated or comma-separated. A v3 line passes both on unchanged, now with their v4 meaning, so check any that your scripts wrote for v3.
- **What gets a page.** v3 decided by name. It skipped every file and folder whose name started with `.`, `_`, `-` or `~`, or contained one of 39 fixed strings, even inside a word: `util`, `lib`, `config` and `test` among them, and `out`, so `routes.py` too. It also skipped some file types, and sent every other file under 1,000,000 bytes to the model. v4 pages the source files in the languages it recognises, `__init__.py` included; configuration files get none. It leaves out tests, vendored and generated code, what `.gitignore` or its built-in list of tool and build directories excludes, symbolic links, and files over 1,000,000 bytes. So v4 may page files v3 skipped, and the reverse.
- **`--overwrite`** has no effect: v4 writes its pages only inside its output directory. It is not the same as v4's `--force`, which replaces files in the output directory that devdocs has no record of writing.
- **`--authors` and `--keywords`** are accepted on a v3 line and ignored.

## Where output goes now

- **v4 commands** write to `<project>/<out>`, which is `<project>/docs` by default.
- **v3** wrote to `<current directory>/<output>/<project path from there>`: `devDocs -p app` wrote `docs/app/`. A v3 line run by v4 writes to the same place, so `devdocs -p app` still writes `docs/app/`.
- **State.** v4 also keeps a response cache and a run ledger in `<project>/.devdocs/`. Keep that directory out of version control: `devdocs init` adds `.devdocs/` to an existing `.gitignore`.
- **Not among the sources.** An output directory that is the project root, or above it, is refused, so `devdocs -p . --output .` exits 2 with `out is the project root, ...`. v3 mixed its pages with the project's own files there.

The pages are named differently too:

| Page | v3 | v4 with `--per-file` | v4 default |
|---|---|---|---|
| The file `shop/cart.py` | `shop/cart.md` | `shop/cart.md` | `shop/cart.py.md` |
| `cart.py` and `cart.ts` in one directory | both `shop/cart.md`, so one was lost | `shop/cart.py.md` and `shop/cart.ts.md` | `shop/cart.py.md` and `shop/cart.ts.md` |
| The folder `shop/` | `shop/shop.md` | `shop/index.md` | `shop/index.md` |
| The project | `README.md`, written by the model | `README.md`, an index of the modules and files | the same |
| The architecture | none | `architecture.md` | the same |
| The manifest | none | `index.json` | the same |

The pages read differently as well: front matter, a facts line, and short prose beside exact facts, in place of a model-written README per file. [output.md](output.md) describes every file and both layouts.

## Your existing docs tree

v3 kept no record of the pages it wrote, and v4 never replaces a file it has no record of writing. So the first v4 run whose pages land on v3's stops before it buys anything, with exit 2, and names them. This is `devdocs -p .` over the tree that devdocs 3.0.0 wrote for a small Python project. The key was in `GEMINI_API_KEY`, and a scripted test model stood in for Gemini, so the `gemini:` line counts that model's listing:

```text
devdocs: v3-style flags are deprecated; running `devdocs generate . --per-file --out docs`
devdocs: v4 never replaces a page it has no record of writing: if docs/ still holds v3 pages, add --force once to take them over
gemini: 3 models available to this key
scanned 16 files, 6 documentable, 8 model calls planned (0.0s)
devdocs: error: refusing to replace 6 files in /path/to/shop/docs that devdocs has no record of writing: README.md, shop/cart.md, shop/cli.md, shop/money.md, shop/orders.md and 1 more
  hint: move them away or choose another --out; --force replaces them
```

You have three choices:

1. **Start clean.** Move or delete the v3 tree, then run v4. Nothing is left over to clean up later.
2. **Keep the v3 layout in place.** Run `devdocs generate --per-file --force` once. v4 replaces the v3 pages at the paths it writes and records them in `index.json`. This is the run that pays for its answers, since the refused run bought nothing. From then on v4 replaces and removes those pages as usual, without `--force`. Keep `--per-file` on every later run, or set `per_file = true` in `devdocs.toml`: a run without it switches to the default layout and removes the per-file pages.
3. **Switch to the default layout in place.** Over a v3 tree, `devdocs generate` conflicts on far fewer pages: `README.md`, and any v3 page whose name a v4 page also takes, such as `src/index.md`, which v3 wrote for `src/index.js` and v4 writes for the folder. `devdocs generate --force` replaces those pages. v3's other pages, such as `shop/cart.md`, then stay beside v4's `shop/cart.py.md`.

v4 never deletes a page it has no record of writing, so after choice 2 or 3 you delete the v3 pages that v4 does not write. With `--per-file`, those are the folder pages, such as `shop/shop.md`, and the pages of files v4 does not document, such as `pyproject.md`. With the default layout, they are every v3 page that `--force` did not replace.

## Keys and environment variables

v3 asked for a Gemini key at a prompt on every run. v4 never prompts. It takes the first key it finds:

1. `--api-key`;
2. for Gemini, `DEVDOCS_API_KEY`, `GEMINI_API_KEY`, then `GOOGLE_API_KEY`; for Claude, `DEVDOCS_API_KEY`, then `ANTHROPIC_API_KEY`;
3. `api_key` in `devdocs.toml`.

Without a key, `generate` exits 2 and its hint names the variables. Set the key in your environment, or as a secret in CI, rather than on the command line, where it stays in your shell's history. `devdocs doctor --offline` shows where devdocs found the key, never the key itself. [configuration.md](configuration.md#the-api-key) has the details.

`estimate`, `export`, `init` and `cache` need no key.

## Python and dependencies

- **Python 3.11 or newer.** CI tests 3.11 to 3.15. v3 declared Python 3.8.
- **No dependencies.** v4 uses only Python's standard library. v3 needed `google-genai` and `httpx`. Removing or upgrading devdocs 3 with pip leaves those two installed; remove them if nothing else uses them.
- **An optional extra**, `anthropic`, adds Anthropic's SDK for provider `anthropic-sdk`. Provider `anthropic` needs no SDK.
- **The command is `devdocs`.** v3 installed a command named `devDocs`. On macOS and Windows, whose file systems ignore case by default, a v3 `devDocs` that comes earlier on `PATH`, from another virtual environment or your user site, answers when you type `devdocs`. Remove v3 first; the [steps below](#upgrade-step-by-step) show how, and how to check which command runs.
- **The Python package is `devdocs`.** `import devDocs` no longer works, and devdocs 4.0.0 has no public Python API beyond `devdocs.__version__`.

[install.md](install.md) covers every way to install devdocs 4.

## v3 command lines

A command line that does not start with a v4 command, and that uses `-p`, `--path`, `--output`, `--overwrite`, `--authors` or `--keywords` exactly as written (alone, or as `--flag=value`), is a v3 line. devdocs translates it into `devdocs generate <path> --per-file --out <out>` and runs that, after a two-line notice on stderr. The notice shows even with `-q`, and masks any key as `***`.

From the directory that holds `app/`:

```sh
devdocs -p app
```

```text
devdocs: v3-style flags are deprecated; running `devdocs generate app --per-file --out /path/to/work/docs/app`
devdocs: v4 never replaces a page it has no record of writing: if docs/app/ still holds v3 pages, add --force once to take them over
```

The run's own output follows. With `--overwrite`, the second line starts `devdocs: --overwrite no longer applies; ...`.

How each argument of a v3 line is translated:

| Argument | Becomes |
|---|---|
| `-p PATH`, `--path PATH` | The project, `PATH`. The default is `.`. |
| `--output DIR` | `--out`, aimed where v3 wrote: `DIR` itself with `-p .`, else the absolute `<current directory>/DIR/<project path from there>`. The default is `docs`. |
| `--include`, `--exclude` | Passed on, with their v4 meaning. |
| `--name`, `--description` | Passed on, except v3's defaults `My Project` and `No description provided.` |
| `--api-key` | Passed on. devdocs 3 had no such flag. |
| `--overwrite`, `--authors`, `--keywords` | Dropped. |
| Anything else, such as `-q`, `--format pdf` or `--force` | Passed on to `generate`, which applies or rejects it. |

A v3 line also reads `devdocs.toml` and the `DEVDOCS_*` variables, for everything the translation does not set.

These are not v3 lines, and fail with exit 2:

- A line with only flags v4 also has, such as `devdocs --name Shop`: `devdocs: error: argument command: invalid choice: 'Shop' (choose from ...)`.
- An abbreviated v3 flag, such as `devdocs --out manual`: `invalid choice: 'manual'`.
- A v3 flag after a v4 command, such as `devdocs generate -p .`: `devdocs: error: unrecognized arguments: -p`.

v3 lines are deprecated. The notice prints the v4 command each one runs; put that command in your scripts. For a project other than `.`, it gives `--out` as an absolute path. A script can give it relative to the project instead: run from the same directory, `devdocs generate app --per-file --out ../docs/app` writes the same `docs/app/`.

## Upgrade step by step

1. Remove devdocs 3 from every environment that has it, such as a virtual environment or your user site, with that environment's Python:

   ```sh
   python3 -m pip uninstall devDocs
   ```

2. Install devdocs 4 with one of the methods in [install.md](install.md).
3. Check which command runs:

   ```sh
   devdocs --version
   which -a devdocs
   ```

   `devdocs --version` prints `devdocs` and a 4.x version. If it prints a usage message that lists `--overwrite` instead, devdocs 3 answered. `which -a devdocs` lists every `devdocs` on `PATH` in the order the shell tries them, so the first one listed is the one that runs; on Windows, use `where devdocs`. On macOS and Windows it also finds a v3 `devDocs`, as their file systems ignore case. Remove any v3 it finds from its environment, as in step 1.
4. Check the setup. `doctor` exits 1 until you set a key:

   ```sh
   devdocs doctor --offline
   ```

5. Set your key in the environment, or as a CI secret; see [Keys and environment variables](#keys-and-environment-variables).
6. Optionally, write a starter `devdocs.toml`. `init` also adds `.devdocs/` to the project's `.gitignore`, if it has one:

   ```sh
   devdocs init
   ```

7. Decide what to do with the v3 tree: see [Your existing docs tree](#your-existing-docs-tree).
8. See what the run will cost. `estimate` needs no key and sends nothing:

   ```sh
   devdocs estimate
   ```

9. Generate the docs. If you chose to take over the v3 tree in place, add `--force` to this first run only, and for choice 2 also `--per-file`, which every later run needs too:

   ```sh
   devdocs generate
   ```

10. Delete the v3 pages that v4 does not write.
11. Update scripts and CI: replace each v3 line with the v4 command its notice printed, drop `--overwrite`, `--authors` and `--keywords`, take the key from a secret, and act on the exit codes. [ci.md](ci.md) covers unattended runs.
