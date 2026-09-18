"""Log levels shared by invocation diagnostics and execution events."""

from enum import StrEnum, unique


@unique
class LogLevel(StrEnum):
    """Supported diagnostic levels and their persisted string values."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
