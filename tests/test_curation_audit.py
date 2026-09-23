"""Tests for the curation backlog audit (memex check --signals / --condense).

Both scans were re-derived by hand in every tending pass and got two things
wrong each time: the signal count included closed/archived sections, and the
condense count string-compared legacy ``YYYYMMDD-`` memo names against an ISO
``condensed:`` date (every legacy memo sorted as "newer").
"""

from __future__ import annotations

from pathlib import Path

from memex.scripts.curation_audit import audit_condense, audit_signals


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _topic(vault: Path, slug: str, fm: str, body: str) -> Path:
    return _write(vault / "topics" / f"{slug}.md", f"---\n{fm}\n---\n\n# {slug}\n\n{body}")


def _slugs(report: dict) -> dict[str, int]:
    return {t["slug"]: t["open"] for t in report["topics"]}


# ── --signals ─────────────────────────────────────────────────────────

def test_open_signals_counted(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept\nupdated: 2026-09-08",
           "Body.\n\n## Recent signals\n\n"
           "- 2026-09-01: old ([[projects/p/memos/x|memo]])\n"
           "- 2026-09-10: new ([[projects/p/memos/y|memo]])\n"
           "- 2026-09-12: newer ([[projects/p/memos/z|memo]])\n")
    r = audit_signals(tmp_path)
    t = r["topics"][0]
    assert (t["slug"], t["open"], t["since_updated"]) == ("a", 3, 2)
    assert (t["oldest"], t["newest"]) == ("2026-09-01", "2026-09-12")
    assert r["total_open"] == 3


def test_closed_header_is_audit_trail(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept",
           "## Recent signals (closed — migrated to canonical)\n\n- 2026-04-01: x\n- 2026-04-02: y\n")
    r = audit_signals(tmp_path)
    assert r["topics"] == [] and r["closed_sections"] == 1


def test_closed_blockquote_is_audit_trail(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept",
           "## Recent signals\n\n> **Closed 2026-05-25.** All absorbed.\n\n- 2026-04-09: x\n")
    assert audit_signals(tmp_path)["topics"] == []


def test_archived_and_redirect_topics_skipped(tmp_path: Path) -> None:
    _topic(tmp_path, "arch", "status: archived", "## Recent signals\n\n- 2026-09-10: x\n")
    _topic(tmp_path, "redir", "redirect_to: canonical", "## Recent signals\n\n- 2026-09-10: x\n")
    r = audit_signals(tmp_path)
    assert r["topics"] == [] and r["retired_topics_skipped"] == 2


def test_section_ends_at_next_heading(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept",
           "## Recent signals\n\n- 2026-09-10: x\n\n## Related\n\n- [[b]]\n- [[c]]\n")
    assert _slugs(audit_signals(tmp_path)) == {"a": 1}


def test_subheading_does_not_end_section(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept",
           "## Recent signals\n\n- 2026-09-10: x\n### note\n- 2026-09-11: y\n")
    assert _slugs(audit_signals(tmp_path)) == {"a": 2}


def test_open_and_closed_sections_on_one_topic(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept",
           "## Recent signals (closed — absorbed)\n\n- 2026-04-01: old\n\n"
           "## Recent signals\n\n- 2026-09-10: live\n")
    r = audit_signals(tmp_path)
    assert _slugs(r) == {"a": 1} and r["closed_sections"] == 1


def test_topic_without_section_or_bullets_not_listed(tmp_path: Path) -> None:
    _topic(tmp_path, "none", "type: concept", "Just prose.\n")
    _topic(tmp_path, "empty", "type: concept", "## Recent signals\n\n## Next\n")
    assert audit_signals(tmp_path)["topics"] == []


def test_trail_type_reported(tmp_path: Path) -> None:
    _topic(tmp_path, "t", "type: trail", "## Recent signals\n\n- 2026-09-10: x\n")
    assert audit_signals(tmp_path)["topics"][0]["type"] == "trail"


def test_missing_topics_dir(tmp_path: Path) -> None:
    assert audit_signals(tmp_path)["topics"] == []


# ── --condense ────────────────────────────────────────────────────────

def _project(vault: Path, name: str, fm: str, memos: dict[str, str]) -> None:
    _write(vault / "projects" / name / "_project.md", f"---\ntype: project\nname: {name}\n{fm}\n---\n\n# {name}\n")
    for fname, body in memos.items():
        _write(vault / "projects" / name / "memos" / fname, body)


def _rows(report: dict) -> dict[str, dict]:
    return {r["project"]: r for r in report["projects"]}


def test_legacy_compact_filenames_are_not_newer(tmp_path: Path) -> None:
    # the exact 2026-09-23 miscount: "20260119-…" > "2026-09-08" as strings
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 2", {
        "20260119-1424-old-fix.md": "---\ntitle: x\n---\n",
        "20260812-0900-also-old.md": "---\ntitle: x\n---\n",
    })
    assert audit_condense(tmp_path)["projects"] == []


def test_iso_memos_after_condensed_counted(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 1", {
        "2026-09-01-before.md": "x", "2026-09-10-after.md": "x", "20260915-1200-after2.md": "x",
    })
    r = _rows(audit_condense(tmp_path))["p"]
    assert (r["newer"], r["digested_gap"], r["newest"]) == (2, 2, "2026-09-15")
    assert r["newer_memos"] == ["2026-09-10-after.md", "20260915-1200-after2.md"]


def test_same_day_memo_caught_by_digested_gap(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 1", {
        "2026-09-08-a.md": "x", "2026-09-08-b.md": "x",
    })
    r = _rows(audit_condense(tmp_path))["p"]
    assert (r["newer"], r["digested_gap"]) == (0, 1)


def test_never_condensed_counts_all(tmp_path: Path) -> None:
    _project(tmp_path, "p", "created: 2026-01-01", {"2026-02-01-a.md": "x", "2026-03-01-b.md": "x"})
    r = _rows(audit_condense(tmp_path))["p"]
    assert (r["condensed"], r["newer"], r["digested_gap"]) == (None, 2, None)


def test_undated_filename_falls_back_to_frontmatter_date(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 1", {
        "notes-on-x.md": "---\ntitle: x\ndate: 2026-09-20\n---\n",
        "other.md": "---\ntitle: y\ndate: 2026-08-01\n---\n",
    })
    r = _rows(audit_condense(tmp_path))["p"]
    assert r["newer_memos"] == ["notes-on-x.md"]


def test_archived_or_redirect_project_skipped(tmp_path: Path) -> None:
    _project(tmp_path, "old", "status: archived", {"2026-09-10-a.md": "x"})
    _project(tmp_path, "moved", "redirect_to: new", {"2026-09-10-a.md": "x"})
    assert audit_condense(tmp_path)["projects"] == []


def test_current_project_not_listed(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-23\nmemos_digested: 1", {"2026-09-20-a.md": "x"})
    assert audit_condense(tmp_path)["projects"] == []


def test_obs_sidecars_are_not_memos(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 1", {
        "2026-09-01-a.md": "x", "2026-09-10-b.obs.jsonl": "{}\n",
    })
    assert audit_condense(tmp_path)["projects"] == []


def test_ordering_by_newer_then_gap(tmp_path: Path) -> None:
    _project(tmp_path, "small", "condensed: 2026-09-08\nmemos_digested: 1", {"2026-09-10-a.md": "x"})
    _project(tmp_path, "big", "condensed: 2026-09-08\nmemos_digested: 0",
             {"2026-09-10-a.md": "x", "2026-09-11-b.md": "x"})
    assert [r["project"] for r in audit_condense(tmp_path)["projects"]] == ["big", "small"]


def test_updated_is_fallback_basis_when_no_condensed(tmp_path: Path) -> None:
    _project(tmp_path, "p", "updated: 2026-09-08", {"2026-09-01-a.md": "x", "2026-09-10-b.md": "x"})
    r = _rows(audit_condense(tmp_path))["p"]
    assert (r["basis"], r["condensed"], r["newer"]) == ("updated", "2026-09-08", 1)


def test_substantial_unstamped_overview_is_maintained_not_never(tmp_path: Path) -> None:
    _project(tmp_path, "big", "created: 2026-01-01", {"2026-02-01-a.md": "x"})
    ov = tmp_path / "projects" / "big" / "_project.md"
    ov.write_text(ov.read_text() + "\n".join(f"line {i}" for i in range(60)))
    _project(tmp_path, "stub", "created: 2026-01-01", {"2026-02-01-a.md": "x"})
    rows = _rows(audit_condense(tmp_path))
    assert rows["big"]["basis"] == "unstamped" and rows["stub"]["basis"] == "never"


# ── v0.20.0 review round 1 (Codex) ────────────────────────────────────

def test_yaml_inline_comments_and_nulls(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08  # last pass\nmemos_digested: 1 # previously folded\n"
             "redirect_to: null\nstatus: active # note", {"2026-09-08-a.md": "x", "2026-09-08-b.md": "x"})
    r = _rows(audit_condense(tmp_path))["p"]
    assert (r["condensed"], r["digested_gap"]) == ("2026-09-08", 1)
    _project(tmp_path, "gone", "status: archived # old", {"2026-09-10-a.md": "x"})
    assert "gone" not in _rows(audit_condense(tmp_path))


def test_negated_closure_words_do_not_close_a_section(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept",
           "## Recent signals\n\n> Not yet absorbed into the topic.\n\n- 2026-09-23: important\n")
    _topic(tmp_path, "b", "type: concept",
           "## Recent signals (not closed — three pending)\n\n- 2026-09-23: x\n")
    assert _slugs(audit_signals(tmp_path)) == {"a": 1, "b": 1}


# ── wiring: `memex check --signals/--condense --json` end to end ──────

def test_cli_check_signals_and_condense_json(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    from typer.testing import CliRunner

    import memex.scripts.curation_audit as ca
    from memex.cli import app

    _topic(tmp_path, "a", "type: concept", "## Recent signals\n\n- 2026-09-10: x\n")
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 0", {"2026-09-10-a.md": "x"})
    monkeypatch.setattr(ca, "get_memex_path", lambda: tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["check", "--signals", "--json"])
    assert r.exit_code == 0, r.output
    assert _json.loads(r.output)["topics"][0]["slug"] == "a"
    r = runner.invoke(app, ["check", "--condense", "--json"])
    assert r.exit_code == 0, r.output
    assert _json.loads(r.output)["projects"][0]["project"] == "p"


def test_comment_only_value_is_empty(tmp_path: Path) -> None:
    _topic(tmp_path, "a", "type: concept\nredirect_to: # none yet", "## Recent signals\n\n- 2026-09-10: x\n")
    assert _slugs(audit_signals(tmp_path)) == {"a": 1}


def test_quoted_value_with_inline_comment(tmp_path: Path) -> None:
    _topic(tmp_path, "gone", 'status: "archived"  # old', "## Recent signals\n\n- 2026-09-10: x\n")
    assert audit_signals(tmp_path)["topics"] == []


def test_negative_gap_is_reported_as_stamp_drift_not_backlog(tmp_path: Path) -> None:
    # (Kimi round 4) memos moved out after stamping: stamp > files
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 5", {"2026-09-01-a.md": "x"})
    r = audit_condense(tmp_path)
    assert r["projects"] == []
    assert r["stamp_drift"] == [{"project": "p", "memos_digested": 5, "memos": 1}]


def test_indented_dashes_do_not_close_frontmatter(tmp_path: Path) -> None:
    _topic(tmp_path, "t", "type: concept\nnote: |\n  a\n  ---\nstatus: archived", "## Recent signals\n\n- 2026-09-10: x\n")
    assert audit_signals(tmp_path)["topics"] == []
