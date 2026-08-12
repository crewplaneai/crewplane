from typing import TYPE_CHECKING

from .naming import safe_artifact_name

if TYPE_CHECKING:
    from .manager import OutputManager
    from .results.findings import FindingsExtractionError

__all__ = [
    "FindingsExtractionError",
    "OutputManager",
    "safe_artifact_name",
]


def __getattr__(name: str) -> object:
    if name in {
        "FindingsExtractionError",
        "OutputManager",
    }:
        from .manager import OutputManager
        from .results.findings import FindingsExtractionError

        exports = {
            "FindingsExtractionError": FindingsExtractionError,
            "OutputManager": OutputManager,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
