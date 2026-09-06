"""Opt-in local Gemini credentials; never invoke a credential manager."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import sys
import tempfile


@dataclass(frozen=True)
class GeminiCredential:
    value: str = field(repr=False)
    source: str


def gemini_key_path() -> Path:
    """Return the installation's key path without creating directories."""
    from memex.config import get_settings

    return Path(get_settings().state_dir).expanduser().resolve() / "credentials" / "gemini-api-key"


def missing_gemini_key_help(api_key_env: str = "GEMINI_API_KEY") -> str:
    return (
        f"Gemini API key not found: set ${api_key_env}, or use 1Password explicitly:\n"
        '  op run --env-file ~/.secrets.op -- memex search "your query"\n'
        "Save a key for automatic loading on this installation: memex auth set-key\n"
        "Inspect local credential configuration: memex auth status"
    )


def _validate_key(value: str) -> str:
    value = value.strip()
    if value.startswith("op://"):
        raise ValueError(
            "The API key is an unresolved 1Password reference. Run the command with "
            "op run --env-file ~/.secrets.op -- <command>."
        )
    if not value or any(char.isspace() for char in value):
        raise ValueError("The API key must be a nonempty value without whitespace.")
    return value


def gemini_key_from_env(api_key_env: str = "GEMINI_API_KEY") -> GeminiCredential | None:
    # Preserve Memex's configured variable preference. GOOGLE_API_KEY is a
    # fallback only for the default name; custom names remain explicit.
    names = [api_key_env]
    if api_key_env == "GEMINI_API_KEY":
        names.append("GOOGLE_API_KEY")
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return GeminiCredential(_validate_key(value), f"environment variable {name}")
    return None


def resolve_gemini_key(api_key_env: str = "GEMINI_API_KEY") -> GeminiCredential | None:
    """Prefer the process environment, then the explicitly saved local key."""
    credential = gemini_key_from_env(api_key_env)
    if credential is not None:
        return credential
    path = gemini_key_path()
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            f"Cannot read the saved Gemini key at {path}. Use memex auth set-key "
            "to replace it, or memex auth clear-key to remove it."
        ) from exc
    try:
        value = _validate_key(value)
    except ValueError as exc:
        # Name the culprit: the same validator runs on env values, and a bare
        # "must be nonempty" leaves the user hunting the wrong source.
        raise ValueError(
            f"Saved key file {path} is invalid: {exc} Use memex auth set-key to "
            "replace it, or memex auth clear-key to remove it."
        ) from exc
    return GeminiCredential(value, f"saved key file {path}")


def save_gemini_key(value: str) -> Path:
    """Atomically save a key with owner-only permissions, outside vault content."""
    value = _validate_key(value)
    path = gemini_key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode applies only on creation; tighten a pre-existing directory
    # too so the key file's name and existence stay owner-only.
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        try:
            os.chmod(path.parent, 0o700)
        except OSError as exc:
            # Writable-but-not-owned directory: saving still works; the
            # key file itself is 0600, so only its existence is visible.
            print(f"Warning: could not tighten {path.parent} to 0700: {exc}", file=sys.stderr)
    fd, temporary = tempfile.mkstemp(prefix=".gemini-key-", dir=path.parent)
    try:
        # mkstemp creates mode 0600, including when replacing an older file.
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def clear_gemini_key() -> bool:
    path = gemini_key_path()
    if path.is_dir():
        raise ValueError(
            f"{path} is a directory, not a saved key file; remove it manually."
        )
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
