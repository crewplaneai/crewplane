# Security Policy

## Supported Versions

The public alpha supports the current `0.1.x` line only.

## Reporting Vulnerabilities

Report vulnerabilities through GitHub Security Advisories for `https://github.com/crewplaneai/crewplane` .

## Provider CLI Boundary

`crewplane` orchestrates external provider CLIs. It does not install provider CLIs and does not manage provider credentials. Provider installation, authentication, credential storage, network access, and command approval modes belong to those provider tools.

Generated provider profiles may include unattended approval flags for tools such
as Claude, Codex, Gemini, Copilot, or Kilo. Review those flags before running
against sensitive repositories.

`crewplane` does not sandbox provider CLI execution. Run workflows in a
workspace and operating-system account appropriate for the trust level of the
provider CLIs you configure.
