"""Shared model eligibility for discovery, readiness and actual execution."""


def is_local_tool_model(details: dict[str, object]) -> bool:
    capabilities = details.get("capabilities")
    return (
        isinstance(capabilities, list)
        and "completion" in capabilities
        and "tools" in capabilities
        and not details.get("remote_model")
        and not details.get("remote_host")
    )


def validate_model(details: dict[str, object], model: str) -> None:
    if not is_local_tool_model(details):
        raise ValueError(
            f"Model {model!r} must be installed locally on the selected server "
            "and advertise completion and tools; cloud-backed models are unsupported"
        )
