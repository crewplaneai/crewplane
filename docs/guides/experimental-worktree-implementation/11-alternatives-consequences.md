# Experimental Worktree Implementation: Rejected Alternatives and Consequences

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Rejected Alternatives
1. Workflow-scoped mutable worktree
   - Leaves parallel nodes sharing mutable state.
   - Requires hidden ordering or merge semantics.

2. Provider-level worktree support
   - Leaks orchestration policy into provider adapters.
   - Duplicates behavior across providers.

3. Ephemeral clones with shared alternates
   - Stronger metadata isolation.
   - More disk, checkout, object-lifetime, and cleanup complexity.
   - Better suited as a future high-isolation backend.

4. Patch/archive lineage instead of Git bundles
   - Easier to inspect in small cases.
   - Weaker for binary files, executable bits, deletes, renames, symlinks, and
     tree identity.
   - Larger replay and validation surface.

5. Container or devcontainer workspace execution
   - Stronger execution boundary.
   - Changes scope into environment provisioning.
   - Better as future backend or operational recommendation.

6. Automatic merge-back or promotion
   - Requires conflict, branch-safety, review, and confirmation semantics.
   - Not part of source isolation.

7. Hidden Git refs without artifact mirrors
   - Breaks auditability.
   - Makes cleanup/ref deletion correctness-sensitive.

8. Transitive or multi-source lineage
   - Adds implicit merge semantics.
   - Direct upstream lineage is sufficient for v1.

9. Lazy or sparse provisioning
   - Reduces large-repo cost.
   - Deferred because provider CLIs and workspace-file tokens expect ordinary
     files.

10. Runtime template parsing fallback
    - Reintroduces split authority between preflight and runtime.

11. Broad failure on all Git ref/config changes
    - Produces false positives from unrelated user Git activity.
    - V1 protects orchestrator refs, contract invariants, and expected
      identities.

12. Preserving or squashing provider-created commits
    - Allows provider-controlled metadata, hooks, global/system config,
      worktree config, filters, dates, and history shape into or near the
      lineage contract.
    - Validating the final commit tree does not prove the committed blob bytes
      came from materialized workspace files under runtime's no-transform
      contract.
    - Detecting transient commit creation after a reset would require scanning
      shared object storage and would produce false positives from unrelated
      local Git activity.
    - V1 rejects final `HEAD` movement and never uses provider-created commit
      trees. Providers should leave file edits in the worktree for
      runtime-owned capture.

13. Native Windows support in v1
    - Adds junction, reparse-point, path normalization, locking, symlink,
      executable-bit, and Git-for-Windows differences.
    - Users can run workspace-enabled workflows under WSL.
    - Native support can be revisited after POSIX semantics are stable.

14. Snapshot lineage from upstream nodes
    - Adds bundle rehydration and lineage ordering to a strategy meant to be
      disposable and non-lineage.
    - Makes downstream code inheritance less explicit.
    - V1 keeps snapshots project-source only; select the same
      `kind: worktree` source line for upstream code state.

15. Preserving `settings.default_workspace`
    - The field is dormant and misleading.
    - Keeping it would create two workspace-looking config surfaces while only
      one controls this feature.

16. `git archive` as the snapshot baseline
    - `git archive` can honor archive-specific attributes such as
      `export-ignore` and `export-subst`.
    - Snapshot workspaces should represent the selected source commit for
      provider inspection, not release archive semantics.
    - V1 uses isolated temporary index checkout.

17. Custom `.gitignore` matcher in v1
    - Correctly matching Git ignore semantics is a separate policy engine.
    - V1 uses Git's standard ignored/untracked classification under bounded
      ignore sources.
    - A deterministic custom ignore policy may be considered for a future
      high-integrity backend.

18. Allowing byte-transforming attributes in unrelated paths
    - Full snapshot and worktree materialization checks out the full tree.
    - Allowing unrelated filters or text conversion would make provider-visible
      bytes dependent on local Git filter/config behavior.
    - V1 rejects byte-transforming attributes across the tracked regular-file
      tree. This is stricter but coherent.

19. Reading workspace filesystem bytes for file-token injection
    - Duplicate lookup happens before workspace allocation, so workspace
      filesystem bytes are not available without side effects.
    - Reading materialized files would add a second token authority outside
      preflight.
    - V1 uses Git object bytes as canonical file-token bytes and rejects
      byte-transforming attributes globally.

20. Exempting dirty workflow/config files from clean-start when tracked
    - It would let preflight use uncommitted control-plane bytes while providers
      see committed workspace bytes.
    - V1 keeps clean-start literal for tracked dirty state.

21. Falling back to assumed SHA-1 object format
    - Object format affects repository identity, source signatures, and bundle
      verification.
    - V1 records the Git storage object format and fails if it cannot be probed.

22. Trusting custom process invokers without a workspace launch contract
    - A dotted-path invoker could launch provider processes without the
      controlled `cwd` or Git environment.
    - V1 adds optional workspace capability metadata and fails
      workspace-enabled real runs for unsupported launch modes.

23. Making workspace capability mandatory on the base invoker port
    - It would leak the feature flag into disabled-mode project-root execution.
    - V1 keeps the base port valid for disabled mode and validates optional
      capability metadata only when workspace execution is enabled.

24. Supporting future non-process invokers in v1
    - The filesystem, local command, artifact, and observability contracts are
      not yet defined for API-style invokers.
    - Accepting a placeholder launch mode would create an untested path.
    - V1 supports only `runtime_command_runner` and `mock_no_child_process`.

25. Requiring full snapshot directories for input nodes
    - Input nodes do not invoke providers and usually need only file-token
      bytes.
    - V1 assembles input outputs from compiled project-source blob records
      without allocating provider workspaces.

26. Following symlinks for enabled-mode file tokens
    - Symlink resolution would reintroduce filesystem path interpretation and
      possible checkout-dependent behavior.
    - V1 file-token injection is Git-object based and requires regular tracked
      blobs.
    - Providers may still see symlinks in workspaces according to normal Git
      checkout behavior when filesystem probes pass.

27. Signing caller Git config instead of forcing a bounded runtime-owned
    contract
    - Signing all relevant local, global, system, worktree, include, attribute,
      ignore, and pathspec config would be hard to make complete.
    - It would make duplicate identity depend on user-machine details unrelated
      to the repository.
    - V1 forces runtime-owned Git command environment where correctness depends
      on it, overrides a small set of local core settings, and rejects unsafe
      local/worktree overrides.

28. Allowing Git LFS for workspace-enabled v1
    - LFS depends on filter attributes and local filter availability.
    - Provider-visible bytes can differ between machines with and without LFS
      content materialized.
    - V1 rejects effective `filter=lfs`; a future backend can add explicit LFS
      materialization and integrity semantics.

29. Building a raw-byte capture platform in v1
    - It can support more repositories later, especially ones with filters or
      text conversion.
    - It requires a much larger path, attribute, ignore, and byte-source engine.
    - V1 narrows the problem by rejecting byte-transforming attributes and using
      Git's normal staging under a strict no-transform contract.

30. Capturing provider final `HEAD` tree
    - Provider-created commits can leave the worktree clean while changing
      source state.
    - Provider-run Git can use global/system config, worktree config, hooks, and
      filters outside runtime's no-transform contract.
    - V1 rejects final provider `HEAD` movement instead of reading
      provider-created commit trees.

31. Multiple content-hash-derived workspace profiles
    - They add identity churn, observability noise, and implementation surface.
    - V1 uses one authored behavior contract mode, `blob_exact`, and changes
      that mode only when semantics change.

32. Control-plane input signing beyond ADR 0012
    - ADR 0012 already signs normalized workflow/config/template inputs into
      workflow identity.
    - Source policy handles tracked dirty source state.
    - A separate control-plane signing phase is unnecessary for v1.

33. Node-source-only file-token resolution
    - It is simple, but wrong for review and remediation loops.
    - Reviewers inspect the current candidate, so file-token bytes must come
      from the current candidate.
    - V1 resolves file tokens per invocation source identity.

34. One monolithic implementation milestone
    - It makes v1 too large to validate safely.
    - This ADR defines implementation slices so disabled-mode preservation,
      invocation `cwd`, source policy, provisioning, lineage, cleanup, and
      observability can be proved incrementally.
    - Public workspace-enabled behavior remains incomplete until the required
      slices are all implemented.

## Consequences
### Positive
- Default execution remains simple and non-Git-compatible.
- Workspace isolation is opt-in.
- Enabled runs isolate parallel coding nodes.
- Downstream code inheritance is explicit and auditable.
- Workspace lineage artifacts support duplicate skip and resume without relying
  on hidden refs.
- Provider adapters remain focused on invocation transport.
- Replaceable invoker adapters cannot silently bypass workspace launch controls
  in enabled mode.
- Disabled-mode custom invokers are not forced to implement workspace
  capability metadata.
- Preflight remains authoritative.
- Validate and dry-run stay side-effect-free.
- Dirty, ignored, untracked, hidden-index, ambient ignore-source, ambient
  worktree-config, ambient pathspec, ambient object-store, and unsafe local
  config state cannot silently enter enabled-mode workspaces.
- Runtime uses standard Git checkout, worktree, object-read, literal pathspec,
  attribute inspection, staging, commit-tree, and bundle behavior instead of
  shipping broad custom Git policy engines in v1.
- File-token prompt bytes are deterministic because they come from Git blob
  objects resolved through literal path handling.
- Reviewer and remediation file-token bytes are consistent with the current
  candidate being inspected or modified.
- Explicit handling for `core.filemode`, `core.symlinks`, `core.ignorecase`, and
  `core.precomposeunicode` closes a class of platform-dependent source/capture
  bugs.
- Rejecting final provider `HEAD` movement closes the main provider-commit
  lineage bypass without claiming impossible full object-database attribution.
- Input-node file assembly avoids unnecessary snapshot checkout cost.
- Rendered workspace-file digests and invocation-source descriptors give
  duplicate skip and resume a concrete byte-integrity contract without storing
  prompt text.
- Removing `settings.default_workspace` eliminates a misleading config surface.

### Negative
- Enabled runs require Git 2.34.1 or newer plus required capability probes.
- Workspace-enabled real runs require an invoker adapter that declares and
  satisfies v1 workspace launch compatibility.
- Custom process invokers that bypass the runtime-owned command runner are
  unsupported in v1.
- Large repositories may consume significant cache disk space.
- V1 does not support submodules, sparse checkouts, partial clones, object
  alternates, grafts, native Windows, non-filesystem artifact backends for real
  execution, non-process invokers, Git LFS, custom filters, `ident`,
  `working-tree-encoding`, line-ending conversion, worktree-specific Git config,
  split index, fsmonitor, untracked cache, or provider-created commit
  preservation.
- Repositories with common byte-transforming attributes such as `* text=auto` or
  LFS-tracked assets cannot use workspace-enabled v1 until a future
  materialization/capture design supports them.
- Local/worktree Git config includes, local/worktree `core.attributesFile`,
  local/worktree `core.excludesFile`, active worktree config, effective
  `info/exclude` patterns, object alternates, grafts, and provider-created
  `.gitattributes` changes are unsupported in workspace-enabled v1.
- Some macOS or WSL-mounted repositories may fail if path casing, Unicode
  normalization, symlink support, or executable-bit support cannot satisfy the
  v1 contract.
- Git worktrees share repository metadata and are not a provider sandbox.
- Snapshot workspaces are writable and disposable; source-looking drift is
  summarized and discarded, not promoted to lineage.
- Providers that create commits must be instructed not to leave `HEAD` moved
  during workspace-enabled v1 runs.
- Providers can still leave unreachable objects in the shared Git object
  database; v1 does not scan or remove them.
- Raw capture, custom ignore semantics, LFS-aware materialization,
  provider-created commit preservation, native Windows support, and stronger Git
  metadata isolation remain future work.
- Removing `settings.default_workspace` is a schema break for stale configs.
