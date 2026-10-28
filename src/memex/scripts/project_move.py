"""`memex project move <src> <dst>` — move a project's documents, keeping every observation.

The 2026-10-28 tending pass consolidated 19 drifted folders by hand, each one
the same loop: ingest hand-edited sidecars, `obs reassign` memos/ +
transcripts/ (+ auto-memory/), move the files and their `.obs.jsonl`
sidecars, restamp `project:` in every moved file, delete the template
`_project.md`, rebuild. This module is that loop as one command, in the
order that loop had to learn:

1. Plan from disk and refuse on ANY collision — a target file that exists,
   or a target path that already has index rows. Nothing is touched.
2. Ingest every moved sidecar whose bytes differ from what the index last
   recorded (a hand edit, or a sync), BEFORE the reassign. The vault file is
   truth; a reassign that re-rendered sidecars from the DB (`memex obs
   reassign --apply` does) would overwrite the edit with stale DB rows.
3. Reassign the whole documents in the index (`reassign_doc_paths`, exactly
   the documents whose files move — never a prefix), checking matched ==
   updated for every table.
4. Move the files — `git mv` for tracked ones (memos, sidecars), a plain
   rename for the rest (transcripts and auto-memory are gitignored, so the
   curator log is their only record) — then restamp `project:` and remove a
   template-only `_project.md` together with its index rows.
5. Commit only when the observation total is what it was after step 2; on
   any failure the file operations are reverted and the transaction rolled
   back.

Sidecars move byte-for-byte and are not re-rendered: a sidecar has no
doc_path field, so a byte-identical move IS the write-through, and the
`obs_sidecars` record moves with the document, so the next rebuild sees it
unchanged. Dry run by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

SUBFOLDERS = ("memos", "transcripts", "auto-memory")
SIDECAR_SUFFIX = ".obs.jsonl"

# A project folder name as `sanitize_project_name` produces it (word chars and
# hyphens), plus dots: folders like `example.com` exist.
_NAME_RE = re.compile(r"[\w][\w.-]*")

# The stub `hooks/session-end.py:create_project_meta` writes for a new project
# — the only `_project.md` this command deletes. Anything else is somebody's
# overview. `tests/test_project_move.py` renders the hook's stub and checks it
# still matches, so the two cannot drift apart silently.
_STUB_RE = re.compile(
    r"---\ntype: project\nname: (?P<name>[^\n]*)\ncreated: \d{4}-\d{2}-\d{2}\n---\n\n"
    r"# (?P<title>[^\n]*)\n\n"
    r"## Overview\n\n"
    r"Project workspace: `[^`\n]*`\n\n"
    r"## Session History\n\n"
    r"<!-- Transcripts and memos will accumulate in this project folder -->"
)

_FRONTMATTER_RE = re.compile(rb"\A---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_PROJECT_LINE_RE = re.compile(rb"^project:[ \t]*([^\r\n]*?)[ \t]*(?=\r?$)", re.MULTILINE)

_GIT_BATCH = 200


class MoveError(Exception):
    """A refusal or a failure that left the vault and the index as they were."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class FileMove:
    src: str  # vault-relative
    dst: str
    tracked: bool = False


@dataclass
class MovePlan:
    vault: Path
    src: str
    dst: str
    mode: str  # "project" (whole folder) or "files" (--file)
    moves: list[FileMove] = field(default_factory=list)
    prefixes: list[tuple[str, str]] = field(default_factory=list)
    doc_mapping: dict[str, str] = field(default_factory=dict)
    collisions: list[str] = field(default_factory=list)
    stub: str | None = None
    stub_tracked: bool = False
    notes: list[str] = field(default_factory=list)
    left_behind: list[str] = field(default_factory=list)
    git: bool = False


# ── planning ────────────────────────────────────────────────────────────────


def is_template_stub(text: str) -> bool:
    """True when `text` is exactly the stub SessionEnd writes for a new project."""
    match = _STUB_RE.fullmatch(text.replace("\r\n", "\n").rstrip())
    return bool(match) and match["name"] == match["title"]


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name)) and ".." not in name


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args], capture_output=True, text=True, check=False
    )


def _git_enabled(vault: Path) -> bool:
    if shutil.which("git") is None:
        return False
    result = _git(vault, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _tracked_files(vault: Path, rel_dir: str) -> set[str]:
    result = _git(vault, "ls-files", "-z", "--", rel_dir)
    if result.returncode != 0:
        raise MoveError(f"git ls-files failed: {result.stderr.strip()}")
    return {path for path in result.stdout.split("\0") if path}


def _walk_files(root: Path, vault: Path) -> tuple[list[str], list[str]]:
    """Vault-relative files under `root`, plus what is skipped: dot-directories
    (SessionEnd's `.archive-*` staging) and symlinked directories."""
    files: list[str] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for name in sorted(dirnames):
            full = Path(dirpath) / name
            if name.startswith(".") or full.is_symlink():
                skipped.append(str(full.relative_to(vault)))
            else:
                keep.append(name)
        dirnames[:] = keep
        files.extend(str((Path(dirpath) / name).relative_to(vault)) for name in sorted(filenames))
    return files, skipped


def _resolve_file_arg(arg: str, vault: Path, src_root: Path, caller_cwd: Path | None) -> str:
    """A --file argument -> vault-relative path of an existing .md under src."""
    raw = Path(arg).expanduser()
    candidates = (
        [raw]
        if raw.is_absolute()
        else [
            *([caller_cwd / raw] if caller_cwd else []),
            vault / raw,
            src_root / raw,
            src_root / "memos" / raw,
        ]
    )
    vault_resolved = vault.resolve()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            rel = candidate.resolve().relative_to(vault_resolved)
        except ValueError:
            continue
        return rel.as_posix()
    raise MoveError(f"--file {arg}: no such file under {src_root}")


def _companions(rel_md: str, vault: Path) -> list[str]:
    """The sidecar and the raw transcript `.jsonl` that travel with a document."""
    stem = rel_md[: -len(".md")]
    return [
        stem + suffix
        for suffix in (SIDECAR_SUFFIX, ".jsonl")
        if (vault / (stem + suffix)).is_file()
    ]


def plan_move(
    vault: Path,
    src: str,
    dst: str,
    files: Sequence[str] | None = None,
    *,
    caller_cwd: Path | None = None,
) -> MovePlan:
    """Work out what a move would do, from disk only. Raises MoveError on bad input."""
    if not _valid_name(src) or not _valid_name(dst):
        raise MoveError(f"project names must look like folder names: {src!r}, {dst!r}")
    if src == dst:
        raise MoveError("source and destination are the same project")
    if src.lower() == dst.lower():
        # On a case-insensitive volume (APFS default) these are one folder.
        raise MoveError(f"{src!r} → {dst!r} differs only in case; rename the folder by hand")
    # Names are compared against what the directory really holds: on a
    # case-insensitive volume `projects/github` opens `projects/GitHub`, but
    # git and the index are case-sensitive, so a wrongly-cased name would
    # move files while matching no tracked path and no index row.
    projects_dir = vault / "projects"
    existing = {p.name for p in projects_dir.iterdir() if p.is_dir()} if projects_dir.is_dir() else set()
    if src not in existing:
        near = sorted(name for name in existing if name.lower() == src.lower())
        hint = f" (did you mean {near[0]!r}?)" if near else ""
        raise MoveError(f"no project folder projects/{src}/{hint}")
    twins = sorted(name for name in existing if name.lower() == dst.lower() and name != dst)
    if twins:
        raise MoveError(
            f"projects/{twins[0]}/ exists and differs from {dst!r} only in case — one folder "
            f"on a case-insensitive volume; move into {twins[0]!r} instead"
        )
    src_root = projects_dir / src

    plan = MovePlan(vault=vault, src=src, dst=dst, mode="files" if files else "project")
    plan.git = _git_enabled(vault)
    tracked = _tracked_files(vault, f"projects/{src}") if plan.git else set()
    src_prefix = f"projects/{src}/"
    dst_prefix = f"projects/{dst}/"

    selected: list[str] = []
    if files:
        for arg in files:
            rel = _resolve_file_arg(arg, vault, src_root, caller_cwd)
            inside = any(rel.startswith(f"{src_prefix}{sub}/") for sub in SUBFOLDERS)
            if not inside or not rel.endswith(".md") or rel.endswith(SIDECAR_SUFFIX):
                raise MoveError(
                    f"--file {arg}: must be a .md document under "
                    f"{src_prefix}{{{','.join(SUBFOLDERS)}}}/"
                )
            for path in (rel, *_companions(rel, vault)):
                if path not in selected:
                    selected.append(path)
    else:
        for sub in SUBFOLDERS:
            root = src_root / sub
            if root.is_dir():
                found, skipped = _walk_files(root, vault)
                selected.extend(found)
                plan.left_behind.extend(skipped)
        plan.prefixes = [(f"{src_prefix}{sub}/", f"{dst_prefix}{sub}/") for sub in SUBFOLDERS]

    # The index follows exactly the files that move — never a prefix, which
    # would also carry rows of documents left behind (in a skipped folder) to
    # a path with no file. A sidecar stands for its document even when the
    # `.md` itself is absent (mid-sync): its observations must follow it.
    for rel in selected:
        target = dst_prefix + rel[len(src_prefix) :]
        plan.moves.append(FileMove(src=rel, dst=target, tracked=rel in tracked))
        if rel.endswith(SIDECAR_SUFFIX):
            doc = rel[: -len(SIDECAR_SUFFIX)] + ".md"
            plan.doc_mapping.setdefault(doc, dst_prefix + doc[len(src_prefix) :])
        elif rel.endswith(".md"):
            plan.doc_mapping[rel] = target
        if os.path.lexists(vault / target):
            plan.collisions.append(target)

    if not files:
        stub = src_root / "_project.md"
        if stub.is_file():
            rel_stub = f"{src_prefix}_project.md"
            if (src_root / f"_project{SIDECAR_SUFFIX}").exists():
                plan.notes.append(f"kept {rel_stub}: it has observations (a sidecar)")
            elif is_template_stub(stub.read_bytes().decode("utf-8", errors="replace")):
                plan.stub = rel_stub
                plan.stub_tracked = rel_stub in tracked
            else:
                plan.notes.append(
                    f"kept {rel_stub}: it has content — condense it into "
                    f"projects/{dst}/_project.md or archive it as a redirect stub"
                )
        moved = {move.src for move in plan.moves}
        everything, skipped = _walk_files(src_root, vault)
        rest = [
            rel
            for rel in everything
            if rel not in moved and rel != plan.stub and Path(rel).name != ".DS_Store"
        ]
        plan.left_behind = sorted(set(plan.left_behind) | set(skipped) | set(rest))
        if not (vault / dst_prefix / "_project.md").exists():
            plan.notes.append(f"projects/{dst}/_project.md does not exist")
    return plan


# ── frontmatter ─────────────────────────────────────────────────────────────


def frontmatter_project(path: Path) -> str | None:
    """The `project:` value in a file's frontmatter (quotes stripped), or None.

    Reads only the frontmatter: transcripts run to megabytes.
    """
    try:
        with path.open("rb") as handle:
            first = handle.readline()
            if first.rstrip(b"\r\n").rstrip() != b"---":
                return None
            for line in handle:
                line = line.rstrip(b"\r\n")
                if line.rstrip() == b"---":
                    return None
                match = _PROJECT_LINE_RE.match(line)
                if match:
                    return _project_value(match.group(1)).decode("utf-8", errors="replace")
    except OSError:
        return None
    return None


def _project_value(raw: bytes) -> bytes:
    return raw.strip().strip(b"'\"")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with `data` atomically, keeping its permissions.

    Bytes, not text: a transcript may hold bytes that are not valid UTF-8,
    and a frontmatter-only edit must neither fail on them nor alter them.
    """
    import stat
    import tempfile

    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name[:200]}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restamp_project(path: Path, project: str | bytes) -> bytes | None:
    """Set the frontmatter `project:` value to `project`, in place, atomically.

    Returns the previous raw value (bytes, quotes included — pass it back to
    undo exactly) when the line was rewritten; None when there was nothing to
    do (no frontmatter, no `project:` key, or already `project`). Every byte
    outside the one value is untouched, CRLF and undecodable bytes included.
    A frontmatter-only edit, so no secret scrub (`safe_write_text` exempts it).
    """
    value = project.encode("utf-8") if isinstance(project, str) else project
    result = _restamp_bytes(path.read_bytes(), value)
    if result is None:
        return None
    new_data, old = result
    _atomic_write_bytes(path, new_data)
    return old


def _restamp_bytes(data: bytes, value: bytes) -> tuple[bytes, bytes] | None:
    """(data with the frontmatter `project:` value set to `value`, old value), or None."""
    fm = _FRONTMATTER_RE.match(data)
    if not fm or fm.group(1) is None:
        return None
    start, end = fm.span(1)
    match = _PROJECT_LINE_RE.search(data, start, end)
    if not match:
        return None
    old = match.group(1)
    if old == value or _project_value(old) == _project_value(value):
        return None
    return data[: match.start(1)] + value + data[match.end(1) :], old


def _git_bytes(vault: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(vault), *args], input=stdin, capture_output=True, check=False)


def _stage_restamp(vault: Path, rel: str, project: str) -> str | None:
    """Restamp `project:` in the STAGED copy of `rel`, leaving the rest of the
    index entry — and any unstaged edits in the working tree — alone.

    A whole-file `git add` after the working-tree restamp would also stage edits
    the owner had deliberately left unstaged (gauntlet round 3b). Returns the
    original `mode,sha` index entry for rollback, or None when the staged copy
    needed no change."""
    listed = _git(vault, "ls-files", "-s", "--", rel)
    if listed.returncode != 0:
        raise MoveError(f"git ls-files of {rel} failed: {listed.stderr.strip()}")
    if not listed.stdout.strip():
        return None  # not in the index
    meta = listed.stdout.split("\t", 1)[0].split()
    if len(meta) < 2:
        return None
    mode, sha = meta[0], meta[1]
    blob = _git_bytes(vault, "cat-file", "blob", sha)
    if blob.returncode != 0:
        raise MoveError(f"git cat-file of {rel} failed: {blob.stderr.decode(errors='replace').strip()}")
    result = _restamp_bytes(blob.stdout, project.encode("utf-8"))
    if result is None:
        return None
    written = _git_bytes(vault, "hash-object", "-w", "--stdin", stdin=result[0])
    if written.returncode != 0:
        raise MoveError(f"git hash-object for {rel} failed: {written.stderr.decode(errors='replace').strip()}")
    new_sha = written.stdout.decode().strip()
    updated = _git(vault, "update-index", "--cacheinfo", f"{mode},{new_sha},{rel}")
    if updated.returncode != 0:
        raise MoveError(f"git update-index for {rel} failed: {updated.stderr.strip()}")
    return f"{mode},{sha}"


# ── index helpers ───────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _obs_total(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "observations"):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])


def _sidecar_moves(plan: MovePlan) -> list[tuple[str, Path]]:
    """(old doc_path, sidecar file) for every moved real sidecar."""
    out = []
    for move in plan.moves:
        if move.src.endswith(SIDECAR_SUFFIX):
            doc_path = move.src[: -len(SIDECAR_SUFFIX)] + ".md"
            out.append((doc_path, plan.vault / move.src))
    return out


def _stale_sidecars(conn: sqlite3.Connection, plan: MovePlan) -> tuple[list[tuple[str, Path, bytes]], list[str]]:
    """Moved sidecars whose bytes differ from the hash the index recorded.

    Returns (to ingest, empty) — an empty or whitespace-only sidecar is an
    iCloud placeholder and never authoritative (Addendum A1): it moves as-is
    and is NOT ingested, because ingesting it would delete the doc's rows.
    """
    recorded: dict[str, str] = {}
    if _table_exists(conn, "obs_sidecars"):
        recorded = dict(conn.execute("SELECT doc_path, content_hash FROM obs_sidecars"))
    stale: list[tuple[str, Path, bytes]] = []
    empty: list[str] = []
    for doc_path, path in _sidecar_moves(plan):
        try:
            raw = path.read_bytes()
            raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MoveError(f"unreadable sidecar {path}: {exc}") from exc
        if not raw.strip():
            empty.append(doc_path)
            continue
        if recorded.get(doc_path) != hashlib.sha256(raw).hexdigest():
            stale.append((doc_path, path, raw))
    return stale, empty


def _ingest_stale_sidecars(
    conn: sqlite3.Connection, plan: MovePlan, stale: list[tuple[str, Path, bytes]]
) -> dict:
    """Diff each stale sidecar into the index at its CURRENT (old) path."""
    from memex.observations import init_observation_schema
    from memex.sidecars import SidecarError, ingest_sidecar, resolve_pending_sources

    totals = {"files": 0, "inserted": 0, "deleted": 0, "updated": 0, "foreign_conflicts": []}
    if not stale:
        return totals
    init_observation_schema(conn)
    pending: list[tuple[int, list[str]]] = []
    for doc_path, path, raw in stale:
        conn.execute("SAVEPOINT move_ingest")
        try:
            # indexed_paths=None: never adopt rows from another document — the
            # same rule `memex obs ingest-sidecars` follows outside a rebuild.
            result = ingest_sidecar(conn, plan.vault, doc_path, path, indexed_paths=None, raw=raw)
        except SidecarError as exc:
            conn.execute("ROLLBACK TO SAVEPOINT move_ingest")
            conn.execute("RELEASE SAVEPOINT move_ingest")
            raise MoveError(
                f"malformed sidecar {path}: {exc} — fix it first (`memex obs sidecars` "
                "lists the line); its edit has not reached the index"
            ) from exc
        conn.execute("RELEASE SAVEPOINT move_ingest")
        totals["files"] += 1
        for key in ("inserted", "deleted", "updated"):
            totals[key] += result[key]
        totals["foreign_conflicts"].extend(result["foreign_conflicts"])
        pending.extend(result["pending_sources"])
    resolve_pending_sources(conn, pending)
    return totals


def _refresh_search_rows(conn: sqlite3.Connection, vault: Path, paths: Sequence[str]) -> None:
    """Re-read restamped, already-indexed documents into `fts_content`, so the
    searchable text carries the new `project:` too, not only the column.

    `doc_hashes` keeps the old hash on purpose: the next incremental rebuild
    must still re-chunk these documents, whose chunk text names the project.
    """
    if not paths or not _table_exists(conn, "doc_hashes") or not _table_exists(conn, "fts_content"):
        return
    from memex.scripts.index_rebuild import index_file_fts

    indexed: set[str] = set()
    for start in range(0, len(paths), 900):
        batch = list(paths[start : start + 900])
        indexed.update(
            row[0]
            for row in conn.execute(
                f"SELECT path FROM doc_hashes WHERE path IN ({','.join('?' for _ in batch)})", batch
            )
        )
    for path in paths:
        if path in indexed:
            try:
                index_file_fts(conn, vault / path, vault)
            except (OSError, UnicodeDecodeError):
                # Best effort: the row already has the new path and project;
                # the next rebuild re-reads the text either way.
                pass


def _index_rows_left(conn: sqlite3.Connection, plan: MovePlan) -> list[dict]:
    """Indexed documents under the moved subfolders that do NOT move.

    Whole-project mode only. Such a row either belongs to a file in a folder
    the walk skips (it stays, correctly, with its file) or to a file missing
    on disk (the next rebuild removes it — with its observations, which is
    worth a warning before anything else happens).
    """
    if plan.mode != "project":
        return []
    paths: set[str] = set()
    for table, col in (("doc_hashes", "path"), ("observations", "doc_path")):
        if not _table_exists(conn, table):
            continue
        for from_prefix, _ in plan.prefixes:
            paths.update(
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE SUBSTR({col}, 1, ?) = ?",
                    (len(from_prefix), from_prefix),
                )
            )
    left = []
    for path in sorted(paths - set(plan.doc_mapping)):
        observations = (
            conn.execute("SELECT COUNT(*) FROM observations WHERE doc_path = ?", (path,)).fetchone()[0]
            if _table_exists(conn, "observations")
            else 0
        )
        left.append({"path": path, "on_disk": (plan.vault / path).exists(), "observations": observations})
    return left


def _inbound_links(conn: sqlite3.Connection, old_paths: Sequence[str]) -> list[dict]:
    if not old_paths or not _table_exists(conn, "wikilinks"):
        return []
    links: list[dict] = []
    paths = list(old_paths)
    for start in range(0, len(paths), 900):
        batch = paths[start : start + 900]
        links.extend(
            {"source_path": source, "target_path": target, "link_text": text}
            for source, target, text in conn.execute(
                "SELECT source_path, target_path, link_text FROM wikilinks "
                f"WHERE target_path IN ({','.join('?' for _ in batch)}) "
                "ORDER BY source_path, line_number",
                batch,
            )
        )
    return links


# ── file operations (journalled, reversible) ────────────────────────────────


class _Journal:
    """Every file operation done so far, so a failure can put them back."""

    def __init__(self, vault: Path):
        self.vault = vault
        self.moved: list[FileMove] = []
        self.restamped: list[tuple[Path, bytes]] = []
        self.staged_restamps: dict[str, str] = {}  # rel -> original "mode,sha" index entry
        self.created_dirs: list[Path] = []
        self.removed_stub: tuple[str, bytes, bool] | None = None

    def ensure_dir(self, path: Path) -> None:
        missing = []
        for parent in [path, *path.parents]:
            if parent.exists() or parent == self.vault:
                break
            missing.append(parent)
        for parent in reversed(missing):
            parent.mkdir()
            self.created_dirs.append(parent)

    def revert(self) -> list[str]:
        """Undo in reverse order. Returns what could NOT be undone."""
        problems: list[str] = []
        if self.removed_stub is not None:
            rel, data, tracked = self.removed_stub
            try:
                (self.vault / rel).write_bytes(data)
                if tracked:
                    _git(self.vault, "add", "--", rel)
            except OSError as exc:
                problems.append(f"could not restore {rel}: {exc}")
        for path, old in reversed(self.restamped):
            try:
                restamp_project(path, old)
            except OSError as exc:
                problems.append(f"could not restore project: in {path}: {exc}")
        # Every staged restamp, including one whose working copy needed no change
        # (gauntlet round 4): put the exact original index entry back, or the
        # index keeps the new `project:` after the files move back.
        for rel, original in reversed(list(self.staged_restamps.items())):
            result = _git(self.vault, "update-index", "--cacheinfo", f"{original},{rel}")
            if result.returncode != 0:
                problems.append(f"could not restore the staged copy of {rel}: {result.stderr.strip()}")
        for move in reversed(self.moved):
            try:
                if move.tracked:
                    result = _git(self.vault, "mv", "--", move.dst, move.src)
                    if result.returncode != 0:
                        raise OSError(result.stderr.strip())
                else:
                    os.rename(self.vault / move.dst, self.vault / move.src)
            except OSError as exc:
                problems.append(f"could not move {move.dst} back to {move.src}: {exc}")
        for path in reversed(self.created_dirs):
            try:
                path.rmdir()
            except OSError:
                pass
        return problems


def _move_files(plan: MovePlan, journal: _Journal) -> None:
    for move in plan.moves:
        journal.ensure_dir((plan.vault / move.dst).parent)

    # git mv in batches per (source dir, target dir): one process per batch,
    # not per file. Never `git mv -q` — this git build rejects it.
    groups: dict[tuple[str, str], list[FileMove]] = {}
    for move in plan.moves:
        if move.tracked:
            key = (str(Path(move.src).parent), str(Path(move.dst).parent))
            groups.setdefault(key, []).append(move)
    for (_, dst_dir), moves in groups.items():
        for start in range(0, len(moves), _GIT_BATCH):
            batch = moves[start : start + _GIT_BATCH]
            for move in batch:
                if os.path.lexists(plan.vault / move.dst):
                    raise MoveError(f"{move.dst} appeared while moving", exit_code=3)
            result = _git(plan.vault, "mv", "--", *(m.src for m in batch), dst_dir + "/")
            if result.returncode != 0:
                raise MoveError(f"git mv failed: {result.stderr.strip()}")
            journal.moved.extend(batch)

    for move in plan.moves:
        if move.tracked:
            continue
        if os.path.lexists(plan.vault / move.dst):
            raise MoveError(f"{move.dst} appeared while moving", exit_code=3)
        os.rename(plan.vault / move.src, plan.vault / move.dst)
        journal.moved.append(move)


def _restamp_moved(plan: MovePlan, journal: _Journal) -> list[dict]:
    restamped = []
    for move in plan.moves:
        if not move.dst.endswith(".md"):
            continue
        path = plan.vault / move.dst
        old = restamp_project(path, plan.dst)
        if old is not None:
            journal.restamped.append((path, old))
            restamped.append(
                {"path": move.dst, "was": _project_value(old).decode("utf-8", errors="replace")}
            )
        if move.tracked:
            # `git mv` staged the rename; restamp the staged copy too, or a plain
            # `git commit` would record the moved memo with its OLD `project:`.
            original = _stage_restamp(plan.vault, move.dst, plan.dst)
            if original is not None:
                journal.staged_restamps[move.dst] = original
    return restamped


def _remove_stub(plan: MovePlan, journal: _Journal) -> str | None:
    """Delete the template `_project.md`. Returns a warning instead of raising:
    a stub that will not go is not worth undoing the move for."""
    assert plan.stub is not None
    path = plan.vault / plan.stub
    data = path.read_bytes()
    if plan.stub_tracked:
        result = _git(plan.vault, "rm", "--", plan.stub)
        if result.returncode != 0:
            return f"kept {plan.stub}: git rm failed ({result.stderr.strip()})"
    else:
        path.unlink()
    journal.removed_stub = (plan.stub, data, plan.stub_tracked)
    return None


def _prune_empty_dirs(plan: MovePlan) -> list[str]:
    """Remove the source subfolders the move emptied, and the project folder
    itself when nothing is left (a lone .DS_Store counts as nothing).
    Best effort — the move is already committed. Returns what was removed."""
    src_root = plan.vault / "projects" / plan.src
    removed: list[str] = []

    def remove_if_empty(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
            if [entry.name for entry in entries] == [".DS_Store"]:
                entries[0].unlink()
                entries = []
            if not entries:
                directory.rmdir()
                removed.append(str(directory.relative_to(plan.vault)))
        except OSError:
            pass

    for sub in SUBFOLDERS:
        top = src_root / sub
        if top.is_dir():
            # Deepest first, so a parent sees its children already gone.
            for dirpath, _, _ in sorted(os.walk(top), key=lambda item: -len(item[0])):
                remove_if_empty(Path(dirpath))
    if src_root.is_dir():
        remove_if_empty(src_root)
    return removed


# ── the move ────────────────────────────────────────────────────────────────


def execute_move(plan: MovePlan, index_path: Path, *, apply: bool) -> dict:
    """Run (or, without `apply`, simulate) the move. Caller holds the rebuild lock
    when applying. Raises MoveError when it refuses or fails; the vault and the
    index are then as they were (or the error says what could not be undone)."""
    from memex.db_utils import connect_index, load_vec_extension
    from memex.observations import (
        delete_document_rows,
        reassign_doc_paths,
        reassign_invariant_violations,
    )

    if not index_path.exists():
        raise MoveError(f"no index at {index_path} — run `memex index rebuild` first")

    report: dict = {
        "src": plan.src,
        "dst": plan.dst,
        "mode": plan.mode,
        "apply": apply,
        "git": plan.git,
        "files": len(plan.moves),
        "files_git": sum(1 for m in plan.moves if m.tracked),
        "documents": len(plan.doc_mapping),
        "file_collisions": plan.collisions,
        "stub": plan.stub,
        "notes": list(plan.notes),
        "left_behind": plan.left_behind,
        "warnings": [],
    }
    old_doc_paths = sorted(plan.doc_mapping)

    conn = connect_index(index_path)
    try:
        load_vec_extension(conn)
        report["obs_before"] = _obs_total(conn)
        stale, empty = _stale_sidecars(conn, plan)
        report["sidecars_to_ingest"] = [doc for doc, _, _ in stale]
        for doc_path in empty:
            report["warnings"].append(f"empty sidecar for {doc_path} moved as-is, not ingested")
        report["restamp"] = [
            {"path": move.src, "was": value}
            for move in plan.moves
            if move.src.endswith(".md")
            and (value := frontmatter_project(plan.vault / move.src)) is not None
            and value != plan.dst
        ]
        report["index_left"] = _index_rows_left(conn, plan)
        for item in report["index_left"]:
            if item["observations"] and not item["on_disk"]:
                report["warnings"].append(
                    f"{item['path']} is missing on disk but holds {item['observations']} "
                    "observation(s); the next rebuild drops them — restore the file first"
                )

        if not apply:
            stats = reassign_doc_paths(conn, plan.doc_mapping, dry_run=True)
            report["reassign"] = stats
            report["collisions"] = sorted(set(plan.collisions) | set(stats["collisions"]))
            report["inbound_links"] = _inbound_links(conn, old_doc_paths)
            report["obs_after"] = report["obs_before"]
            report["exit_code"] = 3 if report["collisions"] else 0
            return report

        if plan.collisions:
            raise MoveError(
                f"refusing: {len(plan.collisions)} target file(s) already exist "
                f"(e.g. {plan.collisions[0]})",
                exit_code=3,
            )

        # One transaction for everything below. Opened explicitly (and
        # IMMEDIATE, so the write lock is ours before anything changes): the
        # first SAVEPOINT would otherwise BE the transaction, and releasing it
        # would commit the sidecar ingest on its own.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        ingested = _ingest_stale_sidecars(conn, plan, stale)
        report["ingested"] = {k: v for k, v in ingested.items() if k != "foreign_conflicts"}
        for pair in ingested["foreign_conflicts"]:
            report["warnings"].append(
                f"sidecar line for {pair['doc_path']} already belongs to {pair['foreign_doc_path']}"
            )
        report["obs_after_ingest"] = _obs_total(conn)

        try:
            stats = reassign_doc_paths(conn, plan.doc_mapping, dry_run=False)
        except sqlite3.IntegrityError as exc:
            raise MoveError(f"refusing: {exc}", exit_code=3) from exc
        report["reassign"] = stats
        violations = reassign_invariant_violations(stats)
        if violations:
            raise MoveError("invariant violated (matched != updated): " + "; ".join(violations), 2)
        report["inbound_links"] = _inbound_links(conn, old_doc_paths)

        if plan.stub is not None:
            stub_obs = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE doc_path = ?", (plan.stub,)
            ).fetchone()[0] if _table_exists(conn, "observations") else 0
            if stub_obs:
                report["notes"].append(f"kept {plan.stub}: it has {stub_obs} observation(s)")
                plan.stub = None
                report["stub"] = None

        journal = _Journal(plan.vault)
        try:
            _move_files(plan, journal)
            report["restamped"] = _restamp_moved(plan, journal)
            _refresh_search_rows(conn, plan.vault, [item["path"] for item in report["restamped"]])
            if plan.stub is not None:
                warning = _remove_stub(plan, journal)
                if warning:
                    report["warnings"].append(warning)
                    report["stub"] = None
                else:
                    report["stub_rows_removed"] = delete_document_rows(conn, plan.stub)

            report["obs_after"] = _obs_total(conn)
            if report["obs_after"] != report["obs_after_ingest"]:
                raise MoveError(
                    f"observation total changed during the move: "
                    f"{report['obs_after_ingest']} → {report['obs_after']}",
                    exit_code=2,
                )
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            problems = journal.revert()
            if problems:
                raise MoveError(
                    f"{exc}\nREVERT INCOMPLETE — repair by hand:\n  " + "\n  ".join(problems),
                    exit_code=getattr(exc, "exit_code", 1),
                ) from exc
            if isinstance(exc, MoveError) or not isinstance(exc, Exception):
                raise
            raise MoveError(f"move failed and was undone: {exc}") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    report["removed_dirs"] = _prune_empty_dirs(plan)
    report["exit_code"] = 0
    return report


# ── output ──────────────────────────────────────────────────────────────────


def format_report(report: dict) -> str:
    verb = "Moved" if report["apply"] else "Would move"
    lines = [f"{verb} projects/{report['src']}/ → projects/{report['dst']}/ ({report['mode']})"]
    plain = report["files"] - report["files_git"]
    how = f"{report['files_git']} via git mv, {plain} plain" if report["git"] else "plain moves (no git)"
    lines.append(f"  files:       {report['files']} ({report['documents']} documents; {how})")
    stats = report.get("reassign") or {}
    if stats:
        tables = stats.get("tables", {})
        lines.append(
            f"  index:       {stats['paths']} documents · "
            f"{tables.get('observations', {}).get('matched', 0)} observations · "
            f"{tables.get('chunks', {}).get('matched', 0)} chunks · "
            f"{tables.get('fts_content', {}).get('matched', 0)} search rows"
            + (f" · project → {report['dst']} on {stats['project_changed']}" if stats["project_changed"] else "")
        )
    if report.get("sidecars_to_ingest"):
        word = "Ingested" if report["apply"] else "Would ingest"
        lines.append(f"  sidecars:    {word} {len(report['sidecars_to_ingest'])} changed sidecar(s) first:")
        lines.extend(f"                 {doc}" for doc in report["sidecars_to_ingest"])
    stamps = report.get("restamped") if report["apply"] else report.get("restamp")
    if stamps:
        lines.append(f"  project:     {len(stamps)} file(s) restamped to {report['dst']}")
        unusual = sorted({s["was"] for s in stamps} - {report["src"]})
        if unusual:
            lines.append(f"               (stamps other than {report['src']}: {', '.join(unusual)})")
    if report.get("stub"):
        word = "removed" if report["apply"] else "would be removed"
        lines.append(f"  _project.md: template stub {word}")
    for note in report.get("notes", []):
        lines.append(f"  note:        {note}")
    if report.get("left_behind"):
        lines.append(f"  left behind: {len(report['left_behind'])} (not moved):")
        lines.extend(f"                 {rel}" for rel in report["left_behind"][:20])
    if report.get("index_left"):
        lines.append(
            f"  not moved:   {len(report['index_left'])} indexed document(s) under the moved "
            "folders stay where they are:"
        )
        lines.extend(
            f"                 {item['path']} ({'on disk' if item['on_disk'] else 'missing on disk'})"
            for item in report["index_left"][:20]
        )
    links = report.get("inbound_links") or []
    if links:
        lines.append(f"  links:       {len(links)} wikilink(s) resolve to a moved path — update their text:")
        lines.extend(
            f"                 {link['source_path']}: [[{link['link_text']}]]" for link in links[:20]
        )
    collisions = report.get("collisions") or report.get("file_collisions") or []
    if collisions:
        lines.append(f"  REFUSED:     {len(collisions)} collision(s) at the target:")
        lines.extend(f"                 {path}" for path in collisions[:20])
    for warning in report.get("warnings", []):
        lines.append(f"  warning:     {warning}")
    before = report.get("obs_after_ingest", report["obs_before"])
    lines.append(f"  obs total:   {before} → {report.get('obs_after', before)}")
    if report["apply"]:
        if report.get("removed_dirs"):
            lines.append(f"  removed:     {', '.join(report['removed_dirs'])}")
        lines.append("  next: memex index rebuild --incremental (moved docs show as updated, none as new/deleted)")
    elif not collisions:
        lines.append("  (dry run — re-run with --apply)")
    return "\n".join(lines)


def run(
    vault: Path,
    src: str,
    dst: str,
    *,
    files: Sequence[str] | None = None,
    apply: bool = False,
    as_json: bool = False,
    caller_cwd: Path | None = None,
    index_path: Path | None = None,
) -> int:
    """Plan + execute + print. Returns the exit code (0 ok, 1 refused,
    2 invariant violated and undone, 3 collision, 4 index busy)."""
    from memex.db_utils import rebuild_lock
    from memex.paths import get_index_path

    index_path = index_path or get_index_path(vault)
    try:
        if apply:
            # Exclusive: no rebuild and no observation writer may run while
            # documents change path under them.
            entered = False
            try:
                with rebuild_lock():
                    entered = True
                    plan = plan_move(vault, src, dst, files, caller_cwd=caller_cwd)
                    report = execute_move(plan, index_path, apply=True)
            except SystemExit:
                if entered:
                    raise
                # rebuild_lock's contention exit (3) would read as "collision".
                raise MoveError(
                    "the index is busy (a rebuild or an observation write holds the lock); "
                    "nothing was changed — retry when it finishes",
                    exit_code=4,
                ) from None
        else:
            plan = plan_move(vault, src, dst, files, caller_cwd=caller_cwd)
            report = execute_move(plan, index_path, apply=False)
    except MoveError as exc:
        if as_json:
            print(json.dumps({"error": str(exc), "exit_code": exc.exit_code}, indent=2))
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code

    if as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
    return int(report["exit_code"])


def main(argv: Sequence[str] | None = None) -> None:
    from memex.paths import get_memex_path

    parser = argparse.ArgumentParser(description="Move a project's documents to another project")
    parser.add_argument("src")
    parser.add_argument("dst")
    parser.add_argument("--file", action="append", dest="files", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.exit(
        run(
            get_memex_path(),
            args.src,
            args.dst,
            files=args.files,
            apply=args.apply,
            as_json=args.json,
            caller_cwd=Path.cwd(),
        )
    )


if __name__ == "__main__":
    main()
