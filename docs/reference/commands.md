# Command Reference

Use this page for exact CLI syntax. For task-oriented guidance, start with
[Running workflows](../guides/running-workflows.md).

| Command | Use when |
| --- | --- |
| `crewplane init` | Create project-local config and example workflows. |
| `crewplane onboarding` | Prepare one real provider after the provider-free first run. |
| `crewplane validate` | Check workflow/config validity without invoking providers. |
| `crewplane run` | Execute, dry-run, or force a workflow run. |
| `crewplane cleanup workspaces` | Remove generated workspace cache entries. |

## Global Options

Print the installed package release version and exit:

```bash
crewplane --version
crewplane -v
```

Both aliases print one line in this format:

```text
crewplane <installed-version>
```

Update the installed copy of Crewplane and exit:

```bash
crewplane --update
crewplane -u
```

These options run immediately. Crewplane does not read `.crewplane/`, load
config, discover workflows, or create run artifacts.

Crewplane only updates the copy that is currently running. Before running an
upgrade command, it checks the installation path against the package manager's
records. If it cannot verify which package manager installed that copy, it
stops and prints manual update instructions.

For installations created by `uv tool` (including the install script), `pipx`,
or Homebrew, Crewplane runs that manager's standard command: `uv tool upgrade`,
`pipx upgrade`, or `brew upgrade`. It leaves the manager's configured package
source, allowed versions, and security checks unchanged.

Crewplane cannot automatically update a global `npm` installation because `npm`
recreates Crewplane's private Python environment in a required `postinstall`
script. Crewplane prints the manual `npm` update commands instead.

After the package manager reports success, Crewplane starts a fresh process to
read the installed version. This avoids reusing version information loaded
before the update:

- If the version changed, Crewplane prints the previous and new versions.
- If the version stayed the same, the package manager did not select a
  different version from its configured package source and version rules.
  A newer release may still exist elsewhere.

If the package manager command fails, Crewplane exits with the same status code
and prints the command you can retry. It does not retry automatically, run
`sudo`, or change the command. If a global `pipx` installation requires
administrator privileges, rerun the displayed command using the elevation
method approved for your system.

Crewplane refuses to update these installations automatically:

- a direct `pip` or `uv pip` installation
- an editable checkout or an installation from a direct URL or local source
- a project-local `npm` dependency
- a temporary `uvx` or `npx` environment

In these cases, Crewplane exits without changing the installation and tells
you which environment, checkout, project, or command to update manually.

Older versions of the install script recorded a fixed Crewplane version in
`uv`'s installation record. Because `uv tool upgrade` honors that version pin,
it cannot select a newer release. If you used the default install settings,
remove the pin by reinstalling Crewplane once:

```bash
uv tool install --force crewplane
```

If the original installation used a custom Python interpreter, package source,
or constraints, rerun the original install command. Replace the exact
requirement, such as `crewplane==0.1.0`, with `crewplane` and keep all other
options. See the
[installation guide](../getting-started/installation.md#update) for exact
package-manager commands and manual update instructions.

## `crewplane init`

Initialize project-local config and example workflows.

```bash
crewplane init
```

Creates:

- `.crewplane/config.yml`
- `.crewplane/workflows/single-agent-review.task.md`
- `.crewplane/workflows/example-templates/**`
- `.crewplane/workflows/example-templates/sample-inputs/*.md`
- `.crewplane/preflight/fingerprint.key`, when possible

The generated config selects the `mock` invoker for deterministic provider-free
execution.

Existing files are not overwritten by template creation.

## `crewplane onboarding`

Prepare one real provider after the provider-free first run.

```bash
crewplane onboarding
```

Use this after `crewplane init`, `crewplane validate`, and a mock
`crewplane run` when you want to connect one real provider without editing
config by hand. The command checks generated config and workflow files, detects
supported provider CLIs on `PATH`, prompts for one provider, updates unchanged
generated defaults, and validates the result.

It does not authenticate provider tools, verify account or model access, or run
providers. If `.crewplane/` or generated files are missing, it may offer a
confirmed, non-overwriting init recovery step. There are no command-specific
options in v1.

## `crewplane validate`

Validate a workflow definition.

```bash
crewplane validate [TASKS_FILE] --config .crewplane/config.yml
crewplane validate [TASKS_FILE] -c .crewplane/config.yml
```

Arguments and options:

| Name | Description |
| --- | --- |
| `TASKS_FILE` | Workflow file to validate. Defaults to a single top-level `.crewplane/workflows/*.task.md`. |
| `--config`, `-c` | Config file. Defaults to `.crewplane/config.yml`. |

`validate` invokes no providers and writes no run artifacts. For the built-in
`cli` invoker, it checks configured provider CLI availability.

## `crewplane run`

Execute a workflow DAG.

```bash
crewplane run
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
crewplane run -t .crewplane/workflows/single-agent-review.task.md
```

Options:

| Name | Description |
| --- | --- |
| `--tasks`, `-t` | Workflow file. Defaults to a single top-level `.crewplane/workflows/*.task.md`. |
| `--config`, `-c` | Config file. Defaults to `.crewplane/config.yml`. |
| `--dry-run`, `-n` | Show the execution plan without invoking providers or writing run artifacts. |
| `--force` | Run fresh and intentionally bypass both duplicate skip and resume hydration. |
| `--no-live` | Disable live topology dashboard output. |

When the mock invoker is active, `run` prints that no provider CLI commands will
be started. `run --dry-run` skips provider executable availability checks and
may read existing manifests for an advisory skip/resume message.

Use `--force` when you want a fresh run and intentionally want to bypass both
duplicate skip and resume hydration.

## `crewplane cleanup workspaces`

Remove generated workspace cache entries.

```bash
crewplane cleanup workspaces --dry-run
crewplane cleanup workspaces --yes
```

The command is advisory by default. Destructive cleanup happens only when
`--yes` is set and `--dry-run` is not set.

See [Cleaning Up Workspace Caches](../guides/cleanup.md) for the operational
guide and run-record retention note.

Options:

| Name | Description |
| --- | --- |
| `--config`, `-c` | Config file. Defaults to `.crewplane/config.yml`. |
| `--dry-run` | Show workspaces that would be removed. |
| `--run` | Only clean workspaces for this run key. |
| `--older-than` | Only clean entries older than a duration such as `30m`, `12h`, or `7d`. |
| `--yes` | Confirm destructive workspace cleanup. |
| `--successful` | Only clean succeeded workspace states. |
| `--failed` | Only clean failed workspace states. |
| `--cancelled` | Only clean cancelled workspace states. |
| `--orphans` | Only clean cache paths without workspace state. |
| `--all-projects` | Clean every repository bucket under the workspace cache. |

By default, cleanup is scoped to the current Git repository. Non-Git projects
must use `--all-projects`. `--all-projects` cannot be combined with
`--orphans`, `--successful`, `--failed`, or `--cancelled` because those filters
depend on current-project workspace-state artifacts.

Cleanup rejects workspace cache roots that are relative, symlinks, overlap the
project, overlap `.crewplane/`, overlap run artifact directories, or overlap Git
metadata paths.
