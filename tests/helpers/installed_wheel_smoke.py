"""Run installed-wheel acceptance outside the checkout through pip or uv."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import tomllib
import venv
from pathlib import Path

if __package__:
    from .installed_package_assertions import run, smoke
else:
    from installed_package_assertions import run, smoke


def install(wheel: Path, root: Path, installer: str) -> tuple[Path, Path]:
    plugin_wheel = next(wheel.parent.glob("crewplane_ollama-*.whl"))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if installer == "pip":
        environment = root / "pip-environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--find-links",
                str(wheel.parent),
                str(wheel),
                str(plugin_wheel),
            ],
            root,
            env,
        )
    else:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required for the uv installed-wheel smoke")
        env.update(
            UV_TOOL_DIR=str(root / "tools"), UV_TOOL_BIN_DIR=str(root / "tool-bin")
        )
        run(
            [
                uv,
                "tool",
                "install",
                "--python",
                sys.executable,
                "--find-links",
                str(wheel.parent),
                "--with",
                str(plugin_wheel),
                str(wheel),
            ],
            root,
            env,
        )
        binary = root / "tools/crewplane" / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
    return python, binary / ("crewplane.exe" if os.name == "nt" else "crewplane")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", choices=("pip", "uv"), required=True)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel or next(Path("dist").glob("crewplane-*.whl"))
    with tempfile.TemporaryDirectory(prefix="crewplane-wheel-smoke-") as temporary:
        root = Path(temporary)
        python, cli = install(wheel.resolve(), root, args.installer)
        env = dict(os.environ)
        directories = [str(python.parent)]
        if os.name == "nt":
            directories.append(str(Path(env["SYSTEMROOT"]) / "System32"))
        env["PATH"] = os.pathsep.join(directories)
        assert shutil.which("node", path=env["PATH"]) is None
        assert shutil.which("npm", path=env["PATH"]) is None
        checkout = Path(__file__).resolve().parents[2]
        version = tomllib.loads(
            (checkout / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        results = checkout / "test-results"
        results.mkdir(exist_ok=True)
        diagnostics = Path(
            tempfile.mkdtemp(prefix=f"package-{args.installer}-", dir=results)
        )
        original_path = os.environ.get("PATH")
        try:
            os.environ["PATH"] = env["PATH"]
            smoke(python, cli, root / "project", version, checkout, diagnostics)
        finally:
            if original_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original_path


if __name__ == "__main__":
    main()
