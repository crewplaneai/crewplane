# Changelog

All notable user-facing changes are recorded here.

## [Unreleased]

## [0.1.0] - 2026-06-25

### Added

- Reserved package-facing installation surfaces for the public `crewplane`
  distribution name.
- Added local release validation targets for Python builds, install smokes,
  `install.sh`, npm packaging, and Homebrew formula checks.
- Added `install.sh` for macOS and WSL/Ubuntu-style Linux installs through
  `uv tool install crewplane`.
- Added npm wrapper package metadata for `npm install -g crewplane`.
- Added Homebrew formula source for the future `crewplaneai/crewplane` tap.
- Added a mock-first quickstart path: new projects now validate and run without
  provider CLIs, API keys, provider accounts, or config edits.
- Added `single-agent-review.task.md` as the default first-run workflow and
  moved the advanced code-review workflow into the example template library.
- Added setup checklist and reproducible support bundle documentation.
- Added a state-aware release tool behind the existing Make targets, including
  `release-prepare`, completed-release verification, partial publish recovery,
  npm `latest` reconciliation, release manifests, and post-publish install
  checks.
- Added `crewplane init` guidance for switching from the mock quickstart to real
  provider CLI workflows.

### Changed

- Python package metadata now publishes as `crewplane` version `0.1.0`.
- Generated config now enables the deterministic `mock` invoker by default and
  keeps real-provider examples commented until users opt in.
- Generated config and setup docs now make the mock-to-CLI switch explicit:
  replace mock invoker options with `options: {}` when using the built-in `cli`
  invoker.
- Generated real-provider examples now use `gpt-5.5` for Codex and
  `claude-sonnet-4.6` for Copilot.
- Relative `{{file:path}}` tokens now resolve from the project root, including
  tokens authored in imported Markdown workflows.
- Provider setup diagnostics now point users to the provider setup guide.
- Provider log files are created before invocation-started telemetry so live
  observability can show a resolvable log path immediately.
- Compact log presentation now expands decoded multiline provider JSON fields
  into display lines.
- DAG graph rendering now preserves fan-in connector continuity across empty
  columns.
- Release Make targets now delegate packaging, smoke, publish, and verification
  behavior to the Python release tool.
- Release metadata synchronization now updates npm package metadata, install
  documentation, `uv.lock`, and Homebrew formula resource pins from the current
  project version.
- Public npm install examples now use the default `crewplane` package instead
  of the alpha dist-tag.
- Codex JSON log presentation now preserves multiline command execution output
  as separate display lines.
- Consolidated result and findings Markdown now uses human-readable section
  headings while preserving provider task IDs in stage artifacts, logs,
  manifests, and review-loop state.

### Fixed

- Fresh `crewplane init && crewplane validate && crewplane run --no-live` can
  complete through the built-in mock invoker without external provider
  commands.
- CLI invoker option validation now points users at the required `options: {}`
  config when stale mock options are left behind.
- Sequential review-loop remediation now stops cleanly after provider session
  context exhaustion when a previous valid candidate exists, discarding any
  recovered executor workspace lineage and continuing with that candidate.
- Fatal artifact drift is now reported as the primary error even when the
  provider invocation also failed.
- Resume locks can now load owner files that lack process start identity
  metadata.

### Known Limitations

- Native Windows is not supported outside WSL.
- Provider CLIs and credentials are installed and managed separately.
- `crewplane` does not sandbox provider CLI execution.
