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
import os

import pytest

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
    # window-only coverage needs --trust-window since v0.20.0 (exact evidence clears without it)
    pending = _setup_main(tmp_path, monkeypatch, ["--apply", "--trust-window"])
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


# ── exact evidence (v0.20.0): session_id frontmatter, transcript Write ──

def _tool_result(use_id: str, is_error: bool | None = None) -> str:
    item = {"type": "tool_result", "tool_use_id": use_id, "content": "ok"}
    if is_error is not None:
        item["is_error"] = is_error
    return json.dumps({"type": "user", "message": {"role": "user", "content": [item]}})


def _write_call(path: str, use_id: str = "toolu_w", is_error: bool | None = None) -> str:
    """A Write tool_use plus its tool_result (successful unless is_error=True)."""
    use = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": use_id, "name": "Write", "input": {"file_path": path, "content": "x"}}]}})
    return use + "\n" + _tool_result(use_id, is_error)


def _bash_call(command: str, use_id: str = "toolu_b", is_error: bool | None = None) -> str:
    use = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": "Bash", "input": {"command": command}}]}})
    return use + "\n" + _tool_result(use_id, is_error)


def test_main_window_only_match_is_kept_without_trust(tmp_path: Path, monkeypatch, capsys) -> None:
    # same-project memo within the window, but nothing ties it to THIS session:
    # with several sessions per project per day it may be another session's memo
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    (pending / "near.json").write_text(
        json.dumps({"session_id": "a", "project": "demo", "timestamp": "2026-05-31T10:00:00.0"})
    )
    main()
    assert (pending / "near.json").exists()
    assert "likely" in capsys.readouterr().out


def test_main_trust_window_clears_window_match(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply", "--trust-window"])
    (pending / "near.json").write_text(
        json.dumps({"session_id": "a", "project": "demo", "timestamp": "2026-05-31T10:00:00.0"})
    )
    main()
    assert not (pending / "near.json").exists()


def test_main_session_id_frontmatter_is_exact(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "elsewhere" / "memos" / "2026-01-01-m.md",
           "---\ntitle: M\nsession_id: 0123abcd-0000-0000-0000-000000000000\n---\n")
    (pending / "s.json").write_text(json.dumps({
        "session_id": "0123abcd-0000-0000-0000-000000000000", "project": "ghost",
        "timestamp": "2026-09-01T10:00:00.0"}))
    main()
    assert not (pending / "s.json").exists()  # exact, even though project/date don't match


def test_transcript_write_is_likely_and_survives_folder_move(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    # the session wrote the memo under 'materials'; it was later consolidated into
    # 'teaching', leaving a redirect stub — the stub is what makes the move provable
    _write(vault / "projects" / "teaching" / "memos" / "2026-09-10-deck-final.md", "---\ntitle: D\n---\n")
    _write(vault / "projects" / "materials" / "_project.md",
           "---\ntype: project\nname: materials\nstatus: archived\nredirect_to: teaching\n---\n")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "save it"}}),
        _write_call(str(vault / "projects" / "materials" / "memos" / "2026-09-10-deck-final.md")),
    ]) + "\n")
    (pending / "t.json").write_text(json.dumps({
        "session_id": "b", "project": "materials", "timestamp": "2026-09-10T10:00:00.0",
        "transcript_path": str(transcript)}))
    main()
    # a transcript Write is supporting evidence ("likely"), never an auto-clear
    out = capsys.readouterr().out
    assert (pending / "t.json").exists()
    assert "likely" in out and "transcript Write" in out and "projects/teaching/memos/2026-09-10-deck-final.md" in out


def test_transcript_read_of_a_memo_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-01-01-old.md", "---\ntitle: O\n---\n")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": str(vault / "projects/p/memos/2026-01-01-old.md")}}]}}) + "\n")
    (pending / "t.json").write_text(json.dumps({
        "session_id": "c", "project": "p", "timestamp": "2026-09-10T10:00:00.0",
        "transcript_path": str(transcript)}))
    main()
    assert (pending / "t.json").exists()  # reading an old memo proves nothing


def test_missing_transcript_falls_back_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    (pending / "t.json").write_text(json.dumps({
        "session_id": "d", "project": "ghost", "timestamp": "2026-09-10T10:00:00.0",
        "transcript_path": str(tmp_path / "gone.jsonl")}))
    main()
    assert (pending / "t.json").exists()


def test_subagent_transcript_write_is_likely_until_trusted(tmp_path: Path, monkeypatch, capsys) -> None:
    # the memo writer usually runs as a subagent: <session>.jsonl + <session>/subagents/*.jsonl
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-part2.md", "---\ntitle: P\n---\n")
    main_jsonl = tmp_path / "sess.jsonl"
    main_jsonl.write_text(json.dumps({"type": "user", "message": {"content": "save"}}) + "\n")
    sub = tmp_path / "sess" / "subagents" / "agent-memo.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-part2.md")) + "\n")
    (pending / "s.json").write_text(json.dumps({
        "session_id": "e", "project": "p", "timestamp": "2026-09-20T10:00:00.0",
        "transcript_path": str(main_jsonl)}))
    main()
    out = capsys.readouterr().out
    assert (pending / "s.json").exists() and "transcript Write" in out
    monkeypatch.setattr("sys.argv", ["reconcile_orphans", "--apply", "--trust-window"])
    main()
    assert not (pending / "s.json").exists()  # cleared only when the human trusts it


def test_backfill_obs_is_not_exact_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    # memo drafted in a scratchpad, copied in with Bash: the obs step names it, but
    # Bash text is never exact evidence (round 3) — a same-day memo makes it "likely"
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "llm-world" / "memos" / "2026-09-20-part2.md", "---\ntitle: P\n---\n")
    main_jsonl = tmp_path / "sess.jsonl"
    main_jsonl.write_text(_bash_call(
        'cd vault && memex backfill obs --stdin --replace --doc-path "projects/llm-world/memos/2026-09-20-part2.md" <<EOF') + "\n")
    (pending / "s.json").write_text(json.dumps({
        "session_id": "f", "project": "llm-world", "timestamp": "2026-09-20T10:00:00.0",
        "transcript_path": str(main_jsonl)}))
    main()
    assert (pending / "s.json").exists()
    assert "likely" in capsys.readouterr().out


def test_other_projects_memo_writes_are_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    # a curator session in the vault (project 'memex') wrote an orphan memo for
    # 'aha-remote' via a subagent — that does not mean the curator session was saved
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "aha-remote" / "memos" / "2026-09-18-x.md", "---\ntitle: X\n---\n")
    main_jsonl = tmp_path / "curator.jsonl"
    main_jsonl.write_text(_write_call(str(vault / "projects/aha-remote/memos/2026-09-18-x.md")) + "\n")
    (pending / "c.json").write_text(json.dumps({
        "session_id": "g", "project": "memex", "timestamp": "2026-09-23T10:00:00.0",
        "transcript_path": str(main_jsonl)}))
    main()
    assert (pending / "c.json").exists()


# ── v0.20.0 review round 1 (Codex): evidence must be real, successful, in-vault ──

def _sig(pending: Path, transcript: Path, project: str = "p", sid: str = "s") -> Path:
    f = pending / "s.json"
    f.write_text(json.dumps({"session_id": sid, "project": project,
                             "timestamp": "2026-09-20T10:00:00.0", "transcript_path": str(transcript)}))
    return f


def test_quoted_backfill_command_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    _write(tmp_path / "vault" / "projects" / "p" / "memos" / "old.md", "---\ntitle: O\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_bash_call('echo "memex backfill obs --doc-path projects/p/memos/old.md"') + "\n"
                 + _bash_call('grep -n "memex backfill obs --doc-path projects/p/memos/old.md" log', "toolu_g") + "\n")
    assert _sig(pending, t).exists() and (main() or True) and (pending / "s.json").exists()


def test_env_prefixed_backfill_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    _write(tmp_path / "vault" / "projects" / "p" / "memos" / "new.md", "---\ntitle: N\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_bash_call('cd /v && GEMINI_API_KEY=x memex backfill obs --stdin --replace '
                            '--doc-path "projects/p/memos/new.md" <<\'EOF\'') + "\n")
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


def test_failed_or_unanswered_write_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "old.md", "---\ntitle: O\n---\n")
    t = tmp_path / "t.jsonl"
    denied = _write_call(str(vault / "projects/p/memos/old.md"), "w1", is_error=True)
    unanswered = _write_call(str(vault / "projects/p/memos/old.md"), "w2").split("\n")[0]
    t.write_text(denied + "\n" + unanswered + "\n")
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


def test_same_basename_in_unrelated_project_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "other" / "memos" / "old.md", "---\ntitle: O\n---\n")
    _write(vault / "projects" / "p" / "_project.md", "---\ntype: project\nname: p\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/old.md")) + "\n")  # file since deleted
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


def test_write_outside_the_vault_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    _write(tmp_path / "vault" / "projects" / "p" / "memos" / "x.md", "---\ntitle: X\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(tmp_path / "scratch" / "projects/p/memos/x.md")) + "\n")
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


def test_session_id_must_be_top_level_and_complete(tmp_path: Path, monkeypatch, capsys) -> None:
    from memex.scripts.reconcile_orphans import _memo_session_map
    vault = tmp_path / "vault"
    uuid = "11111111-2222-3333-4444-555555555555"
    _write(vault / "projects" / "q" / "memos" / "block.md",
           f"---\ntitle: a\ndescription: |\n  session_id: {uuid}\n---\n")
    _write(vault / "projects" / "q" / "memos" / "prefix.md", f"---\ntitle: a\nsession_id: {uuid}xyz\n---\n")
    assert _memo_session_map(vault) == {}
    _write(vault / "projects" / "q" / "memos" / "ok.md", f"---\ntitle: a\nsession_id: \"{uuid}\"  # stamped\n---\n")
    assert _memo_session_map(vault) == {uuid: "projects/q/memos/ok.md"}


def test_signal_without_project_gets_no_transcript_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    # (Kimi review) no project → no own-project guard → don't trust transcript writes
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "other" / "memos" / "x.md", "---\ntitle: X\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/other/memos/x.md")) + "\n")
    _sig(pending, t, project="")
    main()
    assert (pending / "s.json").exists()


def test_string_is_error_counts_as_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "x.md", "---\ntitle: X\n---\n")
    use = json.dumps({"message": {"content": [{"type": "tool_use", "id": "w9", "name": "Write",
                                               "input": {"file_path": str(vault / "projects/p/memos/x.md")}}]}})
    res = json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "w9", "is_error": "true"}]}})
    t = tmp_path / "t.jsonl"
    t.write_text(res + "\n" + use + "\n")  # result-before-use ordering is also tolerated
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


# ── round 3 (Codex): Bash is never evidence — every shape below once passed a parser ──

@pytest.mark.parametrize("cmd", [
    'memex backfill obs --stdin --replace --doc-path "projects/p/memos/a.md" <<\'EOF\'\n[]\nEOF',
    "cd /v && memex backfill obs --doc-path projects/p/memos/a.md",
    'bash -c "memex backfill obs --doc-path projects/p/memos/a.md"',
    "echo ';' memex backfill obs --doc-path projects/p/memos/a.md",
    "true ||\nmemex backfill obs --doc-path projects/p/memos/a.md",
    "true || (memex backfill obs --doc-path projects/p/memos/a.md)",
    "cat <<'EOF'\n EOF\nmemex backfill obs --doc-path projects/p/memos/a.md\nEOF",
    'bash -n -c "memex backfill obs --doc-path projects/p/memos/a.md"',
    "cp /scratch/a.md /v/projects/p/memos/a.md",
])
def test_no_bash_shape_is_evidence(tmp_path: Path, cmd: str) -> None:
    from memex.scripts.reconcile_orphans import _memos_written_by
    t = tmp_path / "t.jsonl"
    t.write_text(_bash_call(cmd) + "\n")
    assert _memos_written_by(t, tmp_path) == []


def test_write_that_rewrote_an_old_memo_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    # (Kimi round 2) touching an old memo is not saving this session
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-01-05-old.md", "---\ntitle: O\ndate: 2026-01-05\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-01-05-old.md")) + "\n")
    _sig(pending, t)  # signal dated 2026-09-20
    main()
    assert (pending / "s.json").exists()


def test_redirect_read_only_from_frontmatter(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _redirect_target
    _write(tmp_path / "projects" / "a" / "_project.md", "# A\n\n```\nredirect_to: q\n```\n")
    _write(tmp_path / "projects" / "b" / "_project.md", "---\nname: b\n---\nredirect_to: q\n")
    _write(tmp_path / "projects" / "c" / "_project.md", "---\nname: c\nredirect_to: teaching\n---\n")
    assert _redirect_target(tmp_path, "a") is None
    assert _redirect_target(tmp_path, "b") is None
    assert _redirect_target(tmp_path, "c") == "teaching"


# ── round 4 (Codex) ──────────────────────────────────────────────────

def test_rewriting_another_sessions_stamped_memo_is_not_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    other = "bbbbbbbb-0000-0000-0000-000000000000"
    _write(vault / "projects" / "p" / "memos" / "2026-09-19-theirs.md",
           f"---\ntitle: T\nsession_id: {other}\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-19-theirs.md")) + "\n")  # typo fix
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()
    assert (pending / "s.json").exists()


@pytest.mark.parametrize("name,fm_date", [
    ("2026-01-05-old.md", "2026-09-20"),   # old filename, fresh frontmatter
    ("2026-09-20-new.md", "2026-01-05"),   # fresh filename, old frontmatter
])
def test_every_known_date_must_be_near_the_signal(tmp_path: Path, monkeypatch, capsys, name, fm_date) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / name, f"---\ntitle: X\ndate: {fm_date}\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos" / name)) + "\n")
    _sig(pending, t)  # 2026-09-20
    main()
    assert (pending / "s.json").exists()


def test_four_redirect_hops_are_followed(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    chain = ["p", "q", "r", "s", "u"]
    for a, b in zip(chain, chain[1:]):
        _write(vault / "projects" / a / "_project.md", f"---\nname: {a}\nredirect_to: {b}\n---\n")
    _write(vault / "projects" / "u" / "memos" / "2026-09-20-moved.md", "---\ntitle: M\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-moved.md")) + "\n")
    _sig(pending, t)
    main()
    out = capsys.readouterr().out
    assert (pending / "s.json").exists() and "projects/u/memos/2026-09-20-moved.md" in out


# ── round 5 (Codex) + Kimi round 4 ───────────────────────────────────

def _long_fm(extra: str, filler: int = 60) -> str:
    return "---\ntitle: T\n" + "".join(f"k{i}: v\n" for i in range(filler)) + extra + "---\n"


def test_late_foreign_stamp_still_blocks_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-x.md",
           _long_fm("session_id: bbbbbbbb-0000-0000-0000-000000000000\n"))
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-x.md")) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()
    assert (pending / "s.json").exists()


@pytest.mark.parametrize("fm", [_long_fm('date: "2026-01-05"\n', 0), _long_fm("date: 2026-01-05\n")])
def test_quoted_or_late_old_date_blocks_evidence(tmp_path: Path, monkeypatch, capsys, fm: str) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-x.md", fm)
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-x.md")) + "\n")
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


def test_unclosed_frontmatter_is_never_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-x.md", "---\ntitle: T\nbody without close\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-x.md")) + "\n")
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


def test_late_own_session_stamp_is_found(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _memo_session_map
    uuid = "cccccccc-1111-2222-3333-444444444444"
    _write(tmp_path / "projects" / "p" / "memos" / "m.md", _long_fm(f"session_id: {uuid}\n"))
    assert _memo_session_map(tmp_path) == {uuid: "projects/p/memos/m.md"}


def test_ids_must_be_present_to_pair(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _memos_written_by
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "x.md", "---\ntitle: X\n---\n")
    use = json.dumps({"message": {"content": [{"type": "tool_use", "name": "Write",
                                               "input": {"file_path": str(vault / "projects/p/memos/x.md")}}]}})
    res = json.dumps({"message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    t = tmp_path / "t.jsonl"
    t.write_text(use + "\n" + res + "\n")
    assert _memos_written_by(t, vault) == []


def test_trust_window_dry_run_keeps_classification(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--trust-window", "--json"])
    (pending / "near.json").write_text(
        json.dumps({"session_id": "a", "project": "demo", "timestamp": "2026-05-31T10:00:00.0"}))
    main()
    out = json.loads(capsys.readouterr().out)
    assert out["covered"] == [] and len(out["likely"]) == 1 and out["trust_window"] is True
    assert (pending / "near.json").exists()


def test_failed_delete_is_reported(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply", "--trust-window"])
    sig = pending / "near.json"
    sig.write_text(json.dumps({"session_id": "a", "project": "demo", "timestamp": "2026-05-31T10:00:00.0"}))
    real_unlink = Path.unlink

    def boom(self, *a, **k):
        if self == sig:
            raise PermissionError("read-only")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", boom)
    main()
    out = capsys.readouterr().out
    assert "FAILED" in out and "read-only" in out


# ── round 6 (Codex): an indented `---` is block-scalar content, not a delimiter ──

@pytest.mark.parametrize("field", ["session_id: bbbbbbbb-0000-0000-0000-000000000000", "date: 2026-01-05"])
def test_indented_dashes_do_not_close_frontmatter(tmp_path: Path, monkeypatch, capsys, field: str) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-x.md",
           f"---\ntitle: T\ndescription: |\n  example\n  ---\n{field}\n---\nbody\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-x.md")) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()
    assert (pending / "s.json").exists()


# ── round 7 (Codex): frontmatter is read as YAML, not as lines ─────────

def test_session_id_line_inside_a_quoted_scalar_is_not_a_stamp(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    uuid = "aaaaaaaa-0000-0000-0000-000000000000"
    _write(tmp_path / "vault" / "projects" / "p" / "memos" / "m.md",
           f'---\ndescription: "example\nsession_id: {uuid}\n"\n---\n')
    (pending / "s.json").write_text(json.dumps(
        {"session_id": uuid, "project": "ghost", "timestamp": "2026-09-20T10:00:00.0"}))
    main()
    assert (pending / "s.json").exists()


@pytest.mark.parametrize("stamp", [
    "session_id: >-\n  bbbbbbbb-0000-0000-0000-000000000000",
    '"session_id": bbbbbbbb-0000-0000-0000-000000000000',
    "session_id: [bbbbbbbb-0000-0000-0000-000000000000]",  # not a single UUID → unusable
])
def test_yaml_valid_foreign_stamp_blocks_transcript_evidence(tmp_path: Path, monkeypatch, capsys, stamp: str) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-x.md", f"---\ntitle: T\n{stamp}\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-x.md")) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()
    assert (pending / "s.json").exists()


def test_block_scalar_stamp_counts_as_own_stamp(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _memo_session_map
    uuid = "dddddddd-1111-2222-3333-444444444444"
    _write(tmp_path / "projects" / "p" / "memos" / "m.md", f"---\ntitle: T\nsession_id: >-\n  {uuid}\n---\n")
    assert _memo_session_map(tmp_path) == {uuid: "projects/p/memos/m.md"}


def test_unparseable_frontmatter_is_untrusted(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-x.md", "---\ntitle: a: b: c\n  - broken\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(vault / "projects/p/memos/2026-09-20-x.md")) + "\n")
    _sig(pending, t)
    main()
    assert (pending / "s.json").exists()


# ── round 8 (Codex): every read/parse failure is untrusted ─────────────

def test_unreadable_memo_cannot_hide_a_foreign_stamp(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    memo = vault / "projects" / "p" / "memos" / "2026-09-20-x.md"
    _write(memo, "---\ntitle: T\nsession_id: bbbbbbbb-0000-0000-0000-000000000000\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(memo)) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    memo.chmod(0)  # genuinely unreadable: exercises the OSError path in _frontmatter
    try:
        from memex.scripts.reconcile_orphans import _UNTRUSTED, _frontmatter
        if os.geteuid() != 0:  # root can read mode-000 files
            assert _frontmatter(memo) is _UNTRUSTED
        main()
    finally:
        memo.chmod(0o644)
    assert (pending / "s.json").exists()


@pytest.mark.parametrize("fm", [
    "session_id: bbbbbbbb-0000-0000-0000-000000000000\nsession_id: aaaaaaaa-0000-0000-0000-000000000000",
    "deep: " + "[" * 1100 + "]" * 1100,
    "date: 2026-09-20T23:30:00+25:00",
    "big: " + "x" * 70_000,
])
def test_hostile_frontmatter_is_untrusted_not_a_crash(tmp_path: Path, monkeypatch, capsys, fm: str) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    memo = vault / "projects" / "p" / "memos" / "2026-09-20-x.md"
    _write(memo, f"---\ntitle: T\n{fm}\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(memo)) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()  # must not raise
    assert (pending / "s.json").exists()


def test_duplicate_keys_do_not_create_a_session_map_entry(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _memo_session_map
    _write(tmp_path / "projects" / "p" / "memos" / "m.md",
           "---\nsession_id: bbbbbbbb-0000-0000-0000-000000000000\n"
           "session_id: aaaaaaaa-0000-0000-0000-000000000000\n---\n")
    assert _memo_session_map(tmp_path) == {}


# ── round 9 (Codex): "no frontmatter" is never a pass ─────────────────

@pytest.mark.parametrize("prefix", ["﻿", "\n", "# heading\n"])
def test_memo_without_leading_frontmatter_is_never_evidence(tmp_path: Path, monkeypatch, capsys, prefix: str) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    memo = vault / "projects" / "p" / "memos" / "2026-09-20-x.md"
    _write(memo, prefix + "---\ntitle: T\nsession_id: bbbbbbbb-0000-0000-0000-000000000000\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(memo)) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()
    assert (pending / "s.json").exists()


def test_bom_prefixed_own_stamp_is_still_read(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _memo_session_map
    uuid = "eeeeeeee-1111-2222-3333-444444444444"
    _write(tmp_path / "projects" / "p" / "memos" / "m.md", f"﻿---\ntitle: T\nsession_id: {uuid}\n---\n")
    assert _memo_session_map(tmp_path) == {uuid: "projects/p/memos/m.md"}


# ── round 10 (Codex): only "\n" separates lines ────────────────────────

@pytest.mark.parametrize("sep", ["\v", "\f", "\x1c", "\x85", " ", " ", "\r"])
def test_exotic_line_separator_cannot_fake_a_delimiter(tmp_path: Path, monkeypatch, capsys, sep: str) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply"])
    vault = tmp_path / "vault"
    memo = vault / "projects" / "p" / "memos" / "2026-09-20-x.md"
    _write(memo, f"---\ntitle: T{sep}---\nsession_id: bbbbbbbb-0000-0000-0000-000000000000\n---\n")
    t = tmp_path / "t.jsonl"
    t.write_text(_write_call(str(memo)) + "\n")
    _sig(pending, t, sid="aaaaaaaa-0000-0000-0000-000000000000")
    main()
    assert (pending / "s.json").exists()


def test_crlf_frontmatter_still_parses(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _memo_session_map
    uuid = "ffffffff-1111-2222-3333-444444444444"
    _write(tmp_path / "projects" / "p" / "memos" / "m.md", f"---\r\ntitle: T\r\nsession_id: {uuid}\r\n---\r\nbody\r\n")
    assert _memo_session_map(tmp_path) == {uuid: "projects/p/memos/m.md"}


# ── round 11 (Codex): delimiters are exactly "---"; only stamps auto-clear ──

@pytest.mark.parametrize("delim", ["---\v", "--- ", "---\t", "\v---"])
def test_near_miss_delimiters_are_not_delimiters(tmp_path: Path, delim: str) -> None:
    from memex.scripts.reconcile_orphans import _UNTRUSTED, _frontmatter
    memo = tmp_path / "m.md"
    memo.write_text(f"---\ntitle: T\n{delim}\nsession_id: bbbbbbbb-0000-0000-0000-000000000000\n---\n")
    fm = _frontmatter(memo)
    assert fm is _UNTRUSTED or (isinstance(fm, dict) and "session_id" in fm)


def test_only_a_session_stamp_clears_without_trust(tmp_path: Path, monkeypatch, capsys) -> None:
    pending = _setup_main(tmp_path, monkeypatch, ["--apply", "--json"])
    vault = tmp_path / "vault"
    uuid = "12345678-1111-2222-3333-444444444444"
    _write(vault / "projects" / "p" / "memos" / "2026-09-20-own.md", f"---\ntitle: O\nsession_id: {uuid}\n---\n")
    (pending / "own.json").write_text(json.dumps(
        {"session_id": uuid, "project": "p", "timestamp": "2026-09-20T10:00:00.0"}))
    main()
    out = json.loads(capsys.readouterr().out)
    assert [r["evidence"] for r in out["covered"]] == ["session_id"]
    assert not (pending / "own.json").exists()


def test_json_lists_malformed_signals(tmp_path: Path, monkeypatch, capsys) -> None:
    # (Kimi round 5) malformed signal files must be visible to --json consumers
    pending = _setup_main(tmp_path, monkeypatch, ["--json"])
    (pending / "bad.json").write_text("{not json")
    main()
    out = json.loads(capsys.readouterr().out)
    assert [e["file"] for e in out["errors"]] == ["bad.json"]


def test_frontmatter_beyond_read_bound_is_untrusted(tmp_path: Path) -> None:
    from memex.scripts.reconcile_orphans import _MAX_FRONTMATTER_BYTES, _UNTRUSTED, _frontmatter
    memo = tmp_path / "m.md"
    memo.write_text("---\ntitle: T\n" + "# pad\n" * (_MAX_FRONTMATTER_BYTES // 3) + "---\n")
    assert _frontmatter(memo) is _UNTRUSTED
