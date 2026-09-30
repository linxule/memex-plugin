"""Tests for the curation backlog audit (memex check --signals / --condense).

Both scans were re-derived by hand in every tending pass and got two things
wrong each time: the signal count included closed/archived sections, and the
condense count string-compared legacy ``YYYYMMDD-`` memo names against an ISO
``condensed:`` date (every legacy memo sorted as "newer").
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from memex.scripts.curation_audit import _git_add_dates, audit_condense, audit_signals


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
    # the CLI resolves the vault itself before delegating (cli._setup); without
    # this the test only passes on a machine with ~/.memex/config.json (CI caught it)
    monkeypatch.setattr("memex.paths.get_memex_path", lambda *a, **k: tmp_path)
    monkeypatch.chdir(tmp_path)
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
    assert r["stamp_drift"] == [{"project": "p", "memos_digested": 5, "memos": 1, "archived": 0}]


def test_indented_dashes_do_not_close_frontmatter(tmp_path: Path) -> None:
    _topic(tmp_path, "t", "type: concept\nnote: |\n  a\n  ---\nstatus: archived", "## Recent signals\n\n- 2026-09-10: x\n")
    assert audit_signals(tmp_path)["topics"] == []


# ── --condense: arrivals (git first-add) and memos/*/ set-asides ─────────


@pytest.fixture(autouse=True)
def _no_ambient_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tmp_path may sit inside a checkout (pytest --basetemp): without a ceiling
    # the non-git tests would see that repo and every memo would be "untracked".
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def _git(vault: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when}T12:00:00"
    env.setdefault("GIT_AUTHOR_NAME", "t"); env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    env.setdefault("GIT_COMMITTER_NAME", "t"); env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=vault, env=env,
                   check=True, capture_output=True)


def _git_vault(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git not available")
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_archived_subfolder_memos_reported_not_counted(tmp_path: Path) -> None:
    # alcor 2026-09-30: stamp 45, 25 top-level memos, 27 under memos/archive/
    _project(tmp_path, "p", "condensed: 2026-09-20\nmemos_digested: 5", {
        "2026-01-01-a.md": "x", "2026-01-02-b.md": "x",
        "archive/2026-01-03-c.md": "x", "archive/2026-01-04-d.md": "x", "archive/2026-01-05-e.md": "x",
    })
    report = audit_condense(tmp_path)
    assert report["projects"] == []
    assert report["stamp_drift"] == [{"project": "p", "memos_digested": 5, "memos": 2, "archived": 3}]


def test_arrived_is_none_without_git(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 1", {
        "2026-09-01-before.md": "x", "2026-09-10-after.md": "x",
    })
    r = _rows(audit_condense(tmp_path))["p"]
    assert r["arrived"] is None and r["arrived_memos"] == [] and r["archived"] == 0


def test_memo_added_to_git_after_condensed_is_arrived(tmp_path: Path) -> None:
    # The 2026-09-30 class: an overview condensed 09-20 with the count right at the
    # time; a Feb-dated memo consolidated in on 09-25 never shows as NEW.
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 2", {
        "2026-03-01-old.md": "x", "2026-03-02-old2.md": "x",
    })
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    _write(vault / "projects" / "p" / "memos" / "2026-02-15-moved-in.md", "x")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "consolidate", when="2026-09-25")
    r = _rows(audit_condense(vault))["p"]
    assert (r["newer"], r["arrived"], r["digested_gap"]) == (0, 1, 1)
    assert r["arrived_memos"] == ["2026-02-15-moved-in.md"]


def test_memo_added_before_condensed_is_not_arrived(tmp_path: Path) -> None:
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-old.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    assert audit_condense(vault)["projects"] == []


def test_untracked_old_dated_memo_counts_as_arrived_today(tmp_path: Path) -> None:
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-old.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    _write(vault / "projects" / "p" / "memos" / "2026-01-01-untracked.md", "x")
    r = _rows(audit_condense(vault))["p"]
    assert r["arrived_memos"] == ["2026-01-01-untracked.md"]


def test_git_mv_between_projects_arrives_at_move_date(tmp_path: Path) -> None:
    # --no-renames: the move is an add at the new path on the day of the move.
    vault = _git_vault(tmp_path)
    _project(vault, "a", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-01-10-x.md": "x"})
    _project(vault, "b", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-01-11-y.md": "y"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-01")
    _git(vault, "mv", "projects/a/memos/2026-01-10-x.md", "projects/b/memos/2026-01-10-x.md")
    _git(vault, "commit", "-q", "-m", "move", when="2026-09-28")
    rows = _rows(audit_condense(vault))
    assert rows["b"]["arrived_memos"] == ["2026-01-10-x.md"] and rows["b"]["digested_gap"] == 1
    assert "a" not in rows  # a now has 0 memos → skipped entirely


def test_arrived_alone_lists_a_project_the_stamp_drift_branch_would_hide(tmp_path: Path) -> None:
    # stamp 3 > 2 on disk AND one of the two arrived after the stamp: the arrival
    # must win over the drift shortcut (a moved-out memo and a moved-in memo can coexist).
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 3", {"2026-03-01-old.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    _write(vault / "projects" / "p" / "memos" / "2026-02-01-in.md", "x")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "in", when="2026-09-25")
    report = audit_condense(vault)
    assert report["stamp_drift"] == []
    assert _rows(report)["p"]["arrived"] == 1


def test_non_ascii_memo_path_is_matched_not_treated_as_untracked(tmp_path: Path) -> None:
    # git octal-escapes non-ASCII bytes in --name-only output unless
    # core.quotePath=false; an escaped path never matches disk → false "arrived".
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 2",
             {"2026-03-01-café-über.md": "x", "2026-03-02-plain.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    report = audit_condense(vault)
    assert report["arrivals_basis"] == "git" and report["projects"] == []


def test_git_mv_first_add_date_is_the_move_date(tmp_path: Path) -> None:
    # Pins --no-renames: with rename detection the moved path would have no add
    # record at all (→ untracked → today), which also passes the presence test above.
    vault = _git_vault(tmp_path)
    _project(vault, "a", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-01-10-x.md": "x"})
    _project(vault, "b", "condensed: 2026-09-20\nmemos_digested: 0", {})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-01")
    (vault / "projects" / "b" / "memos").mkdir(parents=True, exist_ok=True)
    _git(vault, "mv", "projects/a/memos/2026-01-10-x.md", "projects/b/memos/2026-01-10-x.md")
    _git(vault, "commit", "-q", "-m", "move", when="2026-09-28")
    first, basis = _git_add_dates(vault)
    assert first is not None and basis == "git"
    assert first[(vault / "projects" / "b" / "memos" / "2026-01-10-x.md").resolve()] == date(2026, 9, 28)


def test_deleted_then_readded_memo_arrives_on_its_readd(tmp_path: Path) -> None:
    # newest add wins: the file on disk today is the one re-added on 09-25.
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "add", when="2026-09-01")
    _git(vault, "rm", "-q", "projects/p/memos/2026-03-01-x.md"); _git(vault, "commit", "-q", "-m", "rm", when="2026-09-10")
    _write(vault / "projects" / "p" / "memos" / "2026-03-01-x.md", "x again")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "readd", when="2026-09-25")
    assert _rows(audit_condense(vault))["p"]["arrived_memos"] == ["2026-03-01-x.md"]


def test_add_on_the_stamp_day_is_not_arrived(tmp_path: Path) -> None:
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "same day", when="2026-09-20")
    assert audit_condense(vault)["projects"] == []


def test_repo_that_tracks_no_memos_reports_no_arrivals(tmp_path: Path) -> None:
    # projects/ gitignored (or the vault nested in an unrelated repo): no evidence,
    # not "everything arrived today".
    vault = _git_vault(tmp_path)
    _write(vault / ".gitignore", "projects/\n")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "ignore", when="2026-09-01")
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    r = _rows(audit_condense(vault)).get("p")
    assert r is None or r["arrived"] is None


def test_quote_and_tab_in_memo_name_are_matched(tmp_path: Path) -> None:
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 2",
             {'2026-03-01-say-"hi"\tnow.md': "x", "2026-03-02-plain.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    report = audit_condense(vault)
    assert report["arrivals_basis"] == "git" and report["projects"] == []


def test_edit_after_stamp_is_not_an_arrival(tmp_path: Path) -> None:
    # Pins --diff-filter=A: a modification after the stamp must not read as an add.
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "add", when="2026-09-01")
    _write(vault / "projects" / "p" / "memos" / "2026-03-01-x.md", "x edited")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "edit", when="2026-09-25")
    assert audit_condense(vault)["projects"] == []


def test_moved_in_before_stamp_is_dated_at_move_and_not_arrived(tmp_path: Path) -> None:
    # Codex round 1: with rename detection on, a moved-in identical memo is an R,
    # not an A → missing from the dict → "today" → arrived forever.
    vault = _git_vault(tmp_path)
    _project(vault, "a", "condensed: 2026-09-20\nmemos_digested: 0", {"2026-01-10-x.md": "same bytes"})
    _project(vault, "b", "condensed: 2026-09-20\nmemos_digested: 1", {})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-01")
    (vault / "projects" / "b" / "memos").mkdir(parents=True, exist_ok=True)
    _git(vault, "mv", "projects/a/memos/2026-01-10-x.md", "projects/b/memos/2026-01-10-x.md")
    _git(vault, "commit", "-q", "-m", "move before stamp", when="2026-09-05")
    first, _ = _git_add_dates(vault)
    assert first[(vault / "projects" / "b" / "memos" / "2026-01-10-x.md").resolve()] == date(2026, 9, 5)
    assert audit_condense(vault)["projects"] == []


def test_symlinked_vault_path_still_matches_git_paths(tmp_path: Path) -> None:
    # Pins the parent-resolve in _memo_key: git reports real paths, the vault may
    # be reached through a symlink (iCloud Documents on macOS).
    real = _git_vault(tmp_path / "real")
    _project(real, "p", "condensed: 2026-09-20\nmemos_digested: 2",
             {"2026-03-01-x.md": "x", "2026-03-02-y.md": "y"})
    _git(real, "add", "-A"); _git(real, "commit", "-q", "-m", "add", when="2026-09-01")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    report = audit_condense(link)
    assert report["arrivals_basis"] == "git" and report["projects"] == []


def test_arrivals_basis_names_why_arr_is_unavailable(tmp_path: Path) -> None:
    _project(tmp_path, "p", "condensed: 2026-09-08\nmemos_digested: 1", {"2026-09-10-after.md": "x"})
    assert audit_condense(tmp_path)["arrivals_basis"].startswith("not a git checkout")
    vault = _git_vault(tmp_path / "v")
    _write(vault / ".gitignore", "projects/\n")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "ignore", when="2026-09-01")
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    assert audit_condense(vault)["arrivals_basis"] == "git tracks none of the memos"


def test_gitignored_project_beside_a_tracked_one_reports_no_arrivals(tmp_path: Path) -> None:
    # Kimi round 2: the guard must be per project — q has no add history of its own.
    vault = _git_vault(tmp_path)
    _write(vault / ".gitignore", "projects/q/\n")
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "p only", when="2026-09-01")
    _project(vault, "q", "condensed: 2026-09-20\nmemos_digested: 2", {"2026-03-01-a.md": "x", "2026-03-02-b.md": "x"})
    rows = _rows(audit_condense(vault))
    assert "q" not in rows or rows["q"]["arrived"] is None


def test_shallow_clone_reports_reason_and_no_arrivals(tmp_path: Path) -> None:
    src = _git_vault(tmp_path / "src")
    _project(src, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "a", when="2026-09-10")
    _write(src / "README.md", "r"); _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "b", when="2026-09-28")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}", str(clone)], check=True, capture_output=True)
    report = audit_condense(clone)
    assert report["arrivals_basis"].startswith("shallow") and report["projects"] == []


def test_committer_date_is_the_arrival_date(tmp_path: Path) -> None:
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-09-01T12:00:00", GIT_COMMITTER_DATE="2026-09-25T12:00:00",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-c", "commit.gpgsign=false", "add", "-A"], cwd=vault, env=env, check=True, capture_output=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "cherry"], cwd=vault, env=env, check=True, capture_output=True)
    first, _ = _git_add_dates(vault)
    assert first[(vault / "projects" / "p" / "memos" / "2026-03-01-x.md").resolve()] == date(2026, 9, 25)


def test_arrived_printout_is_capped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from memex.scripts.curation_audit import _print_condense
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 7", {})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    for i in range(7):
        _write(vault / "projects" / "p" / "memos" / f"2026-03-0{i + 1}-m{i}.md", "x")
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "seven in", when="2026-09-25")
    _print_condense(audit_condense(vault))
    out = capsys.readouterr().out
    assert out.count("↳ arrived:") == 5 and "+2 more (--json)" in out


def test_all_clear_line_names_why_arrivals_are_unavailable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from memex.scripts.curation_audit import _print_condense
    _project(tmp_path, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _print_condense(audit_condense(tmp_path))
    out = capsys.readouterr().out
    assert "current with its memos" in out and "arrivals not checked: not a git checkout" in out


def test_fresh_git_init_without_commits_gives_a_reason(tmp_path: Path) -> None:
    vault = _git_vault(tmp_path)
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    report = audit_condense(vault)
    assert report["projects"] == []
    assert report["arrivals_basis"].startswith("git log failed") and "commits yet" in report["arrivals_basis"]


def test_git_dir_in_environment_does_not_redirect_the_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _git_vault(tmp_path / "other")
    _write(other / "README.md", "r"); _git(other, "add", "-A"); _git(other, "commit", "-q", "-m", "o", when="2026-09-01")
    vault = _git_vault(tmp_path / "vault")
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 1", {"2026-03-01-x.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "v", when="2026-09-25")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    r = _rows(audit_condense(vault))["p"]
    assert r["arrived_memos"] == ["2026-03-01-x.md"]


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="only macOS distinguishes NFD/NFC on disk")
def test_nfd_memo_name_on_disk_matches_git_nfc(tmp_path: Path) -> None:
    import unicodedata
    vault = _git_vault(tmp_path)
    nfd = unicodedata.normalize("NFD", "2026-03-01-café.md")
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 2", {nfd: "x", "2026-03-02-plain.md": "x"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    report = audit_condense(vault)
    assert report["arrivals_basis"] == "git" and report["projects"] == []


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="only macOS distinguishes NFD/NFC on disk")
def test_nfd_ancestor_directory_keeps_arrivals_on(tmp_path: Path) -> None:
    # git's --show-toplevel comes back as the directory is named (NFD); keys and
    # lookups must normalise the same way or every memo misses → "tracks none".
    import unicodedata
    vault = _git_vault(tmp_path / unicodedata.normalize("NFD", "vaulté"))
    _project(vault, "p", "condensed: 2026-09-20\nmemos_digested: 2", {"2026-03-01-x.md": "x", "2026-03-02-y.md": "y"})
    _git(vault, "add", "-A"); _git(vault, "commit", "-q", "-m", "base", when="2026-09-10")
    report = audit_condense(vault)
    assert report["arrivals_basis"] == "git" and report["projects"] == []
