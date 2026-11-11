# devdocs documentation

This is the reference for devdocs 4. If you are new to devdocs, start with the [quick start](../README.md#quick-start).

## Using devdocs

- [Install](install.md): How do I install, upgrade or remove devdocs with pipx, uv, pip, the installer scripts, npm, Docker, or from source?
- [Commands](commands.md): What does each command and flag do, what does each exit code mean, and how does a run stop?
- [Configuration](configuration.md): Where does `devdocs.toml` live, what can it set, which environment variables does devdocs read, and how is the API key found?
- [Models](models.md): Which providers and models write the prose, how do I get a key, and how is a model chosen?
- [Costs](costs.md): What does a run cost, how do the budget guards work, and how do I read `estimate`?
- [Context](context.md): What does each prompt learn about the rest of the code base, and when is semantic retrieval worth paying for?
- [Output](output.md): What does a run write, in which formats, what happens to files devdocs did not write, and how does `export` render a run again?
- [Troubleshooting](troubleshooting.md): What does `devdocs doctor` check, and how do I fix what goes wrong?
- [Migration](migration.md): How do I move from devdocs 3.x?

## Running it elsewhere

- [CI](ci.md): How do I run devdocs unattended, in GitHub Actions or a container, and keep its cache from one run to the next?
- [The npm package](../npm/README.md): How do I run devdocs from npm? This is the package's own page; how it finds Python is in [Install](install.md#npm), and its exit codes are in [Commands](commands.md#exit-codes).

## The project

- [Architecture](architecture.md): How does devdocs work inside, and where would a change go?
- [Contributing](../CONTRIBUTING.md): How do I set up a checkout, run the checks, and send a change?
- [Changelog](../CHANGELOG.md): What changed in each release?
- [Security](../SECURITY.md): What does devdocs send to the provider, and how do I report a vulnerability?
- [Code of conduct](../CODE_OF_CONDUCT.md): How do we treat each other?
- [License](../LICENSE): Under what terms can I use devdocs? The MIT License.
