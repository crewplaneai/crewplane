# Experimental Worktree Implementation: Implementation Milestones and Tests

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Implementation Milestones
The implementation must land in slices. No public workspace-enabled release is
complete until all required v1 slices are implemented and covered. Earlier
slices may merge behind the disabled default and must preserve current
project-root execution.

### Slice 1: Schema, Config, and Disabled-Mode Preservation
- Add `settings.workspace.enabled`, `cache_root`, and `cleanup_on_success`.
- Remove `settings.default_workspace` from schema, generated templates, README
  examples, and config docs.
- Add workflow-level `worktrees` declarations and node-level `worktree`
  selectors.
- Reject workflow workspace declarations when disabled.
- Preserve disabled-mode non-Git execution semantics.
- Add docs and template updates proving workspace isolation remains disabled by
  default.

### Slice 2: Invocation `cwd` and Adapter Capability Contract
- Add mandatory `cwd` to `AgentInvoker.invoke`.
- Add mandatory `cwd` and optional `child_environment` to `CommandRunner`.
- Keep `InvocationPlan` free of `cwd`, environment policy, Git policy,
  invocation-source policy, and workspace policy.
- Add optional `WorkspaceCompatibleInvokerAdapter.workspace_capabilities()`.
- Normalize missing workspace capability metadata to unsupported.
- Ensure missing capability metadata does not affect disabled-mode execution.
- Support built-in `cli` through `runtime_command_runner`.
- Support built-in `mock` through `mock_no_child_process`.
- Reject adapter-managed local process launch in workspace-enabled v1.
- Teach mock invoker to record workspace context, invocation-source context,
  child-environment status, invoker compatibility mode, and `cwd`.

### Slice 3: Enabled-Mode Workflow Validation and Source Gate
- Normalize effective enabled-mode workspace configs.
- Validate same-logical-worktree ordering and mutable lineage.
- Reject unordered same-worktree writers and implicit cross-worktree merges.
- Reject invalid input-node and mutable multi-executor configs.
- Define input nodes as non-provider nodes that never allocate managed
  workspaces.
- Gate all Git probes behind `enabled: true`.
- Require Git 2.34.1 or newer plus required capability probes.
- Enforce clean-start, including staged/unstaged changes, intent-to-add,
  skip-worktree, and assume-unchanged.
- Detect shallow, partial/promisor, sparse, submodule, object-alternate, graft,
  unsupported platform, and unsupported lock states.
- Support detached `HEAD`.
- Validate local Git config with `--no-includes`.
- Classify local config into rejected, overridden, and ignored-neutral keys.
- Explicitly handle `core.filemode`, `core.symlinks`, `core.ignorecase`, and
  `core.precomposeunicode`.
- Validate active worktree config state and reject worktree-specific config.
- Validate local attribute-source and ignore-source policy.
- Validate byte-transforming attribute rejection for the full source tree.
- Validate source-tree path collision policy.
- Validate cache-root placement.
- Ensure validate and dry-run remain artifact-free and side-effect-free.

### Slice 4: Workspace-Aware File Locators and Inputs
- Preserve ADR 0012 static resources when disabled.
- Compile repo-relative file tokens as workspace-file locators when enabled.
- Resolve project-source initial locators to Git blob identities and canonical
  blob-byte digests before duplicate lookup.
- Resolve project-source initial locators with literal Git path handling and
  exact returned-path equality.
- Reject symlink, tree, gitlink, missing, non-UTF-8, and NUL-containing
  project-source locators.
- Preserve import source-root mapping and project-root-to-Git-top mapping.
- Keep allowlisted absolute paths static.
- Implement invocation-source descriptors.
- Resolve reviewer/remediation file tokens from current candidate commits.
- Implement input-node output assembly from compiled project-source blob
  records without allocating provider workspaces.

### Slice 5: Snapshot and Worktree Provisioning
- Provision snapshot workspaces through isolated temporary index checkout.
- Provision mutable detached locked worktrees.
- Support the documented `add --lock --reason` fallback.
- Verify source commits and trees.
- Verify newly created worktrees have no active worktree-specific config.
- Add POSIX repository locks.
- Apply literal-path rules to runtime-owned Git path operations.
- Apply runtime Git config overlays for filemode, symlink, case, Unicode,
  line-ending, attribute, and ignore behavior.
- Pass effective workspace `cwd` to invocations.
- Add snapshot mutation verification.
- Add workspace-state skeletons for running/succeeded/failed/cancelled nodes.

### Slice 6: Result Capture, Lineage Commits, and Bundles
- Reject final provider `HEAD` movement before result capture.
- Reject protected-state drift before result capture.
- Do not scan for or preserve unreachable provider-created objects.
- Reset runtime-owned index state before staging final filesystem changes.
- Stage tracked, deleted, type-changed, executable-bit, symlink, and untracked
  non-ignored changes.
- Reject provider-created or modified `.gitattributes`.
- Reject provider-created worktree-specific config.
- Verify result trees satisfy `blob_exact`.
- Create candidate and result commits with deterministic metadata.
- Export/import bundles and verify digests.
- Manage cached refs.
- Add retry baseline/reset behavior for mutable workspaces.

### Slice 7: Resume, Duplicate Skip, Cleanup, and Reviewers
- Extend node state with lineage descriptors, contract descriptors, invoker
  compatibility descriptors, child-process launch descriptors,
  invocation-source descriptors and rendered workspace-file descriptors.
- Verify lineage artifacts, selected `worktree_contract`, provider final-tree
  descriptors, invocation-source descriptors, literal path resolution
  descriptors, and rendered-file digests before duplicate skip.
- Hydrate lineage artifacts and descriptors for resume.
- Add reviewer disposable workspaces.
- Resolve reviewer/remediation file tokens from current candidate commits.
- Detect reviewer drift, including reviewer `HEAD` movement as discarded
  non-lineage drift.
- Add `crewplane cleanup workspaces`.
- Add cleanup dry-run and destructive `--yes`.

### Slice 8: Observability, Docs, and Hardening
- Add workspace fields to events and summaries.
- Add invoker compatibility, selected `worktree_contract` status, child-process launch
  status, invocation-source summaries, rendered workspace-file summaries, local
  config policy summaries, filesystem capability summaries,
  ignored-untracked counts, final `HEAD` diagnostics, protected-state
  diagnostics, and active worktree-config status to events and summaries.
- Add failure remediation.
- Surface provisioning duration, materialization mode, rendered-file counts, and
  bundle size.
- Update README, config reference, workflow syntax, architecture docs, and
  templates.
- Document POSIX-only v1 and WSL guidance.
- Document dependency materialization and that workspace isolation is not
  provider sandboxing.
- Document removal of `settings.default_workspace`.
- Document the explicit v1 limitation that unreachable provider-created Git
  objects are not scanned or used for lineage.

## Test Matrix
Required deterministic coverage includes these categories:

- Disabled mode: missing/disabled workspace config preserves project-root
  execution in non-Git projects, no Git probes run, no workspace state is
  created, ADR 0012 static file behavior is preserved, authored workspace declarations
  fail when disabled, `settings.default_workspace` is rejected, generated
  templates omit removed fields, and disabled-mode custom invokers without
  workspace capability metadata still run.
- Enabled schema, validation, and invoker compatibility: defaults normalize,
  invalid worktree declarations and lineage fail, fan-in source inheritance is
  explicit, input nodes do not allocate managed workspaces, mutable
  multi-executor nodes fail, snapshot multi-executor nodes pass with distinct directories,
  imported lineage is namespace-rewritten, native Windows fails, built-in `cli`
  and `mock` declare v1 support, unsupported invokers fail enabled real runs,
  and mock records workspace context.
- Source policy, Git contract, and path handling: no repository, unborn `HEAD`,
  old/missing Git capabilities, shallow/partial/promisor/sparse/submodule
  states, object alternates, grafts, hidden index flags, unsupported config,
  nonempty local attributes or excludes, dirty source state, unsafe cache roots,
  unsafe path aliases, and literal path handling regressions fail before
  provider invocation. Validate and dry-run remain side-effect-free.
- Attribute, config, and LFS policy: effective LFS/custom filters, `ident`,
  `working-tree-encoding`, text/eol conversion, legacy crlf conversion,
  provider-created `.gitattributes`, provider-created worktree config, local
  config includes, local/worktree attribute/exclude injections, and global or
  system attribute influence are rejected or neutralized according to
  `blob_exact`.
- Workspace-file locators: repo-relative file tokens inject canonical Git blob
  bytes, literal path semantics are enforced for wildcard/pathspec-looking
  names, imported source roots cannot be bypassed, reviewer/remediation tokens
  read from the current candidate, missing runtime-generated files fail before
  invocation, symlinks/trees/gitlinks/non-regular targets fail, input-node
  `\{\{file:...\}\}` is assembled from compiled project-source blob records
  without provider workspace allocation, and duplicate skip/resume validate
  rendered descriptors without allocating workspaces only to recompute
  candidate bytes.
- Snapshot behavior: snapshots materialize from `run_base_commit`, contain no
  `.git`, use isolated temporary indexes, reject upstream source, detect
  mutation, allocate distinct directories for parallel snapshot executors, omit
  planned artifact paths from digests, and clean up snapshot directories and
  temp indexes safely.
- Worktree and result capture: provider invocation receives expected `cwd`,
  `AgentInvoker.invoke` and `CommandRunner` require `cwd`, process child
  environment is applied by the command runner, worktrees are detached and
  locked, final provider `HEAD` movement fails, provider-created commits are
  never lineage, runtime resets index state before staging filesystem changes,
  ignored untracked files are excluded, result trees validate against
  `blob_exact`, deterministic commit metadata is stable, and executable
  bit/symlink changes are captured when probes pass.
- Bundles, resume, and lineage: bundle SHA-256 is verified before import,
  downstream rehydrates after cached ref deletion, chained lineage imports in
  dependency order, temporary refs are removed, lineage artifacts and contract
  descriptors gate duplicate skip and resume, `--force` creates fresh workspaces
  and refs, and workflow signatures include source identity and locator data
  while excluding future result/candidate outputs.
- Locking, cleanup, retries, and review loops: metadata mutations serialize per
  repository, active worktrees are locked/unlocked, cleanup uses
  `git worktree remove --force` for registered worktrees, cleanup is idempotent,
  dry-run cleanup is advisory, destructive cleanup requires `--yes`, retryable
  attempts reset tracked/untracked/ignored generated changes and provider
  metadata, cancellation preserves audit state, reviewers use disposable
  non-lineage workspaces, reviewer/remediation file tokens read from current
  candidates, and reviewer drift is reported and discarded.
- Security, placement, observability, and UX: cache-root overlap/symlink/reserved
  slug/ref safety is enforced, workspace state omits secrets and raw rendered
  contents, runtime-owned Git commands sanitize environment and use literal
  pathspecs, process providers receive controlled Git discovery/config/pathspec
  environment, unrelated user ref changes do not fail by themselves, protected
  crewplane ref mutation is detected, run summaries include workspace
  logical-worktree/kind/source/result/contract/materialization/invoker/child-environment and
  rendered-file summaries, diagnostics include concrete remediation, disk
  pressure warnings occur before provider invocation, and non-filesystem
  artifact backends fail real execution clearly.

## Final ADR Coverage Checklist

Workspace-enabled v1 is complete only when the implementation and tests prove
each item below. This checklist is part of the ADR 0016 completion contract.

- [x] Slice 1 schema/config/default behavior: workspace isolation is disabled by
  default, `settings.default_workspace` is rejected, disabled non-Git execution
  is preserved, and templates/docs show `settings.workspace.enabled: false`.
- [x] Slice 2 invocation boundary: provider invocation and command-runner
  contracts require explicit `cwd`; built-in invokers declare workspace
  capability metadata; disabled-mode custom invokers remain valid.
- [x] Slice 3 validation/source gate: enabled-mode workflow policy, invoker
  compatibility, Git source discovery, Git contract, cache placement, native
  Windows rejection, and side-effect-free validate/dry-run gates are covered.
- [x] Slice 4 workspace-file contract: enabled repo-relative file tokens compile
  to workspace-file locators, project-initial blobs are read from Git object
  bytes, input nodes avoid provider workspace allocation, and runtime-dynamic locators resolve from
  invocation source commits.
- [x] Slice 5 runtime provisioning: snapshots and detached worktrees are
  materialized under owner-private cache paths, invocations receive effective
  workspace `cwd`, child environment state is recorded, and running/terminal
  workspace-state envelopes are emitted.
- [x] Slice 6 lineage/capture/bundles: mutable result capture rejects ambiguous
  provider Git state, creates runtime-owned candidate/result commits, exports
  verified bundles, resets retryable attempts, and records that unreachable
  provider-created objects are not scanned in v1.
- [x] Slice 7 resume/skip/reviewer/cleanup: duplicate skip and resume validate
  workspace state, bundles, invocation-source descriptors, rendered workspace
  file descriptors, and literal path facts; reviewer and
  remediation file tokens resolve from current candidates; cleanup is advisory
  by default and destructive only with `--yes`.
- [x] Slice 8 observability/docs/hardening: events and run summaries surface
  logical worktree name, kind, source/result identities, materialization, invoker launch
  mode, child environment status, rendered-file counts, bundle size, retention,
  and diagnostics; docs state POSIX/WSL-only v1, dependency materialization
  limits, non-sandbox semantics, removed `settings.default_workspace`, and the
  unreachable-provider-object limitation.

Validation evidence for this checklist lives in deterministic pytest coverage
under `tests/unit/core`, `tests/unit/cli`, `tests/unit/runtime/workspace`,
`tests/unit/artifacts`, `tests/integration/cli`,
`tests/integration/runtime/execution`, and
`tests/integration/observability/runtime`. The workspace mock end-to-end test
covers input-node file assembly, snapshot execution, mutable lineage, reviewer and
remediation candidate file-token resolution, duplicate skip, resume, cleanup
dry-run, and `--force`.
