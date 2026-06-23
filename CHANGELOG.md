# Changelog

All notable user-facing changes are recorded here.

## [Unreleased]

- No unreleased changes yet.

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
