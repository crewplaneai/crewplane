Use explicit type hints for public APIs and non-trivial logic. Fail loudly on
invalid state. Keep provider handoffs auditable through `.orchestrator/`
artifacts, and prefer deterministic filesystem-local tests over live provider
calls.
