# Developing Memex

The application lives in `src/memex/`; top-level `scripts/` are compatibility
entrypoints. `hooks/` contains Claude Code lifecycle integration. This checkout
also holds a live vault: keep code changes separate from `projects/`, `topics/`,
and the user's external index and state directories.

## Setup and checks

Use Python 3.11 or newer through uv. The tracked lockfile records the development
environment; the default dev group installs both pytest and Ruff.

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src hooks tests
uv build --wheel --out-dir /tmp/memex-dist
```

Run focused regression tests while changing code, then the complete suite once
the changes are integrated. The repository has existing Ruff findings; keep
new code clean and distinguish those findings from regressions. Test database
and hook mutations against temporary vaults and state directories. Use fake
embedding providers instead of credentials or paid API calls.

For installation credential options, see [Gemini setup](docs/gemini-credentials.md).
Credential tests use temporary state directories and fake keys; do not save an
operator's key or invoke 1Password to run the test suite.

## Dependencies and distribution

Use `uv lock --upgrade-package <name>` for a deliberate dependency upgrade and
review its lockfile diff. `pyproject.toml` declares supported minimum versions;
these must cover the APIs used by the code. In particular,
`JsonConfigSettingsSource(deep_merge=True)` requires pydantic-settings 2.13.0
([release notes](https://github.com/pydantic/pydantic-settings/releases/tag/v2.13.0)).

The optional `dev` extra remains available for pip users. Keep its pytest/Ruff
requirements aligned with the uv dev group.

Version lookup reads `pyproject.toml` in source checkouts and plugin caches so
edits are immediately visible. Installed wheels use their distribution metadata.
When changing packaging, verify the built wheel from outside the checkout with
`PYTHONPATH` unset; source-tree imports alone cannot catch installation failures.

## Known host issue: editable installs inside iCloud Drive

If the checkout lives under `~/Documents` (iCloud Drive), macOS marks `.venv`
and everything inside it with the `hidden` flag, and Python 3.13's `site.py`
skips hidden `.pth` files. `uv sync` succeeds, but `.venv/bin/memex` and
`python -m memex.cli` fail with `ModuleNotFoundError: memex`. `chflags -R
nohidden .venv` helps only until the next sync. Use `uv run memex ...` or the
`bin/memex` wrapper (explicit `PYTHONPATH=src`), or point
`UV_PROJECT_ENVIRONMENT` at a directory outside iCloud.

