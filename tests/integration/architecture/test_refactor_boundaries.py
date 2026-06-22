from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
PRODUCTION_LINE_LIMIT = 500

ORIGINAL_OVERSIZED_FILES = (
    "src/orchestrator_cli/core/workflow_markdown.py",
    "src/orchestrator_cli/core/workflow_composition.py",
    "src/orchestrator_cli/cli/workflow_runner.py",
    "src/orchestrator_cli/observability/events.py",
    "src/orchestrator_cli/observability/persistent.py",
    "src/orchestrator_cli/observability/render.py",
    "src/orchestrator_cli/artifacts/result_writer.py",
    "src/orchestrator_cli/runtime/agent/quota.py",
    "src/orchestrator_cli/runtime/agent/failures.py",
    "src/orchestrator_cli/runtime/execution/consensus.py",
    "src/orchestrator_cli/runtime/execution/common.py",
    "src/orchestrator_cli/adapters/invokers/mock.py",
)

SPLIT_MODULES = (
    "src/orchestrator_cli/core/review_contract.py",
    "src/orchestrator_cli/core/workflow_markdown/models.py",
    "src/orchestrator_cli/core/workflow_markdown/frontmatter.py",
    "src/orchestrator_cli/core/workflow_markdown/sections.py",
    "src/orchestrator_cli/core/workflow_markdown/markers.py",
    "src/orchestrator_cli/core/workflow_markdown/payloads.py",
    "src/orchestrator_cli/core/workflow_composition/models.py",
    "src/orchestrator_cli/core/workflow_composition/parsing.py",
    "src/orchestrator_cli/core/workflow_composition/imports.py",
    "src/orchestrator_cli/core/workflow_composition/nodes.py",
    "src/orchestrator_cli/core/workflow_composition/rewrites.py",
    "src/orchestrator_cli/core/workflow_composition/traversal.py",
    "src/orchestrator_cli/cli/run/context.py",
    "src/orchestrator_cli/cli/run/preflight.py",
    "src/orchestrator_cli/cli/run/manifest.py",
    "src/orchestrator_cli/cli/run/components.py",
    "src/orchestrator_cli/cli/run/observability.py",
    "src/orchestrator_cli/cli/run/execution.py",
    "src/orchestrator_cli/cli/run/topology.py",
    "src/orchestrator_cli/observability/events/types.py",
    "src/orchestrator_cli/observability/events/payloads.py",
    "src/orchestrator_cli/observability/events/execution_event.py",
    "src/orchestrator_cli/observability/events/builders.py",
    "src/orchestrator_cli/observability/events/dashboard_state.py",
    "src/orchestrator_cli/observability/events/reducer.py",
    "src/orchestrator_cli/observability/events/log.py",
    "src/orchestrator_cli/observability/run_summary/logger.py",
    "src/orchestrator_cli/observability/run_summary/models.py",
    "src/orchestrator_cli/observability/run_summary/builder.py",
    "src/orchestrator_cli/observability/run_summary/issues.py",
    "src/orchestrator_cli/observability/run_summary/spend.py",
    "src/orchestrator_cli/observability/run_summary/markdown.py",
    "src/orchestrator_cli/observability/run_summary/terminal.py",
    "src/orchestrator_cli/observability/run_summary/formatting.py",
    "src/orchestrator_cli/observability/render/viewport.py",
    "src/orchestrator_cli/observability/render/header.py",
    "src/orchestrator_cli/observability/render/timeline.py",
    "src/orchestrator_cli/observability/render/cells.py",
    "src/orchestrator_cli/observability/render/text.py",
    "src/orchestrator_cli/observability/text_layout.py",
    "src/orchestrator_cli/artifacts/result_selection.py",
    "src/orchestrator_cli/artifacts/review_loop_status.py",
    "src/orchestrator_cli/artifacts/stage_result_document.py",
    "src/orchestrator_cli/artifacts/findings_extraction.py",
    "src/orchestrator_cli/artifacts/stage_output_aggregation.py",
    "src/orchestrator_cli/runtime/agent/quota/lexicons.py",
    "src/orchestrator_cli/runtime/agent/quota/parser_resolution.py",
    "src/orchestrator_cli/runtime/agent/quota/evidence.py",
    "src/orchestrator_cli/runtime/agent/quota/waits.py",
    "src/orchestrator_cli/runtime/agent/quota/classifier.py",
    "src/orchestrator_cli/runtime/agent/failures/types.py",
    "src/orchestrator_cli/runtime/agent/failures/patterns.py",
    "src/orchestrator_cli/runtime/agent/failures/evidence.py",
    "src/orchestrator_cli/runtime/agent/failures/classifier.py",
    "src/orchestrator_cli/runtime/agent/failures/formatting.py",
    "src/orchestrator_cli/runtime/agent/process/diagnostics.py",
    "src/orchestrator_cli/runtime/agent/process/runner.py",
    "src/orchestrator_cli/runtime/agent/process/signals.py",
    "src/orchestrator_cli/runtime/agent/process/streams.py",
    "src/orchestrator_cli/runtime/agent/invocation/command.py",
    "src/orchestrator_cli/runtime/agent/invocation/loop.py",
    "src/orchestrator_cli/runtime/agent/invocation/output.py",
    "src/orchestrator_cli/runtime/agent/invocation/retry.py",
    "src/orchestrator_cli/runtime/agent/invocation/state.py",
    "src/orchestrator_cli/runtime/agent/invocation/telemetry.py",
    "src/orchestrator_cli/runtime/agent/invocation/transitions.py",
    "src/orchestrator_cli/runtime/execution/structured_review.py",
    "src/orchestrator_cli/runtime/execution/review_fingerprints.py",
    "src/orchestrator_cli/runtime/execution/plain_language_review.py",
    "src/orchestrator_cli/runtime/execution/review_consensus.py",
    "src/orchestrator_cli/runtime/execution/review_types.py",
    "src/orchestrator_cli/runtime/execution/runtime_context.py",
    "src/orchestrator_cli/runtime/execution/execution_activity.py",
    "src/orchestrator_cli/runtime/execution/execution_console.py",
    "src/orchestrator_cli/runtime/execution/execution_events.py",
    "src/orchestrator_cli/runtime/execution/fragment_assembler.py",
    "src/orchestrator_cli/runtime/execution/prompt_budgeting.py",
    "src/orchestrator_cli/runtime/execution/stage_tasks.py",
    "src/orchestrator_cli/runtime/execution/provider_invocation.py",
    "src/orchestrator_cli/runtime/execution/provider_display.py",
    "src/orchestrator_cli/runtime/execution/review_loop/audit_round.py",
    "src/orchestrator_cli/runtime/execution/review_loop/drift.py",
    "src/orchestrator_cli/runtime/execution/review_loop/drift_detection.py",
    "src/orchestrator_cli/runtime/execution/review_loop/drift_events.py",
    "src/orchestrator_cli/runtime/execution/review_loop/executor_round.py",
    "src/orchestrator_cli/runtime/execution/review_loop/policy.py",
    "src/orchestrator_cli/runtime/execution/review_loop/prompts.py",
    "src/orchestrator_cli/runtime/execution/review_loop/reviewer_round.py",
    "src/orchestrator_cli/runtime/execution/review_loop/rounds.py",
    "src/orchestrator_cli/runtime/execution/review_loop/state.py",
    "src/orchestrator_cli/runtime/execution/review_loop/types.py",
    "src/orchestrator_cli/runtime/execution/review_loop/validation.py",
    "src/orchestrator_cli/runtime/execution/stage_finalize_events.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/context.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/fixtures.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/invoker.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/logging.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/mutations.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/options.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/outputs.py",
    "src/orchestrator_cli/adapters/invokers/mock_invoker/selectors.py",
)

LEGACY_EVENT_FIELDS = {
    "node_id",
    "provider",
    "role",
    "model",
    "task_id",
    "audit_round_num",
    "round_num",
    "output_file",
    "log_file",
    "log_presentation_format",
    "log_presentation_profile",
    "duration_ms",
    "error",
    "attempt_count",
    "cli_captured",
    "output_extraction_status",
    "provider_usage_status",
    "provider_tokens",
    "visible_estimate_tokens",
    "visible_estimate_method",
    "visible_estimate_is_lower_bound",
    "configured_cost_usd",
    "invocation_cost_confidence",
    "usage_parse_error",
    "failure_kind",
    "failure_phase",
    "failure_source",
    "failure_advice",
    "level",
    "message",
    "operation",
    "attributes",
}


def python_files(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def physical_line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def parse_python(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def is_single_underscore_name(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def expression_chain(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*expression_chain(node.value), node.attr)
    return ()


def module_name_for_path(path: Path) -> str:
    if path.is_relative_to(SRC_ROOT):
        return ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if path.is_relative_to(TESTS_ROOT):
        return ".".join(("tests", *path.relative_to(TESTS_ROOT).with_suffix("").parts))
    raise ValueError(f"Unsupported Python path: {path}")


def import_from_module_name(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    module_parts = module_name_for_path(path).split(".")
    package_parts = module_parts[:-1]
    keep_count = max(0, len(package_parts) - node.level + 1)
    base_parts = package_parts[:keep_count]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def is_repo_module_name(module_name: str) -> bool:
    return (
        module_name == "orchestrator_cli"
        or module_name.startswith("orchestrator_cli.")
        or module_name == "tests"
        or module_name.startswith("tests.")
    )


def imported_repo_aliases(path: Path, module: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_repo_module_name(alias.name):
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module_name = import_from_module_name(path, node)
            if not is_repo_module_name(module_name):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported_name = alias.asname or alias.name
                if imported_name[:1].islower():
                    aliases.add(imported_name)
    return aliases


def is_repo_import_expression(node: ast.AST, imported_aliases: set[str]) -> bool:
    chain = expression_chain(node)
    if not chain:
        return False
    return chain[0] in imported_aliases or chain[0] in {"orchestrator_cli", "tests"}


def string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def has_private_dotted_part(value: str) -> bool:
    return any(is_single_underscore_name(part) for part in value.split("."))


def test_original_oversized_files_are_small_or_deleted() -> None:
    offenders = [
        f"{relative_path}: {physical_line_count(REPO_ROOT / relative_path)}"
        for relative_path in ORIGINAL_OVERSIZED_FILES
        if (REPO_ROOT / relative_path).exists()
        and physical_line_count(REPO_ROOT / relative_path) > 200
    ]
    assert offenders == []


def test_split_modules_stay_under_line_limit() -> None:
    offenders = [
        f"{relative_path}: {physical_line_count(REPO_ROOT / relative_path)}"
        for relative_path in SPLIT_MODULES
        if (REPO_ROOT / relative_path).exists()
        and physical_line_count(REPO_ROOT / relative_path) > 400
    ]
    assert offenders == []


def test_all_production_modules_stay_under_line_limit() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {physical_line_count(path)}"
        for path in python_files(SRC_ROOT / "orchestrator_cli")
        if physical_line_count(path) > PRODUCTION_LINE_LIMIT
    ]
    assert offenders == []


def test_adapters_do_not_import_runtime_execution_modules() -> None:
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "orchestrator_cli" / "adapters"):
        module = parse_python(path)
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("orchestrator_cli.runtime.execution"):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("orchestrator_cli.runtime.execution")
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_architecture_ports_do_not_import_runtime_or_observability() -> None:
    forbidden_prefixes = (
        "orchestrator_cli.core.preflight.runtime_config",
        "orchestrator_cli.runtime",
        "orchestrator_cli.observability",
    )
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "orchestrator_cli" / "architecture" / "ports"):
        module = parse_python(path)
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(forbidden_prefixes)
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_runtime_modules_do_not_own_module_level_console_singletons() -> None:
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "orchestrator_cli" / "runtime"):
        module = parse_python(path)
        for node in module.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            if value is None or not isinstance(value, ast.Call):
                continue
            if call_name(value.func) != "Console":
                continue
            offenders.extend(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}" for _ in targets
            )
    assert offenders == []


def test_cli_modules_do_not_own_module_level_console_singletons() -> None:
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "orchestrator_cli" / "cli"):
        module = parse_python(path)
        for node in module.body:
            value: ast.expr | None
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
            else:
                continue
            if isinstance(value, ast.Call) and call_name(value.func) == "Console":
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_no_cross_module_single_underscore_imports() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in python_files(root):
            module = parse_python(path)
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if any(is_single_underscore_name(part) for part in parts):
                            offenders.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.module and any(
                        is_single_underscore_name(part)
                        for part in node.module.split(".")
                    ):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
                    for alias in node.names:
                        if is_single_underscore_name(alias.name):
                            offenders.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                            )
    assert offenders == []


def test_no_cross_module_single_underscore_attribute_access() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in python_files(root):
            module = parse_python(path)
            imported_aliases = imported_repo_aliases(path, module)
            for node in ast.walk(module):
                if not isinstance(node, ast.Attribute):
                    continue
                if not is_single_underscore_name(node.attr):
                    continue
                if not is_repo_import_expression(node.value, imported_aliases):
                    continue
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {'.'.join(expression_chain(node))}"
                )
    assert offenders == []


def test_source_does_not_suppress_unused_arguments() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}"
        for path in python_files(SRC_ROOT / "orchestrator_cli")
        if "noqa: ARG002" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_private_patch_targets() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in python_files(root):
            module = parse_python(path)
            imported_aliases = imported_repo_aliases(path, module)
            for node in ast.walk(module):
                if not isinstance(node, ast.Call):
                    continue
                call_chain = expression_chain(node.func)
                if call_chain and call_chain[-1] == "patch" and node.args:
                    target = string_value(node.args[0])
                    if target is not None and has_private_dotted_part(target):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target}"
                        )
                if (
                    call_chain
                    and call_chain[-1] == "setattr"
                    and len(node.args) >= 2
                    and is_repo_import_expression(node.args[0], imported_aliases)
                ):
                    target_name = string_value(node.args[1])
                    if target_name is not None and is_single_underscore_name(
                        target_name
                    ):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target_name}"
                        )
                if (
                    len(call_chain) >= 2
                    and call_chain[-2:] == ("patch", "object")
                    and len(node.args) >= 2
                    and is_repo_import_expression(node.args[0], imported_aliases)
                ):
                    target_name = string_value(node.args[1])
                    if target_name is not None and is_single_underscore_name(
                        target_name
                    ):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target_name}"
                        )
    assert offenders == []


def test_pep561_marker_is_packaged() -> None:
    marker = SRC_ROOT / "orchestrator_cli" / "py.typed"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert marker.is_file()
    assert '"src/orchestrator_cli/py.typed"' in pyproject


def test_review_contract_has_neutral_imports() -> None:
    path = SRC_ROOT / "orchestrator_cli" / "core" / "review_contract.py"
    forbidden_prefixes = (
        "orchestrator_cli.runtime",
        "orchestrator_cli.adapters",
        "orchestrator_cli.artifacts",
        "orchestrator_cli.observability",
    )
    offenders: list[str] = []
    module = parse_python(path)
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(forbidden_prefixes):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(forbidden_prefixes)
        ):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_execution_events_do_not_use_legacy_flat_fields() -> None:
    offenders: list[str] = []
    for root in (SRC_ROOT, TESTS_ROOT):
        for path in python_files(root):
            module = parse_python(path)
            for node in ast.walk(module):
                if (
                    not isinstance(node, ast.Call)
                    or call_name(node.func) != "ExecutionEvent"
                ):
                    continue
                legacy_keywords = {
                    keyword.arg
                    for keyword in node.keywords
                    if keyword.arg in LEGACY_EVENT_FIELDS
                }
                if legacy_keywords:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {sorted(legacy_keywords)}"
                    )
    assert offenders == []


def test_execution_event_has_no_legacy_flat_accessors() -> None:
    path = (
        SRC_ROOT
        / "orchestrator_cli"
        / "observability"
        / "events"
        / "execution_event.py"
    )
    module = parse_python(path)
    legacy_properties: list[str] = []
    for node in ast.walk(module):
        if (
            not isinstance(node, ast.FunctionDef)
            or node.name not in LEGACY_EVENT_FIELDS
        ):
            continue
        if any(call_name(decorator) == "property" for decorator in node.decorator_list):
            legacy_properties.append(node.name)
    assert legacy_properties == []


def test_runtime_code_uses_event_builders_instead_of_direct_event_construction() -> (
    None
):
    offenders: list[str] = []
    runtime_root = SRC_ROOT / "orchestrator_cli" / "runtime"
    for path in python_files(runtime_root):
        module = parse_python(path)
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and call_name(node.func) == "ExecutionEvent":
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_runtime_does_not_infer_provider_behavior_from_executable_names() -> None:
    forbidden_names = {
        "AUTO_QUOTA_PARSER_PROVIDER_BY_EXECUTABLE",
        "parser_resolution",
    }
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "orchestrator_cli" / "runtime" / "agent"):
        source = path.read_text(encoding="utf-8")
        for forbidden_name in forbidden_names:
            if forbidden_name in source:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {forbidden_name}")
        module = parse_python(path)
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module == "os.path":
                imported_names = {alias.name for alias in node.names}
                if "basename" in imported_names:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "basename"
                and expression_chain(node.value) in {("os", "path"), ("posixpath",)}
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_log_presentation_does_not_import_runtime_output_extractors() -> None:
    forbidden_modules = {
        "orchestrator_cli.runtime.agent.invocation.output",
        "orchestrator_cli.runtime.agent.invocation.claude_json",
        "orchestrator_cli.runtime.agent.usage_parsing",
        "orchestrator_cli.runtime.agent.quota",
        "orchestrator_cli.runtime.agent.failures",
    }
    offenders: list[str] = []
    presentation_root = (
        SRC_ROOT / "orchestrator_cli" / "observability" / "log_presentation"
    )
    for path in python_files(presentation_root):
        module = parse_python(path)
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom):
                module_name = import_from_module_name(path, node)
                if any(
                    module_name == forbidden or module_name.startswith(f"{forbidden}.")
                    for forbidden in forbidden_modules
                ):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(
                        alias.name == forbidden
                        or alias.name.startswith(f"{forbidden}.")
                        for forbidden in forbidden_modules
                    ):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_runtime_and_tmux_do_not_infer_log_presentation_from_provider_names() -> None:
    provider_literals = {"claude", "codex", "copilot", "gemini", "kilo"}
    checked_roots = [
        SRC_ROOT / "orchestrator_cli" / "runtime" / "execution",
        SRC_ROOT / "orchestrator_cli" / "observability" / "tmux",
    ]
    offenders: list[str] = []
    for root in checked_roots:
        for path in python_files(root):
            source = path.read_text(encoding="utf-8")
            for provider_literal in provider_literals:
                if (
                    f'"{provider_literal}"' in source
                    or f"'{provider_literal}'" in source
                ):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}: {provider_literal}"
                    )
    assert offenders == []


def test_process_stream_capture_uses_persisted_files_and_bounded_tails() -> None:
    streams_path = (
        SRC_ROOT / "orchestrator_cli" / "runtime" / "agent" / "process" / "streams.py"
    )
    capture_path = (
        SRC_ROOT
        / "orchestrator_cli"
        / "runtime"
        / "agent"
        / "process"
        / "stream_capture.py"
    )
    streams_source = streams_path.read_text(encoding="utf-8")
    capture_source = capture_path.read_text(encoding="utf-8")
    assert "asyncio.Queue(maxsize=LOG_QUEUE_MAX_ITEMS)" in streams_source
    assert "stdout_chunks" not in streams_source
    assert "stderr_chunks" not in streams_source
    assert "tempfile.mkstemp" in capture_source
    assert "MAX_CAPTURE_MEMORY_BYTES" in capture_source
    assert "tail_bytes" in capture_source
    assert "SpooledTemporaryFile" not in streams_source
    assert "SpooledTemporaryFile" not in capture_source


def test_provider_invocation_fallback_usage_does_not_materialize_artifact() -> None:
    path = (
        SRC_ROOT
        / "orchestrator_cli"
        / "runtime"
        / "execution"
        / "provider_invocation.py"
    )
    events_path = (
        SRC_ROOT
        / "orchestrator_cli"
        / "runtime"
        / "execution"
        / "provider_invocation_events.py"
    )
    source = path.read_text(encoding="utf-8")
    events_source = events_path.read_text(encoding="utf-8")
    assert ".read_text(" not in source
    assert ".read_text(" not in events_source
    assert "build_fallback_usage_from_output_file" in source + events_source


def test_persistent_run_logger_retains_bounded_event_window() -> None:
    path = SRC_ROOT / "orchestrator_cli" / "observability" / "run_summary" / "logger.py"
    source = path.read_text(encoding="utf-8")
    assert "MAX_RETAINED_SUMMARY_EVENTS" in source
    assert "deque(maxlen=MAX_RETAINED_SUMMARY_EVENTS)" in source
    assert "list[ExecutionEvent]" not in source


def test_run_summary_retains_bounded_invocation_usage_details() -> None:
    accumulator_path = (
        SRC_ROOT
        / "orchestrator_cli"
        / "observability"
        / "run_summary"
        / "accumulator.py"
    )
    markdown_path = (
        SRC_ROOT / "orchestrator_cli" / "observability" / "run_summary" / "markdown.py"
    )
    accumulator_source = accumulator_path.read_text(encoding="utf-8")
    markdown_source = markdown_path.read_text(encoding="utf-8")
    assert "MAX_RETAINED_INVOCATION_USAGE_DETAILS" in accumulator_source
    assert "deque[InvocationUsageSummary]" in accumulator_source
    assert "maxlen=MAX_RETAINED_INVOCATION_USAGE_DETAILS" in accumulator_source
    assert "UsageRollupAccumulator" in accumulator_source
    assert "list[InvocationUsageSummary]" not in accumulator_source
    assert "omitted_invocation_usage_count" in markdown_source
    assert "Full invocation events remain" in markdown_source


def test_docs_and_templates_do_not_reference_legacy_prompt_config_fields() -> None:
    legacy_terms = {
        "prompt_arg",
        "quota_parser",
        "stdin_prompt_arg",
        "use_stdin",
    }
    checked_paths = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "architecture" / "modular-orchestration-architecture.md",
        SRC_ROOT / "orchestrator_cli" / "example_templates" / "config.yml",
    ]
    offenders: list[str] = []
    for path in checked_paths:
        source = path.read_text(encoding="utf-8")
        for term in legacy_terms:
            if term in source:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {term}")
    assert offenders == []


def test_version_catalog_has_single_public_python_source() -> None:
    stale_paths = [
        SRC_ROOT / "orchestrator_cli" / "versions.py",
        SRC_ROOT / "orchestrator_cli" / "core" / "versions.py",
        SRC_ROOT / "orchestrator_cli" / "architecture" / "api_version.py",
    ]
    assert [
        path.relative_to(REPO_ROOT).as_posix() for path in stale_paths if path.exists()
    ] == []

    offenders: list[str] = []
    stale_imports = {
        "orchestrator_cli.architecture.api_version",
        "orchestrator_cli.core.versions",
        "orchestrator_cli.versions",
    }
    for path in python_files(SRC_ROOT / "orchestrator_cli"):
        source = path.read_text(encoding="utf-8")
        for stale_import in stale_imports:
            if stale_import in source:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {stale_import}")
    assert offenders == []


def test_public_package_exports_are_narrow() -> None:
    import orchestrator_cli.core as core_package
    import orchestrator_cli.runtime as runtime_package

    assert core_package.__all__ == ["SCHEMA_VERSION"]
    assert runtime_package.__all__ == []
    assert "__getattr__" not in vars(runtime_package)


def test_output_manager_directory_fields_are_read_only_properties() -> None:
    path = SRC_ROOT / "orchestrator_cli" / "artifacts" / "manager.py"
    module = parse_python(path)
    managed_fields = {
        "base_dir",
        "log_cli_output",
        "logs_dir",
        "results_dir",
        "run_id",
        "stages_dir",
        "task_name",
    }
    assigned_fields: list[str] = []
    property_fields: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or node.name != "OutputManager":
            continue
        for member in node.body:
            if (
                isinstance(member, ast.FunctionDef)
                and member.name in managed_fields
                and any(
                    call_name(decorator) == "property"
                    for decorator in member.decorator_list
                )
            ):
                property_fields.add(member.name)
            if not isinstance(member, ast.FunctionDef) or member.name != "__init__":
                continue
            for child in ast.walk(member):
                targets: list[ast.expr] = []
                if isinstance(child, ast.Assign):
                    targets = list(child.targets)
                elif isinstance(child, ast.AnnAssign):
                    targets = [child.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr in managed_fields
                    ):
                        assigned_fields.append(target.attr)
    assert assigned_fields == []
    assert property_fields == managed_fields


def test_preflight_fingerprint_key_state_is_scoped() -> None:
    secrets_path = SRC_ROOT / "orchestrator_cli" / "core" / "preflight" / "secrets.py"
    compile_state_path = (
        SRC_ROOT / "orchestrator_cli" / "core" / "preflight" / "compile_state.py"
    )
    secrets_source = secrets_path.read_text(encoding="utf-8")
    compile_state_source = compile_state_path.read_text(encoding="utf-8")
    assert "_EPHEMERAL_KEYS" not in secrets_source
    assert "class FingerprintKeyCache" in secrets_source
    assert "fingerprint_key_cache: FingerprintKeyCache" in compile_state_source


def test_boundary_option_contracts_use_json_object() -> None:
    checked_paths = [
        SRC_ROOT / "orchestrator_cli" / "architecture",
        SRC_ROOT / "orchestrator_cli" / "bootstrap",
        SRC_ROOT / "orchestrator_cli" / "adapters",
    ]
    offenders = [
        f"{path.relative_to(REPO_ROOT)}"
        for root in checked_paths
        for path in python_files(root)
        if "dict[str, Any]" in path.read_text(encoding="utf-8")
        or "dict[str, object]" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_preflight_any_maps_are_limited_to_redaction_traversal() -> None:
    allowed_path = (
        SRC_ROOT
        / "orchestrator_cli"
        / "core"
        / "preflight"
        / "runtime_config_redaction.py"
    )
    offenders: list[str] = []
    for path in python_files(SRC_ROOT / "orchestrator_cli" / "core" / "preflight"):
        source = path.read_text(encoding="utf-8")
        if "dict[str, Any]" not in source:
            continue
        if path != allowed_path:
            offenders.append(f"{path.relative_to(REPO_ROOT)}")
            continue
        module = parse_python(path)
        docstring = ast.get_docstring(module) or ""
        if "arbitrary JSON-compatible config snapshots" not in docstring:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: missing rationale")
    assert offenders == []


def test_review_loop_status_error_is_public() -> None:
    from orchestrator_cli.artifacts.review_loop_status import ReviewLoopStatusError

    assert ReviewLoopStatusError.__name__ == "ReviewLoopStatusError"


def test_provider_call_request_does_not_carry_display_state() -> None:
    from dataclasses import fields

    from orchestrator_cli.runtime.execution.provider_invocation import (
        ProviderCallRequest,
    )

    request_fields = {field.name for field in fields(ProviderCallRequest)}
    assert "progress_description" not in request_fields
    assert "show_console_summary" not in request_fields
