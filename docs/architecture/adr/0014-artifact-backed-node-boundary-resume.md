# ADR 0014: Artifact-Backed Node-Boundary Resume

## Status
Accepted

## Date
2026-06-09

## Decision
Implement filesystem-backed same-context resume for failed or cancelled workflow
runs by reusing only validated successful node-boundary artifacts. Preserve ADR
0009 whole-workflow idempotency first: any valid same-context successful
`run.json` skips the workflow before failed or cancelled resume is considered.

Each execution attempt that proceeds receives a fresh `run_id` and fresh run
directories. Current run state is written under:

- `.crewplane/execution-stages/<run_key>/manifests/run.json`
- `.crewplane/execution-stages/<run_key>/manifests/nodes/`
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

Resume hydration copies only consolidated result artifacts and required findings
artifacts into the fresh results directory. Raw stage outputs, invocation logs,
review scratch state, review-loop status, symlinks, and hardlinks are never
hydrated.

## Rationale
Filesystem artifacts are the product's audit boundary. Reusing only validated
completed node boundaries preserves that boundary while avoiding hidden
cross-node state or provider-native replay. Fresh-run semantics keep resumed
runs auditable and leave failed or cancelled runs intact for postmortems.

## Design Tradeoffs
- Fresh-run resume preserves a complete audit trail for both the failed source
  run and the resumed run, but it duplicates consolidated artifacts and creates
  more directories than mutating the failed run in place.
- Node-boundary resume avoids provider-specific replay and hidden in-memory
  state, but any partially completed node must run again.
- Hydrating only consolidated results and required findings keeps downstream
  template lookups equivalent to a fresh upstream completion, but raw stage
  outputs, logs, review scratch state, and review-loop status remain only in the
  source run.
- Success-first duplicate detection keeps ADR 0009 whole-workflow idempotency
  authoritative, even when a newer failed or cancelled same-context run exists.
- Filesystem-only v1 allows strict local path, symlink, hardlink, and lock
  safety checks. Real runs with non-filesystem artifact backends fail before
  skip, resume, or full-run semantics are applied until those backends have an
  equivalent safety contract.
- Unsafe or ambiguous history fails closed. Corrupt manifests and node-state
  records are ignored for reuse, while unsafe filesystem metadata or live locks
  block takeover instead of risking reuse of untrusted artifacts.
- `--dry-run` reports advisory decisions without acquiring locks or recovering
  stale owners, so its answer can differ from a later real run.

## Rejected Alternatives
- Mutate the failed or cancelled run directory in place. Rejected because it
  would blur postmortem state, make terminal manifests harder to trust, and hide
  which artifacts came from the original attempt versus the resumed attempt.
- Add fine-grained successful-run caching. Rejected to preserve ADR 0009's
  coarse workflow-level idempotency and avoid a dependency-aware drift engine in
  this change.
- Hydrate raw stage directories, provider logs, review scratch state, or
  review-loop status. Rejected because those files are not the stable
  downstream contract and may contain provider-specific or partial execution
  state.
- Implement provider-native replay or intra-node resume. Rejected because it
  crosses the invoker adapter boundary and would require provider-specific
  semantics inside runtime scheduling.
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
- It does not support intra-node resume, provider replay, best-frontier scoring,
  or reconstruction of arbitrary workspace side effects.
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
