"""Tests for the date-stripped memo-reference split in `memex check`.

Memos link siblings by bare slug (``[[discussion-diagnostic-revision-plan]]``)
while the file is ``2026-03-11-discussion-diagnostic-revision-plan.md``. Those
ghosts are memo references, not concepts — on 2026-09-23 they were 43 links /
64 refs, one of them OVERDUE — so they are split out of the crystallization tiers.
"""

from __future__ import annotations

from pathlib import Path

from memex.scripts.crystallization_check import _memo_stem_map, analyze, split_memo_refs


def _memo(vault: Path, project: str, name: str) -> None:
    p = vault / "projects" / project / "memos" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntitle: t\n---\n", encoding="utf-8")


def test_stem_map_strips_iso_and_compact_prefixes(tmp_path: Path) -> None:
    _memo(tmp_path, "a", "2026-03-11-discussion-plan.md")
    _memo(tmp_path, "b", "20260119-1424-mdx-fix.md")
    _memo(tmp_path, "b", "20260208-clawd-sprint.md")
    _memo(tmp_path, "c", "no-date-prefix.md")
    m = _memo_stem_map(tmp_path)
    assert m == {
        "discussion-plan": "projects/a/memos/2026-03-11-discussion-plan.md",
        "mdx-fix": "projects/b/memos/20260119-1424-mdx-fix.md",
        "clawd-sprint": "projects/b/memos/20260208-clawd-sprint.md",
    }


def test_ambiguous_stems_are_not_mapped(tmp_path: Path) -> None:
    _memo(tmp_path, "a", "2026-01-01-weekly-sync.md")
    _memo(tmp_path, "a", "2026-01-08-weekly-sync.md")
    assert "weekly-sync" not in _memo_stem_map(tmp_path)


def test_split_removes_memo_refs_from_crystallization(tmp_path: Path) -> None:
    _memo(tmp_path, "duality", "2026-03-11-discussion-diagnostic-revision-plan.md")
    unresolved = {
        "discussion-diagnostic-revision-plan": [f"projects/duality/memos/m{i}.md" for i in range(5)],
        "real-concept": ["projects/x/memos/a.md", "projects/y/memos/b.md", "projects/y/memos/c.md"],
    }
    memo_refs, rest = split_memo_refs(unresolved, _memo_stem_map(tmp_path))
    assert [r["link"] for r in memo_refs] == ["discussion-diagnostic-revision-plan"]
    assert memo_refs[0]["refs"] == 5
    assert memo_refs[0]["memo"] == "projects/duality/memos/2026-03-11-discussion-diagnostic-revision-plan.md"
    assert [e["link"] for e in analyze(rest)] == ["real-concept"]


def test_split_is_case_insensitive(tmp_path: Path) -> None:
    _memo(tmp_path, "p", "2026-05-01-Context-Hygiene.md")
    memo_refs, rest = split_memo_refs({"context-hygiene": ["x.md"]}, _memo_stem_map(tmp_path))
    assert len(memo_refs) == 1 and rest == {}


def test_main_reports_memo_refs_outside_the_tiers(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    import memex.scripts.crystallization_check as cc

    _memo(tmp_path, "d", "2026-03-11-diag-plan.md")
    for i in range(5):
        p = tmp_path / "projects" / "d" / "memos" / f"2026-03-1{i}-m{i}.md"
        p.write_text(f"---\ntitle: m{i}\n---\nsee [[diag-plan]] and [[real-concept]]\n", encoding="utf-8")

    class _NoObsidian:
        def is_available(self) -> bool:
            return False

    monkeypatch.setattr(cc, "get_memex_path", lambda: tmp_path)
    monkeypatch.setattr(cc, "get_state_dir", lambda: tmp_path / ".state")
    monkeypatch.setattr(cc, "get_obsidian_cli", lambda: _NoObsidian())
    monkeypatch.setattr("sys.argv", ["crystallization_check", "--json", "--no-save"])
    cc.main()
    out = json.loads(capsys.readouterr().out)
    assert [r["link"] for r in out["memo_refs"]] == ["diag-plan"]
    links = [e["link"] for e in out["entries"]]
    assert "diag-plan" not in links and "real-concept" in links
