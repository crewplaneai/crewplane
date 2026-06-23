# Experimental Worktree Implementation: Validation Rules

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Validation Rules
Validation first decides whether workspace isolation is enabled:

1. `settings.workspace.enabled` defaults to `false`.
2. `settings.default_workspace` is invalid and must be removed.
3. When disabled, current workflow validation and project-root execution
   behavior remain authoritative.
4. When disabled, authored workflow `worktrees` declarations or node
   `worktree` selectors fail validation.
5. When disabled, no Git repository context is required.
6. When disabled, invoker workspace capability metadata is not required and is
   not validated.
7. When enabled, validation resolves logical `worktrees`, node `worktree`
   selectors, source-line ordering, and workspace policy before preflight source
   checks.
8. Every declared worktree has `kind: worktree` or `kind: snapshot`.
9. The logical worktree name `none` is reserved and cannot be declared.
10. `setup_profile`, `create_branch`, and `branch_name` are valid only on
    `kind: worktree` declarations.
11. `worktree_contract` must be the accepted v1 mode, `blob_exact`.
12. `clean_start` must be `strict` or `tracked_only`.
13. Enabled-mode validate, dry-run, and real run require Git repository context.
14. If the effective clean policy is `strict`, the run-level clean policy is
    `strict`; otherwise it is `tracked_only`.
15. If a workflow declares exactly one worktree, provider nodes inherit it by
    default unless they explicitly set `worktree: none`.
16. If a workflow declares multiple worktrees, every provider node must select a
    declared worktree or explicitly set `worktree: none`.
17. A node naming an undeclared worktree fails validation.
18. Input nodes never allocate provider workspaces and reject explicit
    `worktree` selectors.
19. A node selecting `worktree: none` uses project-root execution and produces
    no workspace lineage.
20. `kind: snapshot` nodes materialize from the recorded project source, allow
    disposable writes, and never produce downstream lineage.
21. `kind: worktree` nodes start from the recorded project source unless they
    have an ordered direct upstream selecting the same logical worktree name.
22. A `kind: worktree` node may inherit mutable source from at most one ordered
    direct upstream on the same logical worktree line.
23. Parallel or unordered writer nodes selecting the same `kind: worktree` name
    fail validation.
24. A `kind: worktree` node cannot implicitly merge source from a direct
    upstream selecting a different `kind: worktree` name.
25. Artifact tokens from any valid upstream dependency remain valid.
26. A `kind: worktree` node may have exactly one executor provider in v1.
27. A mutable node with multiple executor providers fails validation.
28. A mutable node may have one executor and multiple reviewers.
29. Reviewer providers receive disposable non-lineage workspace views rooted at
    the current candidate commit.
30. A `kind: snapshot` node with multiple executor providers gives each
    executor a distinct disposable snapshot workspace.
31. `continue_on_failure` does not produce lineage from failed upstream nodes.
32. Downstream same-worktree lineage is blocked unless the source node
    succeeded.
33. Imported workflow `worktrees` declarations and node `worktree` selectors are
    namespace-rewritten consistently with imported node ids.
34. Import bindings may not create lineage outside the direct composed `needs`
    set.
35. Configured workspace cache placement must pass overlap, symlink, ownership,
    and permission checks before any workspace is allocated.
36. Repo-relative workspace-file locators must resolve to regular-file Git
    blobs in the invocation source tree at runtime, unless they are future
    lineage/candidate-dependent locators whose existence is deferred until that
    invocation source is materialized.
37. Workspace-file locators do not follow Git symlink entries in enabled mode. A
    locator targeting a symlink, tree, gitlink, or missing path fails.
38. Workspace-file locator paths are always passed to Git with literal semantics
    or pathspec-free object APIs.
39. Ignored, untracked, and ordinary ignored `.orchestrator/` files are not
    ambiently imported.
40. Reserved runtime artifact roots are never source roots. Tracked files under
    `.orchestrator/execution-stages/`, `.orchestrator/execution-results/`, or
    `.orchestrator/locks/` are unsupported in enabled mode.
41. Native Windows fails validation for workspace-enabled validate, dry-run, and
    real-run paths with clear WSL remediation.
42. POSIX advisory-lock support is required for workspace-enabled real runs.
43. Workspace-enabled real runs require the selected invoker adapter to declare
    v1 workspace support through `workspace_capabilities()`.
44. Process-based workspace support is valid only when provider child processes
    are launched through the runtime-owned `CommandRunner` with controlled
    `cwd`.
45. Adapter-managed local process launch that bypasses the runtime-owned command
    runner fails workspace-enabled real runs.
46. Mock invokers must record `cwd` and workspace metadata for tests.
47. Future non-process invokers are unsupported in v1 and must fail
    workspace-enabled real execution.
48. The selected `blob_exact` contract must be enforceable before workspace
    allocation.
49. Effective byte-transforming attributes fail for every tracked regular file
    in source and result trees.
50. Nonempty Git `info/attributes` files fail workspace-enabled validation
    because they are local, unsigned attribute inputs.
51. Local or worktree Git config containing `include.*`, `includeIf.*`,
    `core.attributesFile`, `core.excludesFile`, filter driver definitions,
    `core.worktree`, `core.fsmonitor`, `core.untrackedCache`,
    `core.splitIndex`, `index.*`, `extensions.worktreeConfig`, unsupported
    `extensions.*`, or object-store behavior keys fails workspace-enabled
    validation.
52. Runtime-owned commands override and record local `core.filemode`,
    `core.symlinks`, `core.ignorecase`, `core.precomposeunicode`,
    `core.autocrlf`, `core.eol`, `core.safecrlf`, `core.protectHFS`, and
    `core.protectNTFS` where Git exposes those keys. If the override cannot be
    proven or the filesystem cannot support the required behavior, validation
    fails.
53. V1 rejects active worktree-specific Git config. Any nonempty
    `config.worktree`, active `extensions.worktreeConfig`, or provider-created
    worktree config fails before workspace mutation or lineage export.
54. Git `info/exclude` containing effective non-comment, non-blank exclude
    patterns fails workspace-enabled validation because it can make ignored-file
    classification local-machine-dependent.
55. Clean-start validation fails on staged changes, unstaged changes, deletions,
    type changes, unresolved merges, intent-to-add entries, skip-worktree
    entries in project source scope, assume-unchanged entries in project source
    scope, and rejected hidden-index states.
56. Sparse checkout remains unsupported; any skip-worktree state that is part of
    sparse checkout or that can hide tracked changes fails source policy.
57. Provider-created or modified `.gitattributes` in a mutable node fails before
    lineage export in v1.
58. Final provider `HEAD` movement in a mutable node fails before lineage export
    in v1.
59. Unreachable provider-created objects are not scanned, are not lineage, and
    are not themselves a validation failure.
60. Source and result trees must not contain path collisions under the active
    filesystem's case-folding or Unicode normalization behavior. V1 may reject
    such collisions unconditionally if platform detection cannot prove safety.
61. Object alternates, grafts, and replacement behavior are unsupported in v1.
