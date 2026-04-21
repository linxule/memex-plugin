from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memex.config import Settings, get_settings, reset_settings
from memex.paths import get_memex_path


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    reset_settings()

    settings = get_settings()

    assert settings.state_dir == "~/.memex"
    assert settings.memo_generation.min_messages == 5
    assert settings.embeddings.provider == "google"
    assert settings.search.default_mode == "hybrid"
    assert settings.auto_memory.enabled is True


def test_settings_loads_from_temp_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "memex_path": str(tmp_path / "vault"),
                "search": {
                    "vector_weight": 0.4,
                    "bm25_weight": 0.6,
                },
                "auto_memory": {
                    "enabled": False,
                },
            }
        )
    )
    reset_settings()

    settings = get_settings(config_path)

    assert settings.memex_path == str(tmp_path / "vault")
    assert settings.search.vector_weight == 0.4
    assert settings.search.bm25_weight == 0.6
    assert settings.auto_memory.enabled is False


def test_get_memex_path_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reset_settings()
    configured_vault = tmp_path / "configured-vault"
    configured_vault.mkdir()

    assert get_memex_path(Settings(memex_path=str(configured_vault))) == configured_vault

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    env_vault = tmp_path / "env-vault"
    env_vault.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env_vault))
    assert get_memex_path(Settings(memex_path=None)) == env_vault

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with pytest.raises(ValueError):
        get_memex_path(Settings(memex_path=None))
