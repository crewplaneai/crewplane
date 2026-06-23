# Installation

The public package name is `crewplane`. The installed console command is
`orchestrator`.

Crewplane requires Python 3.13 or newer. Provider CLIs are separate tools; install
and authenticate them outside Crewplane.

## Recommended Install

Use `uv tool` for an isolated command-line install:

```bash
uv tool install crewplane
orchestrator --help
```

## Other Install Methods

```bash
pipx install crewplane
python -m pip install crewplane
curl -fsSL https://raw.githubusercontent.com/crewplaneai/crewplane/main/install.sh | sh
brew tap crewplaneai/crewplane && brew install crewplane
npm install -g crewplane@alpha
```

For npm installs, the executable shims are written under
`$(npm config get prefix)/bin`. If npm reports a successful install but your
shell cannot find `orchestrator`, add that directory to `PATH` and confirm the
Node runtime is still available:

```bash
npm_prefix="$(npm config get prefix)"
export PATH="$npm_prefix/bin:$PATH"
command -v node
command -v orchestrator
```

For a local checkout:

```bash
git clone https://github.com/crewplaneai/crewplane.git
cd crewplane
python -m pip install .
```

For contributor setup, use the [development guide](../../DEVELOPMENT.md).

## Update

```bash
uv tool upgrade crewplane
pipx upgrade crewplane
python -m pip install --upgrade crewplane
```

For a local editable checkout:

```bash
git pull
python -m pip install -e '.[dev]'
```

## Uninstall

Use the matching package manager:

```bash
uv tool uninstall crewplane
pipx uninstall crewplane
python -m pip uninstall crewplane
npm uninstall -g crewplane
brew uninstall crewplane
```

## Provider CLIs

Crewplane does not install provider CLIs, does not manage provider credentials,
and does not sandbox provider CLI execution. Install the tools you plan to reference in
`.orchestrator/config.yml`, then confirm they work directly from your shell.

Common examples:

```bash
claude --version
codex --version
gemini --version
copilot version
```

Use [provider setup](provider-setup.md) for config examples and prompt transport
details.
