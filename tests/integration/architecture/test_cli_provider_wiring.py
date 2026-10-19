import asyncio
import copy
import importlib
import sys
from dataclasses import replace

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker.capabilities import CAPABILITIES
from crewplane.architecture.errors import AdapterContractError
from crewplane.bootstrap import build_runtime_components, build_runtime_config_snapshot
from crewplane.cli.run.invoker_preflight import collect_invoker_availability_diagnostics
from crewplane.cli.run.preflight import compile_workflow_preview
from crewplane.core.config import AgentConfig
from tests.helpers.cli_provider import (
    FIXTURE_PACKAGE,
    expose_distribution,
    fixture_options,
    provider_config,
    provider_source,
    quiet_console,
)
from tests.helpers.observability import topology_from_workflow


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    metadata = expose_distribution(
        monkeypatch, tmp_path, FIXTURE_PACKAGE, "crewplane-fixture-provider", "1.2.3"
    )
    return importlib.import_module("crewplane_fixture_provider"), metadata


def config_for():
    return provider_config(
        AgentConfig(
            cli_cmd=[sys.executable], provider_kind="fixture", default_model="text"
        ),
        fixture_options(),
    )


def preview_for(config, root, model=None, availability=False):
    return compile_workflow_preview(
        config,
        provider_source(model),
        quiet_console(),
        True,
        "read_only",
        project_root=root,
        check_cli_availability=availability,
    )


def test_authored_and_supplied_canonical_construction_preserve_identity(
    plugin, tmp_path
):
    module, metadata = plugin
    config = config_for()
    snapshot = build_runtime_config_snapshot(config, quiet_console(), True)
    canonical = copy.deepcopy(snapshot.invoker_options)
    for options in (None, canonical):
        components = build_runtime_components(
            config,
            topology_from_workflow(provider_source().workflow),
            tmp_path / "state",
            tmp_path,
            quiet_console(),
            True,
            invoker_options=options,
        )
        assert (
            components.base_invoker.log_presentation_for(config.agents["local"]).profile
            == "generic"
        )
    metadata.write_text("Name: crewplane-fixture-provider\nVersion: 2.0\n")
    with pytest.raises(ValueError, match="version mismatch"):
        build_runtime_components(
            config,
            topology_from_workflow(provider_source().workflow),
            tmp_path / "state",
            tmp_path,
            quiet_console(),
            True,
            invoker_options=canonical,
        )
    assert snapshot.invoker_options == canonical
    assert module.FIXTURE.provider_kind not in CAPABILITIES


def test_invoker_maps_are_isolated_and_bound_at_construction(plugin, monkeypatch):
    module, _ = plugin
    config = config_for()
    adapter = CliInvokerAdapter()
    canonical = adapter.canonicalize_options("cli", "cli", fixture_options()).options
    first = adapter.create_invoker(config, canonical)
    monkeypatch.setattr(module, "FIXTURE", module.ALTERNATE)
    second = adapter.create_invoker(config, canonical)
    assert first.log_presentation_for(config.agents["local"]).profile == "generic"
    assert second.log_presentation_for(config.agents["local"]).profile == "alternate"
    assert "fixture" not in CAPABILITIES


def test_unused_unknown_agents_fail_preview_and_construction(plugin, tmp_path):
    del plugin
    config = config_for()
    config.agents["unused"] = AgentConfig(cli_cmd=["echo"], provider_kind="unknown")
    preview = preview_for(config, tmp_path)
    assert preview.has_errors()
    assert any(
        "Agent 'unused'" in diagnostic.message for diagnostic in preview.diagnostics
    )
    canonical = build_runtime_config_snapshot(
        config, quiet_console(), True
    ).invoker_options
    with pytest.raises(ValueError, match="unused"):
        CliInvokerAdapter().create_invoker(config, canonical)


def test_canonicalization_and_request_preview_never_probe_or_allocate(
    plugin, tmp_path, monkeypatch
):
    module, _ = plugin

    async def probe(request):
        raise AssertionError(f"unexpected probe: {request}")

    monkeypatch.setattr(
        module, "FIXTURE", replace(module.FIXTURE, check_availability=probe)
    )
    config = config_for()
    snapshot = build_runtime_config_snapshot(config, quiet_console(), True)
    assert snapshot.invoker_options["providers"]["fixture"]["version"] == "1.2.3"
    assert not preview_for(config, tmp_path).has_errors()
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("invalid", ["sync", "result"])
def test_availability_callback_result_contract(plugin, tmp_path, monkeypatch, invalid):
    module, _ = plugin

    def sync(request):
        del request

    async def wrong_result(request):
        del request
        return "invalid"

    monkeypatch.setattr(
        module,
        "FIXTURE",
        replace(
            module.FIXTURE,
            check_availability=sync if invalid == "sync" else wrong_result,
        ),
    )
    with pytest.raises(AdapterContractError, match="check_availability must return"):
        preview_for(config_for(), tmp_path, availability=True)


def test_sync_and_async_preflight_use_exact_effective_requests(
    plugin, tmp_path, monkeypatch
):
    module, _ = plugin
    models = []

    async def probe(request):
        models.append(request.model)

    monkeypatch.setattr(
        module, "FIXTURE", replace(module.FIXTURE, check_availability=probe)
    )
    config = config_for()
    assert not preview_for(config, tmp_path, availability=True).has_errors()
    canonical = build_runtime_config_snapshot(
        config, quiet_console(), True
    ).invoker_options
    diagnostics = asyncio.run(
        collect_invoker_availability_diagnostics(
            provider_source("override").workflow, config, tmp_path, canonical
        )
    )
    assert diagnostics.errors == ()
    assert models == ["text", "override"]
    assert not (tmp_path / ".crewplane").exists()


def test_invalid_requests_and_missing_executables_skip_callbacks(
    plugin, tmp_path, monkeypatch
):
    module, _ = plugin

    async def probe(request):
        raise AssertionError(f"unexpected probe: {request}")

    monkeypatch.setattr(
        module, "FIXTURE", replace(module.FIXTURE, check_availability=probe)
    )
    config = config_for()
    config.agents["local"].cli_cmd = ["nonexistent-fixture-provider"]
    assert preview_for(config, tmp_path, availability=True).has_errors()
    source = provider_source()
    source.workflow.nodes[0].providers[0].reasoning = "high"
    config.agents["local"].cli_cmd = [sys.executable]
    canonical = build_runtime_config_snapshot(
        config, quiet_console(), True
    ).invoker_options
    result = asyncio.run(
        collect_invoker_availability_diagnostics(
            source.workflow, config, tmp_path, canonical
        )
    )
    assert any("reasoning" in error for error in result.errors)
