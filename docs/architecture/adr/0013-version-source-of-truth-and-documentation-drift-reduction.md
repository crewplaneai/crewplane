# ADR 0013: Version Source of Truth and Documentation Drift Reduction

## Status
Accepted and implemented

## Date
2026-06-09

## Decision
Use `src/orchestrator_cli/version.py` as the canonical Python source for one authored schema version:

```python
SCHEMA_VERSION = "1.0"
```

`SCHEMA_VERSION` governs current config files, workflow files, and persisted preflight execution-plan artifacts. Keep the package distribution version separate in `pyproject.toml`; it identifies installable releases, not schema compatibility.

Do not maintain separate config, workflow, preflight, or integration API version constants. Adapter compatibility is governed by the package version, public port contracts, loader validation, tests, and normal Python dependency constraints.

Documentation should reference the schema version concept and source location instead of repeating concrete values. Keep concrete schema values only where users need copyable generated examples, or where tests intentionally verify rendered output.

## Context
The project had several versioned surfaces:

- package/distribution version in `pyproject.toml`
- config schema version for `.orchestrator/config.yml`
- workflow schema version for `.task.md` frontmatter and node semantics
- preflight plan schema version for persisted execution-plan artifacts
- integration API version for adapter metadata

The package version is still a separate release identifier. The other four constants created more maintenance surface than value for the current project. The config, workflow, and preflight formats are shipped and validated by the same package. The integration API version was not enforced by the adapter loader; it was only persisted in canonical integration payloads and runtime signatures.

The current implementation consolidates authored schema state into `SCHEMA_VERSION`, renders generated templates from that constant, serializes preflight execution plans with `plan_schema_version` populated from `SCHEMA_VERSION`, removes adapter version payloads, rejects stale persisted plan shapes explicitly, and documents the maintainer-facing policy in `DEVELOPMENT.md`.

## Goals
1. Provide one obvious Python source of truth for supported schema compatibility.
2. Remove redundant version constants and adapter API version metadata.
3. Reduce documentation drift by avoiding repeated concrete version values.
4. Keep generated templates and tests tied to `SCHEMA_VERSION` rather than literals.
5. Preserve the separate package release version in `pyproject.toml`.

## Non-Goals
1. Do not collapse schema compatibility into the package release version.
2. Do not add migration shims for old schemas.
3. Do not introduce a generated documentation pipeline.
4. Do not broaden adapter discovery or plugin packaging.
5. Do not define a separate external adapter API compatibility scheme before one is needed.

## Rationale
Package releases and schema compatibility change for different reasons. A package patch release may include bug fixes, documentation updates, or internal refactors without changing config, workflow, or preflight artifact compatibility. Tying schema compatibility to the package version would either force unnecessary user file churn or make the version field stop meaning schema compatibility.

At the same time, separate config, workflow, and preflight constants were too granular for the current compatibility policy. The CLI validates only the current supported schema, generated project files are created together, and no old-plan loader exists for persisted preflight artifacts. One schema constant gives maintainers one bump decision for incompatible schema changes.

The integration API version was also removed because it did not enforce compatibility. If external adapter packages later need a stable generation contract, that should be introduced with loader-level validation and a new ADR.

## Tradeoffs
### Positive
- Fewer version concepts for maintainers and users to understand.
- Fast package releases do not force schema churn.
- Adapter metadata no longer carries an unenforced compatibility field.
- Docs and templates have one schema source of truth.

### Negative
- A config-only breaking change still requires the shared schema version to move.
- Preflight artifact shape hardening can reject stale same-version alpha artifacts without a migration path.
- External adapter compatibility relies on package versioning and structural validation until a real adapter ecosystem exists.

## Consequences
- `src/orchestrator_cli/version.py` exports only `SCHEMA_VERSION`.
- `pyproject.toml` remains the package version source.
- Config validation, workflow validation, preflight plan serialization, and generated templates use `SCHEMA_VERSION`.
- Preflight execution plans continue to serialize `plan_schema_version`; the old preflight-specific schema constant is removed.
- Current preflight execution plans validate required shape markers and reject removed fields such as legacy runtime config schema fields, integration API version metadata, and legacy fingerprint version fields.
- `CanonicalIntegrationConfig` no longer includes adapter version metadata.
- Runtime config signatures no longer include adapter API version metadata.

## Rejected Alternatives
### Use `pyproject.toml` For Schema Compatibility
Rejected because package release version and schema compatibility are different concepts. A normal package release should not require config or workflow schema churn.

### Keep Separate Config, Workflow, And Preflight Schema Constants
Rejected because the project does not currently support independent compatibility windows for these formats. Separate constants added decision overhead without a matching compatibility benefit.

### Keep A Separate Integration API Version
Rejected because the loader did not enforce it. Adapter compatibility is currently better represented by package version, port contracts, structural loader checks, and tests.

### Reintroduce Adapter API Versioning Now With Enforcement
Rejected as premature. A separate adapter API version should be added only if external adapter packages become a real compatibility surface.
