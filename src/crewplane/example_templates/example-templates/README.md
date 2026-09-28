# Choose and Adapt a Preloaded Example

These workflows live under `.crewplane/workflows/example-templates/` in your
project. Edit their prompts and inputs to match your task. The
[examples catalog](https://github.com/crewplaneai/crewplane/blob/master/docs/examples/index.md)
links to complete recipes and guides.

## Choose an Example

| Example | Default agent profiles | What to prepare | Outcome with real providers |
| --- | --- | --- | --- |
| [Code review](code-review-example.task.md) | `codex`, `claude`, `gemini` | The project to inspect | Reports findings, including unresolved concerns and missing coverage. |
| [Feature implementation](feature-implement-example.task.md) | `claude`, `codex`, `gemini` | A [feature brief](sample-inputs/feature-brief.md) | Implements, reviews, and fixes code and tests. |
| [Bug fix](bug-fix-example.task.md) | `claude`, `codex` | Existing code and a [bug report](sample-inputs/bug-report.md) | Reproduces, diagnoses, fixes, and verifies a supported defect. |
| [Review an existing change](review-existing-change-example.task.md) | `claude`, `codex` | Local changes and a [review brief](sample-inputs/change-review-brief.md) | Reviews first, fixes confirmed issues, and verifies the result. |
| [Test generation](test-generation-example.task.md) | `claude`, `codex`, `gemini` | A brief matching existing code | Writes and reviews tests. |
| [Refactoring](refactoring-example.task.md) | `gemini`, `claude`, `codex` | Project [coding standards](sample-inputs/coding-standards.md) | Audits, plans, refactors, and reviews fixes. |
| [Design review](design-review-example.task.md) | `claude`, `codex`, `gemini` | A feature brief | Returns a reviewed design decision record. |
| [Composed review and fix](composition/review-fix-composed-example.task.md) | `gemini`, `codex`, `claude` | Project coding standards | Passes a review report to a reusable fix workflow. |
| [Workspace alternatives](worktree/workspace-alternatives-example.task.md) | `codex`, `claude` | A committed request and workspace setup | Implements separately and compares reports. |
| [Inherited worktree](worktree/workspace-inherited-worktree-example.task.md) | `codex`, `claude` | A committed request and workspace setup | Implements, tests, and fixes on one source line. |
| [Multiple executors](multi-executor-review-chain-example.task.md) | `claude`, `codex`, `gemini` | A feature brief | Produces separate design proposals for review together. |

## Prepare and Run

Enable the listed profiles in `.crewplane/config.yml`, or adapt the workflow to
profiles you already have. Keep `settings.integrations.invoker.implementation: mock`
for a trial with sample responses. Follow
[Provider setup](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md#turn-mock-mode-onoff)
when switching to real provider calls. The
[starter workflow](../single-agent-review.task.md) works with the generated mock
config.

Before running your chosen example:

- Replace demonstration briefs and hypothetical findings with your task's
  paths, requirements, and evidence.
- Test generation, bug fixing, and existing-change review need existing code.
  Use `--force` for bug-fix and existing-change runs that must inspect current
  files again. Keep workspace isolation disabled for existing-change review.
  See the [bug-fix](https://github.com/crewplaneai/crewplane/blob/master/docs/examples/bug-fix.md)
  and [existing-change](https://github.com/crewplaneai/crewplane/blob/master/docs/examples/review-existing-change.md)
  recipes.
- Workspace examples need a supported clean Git repository,
  `settings.workspace.enabled: true`, and a committed
  `docs/crewplane-change-request.md`. Follow
  [Workspace setup](https://github.com/crewplaneai/crewplane/blob/master/docs/examples/workspace.md).
- A completed run can still have unresolved reviewer objections. Choose a
  [review policy](https://github.com/crewplaneai/crewplane/blob/master/docs/guides/review-loops.md#continue-or-fail-on-exhaustion)
  before using a review loop as an approval gate.

From your project directory, validate and run the selected workflow:

```bash
crewplane validate .crewplane/workflows/example-templates/code-review-example.task.md
crewplane run --no-live --tasks .crewplane/workflows/example-templates/code-review-example.task.md
```

Reports are saved under `.crewplane/execution-results/<run-key>/`. See
[Inspecting run records](https://github.com/crewplaneai/crewplane/blob/master/docs/guides/inspecting-artifacts.md)
for reports and captured files.

`crewplane init` preserves existing copies. After upgrading, compare them with
the [packaged examples](https://github.com/crewplaneai/crewplane/tree/master/src/crewplane/example_templates/example-templates)
and apply the updates you want.
