"""Shared artifact identities for build and qualification scripts."""
from __future__ import annotations

from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def release_wheel(path: Path | None = None) -> Path:
    wheel = (path or ROOT / f"dist/goldenmonkey_sentinels-{VERSION}-py3-none-any.whl").resolve()
    if not wheel.is_file():
        raise ValueError(f"Build or download the exact release wheel first: {wheel}")
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or f"Version: {VERSION}\n" not in archive.read(metadata[0]).decode():
            raise ValueError("Wheel version disagrees with the checked source version")
    return wheel
