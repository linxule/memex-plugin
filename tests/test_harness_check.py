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
    assert all(r["source"] != "automation" for r in rows)  # a bare automation never lists
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


AUTOMATION_ONLY = "019a5555-0000-7000-8000-00000000000c"
AUTOMATION_FOLLOWUP = "019a6666-0000-7000-8000-00000000000d"


def test_an_automation_thread_a_person_continued_is_work(tmp_path):
    """An 18-turn design discussion in a daily monitor's thread was skipped by
    the check as an automation (2026-10-28). A person's turn makes it work;
    an automation that ran alone stays out."""
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    report = harness_gaps(vault, since="2000-01-01", root=_root(tmp_path))
    listed = {r["session_id"]: r for r in report["threads_without_memo"]}
    assert AUTOMATION_FOLLOWUP in listed
    assert listed[AUTOMATION_FOLLOWUP]["source"] == "automation, continued by a person"
    assert AUTOMATION_ONLY not in listed
    assert report["automation_skipped"] >= 1
    assert report["complete"]


def test_an_automation_thread_that_cannot_be_read_makes_the_report_incomplete(tmp_path, monkeypatch):
    """Whether a person joined is unknowable then; leaving it out would read as all-clear."""
    from memex.harness.codex_desktop import CodexDesktopAdapter

    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    real = CodexDesktopAdapter.load_explained

    def flaky(self, ref):
        if ref.session_id == AUTOMATION_FOLLOWUP:
            return None, "unreadable"
        return real(self, ref)

    monkeypatch.setattr(CodexDesktopAdapter, "load_explained", flaky)
    report = harness_gaps(vault, since="2000-01-01", root=_root(tmp_path))
    assert AUTOMATION_FOLLOWUP not in {r["session_id"] for r in report["threads_without_memo"]}
    assert report["unjudged_automation_threads"] == 1
    assert report["unreadable_files"] == {}  # threads and files are counted apart (Kimi round 2)
    assert not report["complete"]


def test_a_stamped_automation_thread_is_covered_without_reading_it(tmp_path, monkeypatch):
    """Codex round 2: the stamp is known from the vault, so the rollout is never read."""
    from memex.harness.codex_desktop import CodexDesktopAdapter

    vault = tmp_path / "vault"
    memo = vault / "projects" / "p" / "memos" / "2026-10-22-own.md"
    memo.parent.mkdir(parents=True)
    memo.write_text(f"---\ntype: memo\nharness: codex-desktop\nsession_id: {AUTOMATION_FOLLOWUP}\n---\n\n# own\n")
    loaded = []
    real = CodexDesktopAdapter.load_explained
    monkeypatch.setattr(CodexDesktopAdapter, "load_explained",
                        lambda self, ref: loaded.append(ref.session_id) or real(self, ref))
    report = harness_gaps(vault, since="2000-01-01", root=_root(tmp_path))
    assert AUTOMATION_FOLLOWUP not in loaded
    assert report["covered"] == 1
