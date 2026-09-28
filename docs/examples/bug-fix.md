# Fix a Reported Bug

Use [bug-fix-example.task.md](../../src/crewplane/example_templates/example-templates/bug-fix-example.task.md)
when you have an existing implementation and a reported failure to investigate.
The workflow separates reproduction and diagnosis from editing:

```text
Bug report → reproduce → diagnose → fix and review → verify
```

The reproduction and diagnosis steps inspect the project and run relevant
checks without editing source files. The fix step changes code only when the
evidence supports a defect, and a reviewer checks that fix. The final step runs
verification commands and reports the outcome without making further edits.

## Prepare the Report

After `crewplane init`, edit
`.crewplane/workflows/example-templates/sample-inputs/bug-report.md` or change the
workflow's input path to your own report. Include:

- The affected code and the files the workflow may change.
- Expected behavior and the observed failure.
- Reproduction steps, example inputs, and relevant test commands.

The generated report is a **hypothetical suspicion** that the existing
`normalize_spacing(text)` demonstration loses newlines. It is not evidence of
a defect in your project. Create the demonstration through the feature example
first, or replace the report with a real issue in existing code.

If the implementation is missing or the reported failure cannot be confirmed,
the workflow reports that limitation. It must not invent a failure, implement
missing application code, or edit source just to produce a patch. A report that
no fix is supported by the evidence is a useful result.

## Run the Recipe

Enable the `claude` and `codex` agent profiles in `.crewplane/config.yml`. Keep
the invoker set to `mock` for a trial without provider calls. To investigate and
fix real code, prepare those providers and switch to `cli`; see
[Provider setup](../getting-started/provider-setup.md#turn-mock-mode-onoff).
The recipe runs in the project root; review your current changes before running
it against real files.

To require reviewer approval, set
`settings.sequential_consensus_on_exhaustion: fatal` in your config and leave
`continue_on_failure` unset or false on `bug.fix`. The default exhaustion policy
is `continue`. See [review policy](../guides/review-loops.md#continue-or-fail-on-exhaustion).

```bash
crewplane validate .crewplane/workflows/example-templates/bug-fix-example.task.md
crewplane run --force --tasks .crewplane/workflows/example-templates/bug-fix-example.task.md
```

Use `--force` each time you want to inspect the current code again. Changes to
source files mentioned only in the report do not automatically trigger a fresh
run; the same report can otherwise reuse a saved result.

Read the final verification report under
`.crewplane/execution-results/<run-key>/` for the reproduced behavior, any fix,
reviewer approval or objections, and actual check results. Mock output does not
prove the bug exists or that tests passed. With real providers, workflow
completion alone also does not prove every requested check passed; inspect
failures, blocked checks, and missing evidence in the report.
Problems found by final verification need follow-up; they do not start another
fix cycle automatically.

Return to the [examples catalog](index.md) to choose another recipe.
