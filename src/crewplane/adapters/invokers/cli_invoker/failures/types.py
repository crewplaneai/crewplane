from dataclasses import dataclass

from crewplane.architecture.contracts.invocation_failures import (
    InvocationFailureSummary,
)


@dataclass(frozen=True)
class FailureEvidence:
    summary: InvocationFailureSummary
    priority: int
    sequence: int
