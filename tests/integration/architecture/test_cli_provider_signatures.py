import copy
import importlib

import pytest

from crewplane.bootstrap import build_runtime_config_snapshot
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


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    metadata = expose_distribution(
        monkeypatch, tmp_path, FIXTURE_PACKAGE, "crewplane-fixture-provider", "1.2.3"
    )
    config = provider_config(
        AgentConfig(cli_cmd=["echo"], provider_kind="fixture", default_model="default"),
        fixture_options(),
    )
    return config, metadata


def signature(config, root, model=None):
    preview = compile_workflow_preview(
        config,
        provider_source(model),
        quiet_console(),
        True,
        "read_only",
        project_root=root,
    )
    assert not preview.has_errors(), preview.diagnostics
    return preview.workflow_signature


@pytest.mark.parametrize(
    "field",
    ["implementation", "distribution", "version", "model", "extra_args", "unused"],
)
def test_execution_identity_changes_existing_signatures(
    configuration, tmp_path, monkeypatch, field
):
    config, metadata = configuration
    before = signature(config, tmp_path)
    registration = config.settings.integrations.invoker.options["providers"]["fixture"]
    if field == "implementation":
        registration["implementation"] = "crewplane_fixture_provider:ALTERNATE"
    elif field == "distribution":
        expose_distribution(
            monkeypatch, tmp_path, FIXTURE_PACKAGE, "another-fixture", "1.2.3"
        )
        registration["distribution"] = "another-fixture"
    elif field == "version":
        metadata.write_text("Name: crewplane-fixture-provider\nVersion: 2.0\n")
    elif field == "model":
        assert signature(config, tmp_path, "override") != before
        return
    elif field == "extra_args":
        config.agents["local"].extra_args = ["literal"]
    else:
        module = importlib.import_module("crewplane_fixture_provider")
        monkeypatch.setattr(module, "UNUSED", module.WRONG_IDENTITY, raising=False)
        config.settings.integrations.invoker.options["providers"]["another"] = {
            **registration,
            "implementation": "crewplane_fixture_provider:UNUSED",
        }
    assert signature(config, tmp_path) != before


def test_observation_and_generic_only_model_arg_do_not_change_plugin_identity(
    configuration, tmp_path
):
    config, _ = configuration
    before = signature(config, tmp_path)
    config.agents["local"].model_arg = "--ignored"
    config.settings.integrations.ui.options = {"quiet_after_seconds": 7}
    # tmux options remain observational even when live presentation is disabled.
    config.settings.integrations.ui.implementation = "tmux"
    assert signature(config, tmp_path) == before


def test_registration_order_is_deterministic_and_canonical_values_persist(
    configuration,
):
    config, _ = configuration
    before = build_runtime_config_snapshot(config, quiet_console(), True)
    config.settings.integrations.invoker.options = copy.deepcopy(
        config.settings.integrations.invoker.options
    )
    record = config.settings.integrations.invoker.options["providers"]["fixture"]
    config.settings.integrations.invoker.options["providers"]["fixture"] = dict(
        reversed(list(record.items()))
    )
    after = build_runtime_config_snapshot(config, quiet_console(), False)
    assert (
        before.snapshot.effective_runtime_config_signature
        == after.snapshot.effective_runtime_config_signature
    )
    payload = before.snapshot.model_dump(mode="json")
    assert payload["invoker"]["options"]["providers"]["fixture"]["version"] == "1.2.3"
