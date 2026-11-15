# Project Memory

Project memory recalls accepted lessons during preflight and can propose new
lessons after a run. It is optional, project-local, and separate from any memory
feature in your provider CLI. Curated project guidance remains authoritative.
Recalled lessons provide historical evidence to verify before using them.

## Enable It Deliberately

Run these commands from the project root. First initialize the store:

```bash
crewplane memory init
```

This creates `.crewplane/memory/store.json` and its persistent `store.lock`
without overwriting existing records. No other command initializes memory.
The generated starter workflow remains memory-disabled and provider-free.

Learning sends retained task and output evidence to the configured provider **before**
human review. Choose [recall without learning](#recall-without-learning) if you
do not want this extraction. Task files and outputs can contain sensitive data,
even though environment secrets are excluded from retrieval.

Add a policy to `.crewplane/config.yml`. `codex` below must already be an agent
configured for your chosen invoker; replace it with your configured agent ID.
There is no automatic extractor selection or fallback.

```yaml
settings:
  memory:
    learn: true
    extractor: codex
    max_records: 5
    max_chars: 8000
    extraction_timeout_seconds: 120
```

Then add `memory: true` to your workflow's frontmatter. You can instead enable
individual provider nodes. The
[project-memory example](../../src/crewplane/example_templates/example-templates/project-memory-example.task.md)
shows both inherited recall and a node-level opt-out. After `crewplane init`,
its copy is under `.crewplane/workflows/example-templates/`.

Validate and preview before running:

```bash
crewplane validate .crewplane/workflows/example-templates/project-memory-example.task.md
crewplane run --dry-run --tasks .crewplane/workflows/example-templates/project-memory-example.task.md
crewplane run --no-live --tasks .crewplane/workflows/example-templates/project-memory-example.task.md
```

Initialize memory and configure the example's `codex` and `claude` agents first.
Validation and dry-run read memory but allocate no run artifacts, extract no
lessons, and open no review. Normal provider-validation behavior still applies.
A missing, unsafe, corrupt, oversized, or unsupported store fails opted-in
preflight rather than silently disabling recall.

### Recall Without Learning

Set `settings.memory.learn: false` to use accepted lessons without post-run
extraction or review. This mode still requires an initialized store but does
not require an extractor. A workflow with no enabled provider nodes does not
read the store or require an extractor, regardless of this setting.

### Choose Which Nodes Use Memory

Set `memory: false` on a provider node to opt it out, even when its workflow
enables memory. See the [workflow syntax reference](../reference/workflow-syntax.md#memory)
for inheritance and valid declarations.

An enabled node receives recall and allows its direct retained outputs to become
learning evidence. An opt-out is not transitive information filtering: another
enabled node may receive and repeat its output. It also does not disable a
provider CLI's own memory or filesystem access.

## Inspect What Recall Uses

```bash
crewplane memory list
crewplane memory list --pending
crewplane memory show RECORD_ID
crewplane memory search 'protected file reads'
```

Replace `RECORD_ID` with an ID from `list`. These commands do not call a provider.
The `show` command displays each full revision and its evidence, along with the
conditions for revision decisions and a digest of the record's current state.
It shows saved excerpts and reports whether their originals are unchanged,
changed, missing, or unsafe to read. Archiving an original leaves its accepted,
saved evidence in the store.

For each scheduled executor or reviewer, recall searches using the workflow
prose, node and source-path names, captured nonsensitive variables and static
files, and captured direct input-node `output` references. Available initial
workspace content can contribute even when execution uses a workspace path.
Recall does not reopen source files.

Excluded inputs include all environment values, secret handles, injected memory,
provider-generated outputs (including retained terminal results), runtime
candidate files, and unavailable collection-item data. Artifact path/size/hash
references do not cause those artifacts to be opened for retrieval. A task whose
meaning exists only in runtime input may therefore have no useful static match.
`foreach` instances reuse their child template's selection; there is no per-item
retrieval.

Recall matches words and identifiers rather than meaning. It does not filter
lessons by source version, so check whether a match still applies.

Recall includes only accepted revisions. By default each role receives at most
five complete records and 8,000 rendered characters, including framing, titles,
provenance, qualifications, and separators. Oversized records are skipped, not
truncated; later matches may still fit. No match or no fit produces no block.
Recalled lessons are treated as text, not workflow syntax.

For an executed opted-in run, inspect:

```text
.crewplane/execution-stages/<run-key>/preflight/execution-plan.json
.crewplane/execution-stages/<run-key>/preflight/memory-selection.json
```

The selection receipt is present even when nothing is selected. Treat both files
as potentially sensitive task records. See the [artifact reference](../reference/artifacts.md#project-memory-records)
for their contents.

### Reuse and Active Runs

One snapshot feeds the entire compilation, including child templates. Changes to selected lessons affect `workflow_signature`: acceptance, correction, or
forgetting can change which prior work is reusable. Unrelated proposals,
rejections, whole-store transaction IDs, timestamps, and inspection-only
explanations do not change the signature. Learning-only settings are excluded;
existing adapter-registration identity rules still apply.

`--force` compiles current memory before bypassing history. Resume also compiles
current memory and requires matching identity and verified artifacts. Frozen
child selections are preserved when reconstructing generated work. A mismatched
plan or memory receipt is an error, not a reason to repair it from today's store.
Changes to the store do not alter already compiled or active runs.

## Review Proposed Lessons

With learning enabled, eligible runs visibly capture evidence and start a
separate extraction workflow after the parent result has been committed,
required cleanup has completed, its execution lock has been released, and its
result has been displayed. Extraction uses the configured agent and adds its
own provider cost and usage records. It is not a hidden parent DAG node.

Only new usable retained evidence from enabled nodes qualifies. Duplicate skips,
setup-only failures, and entirely reused work do not learn. A failed parent may
still qualify through earlier completed work. Parent cancellation or unconfirmed
cleanup skips extraction and review. For non-cancelled executed runs, Crewplane
attempts to retain these completion facts in `manifests/completion.json`,
including in recall-only mode. Failure to retain the receipt skips automatic
learning without changing the parent outcome.

The extractor proposes zero to five lessons. No useful lesson is a valid result.
Valid proposals are imported as **pending**, never automatically accepted. In
an interactive terminal, inspect the cards and choose actions:

- `inspect N` shows the complete revision, qualifications, evidence, and any
  correction diff.
- `edit N` creates a new candidate revision against the currently inspected
  accepted revision, preserves its retained evidence, and returns to preview;
  the edited revision is not selected automatically.
- `accept N` or `reject N` displays the exact revision and asks for confirmation.
- `defer N` clears that selection; unchecked means deferred, not rejected.
- `apply` submits selected decisions as one atomic batch; `quit` defers them.

Nothing is selected by default. A stale revision, digest, or accepted predecessor
rejects the whole batch without losing unrelated updates. Inspect current state
again before deciding. No human prompt, editor, or provider call holds the store
lock. EOF or dismissal applies no unsubmitted decisions.

Non-TTY input or output leaves candidates pending and prints a review command:

```bash
crewplane memory review
crewplane memory review --run PARENT_RUN_ID
```

Use the parent run ID printed by Crewplane, not its full artifact key, for this
review filter. Review never invokes extraction. With `repeat_force_run_count`,
eligible passes may generate candidates, but review occurs once after the
sequence, not between passes. Independently accepted external changes can still
affect the next pass's normal fresh compilation. `--no-live`, missing tmux, and
disabled raw provider logs do not disable learning.

### Evidence Is Not Verification

An output saying “tests passed” records an agent's claim. A framework outcome
records invocation status; a provider-process receipt records a process exit.
Neither proves that tests passed. Captured check content supports only the
checks it actually records; there is no general automatic test-result verifier.
Human acceptance does not upgrade the evidence category.

Evidence packets retain verified source identity, bounded excerpts and excerpt
hashes, dates, and available outcome/review/invocation/workspace provenance.
Host-owned parent outcome limitations remain attached to imported evidence, even
if the model omits them or original run artifacts are later archived. Partial
failures, unresolved reviews, and reused work remain qualified. Missing
provenance is unknown, not approval. Capture does not scan arbitrary history,
raw transcripts, or the mutable checkout.

## Propose, Correct, or Forget Manually

`crewplane memory propose` prompts for a pending human-authored lesson in an
interactive terminal. For scripted input, save a strict JSON candidate document
and use `crewplane memory propose --file lesson.json`:

```json
{
  "schema_version": "1.0",
  "lessons": [
    {
      "title": "Preserve protected file reads",
      "body": "Keep the existing symlink and containment checks when changing file-token resolution.",
      "tags": ["file-token"],
      "qualifications": ["Recheck against the current file-access contract."],
      "evidence_ids": []
    }
  ]
}
```

Manual imports cannot assign host evidence or acceptance metadata. A correction
adds both `corrects_record` and `expected_accepted_revision` using inspected IDs.
Pending corrections leave current recall unchanged. Accepting one supersedes its
predecessor atomically; each record has at most one recallable revision. Other
pending corrections against the old predecessor stay non-retrievable. Before
rejecting one, inspect the record again and pass its **current** accepted revision
as `--expected-accepted`; do not copy the stale candidate's old predecessor.
Acceptance still requires both that candidate's original predecessor and the
current pointer to agree. Edit and reinspect a stale correction before considering
its acceptance.

For exact scripted `accept`, `reject`, and `forget` syntax, see the
[command reference](../reference/commands.md#crewplane-memory). They require
inspected revision/digest preconditions. Do not guess those values or retry a
stale approval blindly. Reimporting the same proposal returns its existing
identity, including after rejection. Forgetting removes the complete record,
revisions, retained evidence, and proposal identities from the store. Reimport
after forgetting creates a fresh pending candidate requiring acceptance again.

## Retry Extraction and Handle Failures

```bash
crewplane memory extract --run PARENT_RUN_ID_OR_KEY
crewplane memory extract --run PARENT_RUN_ID_OR_KEY --regenerate
```

Extraction accepts an unambiguous completed parent run ID or its exact artifact
key, with matching retained completion evidence proving cleanup and release.
An older run without that receipt, or one with unconfirmed completion facts,
cannot be used; a terminal manifest or missing lock is not sufficient proof.
Retry reuses the saved packet and any retained canonical result. A malformed
but successfully produced result is revalidated and reported without another
model call. Only `--regenerate` explicitly requests a new attempt. A failed
attempt without usable canonical output follows the normal execution path.

The extraction deadline defaults to 120 seconds across model work and retry/quota
waits. Expiry cancels that work and awaits required cleanup, so return can take
longer than the nominal deadline. Human review has no acceptance deadline.
Ordinary extraction timeout, provider failure, malformed candidates, or import
failure is reported separately and does not change the parent outcome. An
unconfirmed extraction cleanup remains a fatal command-level error while the
parent manifest stays unchanged.

See the [artifact reference](../reference/artifacts.md#project-memory-records)
for extraction records and storage limits. If required evidence cannot fit,
extraction is skipped with an explanation; state exceeding its bounds is rejected,
not silently trimmed.

Only initialization creates the persistent lock. An interrupted lock-only
initialization can be finished with `memory init`; an existing store missing its
lock is an integrity error. Do not remove or recreate the lock to bypass it.
If publication reports an uncertain outcome, inspect the reported transaction
and current state before retrying: atomic replacement may already have occurred.

## Security and Scope

Acceptance is a workflow control, not a security boundary. Historical framing
does not neutralize prompt injection. A provider running as your OS user may
access writable files.

The mutable store is excluded from source capture and `{{file:...}}` access,
including aliases and allowlisted attempts. This is not provider sandboxing.
Forgetting stops future recall but does not erase old plans, prompts, logs,
backups, or active invocations. Stores are project-local; there is no sharing,
automatic guidance rewriting, semantic merging, or provider-native synchronization.
Locking is supported on Linux, macOS, and WSL; this feature does not add native
Windows support. No productivity or usefulness benefit is assumed.
