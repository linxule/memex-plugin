"""The Codex / Kimi Code plugin surfaces generated from skills/ (portable_plugin).

Drift gate + host-safety invariants: Codex must never see memex's Claude hooks
(it auto-discovers hooks/hooks.json in a plugin root and would feed them Codex
transcripts), and Kimi only runs hooks it is told about — so neither manifest
declares hooks, and the Codex plugin root holds nothing hook-shaped.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from memex.scripts.portable_plugin import (
    CODEX_MANIFEST,
    CODEX_MARKETPLACE,
    KIMI_MANIFEST,
    PORTABLE_ROOT,
    build,
    diff,
    portable_skill_md,
    repo_root,
    write,
)
from memex.scripts.wikilink_filters import is_noncurated_source

ROOT = repo_root()


def _load(rel: Path) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_committed_surface_matches_generator() -> None:
    # regenerate with: uv run python scripts/portable_plugin.py
    assert diff(ROOT) == []


def test_every_manifest_carries_the_pyproject_version() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert _load(Path(".claude-plugin/plugin.json"))["version"] == version
    assert _load(Path(".claude-plugin/marketplace.json"))["version"] == version
    assert _load(CODEX_MANIFEST)["version"] == version
    assert _load(KIMI_MANIFEST)["version"] == version


def test_codex_manifest_shape() -> None:
    m = _load(CODEX_MANIFEST)
    assert "hooks" not in m and "commands" not in m and "mcpServers" not in m
    assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"])
    assert (ROOT / PORTABLE_ROOT / m["skills"]).is_dir()
    prompts = m["interface"]["defaultPrompt"]
    assert len(prompts) <= 3 and all(len(p) <= 128 for p in prompts)


def test_codex_plugin_root_has_nothing_codex_would_autoload_as_hooks_or_mcp() -> None:
    assert not (ROOT / PORTABLE_ROOT / "hooks").exists()
    assert not (ROOT / PORTABLE_ROOT / ".mcp.json").exists()
    assert not (ROOT / PORTABLE_ROOT / ".app.json").exists()


def test_codex_marketplace_points_at_the_portable_root() -> None:
    mk = _load(CODEX_MARKETPLACE)
    (entry,) = mk["plugins"]
    assert entry["source"] == {"source": "local", "path": f"./{PORTABLE_ROOT.as_posix()}"}
    assert (ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()


def test_kimi_manifest_shape() -> None:
    m = _load(KIMI_MANIFEST)
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", m["name"])
    assert "hooks" not in m and "commands" not in m  # both Claude-specific
    assert m["skills"].startswith("./") and (ROOT / m["skills"]).is_dir()


def test_portable_skills_keep_only_portable_frontmatter() -> None:
    for skill in sorted((ROOT / PORTABLE_ROOT / "skills").glob("*/SKILL.md")):
        head = skill.read_text().split("---")[1]
        pairs = [ln.split(":", 1) for ln in head.strip().splitlines()]
        assert {k for k, _ in pairs} == {"name", "description"}, skill
        assert all(v.strip() and v.strip()[:1] not in "|>" for _, v in pairs), skill
        assert "!`" not in skill.read_text().replace("`!`", ""), skill


# ── transform rules ───────────────────────────────────────────────────

_SRC = """---
name: demo
argument-hint: "[x]"
description: Demo skill.
allowed-tools: Read, Bash
effort: high
---

# Demo

**Date:** !`date +%Y-%m-%d`
Inside a `!`command`` injection, $1 is substituted.
"""


def test_transform_strips_claude_frontmatter_and_rewrites_injections() -> None:
    out = portable_skill_md(_SRC, "demo")
    assert out.startswith("---\nname: demo\ndescription: Demo skill.\n---\n")
    assert "allowed-tools" not in out and "argument-hint" not in out and "effort:" not in out
    assert "**Date:** (run: `date +%Y-%m-%d`)" in out
    # prose that merely MENTIONS the syntax inside a code span is left alone
    assert "Inside a `!`command`` injection" in out
    assert "Portable copy of `skills/demo/SKILL.md`" in out


def test_transform_rejects_missing_or_unclosed_frontmatter() -> None:
    with pytest.raises(ValueError):
        portable_skill_md("# no frontmatter\n", "x")
    with pytest.raises(ValueError):
        portable_skill_md("---\nname: x\ndescription: y\n", "x")


def test_write_removes_orphans_and_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "skills" / "a").mkdir(parents=True)
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("---\nname: a\ndescription: A.\n---\nbody\n")
    (tmp_path / "commands").mkdir()
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "memex", "description": "d", "author": {"name": "X"}, "license": "MIT",
         "repository": "https://example.invalid/r"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "memex"\nversion = "9.9.9"\n')
    stale = tmp_path / PORTABLE_ROOT / "skills" / "gone" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    write(tmp_path)
    assert not stale.exists()
    assert diff(tmp_path) == []
    assert write(tmp_path) == []
    assert json.loads((tmp_path / KIMI_MANIFEST).read_text())["version"] == "9.9.9"
    assert set(build(tmp_path)) >= {CODEX_MANIFEST, CODEX_MARKETPLACE, KIMI_MANIFEST}


def test_generated_tree_casts_no_wikilink_votes() -> None:
    assert is_noncurated_source("plugins/memex/skills/recall/SKILL.md")
    assert not is_noncurated_source("plugins/notes/x.md")  # a curated plugins/ folder still votes
    assert not is_noncurated_source("projects/plugins/memos/x.md")
    assert not is_noncurated_source("topics/plugins.md")


def test_transform_refuses_block_scalar_description() -> None:
    with pytest.raises(ValueError):
        portable_skill_md("---\nname: x\ndescription: >-\n  Useful description.\n---\nbody\n", "x")


@pytest.mark.parametrize("value", ['""', "''", "null", "~", "# nothing", '""\t# empty',
                                   "null\t# empty", '"multi-line starts here'])
def test_transform_refuses_empty_description(value: str) -> None:
    with pytest.raises(ValueError):
        portable_skill_md(f"---\nname: x\ndescription: {value}\n---\nbody\n", "x")
