from __future__ import annotations


class ArchitectureError(Exception):
    """Base error for architecture/integration failures."""


class IntegrationResolutionError(ArchitectureError):
    """Raised when an integration implementation cannot be resolved."""


class AdapterLoadError(ArchitectureError):
    """Raised when an adapter class cannot be imported or instantiated."""


class AdapterContractError(ArchitectureError):
    """Raised when an adapter does not satisfy the required contract."""
