from __future__ import annotations

import sqlite3
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
