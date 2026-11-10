# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 4.x | Yes |
| 3.x and older | No |

Security fixes are made on the 4.x line only. The 3.x line is no longer maintained: [docs/migration.md](docs/migration.md) explains how to upgrade.

## Reporting a vulnerability

Report a vulnerability privately, in either of these ways:

- through GitHub's private vulnerability reporting, at <https://github.com/bgantavya/devDocs/security/advisories/new>;
- by email to gantavyaoo@gmail.com.

Do not open a public issue, pull request or discussion about it.

Include what you can of:

- the devdocs version (`devdocs --version`) and how you installed it;
- your operating system and Python version;
- what the problem is, and what an attacker could do with it;
- the steps to reproduce it: the command line, and a small project or file that shows it;
- the output, with every API key and other secret removed;
- whether the problem is already public, and any fix you have in mind.

Never send a working API key, yours or anyone else's.

## What happens next

devdocs has one maintainer, so these are aims, not guarantees:

1. You get an acknowledgement within 7 days.
2. Within 30 days, you hear whether the report is accepted as a vulnerability in devdocs, how severe it looks, and what the plan is.
3. A confirmed vulnerability is fixed in a 4.x release and described in a GitHub security advisory, which credits you unless you would rather it did not.

Please keep the report private until the fix is released, or until you and the maintainer agree on a date.

## Scope notes

### API keys

- devdocs reads a key from the `--api-key` flag, from the provider's environment variables, or from `api_key` in `devdocs.toml`. [docs/configuration.md](docs/configuration.md#the-api-key) gives the order and the variable names.
- It sends the key only to the provider. With providers `gemini` and `anthropic`, devdocs' own HTTP client sends it in a request header, never in a URL; with `anthropic-sdk`, Anthropic's SDK sends it.
- It never writes the key to disk, and no cache entry, manifest, page or `--json` output holds it.
- `devdocs doctor` reports where the key came from, never the key itself.
- An argument error, and the notice printed for a v3 command line, mask a key that the command line names with a flag or an assignment, such as `--api-key=***`. A key typed as a bare argument, which no flag names, is printed like any other argument.
- With `gemini` and `anthropic`, a key with a line break inside it, or any other character that an HTTP header cannot carry, is refused before any connection, without being printed. Spaces and line breaks around a key are trimmed first.

A key on a command line can show up in shell history and in the process list, so prefer an environment variable or a CI secret. A key in `devdocs.toml` is stored there as plain text, so keep that file out of version control if it holds one. Apart from a bare argument, if devdocs prints, logs or writes a key anywhere, that is a vulnerability: please report it.

### Keys in this repository

- The `secret` file at the repository root holds a Gemini demo key that has sat in the repository since 1.x. Google has revoked it. No devdocs code reads the file, no package devdocs publishes contains it, and the secret scan allows for it. You do not need to report it. devdocs 4.0.0 ships no shared key.
- `tests/test_http.py` holds a self-signed private key for a TLS test server on 127.0.0.1. It is test data, trusted by nothing, and the secret scan allows for that one key only.

### Generated documentation quotes your code

- Pages quote the source they document: file paths, signatures with their default values, constant declarations, docstrings, and comments with TODO and similar markers. So a secret in the code, such as a key assigned to a constant, can appear in the docs. Publishing the output publishes those parts of the code, so review the docs of a private project before you publish them.
- `generate` sends the provider you configure the project's name and description, its file paths and its source code. Each file that gets prose is sent whole or, when it is long, cut to fit. Unless context is `off`, lines from related files go with it. With semantic context, `generate` also sends the code, in chunks, to Gemini's embedding model. The provider handles all of it under its own terms.
- `models`, and `doctor` without `--offline`, send no project data: only a request, with the key, for the list of models. `estimate`, `export`, `init`, `cache` and `doctor --offline` send nothing.
- `.devdocs/devdocs.db` holds the model's answers about your code and, with semantic context, the file paths and embedding vectors of the code's chunks. Treat it as you treat the code.

### Out of scope

- The providers' own services, and how they handle what is sent to them: report those to Google or Anthropic.
- Vulnerabilities in Python, OpenSSL or the operating system, unless devdocs uses them unsafely.
- Pages that are wrong but not unsafe: open a [Generated docs problem](https://github.com/bgantavya/devDocs/issues/new?template=generated_docs.yml) issue instead.

## How releases are protected

- devdocs has no runtime dependencies. Its development tools are pinned in `uv.lock`.
- CI scans the whole history of the commit it tests for secrets with gitleaks, the package's code with bandit, and every locked dependency, the optional `anthropic` SDK included, with pip-audit. Every GitHub Action is pinned to a commit.
- Releases are built by GitHub Actions from their tag, with build provenance: trusted publishing with attestations on PyPI, provenance on npm, and provenance and an SBOM for the container image. Each GitHub release carries a CycloneDX SBOM of the Python packages.
- The container image runs as a non-root user.
