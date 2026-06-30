from crewplane.architecture.contracts import SUPPORTED_PROVIDER_KINDS, ProviderKind
from crewplane.core.provider_names import known_provider_names


def test_known_provider_names_follow_supported_non_generic_provider_kinds() -> None:
    assert known_provider_names() == tuple(
        provider_kind.value
        for provider_kind in SUPPORTED_PROVIDER_KINDS
        if provider_kind != ProviderKind.GENERIC
    )
    assert ProviderKind.GENERIC.value not in known_provider_names()
