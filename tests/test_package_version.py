"""Version lookup must work in both plugin source trees and installed wheels."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INIT = Path(__file__).resolve().parents[1] / "src" / "memex" / "__init__.py"


@pytest.mark.parametrize("source_layout", [False, True])
def test_version_uses_metadata_for_its_install_layout(tmp_path, source_layout):
    package_root = tmp_path / ("src" if source_layout else "site-packages")
    package = package_root / "memex"
    package.mkdir(parents=True)
    shutil.copyfile(INIT, package / "__init__.py")

    # An installed wheel must ignore unrelated ancestor project metadata;
    # a checkout must prefer its live source over stale installed metadata.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "memex"\nversion = "1.2.3"\n'
    )
    metadata = package_root / "memex-1.0.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: memex\nVersion: 1.0.0\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "import memex; print(memex.__version__)", str(package_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ("1.2.3" if source_layout else "1.0.0")
