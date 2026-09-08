from pathlib import Path

from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES

WORKSPACE_STATE_FILENAME = "workspace-state.json"
WORKSPACE_STATE_PREFIX = "workspace-state-"


def workspace_state_candidates(stage_dir: Path) -> tuple[Path, ...]:
    """List canonical then invocation state paths; callers validate file safety."""
    return (
        stage_dir / WORKSPACE_STATE_FILENAME,
        *sorted(stage_dir.glob(f"{WORKSPACE_STATE_PREFIX}*.json")),
    )


def is_workspace_claim_name(name: str) -> bool:
    return name == WORKSPACE_STATE_FILENAME or (
        name.endswith(".json")
        and name.startswith((WORKSPACE_STATE_PREFIX, "workspace-reuse-claim-"))
    )


def is_temporary_ref_evidence_name(name: str) -> bool:
    return name.startswith("workspace-temporary-refs-") and name.endswith(".json")


def is_safe_workspace_stage_path(value: str) -> bool:
    path = Path(value)
    return (
        bool(value.strip())
        and not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.parts[0] not in RESERVED_RUN_ROOT_NAMES
    )
