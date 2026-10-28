"""Run a hook script end to end against a throwaway home (v0.22.0 wave 0).

Hooks in production run under ``uv run --script`` with only their PEP 723
dependencies, so ``memex.config`` (pydantic) is usually absent and paths come
from the raw ``~/.memex/config.json``. ``run_hook`` reproduces that: it points
HOME at a tmp dir holding a config for a tmp vault and state dir, strips
``MEMEX_*``/``CLAUDE_*`` from the environment, and runs the script with the test
interpreter by default — fast, but that interpreter has EVERY project dependency,
so a hook that imports something its PEP 723 block doesn't list still passes.
``run(..., real=True)`` runs the hook the way Claude Code does
(``uv run --script``, only the inline dependencies; uv's real cache is kept so a
throwaway HOME doesn't re-download everything). ``tests/test_hooks_run_for_real.py``
runs every hook that way; keep memex imports at hook module top level so it sees them.

    def test_something(hook_env):
        result = hook_env.run("session-start", {"session_id": SID, "source": "compact", "cwd": "/x"})
        assert "Memo needed" not in result.context
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"


@dataclass
class HookResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict | None:
        try:
            data = json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @property
    def context(self) -> str:
        """Text the model would receive: ``additionalContext`` or plain stdout."""
        data = self.json
        if data is None:
            return self.stdout
        hso = data.get("hookSpecificOutput") or {}
        return str(hso.get("additionalContext", ""))

    @property
    def system_message(self) -> str:
        """Text the user would see (``systemMessage``)."""
        data = self.json or {}
        return str(data.get("systemMessage", ""))


@dataclass
class HookEnv:
    home: Path
    vault: Path
    state_dir: Path
    extra_env: dict[str, str] = field(default_factory=dict)

    def write_config(self, **extra) -> None:
        cfg = {"memex_path": str(self.vault), "state_dir": str(self.state_dir), **extra}
        (self.home / ".memex").mkdir(parents=True, exist_ok=True)
        (self.home / ".memex" / "config.json").write_text(json.dumps(cfg))

    def write_memo(self, project: str, name: str, frontmatter: str, body: str = "# memo\n") -> Path:
        path = self.vault / "projects" / project / "memos" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter.strip()}\n---\n\n{body}")
        return path

    def run(
        self,
        hook: str,
        payload: dict,
        *,
        env: dict[str, str] | None = None,
        timeout: float = 30,
        real: bool = False,
    ) -> HookResult:
        script = HOOKS_DIR / f"{hook}.py"
        clean = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("MEMEX_", "CLAUDE_", "CLAUDECODE", "VIRTUAL_ENV", "UV_PROJECT"))
        }
        if real:
            uv = shutil.which("uv")
            if uv is None:
                pytest.skip("uv not on PATH: cannot run hooks the way Claude Code does")
            clean.setdefault("UV_CACHE_DIR", _real_uv_cache_dir())
            # --exact: uv keys a script env by path and keeps packages a header no longer
            # lists, so without it a REMOVED dependency still imports (found in wave 1).
            argv = [uv, "run", "--quiet", "--exact", "--script", str(script)]
        else:
            argv = [sys.executable, str(script)]
        clean.update({"HOME": str(self.home), **self.extra_env, **(env or {})})
        proc = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=clean,
            timeout=timeout,
        )
        return HookResult(proc.returncode, proc.stdout, proc.stderr)


def _real_uv_cache_dir() -> str:
    """The developer's uv cache (resolved with the REAL home, before HOME moves)."""
    env_dir = os.environ.get("UV_CACHE_DIR")
    if env_dir:
        return env_dir
    try:
        out = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return str(Path.home() / ".cache" / "uv")


@pytest.fixture
def hook_env(tmp_path: Path) -> HookEnv:
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    state = home / ".memex"
    for d in (home, vault / "projects", vault / "topics", state):
        d.mkdir(parents=True, exist_ok=True)
    env = HookEnv(home=home, vault=vault, state_dir=state)
    env.write_config()
    return env
