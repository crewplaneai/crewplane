# ADR 0014: Artifact-Backed Resume

## Status

Accepted

Extended on 2026-10-02 with [review-loop checkpoints](#review-loop-checkpoints).

## Date

2026-06-09

## Decision

Implement filesystem-backed same-context resume for failed or cancelled workflow
runs by reusing validated artifacts. The first implementation reused only
completed nodes to keep development scope manageable. Resume within an unfinished
node was deferred to a later iteration.

Preserve ADR 0009 whole-workflow idempotency first: any valid same-context
successful `run.json` skips the workflow before failed or cancelled resume is
considered.

Each execution attempt that proceeds receives a fresh `run_id` and fresh run
directories. Current run state is written under:

- `.crewplane/execution-stages/<run_key>/manifests/run.json`
- `.crewplane/execution-stages/<run_key>/manifests/nodes/`
- `.crewplane/execution-stages/<run_key>/manifests/provider-processes/`
- `.crewplane/locks/`

`run_key` is a bounded generated path component composed as:

```text
<safe_workflow_name>--<workflow_name_hash>-<run_id>
```

- `safe_workflow_name`: the workflow name lowercased and slugged for artifact
  paths.
- `workflow_name_hash`: the first 12 lowercase hexadecimal characters of the
  SHA-256 hash of the original workflow name. This disambiguates workflow names
  that slug or truncate to the same prefix.
- `run_id`: a wall-clock timestamp shaped as `YYYYMMDD-HHMMSS`, with a
  microsecond suffix on same-second allocation retry.

Cancelled run manifests record explicit reasons for UI stops, external
cancellation, and stale-lock recovery.

Run manifests validate fields by status. Failed and cancelled manifests require
nonblank reasons; successful manifests reject failure and cancellation reasons.
A resumed run records its source and the work restored from it. Crewplane
records reuse only after the required files are copied and verified.

For completed nodes, resume copies validated consolidated result, required
findings, and generated-file artifacts. Workspace-enabled resume may also copy
only lineage artifacts named by validated node-state descriptors, including the
selected canonical review output and `review-state/review-loop-status.json` when needed
to reconstruct lineage. Each copied file must be contained, regular, safely
mapped into the fresh run, and match its recorded hash and size; hydrated
workspace state is rewritten through the resume schema to preserve provenance
and fresh-run identity. Arbitrary or unselected stage output, logs, scratch
state, live workspaces, cached refs, symlinks, and hardlinks remain ineligible.

## Rationale

Files on disk let maintainers inspect and verify the work being reused. Each
resumed attempt gets fresh run directories, leaving the failed or cancelled run
available for investigation. Recovery uses these files without hidden cross-node
state or provider-specific replay.

## Design Tradeoffs

- Fresh-run resume preserves a complete audit trail for both the failed source
  run and the resumed run, but it duplicates consolidated artifacts and creates
  more directories than mutating the failed run in place.
- The initial implementation required less saved state, but restarted every
  unfinished node. Review-loop checkpoints now allow reuse between completed
  phases.
- Restricting hydration to stable result artifacts and descriptor-named
  workspace lineage keeps downstream templates and source reconstruction
  equivalent to a fresh upstream completion. Unlisted stage output, logs, and
  scratch state remain only in the source run.
- Success-first duplicate detection keeps ADR 0009 whole-workflow idempotency
  authoritative, even when a newer failed or cancelled same-context run exists.
- Filesystem-only v1 allows strict local path, symlink, hardlink, and lock
  safety checks. Real runs with non-filesystem artifact backends fail before
  skip, resume, or full-run semantics are applied until those backends have an
  equivalent safety contract.
- Unsafe or ambiguous history fails closed. Corrupt manifests and node-state
  records are ignored for reuse, while unsafe filesystem metadata or live locks
  block takeover instead of risking reuse of untrusted artifacts.
- The built-in CLI invoker records each child process attempt separately from
  node completion. Stale-lock recovery blocks takeover while a recorded child
  or its process group is still running, or while the child cannot be identified
  safely. These records are a process-liveness guard; they do not make an
  incomplete invocation or node reusable.
- `--dry-run` reports advisory decisions without acquiring locks or recovering
  stale owners, so its answer can differ from a later real run.

## Rejected Alternatives

- Mutate the failed or cancelled run directory in place. Rejected because it
  would blur postmortem state, make terminal manifests harder to trust, and hide
  which artifacts came from the original attempt versus the resumed attempt.
- Add fine-grained successful-run caching. Rejected to preserve ADR 0009's
  coarse workflow-level idempotency and avoid a dependency-aware drift engine in
  this change.
- Hydrate whole stage directories or arbitrary provider logs and review state.
  Rejected because those files are not the stable downstream contract and may
  contain provider-specific or partial execution state. Workspace lineage is
  limited to descriptor-named, integrity-checked files.
- Use provider-native replay. Rejected because it would require the runtime
  scheduler to understand provider-specific behavior that belongs in adapters.
- Generalize resume to every artifact backend immediately. Rejected because the
  current design depends on local filesystem containment checks, atomic writes,
  hardlink/symlink rejection, and process-owned locks that do not yet have a
  portable artifact-store contract.
- Use workflow signature alone as the run directory key. Rejected because
  duplicate and resume decisions need searchable history across separate
  attempts, while every execution attempt still needs a fresh auditable output
  location.

## Consequences

### Positive

- Failed or cancelled same-context runs can continue from trusted completed
  upstream nodes.
- Successful same-context runs still skip as a whole workflow.
- Corrupt history, malformed node state, path containment failures, symlinks,
  hardlinks, and hash mismatches force safe rerun behavior instead of reuse.
- `--force` remains an escape hatch for full execution while preserving active
  same-context lock protection.

### Negative

- V1 resume is limited to the built-in filesystem artifact backend.
- Resume within an unfinished node is limited to completed review-loop phases.
  It does not support provider replay, ranking source runs by partial progress,
  or restoring arbitrary changes to project files.
- It does not automatically terminate a provider that outlives Crewplane. The
  process guard prevents a same-context replacement from starting after stale
  lock recovery, but a hard kill can still occur between child creation and
  durable process publication.
- `--dry-run` decisions remain advisory because it must not acquire locks,
  allocate runs, write artifacts, create fingerprint keys, or recover stale
  owners.

## Updates

- Updates ADR 0008 and ADR 0009 so current-layout per-run `run.json` state is
  the only supported duplicate/resume history source.
- **2026-06-12**: ADR 0016 workspace implementation preserves existing
  duplicate skip/resume behavior for disabled workspace mode and extends
  workspace-enabled skip/resume validation to `workspace-state.json`
  descriptors and exported bundles. Blob-only input nodes, snapshot provider
  workspaces, and mutable worktree provider workspaces can execute in
  fresh runs. Resume hydration copies ordinary node-boundary artifacts plus
  workspace state/bundle descriptors into the new run layout; it does not reuse
  old live workspace directories or cached refs as truth.
- **2026-07-11**: Clarified that descriptor-named workspace lineage may include
  the selected canonical review output and review-loop status without allowing
  arbitrary stage hydration.
- **2026-08-07**: Added durable built-in CLI child-process records. Stale-lock
  recovery now fails closed while a recorded provider process or process group
  is active, or its identity cannot be verified, without changing node-boundary
  resume semantics.
- **2026-09-22**: `repeat_force_run_count` applies existing `--force` semantics
  to every pass, including the first, bypassing duplicate skip and resume
  hydration. Each pass must finish terminalization, observer shutdown, and lock
  release before the next starts. A failed or cancelled pass stops repetition;
  artifacts and recovery remain per run, with no persistent sequence progress.

## Review-Loop Checkpoints

**Added 2026-10-02.**

The initial scope required a failed review loop to restart even when most of its
work had finished. Repeating completed provider calls adds cost and can repeat
file changes.

Allow sequential review loops to resume between completed phases. An executor
phase produces or revises the work; a reviewer phase evaluates it. A checkpoint
records the loop's progress, the files it needs, and the next phase to run.

Apply these safety rules:

- Save progress at phase boundaries. If a phase is interrupted, run that whole
  phase again. Keep saved reviews and attempt counts so resume does not reset
  review limits.
- Verify required files before publishing a checkpoint. Replace its record in
  one operation so readers cannot see a partly written record.
- Copy only verified files into a fresh run, using one source run. Reuse a
  checkpoint only when its upstream nodes also have reusable successful results.
- If review policy or findings validation ends the loop in failure, close its
  checkpoint. This blocks older checkpoints from bypassing that decision.
- Keep saved workspace records separate from temporary checkout cleanup. When
  providers work directly in the project, require project files to match the
  saved state before continuing.
- Start downstream nodes only after the resumed node finishes successfully.
  A `finalize` checkpoint can retry saving results and cleaning up without
  calling providers again.

The artifact layer verifies and copies files. The runtime restores loop progress
and chooses the next phase.

### Tradeoffs

Completed phases can survive an interrupted run and the removal of temporary
workspaces. Incomplete phases may repeat provider calls and their file changes.

Checkpoints require extra storage and validation. Missing or changed files
prevent reuse. If restoring a selected checkpoint fails, the new run fails
instead of silently starting provider calls from scratch.

### Alternatives Considered

- Restart every unfinished node: simpler, but repeats completed review work.
- Resume individual provider calls: requires tracking partial work within a
  phase. Complete phases give a clear point to restart.
- Restore from review status or entire run directories: status reports lack
  some execution state, and run directories can contain incomplete or unrelated
  files. Explicit checkpoint records identify the work that is safe to reuse.
