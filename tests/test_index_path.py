"""get_index_path: configured vault → state dir; any other vault → in-vault."""
from __future__ import annotations

from pathlib import Path

import pytest

from memex.config import Settings
from memex.paths import get_index_path


@pytest.fixture
def vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("MEMEX_INDEX_PATH", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "state"
    settings = Settings(memex_path=str(vault), state_dir=str(state))
    return vault, state, settings


def test_fresh_install_defaults_to_state_dir(vaults) -> None:
    vault, state, settings = vaults
    assert get_index_path(vault, settings) == state / "_index.sqlite"
    assert get_index_path(None, settings) == state / "_index.sqlite"


def test_legacy_in_vault_index_is_honoured(vaults) -> None:
    vault, state, settings = vaults
    (vault / "_index.sqlite").touch()
    assert get_index_path(vault, settings) == vault / "_index.sqlite"


def test_state_dir_index_wins_over_legacy(vaults) -> None:
    vault, state, settings = vaults
    (vault / "_index.sqlite").touch()
    state.mkdir()
    (state / "_index.sqlite").touch()
    assert get_index_path(vault, settings) == state / "_index.sqlite"


def test_explicit_index_path_wins(vaults, tmp_path: Path) -> None:
    vault, state, settings = vaults
    (vault / "_index.sqlite").touch()
    pinned = tmp_path / "elsewhere" / "idx.sqlite"
    settings = settings.model_copy(update={"index_path": str(pinned)})
    assert get_index_path(vault, settings) == pinned.resolve()


def test_env_override(vaults, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, state, settings = vaults
    monkeypatch.setenv("MEMEX_INDEX_PATH", str(tmp_path / "env.sqlite"))
    assert get_index_path(vault, settings) == (tmp_path / "env.sqlite").resolve()


def test_other_vault_stays_in_vault(vaults, tmp_path: Path) -> None:
    """A tmp vault in a test must never resolve to the configured vault's index."""
    vault, state, settings = vaults
    state.mkdir()
    (state / "_index.sqlite").touch()
    other = tmp_path / "other-vault"
    other.mkdir()
    assert get_index_path(other, settings) == other / "_index.sqlite"


def test_no_configured_vault_falls_back_in_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("MEMEX_INDEX_PATH", raising=False)
    settings = Settings(memex_path=None, state_dir=str(tmp_path / "state"))
    assert get_index_path(tmp_path, settings) == tmp_path / "_index.sqlite"


def test_explicit_index_path_creates_parent_dir(vaults, tmp_path: Path) -> None:
    vault, state, settings = vaults
    pinned = tmp_path / "deep" / "er" / "idx.sqlite"
    settings = settings.model_copy(update={"index_path": str(pinned)})
    assert get_index_path(vault, settings) == pinned.resolve()
    assert pinned.parent.is_dir()


def test_atomic_rebuild_with_index_outside_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tmp/bak must follow the index; the vault must stay free of sqlite files."""
    from memex.scripts import index_rebuild as ir

    vault = tmp_path / "vault"
    (vault / "projects" / "p" / "memos").mkdir(parents=True)
    (vault / "projects" / "p" / "memos" / "m.md").write_text("---\ntitle: m\n---\nhello index\n")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(ir, "get_index_path", lambda memex=None, settings=None: state / "_index.sqlite")

    ir.rebuild_full(vault, with_embeddings=False, atomic=True)

    assert (state / "_index.sqlite").exists()
    for suffix in ("", ".tmp", ".bak"):
        assert not (vault / f"_index.sqlite{suffix}").exists()
    assert not (state / "_index.sqlite.tmp").exists()
    assert not (state / "_index.sqlite.bak").exists()
