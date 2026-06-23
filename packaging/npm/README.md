# crewplane npm Wrapper

This alpha npm package installs the Python `crewplane` package into a private
virtual environment and exposes the alpha `orchestrator` command. It also
provides a `crewplane` shim for npm and `npx` usage; both bins delegate to the
same Python console command.

```bash
npm install -g crewplane@alpha
orchestrator --help
npx crewplane@alpha --help
```

Global npm installs create shims under `$(npm config get prefix)/bin`. If the
install succeeds but `orchestrator` is not found, add that directory to your
shell `PATH` and make sure `node` remains on `PATH`:

```bash
npm_prefix="$(npm config get prefix)"
export PATH="$npm_prefix/bin:$PATH"
command -v orchestrator
```

Provider CLIs such as Claude, Codex, Gemini, Copilot, and Kilo are installed
and authenticated separately. This package does not install provider CLIs,
manage credentials, or sandbox provider execution.

The postinstall step creates the private environment with Python 3.13 by
default. Set `CREWPLANE_INSTALL_PYTHON` to an explicit interpreter path when a
maintainer smoke check must use a specific local Python executable.

For local maintainer smoke checks before publication:

```bash
CREWPLANE_INSTALL_FIND_LINKS=/path/to/wheelhouse \
CREWPLANE_INSTALL_NO_INDEX=1 \
npm install -g ./crewplane-0.1.0-alpha.1.tgz
```

If npm lifecycle scripts are disabled, the private environment is not created.
Run `npm rebuild crewplane` with lifecycle scripts enabled.
