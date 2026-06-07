from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.types import InvocationContext

from .context import ContextDisplay, context_display, is_reviewer_context
from .fixtures import fixture_candidates
from .logging import write_invocation_log
from .mutations import apply_fixture_mutations, build_fixture_mutation_plan
from .options import MockOptions
from .outputs import (
    OutputResolution,
    build_findings_lines,
    review_contract_resolution,
)


class MockAgentInvoker:
    def __init__(self, options: MockOptions) -> None:
        self._options = options

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by callback or protocol signature.
        model: str | None,  # noqa: ARG002 - Required by callback or protocol signature.
        prompt: str,
        output_file: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        await asyncio.sleep(self._options.delay_seconds)
        self._raise_if_forced_failure(invocation_context)
        resolution = await self._resolve_output(prompt, invocation_context)
        await asyncio.to_thread(
            self._write_invocation_artifacts,
            resolution,
            output_file,
            log_file,
            invocation_context,
        )

    def _raise_if_forced_failure(self, context: InvocationContext | None) -> None:
        for selector in self._options.fail_when:
            if selector.matches(context):
                raise RuntimeError(
                    f"mock invoker forced failure by selector: {selector.summary()}"
                )

    async def _resolve_output(
        self, prompt: str, context: InvocationContext | None
    ) -> OutputResolution:
        if self._options.observation_delay_seconds:
            await asyncio.sleep(self._options.observation_delay_seconds)

        match self._options.output_mode:
            case "echo":
                if is_reviewer_context(context):
                    return review_contract_resolution("echo_review_contract")
                return OutputResolution(content=prompt, source="echo")
            case "lorem":
                if is_reviewer_context(context):
                    return review_contract_resolution("lorem_review_contract")
                return OutputResolution(
                    content=self._build_lorem_markdown(prompt, context),
                    source="lorem",
                )
            case "file":
                return await asyncio.to_thread(
                    self._resolve_file_mode,
                    prompt,
                    context,
                )

    def _resolve_file_mode(
        self, prompt: str, context: InvocationContext | None
    ) -> OutputResolution:
        output_dir = self._options.output_dir
        if output_dir is None:
            raise RuntimeError(
                "mock invoker internal error: output_dir missing for file mode"
            )
        for candidate in fixture_candidates(output_dir, context):
            if candidate.is_file():
                return OutputResolution(
                    content=candidate.read_text(encoding="utf-8"),
                    source="fixture",
                    fixture_path=candidate,
                )
        if self._options.strict_file_mode:
            raise RuntimeError(
                "mock invoker file mode could not resolve fixture; looked in "
                f"{output_dir.resolve()}"
            )
        if is_reviewer_context(context):
            return review_contract_resolution("fallback_review_contract")
        return OutputResolution(
            content=self._build_lorem_markdown(prompt, context),
            source="fallback_lorem",
        )

    def _build_lorem_markdown(
        self, prompt: str, context: InvocationContext | None
    ) -> str:
        display = context_display(context)
        marker = self._seed_marker(display)

        lines = [
            "# Mock Invocation Output",
            "",
            f"- Node: {display.node_id}",
            f"- Task: {display.task_id}",
            f"- Provider: {display.provider}",
            f"- Role: {display.role}",
            f"- Audit Round: {display.audit_round_display}",
            f"- Round: {display.round_display}",
        ]
        if self._options.seed is not None:
            lines.append(f"- Seed Marker: {marker}")
        lines.extend(
            [
                "",
                "## Summary",
                "Synthetic output generated for deterministic local orchestration checks.",
                "",
                "## Notes",
                f"- Prompt length: {len(prompt)} characters",
                "- Behavior path: mock invoker lorem mode",
                "",
                "## Next Steps",
                "1. Verify downstream template substitution.",
                "2. Validate node and invocation state transitions.",
            ]
        )
        lines.extend(build_findings_lines(context))
        return "\n".join(lines) + "\n"

    def _write_invocation_artifacts(
        self,
        resolution: OutputResolution,
        output_file: Path,
        log_file: Path | None,
        context: InvocationContext | None,
    ) -> None:
        mutation_plan = build_fixture_mutation_plan(resolution, output_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(resolution.content, encoding="utf-8")
        apply_fixture_mutations(mutation_plan)
        write_invocation_log(self._options, log_file, output_file, context, resolution)

    def _seed_marker(self, display: ContextDisplay) -> str:
        seed_value = self._options.seed if self._options.seed is not None else "no-seed"
        digest = hashlib.sha256(
            (
                f"{seed_value}|{display.node_id}|{display.task_id}|{display.provider}|"
                f"{display.role}|{display.audit_round_display}|{display.round_display}"
            ).encode()
        ).hexdigest()
        return digest[:12]
