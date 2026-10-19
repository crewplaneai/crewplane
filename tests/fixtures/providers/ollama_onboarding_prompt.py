"""Reach a real onboarding input prompt without contacting an Ollama server."""

import sys
from collections import deque
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crewplane_ollama import discovery

from crewplane.cli.app import app
from crewplane.cli.onboarding import runner


def main():
    phase, executable = sys.argv[1:]
    answers = deque(["1", "1"] + (["1"] if phase == "model" else []))
    original_options = runner.default_onboarding_options

    def read_input():
        if answers:
            return answers.popleft()
        Path("prompt-ready").touch()
        return runner.read_stdin_line()

    def options(console):
        return replace(
            original_options(console),
            input_is_terminal=True,
            output_is_terminal=True,
            read_input=read_input,
            which_fn={"ollama": executable}.get,
        )

    async def query(task, project_root):
        del project_root
        return discovery.DiscoveryReport(
            values=("http://127.0.0.1:11434",)
            if task.operation == "endpoints"
            else ("local-text",)
        )

    with (
        patch.object(runner, "default_onboarding_options", new=options),
        patch.object(discovery, "run_discovery", new=query),
    ):
        app(args=["onboarding"], prog_name="crewplane")


if __name__ == "__main__":
    main()
