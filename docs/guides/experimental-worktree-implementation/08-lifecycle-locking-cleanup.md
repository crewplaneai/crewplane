# Experimental Worktree Implementation: Lifecycle, Locking, and Cleanup

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Run Manifests and Node State
Node-boundary manifests include workspace descriptors for successful
enabled-mode nodes:

- logical worktree name and kind
- materialization mode
- selected `worktree_contract`
- source kind and source commit
- source tree
- invocation-source descriptors for each provider invocation
- selected invoker workspace compatibility class when a provider invocation is
  configured
- child-process environment application status for process invocations
- effective invocation root
- provider final observed `HEAD` commit diagnostics for mutable nodes
- protected-state diagnostics for failed mutable nodes
- result commit and tree for `kind: worktree`
- rendered workspace-file descriptors, injected-byte SHA-256 values, and literal
  path resolution descriptors
- workspace-state artifact descriptor
- bundle artifact descriptor for `kind: worktree`
- bundle SHA-256 and byte size
- input-node blob identities in preflight/render descriptors when input nodes
  use repo-relative file templates
- resume origin when hydrated from another run

A successful `kind: worktree` node is reusable for duplicate skip or resume only
when all required ordinary artifacts and workspace lineage artifacts validate,
including selected `worktree_contract`, invocation-source descriptors, provider
final-tree descriptors, rendered workspace-file digest validation, and bundle
validation.

## Workspace Placement and Lifecycle
Workspace directories live outside the project checkout and outside project
`.orchestrator/`.

Placement is split:

- Live materializations: `settings.workspace.cache_root`
- Canonical lineage and audit artifacts: project
  `.orchestrator/execution-stages/` and `.orchestrator/execution-results/`
- Planned provider outputs/logs: orchestrator-owned artifact paths under
  `.orchestrator/`
- Operation-scoped temporary Git indexes: owner-private system temporary
  directories

Default cache root:

```text
macOS: ~/Library/Caches/orchestrator-cli/
Linux/POSIX/WSL: ${XDG_CACHE_HOME:-~/.cache}/orchestrator-cli/
```

Mutable worktrees:

```text
<cache-root>/workspaces/<repo-id>/<run-key-name>/<node-slug>/
```

Writable disposable snapshots:

```text
<cache-root>/snapshots/<repo-id>/<run-key-name>/<node-slug>/
```

Disposable reviewer workspaces:

```text
<cache-root>/review-workspaces/<repo-id>/<run-key-name>/<node-slug>/<task-slug>-round-<n>/
```

Canonical artifacts:

```text
.orchestrator/execution-stages/<run-key-name>/
.orchestrator/execution-results/<run-key-name>/
```

Path and ref safety:

- Derive `repo-id` from SHA-256 of canonical common Git directory, project root,
  and Git object format.
- Convert workflow names, run identifiers, task ids, and node ids to ASCII slugs
  containing only `[a-z0-9._-]`.
- Replace other characters with `-`, collapse repeated separators, trim
  leading/trailing separators, and reject empty slugs.
- Reject reserved names: `.`, `..`, `logs`, `manifests`, `preflight`, names
  ending in `.lock`.
- Bound each slug to a fixed maximum length and append a digest suffix when
  truncated.
- Build Git refs only from sanitized components.
- Validate refs with `git check-ref-format --normalize`.
- Pass paths and refs as subprocess arguments, never shell-interpolated strings.
- Treat filesystem paths and Git tree paths as distinct types. Convert only
  through explicit project-root-to-Git-top and workspace-root mappings.
- Reject pre-existing workspace paths that are files, symlinks, or non-empty
  directories.
- Reject cache/workspace roots that overlap project, artifact roots, lock roots,
  active Git dir, or common Git dir.
- On case-insensitive filesystems, compare normalized case-folded paths as well
  as canonical paths.
- If generated paths exceed platform limits, fail with remediation instead of
  silently truncating outside slugging rules.

## Git Metadata Locking
Git worktree administration lives in the repository common Git directory,
resolved with `git rev-parse --git-common-dir`.

The workspace manager serializes shared metadata operations with both:

- per-repository async lock in-process
- interprocess POSIX advisory lock

The interprocess lock path is:

```text
<git-common-dir>/orchestrator-cli/workspace.lock
```

V1 uses `fcntl.flock`. Platforms without compatible POSIX advisory locking fail
before mutating workspace state.

The lock protects:

- worktree create/remove/lock/unlock/prune
- bundle import/export
- cached-ref writes/deletes
- temporary export refs
- object database mutation from bundle import and runtime-owned result object
  writes
- cleanup
- opportunistic `git gc --auto`
- protected ref and identity verification windows where needed

The lock coordinates cooperating orchestrator processes only. It does not
prevent user Git commands, provider Git commands, IDEs, hooks, or other external
processes from mutating repository metadata. Runtime must re-read and verify
expected protected refs, commits, trees, active worktree-config absence,
selected `worktree_contract`, and bundle outputs after lock-protected
operations and before trusting lineage.

If external prune or manual deletion removes administrative entries, cleanup
must remove orphaned cache directories using persisted workspace-state artifacts
and must not treat missing worktree metadata alone as corruption.

## Git Identity and Protected Ref Verification
Git worktrees share repository metadata. A provider with broad shell access can
intentionally mutate shared Git state. Workspace isolation is source-tree
isolation only.

Runtime must avoid false positives from unrelated Git activity. It must not fail
solely because user branches, remote-tracking refs, ordinary repo config
unrelated to protected runtime behavior, hooks, fetched objects, or unreachable
object additions changed.

Runtime must fail when protected execution invariants change:

- expected source commit or tree cannot be verified
- expected candidate/result commit or tree cannot be verified
- selected `worktree_contract` cannot be verified
- orchestrator-owned refs under the current run namespace are missing, moved, or
  unexpectedly created
- provider mutates another node's orchestrator-owned refs
- provider creates nonempty Git `info/attributes`
- provider creates effective non-comment, non-blank patterns in
  Git `info/exclude`
- provider introduces unsupported local/worktree Git config keys
- provider creates or modifies `.gitattributes`
- provider leaves `HEAD` different from the runtime baseline
- provider creates active worktree-specific config
- provider introduces object alternates, grafts, or replacement behavior
- bundle import/export verification does not match `workspace-state.json`
- replacement refs, grafts, alternates, pathspec environment variables, or
  config affect runtime-owned identity-sensitive Git operations

Runtime-owned Git commands use a sanitized environment:

- pass paths and refs as argv
- unset inherited Git variables that redirect repository discovery, object
  lookup, index state, refs, namespaces, config, attributes, attribute source,
  pathspec behavior, lazy fetching, or discovery ceilings
- set `GIT_NO_REPLACE_OBJECTS=1`
- set `GIT_NO_LAZY_FETCH=1`
- set `GIT_TERMINAL_PROMPT=0`
- set `GIT_CONFIG_NOSYSTEM=1`
- set `GIT_CONFIG_GLOBAL=/dev/null`
- set `GIT_ATTR_NOSYSTEM=1`
- use `git --literal-pathspecs` for commands that consume pathspec operands
- use explicit `git -C <path>` or `--git-dir`/`--work-tree`
- use deterministic commit environment variables
- disable commit/tag signing with explicit config overrides
- override local core keys required for deterministic filemode, symlink, case,
  Unicode, line-ending, attribute, and ignore behavior
- reject local/worktree config includes rather than following them
- use plumbing commands for runtime-owned commits

The inherited exact unset list includes:

- `GIT_DIR`
- `GIT_WORK_TREE`
- `GIT_COMMON_DIR`
- `GIT_INDEX_FILE`
- `GIT_OBJECT_DIRECTORY`
- `GIT_ALTERNATE_OBJECT_DIRECTORIES`
- `GIT_NAMESPACE`
- `GIT_CEILING_DIRECTORIES`
- `GIT_DISCOVERY_ACROSS_FILESYSTEM`
- `GIT_CONFIG_SYSTEM`
- `GIT_CONFIG_GLOBAL`
- `GIT_CONFIG_NOSYSTEM`
- `GIT_CONFIG_COUNT`
- `GIT_CONFIG_PARAMETERS`
- `GIT_ATTR_NOSYSTEM`
- `GIT_ATTR_SOURCE`
- `GIT_LITERAL_PATHSPECS`
- `GIT_GLOB_PATHSPECS`
- `GIT_NOGLOB_PATHSPECS`
- `GIT_ICASE_PATHSPECS`
- `GIT_ASKPASS`
- `SSH_ASKPASS`

The runtime helper expands prefix variables into exact names before process
launch:

- `GIT_CONFIG_KEY_`
- `GIT_CONFIG_VALUE_`

Process-based provider invocations receive a controlled Git discovery,
pathspec, and config-overlay environment from the command runner. Runtime
unsets inherited Git variables that would make provider-side `git` commands
target a repository, index, object store, namespace, config injection,
attribute source, pathspec behavior, lazy fetch path, or discovery ceiling
outside the effective workspace. Runtime may set `GIT_CEILING_DIRECTORIES` to
the workspace checkout root's parent so Git can discover metadata inside the
workspace but cannot climb above it.

Runtime does not promise to suppress hooks, filters, or pathspec behavior in
provider-run Git commands after the provider starts. Instead, v1 rejects final
`HEAD` movement, unsupported config, unsupported attributes, and protected-state
drift before lineage export. Provider sandboxing, permissions, and command
policy own the provider-command boundary.

## Cleanup
Cleanup is idempotent and safe to retry.

Terminal cleanup order:

1. Mark run terminal in run manifest.
2. Stop scheduling new nodes.
3. Terminate or drain active provider invocations.
4. Wait for in-flight artifact and workspace-state writes where possible.
5. Unlock worktrees selected for cleanup.
6. Remove orchestrator-owned registered worktrees with
   `git worktree remove --force <path>`.
7. Remove snapshot directories and unregistered residual cache directories.
8. Remove operation-scoped temporary indexes if any remain.
9. Remove run-owned cached refs selected for cleanup.
10. Remove disposable reviewer workspaces.
11. Preserve or remove canonical artifacts according to artifact retention
    behavior.
12. Optionally run `git gc --auto` under repository lock as a best-effort tail
    step.

Cleanup must never use raw directory deletion as the normal path for registered
worktrees. Raw deletion is limited to snapshots, temporary indexes,
unregistered residual cache directories, and recovery paths where
`git worktree list --porcelain` proves no live registration points at the
directory.

Input nodes have no live workspace directory and no workspace-cache cleanup
step. Their canonical node artifacts remain under `.orchestrator/` according to
ordinary artifact retention.

`git gc --auto` is never run during provisioning, result capture, candidate
commit creation, or lineage export. It is best-effort after cleanup and must not
be required for run success.

Retention defaults:

- successful workspaces are removed after terminal success when
  `cleanup_on_success: true`
- failed active workspaces are removed best-effort after terminal state is
  recorded
- cancelled active workspaces are removed best-effort after terminal state is
  recorded
- canonical `workspace-state.json` and `workspace.bundle` remain under
  `.orchestrator/`

V1 CLI cleanup surface:

```text
orchestrator cleanup workspaces [--dry-run] [--run <run-key>] [--older-than <duration>] [--successful] [--failed] [--cancelled] [--orphans] [--all-projects] [--yes]
```

Default mode is advisory. Destructive cleanup requires `--yes`. Status and
orphan filters use current-project workspace-state artifacts, so they are not
valid with `--all-projects`.

The command removes workspace cache directories, worktree registrations,
reviewer workspaces, temporary indexes, and run-owned cached refs. It does not
remove canonical lineage, provider outputs, findings, run manifests, or result
artifacts.
