import unittest

from orchestrator_cli.architecture.contracts import InvocationContext
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.versions import CONFIG_SCHEMA_VERSION


class MockInvokerAdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def _build_config(self) -> Config:
        return Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={"alpha": AgentConfig(cli_cmd=["echo"], default_model="model-a")},
        )

    @staticmethod
    def _options(**overrides: object) -> dict[str, object]:
        return {"observation_delay_seconds": 0, **overrides}

    @staticmethod
    def _context(
        role: str = "executor",
        task_id: str | None = None,
        audit_round_num: int | None = None,
        round_num: int = 1,
        findings_enabled: bool = False,
    ) -> InvocationContext:
        resolved_task_id = task_id or f"alpha_{role}_0"
        return InvocationContext(
            node_id="node.a",
            task_id=resolved_task_id,
            provider="alpha",
            role=role,
            audit_round_num=audit_round_num,
            round_num=round_num,
            findings_enabled=findings_enabled,
        )
