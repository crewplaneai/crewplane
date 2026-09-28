# Review an Existing Change

Use [review-existing-change-example.task.md](../../src/crewplane/example_templates/example-templates/review-existing-change-example.task.md)
to review code you have already changed and fix confirmed issues within that
scope. The workflow starts with review before asking the executor to edit:

```text
Review brief → record baseline and files → review and fix → verify
```

The scope step records the exact baseline commit and selected file set without
editing code. The review loop inspects those changes first; the executor then
addresses confirmed findings or preserves approved work and writes its report.
The final step runs verification commands and reports the result without further
source edits.

## Prepare the Change and Scope

After `crewplane init`, edit
`.crewplane/workflows/example-templates/sample-inputs/change-review-brief.md` or
change the workflow's input path to your own brief. State the baseline, intended
behavior, files to review, allowed fixes, and checks to run.

The sample uses the existing `normalize_spacing(text)` demonstration. It compares
the current staged, unstaged, and untracked changes with `HEAD`, limited to the
demonstration paths named in the brief. The scope step resolves `HEAD` to an
exact commit and records the selected files, so later steps use the same review
boundary. For real work, replace the sample with your actual change and scope.
If your change is already committed, choose the earlier comparison commit in
the brief instead of `HEAD`.

This recipe uses the normal project root in a Git repository. Keep managed
workspace isolation disabled for this run: the changes being reviewed are
already in your working tree. **A clean working tree is not required.** Avoid
making concurrent edits to the selected files while the workflow runs. Unrelated
files and changes remain outside the requested review.

If the demonstration is missing or there are no selected changes, the workflow
reports that gap instead of creating an implementation or inventing review
findings.

## Run the Recipe

Enable the `claude` and `codex` agent profiles in `.crewplane/config.yml`. Keep
the invoker set to `mock` to inspect the workflow without provider calls. Switch
to `cli` only when those providers are ready to inspect and fix your change;
see [Provider setup](../getting-started/provider-setup.md#turn-mock-mode-onoff).

To require reviewer approval, set
`settings.sequential_consensus_on_exhaustion: fatal` in your config and leave
`continue_on_failure` unset or false on `change.review`. The default exhaustion
policy is `continue`. See
[review policy](../guides/review-loops.md#continue-or-fail-on-exhaustion).

```bash
crewplane validate .crewplane/workflows/example-templates/review-existing-change-example.task.md
crewplane run --force --tasks .crewplane/workflows/example-templates/review-existing-change-example.task.md
```

Use `--force` each time you want to review the current patch again. Changes to
source files, the patch, or `HEAD` mentioned only in the brief do not
automatically trigger a fresh run; an unchanged brief can otherwise reuse a
saved result.

Inspect the saved scope, reviewer verdicts, fixes, and final verification report
under `.crewplane/execution-results/<run-key>/`. Check what ran and what remained
blocked; a successful workflow is not proof that every test passed. Review your
working-tree diff before accepting the changes. Mock responses demonstrate the
workflow only; they do not assess your patch.
Problems found by final verification need follow-up; they do not start another
fix cycle automatically.

Return to the [examples catalog](index.md) to choose another recipe.
