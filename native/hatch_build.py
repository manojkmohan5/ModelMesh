"""Ship the compiled native UI in source and wheel builds."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        repository = root.parent
        if (repository / "package-lock.json").is_file():
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError("Building ModelMesh from the repository requires Node.js 22 and npm.")
            subprocess.run([npm, "ci"], cwd=repository, check=True)
            subprocess.run([npm, "run", "build:native"], cwd=repository, check=True)
        assets = root / "src" / "modelmesh" / "static"
        if not (assets / "index.html").is_file():
            raise FileNotFoundError(
                "The native UI is missing. Build from the repository with Node.js and npm, "
                "or use a ModelMesh source distribution containing the compiled UI."
            )
        asset_destination = "modelmesh/static" if self.target_name == "wheel" else "src/modelmesh/static"
        build_data["force_include"][str(assets)] = asset_destination
