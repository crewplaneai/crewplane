---
schema_version: "__SCHEMA_VERSION__"
name: Workspace Inherited Worktree Example
description: Use one logical worktree for a sequential implementation source line.
worktrees:
  implementation_worktree:
    kind: worktree
    create_branch: true
nodes:
  - id: workspace.implement
    mode: sequential
    providers: [codex]
  - id: workspace.test_and_fix
    mode: sequential
    needs: [workspace.implement]
    providers: [codex]
  - id: workspace.handoff
    mode: sequential
    needs: [workspace.test_and_fix]
    worktree: none
    providers: [claude]
---

## workspace.implement

Implement the requested change in the inherited implementation worktree.

Return changed files, validation commands, and any follow-up risk.

## workspace.test_and_fix

Continue from the same implementation worktree, run focused validation, and fix
any failures.

Return the commands run and the current implementation candidate.

## workspace.handoff

Create a project-root handoff summary from:
{{workspace.test_and_fix.output}}

Include the final changed-file list, validation status, and follow-up tasks.
