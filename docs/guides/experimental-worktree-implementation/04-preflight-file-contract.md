# Experimental Worktree Implementation: Preflight and File Template Contracts

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Preflight Phase Model
When workspace isolation is enabled, persisted diagnostics use these preflight
phase values:

1. `parse`
2. `validation`
3. `workspace_policy`
4. `invoker_workspace_compatibility`
5. `source_policy`
6. `worktree_contract`
7. `workspace_file_locator_policy`
8. `provider`
9. `reference`
10. `node_policy`
11. `file_policy`
12. `env_policy`
13. `var_policy`
14. `template_plan`

`invoker_workspace_compatibility` validates the selected invoker adapter's
optional capability record only for enabled-mode real runs that invoke
providers. Missing capability metadata is normalized to `supported: false`.

`source_policy` discovers Git identity and enforces clean-start,
hidden-index-state rejection, unsupported repository state, native Windows
rejection, local/worktree config classification, object-store rejection,
filesystem capability checks, and side-effect-free Git capability checks.

`worktree_contract` verifies that `blob_exact` can be enforced for the
selected repository, filesystem, and runtime.

`workspace_file_locator_policy` compiles project-source file locators to Git
blob identities and canonical blob-byte digests, then validates literal path
resolution. It validates lineage-dependent and candidate-dependent locator
syntax and containment but defers existence and blob identity until runtime
source resolution.

When workspace isolation is disabled, ADR 0012's current preflight phases remain
unchanged.

Preflight remains authoritative. Runtime consumes the compiled plan, workspace
locators, source snapshot, selected `worktree_contract` mode, invoker
compatibility result when present, and render plan. Runtime must not inspect
original workflow source, parse prompt text for tokens, invent file reads, or
re-run workflow policy.

## File Template and Input Source Contract
When workspace isolation is disabled, ADR 0012 behavior remains unchanged:
repo-relative `\{\{file:path\}\}` templates are static preflight resources
resolved from the project root, and input nodes using `\{\{file:path\}\}` do not
require Git.

When enabled, repo-relative file templates are workspace-aware, and their
injected bytes come from Git objects associated with each invocation's effective
source identity.

This applies to prompt segments and input-node sources:

```yaml
nodes:
  - id: review-input
    mode: input
    source: "\{\{file:docs/review-findings.md\}\}"
```

Rules:

1. Relative `\{\{file:path\}\}` references are compiled as workspace-file
   locators.
2. The locator records:
   - raw token
   - token kind and occurrence id
   - source workflow file
   - source span and token span where available
   - authored source root
   - source root relative to project root
   - project root relative to Git top-level
   - normalized repo-relative source path
   - effective workspace-relative path
   - static source class: `project_initial` or `runtime_dynamic`
   - runtime target: input, executor prompt, reviewer prompt, remediation
     prompt, or other render target
3. For project-source initial records, preflight validates against
   `run_base_commit` without reading mutable working-tree bytes.
4. Project-source validation uses Git tree/object reads:
   - `git --literal-pathspecs ls-tree -z --full-tree --full-name <run_base_commit> -- <path>`
     resolves the path.
   - The path must be normalized to a Git-top-relative POSIX path before
     invocation.
   - Exactly one NUL-delimited entry must be returned, and the returned path
     must equal the normalized requested path byte-for-byte.
   - The path must be a regular blob with mode `100644` or `100755`.
   - Symlinks, trees, gitlinks, missing paths, ambiguous paths,
     pathspec-expanded paths, pathspec-magic interpretations, and case/Unicode
     aliases fail.
   - `git cat-file` reads the stored blob bytes by object id, not by pathspec.
   - The bytes must be UTF-8 text without NUL bytes.
   - Preflight records Git blob id, blob size, canonical blob-byte SHA-256, text
     digest, source tree path, and literal path resolution metadata.
5. Project-source initial rendered bytes are available from the canonical blob
   bytes read by preflight. Real runs write them into the preflight static
   bundle only after run allocation. Validate and dry-run keep them in memory
   and write no artifacts.
6. For runtime-dynamic locators, preflight validates syntax and containment but
   cannot require existence because an upstream mutable node or an earlier
   executor round may create, modify, or delete the file.
7. Runtime resolves every file-token occurrence against an
   `InvocationSourceIdentity`:
   - input-node source: `run_base_commit`
   - initial executor source: node source commit/tree
   - reviewer source: current candidate commit/tree
   - remediation executor source: current candidate commit/tree
   - downstream mutable source from upstream: upstream result commit/tree
8. Runtime uses the same literal-path `git ls-tree` and object-id
   `git cat-file` rules against the selected invocation source commit.
9. Runtime does not read file-token bytes from the materialized workspace
   filesystem.
10. The effective workspace root is still recorded for diagnostics, provider
    `cwd`, and path containment. Synthetic generated-file link detection must
    not fall back to the base project checkout for workspace-enabled nodes; it
    resolves provider claims against the effective invocation root and copies
    verified referenced files into the run result directory before linking. In a
    monorepo, provider `cwd` is the project subdirectory inside the workspace,
    not necessarily the Git top-level.
11. For imported workflows, the authored source root is mapped relative to the
    project root, then under the effective workspace root and source tree path
    mapping.
12. Workspace-file locators must not read Git metadata, workspace cache roots,
    or reserved runtime artifact subtrees.
13. Missing files fail the invocation before provider execution.
14. Diagnostics name node, role, round when relevant, token, source workflow,
    invocation source commit, and workspace-relative path.
15. Generated templates and examples must not depend on untracked ignored
    `.crewplane/inputs` files for workspace-enabled examples.
16. Runtime records the digest of the exact bytes injected into the prompt or
    input artifact.

Invocation source records are persisted with rendered workspace-file
descriptors:

```json
{
  "invocation_id": "implement.reviewer.claude.round-1",
  "role": "reviewer",
  "round_num": 1,
  "source_kind": "candidate",
  "source_commit": "git object id",
  "source_tree": "git object id",
  "candidate_sequence": 1
}
```

Duplicate skip and resume validate rendered bytes differently by source class:

- Project-source initial bytes validate from the current signature's
  `run_base_commit` blob identities and canonical blob-byte digests.
- Candidate/reviewer/remediation bytes are execution facts. They validate by
  cross-checking persisted invocation-source descriptors, rendered-file
  descriptors, node manifests, and `workspace-state*.json`.
- Duplicate skip does not allocate workspaces or import bundles only to
  recompute candidate-source prompt bytes. Missing or inconsistent persisted
  descriptors fail closed.

Input nodes do not invoke providers and never allocate provider workspaces. In
workspace-enabled mode, runtime reads only the compiled project-source Git blob
locators needed to assemble the input output. No snapshot directory, workspace
cache directory, `workspace-state*.json`, or source lineage artifact is created
for the input node. Duplicate skip and resume validate recorded blob
identities, rendered workspace-file digests, literal path resolution
descriptors, and output artifact hashes through the ordinary artifact contract.

Absolute paths remain blocked unless explicitly allowlisted through
`settings.integrations.artifacts.options.allowed_template_paths`.

Allowlisted absolute paths are external static resources:

- read during preflight
- hashed into workflow signature
- labeled as external static resources
- not moved under workspaces
- not part of Git lineage

The enabled-mode workflow signature includes workspace policy, selected invoker
workspace compatibility class when relevant, run base commit and source tree,
Git object format, repository id digest, selected `worktree_contract` mode, raw
workspace-file locator metadata, source-root mapping metadata, project-source
initial Git blob identities and canonical blob-byte SHA-256 values, literal
path resolution descriptors for project-source initial file locators, external
static file content hashes, dependency graph and lineage declarations, and
execution-scoped workspace config.

It excludes future result commits, future candidate commits,
reviewer/remediation rendered file bytes that are execution outputs,
lineage-dependent rendered file bytes that are execution outputs, run-specific
workspace paths, stale project-root working-tree bytes for workspace-scoped
tokens, and unchecked workspace filesystem reads for file-token injection.
