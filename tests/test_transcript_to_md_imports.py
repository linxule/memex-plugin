"""Tests for the transcript_to_md relocation + fallback-shadow fix (v0.16.3).

Until v0.16.3 `scripts/transcript_to_md.py` was the canonical module and its
import ladder was mis-indented::

    try:
        from .utils import (log_warning, log_error, log_info,
                            format_frontmatter, sanitize_project_name)
    except ImportError:
        try:
            from utils import (...)          # succeeds on the flat path
        except ImportError:
            def log_warning(msg): ...        # correctly nested
        def log_error(msg): ...              # <- OUTER except: always ran
        def log_info(msg): ...               # <- shadowed the real import
        def format_frontmatter(data): ...    # <- shadowed
        def sanitize_project_name(name): ... # <- shadowed

So on the flat-import path — the one `memex session import` used — Python
imported the real names and then immediately replaced four of them with the
"standalone mode" stubs. The stub `sanitize_project_name` has no
RESERVED_NAMES table and truncates *after* stripping, i.e. it is the exact
pre-v0.16.2 function, both of whose bugs were fixed in utils and neither of
which could fire only because the name happens to be unused in this module.
A future use would have silently resurrected them.

These tests pin the three import shapes real callers use. Each runs in a
subprocess so the shim's own ``sys.path`` insert is exercised for real and no
``sys.modules`` state leaks between them.
"""

import subprocess
import sys
from pathlib import Path

import pytest

VAULT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT / "scripts"

sys.path.insert(0, str(VAULT / "src"))


def _run_py(code: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"import shape failed:\n{proc.stderr}"
    return proc.stdout.strip()


# --- the canonical module ----------------------------------------------------

def test_canonical_module_reuses_the_real_utils_functions():
    """Identity, not behaviour — a re-added stub would be a different object.

    Behavioural assertions can be satisfied by a stub that happens to agree on
    the cases tested (the old `format_frontmatter` fallback was byte-identical
    in effect, which is precisely why the shadowing survived unnoticed). `is`
    cannot be.
    """
    from memex.scripts import transcript_to_md as t
    from memex.scripts import utils

    for name in ("log_warning", "log_error", "log_info",
                 "format_frontmatter", "sanitize_project_name"):
        assert getattr(t, name) is getattr(utils, name), f"{name} is shadowed"


# --- the shapes real callers use ---------------------------------------------

def test_flat_shim_import_yields_real_sanitize():
    """`sys.path.insert(scripts/)` + `import transcript_to_md`.

    The shape `batch_import_transcripts.py` and `stress_test_transcripts.py`
    use, and the shape `discover_sessions.import_sessions` used before the
    move. `_uncategorized` is the discriminator: the real function keeps it
    (RESERVED_NAMES round-trips it), the stub strips the underscore and
    returns a bare `uncategorized`. That is the pre-v0.16.2 behaviour whose
    live form minted a second `projects/uncategorized/` folder beside the
    canonical one; the stub was a dormant copy of it.
    """
    out = _run_py(f"""
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import transcript_to_md as t
print(t.sanitize_project_name('_uncategorized'))
print(t.sanitize_project_name('a' * 49 + '_b'))
print(hasattr(t, 'convert_transcript_file'), hasattr(t, 'parse_transcript_jsonl'))
""")
    kept, truncated, attrs = out.splitlines()
    assert kept == "_uncategorized"
    # 50-char cap must land before the strip, so re-sanitising is a fixed point.
    assert not truncated.endswith("_")
    assert truncated == "a" * 49
    assert attrs == "True True"


def test_package_shim_import_still_resolves():
    """`from scripts.transcript_to_md import convert_transcript_file`.

    The pre-v0.16.3 hook shape. `hooks/session-end.py` now imports the
    canonical module directly, but the shim must keep answering this — an
    unrolled plugin cache and any third-party caller still use it.
    """
    out = _run_py(f"""
import sys
sys.path.insert(0, {str(VAULT)!r})
sys.path.insert(0, {str(VAULT / "src")!r})
from scripts.transcript_to_md import convert_transcript_file
print(callable(convert_transcript_file))
""")
    assert out == "True"


def test_session_end_hook_import_shape_resolves():
    """The exact import `hooks/session-end.py` runs, from the vault root."""
    out = _run_py(f"""
import sys
sys.path.insert(0, {str(VAULT)!r})
sys.path.insert(0, {str(VAULT / "src")!r})
from memex.scripts.transcript_to_md import convert_transcript_file
print(callable(convert_transcript_file))
""")
    assert out == "True"


# --- the relocation itself ---------------------------------------------------

def test_shim_exists_and_canonical_lives_in_src():
    """Repo convention: canonical module in src/, 3-line shim in scripts/."""
    canonical = VAULT / "src" / "memex" / "scripts" / "transcript_to_md.py"
    shim = SCRIPTS / "transcript_to_md.py"
    assert canonical.exists(), "canonical module missing from src/memex/scripts/"
    assert shim.exists(), "backward-compat shim missing from scripts/"
    body = shim.read_text(encoding="utf-8")
    assert "from memex.scripts.transcript_to_md import" in body
    # A shim that grows a function body is a fork waiting to happen.
    assert "def " not in body, "shim should delegate, not define"


def test_no_module_defines_its_own_sanitize_fallback():
    """No 'standalone mode' stub may reappear in either file."""
    for path in (VAULT / "src" / "memex" / "scripts" / "transcript_to_md.py",
                 SCRIPTS / "transcript_to_md.py"):
        body = path.read_text(encoding="utf-8")
        assert "def sanitize_project_name" not in body, f"{path.name} redefines sanitize"
        assert "def format_frontmatter" not in body, f"{path.name} redefines frontmatter"
