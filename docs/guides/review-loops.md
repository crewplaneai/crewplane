# Review Loops

A review loop uses a `sequential` node with providers assigned to the `executor`
and `reviewer` roles. Executors produce work for reviewers to check. That work
is called the candidate. Reviewers approve it or request changes, and executors
get a limited number of attempts to address their feedback.

Start with one executor and one reviewer, then add providers or fix attempts as
needed.

For separate structured issue artifacts, see
[Findings artifacts](findings.md).

## Smallest Review Loop

A review loop starts with one sequential node, one provider in the executor
role, and one provider in the reviewer role:

```yaml
---
schema_version: "1.0"
name: "Review Loop Example"
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
---

## implement
Implement the requested change and list the files changed.
```

This example uses the defaults:

- the provider in the executor role runs first
- providers in the reviewer role run after a candidate exists
- the same node prompt is used for both roles
- one blocked review can trigger one fix attempt by the provider in the
  executor role
- the loop ends when reviewers approve or the configured attempts are exhausted

![Vertical flow chart for the smallest review loop, starting with the node prompt, then the provider in the executor role, candidate output, Crewplane-built reviewer input bundle, provider in the reviewer role, and an approval decision that either selects the candidate or sends blocked feedback into the next executor prompt.](../images/review-loops/review-loop-flow.png)

A candidate is the output from the provider in the executor role that is
currently being reviewed. In the smallest loop, Crewplane first sends the node
prompt to the provider in the executor role. When that provider writes a
candidate, Crewplane sends the same node prompt to the provider in the reviewer
role, plus the current candidate and the required review format.

If the reviewer blocks the candidate, Crewplane carries the unresolved feedback
into the next fix prompt for the provider in the executor role. Review-loop
status records which executor output is selected as the result. Reviewer output
remains available as review evidence; the final result comes from the executor.

## Provider Order

Add more providers by keeping the same `sequential` review-loop shape. Put
every provider in the executor role first, then every provider in the reviewer
role:

```yaml
---
schema_version: "1.0"
name: "Provider Order Example"
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: gemini
        role: executor
      - provider: claude
        role: reviewer
      - provider: copilot
        role: reviewer
---

## implement
Implement the requested change and list the files changed.
```

**List every executor before any reviewer.** Crewplane runs the executors in
order, then runs the reviewers in parallel. Reviewers are not paired with
individual executors: every reviewer checks the complete set of executor outputs.

![Provider role order diagram showing a sequential provider list split into executor and reviewer segments, the executor phase producing one candidate set, and every reviewer checking that same set instead of one-to-one executor-reviewer pairs.](../images/review-loops/review-loop-provider-roles.png)

In this example, `codex` and `gemini` are providers in the executor role.
`claude` and `copilot` are providers in the reviewer role. Each executor round
produces a candidate set. With one executor, that set has one output. With
multiple executors, the set contains one output from each executor in provider
order. Reviewers receive the same current candidate set.

If any executor output in a round is empty, missing, or rejected as an invalid
candidate, Crewplane skips reviewer calls for that round. It does not ask
reviewers to approve a partial candidate set. If reviewers block and another
fix attempt is available, unresolved feedback is carried into the next executor
round for all providers in the executor role.

Most review loops should start with one executor. Add more reviewers when the
same candidate needs independent checks. Add more executors only when multiple
outputs need to be reviewed together.

- **Multiple executors, one reviewer**: executors run in declaration order and
  produce one candidate set. One reviewer checks the whole set and must approve
  it.
  - **Use this sparingly**, when you want several executor outputs reviewed together,
    such as competing drafts, research passes, or complementary sections. It does
    not choose or merge a winner for you.

- **One executor, multiple reviewers**: one executor produces the candidate.
  Reviewers run in parallel against that same candidate, and every reviewer must
  approve.
  - **This is the usual code-review shape**. Use it when one implementation needs
    multiple independent checks, such as correctness, security, docs, or domain
    review.

- **Multiple executors and multiple reviewers**: executors produce one candidate
  set. Every reviewer checks the same full set, and every reviewer must approve.
  Blocking feedback from any reviewer goes to the next executor round for all
  executors.
  - **Use this when several outputs need several independent reviews.**
    For ordinary implementation review, prefer one executor with multiple
    reviewers.

> ⚠️ **Note:** Do not put a reviewer between providers in the executor role, and do not add another provider in the executor role after a reviewer.

A sequential node with one provider is a plain executor node. A parallel node
never accepts reviewers. If you later use `review_starts_with: reviewer`, keep
the provider list in the same executor-role then reviewer-role order.

## Prompt Roles

Unmarked Markdown is shared prompt content. Providers in the executor role
receive shared content plus `executor` blocks. Providers in the reviewer role
receive shared content plus `reviewer` blocks.

The smallest loop sends the same authored prompt to both roles. Add role blocks
when providers in the two roles need different instructions:

```markdown
## implement
Implement the requested change and keep the patch focused.

<!-- crewplane:executor -->
Make the smallest correct change and include validation steps.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Review for correctness, regressions, and missing tests.
End with the structured review verdict.
<!-- /crewplane:reviewer -->
```

Use `executor` and `reviewer` as role markers; unmarked text is shared.
Crewplane also adds the current executor outputs, unresolved feedback,
instructions to review without changing the candidate, and the required review
format to each reviewer prompt.

![Prompt role routing diagram showing the authored Markdown prompt on the left, with unmarked shared content sent to both roles, the executor block sent only to executor-role providers, and the reviewer block sent only to reviewer-role providers.](../images/review-loops/review-loop-prompt-roles.png)

## Reviewer Verdicts

Reviewers are asked to end with this structured review block:

```markdown
## Major Issues
None

## Minor Issues
None

## Nitpicks
None

---
VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS
```

`NO_FINDINGS` and `NITS_ONLY` approve the candidate set. `CHANGES_REQUESTED`
blocks it and sends feedback to the next executor fix attempt.

One reviewer and multiple reviewers use the same loop. The difference is the
review phase:

| Reviewer count | What happens | Approval rule |
| --- | --- | --- |
| One reviewer | One reviewer receives the reviewer prompt and current candidate set. | That reviewer must approve. |
| Multiple reviewers | Reviewers run in parallel against the same current candidate set. | Every reviewer must approve. |

Reviewers do not see each other's current-round feedback before responding.
`settings.max_parallel_invocations` can cap parallel reviewer calls.

If a reviewer returns text without a clear verdict, Crewplane saves it as
feedback and does not count it as approval. Crewplane can recognize some plain
text approvals or requests for changes, but the structured format above is the
reliable way to report a verdict.

## Add Fix Attempts With `depth`

Use `depth` when the executor should get more chances to fix blocked feedback
inside one review pass:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
    depth: 2
```

`depth` counts executor fix attempts after the first reviewed candidate. It does
not count the first executor candidate, reviewer calls, or fresh audit passes.

![Depth diagram based on the review-loop flow, showing blocked feedback feeding the next executor prompt and a dashed loop back to the executor role, with depth limiting how many fix attempts can run after the first candidate while reviewer calls do not count.](../images/review-loops/review-loop-depth.png)

For example, `depth: 2` allows this maximum shape inside one audit round:

```text
Local round 1: executor candidate, then reviewer(s)
Local round 2: if blocked, executor fix attempt 1, then reviewer(s)
Local round 3: if blocked, executor fix attempt 2, then reviewer(s)
```

If all reviewers approve before those attempts are used, the loop stops early.
Only unresolved major issues, minor issues, and unstructured reviewer feedback
are carried into the next executor prompt. Nitpicks stay in the run record
unless a reviewer writes them as major or minor concerns.

## Add Fresh Passes With `audit_rounds`

Use `audit_rounds` when reviewers should get a fresh pass over a candidate
that was approved only after fixes:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
    depth: 1
    audit_rounds: 2
```

Each audit round is a fresh review pass with its own `depth` budget. Later
rounds start from the latest valid executor candidate from the previous round.

![Audit rounds diagram showing the depth loop wrapped inside audit round containers, with a later audit round starting from the latest valid candidate and resetting the local depth budget.](../images/review-loops/review-loop-audit-rounds.png)

Use the controls for different reasons:

| Control | Use when | Default |
| --- | --- | --- |
| `depth` | The executor should get more fix attempts for blocked feedback. | `1` |
| `audit_rounds` | The whole review loop should repeat as a fresh pass. | `1` |

Start with `depth: 1` and `audit_rounds: 1`. Raise `depth` first when failures
are usually fixable. Raise `audit_rounds` when you want reviewers to inspect a
fixed candidate again without carrying over feedback from the prior pass.
`audit_rounds` must not exceed `settings.max_audit_rounds`.

## Start With Reviewers

The usual review loop starts with an executor candidate. Use
`review_starts_with: reviewer` when reviewers should inspect context that
already exists before this node writes its own executor output. Common inputs are
upstream outputs, findings artifacts, metadata references, or project files.

For example, use reviewer-first when there is **already something to inspect**:

- you manually changed code and want review before the fix step runs
- you want reviewers to inspect the current branch or selected project files
- a previous node produced findings, such as `{{test.audit.findings}}`

Reviewers can triage that context first and hand only unresolved feedback to the
executor.

```yaml
nodes:
  - id: review.fix
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
    review_starts_with: reviewer
```

What changes:

- It changes only the first phase of the review loop.
- It does not change provider order. Keep all executors first, then all
  reviewers.
- It does not replace the executor. The node still finishes with an executor
  output from this node as its final result.
- It does not consume `depth`. The first reviewer pass is recorded as round 0.

What reviewers can inspect:

Round 0 gives reviewers a chance to inspect the context you provide before this
node's executor produces a candidate. Include any of these in the prompt:

- shared prompt text
- reviewer-only prompt blocks
- upstream artifacts such as `{{node.output}}` and `{{node.findings}}`
- metadata references such as `{{node.output_sha256}}`
- project files included with `{{file:...}}`

What happens next:

After round 0, local round 1 runs the executor:

- If reviewers approve the existing context, the executor is told to preserve
  it while writing this node's output.
- If reviewers report major issues, minor issues, or unstructured feedback, that
  feedback is passed to the executor.
- Reviewers then inspect the executor candidate using the usual review format.

Be explicit about inputs. `needs` controls node ordering, but it does not tell
reviewers what to inspect. Reference the review context in shared prompt text or
reviewer-only prompt blocks:

```markdown
## review.fix
Use these inputs as review context:

- backend: {{backend.impl.output}}
- frontend: {{frontend.impl.output}}
- test findings: {{test.audit.findings}}
- backend digest: {{backend.impl.output_sha256}}
```

For project files, include each file with `{{file:...}}`:

```markdown
## review.local
Use these files as context for the initial review and any required fix:

{{file:src/foo.py}}
{{file:tests/test_foo.py}}
```

## Continue Or Fail On Exhaustion

A `CHANGES_REQUESTED` verdict asks for another fix attempt while attempts remain.

When attempts run out without approval, `continue_on_failure` and
`settings.sequential_consensus_on_exhaustion` determine whether the node fails or
the workflow continues with a valid candidate. `continue_on_failure` also applies
when a reviewer command fails. Failed dependencies still block later nodes.

Crewplane also stops a node with `no_progress` after two consecutive fix attempts
leave the work unchanged and the same feedback unresolved. After the first such
attempt, it warns the executor that one more unchanged attempt will stop the
node. This limit carries across audit rounds and resets when the work or review
feedback changes.

The global `sequential_consensus_on_exhaustion: continue` setting cannot override
this stop. Set `continue_on_failure: true` on the node only if you want the
workflow to continue with work that reviewers have not approved.

## What You Can Inspect

Review-loop runs write executor outputs, reviewer outputs, logs, and review
state under the node stage directory. Final results are selected from the
review-loop status file.

The important file is:

```text
<node-id>/review-state/review-loop-status.json
```

It records which executor output was selected, the reviewers' verdicts, why the
loop stopped, and whether the allowed attempts were used up. It also stores
enough information to check that the selected files have not changed. Crewplane
checks those files before choosing the final node result. Downstream
`{{node.output}}` points to that selected executor output.

Crewplane saves status between attempts and if a run fails or is cancelled. The
file distinguishes the latest attempted round from the last completed review,
so an interrupted attempt does not attach an older verdict to new work.

Files ending in `.candidate.json` are stored beside executor outputs. They record
how Crewplane compared the content to detect progress, or why it could not make
that comparison. If file changes cannot be checked reliably, reviewers run again.

Each provider's response remains separate until that provider finishes. While
providers are running, Crewplane protects the current candidate, reviewer results,
and other run data from unexpected changes. If protected data changes, Crewplane
fails the node. A completed audit with no valid reviewer results does not reuse
verdicts from an earlier audit.

## Workspace Notes

When Worktrees are enabled, reviewers inspect the executor's current files.
Reviewer runs do not update the file version used by later nodes; executor runs
do. A `kind: worktree` node can have only one executor, but review loops can still
have multiple reviewers.

For a reviewer-first run in a managed workspace, `{{file:...}}` reads a recorded
Git version. It uses this node's existing candidate if available, otherwise the
upstream node's files when the worktree takes its source from another node,
otherwise the project's starting files. Uncommitted manual changes are not
included.

## Next

Continue to [Findings Artifacts](findings.md) to create
structured issue handoffs for downstream workflow nodes.

Or return to the [Guides](../index.md#guided-tutorial-track).
