"""Auto-memory sync only recognizes frontmatter delimiters on their own lines."""

import pytest

import memex.scripts.sync_auto_memory as sync


def test_sync_roundtrip_with_separator_in_title(tmp_path):
    source = tmp_path / "memory.md"
    source.write_text('---\ntitle: "before --- after"\nsource: internal\n---\n# Actual memory\n')
    item = {
        "source_path": str(source),
        "filename": source.name,
        "project_memex": "demo",
        "modified_date": "2026-09-06",
        "source_hash": sync.content_hash(source.read_text()),
        "is_memory_md": False,
        "title": "before --- after",
    }
    vault = tmp_path / "vault"
    plan = sync.compute_sync_plan([item], {}, vault)
    assert sync.sync_all(plan, vault, dry_run=False)[0]["status"] == "created"

    imported = (vault / sync.vault_path_for(item)).read_text()
    body = sync.strip_source_frontmatter(imported)
    assert body.startswith("# Actual memory\n")
    assert "source: internal" not in body
    assert sync.parse_frontmatter_simple(imported)["title"] == item["title"]

    next_plan = sync.compute_sync_plan([item], sync.get_vault_sync_state(vault), vault)
    assert next_plan[0]["action"] == "unchanged"


@pytest.mark.parametrize("source", [
    "---no frontmatter\nkeep this --- text\n",
    '---\ntitle: "no real --- closing delimiter"\n# Keep all of this\n',
])
def test_incomplete_or_non_frontmatter_is_preserved(source):
    assert sync.strip_source_frontmatter(source) == source
    assert sync.parse_frontmatter_simple(source) == {}


def test_crlf_frontmatter_ends_only_on_its_own_line():
    source = '---\r\ntitle: "before --- after"\r\n---\r\n# Body\r\n'
    assert sync.parse_frontmatter_simple(source) == {"title": "before --- after"}
    assert sync.strip_source_frontmatter(source) == "# Body\r\n"
