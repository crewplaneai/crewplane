# Cleaning Up Workspace Caches

`crewplane cleanup workspaces` removes generated Experimental workspace
isolation cache entries.

This command cleans Experimental workspace cache entries. It does not delete
canonical run records under `.crewplane/execution-stages/` or
`.crewplane/execution-results/`.

```bash
crewplane cleanup workspaces --dry-run
crewplane cleanup workspaces --yes
```

By default, cleanup is scoped to the current Git repository. Use `--all-projects`
to clean every repository bucket under the configured workspace cache root.

## Options

```bash
crewplane cleanup workspaces \
  --config .crewplane/config.yml \
  --dry-run \
  --run <run-key> \
  --older-than 7d \
  --successful \
  --failed \
  --cancelled \
  --orphans
```

Cleanup is non-destructive unless `--yes` is set. `--dry-run` prints paths that
would be removed and wins over `--yes` if both are present.

Duration strings for `--older-than` accept integer seconds or suffixes such as
`30s`, `30m`, `12h`, or `7d`.

Status filters are:

- `--successful`
- `--failed`
- `--cancelled`

`--orphans` selects cache paths without workspace state.

`--all-projects` cannot be combined with `--orphans` or status filters because
those filters require current-project workspace-state artifacts.

## Guardrails

Cleanup rejects cache roots that are relative, symlinks, overlap the project,
overlap `.crewplane/`, overlap run artifact directories, or overlap Git
metadata paths.

Cleanup does not remove canonical workspace lineage, provider outputs, findings,
run manifests, or result artifacts under `.crewplane/`.

Cleanup scans generated cache families named `workspace-runs`, `workspaces`,
`snapshots`, and `review-workspaces`. Destructive cleanup can also remove
run-owned cached Git refs, but it does not remove canonical run artifacts.

## To Remove Run Records

Crewplane does not currently provide a run-record prune command. Delete or
archive `.crewplane/execution-stages/<run-key>/` and
`.crewplane/execution-results/<run-key>/` according to your project policy.
