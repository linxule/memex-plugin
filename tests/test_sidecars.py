"""Tests for memex.sidecars — vault-backed observations via `.obs.jsonl`.

See docs/2026-09-13-obs-sidecar-spec.md for the full design. All tests use
tmp_path vaults and tmp_path indexes — never the real vault or ~/.memex.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from memex.observations import (
    count_orphaned_observation_rows,
    init_observation_schema,
    store_observation_topics,
)
from memex.sidecars import (
    SidecarError,
    doc_path_for_sidecar,
    export_sidecars,
    file_hash,
    find_sidecar_conflicts,
    find_sidecars,
    ingest_all_sidecars,
    ingest_sidecar,
    read_sidecar,
    render_sidecar,
    sidecar_health,
    sidecar_path,
    write_sidecar,
)


def _conn(dim: int = 8) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_observation_schema(conn, dim)
    return conn


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "projects" / "p" / "memos").mkdir(parents=True)
    (vault / "topics").mkdir(parents=True)
    return vault


def _insert_obs(
    conn: sqlite3.Connection,
    obs_id: int,
    doc_path: str,
    content: str,
    *,
    content_hash: str | None = None,
    obs_type: str = "explicit",
    confidence: str = "high",
    source_obs_ids: str | None = None,
    created_at: str = "2026-09-13 00:00:00",
) -> None:
    # Default to the REAL sha256 of `content` — read_sidecar validates the
    # hash against the content, so a fabricated hash like "h1" would make
    # every round-trip test raise SidecarError on read-back.
    conn.execute(
        "INSERT INTO observations "
        "(id, doc_path, content, content_hash, obs_type, confidence, "
        "source_obs_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            obs_id,
            doc_path,
            content,
            content_hash or _hash(content),
            obs_type,
            confidence,
            source_obs_ids,
            created_at,
        ),
    )
    conn.execute(
        "INSERT INTO fts_observations (rowid, content, obs_type) VALUES (?, ?, ?)",
        (obs_id, content, obs_type),
    )


# ── path derivation ─────────────────────────────────────────────────────────


def test_sidecar_path_for_memo(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    path = sidecar_path(vault, "projects/p/memos/x.md")
    assert path == vault / "projects" / "p" / "memos" / "x.obs.jsonl"


def test_sidecar_path_for_project_overview(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    path = sidecar_path(vault, "projects/p/_project.md")
    assert path == vault / "projects" / "p" / "_project.obs.jsonl"


def test_sidecar_path_for_topic(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    path = sidecar_path(vault, "topics/t.md")
    assert path == vault / "topics" / "t.obs.jsonl"


def test_sidecar_path_absolute_doc_path_returns_none(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    assert sidecar_path(vault, "/etc/passwd.md") is None


def test_sidecar_path_escaping_vault_returns_none(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    assert sidecar_path(vault, "../../../../etc/passwd.md") is None


def test_doc_path_for_sidecar_is_inverse_of_sidecar_path(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc_path = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc_path)
    assert doc_path_for_sidecar(vault, path) == doc_path


# ── render / write / read round-trip ────────────────────────────────────────


def test_render_write_read_round_trip_is_ordered_and_byte_stable(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 2, doc, "second")
        _insert_obs(conn, 1, doc, "first")
        store_observation_topics(conn, 1, ["topic-b", "topic-a"])
        conn.commit()

        path = write_sidecar(conn, vault, doc)
        conn.commit()
        assert path == vault / "projects" / "p" / "memos" / "x.obs.jsonl"
        assert path.exists()

        records = read_sidecar(path)
        assert [r.content for r in records] == ["first", "second"]
        assert records[0].topics == ["topic-a", "topic-b"]

        # Byte-stable: re-rendering identical DB state produces identical bytes.
        rendered_again = render_sidecar(conn, doc)
        assert path.read_text(encoding="utf-8") == rendered_again
    finally:
        conn.close()


def test_source_obs_renders_hashes_and_drops_unresolvable(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "source one")
        _insert_obs(conn, 2, doc, "source two")
        # id 999 does not exist — must be dropped, not raise.
        _insert_obs(
            conn, 3, doc, "deduction",
            source_obs_ids=json.dumps([1, 2, 999]),
        )
        conn.commit()

        path = write_sidecar(conn, vault, doc)
        conn.commit()
        records = {r.content_hash: r for r in read_sidecar(path)}
        deduction = records[_hash("deduction")]
        assert sorted(deduction.source_obs) == sorted(
            [_hash("source one"), _hash("source two")]
        )
    finally:
        conn.close()


def test_zero_observations_removes_sidecar_and_registry_row(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "only one")
        conn.commit()
        path = write_sidecar(conn, vault, doc)
        conn.commit()
        assert path is not None and path.exists()

        conn.execute("DELETE FROM observations WHERE id = 1")
        conn.execute("DELETE FROM fts_observations WHERE rowid = 1")
        result = write_sidecar(conn, vault, doc)
        conn.commit()

        assert result is None
        assert not path.exists()
        row = conn.execute(
            "SELECT * FROM obs_sidecars WHERE doc_path = ?", (doc,)
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_parent_dir_missing_warns_and_skips(tmp_path: Path, capsys) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/missing-project/memos/x.md"  # parent dir never created
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "orphaned parent")
        conn.commit()
        result = write_sidecar(conn, vault, doc)
        assert result is None
        err = capsys.readouterr().err
        assert "parent directory missing" in err.lower()
        assert not (vault / "projects" / "missing-project").exists()
    finally:
        conn.close()


def test_atomic_write_leaves_no_temp_file(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "content")
        conn.commit()
        write_sidecar(conn, vault, doc)
        conn.commit()
        leftovers = list((vault / "projects" / "p" / "memos").glob(".*"))
        assert leftovers == [], f"stray temp files: {leftovers}"
    finally:
        conn.close()


# ── read_sidecar malformed input ────────────────────────────────────────────


def test_read_sidecar_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.obs.jsonl"
    obj = {"content": "hi", "content_hash": _hash("hi")}
    path.write_text(f"\n{json.dumps(obj)}\n\n")
    records = read_sidecar(path)
    assert len(records) == 1
    assert records[0].content == "hi"


def test_read_sidecar_applies_defaults(tmp_path: Path) -> None:
    path = tmp_path / "x.obs.jsonl"
    obj = {"content": "hi", "content_hash": _hash("hi")}
    path.write_text(json.dumps(obj) + "\n")
    record = read_sidecar(path)[0]
    assert record.obs_type == "explicit"
    assert record.confidence == "high"
    assert record.topics == []
    assert record.source_obs == []
    assert record.created_at is None


def test_read_sidecar_invalid_json_raises_for_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "x.obs.jsonl"
    path.write_text('{"content": "ok", "content_hash": "%s"}\nnot json\n' % _hash("ok"))
    with pytest.raises(SidecarError):
        read_sidecar(path)


def test_read_sidecar_hash_mismatch_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.obs.jsonl"
    path.write_text(json.dumps({"content": "hi", "content_hash": "wrong"}) + "\n")
    with pytest.raises(SidecarError):
        read_sidecar(path)


def test_read_sidecar_non_dict_line_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.obs.jsonl"
    path.write_text("[1, 2, 3]\n")
    with pytest.raises(SidecarError):
        read_sidecar(path)


def test_read_sidecar_empty_content_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.obs.jsonl"
    path.write_text(json.dumps({"content": "", "content_hash": _hash("")}) + "\n")
    with pytest.raises(SidecarError):
        read_sidecar(path)


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(o, sort_keys=True) + "\n" for o in objs),
        encoding="utf-8",
    )


# ── ingest diff ──────────────────────────────────────────────────────────────


def test_ingest_inserts_new_observation_with_fts_and_topics(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    _write_jsonl(path, [
        {
            "content": "new claim", "content_hash": _hash("new claim"),
            "obs_type": "explicit", "confidence": "high",
            "topics": ["topic-a"], "source_obs": [], "created_at": "2026-01-01 00:00:00",
        },
    ])
    conn = _conn()
    try:
        result = ingest_sidecar(conn, vault, doc, path)
        conn.commit()
        assert result == {
            "inserted": 1, "deleted": 0, "updated": 0, "retagged": 0,
            "skipped_foreign": 0, "adopted": 0, "foreign_conflicts": [],
            "pending_sources": [],
        }
        row = conn.execute(
            "SELECT content, created_at FROM observations WHERE doc_path = ?", (doc,)
        ).fetchone()
        assert row == ("new claim", "2026-01-01 00:00:00")
        obs_id = conn.execute(
            "SELECT id FROM observations WHERE doc_path = ?", (doc,)
        ).fetchone()[0]
        fts_row = conn.execute(
            "SELECT content FROM fts_observations WHERE rowid = ?", (obs_id,)
        ).fetchone()
        assert fts_row == ("new claim",)
        topics = {
            r[0] for r in conn.execute(
                "SELECT topic_slug FROM observation_topics WHERE observation_id = ?",
                (obs_id,),
            )
        }
        assert topics == {"topic-a"}
    finally:
        conn.close()


def test_ingest_deletes_rows_absent_from_file(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    _write_jsonl(path, [])  # empty file: nothing survives

    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "gone soon", content_hash=_hash("gone soon"))
        store_observation_topics(conn, 1, ["topic-a"])
        conn.commit()

        result = ingest_sidecar(conn, vault, doc, path)
        conn.commit()
        assert result["deleted"] == 1
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        orphans = count_orphaned_observation_rows(conn)
        assert orphans.get("fts_observations", 0) == 0
        assert orphans.get("observation_topics", 0) == 0
    finally:
        conn.close()


def test_ingest_updates_type_and_confidence_and_retags(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    content = "claim under revision"
    _write_jsonl(path, [
        {
            "content": content, "content_hash": _hash(content),
            "obs_type": "deductive", "confidence": "medium",
            "topics": ["topic-new"], "source_obs": [],
        },
    ])
    conn = _conn()
    try:
        _insert_obs(
            conn, 1, doc, content, content_hash=_hash(content),
            obs_type="explicit", confidence="high",
        )
        store_observation_topics(conn, 1, ["topic-old"])
        conn.commit()

        result = ingest_sidecar(conn, vault, doc, path)
        conn.commit()
        assert result["updated"] == 1
        assert result["retagged"] == 1
        row = conn.execute(
            "SELECT obs_type, confidence FROM observations WHERE id = 1"
        ).fetchone()
        assert row == ("deductive", "medium")
        fts_type = conn.execute(
            "SELECT obs_type FROM fts_observations WHERE rowid = 1"
        ).fetchone()[0]
        assert fts_type == "deductive"
        topics = {
            r[0] for r in conn.execute(
                "SELECT topic_slug FROM observation_topics WHERE observation_id = 1"
            )
        }
        assert topics == {"topic-new"}
    finally:
        conn.close()


def test_ingest_unchanged_row_keeps_id_and_vector(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    content = "steady claim"
    _write_jsonl(path, [
        {
            "content": content, "content_hash": _hash(content),
            "obs_type": "explicit", "confidence": "high",
            "topics": [], "source_obs": [],
        },
    ])
    conn = _conn(8)
    try:
        _insert_obs(conn, 1, doc, content, content_hash=_hash(content))
        conn.commit()
        try:
            conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            vec_available = True
        except Exception:
            vec_available = False
        if vec_available:
            blob = struct.pack("8f", *([0.1] * 8))
            conn.execute(
                "INSERT INTO vec_observations(rowid, embedding, doc_project, doc_type, doc_date) "
                "VALUES (1, ?, 'p', 'memo', 20260913)",
                (blob,),
            )
            conn.commit()

        result = ingest_sidecar(conn, vault, doc, path)
        conn.commit()
        assert result == {
            "inserted": 0, "deleted": 0, "updated": 0, "retagged": 0,
            "skipped_foreign": 0, "adopted": 0, "foreign_conflicts": [],
            "pending_sources": [],
        }
        assert conn.execute("SELECT id FROM observations").fetchone()[0] == 1
        if vec_available:
            assert conn.execute(
                "SELECT COUNT(*) FROM vec_observations WHERE rowid = 1"
            ).fetchone()[0] == 1
    finally:
        conn.close()


def test_ingest_skips_foreign_hash(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc_a = "projects/p/memos/a.md"
    doc_b = "projects/p/memos/b.md"
    content = "shared text"
    path_b = sidecar_path(vault, doc_b)
    _write_jsonl(path_b, [
        {
            "content": content, "content_hash": _hash(content),
            "obs_type": "explicit", "confidence": "high",
            "topics": [], "source_obs": [],
        },
    ])
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc_a, content, content_hash=_hash(content))
        conn.commit()

        result = ingest_sidecar(conn, vault, doc_b, path_b)
        conn.commit()
        assert result["skipped_foreign"] == 1
        assert result["inserted"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path = ?", (doc_b,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_pending_sources_resolved_across_two_files(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc_a = "projects/p/memos/a.md"
    doc_project = "projects/p/_project.md"

    src_content = "source claim"
    ded_content = "deduction referencing source"
    path_a = sidecar_path(vault, doc_a)
    _write_jsonl(path_a, [
        {
            "content": src_content, "content_hash": _hash(src_content),
            "obs_type": "explicit", "confidence": "high",
            "topics": [], "source_obs": [],
        },
    ])
    path_project = sidecar_path(vault, doc_project)
    _write_jsonl(path_project, [
        {
            "content": ded_content, "content_hash": _hash(ded_content),
            "obs_type": "deductive", "confidence": "medium",
            "topics": [], "source_obs": [_hash(src_content), _hash("nonexistent")],
        },
    ])

    conn = _conn()
    try:
        stats = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats["errors"] == 0
        assert stats["pending_sources_resolved"] == 1
        assert stats["pending_sources_unresolved"] == 1

        ded_row = conn.execute(
            "SELECT source_obs_ids FROM observations WHERE content = ?", (ded_content,)
        ).fetchone()
        from memex.observations import decode_source_ids
        source_ids = decode_source_ids(ded_row[0])
        assert len(source_ids) == 1
        src_id = conn.execute(
            "SELECT id FROM observations WHERE content = ?", (src_content,)
        ).fetchone()[0]
        assert source_ids == [src_id]
    finally:
        conn.close()


# ── ingest_all_sidecars ──────────────────────────────────────────────────────


def test_ingest_all_skips_unchanged_hash(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    _write_jsonl(path, [
        {"content": "c", "content_hash": _hash("c"), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    conn = _conn()
    try:
        stats1 = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats1["ingested"] == 1
        assert stats1["unchanged"] == 0

        stats2 = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats2["ingested"] == 0
        assert stats2["unchanged"] == 1
    finally:
        conn.close()


def test_ingest_all_reingests_changed_file(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    _write_jsonl(path, [
        {"content": "c1", "content_hash": _hash("c1"), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    conn = _conn()
    try:
        ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()

        _write_jsonl(path, [
            {"content": "c1", "content_hash": _hash("c1"), "obs_type": "explicit",
             "confidence": "high", "topics": [], "source_obs": []},
            {"content": "c2", "content_hash": _hash("c2"), "obs_type": "explicit",
             "confidence": "high", "topics": [], "source_obs": []},
        ])
        stats = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats["ingested"] == 1
        assert stats["inserted"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path = ?", (doc,)
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_ingest_all_skips_sidecar_without_doc_and_does_not_record_hash(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    _write_jsonl(path, [
        {"content": "c", "content_hash": _hash("c"), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    conn = _conn()
    try:
        # indexed_paths given but doesn't include `doc` — memo missing on
        # disk mid-sync, or archived.
        stats = ingest_all_sidecars(conn, vault, indexed_paths=set())
        conn.commit()
        assert stats["skipped_no_doc"] == 1
        assert stats["ingested"] == 0
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        row = conn.execute(
            "SELECT * FROM obs_sidecars WHERE doc_path = ?", (doc,)
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_ingest_all_broken_file_counts_error_others_still_ingest(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    good_doc = "projects/p/memos/good.md"
    bad_doc = "projects/p/memos/bad.md"
    good_path = sidecar_path(vault, good_doc)
    bad_path = sidecar_path(vault, bad_doc)
    _write_jsonl(good_path, [
        {"content": "good", "content_hash": _hash("good"), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    bad_path.write_text('{"content": "bad", "content_hash": "wrong"}\n')

    conn = _conn()
    try:
        stats = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats["errors"] == 1
        assert stats["ingested"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path = ?", (good_doc,)
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT * FROM obs_sidecars WHERE doc_path = ?", (bad_doc,)
        ).fetchone()
        assert row is None, "hash of a broken sidecar must not be recorded"
    finally:
        conn.close()


def test_ingest_all_force_reingests_even_when_unchanged(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    _write_jsonl(path, [
        {"content": "c", "content_hash": _hash("c"), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    conn = _conn()
    try:
        ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        stats = ingest_all_sidecars(conn, vault, indexed_paths=None, force=True)
        conn.commit()
        assert stats["unchanged"] == 0
        assert stats["ingested"] == 1
    finally:
        conn.close()


# ── export_sidecars ──────────────────────────────────────────────────────────


def test_export_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "exported claim")
        conn.commit()
        stats = export_sidecars(conn, vault, apply=False, force=False)
        assert stats["would_write"] == 1
        assert stats["written"] == 0
        assert not sidecar_path(vault, doc).exists()
    finally:
        conn.close()


def test_export_apply_writes_and_records(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "exported claim")
        conn.commit()
        stats = export_sidecars(conn, vault, apply=True, force=False)
        conn.commit()
        assert stats["written"] == 1
        path = sidecar_path(vault, doc)
        assert path.exists()
        row = conn.execute(
            "SELECT content_hash FROM obs_sidecars WHERE doc_path = ?", (doc,)
        ).fetchone()
        assert row is not None and row[0] == file_hash(path)
    finally:
        conn.close()


def test_export_existing_identical_reports_current(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "exported claim")
        conn.commit()
        export_sidecars(conn, vault, apply=True, force=False)
        conn.commit()
        stats = export_sidecars(conn, vault, apply=True, force=False)
        conn.commit()
        assert stats["current"] == 1
        assert stats["written"] == 0
    finally:
        conn.close()


def test_export_conflict_untouched_without_force_overwritten_with_it(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "exported claim")
        conn.commit()
        path = sidecar_path(vault, doc)
        path.write_text('{"content": "stale copy", "content_hash": "%s"}\n' % _hash("stale copy"))

        stats = export_sidecars(conn, vault, apply=True, force=False)
        conn.commit()
        assert stats["conflict"] == [doc]
        assert path.read_text() == '{"content": "stale copy", "content_hash": "%s"}\n' % _hash("stale copy")

        stats_forced = export_sidecars(conn, vault, apply=True, force=True)
        conn.commit()
        assert stats_forced["written"] == 1
        assert "exported claim" in path.read_text()
    finally:
        conn.close()


def test_export_absolute_doc_path_is_unportable(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    conn = _conn()
    try:
        _insert_obs(conn, 1, "/etc/passwd.md", "legacy absolute row")
        conn.commit()
        stats = export_sidecars(conn, vault, apply=True, force=False)
        conn.commit()
        assert stats["unportable"] == ["/etc/passwd.md"]
    finally:
        conn.close()


# ── conflict-copy glob ───────────────────────────────────────────────────────


def test_find_sidecar_conflicts_lists_icloud_conflict_copies(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    memos = vault / "projects" / "p" / "memos"
    conflict = memos / "x.obs 2.jsonl"
    conflict.write_text("")
    real = memos / "x.obs.jsonl"
    real.write_text("")

    conflicts = find_sidecar_conflicts(vault)
    assert conflict in conflicts

    sidecars = find_sidecars(vault)
    assert real in sidecars
    assert conflict not in sidecars


# ── sidecar_health ───────────────────────────────────────────────────────────


def test_sidecar_health_reports_missing_and_stale(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    # sidecar_health distinguishes "orphan" (doc missing on disk) from
    # "stale" (doc present, sidecar hash drifted) — the doc file must
    # actually exist for this test to exercise the stale branch.
    (vault / doc).write_text("---\ntype: memo\n---\n\nBody.\n")
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc, "claim")
        conn.commit()

        report = sidecar_health(conn, vault)
        assert doc in report["missing"]

        write_sidecar(conn, vault, doc)
        conn.commit()
        report = sidecar_health(conn, vault)
        assert doc not in report["missing"]
        assert report["stale"] == []

        # Hand-edit the sidecar file without re-ingesting -> stale (file hash
        # now differs from the recorded obs_sidecars hash).
        path = sidecar_path(vault, doc)
        path.write_text(path.read_text() + "\n")
        report = sidecar_health(conn, vault)
        assert doc in report["stale"]
    finally:
        conn.close()


# ── Addendum A — 2026-09-13 adversarial review ────────────────────────────
#
# A1: an empty sidecar is never authoritative. A2: `ingest_sidecar` adopts an
# orphaned row across a rename instead of losing it to the deleted-doc loop,
# and a genuine foreign-hash conflict re-surfaces every run. A3: unresolved
# `source_obs` references are remembered in `obs_pending_sources` and retried
# on every `ingest_all_sidecars` call, even one where the referencing
# sidecar itself is unchanged.


def test_ingest_all_skips_empty_sidecar_and_does_not_record_hash(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    doc = "projects/p/memos/x.md"
    path = sidecar_path(vault, doc)
    path.write_text("")  # zero-byte — an iCloud mid-transfer placeholder shape

    conn = _conn()
    try:
        # A doc with existing observations must NOT be wiped by an empty
        # sidecar arriving mid-sync.
        _insert_obs(conn, 1, doc, "must survive")
        conn.commit()

        stats = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats["empty"] == 1
        assert stats["ingested"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path = ?", (doc,)
        ).fetchone()[0] == 1, "an empty sidecar must never delete existing rows"
        row = conn.execute(
            "SELECT * FROM obs_sidecars WHERE doc_path = ?", (doc,)
        ).fetchone()
        assert row is None, "an empty sidecar's hash must never be recorded"

        # Whitespace-only counts the same as zero-byte.
        path.write_text("   \n\n\t\n")
        stats2 = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats2["empty"] == 1
    finally:
        conn.close()


def test_ingest_adopts_orphaned_row_across_rename_keeps_id_and_vector(tmp_path: Path) -> None:
    """A rename arrives as: new memo + new sidecar (same content_hash)
    present, old memo (and its doc_path) gone from `indexed_paths`. The row
    must be reassigned in place — same id, existing vector untouched — not
    deleted-then-reinserted."""
    vault = _make_vault(tmp_path)
    old_doc = "projects/p/memos/old.md"
    new_doc = "projects/p/memos/new.md"
    content = "renamed claim"
    new_path = sidecar_path(vault, new_doc)
    _write_jsonl(new_path, [
        {"content": content, "content_hash": _hash(content), "obs_type": "explicit",
         "confidence": "high", "topics": ["kept-topic"], "source_obs": []},
    ])

    conn = _conn(8)
    try:
        _insert_obs(conn, 1, old_doc, content, content_hash=_hash(content))
        store_observation_topics(conn, 1, ["kept-topic"])
        conn.commit()
        try:
            conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            vec_available = True
        except Exception:
            vec_available = False
        if vec_available:
            blob = struct.pack("8f", *([0.3] * 8))
            conn.execute(
                "INSERT INTO vec_observations(rowid, embedding, doc_project, doc_type, doc_date) "
                "VALUES (1, ?, 'p', 'memo', 20260913)",
                (blob,),
            )
            conn.commit()

        # `old_doc` is gone from disk this run — only `new_doc` is indexed.
        stats = ingest_all_sidecars(conn, vault, indexed_paths={new_doc})
        conn.commit()
        assert stats["adopted"] == 1
        assert stats["inserted"] == 0
        assert stats["skipped_foreign"] == 0

        row = conn.execute(
            "SELECT id, doc_path FROM observations WHERE content_hash = ?", (_hash(content),)
        ).fetchone()
        assert row == (1, new_doc), "adoption must keep the id, only doc_path changes"
        if vec_available:
            assert conn.execute(
                "SELECT COUNT(*) FROM vec_observations WHERE rowid = 1"
            ).fetchone()[0] == 1, "adoption must not disturb the existing vector row"
        topics = {
            r[0] for r in conn.execute(
                "SELECT topic_slug FROM observation_topics WHERE observation_id = 1"
            )
        }
        assert topics == {"kept-topic"}
    finally:
        conn.close()


def test_ingest_foreign_conflict_with_live_doc_does_not_adopt(tmp_path: Path) -> None:
    """When the foreign doc_path IS still on disk this run (`indexed_paths`
    includes it), the hash collision is a genuine duplicate, not a rename —
    must skip, never adopt."""
    vault = _make_vault(tmp_path)
    doc_a = "projects/p/memos/a.md"
    doc_b = "projects/p/memos/b.md"
    content = "shared text"
    path_b = sidecar_path(vault, doc_b)
    _write_jsonl(path_b, [
        {"content": content, "content_hash": _hash(content), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc_a, content, content_hash=_hash(content))
        conn.commit()

        result = ingest_sidecar(conn, vault, doc_b, path_b, indexed_paths={doc_a, doc_b})
        conn.commit()
        assert result["adopted"] == 0
        assert result["skipped_foreign"] == 1
        assert result["foreign_conflicts"] == [{"doc_path": doc_b, "foreign_doc_path": doc_a}]
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path = ?", (doc_b,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_ingest_all_foreign_conflict_resurfaces_every_run(tmp_path: Path) -> None:
    """A genuine foreign-hash conflict must never have its file hash
    recorded — so the same conflict is reported on every subsequent run
    instead of silently going stale after the first."""
    vault = _make_vault(tmp_path)
    doc_a = "projects/p/memos/a.md"
    doc_b = "projects/p/memos/b.md"
    content = "shared text"
    path_b = sidecar_path(vault, doc_b)
    _write_jsonl(path_b, [
        {"content": content, "content_hash": _hash(content), "obs_type": "explicit",
         "confidence": "high", "topics": [], "source_obs": []},
    ])
    conn = _conn()
    try:
        _insert_obs(conn, 1, doc_a, content, content_hash=_hash(content))
        conn.commit()

        for _ in range(2):
            stats = ingest_all_sidecars(conn, vault, indexed_paths={doc_a, doc_b})
            conn.commit()
            assert stats["skipped_foreign"] == 1
            assert stats["foreign_conflicts"] == [
                {"doc_path": doc_b, "foreign_doc_path": doc_a}
            ]
            row = conn.execute(
                "SELECT * FROM obs_sidecars WHERE doc_path = ?", (doc_b,)
            ).fetchone()
            assert row is None, "a foreign-conflict file's hash must never be recorded"
    finally:
        conn.close()


def test_pending_source_resolved_on_later_run_with_no_file_changes(tmp_path: Path) -> None:
    """A deduction ingested before its source's sidecar synced in must have
    its provenance resolved retroactively — even on a run where the
    deduction's OWN sidecar file is unchanged (and so is never re-parsed)."""
    vault = _make_vault(tmp_path)
    doc_project = "projects/p/_project.md"
    doc_a = "projects/p/memos/a.md"
    src_content = "late-arriving source"
    ded_content = "deduction made before source synced"

    path_project = sidecar_path(vault, doc_project)
    _write_jsonl(path_project, [
        {"content": ded_content, "content_hash": _hash(ded_content),
         "obs_type": "deductive", "confidence": "medium",
         "topics": [], "source_obs": [_hash(src_content)]},
    ])

    conn = _conn()
    try:
        # Run 1: only the deduction's sidecar exists. Its source hash cannot
        # resolve yet — persisted to obs_pending_sources for later retry.
        stats1 = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats1["pending_sources_unresolved"] == 1
        assert conn.execute("SELECT COUNT(*) FROM obs_pending_sources").fetchone()[0] == 1

        ded_id = conn.execute(
            "SELECT id FROM observations WHERE content = ?", (ded_content,)
        ).fetchone()[0]

        # Run 2: the source's sidecar arrives. The deduction's OWN sidecar
        # file never changed (same bytes, same recorded hash) — it must NOT
        # be re-parsed for this to work.
        path_a = sidecar_path(vault, doc_a)
        _write_jsonl(path_a, [
            {"content": src_content, "content_hash": _hash(src_content),
             "obs_type": "explicit", "confidence": "high",
             "topics": [], "source_obs": []},
        ])
        stats2 = ingest_all_sidecars(conn, vault, indexed_paths=None)
        conn.commit()
        assert stats2["pending_sources_resolved"] == 1
        assert stats2["pending_sources_unresolved"] == 0
        assert stats2["pending_sources"] == 0
        assert conn.execute("SELECT COUNT(*) FROM obs_pending_sources").fetchone()[0] == 0

        from memex.observations import decode_source_ids

        src_id = conn.execute(
            "SELECT id FROM observations WHERE content = ?", (src_content,)
        ).fetchone()[0]
        ded_row = conn.execute(
            "SELECT source_obs_ids FROM observations WHERE id = ?", (ded_id,)
        ).fetchone()
        assert decode_source_ids(ded_row[0]) == [src_id]
    finally:
        conn.close()


def test_write_sidecar_scrubs_secret_inside_content_and_keeps_hash_consistent(tmp_path):
    """A secret inside an observation's own text must be redacted in the DB row
    (content + hash + FTS) before render, so the sidecar line's content_hash
    matches its content and read_sidecar accepts the file."""
    vault = _make_vault(tmp_path)
    conn = _conn()
    doc = "projects/p/memos/m.md"
    # Built at runtime so the source bytes never contain a provider-shaped key.
    secret = "sk-ant" + "-api03-" + "A" * 56
    content = f"Decision: the key {secret} was rotated on 2026-09-13"
    _insert_obs(conn, 1, doc, content)

    path = write_sidecar(conn, vault, doc)
    assert path is not None

    records = read_sidecar(path)  # would raise SidecarError on a hash mismatch
    assert len(records) == 1
    assert secret not in records[0].content
    row = conn.execute(
        "SELECT content, content_hash FROM observations WHERE id = 1"
    ).fetchone()
    assert row[0] == records[0].content
    assert row[1] == records[0].content_hash == _hash(records[0].content)
    fts = conn.execute("SELECT content FROM fts_observations WHERE rowid = 1").fetchone()
    assert secret not in fts[0]


def test_ingest_all_non_utf8_sidecar_is_a_per_file_error_not_a_crash(tmp_path):
    """A truncated multi-byte character (mid-iCloud-transfer) must count as
    an error for that file — hash not recorded — and not abort the run."""
    vault = _make_vault(tmp_path)
    conn = _conn()
    bad = vault / "projects" / "p" / "memos" / "bad.obs.jsonl"
    bad.write_bytes(b'{"content": "caf\xc3')  # cut inside a 2-byte UTF-8 sequence
    good_doc = "projects/p/memos/good.md"
    good = vault / "projects" / "p" / "memos" / "good.obs.jsonl"
    _write_jsonl(good, [{"content": "ok", "content_hash": _hash("ok")}])

    stats = ingest_all_sidecars(conn, vault, indexed_paths=None)

    assert stats["errors"] == 1
    assert stats["ingested"] == 1
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE doc_path = ?", (good_doc,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM obs_sidecars WHERE doc_path = ?", ("projects/p/memos/bad.md",)
    ).fetchone()[0] == 0
    health = sidecar_health(conn, vault)
    assert health["unreadable"] == ["projects/p/memos/bad.md"]


def test_render_sidecar_keeps_pending_source_hashes(tmp_path):
    """A deduction whose source hasn't synced in yet must keep its
    `source_obs` reference across a local write-through (A3)."""
    vault = _make_vault(tmp_path)
    conn = _conn()
    doc = "projects/p/memos/m.md"
    _insert_obs(conn, 1, doc, "deduction")
    conn.execute(
        "INSERT INTO obs_pending_sources (observation_id, source_hash) VALUES (1, ?)",
        (_hash("not here yet"),),
    )
    path = write_sidecar(conn, vault, doc)
    assert read_sidecar(path)[0].source_obs == [_hash("not here yet")]


def test_export_sidecars_current_branch_records_hash(tmp_path):
    vault = _make_vault(tmp_path)
    conn = _conn()
    doc = "projects/p/memos/m.md"
    _insert_obs(conn, 1, doc, "claim")
    from memex.sidecars import export_sidecars, render_sidecar

    path = vault / "projects" / "p" / "memos" / "m.obs.jsonl"
    path.write_text(render_sidecar(conn, doc))  # already current, never recorded
    stats = export_sidecars(conn, vault, apply=True, force=False)
    assert stats["current"] == 1
    row = conn.execute("SELECT content_hash FROM obs_sidecars WHERE doc_path = ?", (doc,)).fetchone()
    assert row is not None and row[0] == _hash_bytes(path.read_bytes())


def _hash_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
