from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from memex import cli
from memex.db_utils import connect_index


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "memex"


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    vault.mkdir()
    state.mkdir()
    (vault / "note.md").write_text("A note from the test vault.\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEMEX_")}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.update({
        "MEMEX_MEMEX_PATH": str(vault),
        "MEMEX_STATE_DIR": str(state),
        "MEMEX_INDEX_PATH": str(state / "_index.sqlite"),
        "MEMEX_EMBEDDINGS__ENABLED": "false",
        "UV_NO_SYNC": "1",
    })
    return env


def run_memex(*args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def test_memex_help(tmp_path: Path, cli_env):
    result = run_memex("--help", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0
    help_text = result.stdout + result.stderr
    assert "search" in help_text
    assert "timeline" in help_text
    assert "read" in help_text


def test_memex_search_help(tmp_path: Path, cli_env):
    result = run_memex("search", "--help", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0


def test_memex_read_missing_file(tmp_path: Path, cli_env):
    result = run_memex("read", "nonexistent.md", cwd=tmp_path, env=cli_env)
    assert result.returncode == 1
    assert "Not found: nonexistent.md" in result.stderr


def test_memex_read_blocks_path_traversal(tmp_path: Path, cli_env):
    result = run_memex("read", "../../etc/passwd", cwd=tmp_path, env=cli_env)
    assert result.returncode == 1
    assert "Path traversal blocked" in result.stderr


def test_memex_read_from_another_directory(tmp_path: Path, cli_env):
    result = run_memex("read", "note.md", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "A note from the test vault.\n"


def test_memex_status_without_index(tmp_path: Path, cli_env):
    result = run_memex("status", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"exists": False}


def test_memex_status_with_index(tmp_path: Path, cli_env):
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    with closing(connect_index(index)) as conn:
        conn.executescript("""
            CREATE VIRTUAL TABLE fts_content USING fts5(path, type, content);
            INSERT INTO fts_content VALUES ('note.md', 'memo', 'Test note');
        """)
        conn.commit()

    result = run_memex("status", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["exists"] is True
    assert status["fts_documents"] == 1
    assert status["fts_by_type"] == {"memo": 1}


@pytest.fixture
def script_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise real cwd switching while keeping every lookup in this fixture."""
    from memex import paths

    caller = tmp_path / "caller"
    vault = tmp_path / "vault"
    caller.mkdir()
    vault.mkdir()
    monkeypatch.chdir(caller)
    original_argv = ["host-app", "original-argument"]
    monkeypatch.setattr(sys, "argv", original_argv)
    monkeypatch.setattr(paths, "get_memex_path", lambda: vault)
    return caller, vault, original_argv


@pytest.mark.parametrize("exit_code", [None, 0])
def test_delegate_restores_process_context(script_call, monkeypatch, exit_code):
    caller, vault, original_argv = script_call

    def script_main():
        assert Path.cwd() == vault
        assert sys.argv == ["search.py", "query", "--format", "json"]
        if exit_code is not None:
            raise SystemExit(exit_code)

    def import_script(name):
        assert name == "memex.scripts.search"
        return SimpleNamespace(main=script_main)

    monkeypatch.setattr(cli.importlib, "import_module", import_script)
    cli._delegate("search.py", ["query", "--format", "json"])
    assert Path.cwd() == caller
    assert sys.argv is original_argv


@pytest.mark.parametrize("failure", [SystemExit(7), RuntimeError("script failed")])
def test_delegate_restores_process_context_after_failure(script_call, monkeypatch, failure):
    caller, vault, original_argv = script_call

    def script_main():
        assert Path.cwd() == vault
        raise failure

    monkeypatch.setattr(cli.importlib, "import_module", lambda name: SimpleNamespace(main=script_main))
    with pytest.raises(type(failure)) as raised:
        cli._delegate("search.py", ["query"])
    assert raised.value is failure
    assert Path.cwd() == caller
    assert sys.argv is original_argv


def test_delegate_restores_process_context_after_import_error(script_call, monkeypatch):
    caller, vault, original_argv = script_call

    def fail_import(name):
        assert Path.cwd() == vault
        raise ImportError("missing script dependency")

    monkeypatch.setattr(cli.importlib, "import_module", fail_import)
    with pytest.raises(ImportError, match="missing script dependency"):
        cli._delegate("search.py", ["query"])
    assert Path.cwd() == caller
    assert sys.argv is original_argv


@pytest.mark.parametrize("exit_code", [0, 2])
def test_ask_restores_process_context_and_forwards_arguments(script_call, monkeypatch, exit_code):
    import memex.ask as ask_module

    caller, vault, original_argv = script_call
    index = vault / "test.sqlite"
    monkeypatch.setattr(cli, "get_index_path", lambda selected_vault: index)

    def ask_main():
        assert Path.cwd() == vault
        assert sys.argv == [
            "memex.ask", "What changed?",
            "--index", str(index), "--vault", str(vault),
            "--depth", "thorough", "--project", "memex", "--limit", "3",
        ]
        raise SystemExit(exit_code)

    monkeypatch.setattr(ask_module, "main", ask_main)
    result = CliRunner().invoke(cli.app, [
        "ask", "What changed?", "--depth", "thorough", "--project", "memex", "--limit", "3",
    ])
    assert result.exit_code == exit_code, result.exception
    assert Path.cwd() == caller
    assert sys.argv is original_argv
