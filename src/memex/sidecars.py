"""Vault-backed observations via `.obs.jsonl` sidecars.

The vault (markdown) is truth; `~/.memex/_index.sqlite` is a per-machine
cache. Historically observations (LLM-extracted) existed ONLY in the DB, so a
second machine sharing the vault via iCloud had none, and two machines
drifted apart. This module is the fix: every document `D.md` that has
observations gets a sidecar `D.obs.jsonl` next to it, containing one JSON
object per line (one per observation), ordered by id.

Three rules, enforced by the call sites in extract.py / dreamer.py / cli.py,
not by this module:

1. Write-through — every DB mutation of a doc's observations rewrites that
   doc's sidecar FROM DB STATE via `write_sidecar`. No per-mutation sidecar
   editing.
2. Ingest on rebuild — `memex index rebuild` (full and incremental) diffs
   each changed sidecar into the DB via `ingest_all_sidecars` — insert /
   delete / update, never wipe-and-reload, so existing rows keep their ids
   and vectors.
3. Sidecars are never deleted automatically except when a LOCAL EXPLICIT
   mutation leaves a doc with zero observations (`write_sidecar` unlinks in
   that case). A memo missing on disk — possibly mid-iCloud-sync — must NOT
   cause its sidecar to be removed or its rows ingested; a sidecar arriving
   before its memo is simply retried next run (`ingest_all_sidecars`'s
   `indexed_paths` filter).

See docs/2026-09-13-obs-sidecar-spec.md for the full design.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from memex.db_utils import (
    release_savepoint_if_exists as _release_savepoint_if_exists,
    rollback_savepoint_or_die as _rollback_savepoint_or_die,
)
from memex.observations import decode_source_ids, encode_source_ids, store_observation_topics
from memex.scrub import _atomic_write, scrub_text

SIDECAR_SUFFIX = ".obs.jsonl"

# iCloud conflict copies are named `<stem>.obs 2.jsonl`, `<stem>.obs 3.jsonl`,
# etc. — a space + integer inserted before the final extension. This must
# never match `find_sidecars`' glob output; `find_sidecar_conflicts` lists
# these separately so the health command can flag them for manual resolution.
_CONFLICT_COPY_RE = re.compile(r"\.obs \d+\.jsonl$")

# Same host-parameter ceiling rationale as `observations._SQL_VAR_CHUNK`:
# chunk IN-lists below SQLite's pre-3.32 999-variable limit.
_SQL_VAR_CHUNK = 900


def _chunked(items: Sequence, size: int = _SQL_VAR_CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SidecarError(Exception):
    """A `.obs.jsonl` file is malformed. Raised for the WHOLE file — a
    partially broken sidecar is more likely a sync artifact than intent, and
    a partial ingest would delete DB rows the truncated tail never mentions.
    """


@dataclass(slots=True)
class SidecarRecord:
    content: str
    content_hash: str
    obs_type: str = "explicit"
    confidence: str = "high"
    topics: list[str] = field(default_factory=list)
    source_obs: list[str] = field(default_factory=list)
    created_at: str | None = None


def sidecar_path(vault: Path, doc_path: str) -> Path | None:
    """Vault-relative doc_path -> its sidecar path, or None if unportable.

    `projects/p/memos/x.md` -> `projects/p/memos/x.obs.jsonl`;
    `projects/p/_project.md` -> `projects/p/_project.obs.jsonl`;
    `topics/t.md` -> `topics/t.obs.jsonl`. An absolute doc_path (a handful of
    legacy rows exist) or one that resolves outside the vault returns None —
    the caller is responsible for warning.
    """
    if Path(doc_path).is_absolute():
        return None
    stem = doc_path[: -len(".md")] if doc_path.endswith(".md") else doc_path
    candidate = vault / f"{stem}{SIDECAR_SUFFIX}"
    try:
        # Containment check per .claude/rules/python-patterns.md: relative_to
        # in try/except, not a string startswith check (which a crafted
        # doc_path like "../../etc/passwd.md" would defeat via normalization
        # differences).
        candidate.resolve().relative_to(vault.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def doc_path_for_sidecar(vault: Path, path: Path) -> str:
    """Inverse of `sidecar_path`: a sidecar file's path -> its doc's vault-relative path."""
    rel = str(path.relative_to(vault))
    assert rel.endswith(SIDECAR_SUFFIX), f"not a sidecar path: {path}"
    return rel[: -len(SIDECAR_SUFFIX)] + ".md"


def find_sidecars(vault: Path) -> list[Path]:
    """Every real sidecar under the vault, skipping templates/views and
    iCloud conflict copies (which don't match the glob in the first place —
    see `_CONFLICT_COPY_RE`)."""
    found: list[Path] = []
    for pattern in (f"projects/**/*{SIDECAR_SUFFIX}", f"topics/*{SIDECAR_SUFFIX}"):
        for path in vault.glob(pattern):
            if "_templates" in path.parts or "_views" in path.parts:
                continue
            found.append(path)
    return sorted(found)


def find_sidecar_conflicts(vault: Path) -> list[Path]:
    """iCloud conflict copies of sidecars (`x.obs 2.jsonl`) — never ingested,
    surfaced only for the `memex obs sidecars` health report."""
    found: list[Path] = []
    for pattern in ("projects/**/*.jsonl", "topics/*.jsonl"):
        for path in vault.glob(pattern):
            if "_templates" in path.parts or "_views" in path.parts:
                continue
            if _CONFLICT_COPY_RE.search(path.name):
                found.append(path)
    return sorted(found)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_sidecar_hash(conn: sqlite3.Connection, doc_path: str, digest: str) -> None:
    conn.execute(
        """
        INSERT INTO obs_sidecars (doc_path, content_hash, ingested_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(doc_path) DO UPDATE SET
            content_hash = excluded.content_hash,
            ingested_at = CURRENT_TIMESTAMP
        """,
        (doc_path, digest),
    )


def scrub_doc_observations(conn: sqlite3.Connection, doc_path: str) -> int:
    """Redact secrets INSIDE each observation's content, in the DB, before render.

    Scrubbing the rendered JSONL as a whole (the obvious hook-bypass gate)
    would redact the text but leave each line's `content_hash` computed
    from the pre-redaction content — a self-inconsistent line that
    `read_sidecar` rejects, and that every re-render reproduces, wedging
    that doc's sidecar for every other machine. Scrubbing per row and
    rewriting the row (content + hash + FTS mirror) keeps hash and content
    consistent by construction. A redaction that collides with an existing
    hash means the row is now a duplicate; it is removed via the sanctioned
    deleter. Returns the number of rows redacted. Does not commit.
    """
    from memex.observations import content_hash as _content_hash, delete_observation_ids

    rows = conn.execute(
        "SELECT id, content FROM observations WHERE doc_path = ?", (doc_path,)
    ).fetchall()
    redacted = 0
    for obs_id, content in rows:
        scrubbed, matches = scrub_text(content, apply=True)
        if not matches or scrubbed == content:
            continue
        new_hash = _content_hash(scrubbed)
        clash = conn.execute(
            "SELECT id FROM observations WHERE content_hash = ? AND id != ?",
            (new_hash, obs_id),
        ).fetchone()
        if clash is not None:
            delete_observation_ids(conn, [obs_id])
        else:
            conn.execute(
                "UPDATE observations SET content = ?, content_hash = ? WHERE id = ?",
                (scrubbed, new_hash, obs_id),
            )
            conn.execute(
                "UPDATE fts_observations SET content = ? WHERE rowid = ?",
                (scrubbed, obs_id),
            )
        redacted += 1
    return redacted


def render_sidecar(conn: sqlite3.Connection, doc_path: str) -> str:
    """Render a doc's sidecar content from DB state. `""` when it has no
    observations — the caller (write_sidecar) treats that as "remove"."""
    rows = conn.execute(
        "SELECT id, content, content_hash, obs_type, confidence, source_obs_ids, created_at "
        "FROM observations WHERE doc_path = ? ORDER BY id ASC",
        (doc_path,),
    ).fetchall()
    if not rows:
        return ""

    # Resolve every referenced source id -> content_hash in one pass rather
    # than per-row: a doc's observations often share sources.
    all_source_ids: set[int] = set()
    for row in rows:
        all_source_ids.update(decode_source_ids(row[5]))
    hash_by_id: dict[int, str] = {}
    ids = sorted(all_source_ids)
    for batch in _chunked(ids):
        placeholders = ",".join("?" for _ in batch)
        for obs_id, obs_hash in conn.execute(
            f"SELECT id, content_hash FROM observations WHERE id IN ({placeholders})",
            batch,
        ).fetchall():
            hash_by_id[obs_id] = obs_hash

    lines: list[str] = []
    for obs_id, content, obs_hash, obs_type, confidence, source_ids_raw, created_at in rows:
        topics = sorted(
            row[0]
            for row in conn.execute(
                "SELECT topic_slug FROM observation_topics WHERE observation_id = ?",
                (obs_id,),
            ).fetchall()
        )
        # Unresolvable source ids (row deleted since) are dropped at render
        # time — row ids are per-machine and must never appear in the
        # sidecar; only the surviving hashes do.
        source_hashes = [
            hash_by_id[sid] for sid in decode_source_ids(source_ids_raw) if sid in hash_by_id
        ]
        # Plus references still awaiting their source (A3) — otherwise a
        # write-through on this machine would drop them from the vault file
        # and the loss would sync to every other machine.
        for (pending_hash,) in conn.execute(
            "SELECT source_hash FROM obs_pending_sources WHERE observation_id = ? "
            "ORDER BY source_hash",
            (obs_id,),
        ).fetchall():
            if pending_hash not in source_hashes:
                source_hashes.append(pending_hash)
        obj = {
            "content": content,
            "content_hash": obs_hash,
            "obs_type": obs_type,
            "confidence": confidence,
            "topics": topics,
            "source_obs": source_hashes,
            "created_at": created_at,
        }
        lines.append(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return "".join(line + "\n" for line in lines)


def write_sidecar(conn: sqlite3.Connection, vault: Path, doc_path: str) -> Path | None:
    """Rewrite (or remove) `doc_path`'s sidecar from current DB state.

    Every write-through call site (extract.store_observations, dreamer,
    `memex obs retag`, `memex obs reassign --apply`) calls this once per
    affected doc_path after mutating that doc's observations. Does NOT
    commit — caller owns the transaction, same as everything else in
    observations.py.

    Returns the sidecar path when a file was written, else None (no
    observations left — sidecar removed; or the path was unportable/the
    parent directory is missing, in which case the failure is warned to
    stderr rather than raised, so a single bad doc_path in a batch doesn't
    abort the whole write-through call).
    """
    path = sidecar_path(vault, doc_path)
    if path is None:
        print(
            f"Warning: cannot compute sidecar path for {doc_path!r} "
            "(absolute or escapes vault) — skipping sidecar write",
            file=sys.stderr,
        )
        return None

    scrub_doc_observations(conn, doc_path)
    rendered = render_sidecar(conn, doc_path)
    if not rendered:
        # Zero observations: the sidecar is removed, never written empty.
        if path.exists():
            path.unlink()
        conn.execute("DELETE FROM obs_sidecars WHERE doc_path = ?", (doc_path,))
        return None

    if not path.parent.exists():
        # Do not create missing parent directories — a typo'd doc_path
        # should surface as a warning, not silently fabricate a folder tree.
        print(
            f"Warning: parent directory missing for sidecar {path} — "
            "skipping sidecar write",
            file=sys.stderr,
        )
        return None

    # Content was scrubbed per row above (hash-consistent); no whole-file pass.
    _atomic_write(path, rendered)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    _record_sidecar_hash(conn, doc_path, digest)
    return path


def read_sidecar(path: Path, text: str | None = None) -> list[SidecarRecord]:
    """Parse a `.obs.jsonl` file. Raises `SidecarError` for the whole file on
    any malformed line — see the module docstring for why partial ingest of
    a broken file is unsafe.

    `text`, when given, is the file's already-read content; callers that
    also hash the file pass it so the bytes parsed and the bytes hashed are
    the same read (iCloud can replace the file between two reads)."""
    if text is None:
        text = path.read_text(encoding="utf-8")
    records: list[SidecarRecord] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SidecarError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise SidecarError(f"{path}:{line_no}: line is not a JSON object")

        content = obj.get("content")
        if not isinstance(content, str) or not content:
            raise SidecarError(f"{path}:{line_no}: 'content' must be a non-empty string")

        obs_hash = obj.get("content_hash")
        if not isinstance(obs_hash, str) or not obs_hash:
            raise SidecarError(f"{path}:{line_no}: 'content_hash' must be a non-empty string")
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if obs_hash != expected_hash:
            raise SidecarError(
                f"{path}:{line_no}: content_hash mismatch "
                f"(file says {obs_hash}, content hashes to {expected_hash})"
            )

        obs_type = obj.get("obs_type", "explicit")
        if not isinstance(obs_type, str):
            raise SidecarError(f"{path}:{line_no}: 'obs_type' must be a string")
        confidence = obj.get("confidence", "high")
        if not isinstance(confidence, str):
            raise SidecarError(f"{path}:{line_no}: 'confidence' must be a string")

        topics = obj.get("topics", [])
        if not isinstance(topics, list) or any(not isinstance(t, str) for t in topics):
            raise SidecarError(f"{path}:{line_no}: 'topics' must be an array of strings")

        source_obs = obj.get("source_obs", [])
        if not isinstance(source_obs, list) or any(not isinstance(s, str) for s in source_obs):
            raise SidecarError(f"{path}:{line_no}: 'source_obs' must be an array of strings")

        created_at = obj.get("created_at")
        if created_at is not None and not isinstance(created_at, str):
            raise SidecarError(f"{path}:{line_no}: 'created_at' must be a string or null")

        records.append(
            SidecarRecord(
                content=content,
                content_hash=obs_hash,
                obs_type=obs_type,
                confidence=confidence,
                topics=list(topics),
                source_obs=list(source_obs),
                created_at=created_at,
            )
        )
    return records


def _write_pending_sources(conn: sqlite3.Connection, obs_id: int, hashes: Sequence[str]) -> None:
    """Persist source_obs hashes an observation references (Addendum A3).

    Idempotent (`INSERT OR IGNORE` on the `(observation_id, source_hash)`
    primary key). Written unconditionally for every referenced hash, not
    only unresolved ones — `resolve_pending_sources` deletes whichever
    resolve, at the end of THIS `ingest_all_sidecars` call or a later one, so
    a hash that happens to already be resolvable is simply cleaned up
    immediately rather than needing a separate "already known" code path.
    This is what makes a deduction survive a rebuild in which its source
    memo's sidecar hasn't synced in yet: the reference is remembered even
    though the referencing sidecar itself won't be re-parsed once ingested.
    """
    for h in hashes:
        conn.execute(
            "INSERT OR IGNORE INTO obs_pending_sources (observation_id, source_hash) VALUES (?, ?)",
            (obs_id, h),
        )


def _sync_type_confidence_topics(
    conn: sqlite3.Connection,
    obs_id: int,
    current_obs_type: str,
    current_confidence: str,
    record: SidecarRecord,
) -> tuple[bool, bool]:
    """Apply the sidecar record's obs_type/confidence/topics onto an existing
    row. Shared by the "in both" and "adopted" (Addendum A2) branches of
    `ingest_sidecar` — adoption is exactly an "in both" diff after a doc_path
    reassignment. Returns (updated, retagged)."""
    changes: dict[str, str] = {}
    if current_obs_type != record.obs_type:
        changes["obs_type"] = record.obs_type
    if current_confidence != record.confidence:
        changes["confidence"] = record.confidence
    updated = bool(changes)
    if changes:
        set_clause = ", ".join(f"{col} = ?" for col in changes)
        conn.execute(
            f"UPDATE observations SET {set_clause} WHERE id = ?",
            (*changes.values(), obs_id),
        )
        if "obs_type" in changes:
            conn.execute(
                "UPDATE fts_observations SET obs_type = ? WHERE rowid = ?",
                (changes["obs_type"], obs_id),
            )

    current_topics = {
        row[0]
        for row in conn.execute(
            "SELECT topic_slug FROM observation_topics WHERE observation_id = ?",
            (obs_id,),
        ).fetchall()
    }
    new_topics = set(record.topics)
    to_add = new_topics - current_topics
    to_remove = current_topics - new_topics
    retagged = bool(to_add or to_remove)
    if to_add or to_remove:
        for slug in to_add:
            conn.execute(
                "INSERT OR IGNORE INTO observation_topics "
                "(observation_id, topic_slug) VALUES (?, ?)",
                (obs_id, slug),
            )
        for slug in to_remove:
            conn.execute(
                "DELETE FROM observation_topics "
                "WHERE observation_id = ? AND topic_slug = ?",
                (obs_id, slug),
            )
    return updated, retagged


def ingest_sidecar(
    conn: sqlite3.Connection,
    vault: Path,
    doc_path: str,
    path: Path,
    *,
    indexed_paths: set[str] | None = None,
    raw: bytes | None = None,
) -> dict:
    """Diff one sidecar file into the DB, keyed by content_hash.

    `indexed_paths`, when given, is this run's set of doc_paths present on
    disk — used only to distinguish, for a content_hash that already exists
    under a DIFFERENT doc_path, a genuine live duplicate from a rename whose
    old memo is gone (Addendum A2; see `ingest_all_sidecars`'s docstring).

    Raises `SidecarError` (propagated from `read_sidecar`) on a malformed
    file — caller (`ingest_all_sidecars`) rolls back the per-file savepoint
    and does NOT record the file hash, so it's retried next run. Records the
    file hash in `obs_sidecars` on success, UNLESS this file contained a
    genuine foreign-hash conflict (Addendum A2) — that must re-surface every
    run rather than going stale. Does not commit.
    """
    # Single read: the hash recorded below is of exactly the bytes parsed here.
    if raw is None:
        raw = path.read_bytes()
    records = read_sidecar(path, raw.decode("utf-8"))  # may raise SidecarError

    existing_rows = conn.execute(
        "SELECT id, content_hash, obs_type, confidence FROM observations WHERE doc_path = ?",
        (doc_path,),
    ).fetchall()
    existing_by_hash = {row[1]: (row[0], row[2], row[3]) for row in existing_rows}
    file_by_hash = {r.content_hash: r for r in records}

    inserted = deleted = updated = retagged = skipped_foreign = adopted = 0
    pending_sources: list[tuple[int, list[str]]] = []
    foreign_conflicts: list[dict] = []

    # In DB, not in file -> delete. The only sanctioned deleter.
    from memex.observations import delete_observation_ids

    delete_ids = [
        obs_id for h, (obs_id, _, _) in existing_by_hash.items() if h not in file_by_hash
    ]
    if delete_ids:
        deleted = delete_observation_ids(conn, delete_ids)

    for obs_hash, record in file_by_hash.items():
        if obs_hash in existing_by_hash:
            obs_id, obs_type, confidence = existing_by_hash[obs_hash]
            did_update, did_retag = _sync_type_confidence_topics(
                conn, obs_id, obs_type, confidence, record
            )
            updated += int(did_update)
            retagged += int(did_retag)
            if record.source_obs:
                pending_sources.append((obs_id, record.source_obs))
                _write_pending_sources(conn, obs_id, record.source_obs)
            continue

        # Not in DB under this doc_path. content_hash is globally unique
        # (mirrors store_observations) — if it already exists under a
        # DIFFERENT doc_path, this is either a foreign duplicate or an
        # orphaned row waiting to be reclaimed by a rename.
        foreign = conn.execute(
            "SELECT id, doc_path, obs_type, confidence FROM observations WHERE content_hash = ?",
            (obs_hash,),
        ).fetchone()
        if foreign is not None and foreign[1] != doc_path:
            foreign_id, foreign_doc_path, foreign_type, foreign_confidence = foreign
            # Addendum A2: if the foreign doc_path is NOT among this run's
            # on-disk docs, its memo is gone — typically because this very
            # sidecar is the rename's arrival (new memo + new sidecar synced
            # in, old memo already gone). ADOPT the row: reassign doc_path,
            # keep id/fts/vec/topics, then apply the same diff as "in both".
            # Deliberately NOT adopted when the other doc is still live on
            # disk (a hand-move between two existing docs): that is
            # reported as a conflict until the source doc's sidecar drops
            # the line, after which the row is deleted there and inserted
            # here with a new id and no vector (one re-embed). Cheap, and
            # it never guesses which of two live docs owns the text.
            if indexed_paths is not None and foreign_doc_path not in indexed_paths:
                conn.execute(
                    "UPDATE observations SET doc_path = ? WHERE id = ?",
                    (doc_path, foreign_id),
                )
                _sync_type_confidence_topics(
                    conn, foreign_id, foreign_type, foreign_confidence, record
                )
                adopted += 1
                if record.source_obs:
                    pending_sources.append((foreign_id, record.source_obs))
                    _write_pending_sources(conn, foreign_id, record.source_obs)
            else:
                # A genuine cross-doc duplicate — the other doc is still
                # live. Skip rather than raise: two sidecars racing during a
                # move/copy is an operational reality, not corruption. Do
                # NOT record this file's hash (see caller) so the conflict
                # re-surfaces every run instead of going stale.
                skipped_foreign += 1
                foreign_conflicts.append(
                    {"doc_path": doc_path, "foreign_doc_path": foreign_doc_path}
                )
            continue

        cursor = conn.execute(
            """
            INSERT INTO observations
            (doc_path, content, content_hash, obs_type, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (doc_path, record.content, obs_hash, record.obs_type, record.confidence, record.created_at),
        )
        obs_id = int(cursor.lastrowid)
        # Same rowid invariant as extract.store_observations — see the
        # comment on fts_observations there.
        conn.execute(
            "INSERT INTO fts_observations(rowid, content, obs_type) VALUES (?, ?, ?)",
            (obs_id, record.content, record.obs_type),
        )
        if record.topics:
            store_observation_topics(conn, obs_id, record.topics)
        inserted += 1
        # No vector — the existing gap-heal (count_embedding_gaps /
        # reembed_missing / `memex index embed-missing`) embeds it later.
        if record.source_obs:
            pending_sources.append((obs_id, record.source_obs))
            _write_pending_sources(conn, obs_id, record.source_obs)

    if not foreign_conflicts:
        digest = hashlib.sha256(raw).hexdigest()
        _record_sidecar_hash(conn, doc_path, digest)

    return {
        "inserted": inserted,
        "deleted": deleted,
        "updated": updated,
        "retagged": retagged,
        "skipped_foreign": skipped_foreign,
        "adopted": adopted,
        "foreign_conflicts": foreign_conflicts,
        "pending_sources": pending_sources,
    }


def _resolve_hashes(conn: sqlite3.Connection, hashes: Sequence[str]) -> dict[str, int]:
    """Batched content_hash -> observation id lookup, chunked below the
    SQLite host-parameter ceiling like everywhere else in this module."""
    hash_to_id: dict[str, int] = {}
    for batch in _chunked(sorted(set(hashes))):
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        for obs_id, obs_hash in conn.execute(
            f"SELECT id, content_hash FROM observations WHERE content_hash IN ({placeholders})",
            batch,
        ).fetchall():
            hash_to_id[obs_hash] = obs_id
    return hash_to_id


def resolve_pending_sources(
    conn: sqlite3.Connection, pending: list[tuple[int, list[str]]] | None = None
) -> dict:
    """Resolve outstanding `source_obs` references to observation ids.

    Addendum A3 (2026-09-13): runs in two passes so a deduction ingested
    before its sources' sidecar synced in doesn't lose its provenance
    permanently just because its own file hash never changes again.

    1. `pending` — records ingested THIS call, in file order (from
       `ingest_sidecar`'s return value). Resolved ids are written to
       `source_obs_ids` preserving that file order — "file order where
       known".
    2. The ENTIRE `obs_pending_sources` table is swept regardless, including
       rows left over from a PRIOR call whose sidecar is unchanged this run
       (and so was never re-parsed, never appeared in `pending`). Order is
       not known for these, so newly resolved ids are appended to whatever
       `source_obs_ids` already holds rather than replacing it.

    Either way, resolved (observation_id, source_hash) pairs are deleted
    from `obs_pending_sources`; unresolved ones are left for the next call.
    Runs unconditionally — even when `pending` is empty — because pass 2
    must still retry old pending rows. Does not commit.
    """
    pending = pending or []
    resolved = 0

    # Pass 1: this run's freshly-ingested records, in file order.
    if pending:
        hash_to_id = _resolve_hashes(conn, [h for _, hashes in pending for h in hashes])
        for obs_id, hashes in pending:
            ids = [hash_to_id[h] for h in hashes if h in hash_to_id]
            resolved += len(ids)
            if ids:
                new_value = encode_source_ids(ids)
                current = conn.execute(
                    "SELECT source_obs_ids FROM observations WHERE id = ?", (obs_id,)
                ).fetchone()
                if current is not None and current[0] != new_value:
                    conn.execute(
                        "UPDATE observations SET source_obs_ids = ? WHERE id = ?",
                        (new_value, obs_id),
                    )
                for h in hashes:
                    if h in hash_to_id:
                        conn.execute(
                            "DELETE FROM obs_pending_sources "
                            "WHERE observation_id = ? AND source_hash = ?",
                            (obs_id, h),
                        )

    # Pass 2: sweep whatever remains (leftovers from prior runs, plus
    # anything pass 1 just wrote for this run's records that didn't
    # resolve). Queried fresh, after pass 1's deletes, so nothing here was
    # already handled above.
    remaining = conn.execute(
        "SELECT observation_id, source_hash FROM obs_pending_sources"
    ).fetchall()
    by_obs: dict[int, list[str]] = {}
    for obs_id, h in remaining:
        by_obs.setdefault(obs_id, []).append(h)

    if by_obs:
        hash_to_id = _resolve_hashes(conn, [h for hashes in by_obs.values() for h in hashes])
        for obs_id, hashes in by_obs.items():
            newly_resolved = [hash_to_id[h] for h in hashes if h in hash_to_id]
            if newly_resolved:
                current = conn.execute(
                    "SELECT source_obs_ids FROM observations WHERE id = ?", (obs_id,)
                ).fetchone()
                if current is not None:
                    existing_ids = decode_source_ids(current[0])
                    merged = existing_ids + [i for i in newly_resolved if i not in existing_ids]
                    new_value = encode_source_ids(merged)
                    if new_value != current[0]:
                        conn.execute(
                            "UPDATE observations SET source_obs_ids = ? WHERE id = ?",
                            (new_value, obs_id),
                        )
                    resolved += len(newly_resolved)
                # Clear the resolved pending rows whether or not the parent
                # observation still exists — a deleted observation's
                # leftovers must not accumulate forever either.
                for h in hashes:
                    if h in hash_to_id:
                        conn.execute(
                            "DELETE FROM obs_pending_sources "
                            "WHERE observation_id = ? AND source_hash = ?",
                            (obs_id, h),
                        )

    unresolved = conn.execute("SELECT COUNT(*) FROM obs_pending_sources").fetchone()[0]
    return {"resolved": resolved, "unresolved": unresolved}


def ingest_all_sidecars(
    conn: sqlite3.Connection,
    vault: Path,
    *,
    indexed_paths: set[str] | None = None,
    force: bool = False,
) -> dict:
    """Diff every sidecar under `vault` into the DB.

    `indexed_paths`, when given, is the set of doc_paths present in
    `main.fts_content` for THIS run (rebuild callers pass it). A sidecar
    whose doc is not in it is skipped and its hash is NOT recorded — the
    doc may simply not exist yet (mid-iCloud-sync) or was filtered out
    (archived), and either way the sidecar must be retried next run rather
    than treated as consumed. Callers outside a rebuild (`memex obs
    ingest-sidecars`) pass `indexed_paths=None` to ingest against whatever
    the doc's disk state currently is — and, per Addendum A2, `None` also
    disables the "adopt an orphaned row" behavior in `ingest_sidecar` (with
    no on-disk snapshot to consult, a foreign hash is always treated as a
    live duplicate, never an adoption candidate).

    Addendum A1 (2026-09-13): a zero-byte or whitespace-only sidecar is
    never authoritative — almost always an iCloud mid-transfer placeholder,
    never intent. It is skipped entirely: counted under `empty`, its hash is
    NOT recorded (so a still-empty file is re-checked, not silently
    accepted, once real content lands), and nothing is deleted.
    """
    stats = {
        "files": 0,
        "ingested": 0,
        "unchanged": 0,
        "skipped_no_doc": 0,
        "empty": 0,
        "errors": 0,
        "inserted": 0,
        "deleted": 0,
        "updated": 0,
        "retagged": 0,
        "skipped_foreign": 0,
        "adopted": 0,
        "foreign_conflicts": [],
    }
    pending_all: list[tuple[int, list[str]]] = []

    for path in find_sidecars(vault):
        stats["files"] += 1
        doc_path = doc_path_for_sidecar(vault, path)

        # One read per file: emptiness, the unchanged check, the parse and
        # the recorded hash all come from these same bytes. A
        # UnicodeDecodeError (truncated multi-byte char mid-transfer) is a
        # per-file error, not a rebuild abort — it is retried next run.
        try:
            raw_bytes = path.read_bytes()
            raw = raw_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  error reading {path}: {exc}", file=sys.stderr)
            stats["errors"] += 1
            continue
        if not raw.strip():
            stats["empty"] += 1
            continue

        if indexed_paths is not None and doc_path not in indexed_paths:
            stats["skipped_no_doc"] += 1
            continue

        if not force:
            row = conn.execute(
                "SELECT content_hash FROM obs_sidecars WHERE doc_path = ?", (doc_path,)
            ).fetchone()
            if row is not None and row[0] == hashlib.sha256(raw_bytes).hexdigest():
                stats["unchanged"] += 1
                continue

        conn.execute("SAVEPOINT sidecar")
        try:
            result = ingest_sidecar(
                conn, vault, doc_path, path, indexed_paths=indexed_paths, raw=raw_bytes
            )
        except Exception as exc:
            _rollback_savepoint_or_die(conn, "sidecar")
            _release_savepoint_if_exists(conn, "sidecar")
            kind = "malformed sidecar" if isinstance(exc, SidecarError) else "error"
            print(f"  {kind} {path}: {exc}", file=sys.stderr)
            stats["errors"] += 1
            continue

        conn.execute("RELEASE SAVEPOINT sidecar")
        stats["ingested"] += 1
        for key in ("inserted", "deleted", "updated", "retagged", "skipped_foreign", "adopted"):
            stats[key] += result[key]
        stats["foreign_conflicts"].extend(result["foreign_conflicts"])
        pending_all.extend(result["pending_sources"])

    # Addendum A3: this phase runs unconditionally, even when every file was
    # `unchanged` — it must retry pending rows left over from a prior run
    # whose referencing sidecar hasn't changed since (and so was never
    # re-parsed above). Isolated in its own SAVEPOINT so a bug here can't
    # take down an otherwise-successful ingest pass.
    conn.execute("SAVEPOINT sidecar_pending")
    try:
        resolved = resolve_pending_sources(conn, pending_all)
        conn.execute("RELEASE SAVEPOINT sidecar_pending")
    except Exception as exc:
        _rollback_savepoint_or_die(conn, "sidecar_pending")
        _release_savepoint_if_exists(conn, "sidecar_pending")
        print(f"  pending-source resolution error: {exc}", file=sys.stderr)
        resolved = {"resolved": 0, "unresolved": 0}
    stats["pending_sources_resolved"] = resolved["resolved"]
    stats["pending_sources_unresolved"] = resolved["unresolved"]
    # Addendum A7's requested key: the outstanding count after this call's
    # phase 2, for the health report and rebuild-summary line.
    stats["pending_sources"] = resolved["unresolved"]
    return stats


def export_sidecars(conn: sqlite3.Connection, vault: Path, *, apply: bool, force: bool) -> dict:
    """One-off (or repeatable) migration: write every doc's sidecar from the
    DB. Never overwrites a differing existing sidecar without `force` — on a
    machine whose DB is a stale copy, exporting would clobber sidecars
    synced from the machine of record. Caller commits."""
    stats: dict = {
        "unportable": [],
        "current": 0,
        "conflict": [],
        "written": 0,
        "would_write": 0,
    }
    doc_paths = [
        row[0] for row in conn.execute("SELECT DISTINCT doc_path FROM observations").fetchall()
    ]
    for doc_path in doc_paths:
        path = sidecar_path(vault, doc_path)
        if path is None:
            stats["unportable"].append(doc_path)
            continue

        scrub_doc_observations(conn, doc_path)
        scrubbed = render_sidecar(conn, doc_path)
        if not scrubbed:
            continue  # a doc_path from DISTINCT observations always has rows; defensive only

        if path.exists() and not force:
            existing = path.read_text(encoding="utf-8")
            if existing == scrubbed:
                stats["current"] += 1
                # Already matching — still record it as ingested, or status
                # counts it as "pending ingest" forever.
                if apply:
                    _record_sidecar_hash(
                        conn, doc_path, hashlib.sha256(scrubbed.encode("utf-8")).hexdigest()
                    )
            else:
                stats["conflict"].append(doc_path)
            continue

        if not apply:
            stats["would_write"] += 1
            continue

        if not path.parent.exists():
            print(
                f"Warning: parent directory missing for sidecar {path} — "
                "skipping export",
                file=sys.stderr,
            )
            continue
        _atomic_write(path, scrubbed)
        digest = hashlib.sha256(scrubbed.encode("utf-8")).hexdigest()
        _record_sidecar_hash(conn, doc_path, digest)
        stats["written"] += 1

    return stats


def sidecar_health(conn: sqlite3.Connection, vault: Path) -> dict:
    """Health report backing `memex obs sidecars`.

    - `missing`: docs with DB observations but no sidecar file on disk.
    - `orphan`: sidecars whose document is missing on disk or not indexed
      (`fts_content`, when present).
    - `stale`: sidecars whose current file hash differs from the last
      recorded `obs_sidecars` hash (pending ingest).
    - `empty`: sidecars that are zero-byte/whitespace-only (Addendum A1) —
      never authoritative, never ingested.
    - `conflicts`: iCloud conflict copies (`find_sidecar_conflicts`).
    - `unportable`: doc_paths that can't map to a sidecar path at all.
    - `foreign_conflicts`: sidecar records whose content_hash already exists
      under a different, still-live doc_path (Addendum A2) — read-only
      surfacing of what the next ingest would report as `skipped_foreign`;
      does not attempt the adopt/skip distinction an actual ingest makes.
    - `pending_sources`: count of unresolved `source_obs` references
      (Addendum A3), i.e. rows currently in `obs_pending_sources`.
    """
    sidecars = find_sidecars(vault)
    conflicts = find_sidecar_conflicts(vault)

    try:
        fts_paths: set[str] | None = {
            row[0] for row in conn.execute("SELECT DISTINCT path FROM fts_content").fetchall()
        }
    except sqlite3.OperationalError:
        # No fts_content table (minimal/legacy index) — "indexed" can't be
        # asked, so don't manufacture false orphans over it.
        fts_paths = None

    sidecar_doc_paths: set[str] = set()
    orphans: list[str] = []
    stale: list[str] = []
    empty: list[str] = []
    unreadable: list[str] = []
    foreign_conflicts: list[dict] = []
    for path in sidecars:
        doc_path = doc_path_for_sidecar(vault, path)
        sidecar_doc_paths.add(doc_path)

        try:
            raw_bytes = path.read_bytes()
            raw = raw_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(doc_path)
            continue
        if not raw.strip():
            # A1: an empty sidecar is its own category, not orphan/stale —
            # it carries no observations to be stale about, and its doc may
            # be perfectly live.
            empty.append(doc_path)
            continue

        on_disk = (vault / doc_path).exists()
        indexed = fts_paths is None or doc_path in fts_paths
        if not on_disk or not indexed:
            orphans.append(doc_path)
            continue
        row = conn.execute(
            "SELECT content_hash FROM obs_sidecars WHERE doc_path = ?", (doc_path,)
        ).fetchone()
        if row is None or row[0] != hashlib.sha256(raw_bytes).hexdigest():
            stale.append(doc_path)

        # A2: read-only cross-doc content_hash collision check — no writes,
        # so this can't tell adopt from conflict (that needs an on-disk
        # snapshot of the whole vault, which only a rebuild has). Surfaced
        # anyway as a diagnostic: any collision here is worth a look.
        try:
            for record in read_sidecar(path, raw):
                foreign = conn.execute(
                    "SELECT doc_path FROM observations "
                    "WHERE content_hash = ? AND doc_path != ?",
                    (record.content_hash, doc_path),
                ).fetchone()
                if foreign is not None:
                    foreign_conflicts.append(
                        {"doc_path": doc_path, "foreign_doc_path": foreign[0]}
                    )
        except SidecarError:
            pass  # already implied by `stale`/malformed-on-next-ingest; not this report's job

    obs_doc_paths = {
        row[0] for row in conn.execute("SELECT DISTINCT doc_path FROM observations").fetchall()
    }
    missing: list[str] = []
    unportable: list[str] = []
    for doc_path in sorted(obs_doc_paths):
        path = sidecar_path(vault, doc_path)
        if path is None:
            unportable.append(doc_path)
            continue
        if doc_path not in sidecar_doc_paths and not path.exists():
            missing.append(doc_path)

    try:
        pending_sources = conn.execute(
            "SELECT COUNT(*) FROM obs_pending_sources"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        pending_sources = 0  # pre-Addendum-A3 schema — nothing to report

    return {
        "sidecar_count": len(sidecars),
        "missing": sorted(missing),
        "orphan": sorted(orphans),
        "stale": sorted(stale),
        "empty": sorted(empty),
        "unreadable": sorted(unreadable),
        "conflicts": [str(p) for p in conflicts],
        "unportable": sorted(unportable),
        "foreign_conflicts": foreign_conflicts,
        "pending_sources": pending_sources,
    }
