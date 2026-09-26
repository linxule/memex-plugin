"""Verify built archives and exercise the installed wheel outside the checkout."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    wheels = list(args.dist.resolve().glob("*.whl"))
    sdists = list(args.dist.resolve().glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected exactly one wheel and sdist"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert all(n.startswith(("memex/", "memex_plugin-")) for n in names), names
        metadata = BytesParser().parsebytes(archive.read(next(n for n in names if n.endswith("/METADATA"))))
        assert metadata["Name"] == "memex-plugin"
        assert metadata["Version"] == project["version"]
        assert "memex/cli.py" in names
        assert "memex/scripts/index_rebuild.py" in names
    with tarfile.open(sdists[0]) as archive:
        names = [n.split("/", 1)[1] for n in archive.getnames() if "/" in n]
        allowed = {"pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "PKG-INFO", ".gitignore"}
        assert all(n in allowed or n.startswith("src/memex/") for n in names), names

    # No host config, credentials, plugin overlays, vaults, or source imports.
    with tempfile.TemporaryDirectory(prefix="memex-wheel-") as scratch:
        base = Path(scratch)
        venv = base / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheels[0])], check=True)
        vault = base / "vault"
        memo = vault / "projects" / "example" / "memos" / "test.md"
        memo.parent.mkdir(parents=True)
        memo.write_text("---\ntype: memo\ntitle: Packaging test\n---\n\nRegistry smoke narwhal.\n")
        config = base / ".memex"
        config.mkdir()
        (config / "config.json").write_text(json.dumps({
            "memex_path": str(vault), "state_dir": str(config),
            "embeddings": {"enabled": False}, "reranker": {"enabled": False},
        }))
        env = {"PATH": os.environ["PATH"], "HOME": str(base), "USERPROFILE": str(base),
               "PYTHONNOUSERSITE": "1", "NO_COLOR": "1", "MEMEX_STATE_DIR": str(config)}
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

        def run(*argv: str) -> str:
            result = subprocess.run(argv, cwd=base, env=env, text=True, capture_output=True, check=False)
            assert result.returncode == 0, f"{argv}:\n{result.stdout}\n{result.stderr}"
            return result.stdout

        version = run(str(python), "-I", "-c", "import memex; print(memex.__version__)").strip()
        assert version == project["version"], version
        cli = str(venv / ("Scripts/memex.exe" if os.name == "nt" else "bin/memex"))
        assert "search" in run(cli, "--help")
        assert Path(run(cli, "path").strip()).resolve() == vault.resolve()
        run(cli, "index", "rebuild", "--no-embeddings")
        assert "narwhal" in run(cli, "search", "narwhal").lower()
        run(cli, "status")
        # The sdist must also be independently buildable, not just uploadable.
        rebuilt = base / "rebuilt"
        subprocess.run(["uv", "build", "--wheel", str(sdists[0]), "--out-dir", str(rebuilt)], check=True)
        with zipfile.ZipFile(next(rebuilt.glob("*.whl"))) as archive, zipfile.ZipFile(wheels[0]) as original:
            assert archive.read("memex/__init__.py") == original.read("memex/__init__.py")
    print(f"Distribution verified: memex-plugin {version}; wheel install, offline vault search, sdist rebuild")


if __name__ == "__main__":
    main()
