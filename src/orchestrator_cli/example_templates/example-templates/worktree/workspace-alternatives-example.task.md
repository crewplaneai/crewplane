---
schema_version: "__SCHEMA_VERSION__"
name: Experimental Workspace Alternatives Example
description: Explore separate Experimental implementation worktrees and compare them in a snapshot.
worktrees:
  conservative_worktree:
    kind: worktree
    create_branch: true
  experimental_worktree:
    kind: worktree
  comparison_snapshot:
    kind: snapshot
nodes:
  - id: alternatives.conservative
    mode: sequential
    worktree: conservative_worktree
    providers: [codex]
  - id: alternatives.experimental
    mode: sequential
    worktree: experimental_worktree
    providers: [codex]
  - id: alternatives.compare
    mode: sequential
    needs: [alternatives.conservative, alternatives.experimental]
    worktree: comparison_snapshot
    providers: [claude]
---

## alternatives.conservative

Implement a conservative solution for the project change request.

Return changed files, tests run, and the main tradeoffs.

## alternatives.experimental

Implement an experimental solution for the same project change request.

Return changed files, tests run, and the main tradeoffs.

## alternatives.compare

Compare the two implementation reports and recommend one path forward.

Use artifact metadata instead of inlining large outputs:
- Conservative: {{alternatives.conservative.output_path}}
- Experimental: {{alternatives.experimental.output_path}}

Write the recommendation as this node's normal output.
