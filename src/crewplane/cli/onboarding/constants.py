from pathlib import Path

PROVIDER_SETUP_URL = (
    "https://github.com/crewplaneai/crewplane/blob/master/"
    "docs/getting-started/provider-setup.md"
)
CONFIG_RELATIVE_PATH = Path(".crewplane/config.yml")
WORKFLOW_RELATIVE_PATH = Path(".crewplane/workflows/single-agent-review.task.md")
ONBOARDING_COMMAND_HELP = "Prepare one real provider after the provider-free first run."


__all__ = [
    "CONFIG_RELATIVE_PATH",
    "ONBOARDING_COMMAND_HELP",
    "PROVIDER_SETUP_URL",
    "WORKFLOW_RELATIVE_PATH",
]
