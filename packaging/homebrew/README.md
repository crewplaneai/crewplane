# Homebrew Tap Source

This directory contains the formula source intended for the external tap. The
formula installs Crewplane, a Markdown-native control plane for AI coding CLIs:

```bash
brew tap crewplaneai/crewplane
brew install crewplane
crewplane --help
```

Start with the mock workflow path:

```bash
crewplane init
crewplane validate
crewplane run --no-live
```

`crewplane init` creates `.crewplane/config.yml`, a default workflow, and
additional example templates. The default run uses deterministic `mock` output
and writes readable artifacts under `.crewplane/execution-stages/` and
`.crewplane/execution-results/`, so it does not require provider CLIs, API
keys, provider accounts, or config edits. Provider CLIs are installed and
authenticated separately when you configure real provider runs; the formula
does not install provider CLIs, manage credentials, or sandbox provider
execution.

The tap repository is expected to live at
`https://github.com/crewplaneai/homebrew-crewplane`. This repository does not
create or push that external tap.

Before publishing the tap, maintainers must use the exact canonical PyPI
artifact and dependency resources that will be served publicly:

1. Run `make release-prepare` for a coordinated new version.
2. Confirm the prepared formula points at the canonical PyPI sdist and SHA.
3. Run `make release-check`.
4. Copy `packaging/homebrew/Formula/crewplane.rb` into the tap repository.
5. Run `brew audit --strict crewplane` and `brew test crewplane` from the tap.
6. Push the tap update after PyPI and npm are live.

For local validation before publication, use:

```bash
make brew-smoke
```

Release and smoke targets read the version from `pyproject.toml`.
`make release-prepare` verifies that the exact version is not already on PyPI or
npm before it rewrites local release scratch state. `make release-pypi` and
`make release-npm` run registry-specific remote checks so a partial release can
be completed without being blocked by a version that already exists in the other
registry. The local smoke target
creates a temporary formula copy that points at the local sdist. It skips
clearly when Homebrew is not available or when a `crewplane` formula is already
installed. The formula declares `maturin` for the `pydantic-core` runtime sdist,
installs declared runtime resources first, then builds the `crewplane` sdist
with build isolation disabled so the Hatchling backend comes only from the
pinned formula resources.
