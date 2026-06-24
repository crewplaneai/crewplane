# crewplane npm Wrapper

This alpha npm package exposes Crewplane, a Markdown-native control plane for
AI coding CLIs. It installs the Python `crewplane` package into a private
virtual environment and provides a `crewplane` shim for npm and `npx` usage;
both bins delegate to the same Python console command.

```bash
npm install -g crewplane@alpha
crewplane --help
npx crewplane@alpha --help
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
keys, provider accounts, or config edits.

Global npm installs create shims under `$(npm config get prefix)/bin`. If the
install succeeds but `crewplane` is not found, add that directory to your
shell `PATH` and make sure `node` remains on `PATH`:

```bash
npm_prefix="$(npm config get prefix)"
export PATH="$npm_prefix/bin:$PATH"
command -v crewplane
```

Provider CLIs such as Claude, Codex, Gemini, Copilot, and Kilo are installed
and authenticated separately when you configure real provider runs. This
package does not install provider CLIs, manage credentials, or sandbox provider
execution.

The postinstall step creates the private environment with Python 3.13 by
default. Set `CREWPLANE_INSTALL_PYTHON` to an explicit interpreter path when a
maintainer smoke check must use a specific local Python executable.

For local maintainer smoke checks before publication:

```bash
CREWPLANE_INSTALL_FIND_LINKS=/path/to/wheelhouse \
CREWPLANE_INSTALL_NO_INDEX=1 \
npm install -g ./crewplane-0.1.0-alpha.2.tgz
```

If npm lifecycle scripts are disabled, the private environment is not created.
Run `npm rebuild crewplane` with lifecycle scripts enabled.
