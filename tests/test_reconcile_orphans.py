"""Tests for orphan pending-memo reconciliation matching logic.

A PreCompact signal persists until cleared; if a Layer-1 memo already exists
for the session, the signal is stale. `_covering_memo` is the heuristic that
decides "covered" (same project, memo dated within ±window days). These tests
pin the date parsing + the window matching that drives `--apply`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import json

import memex.scripts.reconcile_orphans as ro
from memex.scripts.reconcile_orphans import (
    _covering_memo,
    _date_from_name,
    _memo_date,
    _signal_date,
    main,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_date_from_name_handles_dashed_and_compact() -> None:
    assert _date_from_name("2026-06-09-foo.md") == date(2026, 6, 9)
    assert _date_from_name("20260609-foo.md") == date(2026, 6, 9)
    assert _date_from_name("no-date-here.md") is None
    assert _date_from_name("2026-13-40-bad.md") is None  # invalid month/day


def test_memo_date_prefers_frontmatter_then_filename(tmp_path: Path) -> None:
    fm = tmp_path / "2026-01-01-x.md"
    _write(fm, "---\ntitle: X\ndate: 2026-06-09\n---\nbody\n")
    assert _memo_date(fm) == date(2026, 6, 9)  # frontmatter wins over filename
    nofm = tmp_path / "2026-05-31-y.md"
    _write(nofm, "# no frontmatter\n")
    assert _memo_date(nofm) == date(2026, 5, 31)  # falls back to filename


def test_signal_date_parses_iso_with_microseconds() -> None:
    assert _signal_date("2026-05-31T12:24:30.716567") == date(2026, 5, 31)
    assert _signal_date("2026-05-31") == date(2026, 5, 31)
    assert _signal_date("") is None


def test_covering_memo_matches_within_window(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "demo" / "memos"
    _write(proj / "2026-05-30-thing.md", "---\ndate: 2026-05-30\n---\n")

    # signal 1 day off, window 2 → covered
    assert _covering_memo(tmp_path, "demo", date(2026, 5, 31), window=2) == "2026-05-30-thing.md"
    # signal 5 days off, window 2 → not covered
    assert _covering_memo(tmp_path, "demo", date(2026, 6, 5), window=2) is None
    # wrong project → not covered
    assert _covering_memo(tmp_path, "other", date(2026, 5, 31), window=2) is None
    # missing project dir → not covered (no crash)
    assert _covering_memo(tmp_path, "ghost", date(2026, 5, 31), window=2) is None
    # no signal date → not covered
    assert _covering_memo(tmp_path, "demo", None, window=2) is None


def test_covering_memo_picks_closest(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "demo" / "memos"
    _write(proj / "2026-05-28-far.md", "---\ndate: 2026-05-28\n---\n")
    _write(proj / "2026-05-31-near.md", "---\ndate: 2026-05-31\n---\n")
    assert _covering_memo(tmp_path, "demo", date(2026, 6, 1), window=5) == "2026-05-31-near.md"


def _setup_main(tmp_path: Path, monkeypatch, argv: list[str]) -> Path:
    """Wire main() to a temp vault + temp pending dir; return the pending dir."""
    vault = tmp_path / "vault"
    pending = tmp_path / "pending"
    pending.mkdir(parents=True)
    # a covering memo for project 'demo' dated 2026-05-31
    _write(vault / "projects" / "demo" / "memos" / "2026-05-31-thing.md", "---\ndate: 2026-05-31\n---\n")
    monkeypatch.setattr(ro, "get_memex_path", lambda: vault)
    monkeypatch.setattr(ro, "get_pending_dir", lambda: pending)
    monkeypatch.setattr("sys.argv", ["reconcile_orphans"] + argv)
    return pending


def test_main_apply_deletes_only_covered(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    (pending / "covered.json").write_text(
        json.dumps({"session_id": "a", "project": "demo", "timestamp": "2026-05-31T10:00:00.0"})
    )
    (pending / "retry.json").write_text(
        json.dumps({"session_id": "b", "project": "ghost", "timestamp": "2026-05-31T10:00:00.0"})
    )
    main()
    assert not (pending / "covered.json").exists()  # covered → cleared
    assert (pending / "retry.json").exists()  # genuine retry → kept


def test_main_dry_run_deletes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, [])  # no --apply
    (pending / "covered.json").write_text(
        json.dumps({"session_id": "a", "project": "demo", "timestamp": "2026-05-31T10:00:00.0"})
    )
    main()
    assert (pending / "covered.json").exists()  # dry-run leaves everything


def test_main_tolerates_malformed_signals(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    (pending / "bad-json.json").write_text("{not json")
    (pending / "not-object.json").write_text("[]")  # valid JSON, not a dict (L2)
    (pending / "numeric-ts.json").write_text(
        json.dumps({"session_id": "c", "project": "demo", "timestamp": 12345})  # non-str ts (L1)
    )
    main()  # must not raise
    # malformed files are left in place (not treated as covered)
    assert (pending / "bad-json.json").exists()
    assert (pending / "not-object.json").exists()
