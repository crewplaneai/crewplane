"""Run discovery HTTP requests in a process whose DNS resolver can be stopped."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, FiniteFloat

from .discovery_http import discovery_client, read_json

MAX_MODELS = 128


class DiscoveryTask(BaseModel):
    operation: Literal["endpoints", "models"]
    endpoints: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: FiniteFloat = Field(gt=0)


class DiscoveryReport(BaseModel):
    values: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


async def query_server(task: DiscoveryTask, report_path: Path) -> DiscoveryReport:
    if task.operation == "endpoints":
        return await query_endpoints(task, report_path)
    return await query_models(task, report_path)


async def query_endpoints(task: DiscoveryTask, report_path: Path) -> DiscoveryReport:
    endpoints: set[str] = set()
    diagnostics: list[str] = []
    async with (
        discovery_client(task.timeout_seconds) as client,
        asyncio.TaskGroup() as group,
    ):
        probes = [
            group.create_task(query_endpoint(client, endpoint))
            for endpoint in task.endpoints
        ]
        for probe in asyncio.as_completed(probes):
            result = await probe
            endpoints.update(result.values)
            diagnostics.extend(result.diagnostics)
            report = DiscoveryReport(
                values=tuple(url for url in task.endpoints if url in endpoints),
                diagnostics=tuple(diagnostics),
            )
            write_report(report, report_path)
    return report


async def query_endpoint(client: httpx.AsyncClient, endpoint: str) -> DiscoveryReport:
    try:
        version = await read_json(client, endpoint + "/api/version")
        if not isinstance(version.get("version"), str) or not version["version"]:
            raise ValueError("response does not identify an Ollama version")
    except ValueError as exc:
        return DiscoveryReport(
            diagnostics=(
                f"{endpoint}: {exc}. Check that Ollama is running and reachable.",
            )
        )
    return DiscoveryReport(values=(endpoint,))


async def query_models(task: DiscoveryTask, report_path: Path) -> DiscoveryReport:
    models: list[str] = []
    diagnostics: list[str] = []
    try:
        async with discovery_client(task.timeout_seconds) as client:
            base = task.endpoints[0]
            data = await read_json(client, base + "/api/tags")
            names = model_names(data)
            if len(names) > MAX_MODELS:
                diagnostics.append(
                    f"Only the first {MAX_MODELS} installed models were inspected. Use manual config for others."
                )
            for name in names[:MAX_MODELS]:
                try:
                    details = await read_json(
                        client, base + "/api/show", {"model": name}
                    )
                except ValueError as exc:
                    diagnostics.append(f"Skipped {name}: {exc}")
                else:
                    if is_local_text_model(details):
                        models.append(name)
                write_report(
                    DiscoveryReport(
                        values=tuple(models), diagnostics=tuple(diagnostics)
                    ),
                    report_path,
                )
    except ValueError as exc:
        diagnostics.append(
            f"Model discovery did not complete: {exc}. Rerun onboarding or configure the model manually."
        )
    if not models and not diagnostics:
        diagnostics.append(
            "No installed local text-generation models found. Download a text model on the selected server with 'ollama pull <model>', then rerun onboarding. Embedding-only and cloud models are excluded."
        )
    return DiscoveryReport(values=tuple(models), diagnostics=tuple(diagnostics))


def model_names(data: dict[str, object]) -> tuple[str, ...]:
    entries = data.get("models")
    if not isinstance(entries, list):
        raise ValueError("Ollama model list is missing 'models'")
    names = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(name := entry.get("name"), str)
            or not name.strip()
            or name.startswith("-")
        ):
            raise ValueError("Ollama model list contains an invalid name")
        names.add(name)
    return tuple(sorted(names))


def is_local_text_model(details: dict[str, object]) -> bool:
    capabilities = details.get("capabilities")
    return (
        isinstance(capabilities, list)
        and "completion" in capabilities
        and not details.get("remote_model")
        and not details.get("remote_host")
    )


def write_report(report: DiscoveryReport, destination: Path) -> None:
    """Keep the last complete result available if the supervisor stops this worker."""
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(report.model_dump_json(), encoding="utf-8")
    temporary.replace(destination)


def main() -> None:
    task = DiscoveryTask.model_validate_json(sys.stdin.buffer.read())
    report_path = Path(sys.argv[1])
    result = asyncio.run(query_server(task, report_path))
    write_report(result, report_path)
