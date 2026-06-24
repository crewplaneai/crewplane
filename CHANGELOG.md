# Changelog

All notable user-facing changes are recorded here.

## [Unreleased]

- No unreleased changes yet.

## [0.1.0-alpha.2] - 2026-06-24

### Added

- Added a mock-first quickstart path: new projects now validate and run without
  provider CLIs, API keys, provider accounts, or config edits.
- Added `single-agent-review.task.md` as the default first-run workflow and
  moved the advanced code-review workflow into the example template library.
- Added setup checklist and reproducible support bundle documentation.

### Changed

- Generated config now enables the deterministic `mock` invoker by default and
  keeps real-provider examples commented until users opt in.
- Relative `{{file:path}}` tokens now resolve from the project root, including
  tokens authored in imported Markdown workflows.
- Provider setup diagnostics now point users to the provider setup guide.
- Provider log files are created before invocation-started telemetry so live
  observability can show a resolvable log path immediately.
- Compact log presentation now expands decoded multiline provider JSON fields
  into display lines.
- DAG graph rendering now preserves fan-in connector continuity across empty
  columns.

### Fixed

- Fresh `crewplane init && crewplane validate && crewplane run --no-live` can
  complete through the built-in mock invoker without external provider
  commands.

## [0.1.0-alpha.1] - 2026-06-22

### Added

- Reserved package-facing installation surfaces for the public `crewplane`
  distribution name.
- Added local release validation targets for Python builds, install smokes,
  `install.sh`, npm packaging, and Homebrew formula checks.
- Added `install.sh` for macOS and WSL/Ubuntu-style Linux installs through
  `uv tool install crewplane`.
- Added alpha npm wrapper package metadata for `npm install -g crewplane@alpha`.
- Added Homebrew formula source for the future `crewplaneai/crewplane` tap.

### Changed

- Python package metadata now publishes as `crewplane` version
  `0.1.0-alpha.1`; Python artifacts normalize this to `0.1.0a1`.

### Known Limitations

- Native Windows is not supported outside WSL.
- Provider CLIs and credentials are installed and managed separately.
- `crewplane` does not sandbox provider CLI execution.
