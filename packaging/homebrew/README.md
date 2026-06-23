# Homebrew Tap Source

This directory contains the formula source intended for the external tap:

```bash
brew tap crewplaneai/crewplane
brew install crewplane
crewplane --help
```

The tap repository is expected to live at
`https://github.com/crewplaneai/homebrew-crewplane`. This repository does not
create or push that external tap.

Before publishing the tap, maintainers must regenerate the formula pins from
the exact canonical PyPI artifact and dependency resources that will be served
publicly:

1. Run `make release-check`.
2. Publish the canonical `crewplane-0.1.0a1.tar.gz` artifact.
3. Replace the formula URL and SHA256 with that published artifact.
4. Refresh runtime resources if `uv.lock` changed.
5. Refresh the pinned Hatchling build-backend wheel resources if
   `pyproject.toml` changes build-system requirements.
6. Copy `packaging/homebrew/Formula/crewplane.rb` into the tap repository.
7. Run `brew audit --strict crewplane` and `brew test crewplane` from the tap.

For local validation before publication, use:

```bash
make brew-smoke
```

Release and smoke targets read the version from `pyproject.toml`.
`make release-check` also verifies that the exact version is not already on
PyPI or npm. The local smoke target creates a temporary formula copy that
points at the local sdist. It skips clearly when Homebrew is not available or
when a `crewplane` formula is already installed. The formula declares `maturin`
for the `pydantic-core` runtime sdist, installs declared runtime resources
first, then builds the `crewplane` sdist with build isolation disabled so the
Hatchling backend comes only from the pinned formula resources.
