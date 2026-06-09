# Orchestrator CLI

Run Claude, Codex, Gemini, and other AI coding assistants on different parts of your codebase — at the same time — using simple Markdown files.

> *"The orchestrator doesn't try to understand your AI tools. It just gives them a shared workspace and gets out of the way."*

## Why Orchestrator CLI?

You already have AI coding tools you love. But they can't talk to each other. Claude doesn't know what Codex just wrote. Gemini can't review Claude's output.

**Orchestrator CLI connects them.** Define tasks in a Markdown file, assign each to an AI provider, and the orchestrator runs them — in parallel when possible, in sequence when needed. Each tool reads and writes to a shared folder, so downstream tasks can build on upstream results.

No SDKs. No plugins. No vendor lock-in. If your AI tool has a CLI, it works.

**Every step is a Markdown file.** Inputs, outputs, reviews — all saved to `.orchestrator/execution-stages/` as readable Markdown. Inspect any step in your editor, diff it in git, or debug with `cat`. Nothing is a black box.

| vs. Other Frameworks | Orchestrator CLI |
|---------------------|------------------|
| **Hidden state** | Externalized to markdown — fully auditable |
| **Tight SDK coupling** | CLI-first — zero provider lock-in |
| **Black box debugging** | Inspect `.orchestrator/execution-stages/` at any step |
| **Adapter boilerplate** | Works with any CLI that reads/writes files |

> ⚠️ **Security Note:** `{{file:path}}` templates are restricted to the project root by default. Use `settings.integrations.artifacts.options.allowed_template_paths` for explicit external-file allowlisting.

## Features

- 🔄 **DAG Execution** – Run independent nodes in parallel and dependency nodes in sequence
- 🔍 **Cross-Review** – Agents review each other's outputs with structured verdict detection
- 📝 **Task Files** – Frontmatter+Markdown (`.task.md`) workflows by default
- 🔌 **Pluggable Providers** – Works with any CLI-based AI tool; no API keys or auth managed by the orchestrator
- 📁 **Project-Local Config** – Each project gets its own `.orchestrator/` directory
- 📂 **Transparent Artifacts** – Every intermediate step and final result saved to disk for full auditability
- 📊 **Spend Observability** – Run logs capture CLI capture status, provider token-report status, visible lower-bound estimates, and configured cost confidence summaries
- ⚡ **Smart Caching** – Workflow-signature idempotency skips identical successful workflow runs automatically

> For a deeper look at the architecture behind these features, see [Feature Overview](docs/features.md).

## Prerequisites

Before using the orchestrator, you must have at least one AI CLI tool installed and authenticated:

| Provider | Installation | Authentication |
|----------|-------------|----------------|
| **Claude** | [claude.ai/cli](https://claude.ai) | `claude login` |
| **Codex** | `npm install -g @openai/codex` | Set `OPENAI_API_KEY` |
| **Gemini** | [ai.google.dev/cli](https://ai.google.dev) | `gemini auth login` |
| **GitHub Copilot** | `npm install -g @github/copilot` | `copilot login` or set `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` |

Verify your providers are working:
```bash
which claude codex gemini copilot  # Check they're in PATH
claude --version                    # Verify Claude
copilot version                     # Verify Copilot CLI is installed
```

## Installation

```bash
pip install orchestrator-cli
```

Or install from a local checkout:
```bash
cd orchestrator_cli
pip install .
```

## Update

Upgrade an existing install:

```bash
python -m pip install --upgrade orchestrator-cli
```

If you are running from a local clone:

```bash
git pull
python -m pip install -e '.[dev]'
```

## Run the CLI

```bash
orchestrator --help
orchestrator init
orchestrator run --dry-run
orchestrator run
orchestrator run --tasks .orchestrator/workflows/my_task.task.md
```

## Running Specific Workflows

By default, `orchestrator run` looks for a **single** `.task.md` file in `.orchestrator/workflows/`.

If you have multiple workflow files, you must specify which one to run using the `--tasks` (or `-t`) flag:

```bash
# Run a specific workflow
orchestrator run --tasks .orchestrator/workflows/deploy.task.md

# Short form
orchestrator run -t .orchestrator/workflows/deploy.task.md
```

## Quick Start

### 1. Initialize

```bash
cd /path/to/your/project
orchestrator init
```

This creates:
- `.orchestrator/config.yml`
- `.orchestrator/workflows/code-review-example.task.md` (default runnable workflow)
- `.orchestrator/workflows/example-templates/*.task.md` (additional runnable templates)
- `.orchestrator/workflows/example-templates/sample-inputs/*.md` (copyable sample input files used by example workflows)

Schema version values in generated files are rendered from `src/orchestrator_cli/version.py`.

### 2. Configure Providers

Edit `.orchestrator/config.yml`:
```yaml
version: "<schema-version>"

agents:
  # Optional: omit default_model to let the provider CLI choose its configured default.
  claude:
    cli_cmd: ["claude"]
    provider_kind: "claude"
    prompt_transport: "stdin"
    default_model: "sonnet"
    # Optional configured pricing per million tokens:
    # pricing:
    #   input: 3.0
    #   output: 15.0
    #   cached_input: 0.3
    #   cache_write: 3.75
    # Default 30-minute hard wall-clock kill; set null only under an external supervisor.
    invocation_timeout_seconds: 1800
    invocation_idle_timeout_seconds: 1800
    extra_args:
      - "--bare"
      - "--dangerously-skip-permissions"
    quota_reset_sleep_floor_seconds: 5
    quota_reached_on_contains:
      - "usage limit reached"
      - "rate limit reached"
      - "quota reached"
      - "too many requests"
    quota_reached_retry_delay_seconds: 300

  codex:
    cli_cmd: ["codex", "exec"]
    provider_kind: "codex"
    prompt_transport: "stdin"
    prompt_transport_arg: "-"
    default_model: "gpt-5.4"
    # pricing:
    #   input: 1.5
    #   cached_input: 0.375
    #   output: 6.0
    # Default 30-minute hard wall-clock kill; set null only under an external supervisor.
    invocation_timeout_seconds: 1800
    invocation_idle_timeout_seconds: 1800
    extra_args:
      - "--skip-git-repo-check"
      - "--dangerously-bypass-approvals-and-sandbox"
    quota_reset_sleep_floor_seconds: 5
    quota_reached_on_contains:
      - "usage limit exceeded"
      - "rate limit reached"
      - "too many requests"
    quota_reached_retry_delay_seconds: 300

  gemini:
    cli_cmd: ["gemini"]
    provider_kind: "gemini"
    prompt_transport: "stdin"
    default_model: "auto"
    # pricing:
    #   input: 0.35
    #   output: 1.05
    # Default 30-minute hard wall-clock kill; set null only under an external supervisor.
    invocation_timeout_seconds: 1800
    invocation_idle_timeout_seconds: 1800
    extra_args:
      - "--approval-mode=yolo"
    quota_reset_sleep_floor_seconds: 5
    quota_reached_on_contains:
      - "exhausted your capacity"
      - "resource exhausted"
      - "quota will reset after"
    quota_reached_retry_delay_seconds: 300

settings:
  default_workspace: ".orchestrator/workspaces"
  log_level: "info"
  sequential_consensus_on_exhaustion: "continue" # continue | fatal
  max_audit_rounds: 5
  token_budget:
    warn_threshold_chars: 50000
    fail_threshold_chars: null # optional fail-fast guard for artifact and review-loop context injections
  integrations:
    invoker:
      implementation: "cli"
      options: {}
    ui:
      implementation: "tmux"
      options:
        auto_close_session: true
        quiet_after_seconds: 120.0
        # log_tail_lines: 40 # optional fixed cap; omit or set null to fit the right pane height
    artifacts:
      implementation: "filesystem"
      options:
        log_cli_output: true
        allowed_template_paths: []
```

The generated provider profiles are intentionally optimized for unattended
workflow execution. They include provider-specific approval bypass flags such as
Claude `--dangerously-skip-permissions`, Codex
`--dangerously-bypass-approvals-and-sandbox`, Gemini `--approval-mode=yolo`,
and Copilot `--allow-all-tools` so a DAG node does not block forever waiting
for an interactive confirmation prompt. Agent invocations also have finite
per-attempt `invocation_timeout_seconds` and
`invocation_idle_timeout_seconds` guards so a provider CLI cannot freeze the
workflow indefinitely by running forever or by going quiet. The generated
default keeps a 30-minute hard wall-clock attempt timeout and a 30-minute idle
timeout. Set `invocation_timeout_seconds: null` only when an external supervisor
supplies the elapsed-time cap.

Treat those flags as a trust boundary decision. Orchestrator CLI coordinates
provider CLIs and writes auditable artifacts; it does not sandbox provider tool
execution or make broad provider permissions safe. Use the generated defaults
only in repositories, disposable worktrees, containers, VMs, or CI runners where
you are comfortable letting the configured provider CLIs read, edit, and execute
within their own permission model. If you want provider-native approval prompts
or narrower tool access, remove those `extra_args` or replace them with the
provider's stricter allow/deny options before running real workflows.

Spend observability v2 records four signals for every terminal invocation in `events.ndjson` and `summary.md`: whether the CLI output was captured successfully, whether the provider emitted a full/partial/malformed token report, a visible-text token estimate that is always labeled **lower-bound**, and any configured cost estimate with explicit confidence. Codex and Claude run in structured-output mode so output extraction and provider-usage parsing stay separate; Copilot and Kilo remain visible-estimate-only until they expose documented machine-readable usage payloads.

Batch 2 adds prompt-shape safeguards. Sequential reviewer prompts now lead with stable, review-only instructions so reviewers inspect and report without being told to make changes. Multi-provider sequential nodes prefer a structured review block with `Major Issues`, `Minor Issues`, and `Nitpicks` sections plus a terminal verdict of `CHANGES_REQUESTED`, `NITS_ONLY`, or `NO_FINDINGS`. The runtime normalizes common format drift locally, preserves the raw reviewer text in sidecar artifacts, and only fails the node for real invocation/runtime failures or configured consensus exhaustion. `{{node.output}}`, `{{node.findings}}`, and review-loop previous-candidate context injections can also be guarded by `settings.token_budget` and optional per-node `token_budget` overrides: warn mode records a runtime warning and still injects the full artifact/context, while fail mode aborts before provider invocation. No truncation or hidden summarization is introduced in this batch.

Batch 3 adds first-class findings artifacts. A node can opt in with `findings: true`, include one `<!-- findings --> ... <!-- /findings -->` block in each final executor output, and expose that concise artifact downstream through `{{node.findings}}`. In mixed executor/reviewer sequential nodes, reviewer outputs do not participate in findings extraction. The full consolidated result still exists on disk alongside the concise findings artifact.

Batch 4 strengthens multi-provider sequential review loops without changing the CLI-first transport contract. Review loops now separate two controls explicitly: `depth` is the number of remediation fix/verify cycles allowed after a fresh audit finds issues, while `audit_rounds` is the number of fresh audit passes allowed overall. Reviewer state and inbox artifacts stay on disk, later audit rounds restart fresh without carrying unresolved state from exhausted earlier audits, and reviewers run in parallel inside each local review step. The runtime now also treats each current-round executor response as the canonical candidate for that local review step, skips reviewer calls for clearly invalid or unchanged remediation candidates, records artifact drift warnings, and persists final review-loop status under `review-state/review-loop-status.json`. Node-local artifact drift is always monitored, and shared run-root artifacts are only attributed when the invocation was isolated enough to avoid false positives from other concurrently running nodes. When `audit_rounds > 1`, review artifacts are grouped under per-audit directories while logs stay at the node root with audit-aware filenames.

For Gemini CLI, prefer `default_model: "auto"` unless you intentionally need a specific model. Pinning preview-only model IDs bypasses Gemini's normal routing and fallback behavior.

Model resolution precedence is workflow provider `model` -> agent `default_model` -> provider CLI default.

If `default_model` is omitted and no workflow provider `model` is set, orchestrator does not pass a model flag and the provider CLI uses its own configured or built-in default.

For GitHub Copilot CLI, use the standalone `copilot` binary in programmatic mode. The generic CLI invoker already supports Copilot directly:

```yaml
  copilot:
    cli_cmd: ["copilot"]
    provider_kind: "copilot"
    prompt_transport: "stdin"
    # Choose a model shown by `copilot /model`; availability varies by plan.
    default_model: "claude-sonnet-4.5"
    # Visible-estimate-only in spend observability v2.
    # pricing:
    #   total: 2.0
    extra_args:
      - "--silent"
      - "--no-ask-user"
      - "--allow-all-tools"
      # Replace broad access with precise allow/deny rules when needed:
      # - "--allow-tool=write,shell(git:*)"
      # - "--deny-tool=shell(git push)"
    quota_reset_sleep_floor_seconds: 5
    quota_reached_on_contains:
      - "rate limit reached"
      - "quota reached"
      - "too many requests"
    quota_reached_retry_delay_seconds: 300
```

Because orchestrator runs provider CLIs non-interactively, Copilot profiles should either grant the exact tools they need with `--allow-tool` / `--deny-tool`, or use `--yolo` only in a trusted environment. Copilot also reads repository instruction files such as `AGENTS.md`, `.github/copilot-instructions.md`, and `.github/instructions/**/*.instructions.md`, so keep those files current when you want Copilot-specific behavior.

Kilo follows the same spend-observability limitation as Copilot in this release: output capture still works normally, but token reporting stays visible-estimate-only unless the CLI grows a documented machine-readable usage contract.

For the tmux live dashboard:
- `quiet_after_seconds` controls when a still-running invocation is treated as "quiet" and the right pane adds liveness messaging like "waiting for new output."
- `log_tail_lines` is optional. Omit it or set it to `null` to fit the compact dashboard right pane height automatically, or set an integer to enforce a fixed cap.
- The default right pane is a compact summary/tail view for the selected node.
- Press `Enter` to switch the right pane into a scrollable raw log inspector for the selected node's current invocation log. That inspector stays locked to the opened log until you leave it.
- Press `Esc` in inspect mode to return to the compact dashboard view. Scrolling there uses tmux history/copy-mode semantics rather than terminal scrollback.
- Press `q` to cancel the running workflow, close the dashboard, and return control to the terminal.

For deterministic local runs without provider CLI calls, switch the invoker to `mock`:

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        delay_seconds: 0.25
        observation_delay_seconds: 5
        output_mode: "lorem" # lorem | echo | file
        seed: 42
        # output_dir: ".orchestrator/mock-outputs" # required for output_mode=file
        # strict_file_mode: false
        fail_when:
          - node_id: "summary.final"
            role: "reviewer"
            audit_round_num: 1
            round_num: 2
```

When `output_mode: "lorem"` is used, non-reviewer findings-enabled nodes automatically receive one deterministic findings block in the synthetic output so `{{node.findings}}` workflows still run locally. `echo` mode is exact for non-reviewer invocations, and fixture-backed `file` output is always passed through unchanged. Reviewer invocations in `echo`, `lorem`, and missing-fixture fallback paths emit a deterministic no-findings review contract so review loops can complete locally.

`observation_delay_seconds` adds a mock-only pause after each invocation starts. Leave it at the default `5` seconds when you want the live dashboard to linger in running and quiet states during local demos, or set it to `0` in tests and fast dry runs.

For fixture-driven review-loop tests in `output_mode: "file"`, the mock invoker checks audit-round-aware fixtures first when `audit_round_num` is present, then falls back through flat node-level and global defaults. That makes it practical to script sequential executor/reviewer runs with grouped `review-audit-round-N/` fixtures without losing backward-compatible flat fixtures. A fixture can also include a sibling `<fixture-name>.mutations.json` file to simulate provider-side artifact drift deterministically during integration tests.

### 3. Define Workflow Nodes

Edit `.orchestrator/workflows/code-review-example.task.md`:
```yaml
---
schema_version: "<schema-version>"
name: Example Workflow
nodes:
  - id: backend.auth
    mode: sequential
    providers: [codex]
  - id: backend.billing
    mode: sequential
    providers: [claude]
  - id: summary.final
    mode: sequential
    needs: [backend.auth, backend.billing]
    providers: [claude]
---

## backend.auth

Implement auth changes and summarize outputs.

## backend.billing

Implement billing changes and summarize outputs.

## summary.final

Create one merged summary from:
{{backend.auth.output}}
{{backend.billing.output}}
```

### 4. Run

```bash
# Preview execution plan
orchestrator run --dry-run

# Execute
orchestrator run
```

## Workflow Imports

Workflows can import other `.task.md` files through frontmatter composition:

```yaml
---
schema_version: "<schema-version>"
name: Import Example
imports:
  - path: .orchestrator/workflows/modules/auth.task.md
    as: auth
    with:
      module_name: payments-auth
nodes:
  - id: summary.final
    mode: sequential
    needs: [auth.plan]
    providers: [claude]
---

## summary.final

Summarize:
{{auth.plan.output}}
```

Import rules:
- `imports[].path` and `imports[].as` are required.
- Imported workflows must use the same `schema_version` as the root workflow.
- Imported node IDs are namespaced by alias (`auth.plan`).
- Alias collisions and node ID collisions fail validation.
- `imports[].with` binds only `{{param:key}}` tokens in imported prompts. It does not rewire DAG dependencies.
- `imports[].inputs` binds declared imported workflow inputs to upstream node IDs and rewires only the consuming branches.
- Unbound `{{param:key}}` is still rewritten to `{{var:key}}` for backward compatibility, but reusable standalone/importable workflows should use explicit input nodes instead of relying on `{{var:...}}`.
- Imports must resolve within the current project root (`Path.cwd()` boundary).

### Reusable Workflow Inputs

Use `mode: input` for raw reusable workflow boundaries:

```yaml
---
schema_version: "<schema-version>"
name: Review Fix Consumer
inputs:
  review_input: review-input
nodes:
  - id: review-input
    mode: input
    source: "{{file:docs/review-findings.md}}"
  - id: implement.execute
    mode: sequential
    needs: [review-input]
    providers: [codex]
---

## implement.execute

Use these review findings as raw input:
{{review-input.output}}
```

Imported workflows can bind those inputs explicitly:

```yaml
imports:
  - path: review-fix-consumer-example.task.md
    as: fix
    inputs:
      review_input: quality.review.findings
```

Behavior:
- `mode: input` nodes are root nodes that materialize raw file content as node output.
- Input nodes do not define a Markdown `## <node-id>` section; their content comes from `source`.
- Input files can live anywhere project-local that `{{file:...}}` is allowed to read; the workflow examples include copyable sample files under `.orchestrator/workflows/example-templates/sample-inputs/`.
- Only nodes that actually depend on an input need to list it in `needs`.
- Parallel roots that do not depend on that input stay independent.
- Partial binding is supported: bound inputs are pruned and rewired during composition, while unbound inputs remain file-backed roots so the imported workflow still runs standalone.

### Findings Artifacts

Use `findings: true` when a node should publish a concise downstream artifact in addition to its full result:

```yaml
---
schema_version: "<schema-version>"
name: Findings Example
nodes:
  - id: review.context
    mode: sequential
    findings: true
    providers: [claude]
  - id: implement
    mode: sequential
    needs: [review.context]
    providers: [codex]
---

## review.context

Review the codebase and return a full report.
At the end of the output, include exactly one findings block:
<!-- findings -->
- concise finding
<!-- /findings -->

## implement

Use the concise review artifact:
{{review.context.findings}}
```

Behavior:
- `{{node.output}}` still resolves to the full consolidated result artifact.
- `{{node.findings}}` resolves to the concise findings artifact extracted from the marked block.
- `{{node.output_path}}` and `{{node.findings_path}}` resolve to the artifact paths without injecting artifact contents.
- `{{node.output_size}}`, `{{node.findings_size}}`, `{{node.output_sha256}}`, and `{{node.findings_sha256}}` resolve to artifact metadata for compact handoff manifests.
- Findings artifacts are written only for nodes that declare `findings: true`.
- Findings extraction reads the latest eligible executor outputs only, in configured provider order. Reviewer outputs are ignored in mixed-role sequential nodes.
- Findings extraction fails the node if an eligible executor output is missing the block, contains multiple blocks, or contains an empty block.
- Mock lorem output auto-emits a deterministic findings block for non-reviewer findings-enabled nodes; exact echo outputs and file fixtures must include their own findings blocks when needed.

## Workflow Patterns

`orchestrator init` also creates runnable library workflows in `.orchestrator/workflows/example-templates/`.
Run them with `--tasks`, for example:

```bash
orchestrator run --tasks .orchestrator/workflows/example-templates/design-review-example.task.md
orchestrator run --tasks .orchestrator/workflows/example-templates/test-generation-example.task.md
orchestrator run --tasks .orchestrator/workflows/example-templates/composition/review-fix-composed-example.task.md
```

### Full Parallel

```yaml
---
schema_version: "<schema-version>"
name: Full Parallel Work
nodes:
  - id: backend.auth
    mode: sequential
    providers: [codex]
  - id: backend.billing
    mode: sequential
    providers: [claude]
  - id: frontend.settings
    mode: sequential
    providers: [gemini]
---

## backend.auth
Auth changes.

## backend.billing
Billing changes.

## frontend.settings
Settings changes.
```

### Parallel + Sequential Summary

```yaml
---
schema_version: "<schema-version>"
name: Parallel Then Summary
nodes:
  - id: backend.auth
    mode: sequential
    providers: [codex]
  - id: backend.billing
    mode: sequential
    providers: [claude]
  - id: frontend.settings
    mode: sequential
    providers: [gemini]
  - id: summary.final
    mode: sequential
    needs: [backend.auth, backend.billing, frontend.settings]
    providers: [claude]
---

## backend.auth
Auth changes and summary.

## backend.billing
Billing changes and summary.

## frontend.settings
Settings changes and summary.

## summary.final
Merge:
{{backend.auth.output}}
{{backend.billing.output}}
{{frontend.settings.output}}
```

## Commands

| Command | Description |
|---------|-------------|
| `orchestrator init` | Initialize `.orchestrator/` in current directory |
| `orchestrator run` | Execute the workflow DAG |
| `orchestrator run --dry-run` | Preview without executing |
| `orchestrator run --force` | Execute even when identical workflow-signature outputs already exist |
| `orchestrator validate` | Validate workflow syntax, duplicate frontmatter keys, imports/composition, DAG/node sections, provider references, CLI availability when using the built-in `cli` invoker, token-budget settings, and `{{file/env/var}}` template references (uses `--config` or defaults to `.orchestrator/config.yml`) |

## Configuration Reference

### Agent Options

| Field | Type | Description |
|-------|------|-------------|
| `cli_cmd` | `list[str]` | Command to invoke (e.g., `["codex", "exec"]`, `["copilot"]`) |
| `provider_kind` | `str` | Built-in CLI capability profile: `claude`, `codex`, `copilot`, `gemini`, `kilo`, or `generic`. This selects adapter-owned prompt/model/output, usage, quota, and failure-classification behavior |
| `default_model` | `str \| null` | Optional default model identifier; model resolution is workflow provider `model` -> `default_model` -> provider CLI default, and when both are omitted orchestrator does not pass a model flag |
| `model_arg` | `str \| null` | Generic-provider model flag (e.g., `--model`), null to skip. Built-in provider profiles own their model flag mapping and ignore this field |
| `prompt_transport` | `"stdin" \| "argv"` | Prompt transport. Defaults to `stdin` so rendered prompt text is not placed in process argv. Use `argv` only for generic CLIs that cannot read stdin |
| `prompt_transport_arg` | `str \| null` | Optional stdin sentinel or argv prompt flag. For example, Codex uses `-` with stdin; generic argv transport requires an explicit flag such as `--prompt` |
| `extra_args` | `list[str]` | Additional CLI flags (for example, Copilot `--silent`, `--no-ask-user`, `--allow-tool=...`) |
| `invocation_timeout_seconds` | `float \| null` | Hard wall-clock timeout for one provider CLI attempt (default `1800`). Set `null` only when an external supervisor supplies the elapsed-time cap |
| `invocation_idle_timeout_seconds` | `float \| null` | Maximum quiet period with no provider stdout/stderr before the attempt is killed (default `1800`). Set `null` only for provider CLIs that can legitimately stay silent longer under an external supervisor |
| `pricing` | `object` | Optional configured rates per million tokens. Supported buckets: `input`, `cached_input`, `cache_write`, `output`, `reasoning`, or exclusive `total` |
| `quota_reached_on_contains` | `list[str]` | Specific strings that identify provider quota/rate-limit error responses |
| `quota_reached_retry_delay_seconds` | `int` | Fallback retry delay when quota reset time is not parseable |
| `quota_reset_sleep_floor_seconds` | `int` | Added floor buffer for parsed reset waits (default `5`) |
| `settings.sequential_consensus_on_exhaustion` | `"continue" \| "fatal"` | Policy when sequential reviewer rounds exhaust without consensus (default `"continue"`) |
| `settings.max_audit_rounds` | `int` | Maximum allowed `audit_rounds` value on multi-provider sequential review nodes (default `5`) |
| `settings.token_budget.warn_threshold_chars` | `int \| null` | Warn when a node artifact content injection such as `{{node.output}}` / `{{node.findings}}`, or review-loop previous-candidate context, exceeds this size in characters (default `50000`) |
| `settings.token_budget.fail_threshold_chars` | `int \| null` | Fail before provider invocation when a node artifact content injection such as `{{node.output}}` / `{{node.findings}}`, or review-loop previous-candidate context, exceeds this size in characters (default `null`) |
| `settings.integrations.artifacts.options.allowed_template_paths` | `list[str]` | Explicit allowlist for external `{{file:path}}` template files |

`prompt_transport: "stdin"` is the safe default for generated and in-memory
configuration. `prompt_transport: "argv"` is still available for generic CLIs
that require prompt text in argv, but validation and preflight diagnostics make
that exposure explicit.

### Integration Implementations

Integrations are selected via `settings.integrations.<kind>.implementation`:

| Kind | Built-in Implementations | Purpose |
|------|---------------------------|---------|
| `invoker` | `cli`, `mock` | Provider call transport |
| `ui` | `tmux`, `none` | Live run interface |
| `artifacts` | `filesystem` | Stage/result/log/manifest storage |

You can also provide a dotted class path (`package.module:ClassName`) for custom implementations.

### Mock Invoker Options

`mock` provides deterministic, cost-free execution for UI and orchestration validation.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `delay_seconds` | `int \| float` | `0` | Artificial invocation delay (`>= 0`) |
| `observation_delay_seconds` | `int \| float` | `5` | Extra mock-only pause after invocation start for live-runtime observation |
| `output_mode` | `str` | `"lorem"` | Output source: `lorem`, `echo`, or `file` |
| `output_dir` | `str \| null` | `null` | Fixture root directory (required for `output_mode=file`) |
| `strict_file_mode` | `bool` | `false` | Fail when no fixture is found in `file` mode |
| `seed` | `int \| null` | `null` | Enables deterministic seed marker in lorem output |
| `fail_when` | `list[dict]` | `[]` | Selector-based forced failures (`node_id`, `task_id`, `provider`, `role`, `audit_round_num`, `round_num`) |

File-mode fixture lookup priority:

1. If `audit_round_num` is present, check `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/...` first:
   `{task_id}_round{round_num}.md`, `{role}-round-{round_num}.md`, `{task_id}.md`, `{role}.md`, `default-{role}.md`
2. Then check the flat node directory:
   `{output_dir}/{node_id}/{task_id}_round{round_num}.md`, `{role}-round-{round_num}.md`, `{task_id}.md`, `{role}.md`
3. Then fall back to `{output_dir}/{node_id}.md`, `{output_dir}/default-{role}.md`, and `{output_dir}/default.md`
4. Fallback to lorem output for non-reviewer invocations, or a no-findings review contract for reviewer invocations (or raise when `strict_file_mode=true`)

### Quota Reset Behavior

- Supported parser profiles: `codex`, `copilot`, `claude`, `kilo`, `gemini` (`auto` infers from `cli_cmd[0]`).
- Quota detection intentionally ignores broad prose-only hints such as bare
  `quota` in successful output; use error-shaped phrases such as
  `quota reached`, `rate limit reached`, or `too many requests`.
- If quota is detected and reset time is parseable:
  - reset `<= 5h`: retry after `parsed_reset + quota_reset_sleep_floor_seconds`
  - reset `> 5h`: fail fast with a clear wait-time error
- If quota is detected but reset time is not parseable:
  - retry using `quota_reached_retry_delay_seconds` (existing behavior)
- Quota retries are bounded by a five-hour guard; if the next computed wait would
  cross that guard, the run fails with the elapsed retry time and projected wait.

### Provider Failure Classification

Provider CLI failures are classified in `events.ndjson` and `summary.md` when
orchestrator can identify the terminal cause. Context exhaustion is split into
two cases: `initial_request_too_large` means the resolved prompt or injected
artifact was too large before useful work began, while
`provider_session_context_exhausted` means the provider CLI filled its own
session during tool calls, file reads, or command output. The latter is not
automatically retried; split broad workflows, narrow file scope, or use findings
artifacts to reduce provider-side transcript growth.

### Node Modes

- **parallel** – All providers run concurrently; every provider must use the `executor` role
- **sequential** – Single-provider sequential nodes repeat executor passes based on `depth`; multi-provider sequential nodes are ordered review loops with a contiguous executor segment followed by a contiguous reviewer segment
- For multi-provider review loops:
  - `depth` means remediation fix/verify cycles allowed after a fresh audit finds issues
  - `audit_rounds` means fresh audit passes allowed overall and defaults to `1`
  - local `round1` is the fresh audit over the current candidate
  - local `round2..round(depth+1)` are remediation cycles inside that audit round
  - `audit_rounds` is valid only on multi-provider sequential review nodes
  - each local review step must have a runtime-chosen canonical candidate set before reviewers run
  - remediation reviewer calls are skipped when the current candidate is empty, clearly redirected to another artifact, or unchanged after whitespace normalization
- **input** – Materializes a raw file-backed input artifact for downstream nodes
- **parallel fail-safety** – Use `failure_threshold` and `continue_on_failure` for partial-failure behavior

Workflow keywords are case-sensitive and must be lower-case exactly as documented. This applies to values such as `mode`, provider `role`, artifact names in `{{node.artifact}}`, and `settings.sequential_consensus_on_exhaustion`.

### Template Magic Keywords

Task prompts support several template shortcuts to dynamically inject context:

| Keyword | Description | Example |
|---------|-------------|---------|
| `{{env:KEY}}` | Injects an environment variable from your shell. | `{{env:BRANCH_NAME}}` → `develop` |
| `{{file:PATH}}` | Injects the contents of a local file. | `{{file:src/main.py}}` |
| `{{var:KEY}}` | Injects an internal CLI variable. Currently, only `project_name` (the root directory name) is supported. | `{{var:project_name}}` → `orchestrator_cli` |
| `{{param:KEY}}` | Injects parameter values mapped via `imports[].with` in the frontmatter. | `{{param:module_name}}` |
| `{{node.output}}` | Injects the full consolidated result of a prior node in the workflow DAG. | `{{backend.auth.output}}` |
| `{{node.findings}}` | Injects the concise findings artifact of a prior node when that node declares `findings: true`. | `{{backend.review.findings}}` |
| `{{node.output_path}}` | Injects the path to the full consolidated result artifact without reading the file content into the prompt. | `{{backend.auth.output_path}}` |
| `{{node.findings_path}}` | Injects the path to a findings artifact without reading the file content into the prompt; the node must declare `findings: true`. | `{{backend.review.findings_path}}` |
| `{{node.output_size}}` / `{{node.findings_size}}` | Injects the artifact size in bytes. Findings metadata requires `findings: true`. | `{{backend.auth.output_size}}` |
| `{{node.output_sha256}}` / `{{node.findings_sha256}}` | Injects the artifact SHA-256 hash. Findings metadata requires `findings: true`. | `{{backend.auth.output_sha256}}` |

> **Tip:** Shell variables like `project_name=foo` will **not** affect `{{var:project_name}}`. If you need runtime overrides from the shell, use the `{{env:project_name}}` token instead.

#### Node Context Rules

When using node artifact templates:
- Node IDs must match `[a-z0-9._-]+`
- Every non-input frontmatter node must have exactly one document-root `## <id>` section
- Input nodes must not define a document-root `## <id>` section
- All `{{<id>.<artifact>}}` references are valid only for upstream dependency nodes
- `findings`, `findings_path`, `findings_size`, and `findings_sha256` require the upstream node to declare `findings: true`
- Artifact names are case-sensitive and must be lower-case
- Imported nodes are referenced by alias-prefixed IDs (for example `{{auth.plan.output}}` or `{{auth.review.findings}}`)

### Role-Scoped Prompt Segments

Non-input node prompt text is role-scoped by default:
- Text outside markers is `shared` and is included for every scheduled role.
- `executor` and `reviewer` blocks are opt-in role deltas.

Use standalone HTML comment markers inside a node section:

```markdown
## review.iterate

Shared context for both roles.

<!-- orchestrator:executor -->
Executor-only authored instructions.
<!-- /orchestrator:executor -->

<!-- orchestrator:reviewer -->
Reviewer-only authored instructions.
<!-- /orchestrator:reviewer -->
```

Rules:
- Markers are recognized only as standalone CommonMark `html_block` comments.
- Marker-like text in paragraphs, lists, blockquotes, or code blocks is treated as literal text.
- Unknown roles, malformed markers, nested role blocks, mismatched closes, close-without-open, unclosed blocks, and empty role blocks are validation errors.
- Parallel and single-provider sequential nodes allow only `shared` and `executor` segments.
- Multi-provider sequential review loops allow `shared`, `executor`, and `reviewer` segments.

### Prompt Budget Guards

You can guard node artifact prompt growth globally:

```yaml
settings:
  token_budget:
    warn_threshold_chars: 50000
    fail_threshold_chars: null
```

You can override those thresholds per execution node:

```yaml
nodes:
  - id: summary.final
    mode: sequential
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 40000
```

Behavior:
- warn threshold exceeded: emit a runtime warning and continue with the full injected output
- fail threshold exceeded: abort before provider invocation
- set a node threshold to `null` to disable the inherited setting for that node

### Import Parameter Templates

Imported workflows can define parameterized prompt tokens:

```text
{{param:module_name}}
```

Behavior:
- `imports[].with.module_name: "..."` substitutes `{{param:module_name}}` during composition.
- If no `with` value is provided, it is rewritten to `{{var:module_name}}` for backward compatibility.
- `imports[].with` does not create reusable raw inputs or rewrite DAG edges. Use `mode: input` plus `imports[].inputs` for standalone/importable workflow boundaries.
- `{{param:...}}` is composition-only and distinct from runtime `{{var:...}}`.

### Reviewer Verdict Format (Sequential Review)

Multi-provider sequential reviewer outputs should end with this review block:

```text
## Major Issues
None

## Minor Issues
None

## Nitpicks
None

---
VERDICT: NO_FINDINGS
```

or:

```text
## Major Issues
- concrete required fix

## Minor Issues
None

## Nitpicks
None

---
VERDICT: CHANGES_REQUESTED
```

or:

```text
## Major Issues
None

## Minor Issues
None

## Nitpicks
- optional polish item

---
VERDICT: NITS_ONLY
```

Rules:
- `Major Issues`, `Minor Issues`, and `Nitpicks` should all be present in that order.
- Use `None` as the preferred empty sentinel for a section.
- The runtime extracts the last valid review block anywhere in the response, so optional commentary may appear above it.
- When the verdict token conflicts with the section content, the runtime normalizes to the safer canonical verdict while preserving approval semantics for `NO_FINDINGS` and `NITS_ONLY`.
- If no structured block can be extracted, the runtime may infer approval from explicit plain-language cues like `LGTM` or `ready to merge`; otherwise it records a warning and treats the review as non-approval instead of crashing the workflow.
- If a structured review block is present but malformed, the runtime records a warning, preserves the raw reviewer sidecar, and treats the round as non-approval instead of aborting the loop.
- Raw reviewer text is preserved beside the normalized reviewer artifact as `.raw.txt`, with parse metadata in `.review.json`.
- Reviewers must not approve while any actionable bug, regression risk, missing validation, or other major/minor issue remains. Approval is reserved for outputs where only optional nitpicks, if any, are left.

Sequential reviewer prompts instruct the reviewer to inspect the current candidate, report structured findings, and avoid changing the workspace. During remediation rounds, the prompt carries only the unresolved major/minor packet from the previous review step; fresh audit rounds do not inherit that packet. Invocations remain stateless; prior-cycle review feedback is carried forward explicitly through artifacts and prompt context, not provider-native chat session reuse.

One audit round starts with a fresh audit:
- local `round1`: current candidate is reviewed with no carry-forward from earlier audit rounds
- local `round2..round(depth+1)`: executor/reviewer remediation cycles run only if local `round1` found major or minor issues

Early-stop behavior:
- if every reviewer approves in local `round1`, the node ends immediately and skips remaining configured `audit_rounds`
- if a later remediation cycle reaches approval, that audit round stops immediately and the next configured audit round starts fresh

When one audit round exhausts `depth` without consensus, the next configured audit round starts from the latest executor candidate with no unresolved review state carried forward from the exhausted audit round.

When the final executed audit round still does not reach consensus, behavior is:
- continue with warning by default (`settings.sequential_consensus_on_exhaustion: "continue"`) when at least one valid canonical candidate exists
- fail the node/workflow when set to `fatal`
- always continue for that node when `continue_on_failure: true`
- fail immediately if no valid canonical candidate was ever produced across all audit rounds

When the `continue` policy lets a run finish after unresolved review consensus, the workflow and manifest status remain `succeeded`, but the terminal run summary and `logs/summary.md` include `Review consensus: unresolved; continued after exhaustion` near the top.

## Output

Results are saved to:
- `.orchestrator/execution-stages/<workflow>-<run_id>/<node>/` – Individual outputs per round for a run
- `.orchestrator/execution-stages/<workflow>-<run_id>/preflight/` – Compiled execution plan, preflight manifest/metadata/summary, render plans, dependency graph, runtime config snapshot, token catalog, diagnostics when preflight fails, and bundled static files
- `.orchestrator/execution-stages/<workflow>-<run_id>/logs/events.ndjson` – Run-level orchestrator event log
- `.orchestrator/execution-stages/<workflow>-<run_id>/logs/summary.md` – Run-level orchestrator summary for postmortems
- `.orchestrator/execution-stages/<workflow>-<run_id>/<node>/logs/<provider>/` – Per-invocation logs for each node
- `.orchestrator/execution-stages/<workflow>-<run_id>/manifests/` – Run manifest files (`<workflow_signature>.json`, `latest.json`)
- `.orchestrator/execution-results/<workflow>-<run_id>/` – Consolidated per-node results for a run, created only after node results are finalized

Run manifests use a single workflow-level signature built from the composed workflow, referenced workflow files, provider execution settings, execution/artifact-scoped integration options, dependency graph, static file content hashes, and env/var/config fingerprints. They store the scoped `effective_runtime_config_signature` and redacted runtime config snapshot, not raw config YAML. Observer-only live UI settings such as `--no-live` are excluded. When an identical successful workflow signature already exists, `orchestrator run` suppresses the whole run unless `--force` is provided; it does not perform per-node incremental caching.

Preflight secrets are fingerprinted with `.orchestrator/preflight/fingerprint.key`. `orchestrator init` creates this key, and `orchestrator run` creates it only when sensitive env/var/config values need stable fingerprints. `orchestrator validate` and `orchestrator run --dry-run` do not write artifacts or create the key; if the key is absent, they use a process-local ephemeral key for that preview. Sensitive values are persisted only as handles and fingerprints, so persisted plans cannot replay sensitive prompt text without the same-process secret context. Provider-emitted output is outside orchestrator redaction and may contain anything the provider prints.

`{{file:...}}` references are read during preflight as UTF-8 text and bundled under `preflight/static-files/`; binary, NUL-containing, or non-UTF-8 content fails before provider invocation. In imported Markdown workflows, relative file tokens resolve from the imported module's source directory before the normal project-root and allowlist checks.

Result filenames preserve distinct valid node IDs and use dash suffixes:
- `<node-id>-result.md` for the full consolidated result
- `<node-id>-findings.md` for the concise findings artifact when `findings: true` is enabled

Review-loop stage layout is conditional:
- when `audit_rounds` is omitted or `1`, the stage layout stays flat exactly as before
- when `audit_rounds > 1`, review artifacts are grouped under `<node>/review-audit-round-N/`
- each grouped audit round keeps its own `review-state/` directory and local `round1`, `round2`, ... file numbering
- node-level `logs/<provider>/` stays at the stage root, and filenames include `-auditN-` so local round numbers can reset safely per audit round
- every review-loop node also writes `<node>/review-state/review-loop-status.json` with the final canonical executor outputs, reviewer outputs, and counters for invalid-candidate rounds, no-progress remediation rounds, artifact-drift warnings, and consensus-exhaustion continuation

Consolidated results remain current-output-only per task id for token efficiency, but review-loop stages now prefer the explicit paths in `review-loop-status.json` instead of inferring the final candidate from lexically latest filenames. Full history stays under `.orchestrator/execution-stages/`. Review inbox artifacts record the current and previous executor artifact paths for each executor in the local round so multi-executor follow-up stays auditable. When multiple outputs or findings are consolidated, sections are written in configured provider order where that order is meaningful, not alphabetical `task_id` order.

When included stage outputs contain an explicit `Generated Files` section, or clearly say in past tense that they created, wrote, saved, generated, renamed, moved, or updated an existing workspace file, the consolidated result appends a `Generated Files` section with links to those files. Imperative review guidance such as "move this code" or "update this file" is ignored. This section is link-only: file contents stay in the workspace and are not copied into result artifacts.

`logs` and `manifests` are reserved run-root names and cannot be used as node IDs.

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for development setup and guidelines.

## License

Apache 2.0
