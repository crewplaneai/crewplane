# Sample Bug Report: Lost Line Boundaries

This is a hypothetical report for the `normalize_spacing(text)` demonstration
under `examples/text-cleanup/`. It is not an observed defect in your project.
Use an existing implementation of that exercise, or replace this report with a
real bug before running providers. The workflow must verify the claim before
changing code; a correct implementation needs no fix.

## Reported behavior

The suspected failure is that normalizing spaces and tabs also removes line
boundaries. A whole-string whitespace split and join could cause this, but that
is a hypothesis to check, not an established diagnosis.

| Input | Expected output | Hypothetical incorrect output |
| --- | --- | --- |
| `"alpha  beta\n gamma\t\tdelta\n"` | `"alpha beta\ngamma delta\n"` | `"alpha beta gamma delta"` |
| `" alpha\r\n\tbeta\r gamma "` | `"alpha\r\nbeta\rgamma"` | `"alpha beta gamma"` |

The escapes denote characters in string values. Locate the existing function
and use the project's test tools to record a runnable reproduction command and
its actual output. If the function is absent, report the missing prerequisite;
do not implement the feature as a bug fix.

## Required behavior and scope

- Collapse runs of ASCII spaces and tabs within each line to one ASCII space,
  and remove those characters at each line's ends.
- Preserve LF, CRLF, and CR delimiters exactly, including blank lines and a
  final newline. Preserve all other characters, including non-ASCII whitespace.
- Empty input and a single line of ASCII spaces or tabs produce an empty
  string. A whitespace-only line with a newline keeps that newline.
- Change only the demonstration implementation and directly related tests.
  Use the project's existing test location and tools; add no dependencies.
- For a confirmed defect, show a regression test exposing the original failure
  and passing after the correction. Preserve existing unrelated changes.

For a real report, replace these details with the affected paths, exact steps
and inputs, observed and expected results, and relevant environment details.
