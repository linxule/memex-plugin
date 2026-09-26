# Developing Memex

The application lives in `src/memex/`; top-level `scripts/` are compatibility
entrypoints. `hooks/` contains Claude Code lifecycle integration. This public
repository distributes code, not a user's vault. Keep private `projects/`,
`topics/`, transcripts, and external index/state directories out of code changes
and published packages.

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

The durable fix is a venv outside iCloud with `.venv` as a symlink to it. Make
the link **relative** (`ln -s ../../../.venvs/memex .venv` for a checkout at
`~/Documents/Apps/memex`): iCloud syncs the link to your other Macs, where an
absolute `/Users/<you>/...` target may not exist, and `uv run` then fails with
`File exists (os error 17)`. When the link's target is missing, `bin/memex`
points uv at it and builds that machine's own venv on first run. Never let two
Macs share one synced venv: iCloud leaves conflict copies (`RECORD 2`,
`METADATA 3`, ...) that break later `uv sync` uninstalls. `.gitignore` needs
`.venv` without a trailing slash (`.venv/` ignores directories, not the link).


## Publishing to PyPI

The public distribution is `memex-plugin`, while its executable and import
package stay `memex`. Never publish to the unrelated `memex` project. Version
metadata lives in `pyproject.toml`; update the two Claude JSON manifests and run
`uv run python scripts/portable_plugin.py` to update Codex and Kimi manifests.

```sh
uv lock
uv sync --locked --all-extras
uv run --frozen pytest -q
uv build
uv run --frozen python scripts/check_distribution.py
```

Start with an empty `dist/` directory. The distribution check requires exactly
one wheel and one source archive. It verifies their contents, installs the
wheel into a temporary environment, checks version and CLI discovery from
outside the checkout, rebuilds/searches a temporary vault with embeddings
disabled, and rebuilds a wheel from the source archive. No live vault, saved
credentials, or model calls are involved.

Trusted Publishing is configured, and [0.20.1](https://pypi.org/project/memex-plugin/0.20.1/)
was published through it on September 26, 2026. The published wheel passed a fresh
installation and offline vault search; the published source archive rebuilt
successfully. The active publisher is managed in the project's
[publishing settings](https://pypi.org/manage/project/memex-plugin/settings/publishing/):

- PyPI project: `memex-plugin`
- GitHub owner: `linxule`
- Repository: `memex-plugin`
- Workflow: `publish.yml`
- Environment: `pypi`

Pending-publisher setup was needed only before this project's first publication.
Existing releases use the active project publisher above.

Push the reviewed release commit, wait for CI, then tag `vX.Y.Z` and publish the
GitHub release. `.github/workflows/publish.yml` builds and tests that exact tag,
checks tag/version agreement, and publishes the validated archives through
PyPI Trusted Publishing. GitHub stores no PyPI API token. The workflow can also
be dispatched manually with an existing release tag after checking PyPI's
current files and any failed run. Do not replace an existing release's artifacts
with different bytes.

Verify `https://pypi.org/pypi/memex-plugin/X.Y.Z/json` and a fresh
`uv tool install memex-plugin==X.Y.Z` in isolated tool/home directories before
announcing the release. Keep users' existing tool installations untouched.
