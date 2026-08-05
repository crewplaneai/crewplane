# Changelog

All notable user-facing changes are recorded here.

## [Unreleased]

### Changed

- Run summaries now report normalized Codex, Claude, Gemini, and Kilo token
  totals and report counts from durable terminal invocation events, separately
  from the visible-text lower-bound estimate.

### Fixed

- Prevented completion-buffered Gemini JSON invocations from being terminated
  by output-idle timeouts while preserving configured wall-clock limits.
- Kept complete event-log scans out of bounded observer shutdown so large
  successful runs are not reported as observability failures.

## [0.1.7] - 2026-08-02

### Changed

- Duplicate skip now validates required node and generated-file artifacts and
  can fall back to an older valid matching success.
- Adapter option scopes and observer capabilities are now explicit validated
  extension contracts.

### Fixed

- Redacted secrets across provider and workspace command fields before plans,
  metadata, diagnostics, and observability records are persisted.
- Rejected nonfinite configuration and JSON, unsafe or unbounded workspace
  snapshots, racy process-group discovery, and suppressed artifact durability
  failures.
- Prevented concurrent generated-file publication drift and unrelated
  project-root writes from being attributed to provider invocations.
- Excluded presentation-only and inactive review settings from semantic
  execution identity and made stream-tail retention incrementally bounded.

## [0.1.6] - 2026-07-29

### Changed

- Added one automatic retry for transient Codex model-capacity failures.

### Fixed

- Expected workflow failures now exit cleanly with concise diagnostics without
  masking unexpected runtime defects.
- Prevented concurrent reviewer-generated-file snapshots from causing false
  artifact-drift failures.
- Improved release reliability on macOS and during registry propagation.

## [0.1.5] - 2026-07-28

### Changed

- Streamlined pre-publish validation and consolidated the maintainer release
  workflow.

### Fixed

- Hardened release publishing against partial registry updates, transient
  verification failures, and credential exposure in errors.

## [0.1.4] - 2026-07-27

### Added

- Added optional workflow-provider `reasoning` requests for Codex and Claude
  when using the built-in CLI invoker. Crewplane passes the provider-native
  value through unchanged and records it in dry-run output, preflight plans,
  invocation logs and events, and duplicate-run and resume identity.
- Added preflight validation for reasoning token shape, unsupported invokers or
  provider kinds, and conflicting native reasoning controls in CLI arguments,
  inherited Claude environment state, and explicit Claude settings.
- Added optional `quota_retry_max_wait_seconds` and
  `quota_retry_max_attempts` agent settings to bound quota retries
  independently of ordinary `max_retries`.

### Changed

- Shared-project-root generated-file capture now requires providers to list
  files under `## Generated Files`; Crewplane captures only listed files that
  Git verifies changed during the invocation. Capture fails closed when no
  usable Git baseline is available; managed isolated workspaces retain
  workspace-owned change capture.
- Generated-file snapshot limits and copy failures no longer discard an
  otherwise successful provider result. Crewplane preserves accepted files,
  records bounded rejection metadata, emits capture and finalization warnings,
  copies accepted generated files into the result tree atomically while
  preserving file modes, and omits links for files that could not be
  published.
- Quota retry diagnostics now state that quota attempts are independent of
  `max_retries` and preserve the last distinct non-quota failure when a later
  quota guard or configured ceiling ends the invocation.

### Fixed

- Prevented failed generated-file capture from falling back to live
  project-root file detection during result consolidation.
- Provider failures mentioning `reasoning.effort` are now classified as
  model or configuration errors.

## [0.1.3] - 2026-07-26

### Added

- Added global `crewplane --update` and `crewplane -u` options that verify the
  active installation's owning package manager before delegating updates to
  `uv tool`, `pipx`, or Homebrew, then confirm the installed version in a fresh
  process.
- Added safe manual-update guidance for global npm wrappers, direct Python
  installs, editable or direct-source installs, and temporary or project-local
  package environments.

### Changed

- The install script now follows the latest available Crewplane release by
  default so later `uv tool upgrade crewplane` commands are not held to the
  originally installed version. `CREWPLANE_VERSION` remains available for an
  explicit version pin.
- Artifact location output now preserves long filesystem paths without
  inserting terminal-width line breaks.
- Expanded the npm and Homebrew package documentation with the mock-first
  quickstart, workflow overview, provider onboarding, update guidance, and
  package-specific troubleshooting.

### Fixed

- Fixed GitHub Release publication on macOS Bash when generated release notes
  do not have a predecessor tag.

## [0.1.2] - 2026-07-23

### Added

- Added global `crewplane --version` and `crewplane -v` options that print the installed package version and exit.
- Added repository contribution and support templates plus automated CI, security, dependency, TestPyPI, and release checks.

### Changed

- Strengthened workspace-aware resume hydration so reusable nodes restore only validated results, findings, generated files, and descriptor-declared lineage artifacts required by downstream worktree and review-loop execution.
- Dry-run resume advisories now list the exact dependency-closed node IDs selected for hydration.
- Strengthened cancellation and cleanup handling for workspace-backed provider runs.
- Refreshed README onboarding, quickstart, workflow, and artifact-layout guidance.
- Clarified supported platforms as Linux, macOS, and WSL; native Windows remains unsupported.
- Expanded same-context run-lock troubleshooting and stale-lock recovery guidance.

### Fixed

- Fixed workspace lineage source verification through result bundles.
- Fixed a Git maintenance race during lineage fetches.
- Hardened runtime-owned Git environment handling so trusted local lineage fetches are not blocked by inherited transport-policy settings.

## [0.1.1] - 2026-07-04

### Added

- Added `crewplane onboarding`, an interactive provider handoff that detects
  supported provider CLI names on `PATH`, lets users pick one provider, and
  updates unchanged generated config and workflow defaults for a real-provider
  first run.
- Added onboarding validation, provider setup links, and manual fallback
  snippets so users can recover or complete setup without hand-editing the
  generated YAML from scratch.

### Changed

- Refreshed the mock-first quickstart, provider setup, workflow, observability,
  artifact inspection, workspace isolation, and troubleshooting documentation
  around the new onboarding flow.
- Reorganized documentation images by topic so public guides reference assets
  from clearer locations.

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
