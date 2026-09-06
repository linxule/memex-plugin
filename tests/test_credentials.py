"""Credential setup stays local, explicit, private, and independent of APIs."""

from __future__ import annotations

import os
import stat

import pytest
from typer.testing import CliRunner

from memex import credentials
from memex.cli import app
from memex.config import reset_settings
from memex.scripts import embeddings


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MEMEX_EMBEDDINGS__API_KEY_ENV", "GEMINI_API_KEY")
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "CUSTOM_GEMINI_KEY"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield
    reset_settings()


def test_missing_key_offers_explicit_onepassword_and_local_setup():
    with pytest.raises(ValueError) as exc:
        embeddings.GeminiProvider({})
    assert "op run --env-file ~/.secrets.op -- memex search" in str(exc.value)
    assert "memex auth set-key" in str(exc.value)
    assert not credentials.gemini_key_path().parent.exists()


def test_saved_key_is_private_and_loaded_without_environment():
    path = credentials.save_gemini_key("example-local-key")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    credential = credentials.resolve_gemini_key()
    assert credential.value == "example-local-key"
    assert "example-local-key" not in repr(credential)
    assert "saved key file" in credential.source
    assert embeddings.GeminiProvider({})._api_key == "example-local-key"
    assert "GEMINI_API_KEY" not in os.environ


def test_environment_precedes_saved_key_and_google_fallback(monkeypatch):
    credentials.save_gemini_key("saved-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    assert credentials.resolve_gemini_key().value == "google-key"
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    assert credentials.resolve_gemini_key().value == "gemini-key"
    monkeypatch.setenv("CUSTOM_GEMINI_KEY", "custom-key")
    assert credentials.resolve_gemini_key("CUSTOM_GEMINI_KEY").value == "custom-key"
    monkeypatch.delenv("CUSTOM_GEMINI_KEY")
    assert credentials.resolve_gemini_key("CUSTOM_GEMINI_KEY").value == "saved-key"


@pytest.mark.parametrize("invalid", ["", "  ", "multiple\nlines", "op://Private/item/field"])
def test_invalid_value_does_not_replace_saved_key(invalid):
    path = credentials.save_gemini_key("original-key")
    with pytest.raises(ValueError):
        credentials.save_gemini_key(invalid)
    assert path.read_text().strip() == "original-key"


def test_failed_replacement_retains_old_key_and_removes_temporary_file(monkeypatch):
    path = credentials.save_gemini_key("original-key")

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(credentials.os, "replace", fail)
    with pytest.raises(OSError):
        credentials.save_gemini_key("replacement-key")
    assert path.read_text().strip() == "original-key"
    assert list(path.parent.iterdir()) == [path]


def test_unresolved_onepassword_reference_is_not_sent_or_printed(monkeypatch):
    reference = "op://Private/Example Gemini Key/secret-field"
    monkeypatch.setenv("GEMINI_API_KEY", reference)
    with pytest.raises(ValueError) as exc:
        credentials.resolve_gemini_key()
    assert "op run" in str(exc.value)
    assert reference not in str(exc.value)


def test_cli_hidden_prompt_save_status_and_clear():
    runner = CliRunner()
    result = runner.invoke(app, ["auth", "set-key"], input="example-hidden-key\n")
    assert result.exit_code == 0, result.output
    assert "example-hidden-key" not in result.output
    assert "unencrypted" in result.output
    assert "automatically" in result.output
    status_result = runner.invoke(app, ["auth", "status"])
    assert status_result.exit_code == 0, status_result.output
    assert "saved key file" in status_result.output
    assert "example-hidden-key" not in status_result.output
    assert "API validity has not been checked" in status_result.output
    assert runner.invoke(app, ["auth", "clear-key"]).exit_code == 0
    assert credentials.resolve_gemini_key() is None
    assert runner.invoke(app, ["auth", "clear-key"]).exit_code == 0


def test_cli_from_env_is_explicit_and_never_echoes_key(monkeypatch):
    runner = CliRunner()
    missing = runner.invoke(app, ["auth", "set-key", "--from-env"])
    assert missing.exit_code == 1
    assert not credentials.gemini_key_path().exists()
    monkeypatch.setenv("GEMINI_API_KEY", "example-env-key")
    result = runner.invoke(app, ["auth", "set-key", "--from-env"])
    assert result.exit_code == 0, result.output
    assert "example-env-key" not in result.output
    monkeypatch.delenv("GEMINI_API_KEY")
    assert credentials.resolve_gemini_key().value == "example-env-key"


def test_missing_status_does_not_create_state_directory():
    result = CliRunner().invoke(app, ["auth", "status"])
    assert result.exit_code == 1
    assert "op run" in result.output
    assert not credentials.gemini_key_path().parent.exists()


def test_disabled_embeddings_do_not_resolve_credentials_or_create_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled embeddings attempted credential/provider access")

    monkeypatch.setattr(embeddings, "GeminiProvider", forbidden)
    monkeypatch.setattr(embeddings, "LMStudioProvider", forbidden)
    monkeypatch.setattr(credentials, "resolve_gemini_key", forbidden)
    pipeline = embeddings.EmbeddingPipeline({"enabled": False, "provider": "google"})
    assert pipeline.enabled is False
    assert pipeline.provider == "google"


def test_bad_saved_file_has_actionable_error_without_content(tmp_path):
    path = credentials.gemini_key_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"sensitive-prefix-\xff")
    result = CliRunner().invoke(app, ["auth", "status"])
    assert result.exit_code == 1
    assert "memex auth clear-key" in result.output
    assert "sensitive-prefix" not in result.output
