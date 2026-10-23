"""CLI transport with the active host's workspace limitation declared."""

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    InvokerAdapterCapabilities,
    JsonObject,
)


class DotsHostInvokerAdapter(CliInvokerAdapter):
    """Reuse CLI invocation without claiming ownership of host-owned agents."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        canonical = super().canonicalize_options(
            implementation, resolved_identity, options
        )
        return canonical.model_copy(
            update={"capabilities": InvokerAdapterCapabilities.unsupported().as_dict()}
        )
