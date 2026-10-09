# ADR 0018: Native Windows Support

## Status
Accepted design; native acceptance pending

## Date
2026-10-06

## Decision

Introduce native Windows support in stages, beginning with fresh workflows
running from the project root. The initial target is Python 3.13 and 3.14 on local
NTFS, installed through pip or uv, with `settings.workspace.enabled: false`.

The initial scope includes sequential and parallel execution, review loops,
generated-file capture, console progress, logs, summaries, successful-run
deduplication, and `--force`. Validation and dry-run remain artifact-free. Authored
config and workflow schemas and the artifact layout stay unchanged. WSL retains
the Linux behavior.

Record later stages of Windows support as amendments to this ADR, with their
scope and acceptance evidence, so the Windows design remains in one place.

## Context

Ordinary workflow execution depends on process ownership, contained file access,
exact artifact bytes, and exclusive run locks. The existing POSIX mechanisms do
not establish those guarantees on Windows; removing the platform warning alone
cannot enable support.

Fresh project-root execution is a useful first stage because it preserves the
existing workflow model while deferring the additional ownership and restoration
contracts needed for managed workspaces and interrupted-run recovery.

## Architecture Boundaries

Keep the existing responsibilities: core compiles and validates, invoker adapters
prepare provider commands, runtime owns process execution, and artifacts own
durable publication. The live UI remains observational. Windows support does not
require a new public integration port, OS plugin registry, scheduler, or provider
SDK.

## Execution Contract

### Workflow and Lock Lifecycle

Reject enabled managed workspaces before provider launch. A verified successful
run for the same context and workflow signature skips execution. Otherwise, start
a fresh run without restoring progress from failed or cancelled history. Dry-run
reports the same decision. `--force` bypasses successful-run deduplication, but
never locks or safety checks.

Any existing lock for the same execution context blocks a Windows run, including
malformed or ownerless locks. Do not probe recorded process IDs or attempt
automatic stale-lock recovery. Release a run's own lock only after process cleanup
and final run status are confirmed; unresolved cleanup retains the blocking lock.

### Provider Invocation

Contain provider process trees in Windows Job Objects and record ownership before
provider execution begins. Drain captured output within bounded deadlines and
confirm that all descendants have stopped before reporting invocation completion
or retrying, including when the original process has already exited. If
containment cannot be established, fail before starting the provider.

The CLI adapter resolves normal installed commands, including native executables,
batch launchers, and PowerShell launchers. It owns any shell invocation and must
preserve literal arguments and prompt transport. Runtime consumes provider-neutral
executable/argument plans. Unsupported launcher or argument combinations fail
explicitly; do not inspect provider-internal entry points, bypass execution
policy, or silently retry through another launcher.

Crewplane requires Python and its declared dependencies. Provider CLIs remain
responsible for their own runtimes; installing a provider through npm does not
make Node.js a Crewplane dependency.

### Artifacts and Paths

Use Windows file handles to preserve file identity and containment throughout
reads and publication. Reject filesystem links where they would violate the
existing containment rules. Canonical output publication must not overwrite an
existing output. Source changes during capture must be detected.

Persisted artifact locators remain relative paths with forward slashes and must
be unambiguous on Windows. Generated names must be valid and distinct. Authored
file references continue to accept native paths under the existing project-root
and allowlist policy.

Write Crewplane-generated text as UTF-8 with LF line endings and bind hashes to
the exact persisted bytes. Preserve provider output and captured-file bytes
without newline conversion.

## Non-Goals

The initial stage excludes:

- Managed worktrees, snapshot workspaces, and workspace maintenance.
- Interrupted-run resume and automatic stale-lock recovery.
- Native tmux, Crewplane's npm wrapper, and native self-update.
- Certification of network shares or non-NTFS filesystems.
- Automatic provider runtime installation, ACL management, and artifact migration.

## Rejected Alternatives

- **Track only the main process or establish containment after launch.** A
  provider can create descendants before ownership is established, so these
  approaches cannot guarantee complete cleanup.
- **Rely on pathname checks alone.** Files and directories can change between
  validation and access, breaking containment and artifact integrity.
- **Reuse POSIX stale-lock recovery.** A recorded process ID does not establish
  Windows process ownership or prove that all descendants have stopped.
- **Discover internal provider entry points.** This couples Crewplane to provider
  packaging details and bypasses the normal installed command contract.

## Consequences

### Positive

- Native Windows gains the existing workflow and audit model without requiring
  WSL or a separate orchestration architecture.
- Platform behavior stays behind the existing adapter, process, and artifact
  boundaries.

### Negative

- Interrupted runs may require manual lock recovery and must restart from the
  beginning.
- Metadata replacement provides atomic visibility without a POSIX-equivalent
  power-loss durability guarantee. Fingerprint keys and artifacts rely on project
  ACLs rather than POSIX ownership checks.
- Windows support needs native acceptance evidence. Require Windows CI on Python
  3.13 and 3.14, installed-package checks without Node.js, and recorded smoke
  results for each advertised provider installation form, including artifact
  verification and descendant cleanup. Linux tests and platform mocks do not
  establish Windows acceptance; retain Linux and macOS coverage.

## Related Decisions

- [ADR 0001: Ports, Adapters, and Runtime Integrations](0001-ports-adapters-runtime-integrations.md)
- [ADR 0014: Artifact-Backed Resume](0014-artifact-backed-node-boundary-resume.md)
- [ADR 0016: Node-Scoped Git Workspace Isolation](0016-node-scoped-git-workspace-isolation.md)
