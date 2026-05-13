"""Tests for memex.scripts.topic_resolve — redirect_to chain resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from memex.scripts.topic_resolve import resolve


def _write_topic(
    vault: Path,
    slug: str,
    *,
    redirect_to: str | None = None,
    archived: bool = False,
    body: str = "",
) -> Path:
    """Create a topic file with optional redirect_to / archived state."""
    fm_lines = ["---", "type: concept", f"title: {slug}"]
    if archived:
        fm_lines.append("status: archived")
    if redirect_to is not None:
        fm_lines.append(f"redirect_to: {redirect_to}")
    fm_lines.append("---")
    path = vault / "topics" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")
    return path


def _write_project(vault: Path, project: str, body: str = "") -> Path:
    """Create a project file at projects/<project>/_project.md."""
    path = vault / "projects" / project / "_project.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: project\nname: {project}\n---\n\n{body}",
        encoding="utf-8",
    )
    return path


def test_plain_slug_no_redirect(tmp_path: Path):
    _write_topic(tmp_path, "foo")
    assert resolve(tmp_path, "foo") == "foo"


def test_one_hop_slug_to_slug(tmp_path: Path):
    _write_topic(tmp_path, "old-name", redirect_to="new-name")
    _write_topic(tmp_path, "new-name")
    assert resolve(tmp_path, "old-name") == "new-name"


def test_cross_namespace_slug_to_project_path(tmp_path: Path):
    _write_topic(
        tmp_path, "bloom", redirect_to="projects/clawd-world/_project.md"
    )
    _write_project(tmp_path, "clawd-world")
    assert resolve(tmp_path, "bloom") == "projects/clawd-world/_project"


def test_two_hop_chain(tmp_path: Path):
    _write_topic(tmp_path, "a", redirect_to="b")
    _write_topic(tmp_path, "b", redirect_to="c")
    _write_topic(tmp_path, "c")
    assert resolve(tmp_path, "a") == "c"


def test_five_hop_chain_at_limit(tmp_path: Path):
    # a -> b -> c -> d -> e -> f (5 hops, terminal)
    chain = ["a", "b", "c", "d", "e", "f"]
    for i, slug in enumerate(chain[:-1]):
        _write_topic(tmp_path, slug, redirect_to=chain[i + 1])
    _write_topic(tmp_path, chain[-1])
    assert resolve(tmp_path, "a") == "f"


def test_six_hop_chain_exceeds_limit(tmp_path: Path, capsys):
    # a -> b -> c -> d -> e -> f -> g (6 hops, exceeds max=5)
    chain = ["a", "b", "c", "d", "e", "f", "g"]
    for i, slug in enumerate(chain[:-1]):
        _write_topic(tmp_path, slug, redirect_to=chain[i + 1])
    _write_topic(tmp_path, chain[-1])
    assert resolve(tmp_path, "a") is None
    err = capsys.readouterr().err
    assert "exceeded" in err and "5 hops" in err


def test_cycle_detection_two_node(tmp_path: Path, capsys):
    _write_topic(tmp_path, "a", redirect_to="b")
    _write_topic(tmp_path, "b", redirect_to="a")
    assert resolve(tmp_path, "a") is None
    err = capsys.readouterr().err
    assert "cycle" in err.lower()
    assert "->" in err  # chain-arrow format


def test_self_cycle(tmp_path: Path, capsys):
    _write_topic(tmp_path, "a", redirect_to="a")
    assert resolve(tmp_path, "a") is None
    err = capsys.readouterr().err
    assert "cycle" in err.lower()
    assert "->" in err  # chain-arrow format


def test_archived_without_redirect_warns(tmp_path: Path, capsys):
    _write_topic(tmp_path, "dead", archived=True)
    assert resolve(tmp_path, "dead") is None
    err = capsys.readouterr().err
    assert "archived" in err and "skipping signal" in err


def test_double_quoted_target(tmp_path: Path):
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '---\ntype: concept\nredirect_to: "embedding-models"\n---\n',
        encoding="utf-8",
    )
    _write_topic(tmp_path, "embedding-models")
    assert resolve(tmp_path, "src") == "embedding-models"


def test_single_quoted_target(tmp_path: Path):
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\nredirect_to: 'embedding-models'\n---\n",
        encoding="utf-8",
    )
    _write_topic(tmp_path, "embedding-models")
    assert resolve(tmp_path, "src") == "embedding-models"


def test_target_with_inline_comment(tmp_path: Path):
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\nredirect_to: foo  # absorbed into bar\n---\n",
        encoding="utf-8",
    )
    _write_topic(tmp_path, "foo")
    assert resolve(tmp_path, "src") == "foo"


def test_target_with_trailing_whitespace(tmp_path: Path):
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\nredirect_to: foo   \n---\n",
        encoding="utf-8",
    )
    _write_topic(tmp_path, "foo")
    assert resolve(tmp_path, "src") == "foo"


def test_missing_first_hop_silent(tmp_path: Path, capsys):
    # No file at all: caller passed a slug for a non-existent topic.
    assert resolve(tmp_path, "does-not-exist") is None
    err = capsys.readouterr().err
    assert err == ""  # no warning for first-hop misses


def test_missing_mid_chain_warns(tmp_path: Path, capsys):
    _write_topic(tmp_path, "a", redirect_to="b-missing")
    # b-missing has no file
    assert resolve(tmp_path, "a") is None
    err = capsys.readouterr().err
    assert "missing" in err.lower()


def test_empty_redirect_value_falls_through(tmp_path: Path):
    # redirect_to: (empty) is treated as no-redirect; falls through to
    # the terminal/archived check. Without status:archived, it should
    # resolve to the file itself.
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\nredirect_to: \n---\n",
        encoding="utf-8",
    )
    assert resolve(tmp_path, "src") == "src"


def test_archived_with_redirect_follows_redirect(tmp_path: Path):
    # Archived stubs that DO have a redirect_to should follow it,
    # not warn about being archived.
    _write_topic(
        tmp_path, "old", archived=True, redirect_to="new"
    )
    _write_topic(tmp_path, "new")
    assert resolve(tmp_path, "old") == "new"


def test_resolve_quoted_target_with_hash(tmp_path: Path):
    # redirect_to: "foo#bar" should resolve to slug "foo#bar", not "foo
    # (pre-fix bug: regex ate `#bar"` first, leaving `"foo` as the slug).
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '---\ntype: concept\nredirect_to: "foo#bar"\n---\n',
        encoding="utf-8",
    )
    # Create the actual destination file so the resolver finds a terminal.
    dest = tmp_path / "topics" / "foo#bar.md"
    dest.write_text("---\ntype: concept\n---\n", encoding="utf-8")
    assert resolve(tmp_path, "src") == "foo#bar"


def test_resolve_redirect_target_with_hash_no_quotes(tmp_path: Path):
    # redirect_to: foo#anchor (no whitespace before #) should NOT be
    # truncated — only `\s+#...` is treated as an inline comment.
    path = tmp_path / "topics" / "src.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\nredirect_to: foo#anchor\n---\n",
        encoding="utf-8",
    )
    dest = tmp_path / "topics" / "foo#anchor.md"
    dest.write_text("---\ntype: concept\n---\n", encoding="utf-8")
    assert resolve(tmp_path, "src") == "foo#anchor"


def test_resolve_empty_frontmatter_falls_through_to_terminal(tmp_path: Path):
    # File exists but has no `---` frontmatter block. _read_frontmatter
    # returns "" (not None), so the resolver treats it as a terminal
    # destination and returns the slug.
    path = tmp_path / "topics" / "plain.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("just body text, no frontmatter\n", encoding="utf-8")
    assert resolve(tmp_path, "plain") == "plain"


def test_resolve_blocks_path_traversal(tmp_path: Path, capsys):
    # redirect_to: ../../../etc/passwd should be blocked and warn.
    _write_topic(tmp_path, "evil", redirect_to="../../../etc/passwd")
    assert resolve(tmp_path, "evil") is None
    err = capsys.readouterr().err
    assert "escapes vault" in err or "unresolvable" in err


def test_resolve_blocks_path_traversal_on_first_hop(
    tmp_path: Path, capsys
):
    # Calling resolve(vault, "../../../etc/passwd") directly should
    # return None and warn — the first hop's path escapes the vault.
    assert resolve(tmp_path, "../../../etc/passwd") is None
    err = capsys.readouterr().err
    assert "escapes vault" in err or "unresolvable" in err


def test_resolve_handles_binary_target_gracefully(tmp_path: Path):
    # If a redirect target exists but is binary (non-UTF-8), the resolver
    # should not crash with UnicodeDecodeError — treat as missing.
    _write_topic(tmp_path, "src", redirect_to="binblob")
    binpath = tmp_path / "topics" / "binblob.md"
    binpath.write_bytes(b"\xff\xfe\x00\x01invalid-utf8\xc3\x28")
    # binary unreadable → _read_frontmatter returns None → mid-chain miss
    assert resolve(tmp_path, "src") is None


def test_resolve_appends_md_to_path_target(tmp_path: Path):
    # redirect_to: projects/foo/_project (no .md) should resolve same as
    # redirect_to: projects/foo/_project.md
    _write_topic(
        tmp_path, "bloom", redirect_to="projects/clawd-world/_project"
    )
    _write_project(tmp_path, "clawd-world")
    assert resolve(tmp_path, "bloom") == "projects/clawd-world/_project"


def test_resolves_vault_relative_path_chain(tmp_path: Path):
    # Path → path → path chain: a vault-relative path whose redirect_to is
    # another vault-relative path, which itself redirects to a third
    # vault-relative path. Confirms chain resolution still works after the
    # v0.11.4 path-traversal hardening tightened vault-relative handling.
    foo_dir = tmp_path / "projects" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "_project.md").write_text(
        "---\ntype: project\nredirect_to: projects/bar/_project.md\n---\n",
        encoding="utf-8",
    )
    bar_dir = tmp_path / "projects" / "bar"
    bar_dir.mkdir(parents=True)
    (bar_dir / "_project.md").write_text(
        "---\ntype: project\nredirect_to: topics/baz.md\n---\n",
        encoding="utf-8",
    )
    _write_topic(tmp_path, "baz")
    # Terminal is in topics/ — resolver returns bare slug, not the path.
    assert resolve(tmp_path, "projects/foo/_project.md") == "baz"
