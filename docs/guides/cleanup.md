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
see [Workspace isolation](workspace-isolation.md).

By default, cleanup is scoped to the current Git repository. Use `--all-projects`
to clean every repository bucket under the configured workspace cache root.
Crewplane cannot tell whether another project's cached workspace is still in
use, so always preview an all-projects cleanup first. Adding
`--all-projects --yes` confirms that you accept this risk.

## Preview Cleanup

```bash
crewplane cleanup workspaces --dry-run
```

Cleanup is non-destructive unless `--yes` is set. `--dry-run` prints the paths
that would be removed and wins over `--yes` if both are present.

With no status flags, cleanup selects only workspaces from completed runs in the
current project. It leaves running workspaces and workspaces whose state cannot
be confirmed.

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

`--orphans` selects cache folders that have no saved workspace record. A broken
or conflicting record does not count as a missing record. Crewplane removes an
orphan only when it can confirm that the related run has ended and no provider
process is still active. Otherwise, it keeps the folder and explains why.

Some workspaces can be reused during a run and therefore have more than one
saved record. Before deleting one, Crewplane confirms that all of its records
agree, the folder belongs to the expected repository, Git still recognizes the
worktree, and removing it will not change a branch. If any check cannot be
confirmed, Crewplane leaves the workspace in place.

Cleanup can also remove internal Git references that Crewplane created for the
run. It removes them only when they still point to the exact commits originally
recorded. If a reference changed or Crewplane cannot confirm that it owns it,
the reference is left untouched. Related workspace-result references are
removed together so cleanup does not leave a partial result behind.

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
return to the [Guides](../index.md#guided-tutorial-track).
