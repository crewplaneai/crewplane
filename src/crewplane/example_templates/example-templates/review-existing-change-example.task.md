---
schema_version: "__SCHEMA_VERSION__"
name: Review Existing Change Example
description: Establish an existing patch's scope, review it before editing, fix confirmed issues, and verify the result.
# Use run --force to inspect the current patch again when the brief is unchanged.
inputs:
  change_brief: change.brief
nodes:
  - id: change.brief
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/change-review-brief.md}}"
  - id: change.scope
    mode: parallel
    needs: [change.brief]
    providers: [claude]
  - id: change.review
    mode: sequential
    needs: [change.scope]
    review_starts_with: reviewer
    depth: 2
    audit_rounds: 1
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
  - id: change.verify
    mode: parallel
    needs: [change.review]
    providers: [claude]
---

## change.scope

Open the complete change brief at `{{change.brief.output_path}}`. Establish the
requested comparison without changing files or Git state. Resolve its baseline
revision to an exact commit, inspect the current checkout, and identify the
selected changes within the allowed paths. Account for staged, unstaged, and
untracked files when the brief includes them. Record renamed and deleted paths.

Return the resolved baseline, current HEAD, selected file list, the change's
purpose and acceptance criteria, relevant validation commands, and any existing
changes outside the requested scope that must be preserved. The candidate is
the current working-tree content, including selected uncommitted changes; do
not switch revisions or create a replacement implementation.

If the baseline, scope, or intended behavior cannot be established, or no
matching change exists, report that blocker explicitly. Do not broaden the
review to the whole repository or treat the sample brief as proof of a patch.

## change.review

Open the change brief at `{{change.brief.output_path}}` and the scope report at
`{{change.scope.output_path}}`. Use the recorded baseline and selected files to
review the existing patch first. The initial review inspects current files;
later reviews assess the current candidate after any fixes. Preserve the
original scope and unrelated user work throughout.

<!-- crewplane:executor -->
Fix confirmed issues from the review within the selected change and add focused
regression coverage where needed. New test files are allowed only when directly
needed to cover an in-scope fix. Run the relevant checks and explain any finding
you reject. If no changes are needed, return a complete report without editing
files. If the scope remains unresolved, leave files unchanged and report the
blocker. Do not reset files, discard unrelated edits, stage or commit changes,
or rewrite Git history.

Return the complete current candidate report: selected files, fixes made,
rejected claims, commands and results, and unresolved concerns. Keep the
original patch distinct from your review fixes. For files created or changed
during this invocation, end with an exact `## Generated Files` section with one
project-relative path or Markdown link per line. Do not list unchanged files
from the original patch there. Report deleted files separately, and omit the
section if no files changed.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Find defects introduced by the selected change and gaps against its acceptance
criteria. Check correctness, regressions, and missing tests with concrete file
and behavior evidence. Keep unrelated pre-existing issues separate from
requested fixes. Unresolved scope or an unavailable candidate is a blocker,
not a clean review. On subsequent rounds, verify the fixes and check that they
preserve the intended change without expanding its scope.
<!-- /crewplane:reviewer -->

## change.verify

Open the scope report at `{{change.scope.output_path}}`, the acceptance criteria
at `{{change.brief.output_path}}`, and the complete reviewed result at
`{{change.review.output_path}}`. Inspect the selected final changes and rerun
the relevant checks against the current files. Keep source and tests unchanged;
normal temporary test outputs are allowed.

Return the final verification report: reviewed baseline and scope, original
patch versus review fixes, actual commands and results, reviewer approval or
unresolved objections, and follow-up work. Separate checks you performed from
results reported earlier. Cite original files by project path and captured
fixes by their saved links when available. A no-edit result does not mean the
original patch was captured as generated files. Workflow completion alone does
not establish approval or passing checks, and a failure found here does not
start another fix cycle automatically.
