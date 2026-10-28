"""v0.22.0 schema lint (plan E1, E2): the harness-aware capture keys and the memo
``topics:`` slug lint, on top of the YAML-damage checks in test_frontmatter_audit.

Two tiers: ``issues`` are errors (fail the exit code); ``warnings`` are judgement
calls (reported, never fatal). Every fixture is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memex.harness import HARNESSES
from memex.scripts import frontmatter_audit as fa
from memex.scripts.frontmatter_audit import (
    _UNREADABLE,
    _fragment,
    _scan_file,
    audit_frontmatter,
)

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _memo(tmp_path: Path, extra: str, *, project: str = "proj-a", name: str = "2026-10-28-m.md") -> Path:
    return _write(
        tmp_path / "projects" / project / "memos" / name,
        f'---\ntype: memo\ntitle: "T"\nproject: {project}\ndate: 2026-10-28\n{extra}---\nbody\n',
    )


def _by_kind(findings: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in findings:
        out.setdefault(f["kind"], []).append(f)
    return out


# --- E1: harness -------------------------------------------------------------

@pytest.mark.parametrize("harness", HARNESSES)
def test_every_known_harness_is_clean(tmp_path: Path, harness: str) -> None:
    assert _scan_file(_memo(tmp_path, f"harness: {harness}\n")) == []


def test_harness_value_may_be_quoted_or_carry_a_comment(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, 'harness: "codex-desktop"\n')) == []
    assert _scan_file(_memo(tmp_path, "harness: kimi-code   # the orchestrator\n")) == []


def test_unknown_harness_is_an_error_naming_the_value(tmp_path: Path) -> None:
    (iss,) = _scan_file(_memo(tmp_path, "harness: codex\n"))
    assert iss["kind"] == "bad-harness"
    assert iss["severity"] == "error"
    assert "'codex'" in iss["detail"]
    assert "codex-desktop" in iss["detail"]  # the message lists the valid values
    assert iss["line"] == 6  # opener + type, title, project, date, then harness


def test_harness_is_case_sensitive(tmp_path: Path) -> None:
    assert [i["kind"] for i in _scan_file(_memo(tmp_path, "harness: Codex-Desktop\n"))] == ["bad-harness"]


def test_empty_or_list_harness_is_an_error(tmp_path: Path) -> None:
    (empty,) = _scan_file(_memo(tmp_path, "harness:\n"))
    assert empty["kind"] == "bad-harness" and "empty" in empty["detail"]
    (lst,) = _scan_file(_memo(tmp_path, "harness: [codex-desktop]\n"))
    assert lst["kind"] == "bad-harness" and "list" in lst["detail"]


def test_absent_harness_is_clean(tmp_path: Path) -> None:
    # pre-0.22.0 memos have no harness; the key is optional
    assert _scan_file(_memo(tmp_path, "")) == []


# --- E1: session_ids ---------------------------------------------------------

def test_session_ids_flow_list(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, f"session_ids: [{U1}, {U2}]\n")) == []


def test_session_ids_block_list_indented_and_flush(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, f"session_ids:\n  - {U1}\n  - {U2}\nmanual: false\n")) == []
    # YAML allows a sequence at the key's own indentation
    assert _scan_file(_memo(tmp_path, f"session_ids:\n- {U1}\n- {U2}\nmanual: false\n")) == []


def test_session_ids_block_list_quotes_comments_and_uppercase(tmp_path: Path) -> None:
    extra = f'session_ids:\n  - "{U1}"   # first thread\n  # a comment line\n  - {U2.upper()}\n'
    assert _scan_file(_memo(tmp_path, extra)) == []


def test_session_ids_flow_list_spread_over_lines(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, f"session_ids: [{U1},\n  {U2}]\nmanual: false\n")) == []


def test_session_ids_empty_or_null_is_clean(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, "session_ids: []\n")) == []
    assert _scan_file(_memo(tmp_path, "session_ids:\nmanual: false\n")) == []


def test_session_ids_names_only_the_bad_entries(tmp_path: Path) -> None:
    (iss,) = _scan_file(_memo(tmp_path, f"session_ids: [{U1}, 9f3c1a2b, {U2}, agent-0abc123]\n"))
    assert iss["kind"] == "bad-session-ids"
    assert iss["severity"] == "error"
    assert "'9f3c1a2b'" in iss["detail"] and "'agent-0abc123'" in iss["detail"]
    assert U1 not in iss["detail"] and U2 not in iss["detail"]


def test_session_ids_block_list_with_a_prefix_entry(tmp_path: Path) -> None:
    (iss,) = _scan_file(_memo(tmp_path, f"session_ids:\n  - {U1}\n  - {U2[:8]}\n"))
    assert iss["kind"] == "bad-session-ids" and f"'{U2[:8]}'" in iss["detail"]


def test_session_ids_scalar_is_an_error(tmp_path: Path) -> None:
    # memo_stamps ignores a non-list, so a scalar silently unlinks the memo
    (iss,) = _scan_file(_memo(tmp_path, f"session_ids: {U1}\n"))
    assert iss["kind"] == "bad-session-ids" and "list" in iss["detail"]


def test_session_ids_many_bad_entries_are_summarised(tmp_path: Path) -> None:
    (iss,) = _scan_file(_memo(tmp_path, "session_ids: [a1, b2, c3, d4, e5]\n"))
    assert "(+2 more)" in iss["detail"]


# --- E1: session_id ----------------------------------------------------------

def test_session_id_full_uuid_is_clean(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, f"session_id: {U1}\n")) == []
    assert _scan_file(_memo(tmp_path, f'session_id: "{U1.upper()}"\n')) == []


def test_session_id_prefix_is_a_warning_not_an_error(tmp_path: Path) -> None:
    # legacy memos carry 8-hex prefixes: real, never matched, but not damage
    for stamp in ("9f3c1a2b", f'"ws-{U1}"', "agent-0def456"):
        (iss,) = _scan_file(_memo(tmp_path, f"session_id: {stamp}\n"))
        assert iss["kind"] == "session-id-not-uuid"
        assert iss["severity"] == "warning"


def test_session_id_does_not_get_confused_with_session_ids(tmp_path: Path) -> None:
    # a good session_ids next to a good session_id: nothing; the keys are distinct
    assert _scan_file(_memo(tmp_path, f"session_id: {U1}\nsession_ids: [{U2}, {U3}]\n")) == []


# --- E1: new keys are schema keys --------------------------------------------

def test_new_keys_are_known_and_never_read_as_merged(tmp_path: Path) -> None:
    extra = (
        "harness: claude-desktop-relay\n"
        f"session_ids: [{U1}, {U2}]\n"
        f"parent_session_id: {U3}\n"
        "subagents: 3\n"
        "relay_channel: chan_01AbCdEf\n"
        "manual: false              # distinguishes written memos from extracted ones\n"
        "source_cwd: /tmp/some dir/with spaces\n"
        "written_by: a curator agent, reconstructed from the record\n"
        'relay_coordinator: "coordinator session (channel: ops)"\n'  # quoted: a colon is fine
    )
    assert _scan_file(_memo(tmp_path, extra)) == []


def test_new_keys_are_in_the_merge_detector(tmp_path: Path) -> None:
    # a v0.22.0 key glued onto an enum value is now caught
    p = _memo(tmp_path, "status: archivedharness: codex-desktop\n")
    assert "merged-line" in {i["kind"] for i in _scan_file(p)}


def test_unquoted_colon_in_a_prose_key_is_a_merge(tmp_path: Path) -> None:
    # not exempted as free text: 'written_by: x (harness: y)' is a YAML error that
    # makes hooks ignore the memo's stamps, and a glued 'session_ids:' would hide here
    for line in ("written_by: curator agent (harness: codex-desktop)\n",
                 f"written_by: curator agent session_ids: [{U1}]\n"):
        assert "merged-line" in {i["kind"] for i in _scan_file(_memo(tmp_path, line))}


def test_trailing_comment_naming_a_key_is_not_a_merge(tmp_path: Path) -> None:
    # valid YAML: the comment is not a second key
    for line in ("harness: kimi-code   # type: orchestrator\n",
                 f"session_ids: [{U1}]   # source: codex rollout\n"):
        assert _scan_file(_memo(tmp_path, line)) == []


def test_merged_harness_line_is_reported_once(tmp_path: Path) -> None:
    # 'harness: codex-desktop session_ids: [..]' is ONE defect (a merge); judging
    # the glued value as an unknown harness too would double-report it
    p = _memo(tmp_path, f"harness: codex-desktop session_ids: [{U1}]\n")
    kinds = [i["kind"] for i in _scan_file(p)]
    assert kinds == ["merged-line"]


# --- E1: reading a key out of damaged frontmatter -----------------------------

def test_fragment_ignores_indented_lookalikes() -> None:
    fm = ["type: memo", "details:", "  harness: nonsense", "  session_ids: junk"]
    assert _fragment(fm, "harness") is None
    assert _fragment(fm, "session_ids") is None


def test_fragment_shapes() -> None:
    assert _fragment(["k: v"], "k") == (2, "v")
    assert _fragment(["a: 1", "k: [x, 'y']"], "k") == (3, ["x", "y"])
    assert _fragment(["k:", "  - x", "  - \"y\"", "z: 1"], "k") == (2, ["x", "y"])
    assert _fragment(["k:", "- x", "- y", "z: 1"], "k") == (2, ["x", "y"])  # flush sequence
    assert _fragment(["k:", "z: 1"], "k") == (2, None)
    assert _fragment(["k: ~"], "k") == (2, None)
    assert _fragment(["k: []"], "k") == (2, [])
    assert _fragment(["k: >-", "  full text", "z: 1"], "k") == (2, "full text")
    assert _fragment(["other: 1"], "k") is None


def test_fragment_stops_at_the_next_key_and_does_not_swallow_neighbours() -> None:
    # an unterminated flow list must not run on into the lines below it
    fm = ["topics: [a, b", "manual: false", "harness: bad"]
    assert _fragment(fm, "topics") == (2, _UNREADABLE)
    assert _fragment(fm, "harness") == (4, "bad")


def test_null_session_keys_are_not_errors(tmp_path: Path) -> None:
    # YAML null claims no stamps; memo_stamps treats it as none
    for line in ("session_ids: null\n", "session_ids: ~\n", "session_id: ~\n", "session_id:\n"):
        assert _scan_file(_memo(tmp_path, line)) == [], line


def test_block_scalar_values_are_read_by_value_not_indicator(tmp_path: Path) -> None:
    # reconcile_orphans supports `session_id: >-` with the value on the next line
    assert _scan_file(_memo(tmp_path, f"session_id: >-\n  {U1}\nharness: >-\n  kimi-code\n")) == []
    (iss,) = _scan_file(_memo(tmp_path, "harness: >-\n  nope\n"))
    assert iss["kind"] == "bad-harness" and "'nope'" in iss["detail"]


def test_unreadable_capture_lines_are_reported_not_skipped(tmp_path: Path) -> None:
    (a,) = _scan_file(_memo(tmp_path, "harness: [codex-desktop\n"))
    assert a["kind"] == "bad-harness" and "not valid YAML" in a["detail"]
    (b,) = _scan_file(_memo(tmp_path, "session_ids: [a, b\n"))
    assert b["kind"] == "bad-session-ids" and "not valid YAML" in b["detail"]


# --- E1: a stamp the strict reader never sees ----------------------------------

def test_valid_stamp_in_frontmatter_that_is_not_yaml_is_an_error(tmp_path: Path) -> None:
    # an unquoted `title: ...: ...` makes the whole block unparseable; hooks then
    # ignore the (perfectly valid) stamp and the memo looks unlinked
    p = _write(tmp_path / "projects" / "proj-a" / "memos" / "2026-10-21-m.md",
               f"---\ntype: memo\ntitle: Round 3: six models drawn: six models\nsession_id: {U1}\n---\nbody\n")
    (iss,) = _scan_file(p)
    assert iss["kind"] == "stamps-ignored" and iss["severity"] == "error"
    assert "not valid YAML" in iss["detail"] and "line 3" in iss["detail"]


def test_duplicate_stamp_key_is_an_error(tmp_path: Path) -> None:
    # PyYAML would silently keep the last one; the strict reader refuses both
    (iss,) = _scan_file(_memo(tmp_path, f"session_ids: [{U1}]\nsession_ids: [{U2}]\n"))
    assert iss["kind"] == "stamps-ignored" and "duplicate key" in iss["detail"]


def test_stamps_the_strict_reader_sees_are_clean(tmp_path: Path) -> None:
    assert _scan_file(_memo(tmp_path, f"session_id: {U1}\nsession_ids: [{U2}, {U3}]\n")) == []


def test_stamps_ignored_is_not_stacked_on_a_merged_line(tmp_path: Path) -> None:
    # the merge is the explained root cause; one finding, not two
    p = _memo(tmp_path, f"status: archivedtitle: x\nsession_id: {U1}\n")
    assert {i["kind"] for i in _scan_file(p)} == {"merged-line"}


def test_unstamped_memo_with_broken_yaml_is_left_alone(tmp_path: Path) -> None:
    # ~1.4% of old memos don't parse; with no stamp to lose, that is not this lint's call
    p = _write(tmp_path / "projects" / "proj-a" / "memos" / "2026-01-01-m.md",
               "---\ntype: memo\ntitle: Notes: a study: part two\nproject: proj-a\n---\nbody\n")
    assert _scan_file(p) == []


# --- E2: topic slug lint (warning tier) --------------------------------------

def _topic_vault(tmp_path: Path) -> Path:
    _write(tmp_path / "topics" / "real-topic.md",
           '---\ntype: concept\ntitle: "Real"\n---\nbody\n')
    _write(tmp_path / "topics" / "renamed.md",
           '---\ntype: concept\ntitle: "Renamed"\naliases:\n  - old-name\n  - "Older Name"\n---\nbody\n')
    _write(tmp_path / "topics" / "inline-alias.md",
           '---\ntype: concept\ntitle: "I"\naliases: [flow-alias, other-alias]\n---\nbody\n')
    _write(tmp_path / "topics" / "gone.md",
           '---\ntype: concept\ntitle: "Gone"\nstatus: archived\nredirect_to: real-topic\n---\nbody\n')
    _write(tmp_path / "projects" / "proj-a" / "_project.md",
           '---\ntype: project\nname: proj-a\naliases: [only-on-a-project-note]\n---\nbody\n')
    (tmp_path / "projects" / "no-overview").mkdir(parents=True)
    return tmp_path


def test_phantom_topics_per_class(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [real-topic, old-name, flow-alias, proj-a, truly-unknown]\n")

    report = audit_frontmatter(vault)
    assert report["issues"] == []  # nothing here is an error
    kinds = _by_kind(report["warnings"])
    assert set(kinds) == {"unknown-topic", "project-slug-topic"}

    (proj,) = kinds["project-slug-topic"]
    assert proj["severity"] == "warning"
    assert "project slug used as topic" in proj["detail"] and "'proj-a'" in proj["detail"]

    (unknown,) = kinds["unknown-topic"]
    assert "'truly-unknown'" in unknown["detail"]
    assert "project slug" not in unknown["detail"]


def test_alias_only_resolution_is_clean(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, 'topics: [old-name, "Older Name", flow-alias, other-alias]\n')
    assert audit_frontmatter(vault)["warnings"] == []


def test_archived_topic_file_still_resolves(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [gone]\n")
    assert audit_frontmatter(vault)["warnings"] == []


def test_alias_on_a_non_topic_note_does_not_resolve(tmp_path: Path) -> None:
    # only topics/*.md aliases count; a project overview's alias is not a topic
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [only-on-a-project-note]\n")
    (w,) = audit_frontmatter(vault)["warnings"]
    assert w["kind"] == "unknown-topic" and "'only-on-a-project-note'" in w["detail"]


def test_project_slug_with_its_own_topic_file_is_clean(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _write(vault / "topics" / "proj-a.md", '---\ntype: concept\ntitle: "A"\n---\nbody\n')
    _memo(vault, "topics: [proj-a]\n")
    assert audit_frontmatter(vault)["warnings"] == []


def test_project_folder_without_overview_still_counts_as_project_slug(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [no-overview]\n")
    (w,) = audit_frontmatter(vault)["warnings"]
    assert w["kind"] == "project-slug-topic"


def test_topics_in_block_list_wikilink_and_case_forms(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, 'topics:\n  - Real-Topic\n  - "[[renamed]]"\n  - nope\n')
    (w,) = audit_frontmatter(vault)["warnings"]
    assert w["kind"] == "unknown-topic" and "'nope'" in w["detail"]
    assert w["line"] == 6  # the `topics:` key line


def test_duplicate_slug_in_one_memo_is_reported_once(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [nope, nope, NOPE]\n")
    assert len(audit_frontmatter(vault)["warnings"]) == 1


def test_empty_absent_and_scalar_topics_are_not_judged(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: []\n", name="2026-10-01-a.md")
    _memo(vault, "", name="2026-10-02-b.md")
    _memo(vault, "topics:\nmanual: false\n", name="2026-10-03-c.md")
    _memo(vault, "topics: some-scalar\n", name="2026-10-04-d.md")
    assert audit_frontmatter(vault)["warnings"] == []


def test_wikilink_style_flow_lists_are_judged_item_by_item(tmp_path: Path) -> None:
    # '[[a]]' inside a flow list must not be cut at its first ']' (33 vault memos
    # write topics this way): 'renamed' resolves, 'zzz' is the one real phantom
    vault = _topic_vault(tmp_path)
    _memo(vault, 'topics: ["[[renamed]]", "[[real-topic]]", zzz]\n')
    (w,) = audit_frontmatter(vault)["warnings"]
    assert w["kind"] == "unknown-topic" and "'zzz'" in w["detail"]


def test_unparseable_topics_are_not_judged_and_do_not_swallow_neighbours(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    # invalid YAML (a bare '[[a]], [[b]]' run) and an unterminated flow list: the
    # topics line is skipped, and the harness line below it is still checked
    _memo(vault, "topics: [[renamed]], [[nope]]\nharness: bogus\n", name="2026-10-01-a.md")
    _memo(vault, "topics: [nope, zzz\nharness: bogus\n", name="2026-10-02-b.md")
    report = audit_frontmatter(vault)
    assert report["warnings"] == []
    assert [i["kind"] for i in report["issues"]] == ["bad-harness", "bad-harness"]


def test_no_topic_files_at_all_means_no_topic_lint(tmp_path: Path) -> None:
    # fresh vault / sparse checkout: with nothing to compare against, every slug
    # would be reported as a phantom
    _memo(tmp_path, "topics: [anything, proj-a]\n")
    assert audit_frontmatter(tmp_path)["warnings"] == []


def test_alias_in_an_unclosed_topic_file_does_not_resolve(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _write(vault / "topics" / "broken.md", "---\ntype: concept\ntitle: B\naliases: [sneaky]\nbody, no closing\n")
    _memo(vault, "topics: [sneaky]\n")
    (w,) = audit_frontmatter(vault)["warnings"]
    assert w["kind"] == "unknown-topic"


def test_topic_lint_only_applies_to_memos(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _write(vault / "topics" / "other.md",
           '---\ntype: concept\ntitle: "O"\ntopics: [nope]\n---\nbody\n')
    _write(vault / "projects" / "proj-a" / "_project.md",
           '---\ntype: project\nname: proj-a\ntopics: [nope]\n---\nbody\n')
    assert audit_frontmatter(vault)["warnings"] == []


def test_scan_file_without_a_topic_index_skips_the_topic_lint(tmp_path: Path) -> None:
    # the per-file entry point other callers/tests use stays context-free
    assert _scan_file(_memo(tmp_path, "topics: [nope]\n")) == []


def test_warning_and_error_in_one_memo_land_in_their_tiers(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "harness: bogus\ntopics: [nope]\n")
    report = audit_frontmatter(vault)
    assert [i["kind"] for i in report["issues"]] == ["bad-harness"]
    assert [w["kind"] for w in report["warnings"]] == ["unknown-topic"]


def test_clean_v022_memo_in_a_real_vault_passes(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault,
          f"harness: codex-desktop\nsession_ids:\n  - {U1}\n  - {U2}\n"
          "written_by: curator memo agent, reconstructed from the record\n"
          "topics: [real-topic, old-name]\n")
    report = audit_frontmatter(vault)
    assert report["issues"] == [] and report["warnings"] == []


# --- exit codes and output ----------------------------------------------------

def _run(vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
         *, json_out: bool = False) -> tuple[int, str]:
    monkeypatch.setattr(fa, "get_memex_path", lambda: vault)
    code = fa.run_frontmatter_audit(json_out=json_out)
    return code, capsys.readouterr().out


def test_warnings_alone_do_not_fail_the_check(tmp_path, monkeypatch, capsys) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [nope, proj-a]\n")
    code, out = _run(vault, monkeypatch, capsys)
    assert code == 0
    assert "No frontmatter errors" in out
    assert "[unknown-topic] ×1" in out and "[project-slug-topic] ×1" in out
    assert "project slug used as topic" in out


def test_an_error_still_fails_the_check_and_warnings_still_print(tmp_path, monkeypatch, capsys) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "harness: bogus\nsession_ids: [x]\ntopics: [nope]\n")
    code, out = _run(vault, monkeypatch, capsys)
    assert code == 1
    assert "[bad-harness] ×1" in out and "[bad-session-ids] ×1" in out
    assert "[unknown-topic] ×1" in out


def test_json_output_has_both_tiers(tmp_path, monkeypatch, capsys) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "topics: [nope]\n")
    code, out = _run(vault, monkeypatch, capsys, json_out=True)
    data = json.loads(out)
    assert code == 0
    assert data["issues"] == []
    assert [w["kind"] for w in data["warnings"]] == ["unknown-topic"]
    assert data["warnings"][0]["severity"] == "warning"


def test_clean_vault_prints_the_all_clear(tmp_path, monkeypatch, capsys) -> None:
    vault = _topic_vault(tmp_path)
    _memo(vault, "harness: kimi-code\ntopics: [real-topic]\n")
    code, out = _run(vault, monkeypatch, capsys)
    assert code == 0
    assert "✓ No frontmatter issues" in out and "warning" not in out


def test_warning_rows_are_capped_newest_first(tmp_path, monkeypatch, capsys) -> None:
    vault = _topic_vault(tmp_path)
    for day in range(1, 31):  # 30 memos, each with one phantom topic
        _memo(vault, f"topics: [phantom-{day:02d}]\n", name=f"2026-09-{day:02d}-m.md")
    code, out = _run(vault, monkeypatch, capsys)
    assert code == 0
    assert "[unknown-topic] ×30" in out
    assert out.count("matches no topics/*.md file") == fa._WARNING_ROWS_SHOWN
    assert "… and 5 more" in out
    assert "phantom-30" in out and "phantom-06" in out  # newest 25 kept
    assert "phantom-05" not in out and "phantom-01" not in out


def test_capped_rows_sort_by_the_date_in_the_name_not_the_raw_string(tmp_path, monkeypatch, capsys) -> None:
    # '20260214-…' > '2026-10-28-…' as strings ('0' > '-'), and undated names sort
    # above any date: neither may crowd the newest dashed memos out of the cap
    vault = _topic_vault(tmp_path)
    for i in range(fa._WARNING_ROWS_SHOWN):
        _memo(vault, f"topics: [recent-{i:02d}]\n", name=f"2026-10-{i + 1:02d}-m.md")
    _memo(vault, "topics: [compact-old]\n", name="20260214-m.md")
    _memo(vault, "topics: [undated-old]\n", name="xhs-skill-notes.md")
    _memo(vault, "topics: [epoch-old]\n", name="1738605-m.md")
    code, out = _run(vault, monkeypatch, capsys)
    assert code == 0
    assert "recent-00" in out and "recent-24" in out
    assert "compact-old" not in out and "undated-old" not in out and "epoch-old" not in out
    assert "… and 3 more" in out


# --- second-round review: faithfulness to the strict reader ---------------------

def test_flow_list_may_continue_at_column_zero(tmp_path: Path) -> None:
    # YAML accepts it; the key's block ends at the next `key:` line, not at the
    # first unindented line
    assert _scan_file(_memo(tmp_path, f"session_ids: [{U1},\n{U2}]\nmanual: false\n")) == []


def test_session_id_given_as_a_collection_is_an_error(tmp_path: Path) -> None:
    # unlike a legacy prefix, a list is ignored by the reader: the memo is unlinked
    (iss,) = _scan_file(_memo(tmp_path, f"session_id: [{U1}]\n"))
    assert iss["kind"] == "session-id-not-uuid" and iss["severity"] == "error"
    assert "collection" in iss["detail"]


def test_unrelated_yaml_error_is_not_hidden_by_a_merged_line_elsewhere(tmp_path: Path) -> None:
    # the first YAML error is the unquoted title colon (line 3), not the merge: both are real
    p = _write(tmp_path / "projects" / "proj-a" / "memos" / "2026-10-20-m.md",
               "---\ntype: memo\ntitle: Round 3: six models drawn: six\n"
               f"status: archivedharness: kimi-code\nsession_id: {U1}\n---\nbody\n")
    assert {i["kind"] for i in _scan_file(p)} == {"merged-line", "stamps-ignored"}


def test_delimiter_only_disagreement_gets_an_honest_reason(tmp_path: Path) -> None:
    # the lint's block finder strips whitespace from '---'; the strict reader does not
    p = _write(tmp_path / "projects" / "proj-a" / "memos" / "2026-10-20-m.md",
               f"---\ntype: memo\ntitle: T\nsession_id: {U1}\n--- \nbody\n")
    (iss,) = _scan_file(p)
    assert iss["kind"] == "stamps-ignored"
    assert "still rejects the block" in iss["detail"] and "'---' line" in iss["detail"]


def test_wikilink_forms_in_topics_resolve_to_their_target(tmp_path: Path) -> None:
    vault = _topic_vault(tmp_path)
    # unquoted [[a]] parses as a nested list; the others carry a label / folder / suffix
    _memo(vault, "topics: [[renamed]]\n", name="2026-10-01-a.md")
    _memo(vault, 'topics: ["[[renamed|Renamed]]", "[[topics/real-topic]]", "topics/real-topic.md", '
                 '"[[real-topic#Heading]]"]\n', name="2026-10-02-b.md")
    _memo(vault, "topics:\n  - [[zzz]]\n", name="2026-10-03-c.md")
    (w,) = audit_frontmatter(vault)["warnings"]
    assert w["kind"] == "unknown-topic" and "'zzz'" in w["detail"] and "2026-10-03-c" in w["path"]
