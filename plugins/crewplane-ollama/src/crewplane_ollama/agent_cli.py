"""Installed final-stdout-only Ollama agent entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import BinaryIO

from . import agent
from .limits import MAX_DIAGNOSTIC_CHARACTERS, MAX_PROMPT_BYTES


def read_prompt(stream: BinaryIO) -> str:
    content = stream.read(MAX_PROMPT_BYTES + 1)
    if len(content) > MAX_PROMPT_BYTES:
        raise ValueError("Ollama prompt exceeded 1 MiB UTF-8")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Ollama prompt must be UTF-8") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded, command-enabled Ollama agent", allow_abbrev=False
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.model.strip() or args.model.startswith("-"):
        parser.error("--model must name a nonblank model, not an option")
    try:
        result = asyncio.run(
            agent.check(args.model)
            if args.check
            else agent.run(args.model, read_prompt(sys.stdin.buffer))
        )
        final_output = result.encode("utf-8")
    except (ValueError, OSError) as exc:
        print(
            f"Ollama {args.model!r}: {exc}"[:MAX_DIAGNOSTIC_CHARACTERS],
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("Ollama invocation interrupted", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
    sys.stdout.buffer.write(final_output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
