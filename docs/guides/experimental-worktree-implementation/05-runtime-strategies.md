# Experimental Worktree Implementation: Runtime, Snapshot, and Worktree Strategies

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Runtime Semantics
When disabled, runtime uses the existing execution path: provider invocations
receive project root `cwd`, prompts are assembled from current preflight render
fragments, artifacts are written under `.crewplane/`, and no workspace state
is created.

For each enabled-mode node:

1. Resolve effective workspace config from the compiled plan.
2. Verify selected invoker workspace compatibility for the node's invocation
   shape when provider invocation will occur.
3. Resolve node source:
   - `worktree: none` or no managed worktree uses project-root execution
   - `kind: snapshot` uses `run_base_commit`
   - `kind: worktree` with no ordered same-name ancestor uses `run_base_commit`
   - `kind: worktree` with an ordered same-name ancestor uses the latest such
     ancestor's verified result commit
4. Verify the selected `worktree_contract` mode is still enforceable.
5. Provision workspace:
   - `kind: snapshot`: disposable writable source workspace from
     `run_base_commit`
   - input node: no provider workspace allocation
   - `kind: worktree`: detached mutable Git worktree from source commit
6. Compute `effective_workspace_root` as workspace checkout root plus the
   recorded project-root-relative path.
7. For managed provider workspaces, write initial `workspace-state*.json` with
   `status: running`.
8. For each provider invocation, compute an `InvocationSourceIdentity`.
9. Resolve workspace-file locators:
   - if invocation source is the project initial source and the preflight blob
     record is valid, consume the preflight-compiled blob content record
   - otherwise read Git blob bytes from the invocation source commit/tree with
     literal path resolution
10. Record invocation-source descriptors, rendered workspace-file byte digests,
    and literal path resolution descriptors in workspace state and node
    manifest.
11. Assemble prompts and input content from compiled render fragments.
12. Invoke provider with explicit `cwd` when the node has provider invocations.
13. For process-based invocations, the runtime-owned command runner applies
    workspace child-environment controls derived from
    `InvocationContext.workspace`.
14. For retryable transport failures, reset workspace to the attempt baseline
    before retry.
15. For successful mutable executor candidates, reject final `HEAD` movement and
    protected-state drift, reset runtime-owned index state, capture final
    worktree filesystem changes under `blob_exact`, validate the
    candidate tree, and create a Crewplane-owned candidate commit.
16. For reviewer invocations, provision disposable reviewer workspaces from the
    current candidate commit and resolve reviewer file tokens from that same
    candidate commit.
17. For remediation executor invocations, start from the current candidate
    workspace and resolve remediation file tokens from the current candidate
    commit.
18. For final successful mutable nodes, export lineage artifacts only after
    source/result identities and protected refs validate.
19. Record and discard snapshot source drift or verify worktree result identity.
20. Finalize ordinary node output and findings artifacts.
21. Write terminal `workspace-state*.json` when workspace state exists.

Provider outputs, logs, node manifests, run manifests, summaries, and results
remain under project `.crewplane/`. Provider CLIs receive absolute artifact
paths where the invocation plan requires output paths. The provider process
`cwd` is the effective node workspace root when enabled and the project root
when disabled.

Generated-file reference detection uses the effective invocation root: workspace
project root when enabled, project root when disabled. Workspace-enabled result
links point at verified copies under the run result directory, not live cache
paths that cleanup may remove.

## Environment and Dependency Materialization
Workspace provisioning materializes repository source state only. It does not
copy ignored dependency directories, virtual environments, tool caches, build
outputs, generated files, or other untracked local state into node workspaces.

Setup profiles are optional project-config command lists selected by
`kind: worktree` declarations. Runtime audits setup command output under the
node stage directory and runs setup only for selected `kind: worktree` nodes.

Users who need additional dependencies inside isolated workspaces can also:

- have the provider run setup commands in the node workspace
- prepare dependencies outside Crewplane in locations the provider may
  access
- commit tracked dependency inputs such as lockfiles
- use a prebuilt container, VM, or provider-native environment outside this ADR

Setup side effects are not canonical lineage unless they become source changes
in a successful `kind: worktree` node and are exported in that node's bundle.

Ignored caches created during provider execution, such as `.venv/`,
`node_modules/`, `.pytest_cache/`, or build directories, are execution
conveniences only. Ignored untracked files are excluded from lineage. If a
provider intentionally writes an ignored file and then forces it into the index
without moving `HEAD`, runtime's capture path must still validate the final
result under `blob_exact`.

Provider CLIs may still access user-level caches according to their own
permission model. Runtime may set writable temp/cache environment variables at
the process-launch boundary where practical, but those directories are not
canonical artifacts.

Provider-side Git commands are not sandboxed by Crewplane. Runtime
removes caller-shell Git redirection and pathspec environment variables from
process-based provider environments and applies deterministic Git
config-injection overrides so provider `git` commands discover the effective
workspace by `cwd` and are not accidentally influenced by the caller shell.
Provider permission, network, shell-command, pathspec usage, and intentional Git
command controls remain provider-owned.

## Snapshot Strategy
`kind: snapshot` provisions a writable disposable source workspace from
`run_base_commit` for provider nodes. Input nodes do not allocate provider
workspaces.

V1 snapshot directory materialization uses a Git-native tree materialization
path:

1. Create an owner-private snapshot directory outside the project and
   `.crewplane/`.
2. Create an owner-private temporary index under the system temporary directory.
3. Use sanitized runtime Git environment with `GIT_INDEX_FILE` set only for this
   operation.
4. Apply the deterministic Git config overlay.
5. Neutralize inherited pathspec environment.
6. Run `git read-tree <run_base_commit>`.
7. Run `git checkout-index --all --prefix=<snapshot-root>/`.
8. Remove the temporary index.
9. Compute the snapshot digest before invocation.

Rules:

- Snapshot source is always the recorded project source in v1.
- Snapshot never produces lineage.
- Snapshot directory materialization uses selected Git commit content, not the
  user's dirty working tree.
- Snapshot directories contain no `.git` metadata.
- Snapshot cache directories are outside the project and `.crewplane/`.
- Ignored, untracked, dirty working-tree, and uncommitted source bytes are not
  included.
- Runtime computes a stable snapshot digest before invocation.
- The digest includes relative path, type, executable bit, symlink target, file
  content digest, and selected `worktree_contract`, excluding planned
  crewplane output/log paths.
- Runtime provides writable temp directories through process-launch environment
  where practical.
- After invocation, runtime summarizes source-looking drift and discards the
  snapshot directory according to cleanup policy.
- Snapshot mutation is not lineage and does not fail solely because a provider
  wrote scratch files.
- Snapshot isolation is source isolation, not a security boundary.

For snapshot nodes with multiple executor providers, each executor invocation
receives a distinct disposable snapshot workspace rooted at `run_base_commit`.

Use `kind: worktree` when a provider CLI should edit code, produce upstream
code lineage, use Git-aware checkout metadata, or persist source changes.

## Git Worktree Strategy
`kind: worktree` provisions a detached mutable worktree rooted at the resolved
source commit for the logical source line.

Provisioning sequence:

1. Acquire the repository lock.
2. Create the owner-private worktree parent path.
3. Run `git worktree add --detach --lock --reason <reason> <path> <source-commit>`
   when available.
4. If needed, run `git worktree add --detach <path> <source-commit>` followed
   immediately by `git worktree lock --reason <reason> <path>` under the same
   repository lock.
5. Verify the new worktree's `HEAD`, source commit, source tree, selected
   `worktree_contract`, expected registration, and absence of worktree-specific
   config.
6. Release the repository lock before provider invocation.

Rules:

- Worktrees are outside the project root and project `.crewplane/`.
- Worktrees are owner-private where supported.
- Worktrees are detached from user branches.
- Worktree administration uses the repository common Git directory.
- Worktree creation, removal, lock, unlock, prune, bundle import/export,
  cached-ref update, and cleanup are repository-lock protected.
- Parallel provider execution never shares a mutable worktree across nodes.
  Ordered nodes on the same logical worktree may reuse a physical checkout only
  after runtime verifies and resets it to the prior node boundary.
- Runtime does not merge, rebase, push, or update user branches.
- Final provider `HEAD` movement is rejected in v1.
- Runtime captures final source state only from worktree filesystem changes
  under runtime-owned Git staging and validation.
- Provider-authored commit objects are never lineage parents.
- Unreachable provider-created objects are not scanned.
- Provider-created or modified `.gitattributes` fails mutable lineage export in
  v1.
- Provider-created worktree-specific config fails mutable lineage export in v1.
- Runtime-owned path operands in provisioning, verification, reset, capture, and
  cleanup are handled under literal path rules.

If a provider leaves `HEAD` on another commit, checks out another detached
commit, leaves the worktree attached to a branch, or creates worktree-specific
config, runtime fails the node before lineage export. The retained workspace may
remain only when terminal cleanup fails; it is not a source of truth for
downstream lineage.

If a provider creates commits and then restores `HEAD` to the runtime baseline,
runtime does not use those commit objects and does not guarantee detection.
Final lineage is still determined only by runtime-captured filesystem changes.

## Result Capture and Lineage Commit
A successful mutable executor attempt produces a candidate tree. V1
intentionally uses Git's normal index/staging machinery under the strict
`blob_exact` no-transform contract instead of a custom raw-capture engine.

Runtime records the attempt baseline:

- `runtime_parent_commit`: source commit or previous runtime candidate commit
- `runtime_parent_tree`: corresponding tree
- `runtime_expected_head`: worktree `HEAD` at attempt start
- protected crewplane refs
- absence of active worktree-specific config
- selected `worktree_contract`

At capture time runtime observes:

- final `HEAD^{commit}` if it resolves
- whether `HEAD` moved from `runtime_expected_head`
- whether the worktree has staged or unstaged changes
- whether `.gitattributes` changed in the final source view
- whether worktree-specific config was created or changed
- whether protected Git state changed

Capture rules:

1. Verify source identity, protected refs, selected `worktree_contract`, and
   worktree registration.
2. Reject if final `HEAD` differs from `runtime_expected_head`.
3. Reject branch checkouts, detached-commit changes, and any final `HEAD`
   movement.
4. Reject nonempty worktree `info/attributes`.
5. Reject effective non-comment, non-blank patterns in worktree `info/exclude`.
6. Reject newly unsupported local/worktree config keys.
7. Reject nonempty `config.worktree` or active worktree-specific config.
8. Reject provider-created or modified `.gitattributes`.
9. Reset runtime-owned index state to the runtime parent tree without changing
   worktree files.
10. Stage tracked modifications, deletions, type changes, executable-bit
    changes, symlink changes, and untracked non-ignored files with
    runtime-owned `git add -A` under sanitized Git environment and deterministic
    config overlay.
11. Exclude ignored untracked files by default.
12. Write a candidate tree with `git write-tree`.
13. Validate the candidate tree still satisfies `blob_exact`, including no
    byte-transforming attributes on tracked regular files and no unsafe path
    collisions.
14. Create a Crewplane-owned candidate commit with `git commit-tree`,
    parented to `runtime_parent_commit`.
15. Reset or preserve the worktree according to review-loop or finalization
    flow.

Byte source rules:

- Bytes for captured changes come from the final materialized worktree
  filesystem through runtime-owned staging.
- Because `blob_exact` rejects byte-transforming attributes and config,
  staging must not apply clean filters, LFS filters, line-ending conversion,
  working-tree encoding conversion, or `ident` substitution.
- Provider-created commit trees are never read as source input for lineage.
- Unreachable provider-created objects are not scanned and do not affect lineage
  identity.
- If runtime cannot prove the contract, candidate creation fails before lineage
  export.

An empty result commit is valid when the final captured tree equals the runtime
parent tree after ignored untracked files are excluded.

Runtime must not create reviewer workspaces, final result commits, bundles,
cached refs, or downstream lineage descriptors before candidate-tree validation
succeeds.

If capture fails after provider output artifacts were written, those artifacts
may remain as failed diagnostics, but no source lineage is produced.
