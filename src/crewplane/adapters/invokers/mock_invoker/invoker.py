from __future__ import annotations

import asyncio
from pathlib import Path

from crewplane.architecture.contracts import (
    InvocationContext,
    LogPresentationDescriptor,
    MockInvokerOptions,
)
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.failures import (
    InvocationFailureError,
    InvocationFailureSummary,
)

from .context import is_reviewer_context
from .fixtures import fixture_candidates
from .logging import write_invocation_log
from .mutations import apply_fixture_mutations, build_fixture_mutation_plan
from .outputs import (
    OutputResolution,
    build_lorem_markdown,
    review_contract_resolution,
)
from .selectors import selector_matches, selector_summary


def _mock_invocation_failure(message: str) -> InvocationFailureError:
    summary = InvocationFailureSummary(
        kind="provider_error",
        phase="provider_transport",
        source="none",
        message=message,
        advice="The mock invoker reported a deterministic provider failure.",
        condensed=False,
    )
    return InvocationFailureError("mock invoker failed", summary, None)


class MockAgentInvoker:
    """Deterministic, filesystem-local invoker for tests and first-run workflows."""

    def __init__(self, options: MockInvokerOptions) -> None:
        self._options = options

    def log_presentation_for(
        self,
        config: AgentConfig,
    ) -> LogPresentationDescriptor | None:
        self._validate_invocation_request(config, model=None)
        return LogPresentationDescriptor(format="json_lines", profile="mock")

    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        self._validate_invocation_request(config, model)
        await asyncio.sleep(self._options.delay_seconds)
        self._raise_if_forced_failure(invocation_context)
        resolution = await self._resolve_output(prompt, invocation_context)
        await asyncio.to_thread(
            self._write_invocation_artifacts,
            resolution,
            output_file,
            cwd,
            prompt,
            log_file,
            invocation_context,
        )

    def _validate_invocation_request(
        self, config: AgentConfig, model: str | None
    ) -> None:
        if not isinstance(config, AgentConfig):
            raise TypeError("config must be an AgentConfig instance")
        if model is not None and not isinstance(model, str):
            raise TypeError("model must be a string or None")

    def _raise_if_forced_failure(self, context: InvocationContext | None) -> None:
        for selector in self._options.fail_when:
            if selector_matches(selector, context):
                raise _mock_invocation_failure(
                    f"forced failure by selector: {selector_summary(selector)}"
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
                    content=build_lorem_markdown(
                        prompt,
                        context,
                        self._options.seed,
                    ),
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
        output_dir_path = Path(output_dir)
        for candidate in fixture_candidates(output_dir_path, context):
            if candidate.is_file():
                return OutputResolution(
                    content=candidate.read_text(encoding="utf-8"),
                    source="fixture",
                    fixture_path=candidate,
                )
        if self._options.strict_file_mode:
            raise _mock_invocation_failure(
                "file mode could not resolve fixture; looked in "
                f"{output_dir_path.resolve()}"
            )
        if is_reviewer_context(context):
            return review_contract_resolution("fallback_review_contract")
        return OutputResolution(
            content=build_lorem_markdown(
                prompt,
                context,
                self._options.seed,
            ),
            source="fallback_lorem",
        )

    def _write_invocation_artifacts(
        self,
        resolution: OutputResolution,
        output_file: Path,
        cwd: Path,
        prompt: str,
        log_file: Path | None,
        context: InvocationContext | None,
    ) -> None:
        mutation_plan = build_fixture_mutation_plan(
            resolution,
            output_file,
            cwd,
            context,
            prompt,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(resolution.content, encoding="utf-8")
        apply_fixture_mutations(mutation_plan)
        write_invocation_log(
            self._options,
            log_file,
            output_file,
            cwd,
            context,
            resolution,
        )
