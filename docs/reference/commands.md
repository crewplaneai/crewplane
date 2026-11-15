# Command Reference

Use this page for exact CLI syntax. For task-oriented guidance, start with
[Running workflows](../guides/running-workflows.md).

| Command | Use when |
| --- | --- |
| `crewplane init` | Create project-local config and example workflows. |
| `crewplane onboarding` | Prepare real providers after the provider-free first run. |
| `crewplane validate` | Check workflow/config validity and provider availability, including configured diagnostic probes. |
| `crewplane run` | Execute, dry-run, or force a workflow run. |
| `crewplane runs` | List saved executions, read reports, open results, or inspect logs. |
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

On native Windows, initialization warns that the platform is not officially
supported, recommends WSL, and then continues.

## `crewplane onboarding`

Prepare real providers after the provider-free first run.

```bash
crewplane onboarding
```

Use this after `crewplane init`, `crewplane validate`, and a mock
`crewplane run` when you want to connect real providers without editing
config by hand. For the generated mock setup, the command checks config and
workflow files, detects supported provider CLIs on `PATH`, prompts for one or
more providers, updates unchanged generated defaults, and validates the result.

For projects already using CLI providers, onboarding offers Ollama connection
setup without changing workflows or existing model settings. After saving, run
`crewplane validate` on a workflow that uses `local` to check the model. See the
[plugin setup guide](../../plugins/crewplane-ollama/docs/setup.md#configure-crewplane)
for profile requirements and migration from older versions.

The command does not install provider software or authenticate providers.
If `.crewplane/` or generated files are missing, it may offer a
confirmed, non-overwriting init recovery step.

For provider selection and readiness checks, follow the
[quickstart onboarding step](../getting-started/quickstart.md#5-onboard-a-provider).

| Option | Description |
| --- | --- |
| `--discovery-timeout SECONDS` | Time budget in seconds for each local provider discovery phase. Must be positive and finite. Defaults to `15`. |

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

`validate` writes no run artifacts. The `cli` invoker checks executables and
awaits availability callbacks for configured capabilities. Ollama checks model
eligibility and sends a short prompt without tools; it cannot execute
model-selected commands. Registration validation itself runs no probes. See the
[plugin setup guide](../../plugins/crewplane-ollama/docs/setup.md#check-and-run)
for prerequisites and readiness behavior.
If `repeat_force_run_count` is set, it also checks that workspace isolation is
disabled and no worktrees are declared.

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
| `--dry-run`, `-n` | Preview the first run and, if repetition is set, show the total planned runs. Does not run providers or write run artifacts. |
| `--force` | Run from the beginning, even if results from an earlier run could be reused. |
| `--no-live` | Disable live topology dashboard output. |

When the mock invoker is active, `run` prints that no provider CLI commands will
be started. `run --dry-run` skips provider executable availability checks and
may read existing manifests for an advisory skip/resume message.
Dry-run also skips capability probes and remains artifact-free. Real runs check
availability before dedupe or resume selection, including Ollama diagnostic
inference; even a run eligible for reuse requires successful preflight.

If the workflow sets `repeat_force_run_count: 3`, one `crewplane run` command
runs the whole workflow three times, one after another. Each run starts from
the beginning. Crewplane stops if a run fails or is cancelled. `--force` does
not add more runs. See
[run a workflow more than once](../guides/running-workflows.md#run-a-workflow-more-than-once).

## `crewplane runs`

Inspect saved executions under the current directory's `.crewplane/`, without
loading current config or workflow files, probing providers, acquiring run locks,
repairing state, or attaching tmux. Deleted workflows and unavailable providers
do not prevent inspection. These commands never regenerate reports or create
missing artifact directories.

```bash
crewplane runs list
crewplane runs show RUN
crewplane runs open RUN [--results]
crewplane runs logs RUN [--node NODE] [--follow]
```

`RUN` is required for `show`, `open`, and `logs`. `NODE` is an exact logical
node ID, including any import namespace or foreach suffix. There are no filters,
JSON export, pagination, remote history, or retention controls in this group.

### Catalogue and selectors

Only valid current-schema execution manifests produce rows. Directories without
an execution manifest are excluded and reported separately; they may contain
preflight-only or incomplete attempts. Malformed or unsupported manifests are
reported on stderr without hiding valid runs. Unsafe metadata or unreadable
catalogue roots fail the command. Duplicate-context skips do not create execution
rows. Missing summaries or results directories do not exclude a valid run.

The catalogue sorts by persisted start time, newest first, then by full run key
for equal starts, never by directory modification time. Offset-bearing timestamps
are compared as instants. Timestamps without an offset are labeled recorded local
time and interpreted in the inspecting machine's local timezone for sorting;
the original timezone cannot be recovered. An absent state directory or empty
catalogue prints `No saved execution runs found.` and succeeds without creating
anything.

Selectors resolve in this order:

1. `latest`: the newest valid execution record.
2. An exact full run key.
3. A unique exact run ID.
4. A unique nonempty prefix of a run ID or full key.

Ambiguous selectors fail and list candidate workflow names, start times, and full
keys; they never silently choose the newest match. A selector is an identifier,
not a path: path separators and traversal selectors are rejected. An exact key
for an excluded directory reports that directory's diagnostic. Use `runs list`
to find a valid selector. The Run column normally shows the run ID; when IDs
collide across records it shows full keys for all colliding rows. Copy an exact
full key into scripts when the selected run must remain fixed.

### `runs list`

The table shows Workflow, Run, Started, Outcome, Elapsed, Reused, and Attention.
Outcome is the manifest's recorded lifecycle state: `succeeded`, `failed`,
`cancelled`, or `running (recorded)`. A recorded running state is not a liveness
check. Elapsed is the finalized manifest interval, not the saved summary's
execution-only interval; unfinalized or inconsistent intervals show `n/a`, not a
growing duration. Reused reports both completed-node and review-checkpoint counts.

Attention can combine `failed`, `cancelled`, `errors recorded`, `review unresolved`,
and `evidence incomplete`. Unresolved review means review continued after
consensus exhaustion or stopped for no progress; it is distinct from execution
failure. Ordinary warnings do not expand this compact column. Missing, corrupt,
partial, mismatched, or unfinalized relevant evidence remains incomplete even when
other facts are known. `none recorded` does not claim all reviews passed.

Completed-node reuse follows saved provenance and inherits only that node's
unresolved-review evidence, not unrelated source-run issues. Missing, mismatched,
or cyclic source evidence is incomplete. Checkpoints that resume review use the
current run's decision. A checkpoint resumed only to finalize results retains its
saved review decision, including unresolved review; missing or mismatched saved
decisions are incomplete.

### `runs show RUN` and `runs open RUN [--results]`

`show` prints the full key, recorded manifest outcome, summary path, and
relative-link base directory before rendering the saved `logs/summary.md`.
The body is a saved report snapshot and may predate recovery or finalization;
it is not rewritten to agree with the manifest header. Generated collapsible
section labels become visible headings in the terminal, with their bodies
retained. Relative links display their paths rather than terminal hyperlinks;
resolve them from the printed summary directory.

`open` submits a nonblocking request to open the exact saved summary in the local
associated application. `--results` instead opens the matching existing results
directory. Neither target falls back to the other, and no missing target is
created. The printed success message means the open request was submitted, not
that a desktop application was observed opening. On headless systems use `show`.

`show` fails without regeneration when a summary is missing, unreadable, unsafe,
changing, or invalid UTF-8. Retry if publication is in progress, or inspect
`runs logs RUN` and, if it exists, `runs open RUN --results`.

### `runs logs RUN [--node NODE] [--follow]`

Without `--node`, stdout contains the raw saved `logs/events.ndjson`, even if its
events cannot be decoded. With `--node`, stdout contains one saved provider log.
Selection details (node, task, provider, audit round, round, and path) and warnings
go to stderr, leaving stdout as raw text with its original whitespace.

Provider-log selection uses persisted events, not directory names or file mtimes.
Repeated lifecycle references are deduplicated by node, task, provider, audit
round, and round. Among invocations with a log reference and a retained start,
the newest recorded start timestamp wins; event order breaks ties. A terminal-only
invocation can supply a log when its start was not retained. The last such
invocation in event order wins only if there is no recorded-start candidate or its
event position follows the selected candidate's start. Its finish timestamp is
never treated as a recorded start. This selects one stream, not merged provider
output.

Completed-node reuse selects from that node's effective source run. Checkpoint
reuse prefers current-run invocation log evidence and only falls back to its
explicit checkpoint source when no such evidence exists. Both requested and
source runs are labeled when they differ. Corrupt, mismatched, or unfinished event
evidence prevents a confident node-log selection; retry or inspect raw run events
without `--node`. It is not permission to fall back to a different run.

No provider-log evidence can mean an unknown, pending, input-only, or uncaptured
node; it does not prove capture was disabled. A referenced file that disappeared
is reported separately. References must identify safe regular files under their
evidence run: relative references resolve there, and absolute references must
already be contained there. Symlinks, hardlink aliases, traversal, and external
paths are rejected. Moving an archive does not rebase saved absolute references.

`--follow` reads from the beginning, then emits appended bytes until Ctrl-C. It
resolves the run, node invocation, and file once: `latest` does not switch to a
new run, and later rounds do not replace the selected log. It stays at EOF even
when the run is complete. Ctrl-C ends only the reader, not the workflow. Deletion,
replacement, truncation, decoding failures, and read failures stop the reader;
rotation and reopening are not supported. A missing file fails immediately rather
than waiting for it to appear.

Usage, selection, and operational failures exit nonzero. Successfully inspecting
a historically failed or cancelled run exits zero. Saved logs may contain
sensitive provider output; inspection does not publish or upload it.

## `crewplane cleanup workspaces`

Remove generated workspace cache entries.

```bash
crewplane cleanup workspaces --dry-run
crewplane cleanup workspaces --yes
```

The command is advisory by default. Destructive cleanup happens only when
`--yes` is set and `--dry-run` is not set. With no status flags, current-project
cleanup selects only workspaces from completed runs. Explicitly selected
orphans are removed only when Crewplane can confirm that their run has ended and
no provider process is active. Other entries are kept and reported.

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
depend on current-project artifacts. Cross-project ownership and activity are
not verifiable, so preview the scan first. `--all-projects --yes` confirms that
you accept this risk.

Cleanup rejects workspace cache roots that are relative, symlinks, overlap the
project, overlap `.crewplane/`, overlap run artifact directories, or overlap Git
metadata paths.
