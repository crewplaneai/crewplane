---
schema_version: "__SCHEMA_VERSION__"
name: Workspace Alternatives Example
description: Implement one explicit brief in separate worktrees and compare their reports in a snapshot.
# Prepare and commit docs/crewplane-change-request.md before running.
# The generated sample-inputs/feature-brief.md is a demonstration starting point.
inputs:
  change_request: change-request
worktrees:
  conservative_worktree:
    kind: worktree
    create_branch: true
  experimental_worktree:
    kind: worktree
  comparison_snapshot:
    kind: snapshot
nodes:
  - id: change-request
    mode: input
    source: "{{file:docs/crewplane-change-request.md}}"
  - id: alternatives.conservative
    mode: parallel
    needs: [change-request]
    worktree: conservative_worktree
    providers: [codex]
  - id: alternatives.experimental
    mode: parallel
    needs: [change-request]
    worktree: experimental_worktree
    providers: [codex]
  - id: alternatives.compare
    mode: parallel
    needs: [alternatives.conservative, alternatives.experimental]
    worktree: comparison_snapshot
    providers: [claude]
---

## alternatives.conservative

Open the change request at `{{change-request.output_path}}` and implement a
conservative solution that meets its acceptance criteria in this worktree.
Keep changes within the brief and use the project's existing tools.

Return changed files, validation commands and results, acceptance-criteria
coverage, and the main tradeoffs. If you create or modify files during this
invocation, end with an exact `## Generated Files` section containing one
Markdown link per line to a concrete path relative to the project or workspace
root, and no prose. Report deleted files separately.

## alternatives.experimental

Implement an experimental solution for the same change request, opened from
`{{change-request.output_path}}`. Meet the same acceptance criteria in this
independent worktree. Keep changes within the brief and use the project's
existing tools.

Return changed files, validation commands and results, acceptance-criteria
coverage, and the main tradeoffs. If you create or modify files during this
invocation, end with an exact `## Generated Files` section containing one
Markdown link per line to a concrete path relative to the project or workspace
root, and no prose. Report deleted files separately.

## alternatives.compare

Compare the two implementation reports against the shared change request and
recommend one path forward. Open:

- Change request: `{{change-request.output_path}}`
- Conservative report: `{{alternatives.conservative.output_path}}`
- Experimental report: `{{alternatives.experimental.output_path}}`

This is a report comparison in a snapshot of the original recorded project
source. The snapshot does not contain or combine the alternatives' edits, and
its local files are not either implementation candidate. Do not edit files or
claim to have tested either alternative in this snapshot.

Use captured result-tree file links from the reports when available to inspect
supporting evidence. Mark missing evidence and distinguish reported test results
from checks you performed. Return acceptance-criteria coverage, tradeoffs, a
recommendation, and any further validation needed. The recommendation does not
merge either source line or change which worktree has branch export enabled.
