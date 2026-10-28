"""memex check --harnesses: Codex threads without their own memo (v0.22.0)."""

from __future__ import annotations

import shutil
from pathlib import Path

from memex.scripts.harness_check import harness_gaps

FIXTURES = Path(__file__).parent / "fixtures" / "codex_desktop"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "codex_sessions"
    shutil.copytree(FIXTURES, root)
    return root


def test_unmemoed_threads_are_listed_and_a_stamp_covers_one(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    root = _root(tmp_path)
    before = harness_gaps(vault, since="2000-01-01", root=root)
    rows = before["threads_without_memo"]
    assert rows, "the fixtures hold top-level Desktop threads"
    assert all(r["source"] != "automation" for r in rows)
    assert before["covered"] == 0
    target = rows[0]["session_id"]
    memo = vault / "projects" / "p" / "memos" / "2026-10-22-own.md"
    memo.parent.mkdir(parents=True)
    memo.write_text(f"---\ntype: memo\nharness: codex-desktop\nsession_id: {target}\n---\n\n# own memo\n")
    after = harness_gaps(vault, since="2000-01-01", root=root)
    assert after["covered"] == 1
    assert target not in {r["session_id"] for r in after["threads_without_memo"]}
    assert len(after["threads_without_memo"]) == len(rows) - 1


def test_since_cuts_older_threads(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    report = harness_gaps(vault, since="2999-01-01", root=_root(tmp_path))
    assert report["threads_without_memo"] == [] and report["covered"] == 0


def test_unreadable_rollouts_make_the_report_incomplete(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    root = _root(tmp_path)
    bad = root / "sessions" / "2026" / "10" / "20" / "rollout-2026-10-20T10-00-00-broken.jsonl.zst"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not zstd at all")
    report = harness_gaps(vault, since="2000-01-01", root=root)
    assert report["complete"] is False and sum(report["unreadable_files"].values()) >= 1
    clean = harness_gaps(vault, since="2000-01-01", root=_root(tmp_path / "again"))
    assert clean["complete"] is True and clean["unreadable_files"] == {}


def test_an_unreadable_rollout_outside_the_window_does_not_make_it_incomplete(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    root = _root(tmp_path)
    tid = "019a0000-0000-7000-8000-00000000dead"
    old = root / "sessions" / "2020" / "01" / "02" / f"rollout-2020-01-02T10-00-00-{tid}.jsonl.zst"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"not zstd at all")
    assert harness_gaps(vault, since="2026-01-01", root=root)["complete"] is True
    late = harness_gaps(vault, since="2019-12-31", root=root)
    assert late["complete"] is False and sum(late["unreadable_files"].values()) == 1
