"""Regression tests for sanitize_project_name idempotency (the underscore bug).

Found 2026-08-05: sanitize_project_name('_uncategorized') stripped the leading
underscore and — because 'uncategorized' was not itself in RESERVED_NAMES —
returned 'uncategorized'. detect_project correctly resolved /tmp-style cwds to
'_uncategorized', but the write path (safe_project_path / get_unique_project_name)
re-sanitizes the already-detected name, so transcripts landed in
projects/uncategorized/ alongside the canonical projects/_uncategorized/.
47 files drifted this way during the 2026-07-28 cc-fleet fan-out runs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.scripts.utils import RESERVED_NAMES, sanitize_project_name, safe_project_path


def test_uncategorized_fallback_is_a_fixed_point():
    """sanitize(sanitize(x)) == sanitize(x) must hold for the fallback value."""
    assert sanitize_project_name("_uncategorized") == "_uncategorized"


def test_bare_uncategorized_is_reserved():
    """The underscore-stripped form must round-trip back to the canonical name."""
    assert sanitize_project_name("uncategorized") == "_uncategorized"


def test_sanitize_is_idempotent_on_all_reserved_inputs():
    """Every reserved name resolves to _uncategorized, which must survive re-sanitize."""
    for name in RESERVED_NAMES:
        once = sanitize_project_name(name)
        assert sanitize_project_name(once) == once, (
            f"sanitize not idempotent for reserved input {name!r}: "
            f"{once!r} -> {sanitize_project_name(once)!r}"
        )


def test_sanitize_is_idempotent_on_regular_names():
    for name in ("arena", "kimi-plugin-cc", "linxule_com", "_private_thing", "a_b-c"):
        once = sanitize_project_name(name)
        assert sanitize_project_name(once) == once


def test_sanitize_is_idempotent_when_truncation_lands_on_underscore():
    """The 50-char cap must not leave a trailing '_' for the next call to strip.

    Same failure as the fallback bug, different trigger: the write path
    re-sanitizes an already-detected name, so a long name truncated to
    'a...a_' would become two folders — 'a...a_' and 'a...a'.
    """
    for name in ("a" * 49 + "_" + "b" * 10,
                 "my_very_long_project_name_that_keeps_going_and_go_ing_here",
                 "_" * 3 + "x" * 47 + "_" + "y" * 5):
        once = sanitize_project_name(name)
        assert not once.endswith("_"), f"truncation left a trailing underscore: {once!r}"
        assert sanitize_project_name(once) == once, (
            f"sanitize not idempotent for {name!r}: "
            f"{once!r} -> {sanitize_project_name(once)!r}"
        )


def test_sanitize_still_caps_length_at_50():
    assert len(sanitize_project_name("z" * 200)) == 50


def test_safe_project_path_keeps_underscore_folder(tmp_path):
    """The write path must target projects/_uncategorized/, not projects/uncategorized/."""
    (tmp_path / "projects").mkdir()
    result = safe_project_path("_uncategorized", tmp_path)
    assert result.name == "_uncategorized"
