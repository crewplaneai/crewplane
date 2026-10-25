from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from crewplane.adapters.invokers.cli_invoker import (
    CliDiscoveredEndpoint,
    CliDiscoveredModels,
    CliDiscoveryRequest,
    CliProviderDiscovery,
    run_command_once,
    unconfirmed_process_cleanup,
)

from .command import cleanup_temporary_captures
from .discovery_platform import discover_client, endpoint_candidates, running_in_wsl
from .discovery_worker import DiscoveryReport, DiscoveryTask


async def discover(request: CliDiscoveryRequest) -> CliProviderDiscovery:
    diagnostics = []
    try:
        command = discover_client(request)
        candidates = endpoint_candidates(request)
    except (OSError, ValueError) as exc:
        return CliProviderDiscovery((), (), (str(exc),))
    if platform.system() == "Windows":
        diagnostics.append(
            "Native Windows detection is available; Crewplane native Windows execution acceptance is still pending."
        )
    if not command:
        diagnostics.append(
            "crewplane-ollama-agent not found. Install crewplane-ollama in Crewplane's Python environment, then rerun onboarding."
        )
    if running_in_wsl(request):
        diagnostics.append(
            "WSL checks loopback and the default-route Windows host unless OLLAMA_HOST is set. For Windows NAT access, configure Ollama's listen address and Windows firewall on the host; Crewplane does not change them."
        )
    report = await run_discovery(
        DiscoveryTask(
            operation="endpoints",
            endpoints=candidates,
            timeout_seconds=request.timeout_seconds,
        ),
        request.project_root,
    )
    diagnostics.extend(report.diagnostics)
    endpoints = tuple(
        CliDiscoveredEndpoint(endpoint, {"OLLAMA_HOST": endpoint})
        for endpoint in report.values
    )
    if "OLLAMA_HOST" in request.environment and not endpoints:
        diagnostics.append(
            "Explicit OLLAMA_HOST failed; no other endpoint was tried. Correct it and rerun onboarding."
        )
    return CliProviderDiscovery(command, tuple(endpoints), tuple(diagnostics))


async def list_models(
    request: CliDiscoveryRequest, endpoint: CliDiscoveredEndpoint
) -> CliDiscoveredModels:
    report = await run_discovery(
        DiscoveryTask(
            operation="models",
            endpoints=(endpoint.environment["OLLAMA_HOST"],),
            timeout_seconds=request.timeout_seconds,
        ),
        request.project_root,
    )
    return CliDiscoveredModels(report.values, report.diagnostics)


async def run_discovery(task: DiscoveryTask, project_root: Path) -> DiscoveryReport:
    with TemporaryDirectory(prefix="crewplane-ollama-discovery-") as temporary:
        report_path = Path(temporary) / "report.json"
        deadline = asyncio.timeout(task.timeout_seconds)
        try:
            async with deadline:
                await invoke_discovery_worker(task, project_root, report_path)
        except TimeoutError as exc:
            if not deadline.expired() or unconfirmed_process_cleanup(exc) is not None:
                raise
            report = (
                DiscoveryReport.model_validate_json(report_path.read_bytes())
                if report_path.exists()
                else DiscoveryReport()
            )
            return DiscoveryReport(
                values=report.values,
                diagnostics=(
                    *report.diagnostics,
                    f"Ollama {task.operation} discovery timed out after {task.timeout_seconds:g}s. "
                    "Discovery is incomplete; rerun with a larger --discovery-timeout or configure the provider manually.",
                ),
            )
        return DiscoveryReport.model_validate_json(report_path.read_bytes())


async def invoke_discovery_worker(
    task: DiscoveryTask, project_root: Path, report_path: Path
) -> None:
    result = await run_command_once(
        cmd=[
            sys.executable,
            "-P",
            "-c",
            "from crewplane_ollama.discovery_worker import main; main()",
            str(report_path),
        ],
        stdin_data=task.model_dump_json().encode("utf-8"),
        log_file=None,
        append_log=False,
        log_header=None,
        cwd=project_root,
        invocation_context=None,
        idle_timeout_seconds=None,
    )
    try:
        if result.returncode:
            raise ValueError(
                f"Ollama discovery worker failed ({result.returncode}): {result.stderr_text[:1000]}"
            )
    finally:
        cleanup_temporary_captures(result)
