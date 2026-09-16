from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from crewplane.architecture.contracts import ProviderTokenUsage, UsageDecodeResult
from crewplane.core.value_checks import is_nonnegative_int


@dataclass
class UsageAccumulator:
    totals: ProviderTokenUsage | None = None
    valid_report_count: int = 0
    malformed_error: str | None = None

    def record_usage(self, usage: ProviderTokenUsage) -> None:
        if not usage.has_any_value():
            return
        self.totals = usage if self.totals is None else self.totals.add_exact(usage)
        self.valid_report_count += 1

    def record_error(self, error: str) -> None:
        self.malformed_error = error

    def decode_and_record(
        self,
        decoder: Callable[[Mapping[str, object]], ProviderTokenUsage],
        payload: Mapping[str, object],
    ) -> None:
        try:
            usage = decoder(payload)
        except MalformedUsageError as exc:
            self.record_error(str(exc))
            return
        self.record_usage(usage)

    def result(self) -> UsageDecodeResult:
        return retained_usage_result(
            self.totals,
            self.valid_report_count,
            self.malformed_error,
        )


def retained_usage_result(
    tokens: ProviderTokenUsage | None,
    valid_report_count: int,
    error: str | None,
) -> UsageDecodeResult:
    retained_tokens = tokens if valid_report_count else None
    return UsageDecodeResult(
        tokens=retained_tokens,
        error=error,
        valid_report_count=valid_report_count,
    )


class MalformedUsageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CounterReader:
    provider: str
    payload: Mapping[str, object]

    def optional(self, key: str) -> int | None:
        value = self.payload.get(key)
        if value is None:
            return None
        if not is_nonnegative_int(value):
            raise MalformedUsageError(
                f"Malformed {self.provider} usage: "
                f"{key} must be a non-negative integer."
            )
        return value


def sum_present(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def complete_sum(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if len(present) == len(values) else None
