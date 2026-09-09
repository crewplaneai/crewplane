from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from crewplane.cli.update.types import (
    CommandRunner,
    InstalledMetadata,
    UpdateCommand,
    UpdateContext,
)


@dataclass
class FailedCommands:
    responses: dict[UpdateCommand, str | BaseException]
    calls: list[UpdateCommand] = field(default_factory=list)

    def __call__(
        self, args: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        assert options["shell"] is False
        arguments = tuple(args)
        self.calls.append(arguments)
        result = self.responses[arguments]
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(list(arguments), 0, stdout=result, stderr="")


def update_context(root: Path, commands: FailedCommands) -> UpdateContext:
    environment = root / "tools" / "crewplane"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    executables = {"uv": str(root / "bin" / "uv")}
    return UpdateContext(
        "crewplane",
        environment / "bin" / "python",
        environment,
        InstalledMetadata(installer="uv"),
        "1.0",
        executables.get,
        cast(CommandRunner, commands),
    )
