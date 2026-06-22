# ADR 0016: Node-Scoped Git Workspace Isolation

## Status
Accepted

## Date
2026-06-12

## Decision
Adopt optional Git-backed workspace isolation for provider nodes while keeping
project-root execution as the default. The durable contract is artifact-first:
provider workspaces are materializations, while source lineage is recorded
through `.orchestrator/` artifacts.

The selected model is:

- `settings.workspace.enabled` gates managed workspace use and defaults to
  `false`.
- Workflow files declare logical worktrees in a workflow-level `worktrees`
  mapping.
- Provider nodes select a logical worktree with `worktree: <name>` or opt out
  with `worktree: none`.
- `kind: worktree` means a Git-backed mutable source line that can produce
  `workspace-state.json`, `workspace.bundle`, and optional local branch export.
- `kind: snapshot` means writable disposable scratch space that never produces
  source lineage or a branch.
- Worktree source is compiled from the workflow DAG and logical worktree name.
  Workflows do not author source override fields.
- The initial supported worktree contract is `blob_exact`, a fail-closed mode
  requiring provider-visible bytes to match Git blob bytes exactly.
- Setup profiles are project-config command lists selected by workflow
  worktree declarations, audited under node stage artifacts, and run only for
  selected `kind: worktree` nodes.
- Optional branch export is local-only, verification-first, and configured per
  logical `kind: worktree` declaration.

Workspace isolation is source-tree isolation, not a provider sandbox. Provider
CLIs still run with the configured provider permissions, approval model, and
process privileges.

## Context
Provider CLIs normally run against the project working directory. That keeps
simple workflows cheap to start, but it creates correctness problems for
parallel and multi-step coding workflows:

1. Parallel nodes can race in one mutable checkout.
2. Ambient dirty, ignored, or untracked files can affect provider behavior.
3. Downstream code lineage is ambiguous when multiple upstream nodes edit the
   same checkout.
4. Retry, duplicate skip, and resume need to distinguish source-tree state from
   ordinary orchestration artifacts.
5. Repo-relative `\{\{file:path\}\}` bytes must match the source tree the
   provider is actually inspecting, especially during review and remediation.
6. Local Git config, attributes, filters, path handling, object-store
   indirection, and provider-created commits can silently change materialized
   bytes or captured result trees unless the runtime defines a bounded contract.

The repository architecture already provides the required ownership split:

- Core composes workflows, validates schemas, and compiles node policy.
- CLI preflight validates source policy, renders execution plans, and records
  deterministic signatures.
- Runtime executes compiled plans, materializes provider `cwd`, and captures
  workspace results.
- `.orchestrator/` artifacts remain the blackboard and audit boundary.
- Invoker adapters own provider launch capabilities, not workspace policy.
- UI adapters remain observer-only.

The adopted design keeps default project-root execution intact for non-Git and
simple projects while allowing workflows to opt into isolated source lines when
they need auditable code lineage.

## Goals
- Preserve blackboard orchestration through `.orchestrator/` artifacts.
- Keep provider invocation CLI-first and provider-neutral.
- Preserve project-root execution by default, including non-Git projects.
- Require Git only when a workflow selects managed `worktrees` and
  `settings.workspace.enabled: true`.
- Preserve artifact-first code lineage through `workspace-state.json` and
  Git `workspace.bundle` artifacts.
- Use workflow-level `worktrees` declarations and node-level `worktree`
  selectors as the only current authored workspace model.
- Let one logical worktree represent a whole source line without repeated
  node-level boilerplate.
- Let different logical worktree names represent independent source lines.
- Let review, audit, test discovery, and reporting tools use writable
  disposable `snapshot` workspaces without failing solely because they wrote
  scratch files.
- Make setup optional, audited, and active only for selected `kind: worktree`
  declarations with a `setup_profile`.
- Keep cache placement useful across machines by excluding `cache_root` from
  semantic identity by default.
- Let users opt into local branch creation per logical worktree.
- Keep unsupported repository features fail-closed with plain diagnostics before
  provider cost.

## Non-Goals
- No automatic merge, rebase, push, pull request creation, checkout switch, or
  user-branch promotion.
- No change to disabled-mode project-root execution semantics.
- No project-config default worktree selector. Workflows own their source-line
  shape.
- No project-root registry of reusable logical worktrees.
- No hidden import of dirty, ignored, or untracked project-root state.
- No provider-specific setup behavior.
- No orchestrator-owned provider permission, sandbox, or approval subsystem.
- No weakening of `{{file:path}}` project-root path restrictions or absolute
  file allowlist behavior.
- No automatic inference from node names such as `plan`, `implement`, or
  `review`.
- No read-only snapshot mode. `snapshot` means writable, disposable, and
  non-lineage.
- No non-filesystem artifact backend support for real execution in this
  release. Such backends may support `validate` and `run --dry-run` advisory
  paths only.
- No provider-created commit objects as lineage parents.
- No reliable detection of transient unreachable provider-created Git objects
  after providers reset `HEAD`.
- No Git LFS, custom filter, text/eol normalization, submodule, sparse checkout,
  partial clone, lazy checkout, or lazy materialization support in the initial
  `blob_exact` contract.
- No custom `.gitignore`, `.gitattributes`, Git pathspec, or raw-byte lineage
  engine in the initial contract.
- No shared mutable workspace across executor providers.
- No guarantee that workspace isolation is a security sandbox.

## Design Principles
1. **Strict core, friendly shell.** Keep the Git contract strict, but expose it
   through actionable diagnostics and concise workflow syntax.
2. **Workflow-owned source lines.** Declare logical source lines once in
   workflow-level `worktrees`, then let nodes inherit or select names with
   `worktree`.
3. **Worktree names are the source-line handle.** Same logical worktree name
   means the same mutable source line. Different names mean independent source
   lines.
4. **Artifacts remain canonical.** Physical worktrees, cache directories, and
   local branches are materializations. Durable source truth remains under
   `.orchestrator/`.
5. **Default to practical reuse.** Machine-local cache paths are execution
   facts, not semantic workflow inputs, unless strict identity is requested.
6. **No surprise promotion.** Branch creation is explicit, default false,
   verification-first, and local-only.

## Configuration Model
Workspace settings live in project config under `settings.workspace`:

```yaml
settings:
  workspace:
    enabled: false
    cache_root: null
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
a workflow declares no `worktrees`, no managed worktree or snapshot is created,
even when workspace support is enabled. Those nodes keep project-root execution:
provider `cwd` is the project root, repo-relative file templates use the normal
project-root preflight behavior, no setup profile runs, no workspace source
policy runs for that node, and no workspace state, bundle, snapshot, or managed
worktree is created.

Project config does not define a default worktree selector. Physical workspace
storage is controlled by `settings.workspace.cache_root`; logical source lines
are declared only by workflow files.

Native Windows support is outside the initial workspace-enabled contract.
Workspace-enabled validation and execution require POSIX-compatible filesystem,
locking, symlink, executable-bit, and path behavior. Disabled-mode project-root
execution remains whatever the CLI otherwise supports.

Real execution is filesystem-artifact-only in this release, even when workspace
isolation is disabled. Same-context locking, duplicate-skip, run-history scan,
resume hydration, run output allocation, and workspace lineage all currently
depend on local `.orchestrator/` filesystem paths. Non-filesystem artifact
backends may participate in `validate` and `run --dry-run`, but real runs fail
before lock, skip, resume, or full-run execution until an equivalent
materialization and integrity contract exists.

## Workspace Support Diagnostics
Workspace-enabled mode fails unsupported repositories before provider cost with
developer-facing messages. Diagnostics should name the unsupported feature,
include representative paths when useful, cite the selected contract mode, and
offer a practical remediation.

Examples:

```text
Workspace-enabled mode does not support Git LFS repositories with worktree_contract: blob_exact.
This repository has effective filter=lfs on assets/logo.png.
Set settings.workspace.enabled: false, run without workspace isolation, or use a future LFS-aware backend.
```

```text
Workspace-enabled mode does not support byte-transforming text attributes with worktree_contract: blob_exact.
This repository has effective text normalization on src/app.ts.
Set settings.workspace.enabled: false, remove the attribute for workspace runs, or use a future text-normalized contract.
```

```text
Workspace-enabled mode requires a Git repository.
No .git directory was found at /home/user/project.
Set settings.workspace.enabled: false to run providers in the project root without workspace isolation.
```

The README and generated config comments should keep this support matrix
visible:

| Repository feature | Initial workspace support | Remediation |
| --- | --- | --- |
| Clean ordinary Git repository | Yes | Use `settings.workspace.enabled: true` with workflow `worktrees` |
| Non-Git project | No | Set `settings.workspace.enabled: false` |
| Git LFS or custom filters | No | Disable workspace isolation or wait for an LFS-aware backend |
| `* text=auto`, `eol`, `crlf`, or encoding conversion | No | Disable workspace isolation or wait for a text-normalized contract |
| Submodules | No | Disable workspace isolation or restructure the workflow |
| Sparse or partial clone | No | Use a full clone |
| Ordinary ignored caches | Yes, excluded from lineage | Use setup profiles or writable `snapshot` scratch space |

## Worktree Contract Modes
The initial authored contract mode is:

```yaml
settings:
  workspace:
    worktree_contract: blob_exact
```

`blob_exact` means:

- Git worktrees are used for mutable source materialization.
- Effective Git LFS and custom filters are rejected.
- Text, eol, encoding, and other byte-transforming attributes are rejected.
- Repo-relative file-token bytes are captured from Git blobs.
- Mutable results are captured only when the final tracked tree is
  representable under the same no-transform contract.

Authored config uses stable behavior names such as `blob_exact`. Persisted
artifacts record the selected mode plus `SCHEMA_VERSION` from
`src/orchestrator_cli/version.py`:

```json
{
  "worktree_contract": {
    "mode": "blob_exact",
    "schema_version": "<SCHEMA_VERSION from src/orchestrator_cli/version.py>"
  }
}
```

If a compact internal contract id is needed, it is derived from `mode` and
`schema_version`; it is not a separate persisted artifact field. Contract mode
names with embedded release versions are rejected in authored config.

Future contract slots are reserved but not accepted until their source,
template, duplicate-skip, and artifact semantics are specified:

- `text_normalized`: may accept common Git text/eol normalization and must
  record whether provider-visible bytes are normalized rather than raw
  blob-identical.
- `lfs_materialized`: may deliberately materialize LFS content and must record
  both pointer and materialized-object identities, verify local availability,
  and fail closed when required LFS content is unavailable.

Changing the selected contract mode or schema version changes workspace-enabled
semantic identity.

## Workflow Model
Workflow files own their logical workspace shape:

```yaml
worktrees:
  implementation_worktree:
    kind: worktree
    setup_profile: node_dependencies
    create_branch: true

  review_snapshot:
    kind: snapshot
```

Rules:

- `worktrees` is workflow-scoped and is not read from project config.
- Each mapping key is a logical worktree name for the current workflow run.
  `none` is reserved for explicit node opt-out and cannot be a worktree name.
- Logical worktree names are not physical directory names and are not looked up
  across workflows or runs.
- A `kind: worktree` entry is a Git-backed mutable source line that can emit
  `workspace-state.json`, `workspace.bundle`, and optional local branch export.
- A `kind: snapshot` entry is writable disposable scratch space. It never emits
  lineage or a branch.
- Same logical worktree name inside one workflow run means nodes continue the
  same mutable source line.
- Different logical worktree names inside one workflow run mean independent
  mutable source lines.
- `setup_profile`, `create_branch`, and `branch_name` are valid only for
  `kind: worktree`; validation rejects them on `kind: snapshot`.
- `create_branch` defaults to false.
- Physical checkout reuse is a runtime optimization for same-name
  `kind: worktree` entries. It is never canonical source state.
- Any two writer nodes selecting the same `kind: worktree` name must be ordered
  by the DAG. Parallel roots or parallel branches that select the same logical
  worktree fail validation because one source line cannot fork and merge
  implicitly.
- A node selecting `kind: worktree` cannot share one mutable checkout across
  multiple executor providers. Review-loop reviewer/executor roles are governed
  by the runtime candidate rules, but mutable source advancement remains single
  executor at a time.

Node selection rules:

- If a workflow declares exactly one worktree entry, provider nodes inherit it
  by default.
- A node may explicitly name the single inherited worktree; validation may emit
  an advisory warning because the selector is redundant.
- If a workflow declares exactly one worktree and a node names a different
  worktree, validation fails because the selector is undeclared.
- If a workflow declares multiple worktree entries, every provider node that
  should run in a managed workspace must name one explicitly.
- If a workflow declares multiple worktree entries, a provider node that should
  run without a managed workspace must explicitly set `worktree: none`.
  Omitting the selector is ambiguous and fails validation.
- `worktree: none` is an explicit opt-out from managed workspace behavior. It
  uses project-root execution, produces no source lineage, and does not let
  downstream code state inherit its file mutations.
- A node naming a worktree that is not declared under `worktrees` fails
  validation.
- Input nodes never allocate provider workspaces and reject explicit `worktree`
  selectors.

Example:

```yaml
nodes:
  - id: implement_authentication
    providers: [codex]
    worktree: implementation_worktree

  - id: review_authentication
    needs: [implement_authentication]
    providers: [claude]
    worktree: review_snapshot
```

## Source Compilation and DAG Rules
Runtime first resolves each node's effective worktree selection, then compiles
source from the DAG:

1. If the node has no managed worktree or selects `worktree: none`, it uses
   project-root execution and no workspace source lineage is compiled.
2. If the selected declaration is `kind: snapshot`, it materializes from the
   run's recorded project source and produces no downstream lineage.
3. If the selected declaration is `kind: worktree` and the node has no ordered
   upstream dependency selecting the same worktree name, it starts from the
   recorded project source.
4. If the selected declaration is `kind: worktree` and it has one or more
   ordered upstream dependencies selecting the same worktree name, it starts
   from the latest ordered upstream on that worktree line. Non-lineage nodes
   such as `worktree: none` and `kind: snapshot` do not reset that source line.
5. If two upstream dependencies select the same worktree name but neither
   depends on the other, validation fails because the workflow forked one
   mutable source line and the runtime will not merge those forks.
6. If a `kind: worktree` node has a direct upstream dependency selecting a
   different `kind: worktree` name, validation fails with an automatic-merge
   diagnostic. Cross-worktree information may flow through ordinary
   artifacts, `worktree: none`, or `kind: snapshot`, but not through implicit
   source merges.

The unordered-writer diagnostic must name the affected nodes and logical
worktree, explain that one mutable source line cannot have parallel writers
without an ordering edge, and offer two fixes: add a `needs` edge to serialize
the source line or use separate worktree names for independent alternatives.

Same-worktree placement does not create DAG edges. It only determines the
source line once DAG ordering exists.

The recorded project source is the Git base commit captured after clean-start
validation. It never imports dirty, ignored, or untracked project-root files.
If a later node intentionally needs to restart from the project base, it should
use a different logical worktree name or a `snapshot`, not a source override.

Other cross-node information continues to flow through ordinary artifact tokens
such as `\{\{node.output\}\}`, `\{\{node.findings\}\}`, or artifact paths.

## Clean-Start Policy
`settings.workspace.clean_start` is a project-level workspace setting:

- `strict`: no tracked changes and no untracked non-ignored source files.
- `tracked_only`: no tracked changes; untracked non-ignored files are allowed
  but excluded from managed workspaces with a warning and preflight note.

`clean_start` defaults to `strict`. It is enforced before provider cost when at
least one node selects a managed `kind: worktree` or `kind: snapshot`
workspace. Nodes that run with `worktree: none`, and workflows without
`worktrees` declarations, use project-root execution and do not trigger
clean-start by themselves.

Clean-start diagnostics should name the logical worktrees that require the
check. If any managed workspace node requires a clean start and the project
source violates the selected policy, the whole run fails before provider cost.
Partial execution of unaffected nodes is a non-goal for the first contract.

## File Template Contract
Workspace isolation updates ADR 0012 only for repo-relative `\{\{file:...\}\}`
tokens in managed workspace invocations. Preflight remains authoritative for
parsing, validation, locator compilation, source policy, signatures, and render
planning.

Repo-relative file tokens are compiled into typed workspace-file locators.
Runtime resolves those locators only after the invocation source identity is
known:

- Initial executor invocations resolve against the node source commit/tree.
- Reviewer invocations resolve against the current runtime-owned candidate
  commit/tree.
- Remediation executor invocations after an accepted candidate resolve against
  the current candidate commit/tree.
- Downstream lineage invocations resolve against the selected upstream result
  commit/tree.
- Input-node file sources resolve against the recorded project source without
  allocating provider workspaces.
- Injected bytes are Git blob bytes after UTF-8 and NUL validation.

Runtime does not inject bytes read from the mutable project checkout or
unchecked materialized workspace paths. Runtime must not parse prompt text,
discover new template tokens, reinterpret workflow source, or rerun workflow
policy.

Disabled mode, `worktree: none`, workflows without `worktrees`, and allowlisted
absolute external file resources keep their existing static preflight behavior.

## Setup Profiles
Setup profiles are a minimal dependency-materialization feature, not a
devcontainer or full environment provisioning system. Project config owns the
command bodies:

```yaml
settings:
  workspace:
    setup_profiles:
      node_dependencies:
        run:
          - ["pnpm", "install", "--frozen-lockfile"]
      python_dependencies:
        run:
          - ["uv", "sync"]
    setup_timeout_seconds: 900
```

Workflow worktrees opt in by name:

```yaml
worktrees:
  implementation_worktree:
    kind: worktree
    setup_profile: node_dependencies
```

Rules:

- Setup defaults to none.
- Defining `setup_profiles` in project config does not force any workflow to
  use them.
- Workflow files cannot define setup command bodies; they can only select
  project-config profiles.
- Project-config setup commands must be non-empty argv arrays. A future shell
  shorthand may be added only with explicit `shell: true`.
- Setup is validated and run only for nodes selecting a `kind: worktree`
  declaration with `setup_profile`.
- If no node selects a `kind: worktree` declaration, setup is not required,
  validated for execution, or run.
- If a workflow uses `kind: worktree` but does not select `setup_profile`, no
  setup runs.
- `kind: snapshot` declarations cannot select `setup_profile`.
- Commands run after workspace materialization and reset verification, before
  provider invocation.
- Commands run with the same effective workspace root as the provider.
- Stdout, stderr, command argv, exit code, duration, and working directory are
  recorded under the node stage directory.
- Nonzero setup exit fails the node before provider invocation.
- Setup output is not injected into prompts unless referenced through ordinary
  artifacts in a future design.
- Setup side effects are not lineage unless the final tracked source tree of a
  `kind: worktree` node captures them.
- Ignored caches such as `node_modules/`, `.venv/`, `.pytest_cache/`, and build
  outputs remain excluded from lineage.
- Duplicate skip includes the selected setup profile name and configured
  command payload, but not wall-clock duration or cache paths.
- Setup command argv and output are persisted as audit records. Project
  authors should not place secrets directly in setup argv or emit them in setup
  output.

Setup artifacts are written only when setup is needed:

```text
.orchestrator/execution-stages/<run-key>/<node>/workspace-setup/
  setup.log
  setup.json
```

## Snapshot Workspaces
`kind: snapshot` creates writable disposable scratch space for audit, review,
report generation, test discovery, and similar non-lineage tasks.

Rules:

- Materialize source from the run's recorded project source.
- Allow provider and tool writes inside the disposable source directory.
- Allow new files, cache directories, coverage output, build output, and temp
  files inside the snapshot checkout.
- Discard the directory after invocation according to cleanup policy.
- Never export `workspace.bundle`.
- Never provide downstream source lineage.
- Record a drift summary after invocation when source-looking files changed.
- Do not fail solely because files were created or modified.
- Durable reports must be written through ordinary provider output and
  orchestrator artifact paths, not by relying on files left in the discarded
  snapshot checkout. Provider output files and logs remain orchestrator-managed
  durable artifacts outside the disposable snapshot source directory.

Example warning:

```text
Node 'review_repository' used snapshot workspace and changed 14 source paths.
Changes were discarded and did not produce lineage. Use a kind: worktree declaration if the node should edit code.
```

## Worktree Runtime Contract
`kind: worktree` nodes are lineage-producing checkpoints. Every successful
`kind: worktree` node writes its own state and bundle artifacts:

- `workspace-state.json`
- `workspace.bundle`

A three-node worktree source line produces three state files and three bundles.
Earlier artifacts remain valid audit, resume, and branch-export boundaries. A
physical checkout is only a cache of the latest verified node boundary.

Runtime lineage is created only from runtime-owned candidate and result
commits. Provider-created commit objects are never used as lineage parents,
never used as source for result trees, and are not preserved as lineage
history. Observable provider Git state that would make lineage ambiguous fails
the node, including:

- final `HEAD` movement away from the runtime baseline;
- branch attachment or checkout-state drift;
- protected orchestrator ref mutation;
- worktree-specific config creation or mutation;
- local attribute or ignore source mutation that affects runtime capture;
- `.gitattributes` changes before lineage export.

Runtime does not scan the full object database for transient or unreachable
provider-created commit objects. Such objects cannot become lineage and may
remain until normal Git retention and garbage collection remove them.

In sequential review loops, reviewer invocations inspect the current
runtime-owned candidate source. Reviewer source mutations do not advance the
logical worktree. Only executor or remediator invocations can produce candidate
or result source checkpoints, and only through runtime-owned capture.

`workspace-state.json` records, at minimum:

- worktree contract mode and schema version;
- logical worktree name;
- workspace kind and materialization type;
- source kind, source node, source commit/tree, and source bundle digest when
  applicable;
- result commit, result tree, and result bundle digest for successful
  `kind: worktree` nodes;
- same-worktree reuse or fallback metadata;
- setup summary when setup ran;
- cache-root and physical-path execution facts;
- branch export fulfillment result when applicable;
- snapshot drift summary for `kind: snapshot` nodes.

Rehydrating a source chain imports bundles in dependency order. For example, if
C starts from B and B starts from A, the workspace manager imports A's bundle,
then B's bundle, then validates C's recorded source commit/tree before
materializing C.

## Same-Worktree Reuse and Resume
Same-name sequential `kind: worktree` nodes may reuse a physical checkout only
after the prior node has been captured as an additive verified checkpoint.

Same-worktree reset semantics:

1. Node N runs in the logical worktree checkout.
2. Runtime captures node N's result commit and exports node N's bundle.
3. Runtime normalizes the checkout to a clean source tree at node N's result
   commit.
4. Runtime removes untracked non-ignored files.
5. Runtime removes ignored scratch files by default with an equivalent of
   `git clean -ffdx`.
6. Runtime verifies clean index, expected `HEAD`, source tree, protected refs,
   and worktree contract.
7. Node N+1 setup runs only after reset and verification.

Node N+1 never starts from dirty provider leftovers. It starts from the exact
source commit compiled from the DAG and logical worktree policy.

Failed or cancelled `kind: worktree` nodes do not advance the canonical source
line. Their physical checkouts may be retained for debugging, but they are
untrusted execution state. Resume rehydrates from the latest verified
successful node boundary for the selected worktree name, or from the recorded
project source if there is no verified boundary. Runtime may reset a retained
checkout back to the verified source commit; if reset verification fails, it
falls back to a fresh managed checkout and records the fallback.

Large repository behavior:

- The first node for a logical worktree creates a full checkout.
- Later sequential nodes for the same logical worktree use incremental reset
  and clean when safe.
- Runtime does not perform a full re-checkout between same-worktree nodes on
  the fast path.
- Unsafe reuse falls back to a fresh managed checkout and records the fallback.
- Git worktree administration is serialized per source repository to avoid
  `.git/worktrees/` lock contention.
- A configurable maximum limits concurrently materialized managed checkouts.
- Disk guardrails estimate required checkout capacity and warn or fail when
  configured thresholds would be exceeded.
- Checkout size and provisioning duration are execution metadata.

Ignored cache preservation is intentionally not part of this decision. A future
cache policy may preserve selected ignored dependency directories, but that
would be an explicit execution-cache feature with observability and
duplicate-skip semantics.

## Workflow Identity
Workspace identity uses three buckets.

Semantic workflow identity includes:

- composed workflow content;
- provider execution settings that affect invocation;
- selected `worktree_contract` mode;
- worktree contract schema version from `SCHEMA_VERSION` in
  `src/orchestrator_cli/version.py`;
- source repository identity and captured source commit/tree;
- workspace policy, including worktree kind, compiled source, logical worktree
  name, and selected setup profile;
- selected setup profile command payload;
- static file locators and template inputs;
- artifact settings that affect provider-visible content.

Execution facts include:

- resolved `settings.workspace.cache_root`;
- physical provider `cwd`;
- physical managed worktree path;
- run id, timing, host, user, and cleanup metadata;
- branch creation request, branch name, verification status, and local branch
  created after the run.

By default, `cache_root` is excluded from semantic identity:

```yaml
settings:
  workspace:
    identity:
      include_cache_root: false
```

With `include_cache_root: true`, a different absolute cache path changes the
workflow signature because provider-visible `pwd` is considered semantic.
When no workflow node selects a managed workspace, workspace gate settings do
not affect the semantic workflow identity.

Branch creation is also outside semantic workflow identity. Changing only
`create_branch` or `branch_name` must not force provider reruns or change the
workflow signature. Branch export has its own fulfillment record evaluated
after real execution, duplicate skip, or resume selects a successful run.

## Branch Export
Branch export is workflow config on a workflow-declared `kind: worktree` entry:

```yaml
worktrees:
  implementation_worktree:
    kind: worktree
    create_branch: true
    branch_name: ai/implement-authentication
```

Rules:

- `create_branch` defaults to false.
- `create_branch` and `branch_name` are valid only on `kind: worktree`.
- `branch_name` requires `create_branch: true`.
- A workflow with multiple `kind: worktree` declarations can opt each one in
  or out independently.
- If `create_branch: true` is set on a declared worktree that no node selects,
  validation fails because there can be no verified result checkpoint.
- If the workflow fails or is cancelled, no automatic branch is created.
  Completed bundles remain available for future manual export tooling.
- If `branch_name` is omitted, runtime generates a local branch name from the
  workflow slug, declared worktree name, and run key:
  `orchestrator/<workflow>/<worktree>/<run-key>`.
- Existing branch names are refused by default. When fulfilling a branch export
  for a previously verified successful run, an existing branch is accepted only
  if it already points at the verified result commit for that fulfillment.
- Runtime creates or verifies the local branch after successful workflow
  completion, duplicate-skip, or resume, and only after the final successful
  checkpoint for that worktree name is verified.
- Verification covers `workspace-state.json`, `workspace.bundle`, upstream
  bundle imports, result commit, result tree, and worktree contract before
  touching refs.
- Branch export status is recorded as fulfillment metadata and does not
  participate in semantic workflow identity.
- Fulfillment records include status (`fulfilled`, `skipped`, or
  `failed_verification`), operation, branch existence before and after
  verification, checkpoint identity, and failure message when applicable. They
  are written to `workspace-state.json` and `workspace-exports/`.
- Branch export never pushes, opens a pull request, merges, rebases, switches
  checkout, or updates the user's current branch.
- Dry-run verifies the selected successful run's export plan and prints the
  planned operation without creating refs.

Example output:

```text
Verified workspace result:
  worktree: implementation_worktree
  source: 1a2b3c4
  result: 9f8e7d6
  changed files: 12

Created local branch ai/implement-authentication at 9f8e7d6.
```

## Security Boundary
Workspace isolation does not sandbox provider execution. Provider CLIs and
setup profiles run with the user's process permissions and may read, write,
execute, or access the network according to their own configuration and the
provider's permission model.

The security value of workspace isolation is source-line determinism and
auditable lineage, not containment. Use disposable repositories, worktrees,
containers, VMs, CI runners, or provider-native sandbox controls when provider
tool execution requires a stronger trust boundary.

Branch export is local and verification-first, but it still creates local refs.
It must never push, open pull requests, merge, rebase, or switch the user's
current checkout.

## Git Feature Boundaries
`blob_exact` fails closed for repository features that can change
provider-visible bytes or captured lineage:

- Git LFS and custom filters.
- Text, eol, encoding, ident, or other byte-transforming attributes.
- Submodules.
- Sparse checkout and partial clone.
- Object alternates, grafts, replacement refs, promisor objects, and lazy
  object modes.
- Worktree-specific Git config.
- Local, global, or system Git config that cannot be safely ignored or
  overridden and that can alter repository discovery, checkout, index, path,
  ignore, attribute, filter, object, ref, or worktree semantics.
- Unsupported filesystem behavior, including unsafe case or Unicode collisions,
  missing symlink support, or executable-bit behavior that the runtime cannot
  prove safe.

Runtime-owned Git commands use sanitized environment and literal path semantics
for user-authored paths. Source and result trees are rejected when path
collisions cannot be represented safely on the active filesystem.
Detached `HEAD` is supported when `HEAD^{commit}` resolves and all source
policy checks pass.

Submodules deserve a clear error because recursive setup changes the source
contract. Future submodule support must be an explicit contract mode or config
toggle, not a silent `--recurse-submodules` behavior.

Git administrative entries are not trusted without verification. Before reuse,
runtime validates both the checkout path and Git metadata. If an admin entry is
missing or inconsistent, runtime fails with a repair or cleanup diagnostic
rather than continuing from an untracked directory.

## Lifecycle and Cleanup
Cancellation and cleanup are explicit:

1. Mark the run cancelled in run-root metadata.
2. Stop or drain active provider invocations according to existing timeout and
   subprocess policy.
3. Capture terminal node artifacts that can be safely attributed.
4. Run idempotent cleanup for disposable snapshots and unlocked failed
   worktree materializations.
5. Preserve completed worktree checkpoints, bundles, manifests, and branch
   export records.

Cleanup tolerates missing worktree directories, missing Git admin entries, and
already-removed cache paths. It must never delete a user-created branch or a
worktree not recorded as orchestrator-managed for the current run.

`orchestrator cleanup workspaces` may remove stale orchestrator-managed
workspace cache entries, but canonical audit artifacts remain under
`.orchestrator/`.

## Observability and User Feedback
Validate, dry-run, runtime output, persistent summaries, and generated docs must
surface workspace behavior without exposing implementation internals as primary
UX.

The CLI and observability layers should show:

- unsupported-repository diagnostics and remediation;
- selected contract mode and schema version;
- logical worktree names and kinds;
- compiled source summary for managed nodes;
- setup profile selection, status, duration, and failure summary;
- physical checkout reuse, fallback, and reset verification result;
- snapshot discarded drift counts and bounded path samples;
- cache root and physical provider `cwd` as execution facts;
- branch export verification and fulfillment details.

Dry-run previews setup profiles, compiled worktree sources, rendered
workspace-file locators, cleanup behavior, and planned branch export
fulfillment without mutating Git refs.

## User-Facing Examples

The `schema_version` placeholders below stand for the current
`SCHEMA_VERSION` exported by `src/orchestrator_cli/version.py`; generated
templates should render that value instead of duplicating a literal in docs.

### All Mutating Nodes Use One Worktree
Define the logical worktree once at workflow scope. Each provider node inherits
it. The second node continues from the first because it has a same-worktree
direct dependency.

```markdown
---
schema_version: "<SCHEMA_VERSION from src/orchestrator_cli/version.py>"
name: Authentication Implementation
worktrees:
  implementation_worktree:
    kind: worktree
    setup_profile: node_dependencies
    create_branch: true
    branch_name: ai/implement-authentication
nodes:
  - id: implement_authentication
    mode: sequential
    providers: [codex]

  - id: test_and_fix_authentication
    mode: sequential
    needs: [implement_authentication]
    providers: [codex]
---

## implement_authentication
Implement the authentication workflow and run the focused test suite.

## test_and_fix_authentication
Continue from the implementation worktree, run the full test suite, and fix
failures.
```

### Plan in Snapshot, Then Reuse One Implementation Worktree
Planning can use writable disposable scratch space while implementation and
review/fix continue one mutable source line.

```markdown
---
schema_version: "<SCHEMA_VERSION from src/orchestrator_cli/version.py>"
name: Planned Authentication Implementation
worktrees:
  planning_snapshot:
    kind: snapshot

  implementation_worktree:
    kind: worktree
    setup_profile: node_dependencies
    create_branch: true
nodes:
  - id: plan_authentication_change
    mode: sequential
    providers: [claude]
    worktree: planning_snapshot

  - id: implement_authentication_change
    mode: sequential
    needs: [plan_authentication_change]
    providers: [codex]
    worktree: implementation_worktree

  - id: review_and_fix_authentication_change
    mode: sequential
    needs: [implement_authentication_change]
    worktree: implementation_worktree
    providers:
      - provider: claude
        role: reviewer
      - provider: codex
        role: executor
---

## plan_authentication_change
Review the current code and produce an implementation plan.

## implement_authentication_change
Implement the plan from `{{plan_authentication_change.output}}`.

## review_and_fix_authentication_change
Review the current implementation, report findings, and apply required fixes.
```

### Separate Worktrees for Alternative Implementations
Use different logical worktree names when nodes should explore independent
source states. Compare alternatives through artifacts, not implicit source
merges.

```markdown
---
schema_version: "<SCHEMA_VERSION from src/orchestrator_cli/version.py>"
name: Alternative Authentication Implementations
worktrees:
  conservative_implementation_worktree:
    kind: worktree
    create_branch: true

  experimental_implementation_worktree:
    kind: worktree
    create_branch: false

  comparison_snapshot:
    kind: snapshot
nodes:
  - id: conservative_authentication_implementation
    mode: sequential
    providers: [codex]
    worktree: conservative_implementation_worktree

  - id: experimental_authentication_implementation
    mode: sequential
    providers: [codex]
    worktree: experimental_implementation_worktree

  - id: compare_authentication_implementations
    mode: sequential
    needs:
      - conservative_authentication_implementation
      - experimental_authentication_implementation
    providers: [claude]
    worktree: comparison_snapshot
---

## conservative_authentication_implementation
Implement the conservative approach.

## experimental_authentication_implementation
Implement the experimental approach.

## compare_authentication_implementations
Compare the two implementation reports and recommend one path forward.

Read parent result artifacts from paths instead of asking the orchestrator to
inline their full contents:

- Conservative implementation report:
  `{{conservative_authentication_implementation.output_path}}`
- Experimental implementation report:
  `{{experimental_authentication_implementation.output_path}}`

Write the comparison and recommendation as this node's normal output.
```

### Review With Writable Snapshot
No extra mutability setting is needed. `snapshot` is writable and disposable.

```markdown
---
schema_version: "<SCHEMA_VERSION from src/orchestrator_cli/version.py>"
name: Repository Review
worktrees:
  repository_review_snapshot:
    kind: snapshot
nodes:
  - id: review_repository
    mode: sequential
    providers: [claude]
---

## review_repository
Review the repository and write findings to the normal node output.
```

### Non-Git Project
Managed `kind: worktree` and `kind: snapshot` workspaces are Git-backed.
Non-Git projects should avoid workflow `worktrees` declarations or disable
workspace isolation and run providers in the project root.

```yaml
settings:
  workspace:
    enabled: false
```

## Validation Requirements
Validation must cover the contract, not just parser shape:

- Invalid `worktree_contract` and unsupported future contract modes fail.
- Invalid `settings.workspace.clean_start` fails; omitted value defaults to
  `strict`.
- Workflow `worktrees` declarations require `settings.workspace.enabled: true`;
  otherwise validation fails before provider invocation.
- Setup profile selections on `kind: worktree` declarations must reference
  project-config profiles.
- Workflow files cannot define setup command bodies.
- Project-config setup commands must be non-empty argv arrays.
- `setup_profile`, `create_branch`, and `branch_name` are valid only on
  `kind: worktree`.
- `branch_name` without `create_branch: true` fails validation.
- `none` is reserved and cannot be declared as a logical worktree name.
- Workflows with no `worktrees` declarations run every provider node with
  project-root execution and allocate no managed workspaces.
- Single-worktree workflows allow inheritance and may warn on redundant
  selectors.
- Multi-worktree workflows require explicit node selectors or `worktree: none`.
- Undeclared worktree selectors fail validation.
- Input nodes reject explicit `worktree` selectors.
- Same-name `kind: worktree` writers must be DAG-ordered.
- Same-worktree source compiles to the recorded project source or latest
  ordered upstream on that logical worktree.
- Direct dependencies between different `kind: worktree` source lines fail with
  an automatic-merge diagnostic.
- `worktree: none` compiles to project-root execution and produces no workspace
  state or bundle.
- Mutable `kind: worktree` nodes reject multiple executor providers sharing one
  checkout.
- `strict` clean-start rejects tracked and untracked non-ignored changes.
- `tracked_only` clean-start rejects tracked changes and warns about excluded
  untracked non-ignored files.
- Successful `kind: worktree` nodes emit state and bundle artifacts.
- Snapshot writes do not fail the node, emit no bundle, and record discarded
  drift.
- Setup runs only before provider invocation for selected `kind: worktree`
  nodes with setup profiles.
- Setup-created ignored caches are excluded from lineage.
- Changing cache root does not affect the default workflow signature.
- Enabling workspace support without any selected managed workspace does not
  affect the workflow signature.
- Enabling `identity.include_cache_root` makes cache-root changes affect the
  workflow signature.
- Changing selected setup profile command payload or contract mode changes the
  workflow signature.
- Changing only `create_branch` or `branch_name` does not change the workflow
  signature.
- Branch export targets the final verified checkpoint for the logical worktree,
  verifies state and bundles, refuses unrelated existing branches, and never
  pushes, opens pull requests, merges, rebases, or switches checkout.
- Duplicate skip and resume can fulfill or verify branch export without
  provider reinvocation.
- Dry-run verifies and prints planned branch operations without creating refs.

## Documentation Requirements
Documentation and generated examples must stay aligned with this ADR:

- README workspace docs include the support matrix.
- Generated config comments cover disabled mode for non-Git projects,
  `blob_exact`, clean-start modes, setup profiles, cache-root identity, and
  branch export.
- Generated workflow examples cover inherited single worktrees, separate
  alternative worktrees, writable disposable snapshots, `worktree: none`, and
  worktree-level branch export.
- Workflow docs explain that same-worktree nodes may reuse a physical checkout,
  but canonical state still flows through compiled source and verified node
  artifacts.
- Workflow docs explain that the recorded project source is the clean Git base
  captured at preflight, not a dirty project-root working tree.
- Review-loop docs distinguish reviewer findings from executor or remediator
  source mutations.
- Artifact-handoff docs should stay aligned with automatic parent output path
  handoff so workflows can pass parent outputs by path without inlining large
  result contents.

## Future Extensions
These extensions are intentionally outside the initial accepted contract but
are reserved by the design:

- A manual branch-export command may export verified checkpoints from failed or
  cancelled runs that still produced successful worktree boundaries.
- A guarded branch-update option may allow updating an existing local branch
  after verifying its current commit and the selected checkpoint.
- A `text_normalized` contract must specify whether raw blob digests,
  normalized working-tree digests, or both are persisted and compared.
- An `lfs_materialized` contract must record both LFS pointer and materialized
  object identities and fail closed when required LFS content cannot be verified
  locally.
- Lazy `--no-checkout`, sparse materialization, or lazy file materialization may
  be considered after full-checkout worktrees are stable.
- Preserving selected ignored dependency directories requires an explicit
  execution-cache policy with observability and duplicate-skip semantics.

## Consequences

### Positive
- Parallel coding workflows can isolate source trees without hidden merges.
- Downstream mutable code state is explicit and auditable.
- Workflows can express the common "continue this source line" case with one
  logical worktree declaration.
- Unsupported repository features fail before provider cost with actionable
  diagnostics.
- Duplicate skip and resume remain artifact-backed rather than cache-path
  dependent.
- Local branch export gives users a familiar Git bridge without promoting
  changes automatically.

### Negative

- The initial `blob_exact` contract rejects many common Git repository features.
- Workspace-enabled runs require Git and POSIX-compatible behavior.
- The workflow schema gains a new source-line model that users must learn.
- Setup profiles are arbitrary commands and inherit the same trust boundary as
  provider CLIs.
- Same-worktree reuse, bundle rehydration, and branch export add runtime state
  and validation paths to maintain.

## Rejected Alternatives
- **Keep project-root execution as the only model.** Too much source-state
  ambiguity for parallel or multi-step coding workflows.
- **Use workflow-scoped mutable workspaces.** Easier to explain, but it hides
  source forks and makes parallel node behavior ambiguous.
- **Use provider-owned workspace behavior.** Couples orchestration semantics to
  provider-specific features and weakens adapter boundaries.
- **Automatically merge worktree results.** Would introduce hidden conflict
  handling and branch-promotion behavior outside the blackboard contract.
- **Author per-node source override fields.** Exposes node-scoped
  implementation mechanics instead of workflow-owned source lines.
- **Make snapshots read-only.** Many review and audit tools need writable
  scratch space for caches and temporary files.
- **Include cache root in semantic identity by default.** Maximally strict but
  harms duplicate-skip reuse across machines for little practical gain.
- **Use patches or tarballs for lineage.** Git bundles preserve exact object
  identity and support verification through normal Git plumbing.
- **Add devcontainer or VM provisioning now.** Too broad for source isolation
  and would mix environment management with lineage semantics.

## Updates
- Updates ADR 0012 only when workspace isolation is enabled:
  repo-relative `\{\{file:...\}\}` tokens become workspace-file locators backed
  by Git blob bytes, literal path resolution, and per-invocation source
  identities.
- Extends ADR 0014 with workspace lineage artifact hydration,
  invocation-source descriptor hydration, duplicate-skip verification, and
  resume validation.
- Refines ADR 0001 for mandatory invocation `cwd`, optional invoker workspace
  capability validation, and command-runner child-process environment
  sanitation while keeping workspace policy out of provider adapters and
  `InvocationPlan`.

## References
- [Modular Orchestration Architecture](../modular-orchestration-architecture.md)
- [ADR 0001: Ports + Adapters Runtime Integrations](0001-ports-adapters-runtime-integrations.md)
- [ADR 0011: Review Execution Workflow Optimization](0011-review-execution-workflow-optimization.md)
- [ADR 0012: Preflight-Compiled Runtime Execution Plan](0012-preflight-compiled-runtime-execution-plan.md)
- [ADR 0013: Version Source of Truth and Documentation Drift Reduction](0013-version-source-of-truth-and-documentation-drift-reduction.md)
- [ADR 0014: Artifact-Backed Node-Boundary Resume](0014-artifact-backed-node-boundary-resume.md)
- [Git Worktree Documentation](https://git-scm.com/docs/git-worktree)
- [Git Bundle Documentation](https://git-scm.com/docs/git-bundle)
- [Git Check Ref Format Documentation](https://git-scm.com/docs/git-check-ref-format)
- [Git Rev Parse Documentation](https://git-scm.com/docs/git-rev-parse)
- [Git Cat File Documentation](https://git-scm.com/docs/git-cat-file)
- [Git LS Tree Documentation](https://git-scm.com/docs/git-ls-tree)
- [Git Read Tree Documentation](https://git-scm.com/docs/git-read-tree)
- [Git Checkout Index Documentation](https://git-scm.com/docs/git-checkout-index)
- [Git Write Tree Documentation](https://git-scm.com/docs/git-write-tree)
- [Git Check Attr Documentation](https://git-scm.com/docs/git-check-attr)
- [Git Attributes Documentation](https://git-scm.com/docs/gitattributes)
- [Git Ignore Documentation](https://git-scm.com/docs/gitignore)
- [Git Config Documentation](https://git-scm.com/docs/git-config)
- [Git Status Documentation](https://git-scm.com/docs/git-status)
- [Git Diff Tree Documentation](https://git-scm.com/docs/git-diff-tree)
- [Git Diff Index Documentation](https://git-scm.com/docs/git-diff-index)
- [Git Pathspec Options and Environment](https://git-scm.com/docs/git)
- [Git Glossary: Pathspec](https://git-scm.com/docs/gitglossary)
