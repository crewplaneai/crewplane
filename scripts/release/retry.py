from __future__ import annotations

import time
from collections.abc import Callable

from .state_types import RetryableRegistryError

VERIFICATION_ATTEMPTS = 24
INITIAL_DELAY_SECONDS = 2
MAX_DELAY_SECONDS = 30


def wait_for_verification(
    label: str,
    collect_issues: Callable[[], list[str]],
    attempts: int = VERIFICATION_ATTEMPTS,
    initial_delay_seconds: int = INITIAL_DELAY_SECONDS,
) -> list[str]:
    issues: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            issues = collect_issues()
        except RetryableRegistryError as error:
            issues = [str(error)]
        if not issues:
            if attempt > 1:
                retries = attempt - 1
                suffix = "retry" if retries == 1 else "retries"
                print(f"{label} verification passed after {retries} {suffix}.")
            return []
        if attempt == attempts:
            return issues
        delay = min(MAX_DELAY_SECONDS, initial_delay_seconds * 2 ** (attempt - 1))
        print(
            f"{label} verification pending ({attempt}/{attempts}): "
            f"{'; '.join(issues)}; retrying in {delay}s."
        )
        time.sleep(delay)
    return issues
