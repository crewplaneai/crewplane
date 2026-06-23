# Experimental Workspace Isolation

Workspace isolation is an Experimental, opt-in Git-backed source-tree isolation
feature. It is not sandboxing and is not a security boundary.

When enabled and selected by a workflow, Crewplane materializes provider work in
separate Git-backed worktrees or writable snapshots so providers do not edit the
project root directly. Provider CLIs and setup commands still run with their
configured process permissions, approval settings, network access, credentials,
and filesystem access.

Use this feature only for ordinary supported Git repositories. Non-Git projects
should keep it disabled.

## Enable Workspace Support

Workspace support is disabled by default:

```yaml
settings:
  workspace:
    enabled: false
```

To use managed workspaces, enable the feature. Set `cache_root` when you want to
control the location; if it is omitted, Crewplane uses the platform cache
default (`~/Library/Caches/crewplane` on macOS or
`${XDG_CACHE_HOME:-~/.cache}/crewplane` elsewhere). When provided,
`cache_root` must be absolute.

```yaml
settings:
  workspace:
    enabled: true
    cache_root: /absolute/path/to/crewplane-workspaces
    cleanup_on_success: true
    worktree_contract: "blob_exact"
    clean_start: "strict"
    setup_profiles: {}
    setup_timeout_seconds: 600
    identity:
      include_cache_root: false
    max_concurrent_materializations: 1
    disk:
      warn_free_bytes: null
      fail_free_bytes: null
```

`settings.workspace.enabled` is a gate, not a request to allocate workspaces. If
a workflow declares no `worktrees`, providers still run in the project root and
no Git source policy, worktree, snapshot, workspace state, or bundle is created.

`settings.default_workspace` is not a supported config key.

## Support Matrix

The initial implementation uses the `blob_exact` contract. Provider-visible file
bytes must match Git blob bytes exactly, so repositories with features that can
rewrite bytes fail before provider invocation.

| Repository feature | Experimental workspace support | Remediation |
| --- | --- | --- |
| Clean ordinary Git repository | Yes | Use `settings.workspace.enabled: true` with workflow `worktrees` |
| Non-Git project | No | Set `settings.workspace.enabled: false` |
| Git LFS or custom filters | No | Disable workspace isolation or wait for an LFS-aware backend |
| `* text=auto`, `eol`, `crlf`, or encoding conversion | No | Disable workspace isolation or wait for a text-normalized contract |
| Submodules | No | Disable workspace isolation or restructure the workflow |
| Sparse or partial clone | No | Use a full clone |
| Native Windows | No | Use WSL or another POSIX environment |
| Ordinary ignored caches | Yes, excluded from lineage | Use setup profiles or writable snapshots |

Workspace-enabled runs require supported Git capabilities, POSIX-compatible
filesystem behavior, and an invoker adapter that honors runtime-supplied `cwd`.

## Declare Worktrees

Workflow-level `worktrees` declare logical source lines and disposable
workspaces:

```yaml
worktrees:
  implementation:
    kind: worktree
    create_branch: true
  scratch:
    kind: snapshot
```

Kinds:

- `worktree`: mutable Git-backed source line that can produce
  `workspace-state*.json`, `workspace-bundles/*.bundle`, and optional local
  branch export.
- `snapshot`: writable disposable checkout for review, audit, test discovery,
  or scratch work. It never produces source lineage or a branch.

Logical worktree names are workflow-local. `none` is reserved for explicit
project-root opt-out.

## Select A Worktree

Provider nodes select a logical workspace:

```yaml
nodes:
  - id: implement
    mode: parallel
    providers: ["codex"]
    worktree: implementation

  - id: summarize
    mode: sequential
    providers: ["claude"]
    worktree: none
```

`worktree: none` opts a node out and runs it at the project root. It produces no
source lineage. Input nodes cannot declare worktree selectors and never allocate
provider workspaces.

If a workflow declares exactly one worktree, non-input provider nodes without an
explicit selector inherit that logical worktree. If a workflow declares multiple
worktrees, provider nodes must select one explicitly or set `worktree: none`.

Same-name `kind: worktree` nodes continue one mutable source line only when the
DAG orders them. A mutable worktree node can have only one executor provider;
reviewer providers remain allowed in sequential review loops. Parallel writers
to the same logical worktree fail validation. Different logical worktree names
represent independent source lines; Crewplane does not merge them automatically.

Common validation failures:

- `snapshot` worktrees cannot use `setup_profile`, `create_branch`, or
  `branch_name`.
- `branch_name` requires `create_branch: true`.
- `clean_start` must be `strict` or `tracked_only`.
- Worktree names cannot be `none`.
- Mutable `kind: worktree` nodes cannot use multiple executor providers.

## Setup Profiles

Config can define audited setup commands:

```yaml
settings:
  workspace:
    setup_profiles:
      bootstrap:
        run:
          - ["uv", "sync"]
```

A `kind: worktree` declaration can select a setup profile:

```yaml
worktrees:
  implementation:
    kind: worktree
    setup_profile: bootstrap
```

Setup commands are argv lists, not shell strings. They run after workspace
materialization and before provider invocation. Setup stdout, stderr, argv, exit
code, duration, and working directory are recorded under the node stage
artifacts. Setup side effects are not lineage unless the final tracked source
tree of a successful `kind: worktree` node captures them.

## File Templates

When workspace isolation is disabled, repo-relative `{{file:path}}` templates
use the existing static preflight behavior and read UTF-8 text from the project
root.

When workspace isolation is enabled for managed workspaces, repo-relative
`{{file:path}}` templates are compiled as workspace-file locators. Runtime
injects Git blob bytes from the invocation source commit, not unchecked bytes
from the live project checkout or workspace filesystem. Reviewer and remediation
rounds resolve file tokens from the current candidate commit.

Absolute paths remain blocked unless explicitly allowlisted through
`settings.integrations.artifacts.options.allowed_template_paths`. Allowlisted
absolute files are static external resources and are not part of Git lineage.

## Branch Export

Branch export is local-only and opt-in per `kind: worktree` declaration:

```yaml
worktrees:
  implementation:
    kind: worktree
    create_branch: true
    branch_name: ai/implement-authentication
```

Crewplane creates or verifies the branch only after a successful workflow,
duplicate skip, or resume has a verified final checkpoint for that logical
worktree. It never pushes, opens a pull request, merges, rebases, switches the
checkout, or updates the user's current branch.

## Artifacts And Cleanup

Successful `kind: worktree` nodes write source-lineage artifacts under the node
stage directory:

- `workspace-state*.json`
- Git bundles under `workspace-bundles/`

Snapshots are disposable. If a snapshot node writes source-looking files,
Crewplane records drift diagnostics and discards those changes according to the
cleanup policy.

Use [workspace cleanup](cleanup.md) to remove generated cache entries:

```bash
crewplane cleanup workspaces --dry-run
crewplane cleanup workspaces --yes
```

Cleanup removes managed cache directories, worktree registrations, reviewer
workspaces, temporary indexes, and run-owned cached refs. It does not remove
canonical lineage artifacts, provider outputs, findings, manifests, or final
result artifacts under `.crewplane/`.

## Implementation Reference

The accepted architecture is [ADR 0016: Node-scoped Git workspace isolation (Experimental)](../architecture/adr/0016-node-scoped-git-workspace-isolation.md).
The developer-facing implementation guide is
[Experimental worktree implementation](experimental-worktree-implementation/index.md).
