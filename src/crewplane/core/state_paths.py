from pathlib import Path

STATE_DIR_NAME = ".crewplane"


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
