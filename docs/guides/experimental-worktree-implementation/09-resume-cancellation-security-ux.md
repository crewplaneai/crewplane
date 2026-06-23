# Experimental Worktree Implementation: Resume, Cancellation, Security, Performance, and UX

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Duplicate Skip, Resume, `--force`, and Re-Execution
Every execution attempt that proceeds receives a new `run_id`, run directories,
workspace paths, and run-owned cached refs.

Duplicate skip:

- Source policy, `blob_exact` verification, invoker compatibility
  validation when relevant, project-source blob-byte signature compilation, and
  workflow signature compilation happen before duplicate lookup.
- A same-context successful run may skip only if ordinary artifacts, workspace
  lineage artifacts, `worktree_contract` descriptors, invoker compatibility
  descriptors, invocation-source descriptors, rendered workspace-file digest
  records, literal path resolution descriptors, provider final-tree
  descriptors, and project-source blob identity records validate.
- Project-source initial rendered bytes are validated from the current
  `run_base_commit` blob identities and canonical blob-byte digests.
- Candidate, review, remediation, and lineage-dependent rendered bytes are
  validated as persisted facts of the prior successful run by cross-checking
  node manifests and `workspace-state*.json`.
- Duplicate skip does not allocate a workspace or import bundles only to
  recompute candidate-source prompt bytes.
- Missing or corrupt `workspace-state*.json` or `workspace-bundles/*.bundle`
  prevents workspace-aware duplicate skip.
- Mismatched `worktree_contract` prevents workspace-aware duplicate skip.
- Mismatched invocation-source descriptors prevent workspace-aware duplicate
  skip.
- Mismatched rendered workspace-file digests prevent workspace-aware duplicate
  skip.
- Mismatched literal path resolution descriptors prevent workspace-aware
  duplicate skip.
- Snapshot cache directories are not required for duplicate skip.
- Input nodes have no workspace cache directory requirement.

Resume:

- Resume creates a fresh run.
- Resume never reuses old workspace directories or cached refs as source of
  truth.
- Successful node-boundary resume may hydrate ordinary artifacts and workspace
  lineage artifacts.
- Hydrated mutable nodes copy or re-materialize `workspace-state*.json` and
  `workspace-bundles/*.bundle` into the new run layout.
- Hydrated state uses the new run id and artifact-relative bundle path while
  preserving original source/result identities, selected `worktree_contract`,
  provider final-tree descriptors, invocation-source descriptors, rendered
  workspace-file digests, literal path resolution descriptors, invoker
  compatibility descriptors, and provenance.
- Missing or invalid mutable lineage artifacts make that node non-reusable.
- Downstream same-logical-worktree nodes may use hydrated upstream lineage only
  after the upstream result commit and bundle validate.
- Unsafe or ambiguous lineage history fails closed to rerun from the earliest
  safe boundary.

`--force`:

- bypasses successful duplicate skip
- bypasses failed/cancelled resume
- creates new workspaces and refs
- does not reuse prior bundles, cached refs, or workspaces

## Cancellation Semantics
Cancellation must leave enough state for audit and cleanup.

On SIGINT, SIGTERM, UI stop, or internal cancellation:

1. Mark run `cancelled`.
2. Stop scheduling new nodes.
3. Terminate active provider process groups through existing cancellation.
4. Wait for in-flight artifact and workspace-state writes where possible.
5. Mark active workspace states cancelled where possible.
6. Run idempotent cleanup for active managed workspaces selected for terminal
   cleanup.
7. Preserve selected `worktree_contract`, invoker compatibility descriptors,
   child-process launch status, invocation-source descriptors, provider
   final-tree diagnostics, provider `HEAD` diagnostics, cleanup/retention
   outcome, and rendered workspace-file digests in `workspace-state*.json`.

Cancellation cleanup is best-effort. A later cleanup command can complete
removal for any workspace cache directory left after a terminal cleanup failure.

## Security Considerations
Workspace confinement protects crewplane-controlled source materialization
and file-template reads. It is not a provider sandbox.

Provider CLIs may still read, write, execute, mutate Git metadata, or exfiltrate
outside the workspace if their own permissions allow it.

Security requirements:

- Resolve relative file-template locators against compiled source tree paths.
- Use Git object bytes for file-token injection in enabled mode.
- Resolve file-token bytes against the invocation source commit/tree, including
  current candidate commits for reviewers and remediation rounds.
- Use literal path semantics or pathspec-free APIs for Git commands that consume
  runtime-owned path operands.
- Sanitize inherited Git pathspec environment variables for runtime-owned Git
  commands and provider child processes.
- Verify exact path equality from Git NUL-delimited output before trusting
  object IDs or file locator resolutions.
- Reject byte-transforming Git attributes across the materialized source and
  result tree.
- Reject local/worktree config sources that can alter includes, filters,
  attributes, ignores, path resolution, worktree identity, object behavior,
  index behavior, or hidden state.
- Override local `core.filemode`, `core.symlinks`, `core.ignorecase`, and
  related core settings for runtime-owned Git operations and provider child
  environments, and fail if the override cannot be proven.
- Reject worktree-specific Git config in v1.
- Reject object alternates, grafts, and replacement behavior.
- Reject final provider `HEAD` movement in v1.
- Never use provider-created commit objects as lineage.
- Do not claim to detect transient provider commits after providers reset
  `HEAD`.
- Reject symlinks, gitlinks, trees, missing paths, non-UTF-8 bytes, and NUL
  bytes for enabled-mode workspace-file injection.
- Reject absolute paths unless allowlisted.
- Keep cache/workspace directories owner-private.
- Create POSIX directories with `0o700` where supported.
- Keep live workspaces outside project checkout, `.crewplane/`, artifact
  roots, lock roots, and Git metadata.
- Reject unsafe symlink paths and overlap in both unresolved and canonical path
  forms.
- Account for case-insensitive aliases and Unicode normalization aliases on
  macOS, WSL-mounted filesystems, and other POSIX filesystems with non-Linux
  path behavior.
- Sanitize path and ref components.
- Validate refs with `git check-ref-format --normalize`.
- Do not place live mutable worktrees under `.crewplane/`.
- Planned provider output/log paths under `.crewplane/` are the only
  intentional write channel outside provider `cwd`.
- Do not log environment secrets, provider credentials, prompt secrets, auth
  paths, raw rendered file contents, raw ignore-rule contents, raw local config
  values, or raw worktree config contents in workspace state.
- Verify bundle SHA-256 before `git bundle verify` or import.
- Verify expected source/result identities before lineage export.
- Use deterministic Git environment for runtime-owned commits.
- Require selected invoker adapter workspace compatibility before
  workspace-enabled real execution.
- Reject adapter-managed local process launch that bypasses runtime-owned
  controlled child-process launch.
- Use `git --no-optional-locks` and `GIT_OPTIONAL_LOCKS=0` for read-only
  tracked-state collection during preflight.
- Reject intent-to-add, skip-worktree, assume-unchanged, split-index,
  fsmonitor, and untracked-cache states that can hide tracked changes.
- Reject local `core.excludesFile` and effective `info/exclude` patterns in
  workspace-enabled v1.
- Use `GIT_NO_REPLACE_OBJECTS=1` for identity-sensitive Git operations.
- Use `GIT_NO_LAZY_FETCH=1` for runtime-owned operations.
- Unset `GIT_ATTR_SOURCE` for runtime-owned Git commands and provider child
  environments.
- Disable system/global Git attributes for runtime-owned operations.
- Reject local/worktree config includes and local/worktree
  `core.attributesFile`.
- Reject nonempty Git `info/attributes`.
- Capture final source changes from filesystem changes under runtime-owned
  staging only.
- Detect protected ref, identity, local config, worktree config,
  attribute-source, ignore-source, pathspec, object-store, and Git contract
  mutation.
- Do not claim to prevent provider-side mutation of user branches, remotes,
  config, hooks, pathspec behavior, object database, or external files.
- Treat worktree isolation as source-tree isolation only.
- Fail enabled-mode workspace paths on native Windows before source mutation.

Provider-created Git objects may be written into the shared repository object
database if providers attempt local commits. Workspace-enabled v1 does not use
those commits for lineage, but the objects may remain until normal Git retention
and garbage collection remove them. If a provider writes or commits sensitive
content, that content may remain in the object database. Stronger Git metadata
and object-store isolation belongs to a future clone/container backend.

## Performance Considerations
Git worktrees share the object database instead of cloning it per node. V1 still
performs full working-tree materialization for each executable snapshot and
mutable node.

Expensive paths:

- full-tree attribute validation
- full-tree path collision validation
- local config classification
- filesystem capability probes
- snapshot checkout materialization through isolated temporary indexes
- worktree checkout materialization
- literal path validation and exact path equality checks
- per-invocation candidate-source file-token resolution
- reviewer workspace materialization
- final-worktree diffing and result staging
- bundle creation/import
- Git metadata mutations
- snapshot digest verification
- cleanup of large directories
- provider-created ignored dependency caches

Performance requirements:

- V1 uses full materialized worktrees for `kind: worktree`.
- V1 snapshots use Git-native `read-tree`/`checkout-index` materialization when a
  directory is required.
- Input nodes avoid provider workspace materialization entirely.
- Provider invocations may run in parallel after provisioning.
- Git metadata operations are serialized per repository.
- Runtime may pre-provision ready nodes as pipelining, not parallel Git metadata
  mutation.
- Filesystem deletion of unregistered cache directories may run concurrently.
- Workspace paths and cached refs are unique per run/node.
- Bundle size is reported as actual post-export bytes.
- Preflight estimates source storage from Git tree/blob metadata and filesystem
  capacity.
- Estimates include snapshots, worktrees, reviewer workspaces, terminal cleanup
  failure retention, input file bytes, and bundle overhead where
  conservatively possible.
- Preflight warns when projected materialized storage would consume a fixed high
  fraction of available cache filesystem space.
- By default, preflight warns when estimated remaining cache filesystem space
  falls below 2 GiB. Users can configure
  `settings.workspace.disk.warn_free_bytes` and
  `settings.workspace.disk.fail_free_bytes`.
- Runtime rechecks free space before large post-invocation operations,
  including result capture and bundle export.
- Cache roots on a different filesystem than Git common dir are valid but warn.
- Diagnostics must state that provider-created dependency installs and build
  outputs may exceed source-tree estimates.

Large repositories are a known v1 cost. A 2 GB checkout with five mutable nodes
may require roughly 10 GB of workspace storage plus bundle and artifact
overhead. This is acceptable only if visible before provider invocation and
bounded by guardrails.

Sparse checkout, partial clone, lazy materialization, native Windows support,
LFS/filter-aware materialization, adapter-managed process launch, non-process
invokers, provider-created commit preservation, submodules, raw capture, custom
attributes, custom ignore matching, and clone/container backends are deferred.

## Observability and UX
Expose per-node workspace metadata in events, summaries, and failure output:

- logical worktree name and kind
- materialization mode
- selected `worktree_contract`
- selected invoker workspace compatibility mode when relevant
- child-process launch environment status
- clean policy
- source kind/node/commit/tree
- invocation source kind/node/commit/tree/candidate sequence
- Git version and object format
- active Git dir and common Git dir
- local config policy summary
- filesystem capability summary
- active worktree-config status
- workspace path
- effective invocation root
- snapshot digest
- input-node project-source blob identities when repo-relative file templates
  are used
- rendered workspace-file digest summaries
- literal path resolution summaries
- canonical workspace-file byte source
- provider final observed `HEAD` commit
- protected-state diagnostics
- explicit note that unreachable provider objects are not scanned in v1 when
  relevant
- candidate commit
- result commit/tree
- bundle path, digest, and byte size
- cached ref presence
- changed-file count
- ignored untracked-file excluded count
- excluded untracked-file note for `tracked_only`
- provisioning duration
- cleanup/retention status
- cancellation status
- protected Git identity/ref verification status
- reviewer workspace drift diagnostics
- failure operation and remediation

Failure messages should name workflow, node, role and round when relevant,
logical worktree name, kind, source commit, candidate commit, upstream node, Git
operation, artifact path when relevant, invoker compatibility issue when
relevant, `worktree_contract` issue when relevant, and concrete next step.

Example diagnostics:

```text
Logical worktree 'implementation_worktree' has unordered writers 'implement_api' and 'implement_ui'. Add a needs edge to serialize the source line or use separate worktree names.
```

```text
Node 'fix' selects worktree 'implementation_worktree' but upstream dependency 'prototype' selects a different worktree. Source lines are not merged implicitly; use artifacts or choose one logical worktree line.
```

```text
Workspace source policy failed: tracked file 'src/api.py' has unstaged changes. Commit or stash source changes, or run from a clean checkout.
```

```text
Workspace source policy warning: tracked_only excluded 7 untracked files. Excluded files are not visible to providers. Commit required files or use explicit allowlisted external resources.
```

```text
Workspace source policy failed: local Git config contains include.path. Workspace isolation v1 rejects local config includes because they can inject unsigned runtime behavior.
```

```text
Workspace invoker compatibility failed: invoker 'custom_cli' launches provider processes outside the runtime-owned command runner. Use the built-in cli invoker or implement the v1 workspace launch contract.
```

```text
Workspace blob_exact contract failed: path 'assets/logo.png' has unsupported attribute 'filter=lfs'. Workspace-enabled v1 does not support byte-transforming Git attributes.
```

```text
Workspace result capture failed: provider moved HEAD. Workspace isolation v1 requires providers to leave source edits as worktree changes with HEAD at the runtime baseline.
```

```text
Workspace result note: provider-created unreachable Git objects are not scanned or used for lineage in v1. Runtime captured only final worktree filesystem changes.
```

```text
Config validation failed: settings.default_workspace: Extra inputs are not permitted.
```

```text
Workspace-enabled runs are not supported on native Windows in v1. Use WSL or a POSIX environment.
```

```text
Input node 'requirements' did not allocate a provider workspace. Recorded Git blob identities, rendered-file digests, and the output artifact are the source of truth.
```
