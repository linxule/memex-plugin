"""Tests for the crystallization-check filesystem fallback.

When Obsidian is not running, ``memex check`` falls back to
``scan_unresolved_via_markdown`` instead of hard-erroring (the pre-fix
behaviour exited 1 on a 15s Obsidian timeout — unusable headless/cron).
These tests pin the fallback's resolution rules: a ``[[link]]`` resolves
against any markdown filename stem OR any frontmatter alias; everything else
is reported unresolved with its source files.
"""

from __future__ import annotations

from pathlib import Path

from memex.scripts.crystallization_check import (
    _read_frontmatter_aliases,
    scan_unresolved_via_markdown,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_resolves_by_filename_stem_and_alias_reports_ghosts(tmp_path: Path) -> None:
    # Existing topic with an inline alias list
    _write(
        tmp_path / "topics" / "existing-topic.md",
        "---\naliases: [the-alias, second-alias]\n---\n\n# Existing\n",
    )
    # A project overview that references one resolvable filename, one alias,
    # and two genuine ghost nodes (one appearing twice across files).
    _write(
        tmp_path / "projects" / "foo" / "_project.md",
        "Links [[existing-topic]], [[the-alias|display]], "
        "[[ghost-concept]] and [[ghost-concept#heading]] and [[another-ghost]].\n",
    )
    _write(
        tmp_path / "projects" / "bar" / "_project.md",
        "Also references [[ghost-concept]] here.\n",
    )

    unresolved, alias_map = scan_unresolved_via_markdown(tmp_path)

    # Resolvable links must NOT appear
    assert "existing-topic" not in unresolved
    assert "the-alias" not in unresolved
    # Ghost nodes appear; heading/display suffixes are stripped before matching
    assert "ghost-concept" in unresolved
    assert "another-ghost" in unresolved
    # Cross-file aggregation: ghost-concept seen in both project files (deduped)
    assert sorted(unresolved["ghost-concept"]) == [
        "projects/bar/_project.md",
        "projects/foo/_project.md",
    ]
    # Alias map is built from frontmatter, lowercased
    assert alias_map.get("the-alias", "").endswith("existing-topic.md")
    assert "second-alias" in alias_map


def test_skips_plumbing_dirs(tmp_path: Path) -> None:
    _write(tmp_path / ".obsidian" / "x.md", "[[should-be-ignored]]\n")
    _write(tmp_path / ".git" / "y.md", "[[also-ignored]]\n")
    _write(tmp_path / "topics" / "real.md", "[[a-real-ghost]]\n")

    unresolved, _ = scan_unresolved_via_markdown(tmp_path)

    assert "should-be-ignored" not in unresolved
    assert "also-ignored" not in unresolved
    assert "a-real-ghost" in unresolved


def test_frontmatter_alias_block_form(tmp_path: Path) -> None:
    f = tmp_path / "t.md"
    _write(
        f,
        "---\naliases:\n  - alpha\n  - beta\ntags: [x]\n---\n\nbody\n",
    )
    aliases = _read_frontmatter_aliases(f)
    assert aliases == ["alpha", "beta"]


def test_frontmatter_no_aliases(tmp_path: Path) -> None:
    f = tmp_path / "t.md"
    _write(f, "---\ntitle: Foo\n---\n\nbody [[x]]\n")
    assert _read_frontmatter_aliases(f) == []


def test_frontmatter_aliases_as_last_block_terminated_by_fence(tmp_path: Path) -> None:
    # Bare `aliases:` list with no following key — closing `---` must terminate it
    f = tmp_path / "t.md"
    _write(f, "---\naliases:\n  - alpha\n  - beta\n---\n\nbody\n")
    assert _read_frontmatter_aliases(f) == ["alpha", "beta"]


def test_frontmatter_quoted_aliases(tmp_path: Path) -> None:
    inline = tmp_path / "i.md"
    _write(inline, '---\naliases: ["foo", \'bar\']\n---\n')
    assert _read_frontmatter_aliases(inline) == ["foo", "bar"]
    block = tmp_path / "b.md"
    _write(block, "---\naliases:\n  - \"alpha\"\n  - 'beta'\n---\n")
    assert _read_frontmatter_aliases(block) == ["alpha", "beta"]


def test_malformed_and_empty_frontmatter_do_not_crash(tmp_path: Path) -> None:
    # Unterminated frontmatter (opening fence, no closing fence)
    unterminated = tmp_path / "u.md"
    _write(unterminated, "---\naliases:\n  - x\nbody with no closing fence\n")
    assert _read_frontmatter_aliases(unterminated) == ["x"]
    # File that is only an opening fence
    bare = tmp_path / "bare.md"
    _write(bare, "---\n")
    assert _read_frontmatter_aliases(bare) == []
    # Truly empty file
    empty = tmp_path / "e.md"
    _write(empty, "")
    assert _read_frontmatter_aliases(empty) == []
    # No frontmatter at all
    plain = tmp_path / "p.md"
    _write(plain, "# Heading\n[[ghost]]\n")
    assert _read_frontmatter_aliases(plain) == []


def test_non_utf8_file_is_tolerated(tmp_path: Path) -> None:
    # Latin-1 / invalid-UTF8 bytes must not crash the scan (errors="ignore")
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"---\naliases: [caf\xe9]\n---\n[[real-ghost]]\n")
    unresolved, _ = scan_unresolved_via_markdown(tmp_path)
    assert "real-ghost" in unresolved
