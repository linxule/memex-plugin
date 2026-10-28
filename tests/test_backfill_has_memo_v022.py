"""v0.22.0 (plan item A2): ``backfill memos`` joins on memo frontmatter stamps.

All ids, titles and bodies are synthetic. A transcript flips ``has_memo: false``
→ true when a memo stamps its FULL session id (``session_id:`` or an entry of
``session_ids:``), in addition to the older filename-suffix and state.json
evidence. Only the frontmatter line changes; the body is byte-identical.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

import memex.scripts.backfill_has_memo as bhm

SID_A = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SID_B = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SID_C = "33333333-cccc-4ccc-8ccc-cccccccccccc"
SID_D = "44444444-dddd-4ddd-8ddd-dddddddddddd"
# Same first 8 hex as SID_A, different thread: the Codex prefix-collision shape.
SID_A_TWIN = "11111111-ffff-4fff-8fff-ffffffffffff"
SID_NONE = "99999999-9999-4999-8999-999999999999"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "projects" / "demo" / "memos").mkdir(parents=True)
    (vault / "projects" / "demo" / "transcripts").mkdir(parents=True)
    return vault


def _memo(vault: Path, name: str, frontmatter: str, body: str = "Body of the memo.\n") -> Path:
    path = vault / "projects" / "demo" / "memos" / name
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _transcript(
    vault: Path,
    name: str,
    sid: str | None,
    *,
    has_memo: str = "false",
    body: bytes = b"# Transcript\n\nhas_memo: false in the body must never be touched.\n",
    eol: bytes = b"\n",
    quote: str = "",
) -> Path:
    lines = [b"---", b"type: transcript", b"title: Synthetic"]
    if sid is not None:
        lines.append(f"session_id: {quote}{sid}{quote}".encode())
    lines += [b"messages: 3", f"has_memo: {has_memo}".encode(), b"---"]
    path = vault / "projects" / "demo" / "transcripts" / name
    path.write_bytes(eol.join(lines) + eol + body)
    return path


def _run(vault: Path, tmp_path: Path, **kw) -> dict:
    return bhm.backfill(vault, tmp_path / "state.json", **kw)


def _flag(path: Path) -> str:
    return "true" if b"\nhas_memo: true" in path.read_bytes().split(b"\n---")[0] else "false"


# --- the plan's acceptance: slug-named memo stamped with a session id flips ----------


def test_slug_named_memo_with_stamp_flips_its_transcript(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-20-reconstructed-slug-name.md", f"title: Slug\nsession_id: {SID_A}")
    stamped = _transcript(vault, "20261020-101500-aaaaaaaa.md", SID_A)
    other = _transcript(vault, "20261020-111500-bbbbbbbb.md", SID_NONE)
    before_other = other.read_bytes()
    before_body = stamped.read_bytes().split(b"\n---\n", 1)[1]

    result = _run(vault, tmp_path, apply=True)

    assert _flag(stamped) == "true"
    assert stamped.read_bytes().split(b"\n---\n", 1)[1] == before_body
    assert other.read_bytes() == before_other  # no memo → untouched
    assert result["updated"] == 1
    assert result["by_kind"] == {"stamp": 1, "session_ids": 0, "suffix": 0, "state": 0}
    assert result["unmatched"] == 1 and result["failed"] == 0


def test_session_ids_list_flips_every_thread_it_names(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-21-three-thread-memo.md", f"title: Multi\nsession_ids: [{SID_A}, {SID_B}]")
    _memo(vault, "2026-10-22-block-list-memo.md", f"title: Block\nsession_ids:\n  - {SID_C}\n  - '{SID_D}'")
    flips = [
        _transcript(vault, f"2026102{i}-120000-{sid[:8]}.md", sid)
        for i, sid in enumerate((SID_A, SID_B, SID_C, SID_D), start=1)
    ]

    result = _run(vault, tmp_path, apply=True)

    assert [_flag(p) for p in flips] == ["true"] * 4
    assert result["by_kind"]["session_ids"] == 4 and result["by_kind"]["stamp"] == 0


def test_prefix_twin_is_not_flipped_by_a_stamped_memo(tmp_path):
    """A memo stamping SID_A must not cover a thread that only shares its 8 hex."""
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-21-slug.md", f"title: T\nsession_id: {SID_A}")
    twin = _transcript(vault, "20261021-130000-11111111.md", SID_A_TWIN)  # same 8 hex in its filename
    before = twin.read_bytes()

    result = _run(vault, tmp_path, apply=True)

    assert twin.read_bytes() == before
    assert result["updated"] == 0 and result["unmatched"] == 1


def test_filename_suffix_collision_with_a_different_full_id_is_a_conflict(tmp_path):
    """The real Codex shape: memo and transcript filenames share the 8 hex, the stamped ids differ."""
    vault = _vault(tmp_path)
    _memo(vault, "20261021-100000-11111111.md", f"title: T\nsession_id: {SID_A}")
    owner = _transcript(vault, "20261021-100000-11111111.md", SID_A)
    twin = _transcript(vault, "20261021-130000-11111111.md", SID_A_TWIN)
    twin_before = twin.read_bytes()

    result = _run(vault, tmp_path, apply=True)

    assert _flag(owner) == "true" and result["by_kind"]["stamp"] == 1  # the stamp, not the weaker suffix
    assert twin.read_bytes() == twin_before
    assert result["conflicts"] == 1 and result["unmatched"] == 0 and result["updated"] == 1


def test_a_memo_without_stamps_cannot_contradict_a_suffix_match(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "20261021-100000-11111111.md", "title: Unstamped")
    _memo(vault, "20261021-110000-22222222.md", f"title: Stamped\nsession_id: {SID_B}")
    covered = _transcript(vault, "20261021-100000-11111111.md", SID_A)

    result = _run(vault, tmp_path, apply=True)

    assert _flag(covered) == "true" and result["by_kind"]["suffix"] == 1 and result["conflicts"] == 0


def test_two_memos_behind_one_suffix_one_unstamped_keeps_the_match(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "20261021-100000-11111111.md", f"title: Stamped\nsession_id: {SID_B}")
    other_dir = vault / "projects" / "second" / "memos"
    other_dir.mkdir(parents=True)
    (other_dir / "20261021-120000-11111111.md").write_text("---\ntitle: Unstamped\n---\nbody\n")
    covered = _transcript(vault, "20261021-100000-11111111.md", SID_A)

    result = _run(vault, tmp_path, apply=True)

    assert _flag(covered) == "true" and result["by_kind"]["suffix"] == 1 and result["conflicts"] == 0


def test_state_json_prefix_with_a_different_full_id_is_a_conflict(tmp_path):
    vault = _vault(tmp_path)
    agreeing = _transcript(vault, "20261021-100000-11111111.md", SID_A)
    twin = _transcript(vault, "20261021-130000-11111111.md", SID_A_TWIN)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"processed_sessions": {SID_A: {"memo_generated_at": "2026-10-21T10:00:00"}}}))

    result = bhm.backfill(vault, state, apply=True)

    assert _flag(agreeing) == "true" and _flag(twin) == "false"
    assert result["by_kind"]["state"] == 1 and result["conflicts"] == 1


def test_stamp_in_memo_body_or_unparseable_frontmatter_is_not_evidence(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-21-body-mention.md", "title: Body", body=f"The session {SID_A} is discussed here.\n")
    _memo(vault, "2026-10-21-broken.md", f"title: [unclosed\nsession_id: {SID_B}")
    a = _transcript(vault, "20261021-100000-aaaaaaaa.md", SID_A)
    b = _transcript(vault, "20261021-100100-bbbbbbbb.md", SID_B)

    result = _run(vault, tmp_path, apply=True)

    assert result["updated"] == 0
    assert _flag(a) == "false" and _flag(b) == "false"


# --- existing evidence stays ------------------------------------------------------------


def test_filename_suffix_and_state_json_evidence_still_work(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "20261019-090000-cafe0001.md", "title: Suffix memo")  # no session_id stamp
    by_suffix = _transcript(vault, "20261019-080000-cafe0001.md", SID_NONE)
    by_state = _transcript(vault, "20261019-081000-beef0002.md", None)
    untouched = _transcript(vault, "20261019-082000-dead0003.md", None)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"processed_sessions": {
        "beef0002-0000-4000-8000-000000000000": {"memo_generated_at": "2026-10-19T09:00:00"},
        "dead0003-0000-4000-8000-000000000000": {"processed_at": "2026-10-19T09:00:00"},  # not a memo
    }}))

    result = bhm.backfill(vault, state, apply=True)

    assert _flag(by_suffix) == "true" and _flag(by_state) == "true" and _flag(untouched) == "false"
    assert result["by_kind"] == {"stamp": 0, "session_ids": 0, "suffix": 1, "state": 1}


def test_each_transcript_is_counted_under_its_strongest_evidence(tmp_path):
    vault = _vault(tmp_path)
    # Suffix AND stamp both cover this transcript; the stamp is the stronger evidence.
    _memo(vault, "20261020-090000-aaaaaaaa.md", f"title: Both\nsession_id: {SID_A}")
    _transcript(vault, "20261020-080000-aaaaaaaa.md", SID_A)

    result = _run(vault, tmp_path)

    assert result["by_kind"] == {"stamp": 1, "session_ids": 0, "suffix": 0, "state": 0}


def test_transcript_without_a_filename_suffix_matches_by_stamp(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_B}")
    odd = _transcript(vault, "codex-thread-notes.md", SID_B)

    result = _run(vault, tmp_path, apply=True)

    assert _flag(odd) == "true" and result["by_kind"]["stamp"] == 1


# --- byte safety, dry run, counters -----------------------------------------------------


def test_only_the_has_memo_line_changes_crlf_and_mode_preserved(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    body = "# Heads\r\n\r\ncafé 日本語   body with no final newline".encode()
    path = _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A, body=body, eol=b"\r\n", quote='"')
    path.chmod(0o644)
    before = path.read_bytes()

    _run(vault, tmp_path, apply=True)

    after = path.read_bytes()
    assert after == before.replace(b"has_memo: false\r\n", b"has_memo: true\r\n", 1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_has_memo_false_text_in_the_body_is_never_rewritten(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    # Frontmatter is already true; a "false" line lives only in the body.
    path = _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A, has_memo="true")
    before = path.read_bytes()

    result = _run(vault, tmp_path, apply=True)

    assert path.read_bytes() == before
    assert result["already_correct"] == 1 and result["updated"] == 0


def test_dry_run_reports_but_writes_nothing(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    path = _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    before = path.read_bytes()

    result = _run(vault, tmp_path)  # apply defaults to False

    assert path.read_bytes() == before
    assert result["updated"] == 1 and result["by_kind"]["stamp"] == 1


def test_unidentifiable_transcripts_are_reported_separately(tmp_path):
    vault = _vault(tmp_path)
    _transcript(vault, "no-suffix-no-id.md", None)

    result = _run(vault, tmp_path)

    assert result["no_identifier"] == 1 and result["unmatched"] == 0


def test_failed_rewrite_is_counted_not_reported_as_updated(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    monkeypatch.setattr(bhm, "_flip_has_memo_line", lambda path: False)

    result = _run(vault, tmp_path, apply=True)

    assert result["updated"] == 0 and result["failed"] == 1
    assert "FAILED" in capsys.readouterr().err


# --- Obsidian CLI is opt-in -------------------------------------------------------------


def test_obsidian_cli_is_not_probed_by_default(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    path = _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    monkeypatch.setattr(bhm, "_load_obsidian_cli", lambda: pytest.fail("Obsidian CLI probed"))

    result = _run(vault, tmp_path, apply=True)

    assert _flag(path) == "true" and result["updated"] == 1


def test_obsidian_cli_flag_uses_the_cli_and_falls_back_when_it_fails(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_ids: [{SID_A}, {SID_B}]")
    good = _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    flaky = _transcript(vault, "20261022-100100-bbbbbbbb.md", SID_B)

    class FakeCLI:
        calls: list[str] = []

        def property_set(self, key, value, path):
            FakeCLI.calls.append(path)
            if path.endswith("bbbbbbbb.md"):
                raise RuntimeError("obsidian hiccup")
            target = vault / path
            target.write_bytes(target.read_bytes().replace(b"has_memo: false", b"has_memo: true", 1))

    monkeypatch.setattr(bhm, "_load_obsidian_cli", lambda: FakeCLI())

    result = _run(vault, tmp_path, apply=True, use_obsidian_cli=True)

    assert sorted(Path(p).name for p in FakeCLI.calls) == [good.name, flaky.name]
    assert _flag(good) == "true" and _flag(flaky) == "true"  # the failing one fell back
    assert result["updated"] == 2 and result["failed"] == 0


def test_obsidian_cli_not_probed_on_a_dry_run_even_with_the_flag(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    monkeypatch.setattr(bhm, "_load_obsidian_cli", lambda: pytest.fail("Obsidian CLI probed"))

    assert _run(vault, tmp_path, use_obsidian_cli=True)["updated"] == 1


# --- command line -----------------------------------------------------------------------


def _main(monkeypatch, vault: Path, state_dir: Path, *argv: str) -> None:
    monkeypatch.setattr(bhm, "get_memex_path", lambda *a, **k: vault)
    monkeypatch.setattr(bhm, "get_state_dir", lambda *a, **k: state_dir)
    monkeypatch.setattr(sys, "argv", ["backfill_has_memo.py", *argv])
    bhm.main()


def test_main_prints_a_count_per_evidence_kind(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-one.md", f"title: T\nsession_id: {SID_A}")
    _memo(vault, "2026-10-22-two.md", f"title: T\nsession_ids: [{SID_B}]")
    _memo(vault, "20261022-090000-cafe0001.md", "title: T")
    for name, sid in (("20261022-100000-aaaaaaaa.md", SID_A), ("20261022-100100-bbbbbbbb.md", SID_B),
                      ("20261022-100200-cafe0001.md", SID_NONE)):
        _transcript(vault, name, sid)

    _main(monkeypatch, vault, tmp_path)
    out = capsys.readouterr().out

    assert "Would update: 3" in out
    assert "via memo session_id: 1" in out and "via memo session_ids: 1" in out
    assert "via memo filename suffix: 1" in out and "via state.json: 0" in out
    assert "Run with --apply" in out

    _main(monkeypatch, vault, tmp_path, "--apply", "-v")
    out = capsys.readouterr().out
    assert "Updated: 3" in out and "MATCH" in out and "Run with --apply" not in out


def test_main_exits_nonzero_when_an_apply_could_not_write(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-one.md", f"title: T\nsession_id: {SID_A}")
    _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    monkeypatch.setattr(bhm, "_flip_has_memo_line", lambda path: False)

    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, vault, tmp_path, "--apply")

    assert exc.value.code == 1
    assert "Failed: 1" in capsys.readouterr().out


# --- robustness -------------------------------------------------------------------------


def test_symlinked_transcript_is_updated_through_the_link(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    real = _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    moved = tmp_path / "elsewhere.md"
    real.rename(moved)
    real.symlink_to(moved)

    result = _run(vault, tmp_path, apply=True)

    assert real.is_symlink() and result["updated"] == 1
    assert b"\nhas_memo: true\n" in moved.read_bytes()


def test_unreadable_and_unclosed_frontmatter_are_reported_not_called_correct(tmp_path):
    vault = _vault(tmp_path)
    tx = vault / "projects" / "demo" / "transcripts"
    (tx / "20261022-100000-aaaaaaaa.md").write_text("# no frontmatter at all\n")
    (tx / "20261022-100100-bbbbbbbb.md").write_text(f"---\nsession_id: {SID_A}\nhas_memo: false\n# never closes\n")
    _transcript(vault, "20261022-100200-cccccccc.md", SID_C, has_memo="true")

    result = _run(vault, tmp_path)

    assert result["unreadable"] == 2 and result["already_correct"] == 1 and result["updated"] == 0


def test_malformed_state_json_does_not_abort_the_run(tmp_path):
    vault = _vault(tmp_path)
    _memo(vault, "2026-10-22-slug.md", f"title: T\nsession_id: {SID_A}")
    _transcript(vault, "20261022-100000-aaaaaaaa.md", SID_A)
    for payload in ('{"processed_sessions": null}', '{"processed_sessions": [1, 2]}', "[]", "not json"):
        state = tmp_path / "state.json"
        state.write_text(payload)
        assert bhm.backfill(vault, state)["by_kind"]["stamp"] == 1, payload
