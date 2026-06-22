"""Tests for the project-folder drift audit (memex check --folders) and the
shared fragment/tripwire helpers in utils."""

from __future__ import annotations

from pathlib import Path

from memex.scripts.project_audit import (
    _content_keys,
    _read_source_cwd,
    audit_folders,
    _drift_folders,
    _review_folders,
)
from memex.scripts.utils import looks_like_cwd_fragment, warn_if_noncanonical


def _memo(project_dir: Path, name: str, source_cwd: str | None = None, body: str = "x") -> None:
    (project_dir / "memos").mkdir(parents=True, exist_ok=True)
    fm = ["---", "type: memo", f"project: {project_dir.name}"]
    if source_cwd:
        fm.append(f"source_cwd: {source_cwd}")
    fm += ["---", "", body, ""]
    (project_dir / "memos" / name).write_text("\n".join(fm), encoding="utf-8")


# --- utils helpers ----------------------------------------------------------

def test_looks_like_cwd_fragment() -> None:
    assert looks_like_cwd_fragment("Apps-arena")
    assert looks_like_cwd_fragment("GitHub-loom")
    assert looks_like_cwd_fragment("Library-CloudStorage-Dropbox-x")
    assert not looks_like_cwd_fragment("arena")
    assert not looks_like_cwd_fragment("mcp-workspace")
    # user/vault-specific tokens are intentionally NOT in the shipped list (v0.15.6)
    assert not looks_like_cwd_fragment("linxule-Papers-predictive-ai")


def test_warn_if_noncanonical() -> None:
    # cwd present + canonical match → no warn
    assert warn_if_noncanonical("arena", "/Users/x/Documents/Apps/arena", "t") is False
    # cwd present + mismatch → warn
    assert warn_if_noncanonical("wrong-name", "/Users/x/Documents/Apps/arena", "t") is True
    # no cwd + fragment-shaped name → warn
    assert warn_if_noncanonical("Apps-arena", None, "t") is True
    # no cwd + canonical-looking name → no warn
    assert warn_if_noncanonical("arena", None, "t") is False


# --- project_audit pieces ---------------------------------------------------

def test_read_source_cwd(tmp_path: Path) -> None:
    pd = tmp_path / "arena"
    _memo(pd, "m.md", source_cwd="/Users/x/Documents/Apps/arena")
    assert _read_source_cwd(pd / "memos" / "m.md") == "/Users/x/Documents/Apps/arena"
    _memo(pd, "n.md")  # no source_cwd
    assert _read_source_cwd(pd / "memos" / "n.md") is None


def test_content_keys_normalizes_transcript_dates(tmp_path: Path) -> None:
    pd = tmp_path / "arena"
    _memo(pd, "2026-01-01-note.md")
    (pd / "transcripts").mkdir()
    (pd / "transcripts" / "20260101-120000-abc123.md").write_text("t", encoding="utf-8")
    keys = _content_keys(pd)
    assert "2026-01-01-note.md" in keys
    assert "session:abc123" in keys  # date prefix stripped


def test_audit_flags_fragment(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    pd = proj / "Apps-arena"
    _memo(pd, "m.md", source_cwd="/Users/x/Documents/Apps/arena")
    report = audit_folders(tmp_path)
    drift = _drift_folders(report)
    assert "Apps-arena" in drift
    assert report["records"]["Apps-arena"]["is_fragment"]


def test_audit_flags_subset_duplicate(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    _memo(proj / "linxule_com", "a.md")
    _memo(proj / "linxule_com", "b.md")
    _memo(proj / "personal_website", "a.md")  # strict subset of linxule_com
    report = audit_folders(tmp_path)
    drift = _drift_folders(report)
    assert "personal_website" in drift
    assert "linxule_com" in report["records"]["personal_website"]["subset_of"]


def test_audit_equal_folders_flag_one_side_only(tmp_path: Path) -> None:
    # Two folders with IDENTICAL content must not each claim the other (which
    # would print circular A→B and B→A reassigns). Exactly one is flagged,
    # deterministically (a > b → 'beta' lists 'alpha').
    proj = tmp_path / "projects"
    _memo(proj / "alpha", "x.md")
    _memo(proj / "beta", "x.md")
    report = audit_folders(tmp_path)
    drift = _drift_folders(report)
    flagged = [n for n in ("alpha", "beta") if n in drift]
    assert flagged == ["beta"]
    assert report["records"]["beta"]["subset_of"] == ["alpha"]
    assert report["records"]["alpha"]["subset_of"] == []


def test_audit_clean_canonical_folders(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    # distinct, non-fragment, non-overlapping folders → no high-confidence drift
    _memo(proj / "arena", "a.md", source_cwd="/Users/x/Documents/Apps/arena")
    _memo(proj / "memex", "b.md", source_cwd="/Users/x/Documents/Apps/memex")
    report = audit_folders(tmp_path)
    assert _drift_folders(report) == {}


def test_mismatch_is_review_not_drift(tmp_path: Path) -> None:
    # A non-fragment folder whose recorded cwd canonicalizes to a different name
    # is REVIEW (lower confidence), not high-confidence drift.
    proj = tmp_path / "projects"
    _memo(proj / "carrel", "a.md", source_cwd="/Users/x/Documents/Apps/somethingelse")
    report = audit_folders(tmp_path)
    drift = _drift_folders(report)
    review = _review_folders(report, drift)
    assert "carrel" not in drift
    assert "carrel" in review
