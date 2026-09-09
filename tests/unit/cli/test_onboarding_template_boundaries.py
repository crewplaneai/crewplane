from __future__ import annotations

import json

import pytest

from crewplane.cli.onboarding.rendering import (
    OnboardingRenderingError,
    manual_config_snippet,
    manual_workflow_snippet,
    render_provider_ready_config,
    render_provider_ready_workflow,
    rendered_default_config,
    rendered_default_workflow,
)
from crewplane.cli.onboarding.rendering_config_validation import (
    validate_provider_ready_config,
)
from crewplane.cli.onboarding.rendering_workflow import validate_provider_ready_workflow
from crewplane.cli.onboarding.rendering_yaml_blocks import (
    comment_yaml_block,
    replace_once,
    uncomment_yaml_comment_block,
    yaml_child_block_end,
)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("  mock:\n", "  renamed:\n", "missing the mock agent"),
        (
            "\n\n  # Real provider examples",
            "\n\n  # Renamed examples",
            "missing the provider examples",
        ),
        ("  # codex:\n", "  # renamed:\n", "missing codex"),
        ("settings:\n", "configuration:\n", "default config is invalid"),
        ("    invoker:\n", "    provider_invoker:\n", "settings.integrations.invoker"),
        (
            '      implementation: "mock"',
            '      implementation: "cli"',
            "not using the mock invoker",
        ),
        (
            '  #   provider_kind: "codex"',
            '  #   provider_kind: "generic"',
            "has provider_kind",
        ),
    ],
)
def test_config_rendering_reports_changed_template_contracts(
    old: str, new: str, message: str
) -> None:
    template = rendered_default_config().replace(old, new, 1)

    with pytest.raises(OnboardingRenderingError, match=message):
        render_provider_ready_config(template, ("codex",))


def test_manual_config_rejects_an_unbounded_provider_example() -> None:
    template = "agents:\n  # codex:\n  #   cli_cmd: [codex]\n"

    with pytest.raises(OnboardingRenderingError, match="cannot bound codex"):
        manual_config_snippet(template, ("codex",))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "missing frontmatter"),
        ("---\nnodes: []\n", "not closed"),
        ("---\nnodes: []\n---", "one frontmatter node"),
        ("---\nnodes: [1]\n---", "node must be a mapping"),
        ("---\nnodes: [{providers: 1}]\n---", "must contain provider names"),
        ("---\nnodes: [{providers: [1]}]\n---", "must contain provider names"),
        ("---\nnodes: [{providers: [mock]}]\n---", "one providers line"),
        ("---\nnodes: [\n---", "not valid YAML"),
        ("---\n[]\n---", "must be a YAML mapping"),
    ],
)
def test_workflow_rendering_rejects_malformed_frontmatter(
    text: str, message: str
) -> None:
    with pytest.raises(OnboardingRenderingError, match=message):
        render_provider_ready_workflow(text, ("codex",))


def test_workflow_rendering_preserves_missing_trailing_newline() -> None:
    template = rendered_default_workflow().rstrip("\n")

    rendered = render_provider_ready_workflow(template, ("codex",))

    assert rendered == template.replace("providers: [mock]", "providers: [codex]")
    assert not rendered.endswith("\n")


def test_manual_workflow_rejects_inline_node_block_without_rewriting_it() -> None:
    template = "---\nnodes: &nodes\n  - id: review\n    providers: [mock]\n---\n## review\nReview"

    with pytest.raises(
        OnboardingRenderingError, match="one default workflow nodes block"
    ):
        manual_workflow_snippet(template, ("codex",))


@pytest.mark.parametrize(
    ("agents", "invoker", "message"),
    [
        (
            {"claude": {"cli_cmd": ["claude"], "provider_kind": "claude"}},
            {"implementation": "cli"},
            "only the selected providers",
        ),
        (
            {"codex": {"cli_cmd": ["codex"], "provider_kind": "codex"}},
            {"implementation": "mock"},
            "activate the cli invoker",
        ),
        (
            {"codex": {"cli_cmd": ["codex"], "provider_kind": "codex"}},
            {"implementation": "cli", "options": {"seed": 1}},
            "must not leave mock options",
        ),
    ],
)
def test_provider_ready_config_validation_rejects_unselected_agents_and_mock_settings(
    agents: dict[str, object], invoker: dict[str, object], message: str
) -> None:
    text = json.dumps(
        {
            "version": "1.0",
            "agents": agents,
            "settings": {"integrations": {"invoker": invoker}},
        }
    )

    with pytest.raises(OnboardingRenderingError, match=message):
        validate_provider_ready_config(text, ("codex",))


def test_provider_ready_workflow_rejects_a_different_provider_selection() -> None:
    with pytest.raises(OnboardingRenderingError, match="only the selected providers"):
        validate_provider_ready_workflow(rendered_default_workflow(), ("codex",))


def test_yaml_block_comments_preserve_blank_lines_and_existing_comments() -> None:
    block = "  agent:\n\n  # note\n    command: [codex]"

    assert (
        comment_yaml_block(block, 2) == "  # agent:\n\n  # note\n  #   command: [codex]"
    )
    assert (
        uncomment_yaml_comment_block("  # agent:\n  #\n  #   command: [codex]", 2)
        == "  agent:\n\n    command: [codex]"
    )
    assert (
        yaml_child_block_end(["agents:", "  codex:", "    command: [codex]"], 0, 0) == 3
    )


def test_yaml_block_rejects_changed_indentation() -> None:
    with pytest.raises(OnboardingRenderingError, match="indentation changed"):
        comment_yaml_block("  agent:\ncommand: [codex]", 2)


@pytest.mark.parametrize("text", ["no placeholder", "anchor anchor"])
def test_template_replacement_rejects_missing_or_ambiguous_anchor(text: str) -> None:
    with pytest.raises(OnboardingRenderingError, match="Expected one provider anchor"):
        replace_once(text, "anchor", "codex", "provider")
