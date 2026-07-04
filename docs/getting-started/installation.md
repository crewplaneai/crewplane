# Installation

Install Crewplane to add the `crewplane` command to your shell.

After installation, open the project where Crewplane should create `.crewplane/`
and write local run records.

The first run does not need provider CLIs, API keys, provider accounts, or
config edits. Those come later, after you have seen the mock workflow complete.

Watch the installation and first mock run walkthrough:

<div align="center">
  <video src="https://github.com/user-attachments/assets/50741c4d-6206-4434-a339-8ab537ea0134" controls width="80%" title="Installation walkthrough"></video>
</div>

## Before You Install

Choose the install method that matches your environment:

- Use Python 3.13 or newer for `uv`, `pipx`, and `pip` installs.
- Use Node.js 18 or newer for the npm wrapper.
- Use macOS or WSL/Ubuntu-style Linux for the install script.
- Install `tmux` to use Crewplane's compact live dashboard during runs. Crewplane
  can continue without it, but `tmux` gives you the best first-run experience.

Provider CLIs are intentionally not part of installation. Install and
authenticate Claude, Codex, Gemini, Copilot, or other provider tools later when
you are ready for real provider runs.

## Install With uv

For most users, `uv tool install` is the simplest way to install the command in
an isolated environment:

```bash
uv tool install crewplane
crewplane --help
```

The install succeeded when `crewplane --help` prints command help.

## Install With pipx

Use `pipx` if you prefer Python CLI tools in isolated environments:

```bash
pipx install crewplane
crewplane --help
```

If `pipx` uses an older Python by default, pass Python 3.13 explicitly:

```bash
pipx install --python python3.13 crewplane
```

## Install With The Script

On macOS and WSL/Ubuntu-style Linux, use the install script when you want one
command that can bootstrap `uv` and install Crewplane as a `uv` tool:

```bash
curl -fsSL https://raw.githubusercontent.com/crewplaneai/crewplane/master/install.sh | sh
crewplane --help
```

## Install With Homebrew

On macOS, use the Crewplane Homebrew tap:

```bash
brew tap crewplaneai/crewplane
brew install crewplane
crewplane --help
```

## Install With pip

If you prefer `pip`, use an activated virtual environment so the package and
command stay together:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install crewplane
crewplane --help
```

## Install With npm

The npm package provides a wrapper around the Crewplane command:

```bash
npm install -g crewplane
crewplane --help
```

> Note: the npm wrapper does not support native Windows. Use WSL on Windows.

<details>
<summary>npm PATH troubleshooting</summary>

Global npm installs create shims under `$(npm config get prefix)/bin`. If npm
reports a successful install but your shell cannot find `crewplane`, add that
directory to `PATH` and confirm Node.js is still available:

```bash
npm_prefix="$(npm config get prefix)"
export PATH="$npm_prefix/bin:$PATH"
command -v node
command -v crewplane
crewplane --help
```

</details>

<br>

Contributors should use the [development guide](../../DEVELOPMENT.md) instead
of the package install path.

## Run The First Workflow

After installation, move into the project where Crewplane should create
`.crewplane/` and run the mock quickstart:

```bash
crewplane init
crewplane validate
crewplane run
```

That path creates local Crewplane files, validates the generated workflow, and
runs it with deterministic mock output. It does not require provider CLIs, API
keys, provider accounts, token spend, or config edits.

Continue with the [Quickstart](quickstart.md) for the full walkthrough of what
those commands create and how to inspect the run record.

## Recommended: Compact Dashboard

Install `tmux` before your first run so Crewplane can open the compact live
dashboard. It gives you a real-time view of the workflow, node status, and
provider output while `crewplane run` is active:

```bash
crewplane run
```

If `tmux` is missing, Crewplane warns and continues without the dashboard.

<div align="center">
  <img src="https://raw.githubusercontent.com/crewplaneai/crewplane/master/.github/crewplane-splash.png" alt="Crewplane live dashboard" width="80%">
</div>

## Provider CLIs Come Later

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

Use [Provider setup](provider-setup.md) when you are ready to switch from the
mock invoker to real provider commands.

## Update

Use the update command for the install method you chose:

```bash
# uv or install script
uv tool upgrade crewplane

# Homebrew
brew update
brew upgrade crewplane

# pipx
pipx upgrade crewplane

# pip
python -m pip install --upgrade crewplane

# npm
npm update -g crewplane
```

## Uninstall

Use the uninstall command for the install method you chose:

```bash
# uv or install script
uv tool uninstall crewplane

# Homebrew
brew uninstall crewplane

# pipx
pipx uninstall crewplane

# pip
python -m pip uninstall crewplane

# npm
npm uninstall -g crewplane
```

## Next

Continue to the provider-free [Quickstart](quickstart.md) to initialize a
project, validate the generated workflow, run it, and inspect the saved run
record.

Or return to the [documentation index](../index.md).
