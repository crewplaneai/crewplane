import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.helpers import installed_wheel_smoke


@pytest.mark.parametrize("installer", ["pip", "uv"])
@pytest.mark.parametrize("indexed_version", ["0.1.0", "0.1.1"])
def test_install_uses_built_plugin_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer: str,
    indexed_version: str,
) -> None:
    dist = tmp_path / "dist"
    core = write_fixture_wheel(dist, "crewplane", "0.3.8", "local")
    write_fixture_wheel(dist, "crewplane_ollama", "0.1.0", "local")
    index = tmp_path / "index"
    indexed = write_fixture_wheel(
        index / "crewplane-ollama", "crewplane_ollama", indexed_version, "index"
    )
    (indexed.parent / "index.html").write_text(
        f'<a href="{indexed.name}">{indexed.name}</a>', encoding="utf-8"
    )
    for name in os.environ:
        if name.startswith(("PIP_", "UV_")):
            monkeypatch.delenv(name)
    for name, value in {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_INDEX_URL": index.as_uri(),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "UV_DEFAULT_INDEX": index.as_uri(),
        "UV_NO_CONFIG": "1",
        "UV_NO_CACHE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }.items():
        monkeypatch.setenv(name, value)

    python, _cli = installed_wheel_smoke.install(core, tmp_path, installer)

    selected = installed_wheel_smoke.run(
        [
            str(python),
            "-I",
            "-c",
            "import crewplane_ollama; from importlib.metadata import version; "
            "print(version('crewplane-ollama'), crewplane_ollama.SOURCE)",
        ],
        tmp_path,
        dict(os.environ),
    )
    assert selected.strip() == "0.1.0 local"


def write_fixture_wheel(directory: Path, name: str, version: str, origin: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    metadata_directory = f"{name}-{version}.dist-info"
    dependency = (
        "crewplane-ollama>=0.1.0,<0.2" if name == "crewplane" else "crewplane>=0.3.8"
    )
    files = {
        f"{name}/__init__.py": f"SOURCE = {origin!r}\ndef main(): pass\n",
        f"{metadata_directory}/METADATA": (
            f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
            f"Requires-Python: >=3.13\nRequires-Dist: {dependency}\n"
        ),
        f"{metadata_directory}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if name == "crewplane":
        files[f"{metadata_directory}/entry_points.txt"] = (
            "[console_scripts]\ncrewplane = crewplane:main\n"
        )
    record = f"{metadata_directory}/RECORD"
    files[record] = "".join(f"{path},,\n" for path in files) + f"{record},,\n"
    with ZipFile(wheel, "w") as archive:
        for path, contents in files.items():
            archive.writestr(path, contents)
    return wheel
