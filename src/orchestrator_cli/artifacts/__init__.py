from .naming import safe_artifact_name

__all__ = [
    "FindingsExtractionError",
    "OutputManager",
    "safe_artifact_name",
]


def __getattr__(name: str):
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
