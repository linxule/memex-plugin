"""Local credential setup commands; no authentication or API calls on their own."""

from __future__ import annotations

import sys

import typer


app = typer.Typer(help="Set up Gemini credentials for this installation.", no_args_is_help=True)


@app.command("set-key")
def set_key(
    from_env: bool = typer.Option(False, "--from-env", help="Save the current environment key instead of prompting."),
) -> None:
    """Save a Gemini key locally for automatic loading on future runs."""
    from memex.config import get_settings
    from memex.credentials import gemini_key_from_env, gemini_key_path, save_gemini_key

    typer.echo(f"Stores an unencrypted key in {gemini_key_path()} (owner-only permissions).")
    try:
        if from_env:
            credential = gemini_key_from_env(get_settings().embeddings.api_key_env)
            if credential is None:
                raise ValueError("No Gemini key found in the environment; nothing was saved.")
            value = credential.value
        elif sys.stdin.isatty():
            value = typer.prompt("Gemini API key", hide_input=True)
        else:
            # Piped input: read one line directly. getpass's non-TTY fallback
            # would print a "may be echoed" warning and a traceback fragment.
            value = sys.stdin.readline()
            if not value:
                raise ValueError("No key provided on stdin; nothing was saved.")
        save_gemini_key(value)
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Saved. Memex will load this key automatically when no environment key is set.")
    typer.echo("The key has not been sent to Gemini or validated with the API.")


@app.command("status")
def status() -> None:
    """Show the credential source without printing keys or contacting Gemini."""
    from memex.config import get_settings
    from memex.credentials import missing_gemini_key_help, resolve_gemini_key

    settings = get_settings()
    typer.echo(f"Embedding provider: {settings.embeddings.provider}")
    typer.echo(f"Embeddings enabled: {settings.embeddings.enabled}")
    try:
        credential = resolve_gemini_key(settings.embeddings.api_key_env)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if credential is None:
        typer.echo(missing_gemini_key_help(settings.embeddings.api_key_env))
        raise typer.Exit(1)
    typer.echo(f"Gemini credential source: {credential.source}")
    typer.echo("Local configuration only; API validity has not been checked.")


@app.command("clear-key")
def clear_key() -> None:
    """Remove the locally saved key. Environment variables still take precedence."""
    from memex.credentials import clear_gemini_key

    try:
        removed = clear_gemini_key()
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Removed the saved Gemini key." if removed else "No saved Gemini key to remove.")
