from __future__ import annotations

SYNTHETIC_INVOCATION_FAILURE_MARKER = "<!-- orchestrator:invocation_failed -->"


def build_invocation_failure_artifact(
    provider: str,
    task_id: str,
    error: str,
    failure_kind: str | None = None,
    failure_advice: str | None = None,
) -> str:
    lines = [
        SYNTHETIC_INVOCATION_FAILURE_MARKER,
        "# Invocation Failed",
        "",
        f"- Provider: {provider}",
        f"- Task ID: {task_id}",
        f"- Error: {error}",
    ]
    if failure_kind is not None:
        lines.append(f"- Failure Kind: {failure_kind}")
    if failure_advice is not None:
        lines.append(f"- Advice: {failure_advice}")
    return "\n".join(lines) + "\n"


def is_synthetic_invocation_failure(content: str) -> bool:
    return content.lstrip().startswith(SYNTHETIC_INVOCATION_FAILURE_MARKER)


def strip_synthetic_invocation_failure_marker(content: str) -> str:
    if not is_synthetic_invocation_failure(content):
        return content
    filtered_lines = [
        line
        for line in content.splitlines()
        if line.strip() != SYNTHETIC_INVOCATION_FAILURE_MARKER
    ]
    return "\n".join(filtered_lines).lstrip("\n")
