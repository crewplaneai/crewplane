"""Exercise update output with the child interpreter's configured encoding."""

from unittest.mock import patch

from rich.console import Console

import crewplane.cli.app as cli


def fake_update(console: Console) -> int:
    console.print("\u2713 updated")
    return 0


def main() -> None:
    with patch.object(cli, "update_crewplane", new=fake_update):
        cli.app(args=["--update"], standalone_mode=False)


if __name__ == "__main__":
    main()
