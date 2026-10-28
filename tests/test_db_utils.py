from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from memex import db_utils, observations


def test_connect_index_returns_configured_connection(monkeypatch):
    conn = Mock(spec=sqlite3.Connection)
    connect = Mock(return_value=conn)
    monkeypatch.setattr(db_utils.sqlite3, "connect", connect)
    index_path = Path("unused-index.sqlite")

    assert db_utils.connect_index(index_path) is conn

    connect.assert_called_once_with(index_path, timeout=10.0)
    assert conn.execute.call_args_list == [
        call("PRAGMA journal_mode = WAL"),
        call("PRAGMA synchronous = NORMAL"),
        call("PRAGMA busy_timeout = 10000"),
    ]
    conn.close.assert_not_called()


@pytest.mark.parametrize("failed_pragma", range(3))
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, KeyboardInterrupt])
def test_connect_index_closes_connection_when_setup_fails(
    monkeypatch, failed_pragma, error_type
):
    conn = Mock(spec=sqlite3.Connection)
    error = error_type("interrupted setup")
    conn.execute.side_effect = [None] * failed_pragma + [error]
    monkeypatch.setattr(db_utils.sqlite3, "connect", Mock(return_value=conn))

    with pytest.raises(error_type) as caught:
        db_utils.connect_index(Path("unused-index.sqlite"))

    assert caught.value is error
    assert conn.execute.call_count == failed_pragma + 1
    conn.close.assert_called_once_with()


@pytest.mark.parametrize(
    "loader", [db_utils.load_vec_extension, observations.load_sqlite_vec]
)
@pytest.mark.parametrize("load_fails", [False, True])
def test_vec_load_disables_extension_loading_even_on_failure(
    monkeypatch, loader, load_fails
):
    conn = Mock(spec=sqlite3.Connection)
    load = Mock(
        side_effect=sqlite3.OperationalError("extension unavailable")
        if load_fails
        else None
    )
    monkeypatch.setitem(sys.modules, "sqlite_vec", SimpleNamespace(load=load))

    assert loader(conn) is not load_fails

    load.assert_called_once_with(conn)
    assert conn.enable_load_extension.call_args_list == [call(True), call(False)]
    conn.close.assert_not_called()


@pytest.mark.parametrize(
    "loader", [db_utils.load_vec_extension, observations.load_sqlite_vec]
)
def test_vec_load_reports_missing_package_without_touching_connection(
    monkeypatch, loader
):
    conn = Mock(spec=sqlite3.Connection)
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)

    assert loader(conn) is False

    assert conn.mock_calls == []


def test_observation_loader_delegates_to_shared_helper(monkeypatch):
    conn = Mock(spec=sqlite3.Connection)
    loader = Mock(return_value=False)
    monkeypatch.setattr(db_utils, "load_vec_extension", loader)

    assert observations.load_sqlite_vec(conn) is False

    loader.assert_called_once_with(conn)


@pytest.mark.parametrize("contender", [db_utils.rebuild_lock, db_utils.writer_lock])
def test_rebuild_lock_excludes_other_rebuilds_and_writers(
    tmp_path, monkeypatch, capsys, contender
):
    monkeypatch.setenv("HOME", str(tmp_path))

    with db_utils.rebuild_lock():
        with pytest.raises(SystemExit) as caught:
            with contender():
                pytest.fail("A concurrent write entered an exclusive rebuild")
        assert caught.value.code == 3

    assert "Retry after" in capsys.readouterr().err
    with contender():
        pass
    assert (tmp_path / ".memex" / "locks" / "full-rebuild.lock").exists()


def test_writer_lock_allows_writers_but_excludes_rebuilds(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))

    with db_utils.writer_lock(), db_utils.writer_lock():
        with pytest.raises(SystemExit) as caught:
            with db_utils.rebuild_lock():
                pytest.fail("A rebuild entered while observations were being written")
        assert caught.value.code == 3

    assert "Retry after" in capsys.readouterr().err
    with db_utils.rebuild_lock():
        pass


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_rebuild_lock_releases_after_errors(tmp_path, monkeypatch, error_type):
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(error_type):
        with db_utils.rebuild_lock():
            raise error_type("rebuild failed")

    with db_utils.rebuild_lock():
        pass
    with db_utils.writer_lock():
        pass


def test_state_dir_without_pydantic_follows_the_env_like_the_cli(tmp_path, monkeypatch):
    """Hooks run without pydantic, so memex.paths falls back to reading
    config.json; it must still honour MEMEX_STATE_DIR first, as pydantic's env
    source does, or a hook and the CLI disagree about where state lives."""
    from memex import paths
    from memex.scripts import utils

    home, state = tmp_path / "home", tmp_path / "state"
    (home / ".memex").mkdir(parents=True)
    (home / ".memex" / "config.json").write_text('{"state_dir": "%s"}' % (tmp_path / "from-config"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(paths, "_try_get_settings", lambda: None)

    assert paths.get_state_dir() == (tmp_path / "from-config").resolve()
    monkeypatch.setenv("MEMEX_STATE_DIR", str(state))
    assert paths.get_state_dir() == state.resolve()
    # state.json stays where every version kept it (Codex round 4): no migration.
    assert utils.get_state_file() == home / ".memex" / "state.json"
    assert paths.get_lock_dir() == state.resolve() / "locks"


def _configure(monkeypatch, tmp_path, *, state: str, index: Path | None = None):
    from memex.config import reset_settings

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMEX_MEMEX_PATH", str(vault))
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / state))
    if index is not None:
        monkeypatch.setenv("MEMEX_INDEX_PATH", str(index))
    reset_settings()


def test_the_index_lock_is_one_place_per_user_whatever_the_configuration(tmp_path, monkeypatch):
    """Two review rounds (Codex, Kimi; 2026-10-28) showed every keyed design
    could split: by state dir (shared MEMEX_INDEX_PATH), by index (an
    `extract --index` caller, a legacy in-vault index). One fixed place cannot."""
    from memex import paths

    seen = set()
    for state, index in (("state-a", None), ("state-b", tmp_path / "shared" / "_index.sqlite"),
                         ("state-c", tmp_path / "vault" / "_index.sqlite")):
        _configure(monkeypatch, tmp_path, state=state, index=index)
        seen.add(paths.get_index_lock_dir())
    assert seen == {tmp_path / "home" / ".memex" / "locks"}


def test_a_writer_under_one_state_dir_blocks_a_rebuild_under_another(tmp_path, monkeypatch):
    """The same split, across real processes: the child holds the shared writer
    lock with state A, the parent asks for the exclusive rebuild lock with state B."""
    shared = tmp_path / "shared" / "_index.sqlite"
    _configure(monkeypatch, tmp_path, state="state-b", index=shared)
    child_env = {**os.environ, "MEMEX_STATE_DIR": str(tmp_path / "state-a")}
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time; sys.path.insert(0, sys.argv[1]); from memex import db_utils\n"
         "with db_utils.writer_lock():\n    print('held', flush=True); time.sleep(3)",
         str(Path(__file__).resolve().parents[1] / "src")],
        stdout=subprocess.PIPE, text=True, env=child_env,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(SystemExit) as caught:
            with db_utils.rebuild_lock(timeout=0.05):
                pytest.fail("a rebuild ran while another process was writing observations")
        assert caught.value.code == 3
    finally:
        child.kill()
        child.wait(timeout=5)


def test_empty_path_env_falls_through_to_config_for_hooks_and_cli(tmp_path, monkeypatch):
    """Codex/Kimi round 2: an empty MEMEX_STATE_DIR made pydantic use the current
    folder (and a blank-to-default validator then masked config.json), while
    the hooks' fallback fell through to config.json. Both now treat empty as
    unset (env_ignore_empty), take other values as given, and match names in
    any case."""
    from memex import paths
    from memex.config import get_settings, reset_settings

    home = tmp_path / "home"
    (home / ".memex").mkdir(parents=True)
    (home / ".memex" / "config.json").write_text('{"state_dir": "%s"}' % (tmp_path / "from-config"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(paths, "_try_get_settings", lambda: None)

    def both():
        reset_settings()
        return get_settings().state_dir, paths._read_config_value("state_dir")

    monkeypatch.setenv("MEMEX_STATE_DIR", "")
    assert both() == (str(tmp_path / "from-config"),) * 2
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / "env"))
    assert both() == (str(tmp_path / "env"),) * 2
    monkeypatch.delenv("MEMEX_STATE_DIR")
    monkeypatch.setenv("memex_state_dir", str(tmp_path / "lower"))
    assert both() == (str(tmp_path / "lower"),) * 2
    reset_settings()


def test_both_spellings_of_one_env_name_resolve_alike_for_hooks_and_cli(tmp_path, monkeypatch):
    """Codex round 3: with MEMEX_STATE_DIR and memex_state_dir both set, pydantic
    took the last one in the environment and the fallback the upper-case one."""
    from memex import paths
    from memex.config import get_settings, reset_settings

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(paths, "_try_get_settings", lambda: None)
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / "upper"))
    monkeypatch.setenv("memex_state_dir", str(tmp_path / "lower"))
    reset_settings()
    assert get_settings().state_dir == paths._read_config_value("state_dir")
    reset_settings()


def test_a_custom_state_dir_upgrade_keeps_the_state_written_before_it(tmp_path, monkeypatch):
    """Codex round 4: moving state.json with state_dir left an upgrading
    custom-state_dir install without the flags 0.22.0 had written to
    ~/.memex/state.json, so SessionEnd archived the same transcript again."""
    import json

    from memex.config import reset_settings
    from memex.scripts import utils

    home = tmp_path / "home"
    (home / ".memex").mkdir(parents=True)
    sid = "abcdef12-3456-4789-8abc-def012345678"
    (home / ".memex" / "state.json").write_text(json.dumps(
        {"processed_sessions": {sid: {"transcript_archived_at": "2026-10-27T10:00:00"}}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / "custom-state"))
    reset_settings()
    assert utils.is_session_processed(sid, "transcript_archived")
    reset_settings()
