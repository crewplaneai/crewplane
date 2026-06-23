# Cleanup

`orchestrator cleanup workspaces` removes generated Experimental workspace
isolation cache entries.

```bash
orchestrator cleanup workspaces --dry-run
orchestrator cleanup workspaces --yes
```

By default, cleanup is scoped to the current Git repository. Use `--all-projects`
to clean every repository bucket under the configured workspace cache root.

## Options

```bash
orchestrator cleanup workspaces \
  --config .orchestrator/config.yml \
  --dry-run \
  --run <run-key> \
  --older-than 7d \
  --successful \
  --failed \
  --cancelled \
  --orphans
```

`--dry-run` prints paths that would be removed. Destructive cleanup requires
`--yes`.

Duration strings for `--older-than` use forms such as `30m`, `12h`, or `7d`.

Status filters are:

- `--successful`
- `--failed`
- `--cancelled`

`--orphans` selects cache paths without workspace state.

`--all-projects` cannot be combined with `--orphans` or status filters because
those filters require current-project workspace-state artifacts.

## Guardrails

Cleanup rejects cache roots that are relative, symlinks, overlap the project,
overlap `.orchestrator/`, overlap run artifact directories, or overlap Git
metadata paths.

Cleanup does not remove canonical workspace lineage, provider outputs, findings,
run manifests, or result artifacts under `.orchestrator/`.
