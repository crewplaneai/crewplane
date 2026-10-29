from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from crewplane.cli.onboarding.rendering import (
    KNOWN_PROVIDER_NAMES,
    OnboardingRenderingError,
    manual_config_snippet,
    manual_workflow_snippet,
    render_provider_ready_config,
    render_provider_ready_workflow,
    rendered_default_config,
    rendered_default_workflow,
)
from crewplane.cli.onboarding.rendering_config_agents import (
    extract_commented_provider_block,
)
from crewplane.cli.onboarding.rendering_yaml_blocks import uncomment_comment_block
from crewplane.cli.onboarding.rendering_yaml_loading import (
    load_yaml_mapping,
    mapping_child,
)
from crewplane.core.config import load_config
from crewplane.core.provider_names import known_provider_names
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.validation import validate_workflow_plan
from crewplane.core.yaml_loader import load_yaml_unique


def test_onboarding_known_provider_names_follow_core_provider_names() -> None:
    assert known_provider_names() == KNOWN_PROVIDER_NAMES


def _provider_template_mapping(template: str, provider: str) -> Mapping[str, object]:
    block = extract_commented_provider_block(template, provider)
    data = load_yaml_mapping(uncomment_comment_block(block), f"{provider} example")
    agent = mapping_child(data, provider, f"{provider} example")
    model = agent.get("default_model")
    if model is not None:
        assert isinstance(model, str), f"{provider} default_model must be a string"
        assert model.strip(), f"{provider} default_model must not be blank"
    return agent


@pytest.mark.parametrize("provider", KNOWN_PROVIDER_NAMES)
def test_provider_ready_config_renders_each_known_provider(
    tmp_path: Path, provider: str
) -> None:
    default_config = rendered_default_config()
    template_agent = _provider_template_mapping(default_config, provider)
    rendered = render_provider_ready_config(default_config, (provider,))
    config_path = tmp_path / f"{provider}.yml"
    config_path.write_text(rendered, encoding="utf-8", newline="\n")

    config = load_config(config_path)

    assert list(config.agents) == [provider]
    assert config.agents[provider].provider_kind == provider
    assert config.agents[provider].default_model == template_agent.get("default_model")
    if provider in {"pi", "deepseek", "opencode"}:
        assert "default_model" not in template_agent
        assert "default_model" not in load_yaml_unique(rendered)["agents"][provider]
        assert config.agents[provider].default_model is None
    if provider == "pi":
        assert any(
            line.startswith("    # default_model:") for line in rendered.splitlines()
        )
    assert config.settings is not None
    assert config.settings.integrations.invoker.implementation == "cli"
    assert config.settings.integrations.invoker.options == {}
    assert f"  {provider}:" in rendered
    assert "  # mock:" in rendered
    assert '  #   cli_cmd: ["__crewplane_mock_invoker_never_executes__"]' in rendered
    assert '      # implementation: "mock"' in rendered
    assert "      # options:" in rendered
    assert '      #   output_mode: "lorem"' in rendered


def _template_with_codex_fields(fields: str) -> str:
    template = rendered_default_config()
    block = extract_commented_provider_block(template, "codex")
    replacement = (
        "  # codex:\n"
        "  #   cli_cmd: [codex, exec]\n"
        "  #   provider_kind: codex\n"
        f"  #   {fields}\n"
    )
    return template.replace(block, replacement, 1)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param("default_model: [", id="malformed-yaml"),
        pytest.param(
            "default_model: first\n  #   default_model: second", id="duplicate-key"
        ),
        pytest.param("default_model: 42", id="numeric-model"),
        pytest.param("default_model: true", id="boolean-model"),
        pytest.param("default_model: [model]", id="list-model"),
        pytest.param("default_model: {name: model}", id="mapping-model"),
        pytest.param("max_retries: invalid", id="invalid-other-field"),
    ],
)
def test_provider_ready_config_rejects_invalid_commented_fields(fields: str) -> None:
    template = _template_with_codex_fields(fields)

    with pytest.raises(OnboardingRenderingError, match="not valid YAML|is invalid"):
        render_provider_ready_config(template, ("codex",))


@pytest.mark.parametrize("model", ["", "   "])
def test_template_contract_rejects_blank_models_without_restricting_user_config(
    tmp_path: Path, model: str
) -> None:
    template = _template_with_codex_fields(f'default_model: "{model}"')

    with pytest.raises(AssertionError, match="default_model must not be blank"):
        _provider_template_mapping(template, "codex")

    config_path = tmp_path / "config.yml"
    config_path.write_text(
        render_provider_ready_config(template, ("codex",)),
        encoding="utf-8",
        newline="\n",
    )
    assert load_config(config_path).agents["codex"].default_model == model


@pytest.mark.parametrize("model", ["  illustrative-model  ", None])
def test_template_contract_preserves_nonblank_models_and_null(
    tmp_path: Path, model: str | None
) -> None:
    field = "default_model: null" if model is None else f'default_model: "{model}"'
    template = _template_with_codex_fields(field)
    assert _provider_template_mapping(template, "codex")["default_model"] == model
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        render_provider_ready_config(template, ("codex",)),
        encoding="utf-8",
        newline="\n",
    )
    assert load_config(config_path).agents["codex"].default_model == model


def test_provider_ready_config_comments_current_mock_invoker_values() -> None:
    default_config = rendered_default_config().replace(
        "        observation_delay_seconds: 5",
        "        observation_delay_seconds: 9",
        1,
    )

    rendered = render_provider_ready_config(default_config, ("codex",))

    assert "      #   observation_delay_seconds: 9" in rendered
    assert "      #   observation_delay_seconds: 5" not in rendered


def test_provider_ready_workflow_changes_only_default_provider(tmp_path) -> None:
    default_workflow = rendered_default_workflow()
    rendered = render_provider_ready_workflow(default_workflow, ("codex",))
    workflow_path = tmp_path / "single-agent-review.task.md"
    workflow_path.write_text(rendered, encoding="utf-8")

    workflow = validate_workflow_plan(
        load_tasks_with_sources(workflow_path, project_root=tmp_path).workflow
    )

    assert rendered == default_workflow.replace(
        "    providers: [mock]",
        "    providers: [codex]",
        1,
    )
    assert [node.id for node in workflow.nodes] == ["review.project"]
    assert workflow.nodes[0].mode == "parallel"
    assert workflow.nodes[0].findings is True
    assert [provider.provider for provider in workflow.nodes[0].providers] == ["codex"]
    assert "{{file:.crewplane/config.yml}}" in rendered
    assert "<!-- findings -->" in rendered


def test_rendering_fails_when_expected_anchors_are_missing() -> None:
    default_config = rendered_default_config()
    default_workflow = rendered_default_workflow()

    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_config(
            default_config.replace("      # options: {}\n", "", 1),
            ("codex",),
        )

    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_config(
            default_config.replace('      implementation: "mock"', "", 1),
            ("codex",),
        )

    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_workflow(
            default_workflow.replace("    providers: [mock]", "    providers: [demo]"),
            ("codex",),
        )


def test_manual_fallback_snippets_are_selected_provider_only() -> None:
    default_config = rendered_default_config()
    default_workflow = rendered_default_workflow()

    config_snippet = manual_config_snippet(default_config, ("gemini",))
    workflow_snippet = manual_workflow_snippet(default_workflow, ("gemini",))

    assert "gemini:" in config_snippet
    assert "mock:" not in config_snippet
    assert 'implementation: "cli"' in config_snippet
    assert "options: {}" in config_snippet
    assert "id: review.project" in workflow_snippet
    assert "findings: true" in workflow_snippet
    assert "providers: [gemini]" in workflow_snippet


def test_manual_config_snippet_uses_commented_cli_invoker_from_template() -> None:
    default_config = rendered_default_config().replace(
        '      # implementation: "cli"',
        "      # implementation: cli",
        1,
    )

    config_snippet = manual_config_snippet(default_config, ("codex",))

    assert "      implementation: cli" in config_snippet
    assert '      implementation: "cli"' not in config_snippet


def test_manual_workflow_snippet_uses_default_workflow_node_block() -> None:
    default_workflow = rendered_default_workflow().replace(
        "    findings: true",
        "    findings: false",
        1,
    )

    workflow_snippet = manual_workflow_snippet(default_workflow, ("codex",))

    assert "    findings: false" in workflow_snippet
    assert "    providers: [codex]" in workflow_snippet


def test_manual_fallback_snippets_include_all_selected_providers() -> None:
    config_snippet = manual_config_snippet(
        rendered_default_config(), ("gemini", "codex")
    )
    workflow_snippet = manual_workflow_snippet(
        rendered_default_workflow(), ("gemini", "codex")
    )

    assert "gemini:" in config_snippet
    assert "codex:" in config_snippet
    assert "claude:" not in config_snippet
    assert "mock:" not in config_snippet
    assert config_snippet.count('implementation: "cli"') == 1
    assert "providers: [gemini, codex]" in workflow_snippet


@pytest.mark.parametrize("providers", [(), ("codex", "unknown")])
def test_rendering_rejects_empty_or_unknown_provider_selections(providers) -> None:
    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_config(rendered_default_config(), providers)
    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_workflow(rendered_default_workflow(), providers)
