"""Build the Codex and Kimi Code plugin surfaces from the Claude Code one.

One source of truth — ``skills/``, ``commands/``, ``.claude-plugin/plugin.json``
and ``pyproject.toml`` — and three hosts:

Claude Code
    ``.claude-plugin/plugin.json`` at the repo root: skills, commands, hooks.

Codex
    ``plugins/memex/.codex-plugin/plugin.json``, a self-contained plugin root,
    listed in ``.agents/plugins/marketplace.json`` as ``source: local``. Codex
    copies the plugin root into its cache and auto-discovers ``skills/``,
    ``hooks/hooks.json`` and ``.mcp.json`` there, so the root can't be the repo
    root: Codex would cache the whole repo and run memex's Claude hooks, which
    expect Claude transcripts. Same layout kimi-plugin-cc shipped and verified
    live in v1.6.0. Codex has no plugin commands; ``/memex:save`` is covered by
    the memo-writing skill.

Kimi Code
    ``kimi.plugin.json`` at the repo root. A GitHub install copies the whole
    repo and can't target a subfolder. It declares ``skills`` →
    ``./plugins/memex/skills/`` (the same portable copy) and nothing else. No
    commands: ``commands/*.md`` are Claude-specific throughout (``Task(...)``
    delegation, ``$CLAUDE_CODE_SESSION_ID``); the memo-writing skill covers
    saving. No hooks: Kimi only runs declared hooks, has its own hook format,
    and gives hooks no transcript path, so SessionEnd archiving and PreCompact
    can't work there. Both non-Claude hosts get the same skills-only contract.

Portable skills are ``skills/`` with:
  - frontmatter reduced to ``name`` + ``description`` (``allowed-tools``,
    ``argument-hint`` and ``effort`` are Claude-only),
  - the Claude-only ``!`cmd``` load-time injections turned into plain
    "(run: `cmd`)" instructions, which would otherwise show up as literal text,
  - one provenance line.
Reference files next to a SKILL.md (e.g. ``memo-default.md``) are copied as-is.

``--check`` exits 1 when the committed surface differs from what would be
generated. ``tests/test_portable_plugin.py`` runs the same check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

PORTABLE_ROOT = Path("plugins/memex")
PORTABLE_SKILLS = PORTABLE_ROOT / "skills"
CODEX_MANIFEST = PORTABLE_ROOT / ".codex-plugin" / "plugin.json"
CODEX_MARKETPLACE = Path(".agents/plugins/marketplace.json")
KIMI_MANIFEST = Path("kimi.plugin.json")

_BANG_RE = re.compile(r"(?<!`)!`([^`\n]+)`")
_KEEP_FM_KEYS = ("name", "description")

DISPLAY_NAME = "Memex"
SHORT_DESCRIPTION = "Session memos, topics and hybrid search across your agent work"
LONG_DESCRIPTION = (
    "Skills for the memex knowledge base: recall past sessions (temporal, keyword, deep "
    "synthesis), write session memos with observations, and tend the vault (condense, "
    "crystallize, consolidate). Requires the memex CLI on PATH (uv tool install); the "
    "vault location comes from ~/.memex/config.json. Transcript archiving and pre-compaction "
    "memo signals are Claude Code hooks and are not part of this package."
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _version(root: Path) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _claude_manifest(root: Path) -> dict:
    return json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def _json(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def portable_skill_md(text: str, skill_name: str) -> str:
    """Rewrite one Claude SKILL.md into its portable form."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"skills/{skill_name}/SKILL.md has no frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError(f"skills/{skill_name}/SKILL.md has an unclosed frontmatter block")
    kept = [ln for ln in lines[1:end] if ln.split(":", 1)[0].strip() in _KEEP_FM_KEYS and not ln[:1].isspace()]
    values = {ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in kept}
    for key in _KEEP_FM_KEYS:
        # a block scalar (`description: >-` + indented lines) would be cut to its
        # indicator by a line filter — refuse rather than ship an empty field
        v = values.get(key, "")
        quoted = re.match(r"""^(["'])(.*?)\1(?:\s+#.*)?\s*$""", v)
        if quoted:
            bare = quoted.group(2).strip()
        elif v[:1] in ('"', "'"):
            bare = ""  # opening quote never closed on this line: a multi-line scalar
        else:
            bare = "" if v.startswith("#") else re.split(r"\s+#", v, maxsplit=1)[0].strip()
        if not bare or bare.lower() in ("null", "~") or v[:1] in ("|", ">"):
            raise ValueError(f"skills/{skill_name}/SKILL.md needs a single-line {key}: value")
    body = "\n".join(lines[end + 1:])
    body = _BANG_RE.sub(lambda m: f"(run: `{m.group(1)}`)", body)
    note = (
        f"> Portable copy of `skills/{skill_name}/SKILL.md` for Codex and Kimi Code, generated by "
        "`scripts/portable_plugin.py` (do not edit here). Needs the `memex` CLI on PATH. Claude "
        "Code names (the Task/Agent tool, `/memex:save`, hooks) map to your host's equivalents."
    )
    return "\n".join(["---", *kept, "---", "", note, "", body.lstrip("\n")])


def build(root: Path) -> dict[Path, bytes]:
    """Every generated file, keyed by repo-relative path."""
    out: dict[Path, bytes] = {}
    skills_src = root / "skills"
    for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
        if not (skill_dir / "SKILL.md").is_file():
            continue
        for f in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
            if "__pycache__" in f.parts or f.name == ".DS_Store":
                continue
            rel = PORTABLE_SKILLS / f.relative_to(skills_src)
            if f.name == "SKILL.md":
                out[rel] = portable_skill_md(f.read_text(encoding="utf-8"), skill_dir.name).encode("utf-8")
            else:
                out[rel] = f.read_bytes()

    claude = _claude_manifest(root)
    version = _version(root)
    author = claude.get("author", {"name": "Xule Lin"})
    homepage = claude.get("homepage") or claude.get("repository")
    common = {
        "name": claude["name"],
        "version": version,
        "description": claude["description"],
        "author": author,
        "homepage": homepage,
        "repository": claude.get("repository", homepage),
        "license": claude.get("license", "MIT"),
        "keywords": claude.get("keywords", []),
    }

    out[CODEX_MANIFEST] = _json({
        **common,
        "skills": "./skills/",
        "interface": {
            "displayName": DISPLAY_NAME,
            "shortDescription": SHORT_DESCRIPTION,
            "longDescription": LONG_DESCRIPTION,
            "developerName": author.get("name", ""),
            "category": "Productivity",
            "capabilities": ["Local Shell", "Read", "Write"],
            "websiteURL": homepage,
            "defaultPrompt": [
                "Use $recall to find what I worked on last week.",
                "Use $memo-writing to save a memo of this session to memex.",
                "Use $garden-tending to check the memex vault's health.",
            ],
        },
    })
    out[CODEX_MARKETPLACE] = _json({
        "name": "memex-plugin",
        "interface": {"displayName": DISPLAY_NAME},
        "plugins": [{
            "name": claude["name"],
            "source": {"source": "local", "path": f"./{PORTABLE_ROOT.as_posix()}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }],
    })
    out[KIMI_MANIFEST] = _json({
        **common,
        "skills": f"./{PORTABLE_SKILLS.as_posix()}/",
        "interface": {
            "displayName": DISPLAY_NAME,
            "shortDescription": SHORT_DESCRIPTION,
            "longDescription": LONG_DESCRIPTION,
            "developerName": author.get("name", ""),
            "websiteURL": homepage,
        },
    })
    return out


def _on_disk(root: Path) -> set[Path]:
    base = root / PORTABLE_SKILLS
    existing = {p.relative_to(root) for p in base.rglob("*") if p.is_file()} if base.is_dir() else set()
    return {p for p in existing if "__pycache__" not in p.parts and p.name != ".DS_Store"}


def diff(root: Path) -> list[str]:
    """Human-readable drift list (empty = in sync)."""
    want = build(root)
    problems = []
    for rel, data in sorted(want.items()):
        path = root / rel
        if not path.is_file():
            problems.append(f"missing  {rel}")
        elif path.read_bytes() != data:
            problems.append(f"stale    {rel}")
    for rel in sorted(_on_disk(root) - set(want)):
        problems.append(f"orphan   {rel}")
    return problems


def write(root: Path) -> list[str]:
    want = build(root)
    changed = []
    for rel in sorted(_on_disk(root) - set(want)):
        (root / rel).unlink()
        changed.append(f"removed  {rel}")
    for rel, data in sorted(want.items()):
        path = root / rel
        if path.is_file() and path.read_bytes() == data:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        changed.append(f"wrote    {rel}")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Codex / Kimi Code plugin surfaces from skills/.")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the committed surface is out of date.")
    parser.add_argument("--root", type=Path, default=None, help="Repo root (default: this checkout).")
    args = parser.parse_args()
    root = (args.root or repo_root()).resolve()
    if args.check:
        problems = diff(root)
        for p in problems:
            print(p)
        if problems:
            print("Portable plugin surface is out of date — run: uv run python scripts/portable_plugin.py")
            sys.exit(1)
        print("✓ Codex / Kimi Code plugin surface is in sync with skills/.")
        return
    changed = write(root)
    print("\n".join(changed) if changed else "✓ Already in sync.")


if __name__ == "__main__":
    main()
