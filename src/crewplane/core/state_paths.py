from pathlib import Path

STATE_DIR_NAME = ".crewplane"
RUNTIME_ARTIFACT_ROOTS = (
    f"{STATE_DIR_NAME}/execution-stages",
    f"{STATE_DIR_NAME}/execution-results",
    f"{STATE_DIR_NAME}/locks",
)
FILE_TOKEN_EXCLUDED_ROOTS = (*RUNTIME_ARTIFACT_ROOTS, f"{STATE_DIR_NAME}/preflight")


def is_reserved_state_path(path: str, reserved_roots: tuple[str, ...]) -> bool:
    """Match a project-relative POSIX path against reserved directory roots."""
    return any(path == root or path.startswith(f"{root}/") for root in reserved_roots)


def get_state_dir(project_root: Path | None = None) -> Path:
    root = Path.cwd() if project_root is None else project_root
    return root / STATE_DIR_NAME


def ensure_state_dir(project_root: Path | None = None) -> Path:
    state_dir = get_state_dir(project_root)
    state_dir.mkdir(exist_ok=True)
    return state_dir


def resolve_state_file(
    override_path: Path | None,
    filename: str,
    project_root: Path | None = None,
) -> Path:
    if override_path is not None:
        return override_path
    return get_state_dir(project_root) / filename


def project_root_from_config_path(config_path: Path) -> Path:
    config_parent = config_path.resolve(strict=False).parent
    if config_parent.name == STATE_DIR_NAME:
        return config_parent.parent
    return config_parent
