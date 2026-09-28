# Sample Review Request: An Existing Text-Cleanup Change

This brief assumes you have already added or changed the
`normalize_spacing(text)` demonstration under `examples/text-cleanup/`. It does
not establish that a patch exists. Replace it with your own change description
and paths for real work. Keep a copy of the original patch in your normal
version-control process before letting a provider fix it.

## Comparison and allowed changes

- Baseline: Git `HEAD`, resolved to its exact commit before review.
- Candidate: the current working tree, including staged, unstaged, and untracked
  demonstration files. Review the combined final content, not just the index.
- Scope: `examples/text-cleanup/`. If related tests live elsewhere, add their
  exact paths here before running. Keep all other existing changes untouched.
- Purpose: add or improve `normalize_spacing(text)` using the project's
  existing language and tools, without changing application behavior or adding
  dependencies.

If the implementation is already committed, replace `HEAD` with the appropriate
earlier baseline commit. The workflow reviews the current checkout against that
baseline; it does not check out another revision. If no selected change exists,
report that fact instead of creating a new implementation.

## Acceptance criteria

- Within each line, collapse runs of ASCII spaces and tabs to one ASCII space
  and remove those characters at the start and end.
- Preserve LF, CRLF, and CR delimiters exactly, including blank lines and a
  final newline. Preserve other characters, including non-ASCII whitespace.
- Empty input and a line of only ASCII spaces or tabs return an empty string.
  A whitespace-only line with a newline keeps that newline.
- Tests cover these behaviors, and a short example README explains how to run
  them using the project's existing tools.

For example, `"alpha  beta\n gamma\t\tdelta\n"` must become
`"alpha beta\ngamma delta\n"`, and `"alpha\u00a0beta"` must stay unchanged.
The escapes denote characters in string values.

Review the actual patch and tests before requesting fixes. Correct only
confirmed in-scope issues, add focused regression tests when needed, and report
the commands and results. Do not stage or commit changes, discard unrelated
edits, or rewrite Git history.
