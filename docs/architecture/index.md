# Architecture

Architecture docs are decision records and maintainer-facing design references.
User task guidance lives in the public docs sections linked from
[docs/index.md](../index.md).

Start here:

- [Modular orchestration architecture](modular-orchestration-architecture.md)
- [ADR 0001: Ports, adapters, and runtime integrations](adr/0001-ports-adapters-runtime-integrations.md)
- [ADR 0012: Preflight compiled runtime execution plan](adr/0012-preflight-compiled-runtime-execution-plan.md)
- [ADR 0016: Node-scoped Git workspace isolation (Experimental)](adr/0016-node-scoped-git-workspace-isolation.md)

Key architectural constraints:

- Providers communicate through artifacts under `.crewplane/`, not hidden
  shared in-memory state.
- Provider integration is CLI-first.
- Runtime execution consumes compiled preflight plans.
- Workspace isolation is Experimental, optional Git-backed source-tree
  isolation, not sandboxing.
- Adapter boundaries keep invoker, UI, and artifact implementations replaceable.
