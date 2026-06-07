import unittest

from orchestrator_cli.runtime.agent.usage import InvocationUsage


class InvocationUsageModelTests(unittest.IsolatedAsyncioTestCase):
    def test_invocation_usage_provider_tokens_are_read_only_snapshot(self) -> None:
        provider_tokens = {"input": 12, "output": 4}

        usage = InvocationUsage(
            attempt_count=1,
            cli_captured=True,
            output_extraction_status="success",
            provider_usage_status="full",
            provider_tokens=provider_tokens,
            visible_estimate_tokens=16,
            visible_estimate_method="char-count-lower-bound",
            visible_estimate_is_lower_bound=True,
            configured_cost_usd=None,
            invocation_cost_confidence="full",
            usage_parse_error=None,
        )
        provider_tokens["input"] = 99

        self.assertEqual(usage.provider_tokens["input"], 12)
        with self.assertRaises(TypeError):
            usage.provider_tokens["input"] = 99
