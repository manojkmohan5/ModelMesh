"""Build the real sdist and wheel; source checkouts must not mask missing resources."""

import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_sdist_to_wheel_retains_runtime_resources(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("The uv build CLI is required for package qualification.")
    source_root = tmp_path / "source"
    for directory in ("native", "frontend"):
        shutil.copytree(
            ROOT / directory, source_root / directory,
            ignore=shutil.ignore_patterns(
                ".venv", "node_modules", ".data", ".env", ".env.*", "dist",
                "__pycache__", ".pytest_cache", "test-results", "static",
            ) if directory == "native" else shutil.ignore_patterns("node_modules", "__pycache__"),
        )
    for filename in ("package.json", "package-lock.json", "tsconfig.json"):
        shutil.copy2(ROOT / filename, source_root / filename)
    assert not (source_root / "native/src/modelmesh/static").exists()
    distribution = tmp_path / "distribution"
    result = subprocess.run(
        [uv, "build", "--out-dir", str(distribution)],
        cwd=source_root / "native", capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = distribution.glob("*.whl")
    sdist, = distribution.glob("*.tar.gz")
    with tarfile.open(sdist) as source:
        assert any(entry.name.endswith("/hatch_build.py") for entry in source.getmembers())
        assert any(entry.name.endswith("/src/modelmesh/static/index.html") for entry in source.getmembers())
    with zipfile.ZipFile(wheel) as package:
        names = set(package.namelist())
        required = {
            "modelmesh/resources/instructions.md", "modelmesh/resources/skills/coding/SKILL.md", "modelmesh/static/index.html",
            "modelmesh/input_attachments.py", "modelmesh/cli.py", "modelmesh/history_routes.py",
        }
        assert required <= names, sorted(required - names)
        assert not any(name.startswith(("app/", "agent/", "sandbox/")) for name in names)
        index = package.read("modelmesh/static/index.html").decode()
        # Vite emits relative URLs so the installed UI works below a public
        # path such as /modelmesh/. Resolve those URLs inside the packaged static tree.
        assets = re.findall(r'(?:src|href)=["\'](\./assets/[^"\']+)["\']', index)
        assert assets
        assert all("modelmesh/static/" + asset.removeprefix("./") in names for asset in assets)

    commands = tmp_path / "commands"
    commands.mkdir()
    npm = commands / "npm"
    npm.write_text("#!/bin/sh\nexit 91\n")
    npm.chmod(0o755)
    rebuilt = tmp_path / "rebuilt"
    result = subprocess.run(
        [uv, "build", "--wheel", str(sdist), "--out-dir", str(rebuilt)],
        cwd=tmp_path, env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rebuilt_wheel, = rebuilt.glob("*.whl")
    with zipfile.ZipFile(wheel) as original, zipfile.ZipFile(rebuilt_wheel) as reconstructed:
        for name in original.namelist():
            if name.startswith(("modelmesh/static/", "modelmesh/resources/")):
                assert reconstructed.read(name) == original.read(name), name
