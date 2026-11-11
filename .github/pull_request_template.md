## What and why

<!-- What this pull request changes, and why. Link the issue it closes, if any. -->

## How it was tested

<!-- The commands you ran and what they showed, and the tests you added. -->

## Checklist

- [ ] The title is a conventional commit subject, `type(scope)!: description`, and so is every commit's. The scope is optional, and `!` marks a breaking change.
- [ ] Tests cover the change, and `make test` passes; no test uses the network or a model API.
- [ ] `make lint` and `make typecheck` pass.
- [ ] `make coverage` and `cd npm && npm test` pass.
- [ ] Where users see the change, the docs and CHANGELOG.md are updated.
- [ ] No new runtime dependency.
