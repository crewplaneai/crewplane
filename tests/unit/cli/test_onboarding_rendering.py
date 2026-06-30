from __future__ import annotations

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
from crewplane.core.config import load_config
from crewplane.core.provider_names import known_provider_names
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.validation import validate_workflow_plan


def test_onboarding_known_provider_names_follow_core_provider_names() -> None:
    assert known_provider_names() == KNOWN_PROVIDER_NAMES


def test_provider_ready_config_renders_each_known_provider(tmp_path) -> None:
    default_config = rendered_default_config()
    for provider in KNOWN_PROVIDER_NAMES:
        rendered = render_provider_ready_config(default_config, provider)
        config_path = tmp_path / f"{provider}.yml"
        config_path.write_text(rendered, encoding="utf-8")

        config = load_config(config_path)

        assert list(config.agents) == [provider]
        assert config.settings is not None
        assert config.settings.integrations.invoker.implementation == "cli"
        assert config.settings.integrations.invoker.options == {}
        assert f"  {provider}:" in rendered
        assert "  # mock:" in rendered
        assert (
            '  #   cli_cmd: ["__crewplane_mock_invoker_never_executes__"]' in rendered
        )
        assert '      # implementation: "mock"' in rendered
        assert "      # options:" in rendered
        assert '      #   output_mode: "lorem"' in rendered


def test_provider_ready_config_comments_current_mock_invoker_values() -> None:
    default_config = rendered_default_config().replace(
        "        observation_delay_seconds: 5",
        "        observation_delay_seconds: 9",
        1,
    )

    rendered = render_provider_ready_config(default_config, "codex")

    assert "      #   observation_delay_seconds: 9" in rendered
    assert "      #   observation_delay_seconds: 5" not in rendered


def test_provider_ready_workflow_changes_only_default_provider(tmp_path) -> None:
    default_workflow = rendered_default_workflow()
    rendered = render_provider_ready_workflow(default_workflow, "codex")
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
            "codex",
        )

    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_config(
            default_config.replace('      implementation: "mock"', "", 1),
            "codex",
        )

    with pytest.raises(OnboardingRenderingError):
        render_provider_ready_workflow(
            default_workflow.replace("    providers: [mock]", "    providers: [demo]"),
            "codex",
        )


def test_manual_fallback_snippets_are_selected_provider_only() -> None:
    default_config = rendered_default_config()
    default_workflow = rendered_default_workflow()

    config_snippet = manual_config_snippet(default_config, "gemini")
    workflow_snippet = manual_workflow_snippet(default_workflow, "gemini")

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

    config_snippet = manual_config_snippet(default_config, "codex")

    assert "      implementation: cli" in config_snippet
    assert '      implementation: "cli"' not in config_snippet


def test_manual_workflow_snippet_uses_default_workflow_node_block() -> None:
    default_workflow = rendered_default_workflow().replace(
        "    findings: true",
        "    findings: false",
        1,
    )

    workflow_snippet = manual_workflow_snippet(default_workflow, "codex")

    assert "    findings: false" in workflow_snippet
    assert "    providers: [codex]" in workflow_snippet
