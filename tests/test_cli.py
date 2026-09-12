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


# ── obs sidecars (v0.17.0) ─────────────────────────────────────────────────
#
# These exercise the full CLI dispatch (subprocess -> typer -> cli.py -> the
# underlying memex.sidecars / memex.observations calls) against a tmp vault
# + tmp state dir from `cli_env` — never the real vault or ~/.memex.

def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_obs(index: Path, rows: list[tuple[int, str, str]]) -> None:
    """rows: (id, doc_path, content). content_hash computed for real so a
    later `read_sidecar` round-trip validates."""
    with closing(connect_index(index)) as conn:
        from memex.observations import init_observation_schema

        init_observation_schema(conn, 8)
        for obs_id, doc_path, content in rows:
            conn.execute(
                "INSERT INTO observations (id, doc_path, content, content_hash, "
                "obs_type, confidence) VALUES (?, ?, ?, ?, 'explicit', 'high')",
                (obs_id, doc_path, content, _hash(content)),
            )
            conn.execute(
                "INSERT INTO fts_observations (rowid, content, obs_type) VALUES (?, ?, 'explicit')",
                (obs_id, content),
            )
        conn.commit()


def test_memex_obs_retag_rewrites_sidecars(tmp_path: Path, cli_env):
    vault = Path(cli_env["MEMEX_MEMEX_PATH"])
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    doc = "projects/p/memos/x.md"
    (vault / "projects" / "p" / "memos").mkdir(parents=True)
    (vault / doc).write_text("---\ntype: memo\n---\n\nBody.\n")

    _seed_obs(index, [(1, doc, "tagged claim")])
    with closing(connect_index(index)) as conn:
        from memex.observations import store_observation_topics

        store_observation_topics(conn, 1, ["old-topic"])
        conn.commit()

    result = run_memex("obs", "retag", "old-topic", "new-topic", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0, result.stderr
    assert "Retagged 1 observations" in result.stdout

    sidecar = vault / "projects" / "p" / "memos" / "x.obs.jsonl"
    assert sidecar.exists()
    line = json.loads(sidecar.read_text().splitlines()[0])
    assert line["topics"] == ["new-topic"]


def test_memex_obs_reassign_apply_moves_sidecar(tmp_path: Path, cli_env):
    vault = Path(cli_env["MEMEX_MEMEX_PATH"])
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    old_doc = "projects/Apps-X/memos/a.md"
    new_doc = "projects/X/memos/a.md"
    # Simulate the documented SOP: `git mv` already happened before --apply.
    (vault / "projects" / "X" / "memos").mkdir(parents=True)
    (vault / new_doc).write_text("---\ntype: memo\n---\n\nBody.\n")

    _seed_obs(index, [(1, old_doc, "moved claim")])
    with closing(connect_index(index)) as conn:
        # reassign_doc_path_prefix also touches `chunks` — minimal stub,
        # same shape as tests/test_obs_reassign.py's fixture.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, "
            "doc_path TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
            "content TEXT NOT NULL, content_hash TEXT NOT NULL, "
            "UNIQUE(doc_path, chunk_index))"
        )
        conn.commit()
    old_sidecar = vault / "projects" / "Apps-X" / "memos" / "a.obs.jsonl"
    old_sidecar.parent.mkdir(parents=True)
    old_sidecar.write_text(
        json.dumps({
            "content": "moved claim", "content_hash": _hash("moved claim"),
            "obs_type": "explicit", "confidence": "high",
            "topics": [], "source_obs": [], "created_at": None,
        }, sort_keys=True) + "\n"
    )

    result = run_memex(
        "obs", "reassign",
        "--from-prefix", "projects/Apps-X/", "--to-prefix", "projects/X/", "--apply",
        cwd=tmp_path, env=cli_env,
    )
    assert result.returncode == 0, result.stderr

    new_sidecar = vault / "projects" / "X" / "memos" / "a.obs.jsonl"
    assert new_sidecar.exists(), "reassign --apply must write the sidecar at the new location"
    assert not old_sidecar.exists(), "reassign --apply must remove the sidecar at the old location"
    with closing(connect_index(index)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM obs_sidecars WHERE doc_path = ?", (old_doc,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM obs_sidecars WHERE doc_path = ?", (new_doc,)
        ).fetchone()[0] == 1


def test_memex_obs_export_sidecars_dry_run_then_apply(tmp_path: Path, cli_env):
    vault = Path(cli_env["MEMEX_MEMEX_PATH"])
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    doc = "projects/p/memos/x.md"
    (vault / "projects" / "p" / "memos").mkdir(parents=True)
    (vault / doc).write_text("---\ntype: memo\n---\n\nBody.\n")
    _seed_obs(index, [(1, doc, "export me")])

    sidecar = vault / "projects" / "p" / "memos" / "x.obs.jsonl"

    dry = run_memex("obs", "export-sidecars", "--json", cwd=tmp_path, env=cli_env)
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["would_write"] == 1
    assert not sidecar.exists()

    applied = run_memex("obs", "export-sidecars", "--apply", "--json", cwd=tmp_path, env=cli_env)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["written"] == 1
    assert sidecar.exists()


def test_memex_obs_ingest_sidecars(tmp_path: Path, cli_env):
    vault = Path(cli_env["MEMEX_MEMEX_PATH"])
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    doc = "projects/p/memos/x.md"
    memos_dir = vault / "projects" / "p" / "memos"
    memos_dir.mkdir(parents=True)
    (vault / doc).write_text("---\ntype: memo\n---\n\nBody.\n")
    with closing(connect_index(index)) as conn:
        from memex.observations import init_observation_schema

        init_observation_schema(conn, 8)
        conn.commit()

    (memos_dir / "x.obs.jsonl").write_text(
        json.dumps({
            "content": "ingested claim", "content_hash": _hash("ingested claim"),
            "obs_type": "explicit", "confidence": "high",
            "topics": [], "source_obs": [], "created_at": None,
        }, sort_keys=True) + "\n"
    )

    result = run_memex("obs", "ingest-sidecars", "--json", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["inserted"] == 1

    with closing(connect_index(index)) as conn:
        row = conn.execute(
            "SELECT content FROM observations WHERE doc_path = ?", (doc,)
        ).fetchone()
    assert row == ("ingested claim",)


def test_memex_obs_sidecars_health_report(tmp_path: Path, cli_env):
    vault = Path(cli_env["MEMEX_MEMEX_PATH"])
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    doc = "projects/p/memos/x.md"
    (vault / "projects" / "p" / "memos").mkdir(parents=True)
    (vault / doc).write_text("---\ntype: memo\n---\n\nBody.\n")
    _seed_obs(index, [(1, doc, "unsidecarred claim")])

    result = run_memex("obs", "sidecars", "--json", cwd=tmp_path, env=cli_env)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert doc in report["missing"]
    assert report["sidecar_count"] == 0


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


def test_memex_obs_reassign_apply_keeps_old_sidecar_when_new_write_fails(tmp_path: Path, cli_env):
    """Running reassign BEFORE the `git mv` step: the target folder doesn't
    exist, so the new sidecar can't be written — the old one must survive
    (it is the only vault-side copy of those rows)."""
    vault = Path(cli_env["MEMEX_MEMEX_PATH"])
    index = Path(cli_env["MEMEX_INDEX_PATH"])
    old_doc = "projects/Apps-X/memos/a.md"

    _seed_obs(index, [(1, old_doc, "moved claim")])
    with closing(connect_index(index)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, "
            "doc_path TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
            "content TEXT NOT NULL, content_hash TEXT NOT NULL, "
            "UNIQUE(doc_path, chunk_index))"
        )
        conn.commit()
    old_sidecar = vault / "projects" / "Apps-X" / "memos" / "a.obs.jsonl"
    old_sidecar.parent.mkdir(parents=True)
    old_sidecar.write_text(
        json.dumps({
            "content": "moved claim", "content_hash": _hash("moved claim"),
            "obs_type": "explicit", "confidence": "high",
            "topics": [], "source_obs": [], "created_at": None,
        }, sort_keys=True) + "\n"
    )

    result = run_memex(
        "obs", "reassign",
        "--from-prefix", "projects/Apps-X/", "--to-prefix", "projects/X/", "--apply",
        cwd=tmp_path, env=cli_env,
    )
    assert result.returncode == 0, result.stderr
    assert "kept old sidecar" in result.stderr
    assert old_sidecar.exists()
    assert not (vault / "projects" / "X" / "memos" / "a.obs.jsonl").exists()
