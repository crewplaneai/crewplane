# AGENTS.md

Canonical repository instructions for coding agents.

Use this file for repo-wide agent behavior. Use [DEVELOPMENT.md](./DEVELOPMENT.md) for broader engineering context and maintenance notes. More specific `AGENTS.md` files in subdirectories override this file for their subtree.

## Repo Facts

`crewplane` is a Python 3.13+ Typer CLI for running multi-agent workflows defined in Markdown. The core architectural rule is blackboard-style orchestration: providers do not coordinate through shared in-memory state; they communicate through artifacts written under `.crewplane/`.

Crewplane supports Linux, macOS, and WSL. Native Windows is not supported.

When changing behavior, preserve these properties:

- CLI-first provider integration. The project is built around external AI CLIs, not vendor SDKs.
- Auditable execution. Inputs, intermediate outputs, manifests, and results stay on disk as readable files.
- Explicit boundaries. Config loading, workflow parsing/composition, adapter resolution, provider invocation, runtime execution, artifacts, and observability are separated on purpose.
- Deterministic validation. New behavior should be covered by filesystem-local pytest tests, and mock-driven end-to-end checks should stay available.

## Task References

Consult documentation relevant to the task:

- [README.md](README.md): product orientation
- [DEVELOPMENT.md](DEVELOPMENT.md): setup, validation, release, and maintenance procedures
- [Module and test map](DEVELOPMENT.md#module-and-test-map): implementation entry points and affected tests
- [Public documentation](docs/index.md): documented user behavior
- [Architecture](docs/architecture/index.md): boundaries, lifecycle contracts, and ADRs

## Coding Standards

- Keep modules cohesive and boundaries explicit.
- Keep provider-specific invocation transport inside invoker adapters or adapter-owned invoker capability modules. Runtime execution should consume provider-agnostic invoker contracts and should not infer provider behavior from executable names, CLI flags, output formats, quota text, or usage text.
- Do not introduce hidden cross-node state. Downstream behavior should continue to derive from workflow definitions, config, and artifact files.
- Do not weaken template path restrictions casually. `{{file:path}}` is intentionally bounded to the project root unless explicitly allowlisted through `allowed_template_paths`.
- Prefer deleting dead code over leaving compatibility shims, stale branches, or commented-out removals.
- Keep comments rare and high-signal. Explain why a choice exists, not what the code already states.
- Prefer simple, local designs over premature abstraction. Add indirection only when the boundary is already real in the architecture.
- Use explicit type hints for public APIs and non-trivial logic.
- Validate inputs at system boundaries: CLI, config, workflow files, template resolution, and adapter interfaces.
- Fail explicitly on invalid state. Do not swallow exceptions or silently continue when correctness is at risk.
- Keep functions focused and readable. Make illegal states hard to represent.
- Do not use same-name import aliases such as
  `from module import Symbol as Symbol`. In facade modules, use grouped imports
  with an explicit `__all__`. In mixed modules where adding `__all__` would
  narrow existing wildcard behavior, import the owning module privately and
  bind the re-exported symbol explicitly.
- New behavior requires deterministic tests. Bug fixes require regression coverage.
- Avoid hidden fallback behavior unless it is deliberate, documented, and covered by tests.

## Change Guidance

### CLI surface

When CLI changes affect documented behavior or examples, update the affected documentation or templates in the same change.

### Workflow schema, parsing, and composition

Important invariants:

- Workflow schema version must match `src/crewplane/version.py`
- Markdown workflows require one `## <node-id>` section per frontmatter node
- Imports are Markdown-only, alias-namespaced, and must stay within `Path.cwd()`
- `{{param:key}}` is composition-time only; unbound params are rewritten to `{{var:key}}`
- `{{node.output}}` references should only be valid for upstream dependencies

### Config and provider invocation

Keep retry, quota, command-building, prompt transport, output parsing, and usage parsing behavior explicit. Provider-specific rules belong behind the invoker adapter boundary or in shared invoker capability modules owned by that boundary. If you add a provider-specific parsing rule or retry condition, add regression coverage for both positive and failure paths.

### Runtime execution

Preserve DAG semantics, manifest dedupe behavior, `--force` override behavior, and the distinction between node concurrency and per-invocation concurrency.

### Adapters and architecture boundaries

For a new integration or adapter change:

1. Update the relevant port contract or adapter implementation.
2. Register the alias in `registry.py` unless a dotted-path-only integration is intentional.
3. Ensure `loader.py` and `bootstrap/container.py` still wire the adapter correctly.
4. Add or update adapter behavior tests and architecture wiring tests.

Port contracts should remain stable extension boundaries. Avoid importing concrete runtime execution or observability implementations into `architecture/ports/`; move shared DTOs or protocol data into architecture-level or core-neutral modules when a port needs them.

### Artifacts, manifests, and templates

The implementation uses hyphenated output directories:

- `.crewplane/execution-stages/`
- `.crewplane/execution-results/`

Keep new docs and code aligned to those paths.

### Observability and tmux UI

The live UI must degrade cleanly:

- `--no-live` should leave execution fully functional
- missing tmux should not break runs
- tmux live mode depends on artifact log capture being enabled

## Validation Expectations

When setup is needed, run `make setup` from the repository root.

For code changes, run focused tests during development and `make check` before
handoff. Run additional [repository-automation checks](DEVELOPMENT.md#repository-automation)
or [package checks](DEVELOPMENT.md#release-workflow) when the change affects
those areas. For documentation-only changes, verify affected links, paths,
commands, and examples. The [command reference](DEVELOPMENT.md#local-workflows)
identifies checks and commands that modify files.

Review the final diff for correctness and unintended changes. Report the checks
performed, their results, and anything left unverified.

Use the [mock invoker validation flow](DEVELOPMENT.md#mock-invoker-local-validation)
for local orchestration checks. Confirm artifact contents and, when affected,
manifest dedupe behavior against the intended `workflow_signature` rules.

## Documentation Expectations

For documentation accompanying code changes, update only material made inaccurate
or materially incomplete by the change. Bug fixes that restore documented behavior
normally need regression coverage and a concise changelog entry, without additions
to guides or architecture documents.

Update architecture documentation when the described boundaries, responsibilities,
durable contracts, or lifecycle guarantees change. Fixing an implementation to
uphold an existing guarantee does not itself require an architecture update.

Keep each explanation in its canonical location. Avoid repeating implementation
details and regression cases across documentation.

Check for affected documentation when changing:

- CLI flags or defaults
- config keys or schema versions
- workflow composition/import behavior
- artifact directory layout
- built-in integration names or options
- generated example templates

Keep changelog entries concise and outcome-focused. Describe user- or
maintainer-visible effects instead of implementation mechanics, test details,
or commit inventories.

## Guidance Maintenance

- Keep instructions specific, consistent, and compact. Remove or rewrite outdated guidance instead of layering conflicting rules.
- Keep shared repository rules here. If guidance only applies to a narrower area, place it closer to that scope instead of expanding this file indefinitely.
- If instructions grow large, split them by scope so the most relevant guidance stays nearest to the work it controls.

See [Maintaining Agent Guidance](DEVELOPMENT.md#maintaining-agent-guidance) for how to verify instruction changes.
