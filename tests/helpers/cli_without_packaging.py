import importlib.abc
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from types import ModuleType


class BlockPackaging(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        if fullname == "packaging" or fullname.startswith("packaging."):
            raise ModuleNotFoundError("blocked packaging")
        return None


def main() -> None:
    sys.meta_path.insert(0, BlockPackaging())

    from click import unstyle
    from typer.testing import CliRunner

    from crewplane.cli import app as cli

    result = CliRunner().invoke(cli.app, ["--help"], catch_exceptions=False)
    if result.exit_code != 0:
        print(result.output)
        raise SystemExit(result.exit_code)
    if "--update" not in unstyle(result.output):
        raise SystemExit("missing --update")


if __name__ == "__main__":
    main()
