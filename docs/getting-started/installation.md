# Installation

Install the `crewplane` package. It provides the `crewplane` command.

Watch the installation walkthrough:

<div align="center">
  <video src="https://github.com/user-attachments/assets/50741c4d-6206-4434-a339-8ab537ea0134" controls width="80%" title="Installation walkthrough"></video>
</div>

## Requirements

- Use Python 3.13 or newer for `uv` and `pip` installs.
- Use Node.js 18 or newer for the npm wrapper.
- Install `tmux` if you want to use Crewplane's compact dashboard. The mock
  quickstart uses `--no-live` and does not require `tmux`.
- Provider CLIs are not required for installation or for the mock quickstart.
  Install and authenticate provider CLIs later when you are ready for real
  provider runs.

**Choose one of the following methods for installation:**

### uv

```bash
uv tool install crewplane
crewplane --help
```

The install succeeded if `crewplane --help` prints command help.

### pip

Use `pip` inside an activated virtual environment:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install crewplane
crewplane --help
```

### npm

```bash
npm install -g crewplane
crewplane --help
```

> ⚠️ **Note:**  The npm wrapper does not support native Windows. Use WSL on Windows.

<details>
<summary>npm PATH troubleshooting</summary>

Global npm installs create shims under `$(npm config get prefix)/bin`. If npm
reports a successful install but your shell cannot find `crewplane`, add that
directory to `PATH` and confirm Node.js is still available:

```bash
npm_prefix="$(npm config get prefix)"
export PATH="$npm_prefix/bin:$PATH"
command -v node
crewplane --help
```

</details>

<br>

For contributor setup, use the [development guide](../../DEVELOPMENT.md).

## First Run

After installation, run the mock quickstart from the project where you want
Crewplane to create `.crewplane/`:

```bash
crewplane init
crewplane validate
crewplane run --no-live
```

The mock quickstart does not require provider CLIs, API keys, provider
accounts, config edits, or `tmux`. Continue with the [quickstart](quickstart.md)
for the full first-run walkthrough.

## Compact Dashboard

Crewplane can show a compact dashboard during live runs. Install `tmux`, then
omit `--no-live`:

```bash
crewplane run
```

If `tmux` is missing, Crewplane warns and continues without the dashboard.

## Update

Use the update command for the install method you chose:

```bash
uv tool upgrade crewplane
python -m pip install --upgrade crewplane
npm update -g crewplane
```

## Uninstall

Use the uninstall command for the install method you chose:

```bash
uv tool uninstall crewplane
python -m pip uninstall crewplane
npm uninstall -g crewplane
```

## Provider CLIs

Crewplane does not install provider CLIs, manage provider credentials, or
sandbox provider CLI execution. Install each provider CLI outside Crewplane,
authenticate it directly, then confirm it works from your shell before adding
it to `.crewplane/config.yml`.

Common checks:

```bash
claude --version
codex --version
gemini --version
copilot version
```

Use [provider setup](provider-setup.md) for config examples and prompt transport
details.
