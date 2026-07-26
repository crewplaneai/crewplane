# Crewplane for Homebrew

**Turn AI agent calls into structured, repeatable workflows.**

Crewplane is the control plane for AI coding CLIs. Define the stages and
dependencies in Markdown, run them through Claude Code, Codex, Gemini, Copilot,
Kilo, or another command-line tool, and keep a readable record of every run on
disk.

It is designed for the point where agent work stops being one prompt and
becomes a process: planning, implementation, parallel review, revision,
synthesis, and handoffs that should survive a failed terminal session.

<p align="center">
  <img src="https://github.com/user-attachments/assets/dca2dacb-49e4-4849-b92b-7a47b493ea52" alt="Crewplane live workflow dashboard" width="80%">
</p>

## What Crewplane Adds

| Agent work today | With Crewplane |
| --- | --- |
| Process held in one chat | Reviewable Markdown workflow |
| Manual prompt handoffs | Explicit dependencies and artifact references |
| One provider at a time | Multiple CLI providers in one DAG |
| Terminal-only history | Inputs, outputs, logs, and manifests on disk |
| Restart after a failure | Resume from validated completed stages |
| Unclear progress | Console summaries and an optional tmux dashboard |

Crewplane is CLI-first: it invokes the provider tools you already use rather
than replacing their authentication, permissions, models, or tool access with a
vendor SDK.

## Install With Homebrew

On macOS:

```bash
brew tap crewplaneai/crewplane
brew install crewplane
crewplane --help
```

## Run Your First Workflow

Move into a project directory and run:

```bash
crewplane init       # create local config and example workflows
crewplane validate   # compile and validate the workflow DAG
crewplane run        # run with deterministic mock output
```

The generated workflow uses Crewplane's mock invoker, so the first run needs no
provider CLI, account, API key, token spend, or config edit. It still writes the
same inspectable run-record structure used for real provider runs:

```text
.crewplane/
├── execution-results/<run>/   # consolidated outputs and findings
└── execution-stages/<run>/    # inputs, outputs, logs, and manifests
```

After the mock run succeeds, install and authenticate a provider CLI separately
and prepare the project for a real run:

```bash
crewplane onboarding
crewplane run
```

Crewplane does not install provider CLIs, manage credentials, grant
permissions, or sandbox provider execution. It coordinates the workflow around
the provider tools and policies you already trust.

Use a provider CLI directly for a quick question or one-off patch. Use
Crewplane when the work has multiple stages, provider handoffs, review loops,
failure recovery, or a run record that other people need to inspect.

## Update

Crewplane can ask Homebrew to update the installed copy and then report the
version:

```bash
crewplane --update
crewplane --version
```

You can also use Homebrew directly:

```bash
brew upgrade crewplane
```

## Learn More

- [Why Crewplane?](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/why-crewplane.md)
- [Quickstart](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/quickstart.md)
- [Provider setup](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md)
- [Workflow syntax](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/workflow-syntax.md)
- [GitHub repository](https://github.com/crewplaneai/crewplane)

Crewplane is open source under the Apache-2.0 license. Questions and workflow
ideas are welcome in
[GitHub Discussions](https://github.com/crewplaneai/crewplane/discussions).

## Tap Maintainer Notes

This directory contains the formula source intended for the external tap at
`https://github.com/crewplaneai/homebrew-crewplane`. This repository does not
create or push that external tap.

Before publishing the tap, use the exact canonical PyPI artifact and dependency
resources that will be served publicly:

1. Run `make release-prepare` for a coordinated new version.
2. Confirm the prepared formula points at the canonical PyPI sdist and SHA.
3. Run `make release-check`.
4. Copy `packaging/homebrew/Formula/crewplane.rb` into the tap repository.
5. Run `brew audit --strict crewplane` and `brew test crewplane` from the tap.
6. Push the tap update after PyPI and npm are live.

For local validation before publication:

```bash
make brew-smoke
```

Release and smoke targets read the version from `pyproject.toml`.
`make release-prepare` verifies that the exact version is not already on PyPI
or npm before it rewrites local release scratch state. `make release-pypi` and
`make release-npm` run registry-specific remote checks so a partial release can
be completed without being blocked by a version that already exists in the
other registry.

The local smoke target creates a temporary formula copy that points at the
local sdist. It skips clearly when Homebrew is unavailable or a `crewplane`
formula is already installed. The formula declares `maturin` for the
`pydantic-core` runtime sdist, installs declared runtime resources first, and
then builds the `crewplane` sdist with build isolation disabled so the
Hatchling backend comes only from the pinned formula resources.
