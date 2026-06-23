# Experimental Worktree Implementation: Result, Bundle, Fan-In, and State Contracts

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Worktree Result Contract
A successful `kind: worktree` node produces deterministic result identity.

Runtime must:

1. Verify source identity before finalization.
2. Verify `blob_exact` before result capture.
3. Verify protected orchestrator refs and expected Git identities before
   lineage export.
4. Fail if submodule gitlinks or dirty submodule state are detected.
5. Fail if `.gitattributes` changed in the provider final source view.
6. Fail if final provider `HEAD` movement is detected.
7. Fail if worktree-specific config is active or was created.
8. Capture tracked modifications, deletions, type changes, executable-bit
   changes, symlink changes, and untracked non-ignored files inside the node
   worktree.
9. Exclude ignored untracked files by default.
10. Treat every runtime-owned path operand under literal path rules.
11. Write a result tree from the staged index.
12. Validate the result tree satisfies `blob_exact`.
13. Create an orchestrator-owned commit with deterministic metadata.
14. Allow an empty result commit when the final captured tree equals the runtime
    parent tree.
15. Record result commit and tree in `workspace-state.json`.
16. Export `workspace.bundle`.
17. Optionally create a cached final ref.
18. Write changed-file counts, final `HEAD` diagnostics, protected-state
    diagnostics, contract-validation diagnostics, and bundle byte size to
    observability.

Runtime-owned commit creation uses `git commit-tree`, not `git commit`, so user
commit hooks do not alter commit behavior.

Deterministic commit metadata:

- Author name: `Orchestrator CLI`
- Author email: `orchestrator-cli@localhost`
- Committer name: `Orchestrator CLI`
- Committer email: `orchestrator-cli@localhost`
- Author date and committer date: deterministic value derived from source
  commit, node id, workflow signature, `blob_exact`, and candidate
  sequence
- Commit message: deterministic message containing workflow name, workflow
  signature, node id, source commit, source tree, `blob_exact`, and
  candidate sequence

Commit metadata must not include run id, cache root, local paths, wall-clock
time, provider credentials, or secrets.

Actual run timing belongs in `workspace-state.json`, not in commit objects.

## Bundle Semantics
`workspace.bundle` is a Git bundle produced by `git bundle create`. It is a
binary Git object transport artifact, not a patch, tarball, or working-tree
archive.

The bundle must be sufficient to materialize the node result in the same base
repository after cached refs are deleted. It does not need to be a standalone
clone of full repository history. The required base object is the recorded
source commit.

Bundle creation rules:

- Create or use a ref pointing to the result commit because bundle tips are
  refs.
- Export the range from source commit to result ref.
- Temporary export refs are deleted after bundle verification.
- Bundle SHA-256 is recorded in `workspace-state.json`.
- Bundle byte size is recorded in observability.
- Runtime verifies the bundle before marking lineage complete.
- Temporary refs are deleted under repository lock.

Downstream rehydration rules:

1. Verify recorded bundle SHA-256 before any Git operation on the bundle.
2. Run `git bundle verify`.
3. Ensure the required base commit exists.
4. Import the bundle into an orchestrator-owned internal ref.
5. Verify imported result commit and tree match `workspace-state.json`.
6. Verify the downstream plan expects `blob_exact`.
7. Create downstream mutable workspace from the verified result commit.

For chained lineage, the workspace manager imports bundles in dependency order.
Node-sourced state preserves upstream source descriptors so a depth-2 chain
imports the first node bundle before the second node bundle when materializing a
later workspace or fulfilling a branch export.

The bundle contract records Git object format, selected `worktree_contract`,
source commit, source tree, result commit, result tree, required base commit,
bundle path, bundle SHA-256, bundle byte size, empty lineage commit flag,
cached ref presence and name, and import ref name when rehydrated.

## Fan-In Semantics
Code lineage is single-source in v1. Nodes inherit mutable source by selecting
the same logical `kind: worktree` name and by being ordered in the DAG.

```yaml
worktrees:
  implementation_worktree:
    kind: worktree
  review_snapshot:
    kind: snapshot

nodes:
  - id: implement
    providers: [codex]
    worktree: implementation_worktree

  - id: review
    needs: [implement]
    providers: [claude]
    worktree: review_snapshot

  - id: fix
    needs: [implement, review]
    providers: [codex]
    worktree: implementation_worktree
```

`fix` inherits code from the ordered same-worktree `implement` source line. It
may consume `review` through `\{\{node.output\}\}` or `\{\{node.findings\}\}`,
but `review` source changes are not merged.

A downstream `kind: snapshot` node after a `kind: worktree` node materializes
from the run base project source. It can use artifacts from upstream nodes, but
it does not inherit upstream file changes as source lineage. To continue
mutable code state, the downstream node must select the same logical
`kind: worktree` name as the upstream writer. If it makes no changes, runtime
still emits an explicit empty lineage commit.

## `workspace-state.json`
Every managed provider workspace emits an envelope under the node stage
directory. Input nodes and `worktree: none` nodes do not allocate provider
workspaces and do not emit workspace lineage state.

The envelope `version` uses the canonical authored schema version from
`src/orchestrator_cli/version.py` rather than a separate workspace-state schema.

The envelope records:

- `version: <SCHEMA_VERSION>`
- run id, run key, workflow name, workflow signature, node id, and status
- logical worktree name, kind, clean-start mode, and selected
  `worktree_contract`
- selected invoker workspace capability metadata when provider invocation
  occurs
- Git identity: object format, repository id, active Git dir, common dir,
  `run_base_commit`, Git top-level, project root relative to Git top-level,
  worktree lock mode, worktree-config status, local config policy summary, and
  filesystem capability summary
- source identity, including source kind, source node when relevant, commit,
  tree, source bundle digest when inherited from a node, and nested upstream
  source descriptors when chained lineage requires ordered bundle imports
- result identity for mutable nodes: commit, tree, parent commit, bundle path,
  bundle SHA-256, bundle size, cached ref, empty-lineage flag, and required base
  commit
- workspace placement: live path, effective `cwd`, materialization mode,
  disposable flag, retention state, and retained reason
- rendered workspace-file descriptors, including occurrence id, invocation id,
  role, round, source kind, source commit/tree, candidate sequence,
  workspace-relative path, Git blob, file mode, byte size, canonical blob
  digest, injected digest, byte source, literal-path verification, UTF-8
  validation, and target
- child-process environment summary when process invocation occurs
- changed-file counts, ignored-untracked exclusion, final provider `HEAD`
  diagnostics, protected-ref diagnostics, unreachable-provider-object scan
  status, `.gitattributes` drift, and worktree-config drift
- attempt baselines and reset status
- diagnostics and timestamps

Abbreviated disposable snapshot shape:

```json
{
  "version": "1.0",
  "run_id": "20260612-143012",
  "run_key_name": "feature-work--a1b2c3d4e5f6-20260612-143012",
  "workflow_name": "feature-work",
  "workflow_signature": "64 lowercase hex characters",
  "node_id": "review",
  "status": "succeeded",
  "logical_worktree_name": "review_snapshot",
  "kind": "snapshot",
  "clean_start": "strict",
  "worktree_contract": "blob_exact",
  "git": {
    "object_format": "sha1",
    "repo_id": "64 lowercase hex characters",
    "run_base_commit": "git object id",
    "worktree_config_active": false
  },
  "source": {
    "kind": "project",
    "commit": "git object id",
    "tree": "git object id"
  },
  "workspace": {
    "path": "/cache/snapshots/repo/run/review",
    "effective_cwd": "/cache/snapshots/repo/run/review/project",
    "materialization": "snapshot_directory",
    "disposable": true,
    "retention": "removed",
    "retained_reason": null
  },
  "rendered_workspace_files": [
    {
      "occurrence_id": "review:prompt:file:docs/requirements.md",
      "invocation_id": "review.reviewer.claude.round-1",
      "role": "reviewer",
      "round_num": 1,
      "source_kind": "project",
      "source_commit": "git object id",
      "source_tree": "git object id",
      "candidate_sequence": null,
      "workspace_relative_path": "docs/requirements.md",
      "git_blob": "git object id",
      "git_file_mode": "100644",
      "byte_size": 4096,
      "canonical_blob_sha256": "64 lowercase hex characters",
      "injected_sha256": "64 lowercase hex characters",
      "byte_source": "git_blob",
      "literal_path_verified": true,
      "utf8_validated": true,
      "target": "reviewer_prompt"
    }
  ],
  "diagnostics": []
}
```

Field invariants:

- Nodes that do not invoke providers, such as input nodes, do not have
  workspace-state, `invoker`, or `child_process_environment` blocks.
- Executable snapshot nodes with provider invocations include `invoker` and, for
  process-based invokers, `child_process_environment`.
- Mutable `kind: worktree` provider nodes include `invoker`, result capture
  diagnostics, provider final `HEAD` diagnostics, invocation-source descriptors,
  and, for process-based invokers, `child_process_environment`.
- Mock-invoker provider nodes include `invoker` but may omit
  `child_process_environment` because no child process is launched.
- Failed or cancelled nodes include these fields only when the corresponding
  source, materialization, invocation, launch, or result-capture step was
  reached.
- If final provider `HEAD` movement is detected, the terminal state is failed
  and no result lineage is exported.
- If unreachable provider-created objects are not scanned,
  `unreachable_provider_objects_scanned` is `false`. This is an explicit v1
  limitation, not missing telemetry.
- For `kind: snapshot`, emit the same envelope with
  `workspace.materialization: "snapshot_directory"`, snapshot digest, drift
  summary, no result bundle, and no cached final ref.
- For failed or cancelled nodes, `failure` records operation, message,
  remediation, and retention status. Result fields are omitted unless a verified
  final result exists.
- `source.bundle_sha256` is present only when `source.kind: "node"`.
- `changes.provider_head_commit` is diagnostic only. It is never used as
  downstream lineage identity.

Do not store prompt text, environment secrets, provider credentials, raw config
secrets, provider auth paths, raw rendered workspace-file content, raw ignore
rule contents, raw worktree config contents, raw local config values, or
unredacted sensitive values in workspace state.
