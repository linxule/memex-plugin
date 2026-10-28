"""v0.22.0 audit unit (plan items B3, C3): ``memex check --folders`` sees the
drift classes the 2026-10-28 pass found by hand, and ``memex check --channels``
lists relay sessions whose claude.ai channel has no ``channel_mappings`` entry.

Fixtures are synthetic: invented project names, cwds and channel ids only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from memex.scripts import project_audit as pa
from memex.scripts.project_audit import (
    _drift_folders,
    _review_folders,
    audit_channels,
    audit_folders,
    is_temp_cwd,
)
from memex.scripts.utils import sanitize_project_name

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "memex"

TMP_T = "/private/var/folders/ab/cdef1234/T"
HOME = "/Users/tester/Documents"


@pytest.fixture(autouse=True)
def _leaf_detection(monkeypatch):
    """Make ``detect_project`` independent of the developer's own config/pins:
    these tests are about the transcript evidence, not about name≠canonical."""
    monkeypatch.setattr(pa, "detect_project", lambda cwd: sanitize_project_name(Path(cwd).name))


def _session(
    vault: Path,
    project: str,
    sid: str,
    *,
    cwd: str | None = None,
    entrypoint: str | None = None,
    channel: str | None = None,
    body: str = "",
    jsonl: bool = True,
    stamp: str = "20261010-120000",
) -> Path:
    """Archive one synthetic session as ``transcripts/<stamp>-<sid>.{md,jsonl}``."""
    tr = vault / "projects" / project / "transcripts"
    tr.mkdir(parents=True, exist_ok=True)
    fm = ["---", "type: transcript", f"session_id: {sid}", "date: 2026-10-10",
          "start_time: 2026-10-10T12:00:00"]
    if entrypoint:
        fm.append(f"entrypoint: {entrypoint}")
    if channel:
        fm.append(f"relay_channel: {channel}")
    fm += [f"project: {project}", "---", "", f"# Session: {sid[:8]}", ""]
    md = tr / f"{stamp}-{sid}.md"
    md.write_text("\n".join(fm) + (body or "## Turn 1\n\n### User\n\nhello\n"), encoding="utf-8")
    if jsonl:
        line = {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "hi"}}
        if cwd:
            line["cwd"] = cwd
        if entrypoint:
            line["entrypoint"] = entrypoint
        (tr / f"{stamp}-{sid}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    return md


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _kinds(report: dict, name: str) -> dict[str, dict]:
    return {f["kind"]: f for f in report["records"][name]["findings"]}


# --- temp-cwd classification -------------------------------------------------

@pytest.mark.parametrize("cwd", [
    f"{TMP_T}/paperb-fixture-seat-x1",
    "/var/folders/ab/cdef1234/T/tmpabc123",
    "/private/tmp/mcp-host.Jl689Y/repo",
    "/tmp/x",
    "/private/tmp",
    # CLAUDE_CODE_TMPDIR can move the root; the scratchpad tail still identifies it
    "/opt/scratch/claude-501/-Users-tester-Documents-Apps-foo/0b2f6a1c-1111-2222-3333-444455556666/scratchpad/wt",
])
def test_temp_cwds(cwd):
    assert is_temp_cwd(cwd)


@pytest.mark.parametrize("cwd", [
    f"{HOME}/Apps/paperb",
    "/Users/tester/tmp/project",   # a folder merely named tmp
    "/Users/tester/var/folders/ab/cdef1234/T/x",
    "/tmpfiles/x",                 # prefix of a root, not the root
    None,
    "",
])
def test_non_temp_cwds(cwd):
    assert not is_temp_cwd(cwd)


# --- B3(a)+(b): fleet-seat folders and <project>-<suffix> names ---------------

def test_seat_folder_next_to_its_project_is_high_with_target(tmp_path):
    vault = _vault(tmp_path)
    for i in range(2):
        _session(vault, "paperb", f"i{i}00aaaa", cwd=f"{HOME}/Apps/paperb", entrypoint="cli")
    seat = "paperb-fixture-seat-x1"
    for i in range(3):
        _session(vault, seat, f"s{i}00bbbb", cwd=f"{TMP_T}/{seat}", entrypoint="sdk-cli")

    report = audit_folders(vault)
    drift = _drift_folders(report)

    assert seat in drift and "paperb" not in drift
    kinds = _kinds(report, seat)
    assert kinds["fleet-seat"]["confidence"] == "high"
    assert "all 3 session(s) are sdk-cli" in kinds["fleet-seat"]["reason"]
    assert "temp-dir" in kinds["fleet-seat"]["reason"]
    assert kinds["name-extends"]["confidence"] == "high"
    assert kinds["name-extends"]["target"] == "paperb"
    assert report["records"]["paperb"]["findings"] == []


def test_name_alone_is_not_flagged(tmp_path):
    # memex / memex-cloud: a sibling project sharing a prefix. No seat evidence,
    # so no finding of any kind (the earlier audit's 20/20 false REVIEW flags).
    vault = _vault(tmp_path)
    _session(vault, "memex", "a100aaaa", cwd=f"{HOME}/Apps/memex", entrypoint="cli")
    _session(vault, "memex-cloud", "b100bbbb", cwd=f"{HOME}/Apps/memex-cloud", entrypoint="cli")
    _session(vault, "memex-cloud", "b200bbbb", cwd=f"{HOME}/Apps/memex-cloud", entrypoint="sdk-cli")

    report = audit_folders(vault)

    assert report["records"]["memex-cloud"]["findings"] == []
    assert _drift_folders(report) == {}
    assert _review_folders(report, {}) == {}


def test_mixed_seat_evidence_is_not_flagged(tmp_path):
    # Real-vault lesson: a folder of ONLY sdk-cli sessions (a headless pipeline)
    # where some happened to run from a temp dir is a real project, not a seat
    # folder. Mixed tells prove nothing; only "every session is sdk-cli AND temp".
    vault = _vault(tmp_path)
    _session(vault, "paperb", "i100aaaa", cwd=f"{HOME}/Apps/paperb", entrypoint="cli")
    pipe = "paperb-pipeline"
    for i in range(3):
        _session(vault, pipe, f"p{i}00bbbb", cwd=f"{HOME}/Apps/paperb-pipeline", entrypoint="sdk-cli")
    for i in range(2):
        _session(vault, pipe, f"t{i}00bbbb", cwd=f"{TMP_T}/paperb-pipeline", entrypoint="sdk-cli")
    mixed = "paperb-probe"
    _session(vault, mixed, "m100cccc", cwd=f"{HOME}/Elsewhere/{mixed}", entrypoint="sdk-cli")
    _session(vault, mixed, "m200cccc", cwd=f"{TMP_T}/{mixed}", entrypoint="cli")

    report = audit_folders(vault)

    assert report["records"][pipe]["findings"] == []
    assert report["records"][mixed]["findings"] == []
    assert _drift_folders(report) == {} and _review_folders(report, {}) == {}


def test_one_seat_among_real_sessions_does_not_make_a_name_suffix_drift(tmp_path):
    # memex-cloud: 20 interactive sessions and one cc-fleet seat. The name alone
    # never flags, and one seat does not turn it into evidence.
    vault = _vault(tmp_path)
    _session(vault, "memex", "a100aaaa", cwd=f"{HOME}/Apps/memex", entrypoint="cli")
    for i in range(20):
        _session(vault, "memex-cloud", f"c{i:02d}0bbbb", cwd=f"{HOME}/Apps/memex-cloud", entrypoint="cli")
    _session(vault, "memex-cloud", "seat0bbbb", cwd=f"{TMP_T}/memex-cloud-seat", entrypoint="sdk-cli")

    report = audit_folders(vault)

    assert report["records"]["memex-cloud"]["findings"] == []


def test_stable_temp_checkout_without_a_parent_is_review_not_drift(tmp_path):
    # 4 sdk-cli sessions in ONE temp checkout, no project this extends: could be a
    # real project run from /tmp -> REVIEW, with the reason saying so.
    vault = _vault(tmp_path)
    for i in range(4):
        _session(vault, "naive-studio", f"n{i}00aaaa", cwd=f"{TMP_T}/naive-studio", entrypoint="sdk-cli")

    report = audit_folders(vault)
    drift = _drift_folders(report)

    assert "naive-studio" not in drift and "naive-studio" in _review_folders(report, drift)
    f = _kinds(report, "naive-studio")["fleet-seat"]
    assert f["confidence"] == "review"
    assert "4 sessions share 1 temp dir(s)" in f["reason"]
    assert "may be a real project run from /tmp" in f["reason"]


def test_one_temp_dir_per_seat_is_high_even_without_a_parent(tmp_path):
    # the `repo` case: generic leaf, a fresh mkdtemp parent per seat
    vault = _vault(tmp_path)
    _session(vault, "repo", "r100aaaa", cwd="/private/tmp/mcp-host.Jl689Y/repo", entrypoint="sdk-cli")
    _session(vault, "repo", "r200aaaa", cwd="/private/tmp/mcp-host.Qx12Ab/repo", entrypoint="sdk-cli")

    report = audit_folders(vault)

    assert "repo" in _drift_folders(report)
    f = _kinds(report, "repo")["fleet-seat"]
    assert f["confidence"] == "high" and "own temp dir" in f["reason"]
    assert "name-extends" not in _kinds(report, "repo")


def test_seat_controls_stay_silent(tmp_path):
    vault = _vault(tmp_path)
    # sdk-cli only, but from a real repo cwd (a headless job): not a seat folder.
    _session(vault, "nightly-job", "n100aaaa", cwd=f"{HOME}/Apps/nightly-job", entrypoint="sdk-cli")
    # interactive work done in a temp dir: not fleet evidence either.
    _session(vault, "scratch-play", "p100bbbb", cwd=f"{TMP_T}/scratch-play", entrypoint="cli")
    # unknown entrypoint (pre-stamping archive) in a temp dir: no evidence.
    _session(vault, "old-tmp", "o100cccc", cwd=f"{TMP_T}/old-tmp", entrypoint=None)

    report = audit_folders(vault)

    for name in ("nightly-job", "scratch-play", "old-tmp"):
        assert report["records"][name]["findings"] == [], name
    assert _drift_folders(report) == {}


def test_real_memo_cwd_vetoes_seat_folder(tmp_path):
    vault = _vault(tmp_path)
    _session(vault, "alpha", "i100aaaa", cwd=f"{HOME}/Apps/alpha", entrypoint="cli")
    seat = "alpha-direction-x"
    _session(vault, seat, "s100aaaa", cwd=f"{TMP_T}/{seat}", entrypoint="sdk-cli")
    memos = vault / "projects" / seat / "memos"
    memos.mkdir()
    (memos / "m.md").write_text(
        f"---\ntype: memo\nsource_cwd: {HOME}/Apps/alpha\n---\nbody\n", encoding="utf-8")

    report = audit_folders(vault)

    assert report["records"][seat]["findings"] == []


# --- B3(c): catch-all cwds ----------------------------------------------------

def test_catch_all_with_relay_sessions_is_high_and_names_the_projects_below(tmp_path):
    vault = _vault(tmp_path)
    gh = f"{HOME}/GitHub"
    for i in range(2):
        _session(vault, "GitHub", f"r{i}00aaaa", cwd=gh, entrypoint="claude-desktop")
    _session(vault, "GitHub", "h100aaaa", cwd=gh, entrypoint="cli")
    _session(vault, "alpha", "a100bbbb", cwd=f"{gh}/alpha", entrypoint="cli")
    _session(vault, "beta", "b100bbbb", cwd=f"{gh}/beta", entrypoint="cli")

    report = audit_folders(vault)
    drift = _drift_folders(report)

    assert "GitHub" in drift
    f = _kinds(report, "GitHub")["catch-all"]
    assert f["confidence"] == "high"
    assert "parent directory of 2 other projects" in f["reason"]
    assert "'alpha'" in f["reason"] and "'beta'" in f["reason"]
    assert "2 of them claude.ai relay worker(s)" in f["reason"]
    assert report["records"]["GitHub"]["relay_sessions"] == 2
    # the projects below are not themselves flagged
    assert report["records"]["alpha"]["findings"] == []
    assert report["records"]["beta"]["findings"] == []


def test_catch_all_without_relay_sessions_is_review(tmp_path):
    vault = _vault(tmp_path)
    root = f"{HOME}/Apps"
    _session(vault, "Apps", "h100aaaa", cwd=root, entrypoint="cli")
    _session(vault, "one", "o100bbbb", cwd=f"{root}/one", entrypoint="cli")
    _session(vault, "two", "t100bbbb", cwd=f"{root}/two/sub", entrypoint="cli")

    report = audit_folders(vault)
    drift = _drift_folders(report)

    assert "Apps" not in drift
    assert "Apps" in _review_folders(report, drift)
    assert _kinds(report, "Apps")["catch-all"]["confidence"] == "review"


def _projects_below(vault: Path, parent: str) -> None:
    _session(vault, "alpha", "a100zzzz", cwd=f"{parent}/alpha", entrypoint="cli")
    _session(vault, "beta", "b100zzzz", cwd=f"{parent}/beta", entrypoint="cli")


def test_relay_sessions_filed_by_their_channel_mapping_are_not_catch_all_evidence(tmp_path):
    # The owner did what the report says: mapped the channel. The relay workers now
    # sit in their project although their cwd is a parent of other projects.
    vault = _vault(tmp_path)
    stand = f"{HOME}/GitHub"
    _projects_below(vault, stand)
    for i in range(2):
        _session(vault, "research-radar", f"r{i}00aaaa", cwd=stand, entrypoint="claude-desktop",
                 channel="chan_RADAR")

    placed = audit_folders(vault, {"chan_RADAR": "research-radar"})
    unmapped = audit_folders(vault, {})
    misfiled = audit_folders(vault, {"chan_RADAR": "somewhere-else"})

    assert placed["records"]["research-radar"]["findings"] == []
    assert _drift_folders(placed) == {}
    # without the mapping (or filed outside the mapped project) they ARE filed by cwd
    for report in (unmapped, misfiled):
        f = _kinds(report, "research-radar")["catch-all"]
        assert f["confidence"] == "high" and "not by channel mapping" in f["reason"]


def test_relay_only_folders_do_not_make_a_parent_look_like_a_catch_all(tmp_path):
    # Relay workers' stand-in cwds are not "projects below": two channel-mapped
    # folders whose workers stood in subdirectories of ~/Documents must not turn
    # a real folder at ~/Documents into a catch-all.
    vault = _vault(tmp_path)
    _session(vault, "Documents", "d100aaaa", cwd=HOME, entrypoint="cli")
    _session(vault, "teaching", "t100bbbb", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_T")
    _session(vault, "alpha", "o100cccc", cwd=f"{HOME}/Papers", entrypoint="claude-desktop",
             channel="chan_O")

    report = audit_folders(vault, {"chan_T": "teaching", "chan_O": "alpha"})

    assert report["records"]["Documents"]["findings"] == []


def test_two_folders_on_one_subdirectory_do_not_make_the_parent_a_catch_all(tmp_path):
    vault = _vault(tmp_path)
    parent = f"{HOME}/Papers"
    _session(vault, "Papers", "p100aaaa", cwd=parent, entrypoint="cli")
    _session(vault, "alpha", "o100bbbb", cwd=f"{parent}/alpha", entrypoint="cli")
    _session(vault, "alpha-internal", "s100cccc", cwd=f"{parent}/alpha", entrypoint="sdk-cli")

    report = audit_folders(vault)

    assert report["records"]["Papers"]["findings"] == []
    assert _kinds(report, "alpha-internal")["shared-cwd"]["confidence"] == "high"


def test_single_nested_project_is_not_a_catch_all(tmp_path):
    vault = _vault(tmp_path)
    _session(vault, "mono", "m100aaaa", cwd=f"{HOME}/Apps/mono", entrypoint="cli")
    _session(vault, "mono-sub", "s100bbbb", cwd=f"{HOME}/Apps/mono/sub", entrypoint="cli")

    report = audit_folders(vault)

    assert report["records"]["mono"]["findings"] == []


# --- B3(d): two folders, one cwd ------------------------------------------------

def test_seats_filed_beside_the_interactive_folder_of_one_cwd_are_high(tmp_path):
    vault = _vault(tmp_path)
    cwd = f"{HOME}/Papers/alpha"
    for i in range(2):
        _session(vault, "alpha", f"i{i}00aaaa", cwd=cwd, entrypoint="cli")
    for i in range(3):
        _session(vault, "alpha-internal", f"s{i}00bbbb", cwd=cwd, entrypoint="sdk-cli")

    report = audit_folders(vault)
    drift = _drift_folders(report)

    assert "alpha-internal" in drift and "alpha" not in drift
    f = _kinds(report, "alpha-internal")["shared-cwd"]
    assert f["confidence"] == "high" and f["target"] == "alpha"
    assert "3 sdk-cli seat session(s)" in f["reason"] and cwd in f["reason"]
    assert report["records"]["alpha"]["findings"] == []


def test_shared_cwd_target_is_deterministic_on_ties(tmp_path):
    # two "home" folders tie on session count: the reassign target must not depend
    # on set iteration (PYTHONHASHSEED), so the alphabetically first one wins
    vault = _vault(tmp_path)
    cwd = f"{HOME}/Apps/tied"
    _session(vault, "zeta-new", "z100aaaa", cwd=cwd, entrypoint="cli")
    _session(vault, "alpha-old", "a100bbbb", cwd=cwd, entrypoint="cli")
    _session(vault, "tied-seats", "s100cccc", cwd=cwd, entrypoint="sdk-cli")

    report = audit_folders(vault)

    assert _kinds(report, "tied-seats")["shared-cwd"]["target"] == "alpha-old"


def test_two_interactive_folders_on_one_cwd_are_review_on_both_sides(tmp_path):
    vault = _vault(tmp_path)
    cwd = f"{HOME}/Apps/renamed"
    _session(vault, "old-name", "o100aaaa", cwd=cwd, entrypoint="cli")
    _session(vault, "new-name", "n100bbbb", cwd=cwd, entrypoint="cli")
    _session(vault, "new-name", "n200bbbb", cwd=cwd, entrypoint="cli")

    report = audit_folders(vault)
    drift = _drift_folders(report)
    review = _review_folders(report, drift)

    assert drift == {}
    assert set(review) == {"old-name", "new-name"}
    assert "'new-name' (2)" in _kinds(report, "old-name")["shared-cwd"]["reason"]
    assert "'old-name' (1)" in _kinds(report, "new-name")["shared-cwd"]["reason"]


def test_relay_workers_sharing_a_cwd_with_a_project_are_not_shared_cwd(tmp_path):
    # Channel-mapped relay sessions land in their project while their cwd is a
    # place to stand: that is expected, not "two folders, one cwd".
    vault = _vault(tmp_path)
    stand = f"{HOME}/catchall"
    _session(vault, "catchall", "h100aaaa", cwd=stand, entrypoint="cli")
    _session(vault, "teaching", "r100bbbb", cwd=stand, entrypoint="claude-desktop")
    _session(vault, "teaching", "r200bbbb", cwd=stand, entrypoint="claude-desktop")

    report = audit_folders(vault)

    assert report["records"]["catchall"]["findings"] == []
    assert report["records"]["teaching"]["findings"] == []


def test_a_catch_all_cwd_is_not_also_reported_as_shared(tmp_path):
    vault = _vault(tmp_path)
    gh = f"{HOME}/GitHub"
    _session(vault, "GitHub", "h100aaaa", cwd=gh, entrypoint="cli")
    _session(vault, "elsewhere", "e100bbbb", cwd=gh, entrypoint="cli")
    _session(vault, "alpha", "a100cccc", cwd=f"{gh}/alpha", entrypoint="cli")
    _session(vault, "beta", "b100cccc", cwd=f"{gh}/beta", entrypoint="cli")

    report = audit_folders(vault)

    assert "shared-cwd" not in _kinds(report, "GitHub")
    assert "shared-cwd" not in _kinds(report, "elsewhere")
    assert "catch-all" in _kinds(report, "GitHub")


def test_cwd_can_come_from_md_frontmatter_when_the_jsonl_is_gone(tmp_path):
    vault = _vault(tmp_path)
    seat = "paperb-solo"
    _session(vault, "paperb", "i100aaaa", cwd=f"{HOME}/Apps/paperb", entrypoint="cli")
    md = _session(vault, seat, "s100bbbb", cwd=None, entrypoint="sdk-cli", jsonl=False)
    md.write_text(md.read_text().replace("project: ", f"source_cwd: {TMP_T}/{seat}\nproject: ", 1))

    report = audit_folders(vault)

    assert _kinds(report, seat)["fleet-seat"]["confidence"] == "high"


def test_relay_cwd_is_not_a_folders_identity(tmp_path):
    # A channel-mapped folder holds relay workers that "stood" in a catch-all cwd.
    # That cwd must not read as name≠canonical for the folder.
    vault = _vault(tmp_path)
    _session(vault, "teaching", "r100aaaa", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_TESTMAPPED")
    _session(vault, "research-radar", "r200bbbb", cwd=f"{HOME}/GitHub", entrypoint="claude-desktop")
    _session(vault, "research-radar", "n200bbbb", cwd=f"{HOME}/Apps/research-radar", entrypoint="cli",
             stamp="20261011-120000")

    report = audit_folders(vault)

    assert report["records"]["teaching"]["cwd"] is None
    assert not report["records"]["teaching"]["mismatch"]
    assert report["records"]["research-radar"]["cwd"] == f"{HOME}/Apps/research-radar"
    assert not report["records"]["research-radar"]["mismatch"]
    assert _review_folders(report, {}) == {}


def test_evicted_files_are_never_opened(tmp_path, monkeypatch):
    # iCloud dataless placeholders block on read: the old readers must skip them too
    vault = _vault(tmp_path)
    pd = vault / "projects" / "arena"
    memos = pd / "memos"
    memos.mkdir(parents=True)
    memo = memos / "m.md"
    memo.write_text(f"---\nsource_cwd: {HOME}/Apps/arena\n---\nbody\n", encoding="utf-8")
    md = _session(vault, "arena", "a100aaaa", cwd=f"{HOME}/Apps/arena", entrypoint="cli")
    jsonl = md.with_suffix(".jsonl")
    assert pa._cwd_for_project(pd) == f"{HOME}/Apps/arena"

    evicted = {memo, md, jsonl}
    monkeypatch.setattr(pa, "_readable", lambda path: path not in evicted)
    real_open = Path.open
    real_read_text = Path.read_text

    def guarded_open(self, *a, **kw):
        assert self not in evicted, f"opened an evicted file: {self}"
        return real_open(self, *a, **kw)

    def guarded_read_text(self, *a, **kw):
        assert self not in evicted, f"read an evicted file: {self}"
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert pa._read_source_cwd(memo) is None
    assert pa._cwd_for_project(pd) is None
    report = audit_folders(vault)
    assert report["records"]["arena"]["cwd"] is None


# --- clean vault + report text --------------------------------------------------

def _clean_vault(tmp_path: Path) -> Path:
    vault = _vault(tmp_path)
    _session(vault, "arena", "a100aaaa", cwd=f"{HOME}/Apps/arena", entrypoint="cli")
    _session(vault, "memex", "b100bbbb", cwd=f"{HOME}/Apps/memex", entrypoint="cli")
    _session(vault, "memex", "b200bbbb", cwd=f"{HOME}/Apps/memex", entrypoint="sdk-cli")
    _session(vault, "teaching", "r100cccc", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_ABC123")
    return vault


def test_clean_vault_reports_nothing(tmp_path, monkeypatch, capsys):
    vault = _clean_vault(tmp_path)
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)

    report = audit_folders(vault)
    assert _drift_folders(report) == {}
    assert _review_folders(report, {}) == {}
    assert all(r["findings"] == [] for r in report["records"].values())

    assert pa.run_folder_audit() == 0
    assert "No project-folder drift across 3 folders" in capsys.readouterr().out


def _drift_vault(tmp_path: Path) -> Path:
    vault = _vault(tmp_path)
    _session(vault, "paperb", "i100aaaa", cwd=f"{HOME}/Apps/paperb", entrypoint="cli")
    seat = "paperb-fixture-seat-x1"
    _session(vault, seat, "s100bbbb", cwd=f"{TMP_T}/{seat}", entrypoint="sdk-cli")
    return vault


def test_text_report_says_why_and_offers_the_reassign(tmp_path, monkeypatch, capsys):
    vault = _drift_vault(tmp_path)
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)

    assert pa.run_folder_audit() == 1
    out = capsys.readouterr().out

    assert "1 folder(s) with high-confidence drift" in out
    assert "paperb-fixture-seat-x1  [fleet-seat folder; name extends 'paperb']" in out
    assert "why: all 1 session(s) are sdk-cli fleet seats that ran from temp-dir cwds" in out
    assert 'memex obs reassign --from-prefix "projects/paperb-fixture-seat-x1/transcripts/"' in out
    assert 'to-prefix "projects/paperb/transcripts/"' in out


def test_json_report_carries_the_findings(tmp_path, monkeypatch, capsys):
    vault = _drift_vault(tmp_path)
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)

    assert pa.run_folder_audit(json_out=True) == 1
    data = json.loads(capsys.readouterr().out)

    assert data["drift_count"] == 1
    kinds = {f["kind"]: f for f in data["drift"]["paperb-fixture-seat-x1"]["findings"]}
    assert set(kinds) == {"fleet-seat", "name-extends"}
    assert kinds["name-extends"]["target"] == "paperb"
    assert data["drift"]["paperb-fixture-seat-x1"]["sessions"] == 1


# --- C3: unmapped relay channels ----------------------------------------------------

OPENING = (
    "## Turn 1\n\n### User\n\n<relay from=\"coordinator\">go</relay>\n\n"
    "## Turn 2\n\n### User\n\n<wake reason=\"mention\">\n  {tag}\n  </project>\n</wake>\n"
)


def _relay_vault(tmp_path: Path) -> Path:
    vault = _vault(tmp_path)
    # frontmatter channel (v0.22.0 archives), unmapped, filed in two catch-alls
    _session(vault, "Dropbox", "f100aaaa", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_UNMAPPEDONE", stamp="20261011-100000")
    _session(vault, "Dropbox", "f200aaaa", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_UNMAPPEDONE", stamp="20261012-100000")
    _session(vault, "GitHub", "f300aaaa", cwd=f"{HOME}/GitHub", entrypoint="claude-desktop",
             channel="chan_UNMAPPEDONE", stamp="20261013-100000")
    # earlier archive: channel only in the raw opening turns (raw, escaped, json-escaped)
    raw = '<project id="chan_UNMAPPEDTWO" type="project">'
    html = "&lt;project id=&quot;chan_UNMAPPEDTWO&quot; type=&quot;project&quot;&gt;"
    jsn = '<project id=\\"chan_UNMAPPEDTWO\\" type=\\"project\\">'
    for i, tag in enumerate((raw, html, jsn)):
        _session(vault, "Papers", f"o{i}00bbbb", cwd=f"{HOME}/Papers", entrypoint="claude-desktop",
                 body=OPENING.format(tag=tag), stamp=f"2026101{i}-110000")
    # a mapped channel: one session in its project, one archived in a catch-all
    _session(vault, "teaching", "t100cccc", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_TESTMAPPED")
    _session(vault, "Dropbox", "t200cccc", cwd=f"{HOME}/Dropbox", entrypoint="claude-desktop",
             channel="chan_TESTMAPPED", stamp="20261014-100000")
    return vault


MAPPINGS = {"chan_TESTMAPPED": "teaching"}


def test_channels_group_per_channel_with_counts_and_folders(tmp_path):
    vault = _relay_vault(tmp_path)

    report = audit_channels(vault, MAPPINGS)
    by = {e["channel"]: e for e in report["unmapped"]}

    assert set(by) == {"chan_UNMAPPEDONE", "chan_UNMAPPEDTWO"}
    one = by["chan_UNMAPPEDONE"]
    assert one["sessions"] == 3
    assert one["folders"] == {"Dropbox": 2, "GitHub": 1}
    assert one["sources"] == {"frontmatter": 3}
    assert (one["first"], one["last"]) == ("2026-10-10", "2026-10-10")  # start_time of the fixture
    two = by["chan_UNMAPPEDTWO"]
    assert two["sessions"] == 3 and two["folders"] == {"Papers": 3}
    assert two["sources"] == {"opening-turns": 3}
    assert [e["channel"] for e in report["unmapped"]] == ["chan_UNMAPPEDONE", "chan_UNMAPPEDTWO"]
    assert report["relay_sessions"] == 8


def test_mapped_channel_is_not_listed_but_counted(tmp_path):
    vault = _relay_vault(tmp_path)

    report = audit_channels(vault, MAPPINGS)

    assert [e["channel"] for e in report["mapped"]] == ["chan_TESTMAPPED"]
    mapped = report["mapped"][0]
    assert mapped["mapped_to"] == "teaching" and mapped["sessions"] == 2
    assert mapped["misfiled"] == 1  # one archived in Dropbox before the mapping
    assert "chan_TESTMAPPED" not in {e["channel"] for e in report["unmapped"]}


def test_blank_or_non_string_mapping_does_not_count_as_mapped(tmp_path):
    vault = _relay_vault(tmp_path)

    report = audit_channels(vault, {"chan_TESTMAPPED": "  ", "chan_UNMAPPEDONE": None})

    assert "chan_TESTMAPPED" in {e["channel"] for e in report["unmapped"]}
    assert "chan_UNMAPPEDONE" in {e["channel"] for e in report["unmapped"]}
    assert report["mapped"] == []


def test_opening_turn_scan_is_bounded_and_needs_a_claude_desktop_session(tmp_path):
    vault = _vault(tmp_path)
    tag = '<project id="chan_LATEQUOTE" type="project">'
    # a relay session that only quotes a channel id after turn 3 -> unresolved
    late = "## Turn 1\n\nx\n\n## Turn 2\n\nx\n\n## Turn 3\n\nx\n\n## Turn 4\n\n" + tag + "\n"
    _session(vault, "Dropbox", "l100aaaa", entrypoint="claude-desktop", body=late)
    # an interactive session discussing a channel id in turn 2 -> not a relay session
    _session(vault, "memex", "i100bbbb", entrypoint="cli", body=OPENING.format(tag=tag))
    # a claude-desktop session with no tag at all -> unresolved
    _session(vault, "Dropbox", "n100cccc", entrypoint="claude-desktop")

    report = audit_channels(vault, {})

    assert report["unmapped"] == [] and report["mapped"] == []
    assert report["relay_sessions"] == 2
    assert len(report["unresolved"]) == 2
    assert all(p.startswith("projects/Dropbox/transcripts/") for p in report["unresolved"])


def test_garbage_frontmatter_channel_falls_back_to_the_body(tmp_path):
    vault = _vault(tmp_path)
    _session(vault, "Dropbox", "g100aaaa", entrypoint="claude-desktop", channel="not-a-channel",
             body=OPENING.format(tag='<project id="chan_FROMBODY" type="project">'))

    report = audit_channels(vault, {})

    assert [e["channel"] for e in report["unmapped"]] == ["chan_FROMBODY"]
    assert report["unmapped"][0]["sources"] == {"opening-turns": 1}


def test_channel_date_span_comes_from_start_time_not_the_archive_day(tmp_path):
    vault = _vault(tmp_path)
    md = _session(vault, "Dropbox", "d100aaaa", entrypoint="claude-desktop", channel="chan_NOSTART")
    # a one-timestamp session has no start_time; frontmatter `date` is the archive day
    md.write_text(md.read_text().replace("start_time: 2026-10-10T12:00:00\n", ""))

    entry = audit_channels(vault, {})["unmapped"][0]

    assert (entry["first"], entry["last"]) == (None, None)


def test_unreadable_transcripts_make_the_channel_audit_incomplete_not_clean(
    tmp_path, monkeypatch, capsys
):
    vault = _vault(tmp_path)
    evicted = _session(vault, "Dropbox", "e100aaaa", entrypoint="claude-desktop", channel="chan_GONE")
    _session(vault, "teaching", "t100bbbb", entrypoint="claude-desktop", channel="chan_TESTMAPPED")
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {"channel_mappings": MAPPINGS})
    monkeypatch.setattr(pa, "_readable", lambda path: path != evicted)

    report = audit_channels(vault, MAPPINGS)
    assert report["unreadable"] == ["projects/Dropbox/transcripts/20261010-120000-e100aaaa.md"]
    assert report["unmapped"] == []  # the evicted one could not be classified

    assert pa.run_channel_audit() == 1  # NOT a green all-clear
    out = capsys.readouterr().out
    assert "All 1 relay channel(s)" in out and "INCOMPLETE" in out
    assert pa.run_channel_audit(json_out=True) == 1
    assert json.loads(capsys.readouterr().out)["unreadable"] == report["unreadable"]


def test_only_unresolved_sessions_do_not_print_an_all_zero_all_clear(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _session(vault, "Dropbox", "n100aaaa", entrypoint="claude-desktop")  # no tag anywhere
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {})

    assert pa.run_channel_audit() == 0
    out = capsys.readouterr().out
    assert "All 0" not in out and "✓" not in out
    assert "1 claude-desktop session(s) with no channel" in out


def test_channel_report_text_and_exit_code(tmp_path, monkeypatch, capsys):
    vault = _relay_vault(tmp_path)
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {"channel_mappings": MAPPINGS})

    assert pa.run_channel_audit() == 1
    out = capsys.readouterr().out

    assert "2 claude.ai channel(s) not in channel_mappings — 6 relay session(s)" in out
    assert "chan_UNMAPPEDONE  (3 session(s), 2026-10-10)" in out
    assert "filed in: Dropbox (2), GitHub (1)" in out
    assert "channel read: 3 from opening-turns" in out  # chan_UNMAPPEDTWO
    assert '"chan_UNMAPPEDONE": "<project>"' in out
    assert "Mapped: 1 channel(s), 2 session(s); 1 filed outside their mapped project" in out
    assert "chan_TESTMAPPED  (" not in out


def test_channel_report_json(tmp_path, monkeypatch, capsys):
    vault = _relay_vault(tmp_path)
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {"channel_mappings": MAPPINGS})

    assert pa.run_channel_audit(json_out=True) == 1
    data = json.loads(capsys.readouterr().out)

    assert data["unmapped_channel_count"] == 2
    assert data["unmapped_session_count"] == 6
    assert data["mapped"][0]["channel"] == "chan_TESTMAPPED"
    first = data["unmapped"][0]
    assert first["channel"] == "chan_UNMAPPEDONE" and len(first["paths"]) == 3


def test_all_mapped_vault_reports_nothing_to_decide(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _session(vault, "teaching", "t100cccc", entrypoint="claude-desktop", channel="chan_TESTMAPPED")
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {"channel_mappings": MAPPINGS})

    assert pa.run_channel_audit() == 0
    assert "All 1 relay channel(s) are in channel_mappings (1 session(s))." in capsys.readouterr().out


def test_all_mapped_but_misfiled_sessions_are_still_told(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _session(vault, "Dropbox", "t100cccc", entrypoint="claude-desktop", channel="chan_TESTMAPPED")
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {"channel_mappings": MAPPINGS})

    assert pa.run_channel_audit() == 0  # nothing to DECIDE; the misfiled one is information
    assert "1 filed outside their mapped project" in capsys.readouterr().out


def test_vault_without_relay_sessions(tmp_path, monkeypatch, capsys):
    vault = _clean_vault(tmp_path / "v")
    # drop the one relay session of the shared clean fixture
    for p in (vault / "projects" / "teaching" / "transcripts").iterdir():
        p.unlink()
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {})

    assert pa.run_channel_audit() == 0
    assert "No claude.ai relay sessions in the vault" in capsys.readouterr().out
    assert pa.run_channel_audit(json_out=True) == 0


def test_main_default_is_folders_and_both_flags_merge_json(tmp_path, monkeypatch, capsys):
    vault = _relay_vault(tmp_path)
    monkeypatch.setattr(pa, "get_memex_path", lambda: vault)
    monkeypatch.setattr(pa, "get_config", lambda: {"channel_mappings": MAPPINGS})

    monkeypatch.setattr("sys.argv", ["project_audit", "--folders", "--channels", "--json"])
    with pytest.raises(SystemExit) as exc:
        pa.main()
    data = json.loads(capsys.readouterr().out)

    assert exc.value.code == 1
    assert set(data) == {"folders", "channels"}
    assert data["channels"]["unmapped_channel_count"] == 2
    assert "drift_count" in data["folders"]


# --- the CLI hunk: ``memex check --channels`` ------------------------------------------

@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    vault = _relay_vault(tmp_path / "vault")
    state = tmp_path / "state"
    state.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMEX_")}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.update({
        "MEMEX_MEMEX_PATH": str(vault),
        "MEMEX_STATE_DIR": str(state),
        "MEMEX_INDEX_PATH": str(state / "_index.sqlite"),
        "MEMEX_EMBEDDINGS__ENABLED": "false",
        "MEMEX_CHANNEL_MAPPINGS": json.dumps(MAPPINGS),
        "HOME": str(tmp_path / "home"),
        "UV_NO_SYNC": "1",
    })
    (tmp_path / "home").mkdir()
    return env


def test_cli_check_channels(tmp_path, cli_env):
    result = subprocess.run([str(CLI), "check", "--channels"], cwd=tmp_path, env=cli_env,
                            capture_output=True, text=True)

    assert result.returncode == 1, result.stderr
    assert "2 claude.ai channel(s) not in channel_mappings" in result.stdout
    assert "chan_UNMAPPEDTWO" in result.stdout

    as_json = subprocess.run([str(CLI), "check", "--channels", "--json"], cwd=tmp_path,
                             env=cli_env, capture_output=True, text=True)
    assert as_json.returncode == 1, as_json.stderr
    assert json.loads(as_json.stdout)["unmapped_session_count"] == 6


def test_cli_check_channels_refuses_flags_it_would_silently_drop(tmp_path, cli_env):
    result = subprocess.run([str(CLI), "check", "--signals", "--channels"], cwd=tmp_path,
                            env=cli_env, capture_output=True, text=True)

    assert result.returncode == 2
    assert "--channels combines only with --folders and --json" in result.stderr
    assert "chan_UNMAPPEDONE" not in result.stdout  # it did not run a half audit


def test_a_read_failure_after_the_stat_check_is_unreadable_not_clean(tmp_path, monkeypatch):
    """_readable passes, then open() fails: the audit must not read the empty
    result as 'not a relay' (gauntlet round 1, Kimi lane)."""
    vault = _vault(tmp_path)
    broken = _session(vault, "Dropbox", "f100aaaa", entrypoint="claude-desktop", channel="chan_GONE")
    real_open = Path.open

    def failing_open(self, *a, **k):
        if self == broken:
            raise PermissionError("denied")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", failing_open)
    report = audit_channels(vault, MAPPINGS)
    assert report["unreadable"] == ["projects/Dropbox/transcripts/20261010-120000-f100aaaa.md"]
