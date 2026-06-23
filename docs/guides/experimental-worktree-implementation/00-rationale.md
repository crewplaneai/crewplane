# Experimental Worktree Implementation: Rationale

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Rationale
Node-scoped workspaces match the orchestration model. The scheduler, dependency
graph, review loops, findings extraction, resume frontier, duplicate skip,
manifests, and observability are node-centered.

A workflow-scoped mutable workspace would isolate a run from the user checkout,
but it would not isolate parallel nodes from each other. It would also make
downstream lineage ambiguous. If two independent nodes mutate one workflow
workspace, a later node cannot know which code state it inherited without hidden
ordering or merge rules.

Node-scoped workspaces keep the rules explicit:

- Parallel nodes fan out from the same recorded source commit.
- Each logical `kind: worktree` source line advances through explicit node
  selectors and DAG ordering.
- A downstream mutable node inherits source by selecting the same logical
  worktree name as the latest ordered same-worktree ancestor in the DAG.
- Other upstream information flows through artifacts.
- Runtime records source and result identities at node boundaries.

`kind: worktree` is selected for mutable lineage in v1 because Git worktrees
are the most practical local mutable isolation primitive for CLI coding agents.
They avoid cloning the object database per node, work with ordinary Git-aware
provider CLIs, support detached worktrees, and can produce Git-native bundle
artifacts for downstream rehydration.

`kind: snapshot` is included for writable disposable project-source workspaces
because many workflows need isolated source inspection, report generation, test
discovery, or scratch writes without producing code lineage. V1 snapshots use
Git-native tree materialization, contain no `.git` metadata, and discard source
changes after invocation.

V1 rejects final `HEAD` movement instead of preserving provider-created
commits. Preserving provider commits would allow provider-controlled metadata,
hooks, global/system config, worktree config, filters, dates, and history shape
into the lineage contract. Detecting every transient provider-created object
would require scanning the shared object database and would still create false
positives from unrelated user Git commands. V1 therefore narrows the invariant:
runtime never uses provider-created commit trees for lineage, rejects observable
final `HEAD` and protected-state drift, and captures result trees only from
final filesystem changes under the runtime Git contract.

V1 resolves workspace-file tokens per invocation source instead of per node
source. This matters for review loops: reviewers inspect the current candidate,
so file-token prompt bytes must also come from the current candidate.
Remediation rounds likewise start from the current candidate and must not
receive stale initial-source file bytes.

The v1 Git contract keeps correctness-relevant hardening required for a first
usable worktree release:

- explicit `cwd`
- adapter launch compatibility
- side-effect-free source policy
- Git capability probes
- clean-start policy
- literal path handling for file-token paths
- explicit config classification with overrides for local core settings that
  affect file mode, symlink, case, Unicode, line-ending, ignore, and attribute
  behavior
- rejection of local/worktree config keys that cannot be safely ignored or
  overridden
- rejection of byte-transforming attributes
- rejection of object-store indirection and replacement behavior
- rejection of final provider `HEAD` movement and protected-state drift
- invocation-scoped file-token resolution
- source/result identity recording
- deterministic runtime-owned lineage commits
- bundle-backed cross-node transfer
- cleanup and retention

It defers broader raw-capture, custom ignore, custom attributes, LFS,
provider-commit preservation, native Windows, and platform abstraction work to
future ADRs.
