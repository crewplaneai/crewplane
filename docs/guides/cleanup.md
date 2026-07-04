# Cleaning Up Workspace Caches

Workflow runs can leave managed workspace cache entries behind. Use
`crewplane cleanup workspaces` to preview and remove those caches when you no
longer need them.

> ⚠️ **Note:** Destructive cleanup removes saved workspace cache artifacts, such
> as retained workspaces and cache state. It does not delete canonical run
> records under `.crewplane/execution-stages/` or
> `.crewplane/execution-results/`.

Start with a dry run so you can see exactly what would be deleted.

For the workspace boundary and why cleanup only targets managed cache entries,
see [Experimental workspace isolation](workspace-isolation.md).

By default, cleanup is scoped to the current Git repository. Use `--all-projects`
to clean every repository bucket under the configured workspace cache root.

## Preview Cleanup

```bash
crewplane cleanup workspaces --dry-run
```

Cleanup is non-destructive unless `--yes` is set. `--dry-run` prints the paths
that would be removed and wins over `--yes` if both are present.

## Delete Matching Cache Entries

```bash
crewplane cleanup workspaces --yes
```

Use `--yes` only after the dry-run output looks right. Add filters when you want
to narrow the deletion:

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

Duration strings for `--older-than` accept integer seconds or suffixes such as
`30s`, `30m`, `12h`, or `7d`.

Status filters are:

- `--successful`
- `--failed`
- `--cancelled`

`--orphans` selects cache paths that do not have workspace state.

`--all-projects` cannot be combined with `--orphans` or status filters because
those filters require current-project workspace-state artifacts.

## Clean Caches For One Run

```bash
crewplane cleanup workspaces --run <run-key> --dry-run
crewplane cleanup workspaces --run <run-key> --yes
```

Use the full run key from `.crewplane/execution-stages/<run-key>/`.

## Clean Older Caches

```bash
crewplane cleanup workspaces --older-than 7d --dry-run
crewplane cleanup workspaces --older-than 7d --yes
```

## Guardrails

Cleanup rejects cache roots that are relative, symlinks, overlap the project,
overlap `.crewplane/`, overlap run artifact directories, or overlap Git
metadata paths.

Cleanup scans generated cache families named `workspace-runs`, `workspaces`,
`snapshots`, and `review-workspaces`. Destructive cleanup can also remove
run-owned cached Git refs.

Cleanup does not remove canonical workspace lineage, provider outputs, findings,
run manifests, or result artifacts under `.crewplane/`.

## Remove Run Records Separately

Crewplane does not currently provide a run-record prune command. Delete or
archive `.crewplane/execution-stages/<run-key>/` and
`.crewplane/execution-results/<run-key>/` according to your project policy.

## Next

Congratulations 🎉

You made it through the guides tour. That is the full path from setup, provider
configuration, workflow authoring, running, inspection, composition, review
loops, workspace isolation, and cleanup.

When you need exact flags or syntax, see the
[Command reference](../reference/commands.md). To revisit another walkthrough,
return to the [Guides](../index.md#guides).
