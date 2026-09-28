# Demonstration Change Request: Normalize Line Spacing

This is a standalone coding exercise for trying a workflow. Replace this brief
with your own change request before using the workflow for real project work.

Add a small `normalize_spacing(text)` function and focused tests under
`examples/text-cleanup/`. Use the project's existing language and test tools;
use its normal test location if the test runner requires it. Keep the exercise
separate from application behavior and add no runtime dependencies.

The function returns a new string with these rules:

- Within each line, collapse each run of ASCII spaces and tabs to one ASCII
  space, and remove ASCII spaces and tabs at the start and end of the line.
- Preserve every newline delimiter exactly, including LF (`\n`), CRLF (`\r\n`),
  and CR (`\r`). Preserve blank lines and a final newline when present.
- Preserve every other character, including non-ASCII whitespace.
- Return an empty string for empty input or a single line containing only
  ASCII spaces and tabs. A whitespace-only line with a newline becomes an empty
  line with that same newline.

Use these acceptance examples; the escapes denote characters in string values:

| Input | Expected output |
| --- | --- |
| `"  alpha\t beta  "` | `"alpha beta"` |
| `"alpha  beta\n gamma\t\tdelta\n"` | `"alpha beta\ngamma delta\n"` |
| `"  alpha\r\n\tbeta\r gamma  "` | `"alpha\r\nbeta\rgamma"` |
| `" \t\n\t "` | `"\n"` |
| `""` | `""` |
| `" \t "` | `""` |
| `"alpha\u00a0beta"` | `"alpha\u00a0beta"` |

Run focused tests for these cases and report the commands and results. Explain
the function's behavior and how to run the tests in a short example README.
Keep unrelated application code, dependencies, and configuration unchanged.
