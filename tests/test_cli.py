from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "memex"


def run_memex(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(
        [str(CLI), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_memex_help(tmp_path: Path):
    result = run_memex("--help", cwd=tmp_path)
    assert result.returncode == 0
    help_text = result.stdout + result.stderr
    assert "search" in help_text
    assert "timeline" in help_text
    assert "read" in help_text


def test_memex_search_help(tmp_path: Path):
    result = run_memex("search", "--help", cwd=tmp_path)
    assert result.returncode == 0


def test_memex_read_missing_file(tmp_path: Path):
    result = run_memex("read", "nonexistent.md", cwd=tmp_path)
    assert result.returncode == 1


def test_memex_read_blocks_path_traversal(tmp_path: Path):
    result = run_memex("read", "../../etc/passwd", cwd=tmp_path)
    assert result.returncode == 1


def test_memex_status(tmp_path: Path):
    from memex.paths import get_index_path
    try:
        has_index = get_index_path().exists()
    except ValueError:
        has_index = False
    if not has_index:
        pytest.skip("memex status smoke test requires an existing index")

    result = run_memex("status", cwd=tmp_path)
    assert result.returncode == 0
