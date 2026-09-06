"""Regressions from the 2026-09-06 adversarial review of the maintenance commits.

Each test pins a defect the reviewers reproduced: offline incremental runs
stripping vectors without a heal path, aborted atomic rebuilds leaking the
temp database, lock contention failing instantly, `--before` admitting undated
docs on the keyword path, and credential/hook/version edge cases.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memex import credentials, db_utils
from memex.scripts import embeddings
from memex.scripts import hybrid_search as hs
from memex.scripts import index_rebuild as ir
from memex.scripts import search as legacy_search


# ---------------------------------------------------------------------------
# Index: offline incremental must converge to zero gaps on the next keyed run
# ---------------------------------------------------------------------------

class _FakePipeline:
    enabled = True
    model = "fake-embed"
    dimensions = 4

    class _Provider:
        @staticmethod
        def embed_texts(texts, task_type="document"):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    _provider_impl = _Provider()

    def embed_chunks(self, chunks, conn):
        vector = embeddings.serialize_f32([1.0, 0.0, 0.0, 0.0])
        return [(chunk.index, vector) for chunk in chunks]


class _DisabledPipeline:
    enabled = False
    _provider_impl = None


def _memo(vault, body="first version"):
    path = vault / "projects" / "sample" / "memos" / "sample.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: memo\ntitle: Sample\n---\n\n# Sample\n\n{body}\n")
    return path


@pytest.fixture
def small_dims(monkeypatch):
    config = {"dimensions": 4, "index_dimensions": 4}
    monkeypatch.setattr(embeddings, "get_embedding_config", lambda: config)
    return config


def _require_vec(tmp_path):
    conn = sqlite3.connect(":memory:")
    ok = db_utils.load_vec_extension(conn)
    conn.close()
    if not ok:
        pytest.skip("sqlite-vec unavailable")


def test_offline_edit_is_embedded_by_next_keyed_incremental(tmp_path, monkeypatch, small_dims, capsys):
    _require_vec(tmp_path)
    monkeypatch.setattr(ir, "EmbeddingPipeline", _FakePipeline)
    _memo(tmp_path)
    assert ir.rebuild_incremental(tmp_path)["embedding_gaps"]["chunks"] == 0

    # Keyless session (Claude Code inherits env at launch): the doc changes,
    # chunks and hash are refreshed, vectors are not.
    _memo(tmp_path, body="second version with new content")
    monkeypatch.setattr(ir, "EmbeddingPipeline", _DisabledPipeline)
    offline = ir.rebuild_incremental(tmp_path)
    assert offline["updated"] == 1
    assert offline["embedding_gaps"]["chunks"] >= 1
    assert "no API key" in capsys.readouterr().err

    # Keyed run sees an unchanged hash — the gap must still close.
    monkeypatch.setattr(ir, "EmbeddingPipeline", _FakePipeline)
    healed = ir.rebuild_incremental(tmp_path)
    assert healed["unchanged"] == 1
    assert healed["healed"]["chunks_embedded"] >= 1
    assert healed["embedding_gaps"]["chunks"] == 0


def test_keyed_incremental_without_gaps_skips_heal_pass(tmp_path, monkeypatch, small_dims):
    _require_vec(tmp_path)
    monkeypatch.setattr(ir, "EmbeddingPipeline", _FakePipeline)
    _memo(tmp_path)
    ir.rebuild_incremental(tmp_path)
    calls = []
    monkeypatch.setattr(ir, "reembed_missing", lambda *a, **k: calls.append(a))
    stats = ir.rebuild_incremental(tmp_path)
    assert "healed" not in stats
    assert calls == []


# ---------------------------------------------------------------------------
# Index: aborted atomic --full leaves no .tmp behind
# ---------------------------------------------------------------------------

def test_aborted_atomic_full_rebuild_removes_temp_database(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "EmbeddingPipeline", _DisabledPipeline)
    _memo(tmp_path)
    ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)
    index_path = tmp_path / "_index.sqlite"
    before = index_path.read_bytes()

    def fail_after_fts(conn, doc_path, memex, pipeline):
        raise RuntimeError("simulated read failure")

    monkeypatch.setattr(ir, "index_document", fail_after_fts)
    with pytest.raises(RuntimeError, match="sample.md"):
        ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)

    assert index_path.read_bytes() == before
    assert not list(tmp_path.glob("_index.sqlite.tmp*"))


def test_keyboard_interrupt_during_atomic_full_removes_temp_database(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "EmbeddingPipeline", _DisabledPipeline)
    _memo(tmp_path)

    def interrupt(conn, doc_path, memex, pipeline):
        raise KeyboardInterrupt

    monkeypatch.setattr(ir, "index_document", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)
    assert not list(tmp_path.glob("_index.sqlite.tmp*"))


# ---------------------------------------------------------------------------
# Locks: bounded wait instead of instant exit 3; embed-missing takes the lock
# ---------------------------------------------------------------------------

def test_rebuild_lock_waits_for_shared_writer_to_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(db_utils, "_LOCK_POLL_SECONDS", 0.01)
    # Hold the shared lock in a child process for a moment, then release.
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time; sys.path.insert(0, sys.argv[1]); "
         "from pathlib import Path; Path.home = staticmethod(lambda: Path(sys.argv[2])); "
         "from memex import db_utils\n"
         "with db_utils.writer_lock():\n"
         "    print('held', flush=True); time.sleep(0.6)",
         str(Path(__file__).resolve().parents[1] / "src"), str(tmp_path)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        assert child.poll() is None  # writer still holds the shared lock
        started = time.monotonic()
        with db_utils.rebuild_lock(timeout=5.0):
            waited = time.monotonic() - started
        # Acquired only after the child released (it sleeps 0.6s after "held").
        assert child.poll() is not None or waited > 0.1
        assert waited < 5.0
    finally:
        child.wait(timeout=5)


def test_rebuild_lock_gives_up_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(db_utils, "_LOCK_POLL_SECONDS", 0.01)
    with db_utils.writer_lock():
        with pytest.raises(SystemExit) as exc:
            with db_utils.rebuild_lock(timeout=0.05):
                pass
    assert exc.value.code == 3


def test_embed_missing_cli_holds_writer_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "get_memex_path", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(db_utils, "_LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(sys, "argv", ["memex-index", "--embed-missing"])
    monkeypatch.setattr(ir, "reembed_missing", lambda memex: {"error": None})
    with db_utils.rebuild_lock():
        with pytest.raises(SystemExit) as exc:
            ir._run()
    assert exc.value.code == 3


# ---------------------------------------------------------------------------
# Search: --before alone excludes undated docs on the keyword path too
# ---------------------------------------------------------------------------

class _QueryPipeline:
    enabled = True

    def embed_query(self, query):
        return struct.pack("2f", 1.0, 0.0)


@pytest.fixture
def before_index():
    conn = sqlite3.connect(":memory:")
    if not db_utils.load_vec_extension(conn):
        pytest.skip("sqlite-vec unavailable")
    conn.execute("CREATE VIRTUAL TABLE fts_content USING fts5(path, title, content, type, project, date)")
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, doc_path TEXT, content TEXT)")
    conn.execute(
        "CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[2], "
        "doc_project text, doc_type text, doc_date integer)"
    )
    for path, date in (("dated.md", "2025-12-31"), ("undated.md", ""), ("later.md", "2026-02-01")):
        conn.execute("INSERT INTO fts_content VALUES (?, ?, ?, ?, ?, ?)",
                     (path, path, "retrieval", "memo", "demo", date))
        chunk_id = conn.execute("INSERT INTO chunks(doc_path, content) VALUES (?, 'retrieval')",
                                (path,)).lastrowid
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding, doc_project, doc_type, doc_date) VALUES (?, ?, ?, ?, ?)",
            (chunk_id, struct.pack("2f", 1.0, 0.0), "demo", "memo", int(date.replace("-", "")) if date else 0),
        )
    conn.commit()
    yield conn
    conn.close()


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
def test_before_alone_excludes_undated_docs_in_every_mode(before_index, mode):
    results = hs.hybrid_search(before_index, "retrieval", pipeline=_QueryPipeline(),
                               mode=mode, before="2026-01-01")
    assert {r.path for r in results} == {"dated.md"}


@pytest.mark.parametrize("query", ["temporal", "x"])
def test_legacy_fts_and_like_fallback_before_excludes_undated_docs(tmp_path, monkeypatch, query):
    """Legacy `memex.scripts.search` path (FTS5 + LIKE fallback), built from files."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(ir, "EmbeddingPipeline", _DisabledPipeline)
    memos = tmp_path / "projects" / "example" / "memos"
    memos.mkdir(parents=True)
    (memos / "dated.md").write_text("---\ntitle: dated\ntype: memo\ndate: 2026-03-01\n---\n\ntemporal x\n")
    (memos / "undated.md").write_text("---\ntitle: undated\ntype: memo\n---\n\ntemporal x\n")
    legacy_search.rebuild_index(tmp_path)
    rows = legacy_search.search(tmp_path, query, before="2026-04-01")
    assert {row["title"] for row in rows} == {"dated"}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@pytest.fixture
def cred_state(tmp_path, monkeypatch):
    from memex.config import reset_settings
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / "state"))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield tmp_path / "state"
    reset_settings()


def test_save_key_tightens_preexisting_credentials_directory(cred_state):
    cred_dir = credentials.gemini_key_path().parent
    cred_dir.mkdir(parents=True)
    os.chmod(cred_dir, 0o755)
    credentials.save_gemini_key("AIza" + "x" * 35)
    assert stat.S_IMODE(cred_dir.stat().st_mode) == 0o700


def test_clear_key_reports_directory_at_key_path(cred_state):
    credentials.gemini_key_path().mkdir(parents=True)
    with pytest.raises(ValueError, match="is a directory"):
        credentials.clear_gemini_key()


def test_invalid_saved_file_error_names_the_file(cred_state):
    path = credentials.gemini_key_path()
    path.parent.mkdir(parents=True)
    path.write_text("op://Private/Gemini/key\n")
    with pytest.raises(ValueError) as exc:
        credentials.resolve_gemini_key()
    assert str(path) in str(exc.value)
    assert "set-key" in str(exc.value)


def test_set_key_reads_piped_stdin_without_prompt_noise(cred_state):
    from typer.testing import CliRunner
    from memex.cli import app
    key = "AIza" + "y" * 35
    result = CliRunner().invoke(app, ["auth", "set-key"], input=key + "\n")
    assert result.exit_code == 0, result.output
    assert "echoed" not in result.output
    assert key not in result.output
    assert credentials.gemini_key_path().read_text() == key + "\n"


def test_gemini_provider_repr_redacts_key(cred_state, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "z" * 35)
    provider = embeddings.GeminiProvider({})
    assert "z" * 35 not in repr(provider)
    assert "redacted" in repr(provider)


# ---------------------------------------------------------------------------
# Version lookup never breaks import
# ---------------------------------------------------------------------------

def test_version_import_survives_unsupported_layout_without_metadata(tmp_path):
    package_root = tmp_path / "lib"
    import shutil
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "memex", package_root / "memex",
                    ignore=shutil.ignore_patterns("__pycache__"))
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import memex; print(memex.__version__)",
         str(package_root)],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "0.0.0+unknown"


# ---------------------------------------------------------------------------
# SessionEnd hook: stale staging dirs are swept
# ---------------------------------------------------------------------------

def test_hook_sweeps_stale_staging_dirs_but_keeps_fresh_ones(tmp_path):
    path = Path(__file__).resolve().parents[1] / "hooks" / "session-end.py"
    spec = importlib.util.spec_from_file_location("session_end_hook_sweep", path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    transcripts = tmp_path / "transcripts"
    stale = transcripts / ".archive-old"
    fresh = transcripts / ".archive-new"
    for d in (stale, fresh):
        d.mkdir(parents=True)
        (d / "x.jsonl").write_text("{}")
    old = time.time() - hook.STALE_STAGING_SECONDS - 60
    os.utime(stale, (old, old))
    assert hook._sweep_stale_staging(transcripts) == 1
    assert not stale.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# Orphan vec rows must not mask gaps, and embed-missing prunes them
# ---------------------------------------------------------------------------

def test_orphan_vec_rows_do_not_mask_chunk_gaps(tmp_path, monkeypatch, small_dims):
    _require_vec(tmp_path)
    monkeypatch.setattr(ir, "EmbeddingPipeline", _FakePipeline)
    _memo(tmp_path)
    ir.rebuild_incremental(tmp_path)
    conn = db_utils.connect_index(tmp_path / "_index.sqlite")
    try:
        assert db_utils.load_vec_extension(conn)
        # One real gap (unembedded chunk) hidden behind one orphan vector.
        conn.execute(
            "INSERT INTO chunks (doc_path, chunk_index, content, content_hash) "
            "VALUES ('x.md', 0, 'unembedded', 'h')"
        )
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding, doc_project, doc_type, doc_date) VALUES (999, ?, '', '', 0)",
            (embeddings.serialize_f32([0.0, 1.0, 0.0, 0.0]),),
        )
        conn.commit()
    finally:
        conn.close()

    gaps = ir.count_embedding_gaps(tmp_path)
    assert gaps["orphans"] == 1
    assert gaps["chunks"] == 1

    stats = ir.reembed_missing(tmp_path)
    assert stats["chunks_orphans_pruned"] == 1
    assert stats["chunks_embedded"] == 1
    after = ir.count_embedding_gaps(tmp_path)
    assert (after["orphans"], after["chunks"]) == (0, 0)


def test_embed_missing_prunes_orphans_even_without_provider(tmp_path, monkeypatch, small_dims):
    _require_vec(tmp_path)
    monkeypatch.setattr(ir, "EmbeddingPipeline", _FakePipeline)
    _memo(tmp_path)
    ir.rebuild_incremental(tmp_path)
    conn = db_utils.connect_index(tmp_path / "_index.sqlite")
    try:
        assert db_utils.load_vec_extension(conn)
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding, doc_project, doc_type, doc_date) VALUES (999, ?, '', '', 0)",
            (embeddings.serialize_f32([0.0, 1.0, 0.0, 0.0]),),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(ir, "EmbeddingPipeline", _DisabledPipeline)
    stats = ir.reembed_missing(tmp_path)
    assert stats["error"]  # no provider
    assert stats["chunks_orphans_pruned"] == 1

