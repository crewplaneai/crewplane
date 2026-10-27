# AGENTS.md

Guidance for creating, changing, and reviewing tests, fixtures, helpers, and test
automation. Follow the shared rules in the [root AGENTS.md](../AGENTS.md).
Use [DEVELOPMENT.md](../DEVELOPMENT.md#testing-expectations) for validation
procedures and the [module and test map](../DEVELOPMENT.md#module-and-test-map)
for placement.

## Test design and selection

- Preserve shared test execution and installed-package acceptance across local,
  CI, nightly, and release workflows. Express intentional differences through
  profiles or options and reuse shared assertions. Keep operational instructions
  in [DEVELOPMENT.md](../DEVELOPMENT.md#test-suite-contract).
- Assert observable behavior and contracts. Regression tests should distinguish
  the faulty behavior from the expected outcome. Avoid assertions tied to
  implementation text or incidental call sequences.
- Keep each test's setup, action, and expected result clear. Exercise real
  filesystem, process, or installation boundaries when those are the contract
  under test; use deterministic doubles for external providers.
- Select test profiles through discovery and registered markers. Keep selection
  independent of filenames and directory moves; avoid hardcoded test inventories.
  Mark individual qualifying tests and follow the documented
  [race eligibility criteria](../DEVELOPMENT.md#testing-expectations).

## Fixtures and helpers

- Keep short parser inputs and static subprocess snippets inline when that keeps
  the test readable. Use source files for longer or more complex programs. Do not
  modify program behavior through source-text replacement.
- Make fixture bytes deterministic when asserting bytes, sizes, hashes, or
  signatures. Use `Path.write_bytes(...)` or
  `Path.write_text(..., encoding="utf-8", newline="\n")`; default text writes
  can translate LF to CRLF on Windows, even when the string contains `\n`.
- Use `read_bytes()` for exact byte assertions; `read_text()` can normalize
  newlines and hide differences. When newline preservation is the contract,
  cover LF and CRLF inputs explicitly and assert the original bytes. Fix fixture
  generation rather than normalizing production output or weakening assertions.
- Put program files and reusable input files in [fixtures/](fixtures/).
  Pass scenario variations through arguments, environment variables, or data
  files. Keep fixture programs intended for child interpreters outside ordinary
  pytest discovery.
- Keep scenario-specific setup near its tests. Use [helpers/](helpers/) for
  cohesive support shared across test areas. Prefer explicit inputs and
  focused helpers over configurable fixture frameworks or hidden shared state.
- Use the narrowest fixture scope that preserves isolation. Keep fixture code
  subject to the repository's formatting and lint checks.

## Isolation and resource ownership

- Use temporary directories and the existing Git, environment, and platform
  helpers. Restore process-wide state and keep tests independent of execution
  order and parallel workers.
- Establish concurrency interleavings with events, barriers, or readiness
  receipts and bounded waits. Timing alone must not establish correctness.
- Make ownership of processes, tasks, locks, and files explicit. Ensure cleanup
  also runs after failures, timeouts, and cancellation; terminate and reap owned
  child processes and descendants within bounded deadlines.
- Use existing capability checks for platform prerequisites. Preserve explicit
  skips for optional capabilities and failures where the selected profile
  requires them.

## Refactoring and review

- Preserve each test's purpose and meaningful assertions, including failure
  paths, artifact contents, resource cleanup, and subprocess coverage. Passing
  counts alone do not demonstrate equivalent coverage.
- Preserve deliberate interpreter, environment, and working-directory
  isolation in subprocess, nested pytest, and installed-package checks.
- When changing a helper, fixture, or launcher, inspect all callers and affected
  command expectations. Verify the affected scenarios using the
  [repository validation requirements](../AGENTS.md#validation-expectations).
