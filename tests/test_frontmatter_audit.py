"""Tests for the frontmatter lint (memex check --validate) — the YAML-damage
class detector built after the v0.15.x migration merged status:+title: lines."""

from __future__ import annotations

from pathlib import Path

from memex.scripts.frontmatter_audit import (
    _scan_file,
    audit_frontmatter,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _kinds(path: Path) -> list[str]:
    return [i["kind"] for i in _scan_file(path)]


# --- the core damage class -------------------------------------------------

def test_merged_line_glued(tmp_path: Path) -> None:
    # the exact production bug: status: archived + title: glued
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\nstatus: archivedtitle: "DO billing"\nproject: x\n---\nbody\n')
    assert "merged-line" in _kinds(p)


def test_merged_line_space_separated(tmp_path: Path) -> None:
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\nstatus: archived title: "X"\nproject: x\n---\nbody\n')
    assert "merged-line" in _kinds(p)


def test_colon_in_quoted_title_is_clean(tmp_path: Path) -> None:
    # a real colon inside a quoted value must NOT be flagged as a second key
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle: "DO billing: a saga"\nproject: x\ndate: 2026-01-01\n---\nbody\n')
    assert _kinds(p) == []


def test_no_closing_delimiter(tmp_path: Path) -> None:
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle: "Z"\nproject: x\nbody with no closing delimiter\n')
    assert "no-closing-delimiter" in _kinds(p)


# --- identity field --------------------------------------------------------

def test_missing_title_on_memo(tmp_path: Path) -> None:
    p = _write(tmp_path / "projects" / "x" / "memos" / "m.md",
               '---\ntype: memo\nproject: x\ndate: 2026-01-01\n---\nbody\n')
    assert "missing-title" in _kinds(p)


def test_project_uses_name_not_title(tmp_path: Path) -> None:
    # a _project.md with `name` (not `title`) is valid — must not be flagged
    p = _write(tmp_path / "projects" / "x" / "_project.md",
               '---\ntype: project\nname: x\ncreated: 2026-01-01\n---\nbody\n')
    assert "missing-title" not in _kinds(p)


def test_topic_with_type_project_accepts_title(tmp_path: Path) -> None:
    # the *-project.md topics carry `title`, not `name`; must be accepted
    p = _write(tmp_path / "topics" / "alcor-project.md",
               '---\ntype: project\ntitle: "Alcor"\n---\nbody\n')
    assert "missing-title" not in _kinds(p)


def test_no_frontmatter(tmp_path: Path) -> None:
    p = _write(tmp_path / "projects" / "x" / "memos" / "m.md", "# Just a heading\n\nbody\n")
    assert _kinds(p) == ["no-frontmatter"]


# --- status vocabulary is intentionally NOT enforced -----------------------

def test_rich_status_vocabulary_is_not_flagged(tmp_path: Path) -> None:
    # the vault uses evergreen/stub/developing/superseded/... — never flag status
    for status in ("evergreen", "stub", "developing", "superseded", "published"):
        p = _write(tmp_path / "topics" / f"{status}.md",
                   f'---\ntype: concept\ntitle: "T"\nstatus: {status}\n---\nbody\n')
        assert _kinds(p) == [], f"status={status} should be clean"


# --- whole-vault audit + exit semantics ------------------------------------

def test_audit_aggregates_and_clean_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path / "projects" / "x" / "memos" / "ok.md",
           '---\ntype: memo\ntitle: "OK"\nproject: x\ndate: 2026-01-01\n---\nbody\n')
    _write(tmp_path / "topics" / "good.md",
           '---\ntype: concept\ntitle: "Good"\n---\nbody\n')
    report = audit_frontmatter(tmp_path)
    assert report["scanned"] == 2
    assert report["issues"] == []


def test_audit_finds_the_damage(tmp_path: Path) -> None:
    _write(tmp_path / "projects" / "x" / "memos" / "bad.md",
           '---\ntype: memo\nstatus: archivedtitle: "B"\nproject: x\n---\nbody\n')
    report = audit_frontmatter(tmp_path)
    kinds = {i["kind"] for i in report["issues"]}
    assert "merged-line" in kinds


# --- reviewer-found false-positive vectors (must stay clean) ----------------

def test_unquoted_colon_in_title_is_clean(tmp_path: Path) -> None:
    # kimi: 'title: My manual: a guide' — 'manual' is a known key but it's mid-value
    # prose in a free-text field, NOT a merge.
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle: My manual: a guide\nproject: x\ndate: 2026-01-01\n---\nb\n')
    assert "merged-line" not in _kinds(p)


def test_known_key_substring_in_value_is_clean(tmp_path: Path) -> None:
    # kimi: 'subtitle' ends in 'title' but is not a second key.
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle: a subtitle here\nproject: x\ndate: 2026-01-01\n---\nb\n')
    assert "merged-line" not in _kinds(p)


def test_url_in_source_is_clean(tmp_path: Path) -> None:
    # codex: 'source: http://myproject:8080' — 'project:' is a mid-word substring
    # after a colon in a URL, not a merge.
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle: "T"\nsource: http://myproject:8080\n---\nb\n')
    assert "merged-line" not in _kinds(p)


# --- reviewer-found merge cases that MUST be caught ------------------------

def test_value_starts_with_known_key_is_merge(tmp_path: Path) -> None:
    # 'title: status: archived' — a real merge (key at value start)
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle: status: archived\nproject: x\n---\nb\n')
    assert "merged-line" in _kinds(p)


def test_empty_first_value_glue_is_merge(tmp_path: Path) -> None:
    # codex: 'title:name: X' (empty first value) must still be caught
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\ntitle:name: X\nproject: x\n---\nb\n')
    assert "merged-line" in _kinds(p)


def test_glue_on_scalar_key_is_merge(tmp_path: Path) -> None:
    # enum/scalar key value glued to a second key
    p = _write(tmp_path / "m.md",
               '---\ntype: memo\nproject: myprojecttitle: X\n---\nb\n')
    assert "merged-line" in _kinds(p)


# --- reviewer-found false-negative / FP on long frontmatter ----------------

def test_long_frontmatter_with_close_is_not_unclosed(tmp_path: Path) -> None:
    # kimi/in-house: a valid file whose closing '---' is far down (long aliases)
    # must NOT be flagged no-closing-delimiter.
    aliases = "\n".join(f"  - alias-{i}" for i in range(200))
    p = _write(tmp_path / "topics" / "big.md",
               f'---\ntype: concept\ntitle: "Big"\naliases:\n{aliases}\n---\nbody\n')
    assert _kinds(p) == []


def test_empty_frontmatter_memo_flags_missing_title(tmp_path: Path) -> None:
    # codex: '---\n---' memo (no type) must still be caught (path-based identity)
    p = _write(tmp_path / "projects" / "x" / "memos" / "m.md", "---\n---\nbody\n")
    assert "missing-title" in _kinds(p)


def test_date_with_time_is_clean(tmp_path: Path) -> None:
    # ISO datetime has colons but no known-key follows them
    p = _write(tmp_path / "projects" / "x" / "memos" / "m.md",
               '---\ntype: memo\ntitle: "T"\nproject: x\ndate: 2026-01-29T22:07:26\n---\nb\n')
    assert "merged-line" not in _kinds(p)
