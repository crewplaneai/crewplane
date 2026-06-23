# Experimental Worktree Implementation: Source Policy and Git Contract

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Source Policy
This source policy runs only when `settings.workspace.enabled: true`.

Before duplicate lookup, run allocation, workspace-aware file reads, workspace
materialization, or provider invocation:

1. Parse, validate schema, compose imports, and normalize workspace policy.
2. Reject unknown settings keys, including stale `settings.default_workspace`.
3. Validate selected invoker adapter workspace compatibility when provider
   invocation will occur.
4. Fail native Windows with WSL remediation.
5. Discover Git repository context from the project root.
6. Capture:
   - `run_base_commit = HEAD^{commit}`
   - source tree id
   - Git object format
   - top-level working tree path
   - project root path relative to Git top-level
   - common Git directory from `git rev-parse --git-common-dir`
   - active checkout Git directory from `git rev-parse --absolute-git-dir`
   - effective Git attributes info path from `git rev-parse --git-path info/attributes`
   - effective Git excludes info path from `git rev-parse --git-path info/exclude`
   - active worktree config path from `git rev-parse --git-path config.worktree`
   - object alternates path from `git rev-parse --git-path objects/info/alternates`
   - grafts path from `git rev-parse --git-path info/grafts`
   - repository id digest
   - installed Git version
   - required read-only feature-probe results
   - filesystem capability probe results
   - shallow repository status
   - partial clone or promisor status
7. Verify `blob_exact` can be enforced.
8. Inspect local Git config with
   `git config --no-includes --local --list --show-origin --show-scope -z`.
9. Inspect active worktree config state with
   `git config --no-includes --worktree --list --show-origin --show-scope -z`
   when supported, and by checking `git rev-parse --git-path config.worktree`.
10. Classify config:
    - rejected keys fail
    - overridden keys are recorded and forced by runtime-owned command overlays
    - ignored-neutral keys are recorded only as redacted key names and do not
      affect workflow identity
11. Reject unsupported config keys:
    - `include.*`
    - `includeIf.*`
    - `core.attributesFile`
    - `core.excludesFile`
    - `core.worktree`
    - `core.fsmonitor`
    - `core.untrackedCache`
    - `core.splitIndex`
    - `index.*`
    - `extensions.worktreeConfig`
    - unsupported `extensions.*`
    - `filter.*.clean`
    - `filter.*.smudge`
    - `filter.*.process`
    - `filter.*.required`
12. Override and record these local core settings for runtime-owned Git commands
    and provider child environments:
    - `core.filemode=true`
    - `core.symlinks=true`
    - `core.ignorecase=false`
    - `core.precomposeunicode=false`
    - `core.autocrlf=false`
    - `core.eol=lf`
    - `core.safecrlf=false`
    - `core.attributesFile=/dev/null`
    - `core.excludesFile=/dev/null`
    - `core.protectHFS=true` where supported
    - `core.protectNTFS=true` where supported
13. Probe that executable-bit and symlink behavior can be represented by the
    project/workspace filesystem. If not, fail before workspace allocation.
14. Probe case and Unicode path behavior. Reject unsafe aliases and source
    trees with collisions under the active behavior.
15. Reject nonempty Git `info/attributes`.
16. Reject effective non-comment, non-blank patterns in Git `info/exclude`.
17. Reject nonempty active `config.worktree` and active worktree-specific
    config.
18. Reject nonempty object alternates and grafts files.
19. Validate all tracked regular files in `run_base_commit` have no effective
    byte-transforming attributes.
20. Compile project-source workspace-file locator initial blob identities and
    canonical blob-byte digests with literal-path `git ls-tree` and object-id
    `git cat-file`.
21. Validate workspace cache placement.
22. Collect tracked, untracked, ignored, and index-flag state with Git
    plumbing/status commands that do not mutate the index.
23. Enforce clean policy:
    - `strict`: no tracked changes, no hidden tracked-state flags, and no
      untracked non-ignored source files
    - `tracked_only`: no tracked changes and no hidden tracked-state flags;
      untracked non-ignored source files are allowed but excluded from all
      workspaces
24. For `tracked_only`, emit a warning and write a preflight note listing
    excluded source-file counts and representative paths. The warning must say
    excluded untracked files are not visible to providers and users should
    `git add` required new files or use explicit allowlisted external
    resources.
25. Detect unsupported repository states.
26. Estimate workspace storage pressure.
27. Emit warnings for ordinary ignored files, generated dependency caches, and
    build outputs that may increase disk use during provider execution.

Root workflow files, imported workflow files, and config files remain preflight
control-plane inputs under ADR 0012. Their bytes affect workflow identity
through normalized workflow/config signatures. This ADR does not add a separate
control-plane signing system. If a control-plane file is tracked and dirty in
the project source scope, source policy treats it like any other tracked dirty
file. If a control-plane file is untracked or ignored under `.orchestrator/`, it
may still drive preflight, but it is not materialized into node workspaces as
source unless it is a tracked source file outside reserved runtime artifact
roots.

Reserved runtime artifact roots are outside source policy:

- `.orchestrator/execution-stages/`, `.orchestrator/execution-results/`, and
  `.orchestrator/locks/` are never copied into workspaces.
- Untracked files under those roots are excluded from clean-start untracked
  classification and are not signed as source.
- Tracked files under those roots fail source policy because generated runtime
  output must not become workspace source.
- File tokens and generated-file references cannot read those roots.

Ignored files and untracked files are never ambiently imported into workspaces.
Cross-node code transfer remains explicit through lineage bundles.

Detached `HEAD` checkouts are supported when `HEAD^{commit}` resolves and the
clean-start policy passes.

The source gate is run-level and all-or-nothing in v1. If source policy fails,
runtime does not execute a subset of nodes.

## Blob Exact Workspace Contract
`blob_exact` is the bounded behavior contract for workspace-enabled execution.

Runtime-owned Git commands use:

- explicit `git -C <path>` or `--git-dir`/`--work-tree`
- a sanitized environment
- `GIT_CONFIG_NOSYSTEM=1`
- `GIT_CONFIG_GLOBAL=/dev/null`
- `GIT_ATTR_NOSYSTEM=1`
- no inherited `GIT_ATTR_SOURCE`
- no inherited Git pathspec environment variables
- no inherited object-store redirection variables
- `GIT_NO_REPLACE_OBJECTS=1` for identity-sensitive operations
- `GIT_NO_LAZY_FETCH=1`
- `GIT_TERMINAL_PROMPT=0`
- deterministic config overlays:
  - `core.filemode=true`
  - `core.symlinks=true`
  - `core.ignorecase=false`
  - `core.precomposeunicode=false`
  - `core.attributesFile=/dev/null`
  - `core.excludesFile=/dev/null`
  - `core.autocrlf=false`
  - `core.eol=lf`
  - `core.safecrlf=false`
  - `core.protectHFS=true` where supported
  - `core.protectNTFS=true` where supported
  - `commit.gpgsign=false`
  - `tag.gpgsign=false`

The path handling contract requires:

- Unset inherited `GIT_LITERAL_PATHSPECS`, `GIT_GLOB_PATHSPECS`,
  `GIT_NOGLOB_PATHSPECS`, and `GIT_ICASE_PATHSPECS` for runtime-owned Git
  commands.
- Use Git's global `--literal-pathspecs` option for runtime-owned Git commands
  that pass repository paths as pathspec operands.
- Prefer pathspec-free APIs where available:
  - object IDs for `git cat-file`
  - NUL-delimited stdin for `git check-attr`
  - `--index-info` or `--cacheinfo` for `git update-index` when needed
  - NUL-delimited output parsing from Git diff/listing commands
- Pass `--` before path operands.
- Treat leading dashes as ordinary path bytes by using `--`, stdin APIs, or
  object-id APIs.
- Normalize workflow-authored paths to Git-top-relative POSIX paths before Git
  operations.
- Normalize materialized filesystem paths separately for containment under the
  effective workspace root.
- Reject paths containing NUL bytes before any Git invocation.
- Require exact returned-path equality from Git machine-readable output before
  trusting blob IDs, attributes, diff results, or file locator resolutions.
- Reject or fail closed on case-folding or Unicode-normalization aliases that
  can make a Git path map to more than one filesystem path.

The attribute contract rejects effective byte-transforming attributes for every
tracked regular file in source and result trees:

- `filter=<driver>`, including `filter=lfs`
- `ident`
- `working-tree-encoding=<encoding>`
- `text` forms that enable text normalization or checkout conversion
- `eol=<lf|crlf>`
- legacy `crlf` forms that enable conversion
- archive-specific substitutions or exclusions when snapshot materialization
  would otherwise observe archive semantics

The config contract has three classes.

Rejected local/worktree keys fail source policy or result export:

- `include.*`
- `includeIf.*`
- `core.attributesFile`
- `core.excludesFile`
- `core.worktree`
- `core.fsmonitor`
- `core.untrackedCache`
- `core.splitIndex`
- `index.*`
- `extensions.worktreeConfig`
- unsupported `extensions.*`
- local/worktree `filter.*`
- `remote.*.promisor`
- `remote.*.partialclonefilter`
- keys that redirect object lookup, refs, repository discovery, worktree
  identity, attribute lookup, ignore lookup, index format, or path
  interpretation in ways not covered by runtime overlays

Overridden local core keys are recorded and forced by runtime-owned command
overlays and provider child environments:

- `core.filemode`
- `core.symlinks`
- `core.ignorecase`
- `core.precomposeunicode`
- `core.autocrlf`
- `core.eol`
- `core.safecrlf`
- `core.protectHFS`
- `core.protectNTFS`

Ignored-neutral keys may be present but cannot affect runtime-owned
source/materialization/capture behavior. Runtime records only redacted key names
for diagnostics when useful. Examples include ordinary remote/branch metadata
and user identity settings, because runtime does not fetch, push, or use user
identity for lineage commits.

The contract allows:

- executable bit changes when filesystem probes pass and runtime overlays are
  effective
- symlink entries in workspaces when filesystem probes pass, although
  file-token injection does not follow symlinks
- repository `.gitignore` files as tracked source files
- ordinary ignored untracked files as excluded execution byproducts
- non-byte-transforming attributes such as diff or merge drivers
- unreachable provider-created objects that are not protected refs, not final
  `HEAD`, and not lineage inputs

The contract rejects:

- effective LFS or custom filter attributes anywhere in the materialized source
  tree
- local config includes
- local attribute-file overrides
- local exclude-file overrides
- local filter driver definitions
- worktree-specific config
- split-index, fsmonitor, untracked-cache, and unsupported index extensions
- nonempty Git `info/attributes`
- effective patterns in Git `info/exclude`
- object alternates and grafts
- replacement behavior for identity-sensitive operations
- path collisions unsafe for the active filesystem
- provider-created or modified `.gitattributes` before mutable lineage export
- final provider `HEAD` movement before mutable lineage export

This intentionally rejects some normal repositories, including repositories with
global or local `* text=auto`, LFS-tracked assets, worktree-specific config,
local filter drivers, split index, object alternates, and path aliasing that is
unsafe on the active filesystem. V1 chooses deterministic workspace and lineage
bytes over breadth. A future ADR may introduce a richer materialization/capture
contract that supports those repositories.

## Unsupported Repository States
Workspace-enabled v1 fails preflight or pre-invocation checks for:

- no Git repository
- unborn `HEAD`
- missing `HEAD` commit object
- missing, corrupt, or unreadable project index
- intent-to-add index entries
- skip-worktree or assume-unchanged entries in project source scope
- split-index, fsmonitor, untracked-cache, or unsupported index extension state
- sparse checkout enabled
- Git version older than the minimum supported version, or missing required
  feature probes
- selected invoker missing v1 workspace support for enabled real execution
- selected invoker unable to honor explicit `cwd`
- selected process invoker unable to use runtime-owned controlled child-process
  launch
- shallow repository
- partial clone or promisor repository
- object alternates
- grafts
- replacement refs or replacement behavior affecting identity-sensitive
  operations
- `.gitmodules` present
- gitlink entries in the tree
- dirty submodule state
- worktree source path outside discovered Git top-level
- project root path that cannot be mapped under the discovered Git top-level
- tracked runtime output under reserved artifact roots
- unsafe cache-root overlap or symlink placement
- common Git directory that cannot host an owner-private orchestrator lock
  directory
- native Windows platform
- unsupported POSIX locking
- filesystem that cannot preserve required symlink or executable-bit semantics
- path collisions unsafe under active case or Unicode behavior
- nonempty Git `info/attributes`
- effective non-comment, non-blank patterns in Git `info/exclude`
- unsupported local/worktree Git config includes, attribute injection keys,
  exclude injection keys, worktree-config keys, index keys, object behavior
  keys, or filter definitions
- active worktree-specific Git config
- Git command surface unable to enforce literal path semantics for
  runtime-owned path operands
- byte-transforming attributes on any tracked regular file in source or result
  trees
- provider-created or modified `.gitattributes` in a mutable node
- final provider `HEAD` movement in a mutable node

V1 does not initialize, update, or recurse into submodules.

## Git Capability Requirements
Workspace-enabled v1 supports Git 2.34.1 or newer when the required v1 command
surface passes capability probes. No Git capability probe runs when workspace
isolation is disabled.

Git 2.34.1 is the v1 baseline because it is the packaged Git version on Ubuntu
22.04 LTS and WSL environments while still providing the worktree, bundle,
object-format, status v2, temporary-index, checkout-index, commit-tree, and
literal pathspec command surfaces needed by this ADR. Capability probes remain
authoritative; version parsing alone is insufficient.

V1 must not require newer convenience flags without a Git 2.34.1-compatible
fallback. For example, Git 2.34.1 does not provide
`git check-attr --source=<tree-ish>`, so tree-based attribute checks use an
owner-private temporary index loaded from the source tree and
`git check-attr --cached`. The temporary index is outside the repository index
and is removed after the probe or runtime operation.

Runtime must not rely on version parsing alone. Before workspace provisioning,
the source-policy and runtime gates probe the exact command surface v1 uses:

- `git rev-parse --git-common-dir`
- `git rev-parse --absolute-git-dir`
- `git rev-parse --git-path`
- `git rev-parse --is-shallow-repository`
- `git rev-parse --show-object-format=storage`
- `git config --no-includes --local --list --show-origin --show-scope -z`
- `git config --no-includes --worktree --list --show-origin --show-scope -z`,
  or an equivalent probe that proves no active worktree-specific config can
  affect v1
- `git status --porcelain=v2 -z`
- `git ls-files` or equivalent index-flag inspection for intent-to-add,
  skip-worktree, and assume-unchanged
- `git --literal-pathspecs ls-tree -z --full-tree --full-name <tree-ish> -- <literal-path>`
- `git cat-file --batch` or an equivalent `git cat-file blob <object>` path
- `git check-attr --cached -z --stdin` against an owner-private temporary index
  loaded from the tree being inspected, or a native tree-ish attribute probe
  when available
- `git diff-tree -z`
- `git diff-index -z`
- `git read-tree`
- `git checkout-index`
- `git reset`
- `git add -A`
- `git write-tree`
- `git commit-tree`
- `git worktree add --detach --lock --reason`, or `git worktree add --detach`
  followed immediately by `git worktree lock --reason`
- `git worktree remove`
- `git worktree list --porcelain`
- `git bundle create`
- `git bundle verify`
- `git bundle list-heads`
- `git check-ref-format --normalize`
- `git reset --hard`
- `git clean`
- `git update-ref`

V1 fails before provider invocation when the available Git binary cannot support
safe tree/object reads, literal pathspec enforcement, local/worktree config
inspection, attribute scanning, snapshot checkout, worktree registration,
locking, removal, bundle export/import, ref validation, deterministic
runtime-owned commit creation, retry reset, provider-final-worktree capture, or
hidden-index-state inspection.

Worktree lock support:

- Prefer `git worktree add --detach --lock --reason`.
- If the installed Git supports `git worktree lock --reason` but not
  `add --lock --reason`, runtime may create the detached worktree and
  immediately lock it under the same repository lock before provider invocation.
- The selected lock variant is recorded in preflight/run metadata.
- If neither safe lock path is available, workspace-enabled execution fails
  before provider invocation.

Git LFS and filter policy:

- Runtime does not run `git lfs fetch`.
- Effective `filter` attributes fail source policy for every tracked regular
  file.
- A plain text file that happens to contain LFS-pointer-shaped text but has no
  effective LFS/filter attribute is treated as ordinary file content.
- A future ADR may add explicit LFS materialization and integrity semantics.
