"""`memex project move` (A5, v0.22.0): one command for the consolidation loop.

Every fixture is synthetic: a throwaway vault (git-initialised where the test
needs it), its in-vault index, and HOME pointed at tmp so the rebuild lock
never lands in the real ~/.memex.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from memex.db_utils import connect_index
from memex.extract import Observation, store_observations
from memex.scripts import index_rebuild as ir
from memex.scripts import project_move as pm
from memex.scripts.search import search

from hook_helpers import _real_uv_cache_dir

ROOT = Path(__file__).resolve().parents[1]
SRC, DST = "GitHub", "alpha"
# Resolved now, with the real HOME: the autouse fixture moves HOME per test.
_UV_CACHE = _real_uv_cache_dir()

MEMO_A = "projects/GitHub/memos/2026-10-10-alpha.md"
MEMO_B = "projects/GitHub/memos/2026-10-11-beta.md"
TRANSCRIPT = "projects/GitHub/transcripts/2026-10-12-abcd1234.md"
RAW = "projects/GitHub/transcripts/2026-10-12-abcd1234.jsonl"
STUB_PATH = "projects/GitHub/_project.md"
TOPIC = "topics/linking.md"

STUB = (
    "---\ntype: project\nname: GitHub\ncreated: 2026-10-01\n---\n\n# GitHub\n\n"
    "## Overview\n\nProject workspace: `/work/GitHub`\n\n## Session History\n\n"
    "<!-- Transcripts and memos will accumulate in this project folder -->\n"
)


def _moved(path: str) -> str:
    return path.replace(f"projects/{SRC}/", f"projects/{DST}/", 1)


def _sidecar(path: str) -> str:
    return path[: -len(".md")] + ".obs.jsonl"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # rebuild_lock -> ~/.memex/locks
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }.items():
        monkeypatch.setenv(key, value)


def _git(vault: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=vault, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _memo(title: str, word: str) -> str:
    return (
        f"---\ntype: memo\ntitle: {title}\ndate: 2026-10-10\nproject: {SRC}\n---\n\n"
        f"# {title}\n\nWe chose the {word} approach.\n"
    )


def _build(tmp_path: Path, *, git: bool = True, stub: str | None = STUB, extra: dict | None = None):
    vault = tmp_path / "vault"
    files = {
        MEMO_A: _memo("Alpha decision", "zebra"),
        MEMO_B: _memo("Beta decision", "quokka"),
        TRANSCRIPT: (
            f"---\ntype: transcript\ndate: 2026-10-12\nproject: {SRC}\n---\n\n"
            "# Session: abcd1234...\n\n## User\n\nTell me about the walrus.\n"
        ),
        RAW: '{"type": "user", "message": {"role": "user", "content": "walrus"}}\n',
        TOPIC: (
            "---\ntype: concept\ntitle: Linking\n---\n\n"
            "See [[projects/GitHub/memos/2026-10-10-alpha]] for the decision.\n"
        ),
        **(extra or {}),
    }
    if stub is not None:
        files[STUB_PATH] = stub
    for rel, text in files.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text)
    (vault / ".gitignore").write_text(
        "projects/*/transcripts/\nprojects/*/auto-memory/\n_index*.sqlite*\n"
    )

    ir.rebuild_full(vault, with_embeddings=False)
    index = vault / "_index.sqlite"
    assert index.exists(), "a tmp vault must keep its index in-vault"
    store_observations(
        index, MEMO_A,
        [
            Observation("Alpha picked the zebra approach", "explicit", "high", topics=["release-practice"]),
            Observation("Alpha leaves a follow-up open", "explicit", "high"),
        ],
        None, mode="replace", vault=vault,
    )
    store_observations(
        index, MEMO_B, [Observation("Beta picked the quokka approach", "explicit", "high")],
        None, mode="replace", vault=vault,
    )
    if git:
        _git(vault, "init", "-q")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-q", "-m", "base")
    return vault, index


def _obs_total(index: Path) -> int:
    with closing(connect_index(index)) as conn:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def _snapshot(vault: Path, index: Path) -> dict:
    """Every vault file's bytes + every path-keyed index row."""
    files = {
        str(p.relative_to(vault)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(vault.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(vault).parts and not p.name.startswith("_index")
    }
    with closing(connect_index(index)) as conn:
        rows = {
            "fts": sorted(conn.execute("SELECT path, project FROM fts_content").fetchall()),
            "hashes": sorted(conn.execute("SELECT path, content_hash FROM doc_hashes").fetchall()),
            "obs": sorted(conn.execute("SELECT doc_path, content_hash FROM observations").fetchall()),
            "sidecars": sorted(conn.execute("SELECT doc_path, content_hash FROM obs_sidecars").fetchall()),
            "chunks": sorted(conn.execute("SELECT doc_path, doc_project, chunk_index FROM chunks").fetchall()),
        }
    return {"files": files, "rows": rows}


# ── the whole loop ──────────────────────────────────────────────────────────


def test_move_keeps_every_observation_and_the_next_rebuild_sees_no_deleted_or_new(tmp_path, capsys):
    vault, index = _build(tmp_path)
    before = _obs_total(index)
    assert before == 3

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 0
    out = capsys.readouterr().out

    for old in (MEMO_A, MEMO_B, TRANSCRIPT, RAW, _sidecar(MEMO_A), _sidecar(MEMO_B)):
        assert not (vault / old).exists(), old
        assert (vault / _moved(old)).exists(), old
    assert not (vault / "projects" / SRC).exists(), "template stub removed, empty folder pruned"
    for doc in (MEMO_A, MEMO_B, TRANSCRIPT):
        assert f"\nproject: {DST}\n" in (vault / _moved(doc)).read_text()

    status = _git(vault, "status", "--porcelain")
    assert f"{MEMO_A} -> {_moved(MEMO_A)}" in status  # a rename, so history follows
    assert f"{_sidecar(MEMO_B)} -> {_moved(_sidecar(MEMO_B))}" in status
    assert f"D  {STUB_PATH}" in status

    with closing(connect_index(index)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before
        assert sorted(conn.execute(
            "SELECT path, project FROM fts_content WHERE path LIKE 'projects/%'"
        ).fetchall()) == sorted((_moved(d), DST) for d in (MEMO_A, MEMO_B, TRANSCRIPT))
        assert {r[0] for r in conn.execute("SELECT DISTINCT doc_path FROM observations")} == {
            _moved(MEMO_A), _moved(MEMO_B)
        }
        assert {r[0] for r in conn.execute("SELECT doc_path FROM obs_sidecars")} == {
            _moved(MEMO_A), _moved(MEMO_B)
        }
        assert {r[0] for r in conn.execute("SELECT DISTINCT doc_project FROM chunks WHERE doc_path LIKE 'projects/%'")} == {DST}
        searchable = conn.execute(
            "SELECT content FROM fts_content WHERE path = ?", (_moved(MEMO_A),)
        ).fetchone()[0]
        assert f"project: {DST}" in searchable and f"project: {SRC}" not in searchable

    # Search is right immediately, before any rebuild.
    hits = search(vault, "zebra")
    assert [(h["path"], h["project"]) for h in hits if h["type"] == "memo"] == [(_moved(MEMO_A), DST)]
    assert "1 wikilink(s) resolve to a moved path" in out and TOPIC in out

    stats = ir.rebuild_incremental(vault, with_embeddings=False)
    assert (stats["deleted"], stats["new"]) == (0, 0)
    assert stats["updated"] == 3, "only the three restamped documents change"
    assert stats["sidecars"]["ingested"] == 0 and stats["sidecars"]["unchanged"] == 2
    assert _obs_total(index) == before


def test_dry_run_changes_nothing(tmp_path, capsys):
    vault, index = _build(tmp_path)
    snapshot = _snapshot(vault, index)

    assert pm.run(vault, SRC, DST, index_path=index) == 0

    out = capsys.readouterr().out
    assert "Would move projects/GitHub/ → projects/alpha/" in out
    assert "template stub would be removed" in out
    assert "3 file(s) restamped" in out
    assert "(dry run" in out
    assert _snapshot(vault, index) == snapshot
    assert _git(vault, "status", "--porcelain") == ""


def test_works_without_git(tmp_path):
    vault, index = _build(tmp_path, git=False)

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 0

    assert (vault / _moved(_sidecar(MEMO_A))).exists()
    assert not (vault / STUB_PATH).exists()
    assert _obs_total(index) == 3
    stats = ir.rebuild_incremental(vault, with_embeddings=False)
    assert (stats["deleted"], stats["new"]) == (0, 0)


# ── refusals: nothing is touched ────────────────────────────────────────────


def test_refuses_when_a_target_file_exists(tmp_path, capsys):
    vault, index = _build(tmp_path)
    (vault / _moved(MEMO_B)).parent.mkdir(parents=True)
    (vault / _moved(MEMO_B)).write_text("somebody else's memo\n")
    snapshot = _snapshot(vault, index)

    assert pm.run(vault, SRC, DST, index_path=index) == 3  # the dry run says so too
    assert _moved(MEMO_B) in capsys.readouterr().out
    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 3

    assert "refusing" in capsys.readouterr().err
    assert _snapshot(vault, index) == snapshot


def test_refuses_when_the_index_already_has_the_target_path(tmp_path, capsys):
    vault, index = _build(tmp_path)
    with closing(connect_index(index)) as conn:
        # fts_content has no UNIQUE constraint: without the explicit check the
        # move would silently give one path two document rows.
        conn.execute(
            "INSERT INTO fts_content (path, title, content, type, project, date) "
            "VALUES (?, 'ghost', 'ghost', 'memo', ?, '')",
            (_moved(MEMO_A), DST),
        )
        conn.commit()
    snapshot = _snapshot(vault, index)

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 3

    assert "already have index rows" in capsys.readouterr().err
    assert _snapshot(vault, index) == snapshot


def test_refuses_a_malformed_sidecar(tmp_path, capsys):
    vault, index = _build(tmp_path)
    (vault / _sidecar(MEMO_B)).write_text("{not json\n")
    snapshot = _snapshot(vault, index)

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 1

    assert "malformed sidecar" in capsys.readouterr().err
    assert _snapshot(vault, index) == snapshot


def test_bad_names_are_refused(tmp_path):
    vault, index = _build(tmp_path, git=False)
    assert pm.run(vault, SRC, "../escape", index_path=index) == 1
    assert pm.run(vault, SRC, SRC, index_path=index) == 1
    assert pm.run(vault, SRC, "github", index_path=index) == 1  # one folder on APFS
    assert pm.run(vault, "nope", DST, index_path=index) == 1


def test_names_must_match_the_folders_case(tmp_path, capsys):
    """git and the index are case-sensitive even where the volume is not: a
    wrongly-cased name would move files while matching no index row."""
    vault, index = _build(tmp_path, git=False)
    (vault / "projects" / "Alpha").mkdir()
    snapshot = _snapshot(vault, index)

    assert pm.run(vault, "github", DST, apply=True, index_path=index) == 1
    assert "did you mean 'GitHub'" in capsys.readouterr().err
    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 1  # twin of 'Alpha'
    assert "move into 'Alpha' instead" in capsys.readouterr().err
    assert _snapshot(vault, index) == snapshot


def test_busy_index_is_its_own_exit_code(tmp_path, monkeypatch, capsys):
    vault, index = _build(tmp_path, git=False)
    from memex import db_utils

    def busy(timeout=None):
        raise SystemExit(3)  # what rebuild_lock does under contention

    monkeypatch.setattr(db_utils, "rebuild_lock", busy)
    capsys.readouterr()  # drop the fixture's rebuild chatter

    assert pm.run(vault, SRC, DST, apply=True, index_path=index, as_json=True) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["exit_code"] == 4
    assert "index is busy" in captured.err
    assert (vault / MEMO_A).exists()


# ── the order learned on 2026-10-28 ─────────────────────────────────────────


def test_hand_edited_sidecar_is_ingested_before_the_reassign(tmp_path, capsys):
    vault, index = _build(tmp_path)
    sidecar = vault / _sidecar(MEMO_A)
    lines = sidecar.read_text().splitlines()
    edited = None
    for i, line in enumerate(lines):
        obj = json.loads(line)
        if obj["content"] == "Alpha picked the zebra approach":
            obj["content"] = "Alpha picked the zebra approach after a second vote"  # hash left stale
            lines[i] = json.dumps(obj, sort_keys=True)
            edited = obj["content"]
    assert edited
    sidecar.write_text("\n".join(lines) + "\n")
    edited_bytes = sidecar.read_bytes()

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 0
    assert "Ingested 1 changed sidecar(s) first" in capsys.readouterr().out

    # The edit reached the index at the new path BEFORE any rebuild ...
    with closing(connect_index(index)) as conn:
        contents = {r[0] for r in conn.execute(
            "SELECT content FROM observations WHERE doc_path = ?", (_moved(MEMO_A),)
        )}
    assert edited in contents and "Alpha picked the zebra approach" not in contents
    # ... and the vault file still carries it, byte for byte.
    assert (vault / _moved(_sidecar(MEMO_A))).read_bytes() == edited_bytes
    assert _obs_total(index) == 3  # one line replaced, none lost

    stats = ir.rebuild_incremental(vault, with_embeddings=False)
    assert stats["sidecars"]["ingested"] == 0, "the ingest was recorded; nothing left to do"


# ── _project.md ─────────────────────────────────────────────────────────────


def test_project_md_with_content_is_kept(tmp_path, capsys):
    vault, index = _build(tmp_path, stub=STUB + "\n## Decisions\n\n- keep the overview\n")

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 0

    out = capsys.readouterr().out
    assert f"kept {STUB_PATH}: it has content" in out
    assert (vault / STUB_PATH).exists()
    with closing(connect_index(index)) as conn:
        assert conn.execute(
            "SELECT project FROM fts_content WHERE path = ?", (STUB_PATH,)
        ).fetchall() == [(SRC,)]
    stats = ir.rebuild_incremental(vault, with_embeddings=False)
    assert (stats["deleted"], stats["new"]) == (0, 0)


def test_template_stub_matches_what_session_end_writes(tmp_path, monkeypatch):
    """Drift guard: the stub regex must recognise the hook's real output."""
    spec = importlib.util.spec_from_file_location("session_end_for_stub", ROOT / "hooks" / "session-end.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook, "log_info", lambda *args: None)
    path = tmp_path / "_project.md"

    hook.create_project_meta(path, "some-project", "/work/some-project")
    text = path.read_text()

    assert pm.is_template_stub(text)
    assert not pm.is_template_stub(text + "\n## Decisions\n\n- a real line\n")
    assert not pm.is_template_stub(text.replace("## Overview\n\n", "## Overview\n\nWritten by hand.\n\n"))


# ── --file ──────────────────────────────────────────────────────────────────


def test_file_mode_moves_exactly_one_document(tmp_path):
    lookalike = "projects/GitHub/memos/2026-10-10-alpha.md-notes.md"  # MEMO_A's path is its prefix
    vault, index = _build(tmp_path, extra={lookalike: _memo("Alpha notes", "ibis")})

    assert pm.run(vault, SRC, DST, files=[MEMO_A], apply=True, index_path=index) == 0

    assert (vault / _moved(MEMO_A)).exists() and (vault / _moved(_sidecar(MEMO_A))).exists()
    assert (vault / MEMO_B).exists() and (vault / lookalike).exists()
    assert (vault / STUB_PATH).exists(), "--file never removes the source _project.md"
    with closing(connect_index(index)) as conn:
        assert conn.execute(
            "SELECT project FROM fts_content WHERE path = ?", (lookalike,)
        ).fetchall() == [(SRC,)]
    assert _obs_total(index) == 3
    stats = ir.rebuild_incremental(vault, with_embeddings=False)
    assert (stats["deleted"], stats["new"], stats["updated"]) == (0, 0, 1)


def test_document_in_a_skipped_folder_keeps_its_index_rows(tmp_path, capsys):
    """The index follows the files exactly: a document the walk skips (a
    dot-folder like SessionEnd's `.archive-*` staging) stays, rows and all."""
    hidden = "projects/GitHub/transcripts/.archive-x/2026-10-13-ffff0000.md"
    vault, index = _build(tmp_path, git=False, extra={
        hidden: f"---\ntype: transcript\nproject: {SRC}\n---\n\nstaged walrus\n",
    })
    with closing(connect_index(index)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM doc_hashes WHERE path = ?", (hidden,)).fetchone()[0] == 1

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 0

    out = capsys.readouterr().out
    assert hidden in out and "(on disk)" in out
    assert (vault / hidden).exists()
    with closing(connect_index(index)) as conn:
        assert conn.execute("SELECT project FROM fts_content WHERE path = ?", (hidden,)).fetchall() == [(SRC,)]
    stats = ir.rebuild_incremental(vault, with_embeddings=False)
    assert (stats["deleted"], stats["new"]) == (0, 0)


def test_file_mode_takes_the_raw_transcript_along(tmp_path):
    vault, index = _build(tmp_path, git=False)

    assert pm.run(vault, SRC, DST, files=["transcripts/2026-10-12-abcd1234.md"], apply=True, index_path=index) == 0

    assert (vault / _moved(TRANSCRIPT)).exists() and (vault / _moved(RAW)).exists()
    assert not (vault / RAW).exists()


# ── failure is undone ───────────────────────────────────────────────────────


def test_a_failure_mid_move_puts_everything_back(tmp_path, monkeypatch, capsys):
    vault, index = _build(tmp_path)
    snapshot = _snapshot(vault, index)
    real = pm.restamp_project
    calls = {"n": 0}

    def flaky(path, project):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (simulated)")
        return real(path, project)

    monkeypatch.setattr(pm, "restamp_project", flaky)

    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 1

    assert "move failed and was undone" in capsys.readouterr().err
    assert _snapshot(vault, index) == snapshot
    assert _git(vault, "status", "--porcelain") == ""


# ── restamp ─────────────────────────────────────────────────────────────────


def test_restamp_touches_only_the_value(tmp_path):
    path = tmp_path / "m.md"
    original = (
        b'---\r\ntype: memo\r\nproject: "GitHub"   \r\ntitle: x\r\n---\r\n\r\n'
        b"project: GitHub stays in the body\r\n"
    )
    path.write_bytes(original)

    assert pm.restamp_project(path, DST) == b'"GitHub"'
    assert path.read_bytes() == original.replace(b'"GitHub"   \r', b"alpha   \r", 1)
    assert pm.restamp_project(path, DST) is None
    assert pm.frontmatter_project(path) == DST

    assert pm.restamp_project(path, b'"GitHub"') == b"alpha"  # the revert path
    assert path.read_bytes() == original


def test_restamp_survives_bytes_that_are_not_utf8(tmp_path):
    """A transcript with one bad byte must not make --apply fail (and undo
    the whole move) after the dry run promised a clean restamp."""
    path = tmp_path / "t.md"
    original = b"---\ntype: transcript\nproject: GitHub\n---\n\nbody \xff\xfe bytes\n"
    path.write_bytes(original)
    path.chmod(0o644)

    assert pm.frontmatter_project(path) == SRC
    assert pm.restamp_project(path, DST) == b"GitHub"
    assert path.read_bytes() == original.replace(b"project: GitHub", b"project: alpha")
    assert path.stat().st_mode & 0o777 == 0o644, "the atomic rewrite keeps permissions"


def test_restamp_without_project_key_is_a_no_op(tmp_path):
    path = tmp_path / "m.md"
    path.write_text("---\ntype: memo\n---\n\nproject: not frontmatter\n")
    assert pm.restamp_project(path, DST) is None
    assert pm.frontmatter_project(path) is None


# ── the real CLI, end to end (the unit's E2E recipe) ────────────────────────


def test_cli_end_to_end_in_a_sandbox(tmp_path):
    if not shutil.which("uv") or not shutil.which("git"):
        pytest.skip("needs uv and git")
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    (home / ".memex").mkdir(parents=True)
    (vault / "projects").mkdir(parents=True)
    (vault / "topics").mkdir()
    (home / ".memex" / "config.json").write_text(json.dumps({
        "memex_path": str(vault), "state_dir": str(home / ".memex"),
        "embeddings": {"enabled": False},
    }))
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMEX_")}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.update({"HOME": str(home), "UV_CACHE_DIR": _UV_CACHE, "UV_NO_SYNC": "1"})

    def memex(*args: str, stdin: str | None = None) -> str:
        proc = subprocess.run(
            [str(ROOT / "bin" / "memex"), *args], cwd=tmp_path, env=env,
            input=stdin, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
        if os.environ.get("MEMEX_E2E_ECHO"):
            print(f"\n$ memex {' '.join(args)}\n{proc.stdout}{proc.stderr}", file=sys.stderr)
        return proc.stdout

    assert memex("path").strip() == str(vault)
    for rel, text in {
        MEMO_A: _memo("Alpha decision", "zebra"),
        MEMO_B: _memo("Beta decision", "quokka"),
        TRANSCRIPT: f"---\ntype: transcript\ndate: 2026-10-12\nproject: {SRC}\n---\n\n# Session\n\nwalrus talk\n",
        RAW: '{"type": "user"}\n',
        STUB_PATH: STUB,
    }.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text)
    (vault / ".gitignore").write_text("projects/*/transcripts/\n")
    memex("index", "rebuild")
    for doc, contents in ((MEMO_A, ["Alpha picked the zebra approach", "Alpha leaves a follow-up open"]),
                          (MEMO_B, ["Beta picked the quokka approach"])):
        memex("backfill", "obs", "--stdin", "--replace", "--no-embed", "--doc-path", doc,
              stdin=json.dumps([{"content": c, "obs_type": "explicit", "confidence": "high"} for c in contents]))
    _git(vault, "init", "-q")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "base")
    memex("index", "rebuild")
    assert "Total observations: 3" in memex("obs", "stats")

    dry = memex("project", "move", SRC, DST)
    assert "Would move" in dry and "obs total:   3 → 3" in dry
    applied = memex("project", "move", SRC, DST, "--apply")
    assert "obs total:   3 → 3" in applied

    assert "Total observations: 3" in memex("obs", "stats")
    hits = json.loads(memex("search", "zebra", "--json", "--mode", "fts"))
    found = hits["results"] if isinstance(hits, dict) else hits
    assert any(h["path"] == _moved(MEMO_A) and h.get("project") == DST for h in found), found
    rebuild = memex("index", "rebuild")
    assert "Deleted: 0" in rebuild and "New: 0" in rebuild, rebuild


def test_a_failure_after_the_restamp_was_staged_leaves_git_clean(tmp_path, monkeypatch, capsys):
    """The restamp is `git add`-ed; a later failure must stage the restore too, or
    the index keeps the new project: after the files move back (gauntlet round 2)."""
    vault, index = _build(tmp_path)
    snapshot = _snapshot(vault, index)

    def boom(plan, journal):
        raise OSError("disk full after restamp (simulated)")

    monkeypatch.setattr(pm, "_remove_stub", boom)
    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 1
    assert "move failed and was undone" in capsys.readouterr().err
    assert _snapshot(vault, index) == snapshot
    assert _git(vault, "status", "--porcelain") == ""


def test_restamp_is_staged_without_staging_unrelated_unstaged_edits(tmp_path):
    """gauntlet round 3b: a whole-file `git add` would stage edits the owner left
    unstaged; only the staged copy's project: value may change."""
    vault, index = _build(tmp_path)
    memo = vault / MEMO_A
    memo.write_text(memo.read_text() + "\nAn edit the owner has NOT staged.\n")
    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 0
    moved = MEMO_A.replace(f"projects/{SRC}/", f"projects/{DST}/")
    staged = _git(vault, "show", f":{moved}")
    assert f"project: {DST}" in staged and "NOT staged" not in staged
    working = (vault / moved).read_text()
    assert f"project: {DST}" in working and "NOT staged" in working


def test_rollback_restores_a_staged_restamp_whose_working_copy_needed_none(tmp_path, monkeypatch, capsys):
    """gauntlet round 4: working tree already says the destination, the staged
    copy did not; a later failure must still restore the staged copy."""
    vault, index = _build(tmp_path)
    memo = vault / MEMO_A
    memo.write_text(memo.read_text().replace(f"project: {SRC}", f"project: {DST}"))  # unstaged edit
    staged_before = _git(vault, "show", f":{MEMO_A}")

    def boom(plan, journal):
        raise OSError("disk full after restamp (simulated)")

    monkeypatch.setattr(pm, "_remove_stub", boom)
    assert pm.run(vault, SRC, DST, apply=True, index_path=index) == 1
    assert "move failed and was undone" in capsys.readouterr().err
    assert _git(vault, "show", f":{MEMO_A}") == staged_before
