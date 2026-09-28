---
schema_version: "__SCHEMA_VERSION__"
name: Workspace Inherited Worktree Example
description: Use one logical worktree for a sequential implementation source line.
# Prepare and commit docs/crewplane-change-request.md before running.
# The generated sample-inputs/feature-brief.md is a demonstration starting point.
inputs:
  change_request: change-request
worktrees:
  implementation_worktree:
    kind: worktree
    create_branch: true
nodes:
  - id: change-request
    mode: input
    source: "{{file:docs/crewplane-change-request.md}}"
  - id: workspace.implement
    mode: parallel
    needs: [change-request]
    providers: [codex]
  - id: workspace.test_and_fix
    mode: parallel
    needs: [workspace.implement]
    providers: [codex]
  - id: workspace.handoff
    mode: parallel
    needs: [workspace.test_and_fix]
    worktree: none
    providers: [claude]
---

## workspace.implement

Open the change request at `{{change-request.output_path}}` and implement its
acceptance criteria in the inherited implementation worktree. Keep changes
within the brief and use the project's existing tools.

Return the complete changed-file list, validation commands and results,
acceptance-criteria coverage, and follow-up risks. If you create or modify files
during this invocation, end with an exact `## Generated Files` section
containing one Markdown link per line to a concrete path relative to the
project or workspace root, and no prose. Report deleted files separately.

## workspace.test_and_fix

Open the change request at `{{change-request.output_path}}` and the implementation
report at `{{workspace.implement.output_path}}`. Continue from the verified
implementation source on the same logical worktree, run focused validation, and
fix confirmed failures within the brief.

Return a cumulative changed-file list covering both implementation nodes,
acceptance-criteria coverage, and validation commands and results. Distinguish
checks you ran from results reported by the earlier node. Record unresolved
risks. If you create or modify files during this invocation, end with an exact
`## Generated Files` section containing one Markdown link per line to a concrete
path relative to the project or workspace root, and no prose. Include only this
invocation's changes in that section. Report deleted files separately.

## workspace.handoff

Create a project-root handoff without editing files. Open:

- Change request: `{{change-request.output_path}}`
- Implementation report: `{{workspace.implement.output_path}}`
- Validation and fixes report: `{{workspace.test_and_fix.output_path}}`

Include the cumulative changed-file list, acceptance-criteria coverage,
validation status, unresolved risks, and follow-up tasks. Follow captured
result-tree links when file evidence is needed. The project root does not
contain the managed worktree's edits, so do not use its local source as the
final candidate or claim fresh candidate validation from this handoff.
