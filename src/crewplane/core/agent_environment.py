from collections.abc import Mapping

from .platform import is_native_windows


def merge_agent_environment(
    inherited: Mapping[str, str], configured: Mapping[str, str]
) -> dict[str, str]:
    """Overlay explicit agent values without mutating the caller's environment."""
    if is_native_windows():
        return {
            key.upper(): value
            for source in (inherited, configured)
            for key, value in source.items()
        }
    return {**inherited, **configured}


def validate_agent_environment(value: dict[str, str]) -> dict[str, str]:
    for name, content in value.items():
        if not name.strip() or "=" in name or "\0" in name:
            raise ValueError(
                "environment names must be nonblank and contain neither '=' nor NUL"
            )
        if "\0" in content:
            raise ValueError("environment values must not contain NUL")
    return value
