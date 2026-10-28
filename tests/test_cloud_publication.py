"""v0.22.0 F1: `memex cloud forget` deletes a doc from the cloud at once, and
the opt-in `cloud.publish = "committed"` makes a push publish only what HEAD
holds (the default stays the working tree). Against real throwaway git vaults
and a fake Worker that keeps state; no network, no live config."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import unicodedata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memex import cloud
from memex.cli import app
from memex.config import reset_settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".memex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MEMEX_STATE_DIR", str(home / ".memex"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("MEMEX_CLOUD_URL", "MEMEX_CLOUD_TOKEN", "MEMEX_MEMEX_PATH", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield home
    reset_settings()


class FakeCloud:
    """The Worker's sync API, keeping what it was sent so pushes build on each other."""

    def __init__(self, docs: dict | None = None, observations: dict | None = None, meta: dict | None = None):
        self.docs: dict[str, dict] = docs or {}
        self.obs: dict[str, str] = observations or {}
        self.meta: dict = meta or {}
        self.inbox: list[dict] = []  # pending (not yet pulled) inbox memos
        self.posts: list[tuple[str, dict]] = []
        self.base = "https://fake.invalid"
        self.fail_on: str | None = None

    def get(self, route):
        if route == "/api/sync/inbox":
            return {"memos": self.inbox}
        assert route == "/api/sync/manifest", route
        return {"vector_dimensions": 768, "docs": {p: dict(v) for p, v in self.docs.items()},
                "observations": dict(self.obs), "meta": dict(self.meta)}

    def post(self, route, payload):
        self.posts.append((route, payload))
        if route == self.fail_on:
            raise RuntimeError("HTTP 500")
        if route == "/api/sync/docs":
            for d in payload["docs"]:
                self.docs[d["path"]] = {"hash": d["content_hash"], "source": "vault", "content": d["content"]}
        elif route == "/api/sync/delete":
            for p in payload["paths"]:
                self.docs.pop(p, None)
                self.obs.pop(p, None)
        elif route == "/api/sync/observations":
            for s in payload["sets"]:
                self.obs[s["doc_path"]] = s["set_hash"]
        return {"ok": True}

    def sent(self, route):
        return [p for r, p in self.posts if r == route]


def git(vault: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def commit(vault: Path, *rels: str) -> None:
    git(vault, "add", "--", *rels)
    git(vault, "commit", "-q", "-m", "c")


def fake_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sidecar_line(content: str, topics=()) -> str:
    digest = hashlib.sha256(content.encode()).hexdigest()
    return json.dumps({"content": content, "content_hash": digest, "obs_type": "explicit", "confidence": "high",
                       "topics": sorted(topics), "source_obs": [], "created_at": "2026-10-28 10:00:00"},
                      sort_keys=True) + "\n"


SCHEMA = """
CREATE VIRTUAL TABLE fts_content USING fts5(path, title, content, type, project, date);
CREATE TABLE chunks (id INTEGER PRIMARY KEY, doc_path TEXT, chunk_index INTEGER, content TEXT);
CREATE TABLE observations (id INTEGER PRIMARY KEY, doc_path TEXT, content TEXT, content_hash TEXT,
                           obs_type TEXT, confidence TEXT, created_at TEXT);
CREATE TABLE observation_topics (observation_id INTEGER, topic_slug TEXT);
CREATE TABLE obs_sidecars (doc_path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, ingested_at TEXT);
"""


def index_vault(vault: Path, db: str | Path = ":memory:") -> sqlite3.Connection:
    """What `memex index rebuild` leaves for cloud.py to read: file text as
    read_text() returns it, one chunk per doc, observations from sidecars and
    the sidecar's file hash."""
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    for path in sorted([*vault.glob("projects/**/*.md"), *vault.glob("topics/*.md")]):
        rel = path.relative_to(vault).as_posix()
        content = path.read_text()
        kind = "transcript" if "/transcripts/" in rel else "memo"
        conn.execute("INSERT INTO fts_content VALUES (?, ?, ?, ?, ?, ?)",
                     (rel, path.stem, content, kind, rel.split("/")[1], "2026-10-28"))
        conn.execute("INSERT INTO chunks (doc_path, chunk_index, content) VALUES (?, 0, ?)", (rel, content))
    for side in sorted(vault.glob("projects/**/*.obs.jsonl")):
        doc = side.relative_to(vault).as_posix()[: -len(".obs.jsonl")] + ".md"
        for line in side.read_text().splitlines():
            rec = json.loads(line)
            cur = conn.execute(
                "INSERT INTO observations (doc_path, content, content_hash, obs_type, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc, rec["content"], rec["content_hash"], rec["obs_type"], rec["confidence"], rec["created_at"]),
            )
            for slug in rec["topics"]:
                conn.execute("INSERT INTO observation_topics VALUES (?, ?)", (cur.lastrowid, slug))
        conn.execute("INSERT INTO obs_sidecars VALUES (?, ?, '')", (doc, hashlib.sha256(side.read_bytes()).hexdigest()))
    conn.commit()
    return conn


def push(vault: Path, cloud_: FakeCloud, *, dry_run: bool = False, publish: str | None = "committed") -> dict:
    """A push in `publish` mode (None: whatever the configuration says)."""
    conn = index_vault(vault)
    try:
        return cloud.run_push(conn, cloud_, include_transcripts=False, vectors=False, full=False, dry_run=dry_run,
                              echo=lambda *_: None, publish=cloud.resolve_publish_mode(vault, publish))
    finally:
        conn.close()


@pytest.fixture
def vault(tmp_path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    git(path, "init", "-q")
    return path


A = "projects/p/memos/2026-10-01-a.md"
B = "projects/p/memos/2026-10-02-b.md"
DRAFT = "projects/p/memos/2026-10-28-draft.md"


# ── committed content only ──────────────────────────────────────────


def test_push_publishes_head_and_never_an_uncommitted_draft(vault):
    commit(vault, write(vault, A, "# A\nreviewed\n"), write(vault, B, "# B\nreviewed\n"))
    write(vault, A, "# A\nreviewed, then an unreviewed edit\n")
    write(vault, DRAFT, "# Draft\nunreviewed agent draft\n")
    fake = FakeCloud()

    summary = push(vault, fake, dry_run=True)
    assert summary["publish"] == "committed" and summary["held_back"] == 2
    assert summary["plan"]["upsert"] == [B]
    assert summary["plan"]["held_back"] == {A: "modified", DRAFT: "uncommitted"}
    assert fake.posts == []

    push(vault, fake)
    assert set(fake.docs) == {B} and fake.docs[B]["content"] == "# B\nreviewed\n"

    commit(vault, DRAFT)
    summary = push(vault, fake)
    assert summary["docs_upsert"] == 1 and summary["held_back_reasons"] == {"modified": 1}
    assert set(fake.docs) == {B, DRAFT}
    assert all("unreviewed edit" not in d["content"] for d in fake.docs.values())


def test_a_doc_changed_since_head_keeps_its_cloud_copy_until_committed(vault):
    commit(vault, write(vault, A, "# A\nv1\n"), write(vault, B, "# B\nv1\n"))
    fake = FakeCloud()
    push(vault, fake)
    published = {p: d["hash"] for p, d in fake.docs.items()}

    write(vault, A, "# A\nv2, not reviewed\n")
    (vault / B).unlink()  # deleted in the working tree only
    summary = push(vault, fake)
    assert summary["docs_upsert"] == 0 and summary["docs_delete"] == 0
    assert {p: d["hash"] for p, d in fake.docs.items()} == published
    assert fake.sent("/api/sync/delete") == []

    git(vault, "rm", "-q", "--cached", B)
    git(vault, "commit", "-q", "-m", "drop b")
    commit(vault, A)
    summary = push(vault, fake)
    assert summary["docs_delete"] == 1 and summary["docs_upsert"] == 1
    assert set(fake.docs) == {A} and "v2" in fake.docs[A]["content"]


def test_staged_changes_count_as_uncommitted(vault):
    commit(vault, write(vault, A, "# A\nv1\n"))
    write(vault, A, "# A\nv2 staged\n")
    write(vault, DRAFT, "# Draft\nstaged only\n")
    git(vault, "add", "--", A, DRAFT)
    fake = FakeCloud(docs={A: {"hash": "v1", "source": "vault"}, DRAFT: {"hash": "d", "source": "vault"}})
    plan = push(vault, fake, dry_run=True)["plan"]
    assert plan["upsert"] == [] and plan["held_back"] == {A: "modified", DRAFT: "uncommitted"}
    assert plan["delete"] == [DRAFT]  # HEAD has no version of it to keep


def test_uncommitted_docs_already_in_the_cloud_are_removed_but_inbox_memos_stay(vault):
    inbox = "projects/p/memos/2026-10-27-from-claude-ai.md"
    commit(vault, write(vault, A, "# A\n"))
    write(vault, DRAFT, "# Draft\npublished by a 0.21 working-tree push\n")
    write(vault, inbox, "# Pulled from the inbox, not yet committed\n")
    fake = FakeCloud(docs={DRAFT: {"hash": "old", "source": "vault"}, inbox: {"hash": "x", "source": "inbox"}})
    summary = push(vault, fake)
    assert fake.sent("/api/sync/delete") == [{"paths": [DRAFT]}]
    assert set(fake.docs) == {A, inbox} and summary["held_back_reasons"] == {"uncommitted": 2}


def test_index_out_of_step_with_head_is_held_and_newline_only_differences_are_not(vault):
    commit(vault, write(vault, A, "# A\ncommitted\n"))
    crlf = "projects/p/memos/2026-10-03-crlf.md"
    (vault / crlf).write_bytes(b"---\r\ntitle: c\r\n---\r\nbody\r\n")
    commit(vault, crlf)
    conn = index_vault(vault)
    conn.execute("UPDATE fts_content SET content = '# A\nan older draft the index still holds\n' WHERE path = ?", (A,))
    fake = FakeCloud(docs={A: {"hash": "published-earlier", "source": "vault"}})
    summary = cloud.run_push(conn, fake, include_transcripts=False, vectors=False, full=False, dry_run=True,
                             echo=lambda *_: None, publish=cloud.resolve_publish_mode(vault, "committed"))
    assert summary["plan"]["held_back"] == {A: "stale"}
    assert summary["plan"]["upsert"] == [crlf]  # read_text() turned CRLF into LF; HEAD's blob says the same
    assert summary["plan"]["delete"] == []  # held, so the cloud keeps what it has


def test_observation_sets_follow_their_committed_sidecar(vault):
    side = {m: m.replace(".md", ".obs.jsonl") for m in ("projects/p/memos/m1.md", "projects/p/memos/m2.md",
                                                       "projects/p/memos/m3.md", "projects/p/memos/m4.md")}
    m1, m2, m3, m4 = side
    for memo, sidecar in side.items():
        write(vault, memo, f"# {memo}\n")
        write(vault, sidecar, sidecar_line(f"Fact: from {memo}", ["memex"]))
    commit(vault, m1, side[m1], m2, m3, side[m3], m4, side[m4])  # m2's sidecar stays untracked
    write(vault, side[m3], sidecar_line("Fact: m3, re-extracted, not reviewed"))
    conn = index_vault(vault)
    conn.execute("UPDATE obs_sidecars SET content_hash = 'ingested-before-the-commit' WHERE doc_path = ?", (m4,))
    remote_obs = {m2: "old", m3: "old", m4: "old"}
    fake = FakeCloud(docs={m: {"hash": "old", "source": "vault"} for m in side}, observations=dict(remote_obs))
    summary = cloud.run_push(conn, fake, include_transcripts=False, vectors=False, full=False, dry_run=False,
                             echo=lambda *_: None, publish=cloud.resolve_publish_mode(vault, "committed"))
    sets = {s["doc_path"]: s for p in fake.sent("/api/sync/observations") for s in p["sets"]}
    assert set(sets) == {m1, m2}
    assert [o["content"] for o in sets[m1]["observations"]] == [f"Fact: from {m1}"]
    assert sets[m2]["observations"] == []  # HEAD holds no observations for m2
    assert fake.obs[m3] == "old" and fake.obs[m4] == "old"  # neither replaced nor emptied
    assert summary["observation_sets_held"] == 3
    assert {m: r for m, r in cloud.filter_publication(
        conn, cloud.local_docs(conn, False), cloud.observation_sets(conn, set(side)),
        cloud.resolve_publish_mode(vault, "committed"), {}).obs_held.items()} == {m2: "uncommitted", m3: "modified", m4: "stale"}


def test_vault_below_the_repository_root(tmp_path):
    repo = tmp_path / "repo"
    vault = repo / "notes" / "vault"
    vault.mkdir(parents=True)
    git(repo, "init", "-q")
    commit(vault, write(vault, A, "# A\n"), write(vault, B, "# B\n"))
    write(vault, B, "# B\nedited\n")
    write(repo, "elsewhere/x.md", "outside the vault\n")
    plan = push(vault, FakeCloud(), dry_run=True)["plan"]
    assert plan["upsert"] == [A] and plan["held_back"] == {B: "modified"}


def test_decomposed_unicode_paths_match_gits_precomposed_ones(vault):
    nfc = unicodedata.normalize("NFC", "projects/p/memos/2026-10-04-café.md")
    nfd = unicodedata.normalize("NFD", nfc)
    content = "# café\n"
    blob = cloud._blob_id(content.encode(), "0" * 40)
    view = cloud.GitView(vault=vault, head="abc", blobs={nfc: blob}, dirty=set())
    publish = cloud.PublishMode("committed", "test", view)
    doc = cloud.LocalDoc(nfd, "café", "memo", "p", "", content, hashlib.sha256(content.encode()).hexdigest())
    pub = cloud.filter_publication(sqlite3.connect(":memory:"), {nfd: doc}, {}, publish, {})
    assert list(pub.docs) == [nfd]
    view.dirty.add(nfc)
    pub = cloud.filter_publication(sqlite3.connect(":memory:"), {nfd: doc}, {}, publish, {})
    assert pub.held == {nfd: "modified"}
    assert cloud.plan_push({}, {}, {"docs": {nfd: {"hash": "h"}}}, hold=pub.hold).delete == []


def test_gitignored_docs_are_named_as_such(vault):
    write(vault, ".gitignore", "projects/*/auto-memory/\n")
    memory = write(vault, "projects/p/auto-memory/MEMORY.md", "# synced agent memory\n")
    commit(vault, ".gitignore", write(vault, A, "# A\n"))
    write(vault, DRAFT, "# Draft\n")
    fake = FakeCloud(docs={memory: {"hash": "pushed-by-0.21", "source": "vault"}})
    lines: list[str] = []
    conn = index_vault(vault)
    summary = cloud.run_push(conn, fake, include_transcripts=False, vectors=False, full=False, dry_run=True,
                             echo=lines.append, publish=cloud.resolve_publish_mode(vault, "committed"))
    assert summary["plan"]["held_back"] == {memory: "ignored", DRAFT: "uncommitted"}
    assert summary["plan"]["delete"] == [memory]
    assert "held back 2: 1 not committed, 1 gitignored" in lines[0]
    assert any("Gitignored docs" in line for line in lines[1:])


def test_a_model_switch_takes_held_copies_out_instead_of_leaving_old_vectors(vault, monkeypatch):
    monkeypatch.setattr(cloud, "stored_vector_dim", lambda *_: 768)
    monkeypatch.setattr("memex.scripts.embeddings.get_embedding_config",
                        lambda: {"provider": "google", "model": "gemini-embedding-2"})
    memo = "projects/p/memos/m.md"
    commit(vault, write(vault, A, "# A\n"), write(vault, B, "# B\n"), write(vault, memo, "# M\n"),
           write(vault, memo.replace(".md", ".obs.jsonl"), sidecar_line("Fact: one")))
    write(vault, A, "# A\nediting\n")
    write(vault, memo.replace(".md", ".obs.jsonl"), sidecar_line("Fact: one") + sidecar_line("Fact: two, unreviewed"))
    published = {p: {"hash": "x", "source": "vault"} for p in (A, B, memo)}
    old_model = {"embedding_model": "gemini-embedding-001", "embedding_dimensions": "768"}
    same_model = {"embedding_model": "gemini-embedding-2", "embedding_dimensions": "768"}

    def run(meta):
        conn = index_vault(vault)
        monkeypatch.setattr(cloud, "with_observation_vectors", lambda _c, entry: entry["observations"])
        return cloud.run_push(conn, FakeCloud(docs=dict(published), observations={memo: "old"}, meta=meta),
                              include_transcripts=False, vectors=True, full=False, dry_run=True,
                              echo=lambda *_: None, publish=cloud.resolve_publish_mode(vault, "committed"))["plan"]

    plan = run(same_model)
    assert plan["delete"] == [] and plan["observation_sets"] == []  # held as usual
    plan = run(old_model)
    assert plan["delete"] == [A]  # its cloud copy carries old-model vectors
    assert plan["observation_sets"] == [memo]  # emptied for the same reason
    assert sorted(plan["upsert"]) == [B, memo]


def test_an_inbox_memo_pulled_and_then_removed_leaves_the_cloud(vault):
    commit(vault, write(vault, A, "# A\n"))
    rejected = "projects/p/memos/2026-10-27-rejected.md"  # pulled, then deleted by the owner
    pending = "projects/p/memos/2026-10-28-pending.md"  # saved remotely, not pulled yet
    pulled = write(vault, "projects/p/memos/2026-10-28-pulled.md", "# pulled, awaiting review\n")
    fake = FakeCloud(docs={p: {"hash": "h", "source": "inbox"} for p in (rejected, pending, pulled)})
    fake.inbox = [{"path": pending, "content": "# pending\n"}]
    push(vault, fake)
    assert fake.sent("/api/sync/delete") == [{"paths": [rejected]}]
    assert set(fake.docs) == {A, pending, pulled}


# ── choosing the mode ───────────────────────────────────────────────


def test_a_git_vault_without_a_setting_publishes_the_working_tree(vault):
    # Owner decision 2026-10-28: claude.ai sessions see what local agents see.
    commit(vault, write(vault, A, "# A\nv1\n"))
    write(vault, A, "# A\nv2, not committed\n")
    write(vault, DRAFT, "# Draft\nnot committed\n")
    mode = cloud.resolve_publish_mode(vault)
    assert (mode.mode, mode.reason, mode.git) == ("working-tree", "default", None)
    fake = FakeCloud()
    summary = push(vault, fake, publish=None)
    assert summary["publish"] == "working-tree" and summary["held_back"] == 0
    assert set(fake.docs) == {A, DRAFT} and "v2" in fake.docs[A]["content"]


def test_publish_mode_settings(tmp_path, isolated):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cloud.resolve_publish_mode(plain).mode == "working-tree"
    with pytest.raises(RuntimeError, match="not in a git work tree"):
        cloud.resolve_publish_mode(plain, "committed")

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q")
    assert cloud.resolve_publish_mode(fresh).mode == "working-tree"
    with pytest.raises(RuntimeError, match="no commits"):
        cloud.resolve_publish_mode(fresh, "committed")

    untracked = fresh / "vault"  # e.g. a vault inside a dotfiles repo that does not track it
    write(fresh, "dotfile.md", "x\n")
    commit(fresh, "dotfile.md")
    write(untracked, A, "# A\n")
    with pytest.raises(RuntimeError, match="nothing under .* is committed"):
        cloud.resolve_publish_mode(untracked, "committed")  # would read every doc as uncommitted

    commit(fresh, "vault")
    config = isolated / ".memex" / "config.json"
    assert cloud.resolve_publish_mode(untracked).mode == "working-tree"
    config.write_text(json.dumps({"cloud": {"publish": "committed"}}))
    mode = cloud.resolve_publish_mode(untracked)
    assert mode.mode == "committed" and mode.reason.startswith("cloud.publish = committed, HEAD ")
    assert mode.git is not None and A in mode.git.blobs
    assert cloud.resolve_publish_mode(untracked, "working-tree").mode == "working-tree"  # the flag wins
    config.write_text(json.dumps({"cloud": {"publish": "everything"}}))
    with pytest.raises(RuntimeError, match="cloud.publish must be one of"):
        cloud.resolve_publish_mode(untracked)


def test_an_unreadable_repository_stops_the_push_instead_of_publishing_the_working_tree(vault, monkeypatch):
    commit(vault, write(vault, A, "# A\n"))
    real = cloud._git

    def broken(path, *args, **kwargs):
        if args[:1] == ("rev-parse",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: detected dubious ownership\n")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(cloud, "_git", broken)
    with pytest.raises(RuntimeError, match="dubious ownership"):
        cloud.resolve_publish_mode(vault, "committed")


def test_a_head_that_names_no_readable_commit_is_not_taken_for_an_empty_repository(vault):
    commit(vault, write(vault, A, "# A\n"))
    ref = git(vault, "symbolic-ref", "-q", "HEAD").strip()
    (vault / ".git" / ref).write_text("1" * 40 + "\n")  # e.g. a half-synced .git
    with pytest.raises(RuntimeError, match="does not point at a readable commit"):
        cloud.resolve_publish_mode(vault, "committed")


def test_a_git_that_hangs_becomes_a_clean_error(vault, monkeypatch):
    commit(vault, write(vault, A, "# A\n"))

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], cloud.GIT_TIMEOUT)

    monkeypatch.setattr(cloud.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="did not finish"):
        cloud.resolve_publish_mode(vault, "committed")
    fake = FakeCloud(docs={A: {"hash": "h", "source": "vault"}})
    cloud.run_forget(vault, fake, [A], echo=lambda *_: None)  # an urgent forget still goes through
    assert fake.sent("/api/sync/delete") == [{"paths": [A]}] and "head" not in cloud.load_forgotten()[A]


# ── forget ──────────────────────────────────────────────────────────


def test_forget_deletes_now_and_the_default_push_does_not_resurrect_it(vault):
    # The owner's setup: a git vault on the default publish=working-tree, where
    # forget is the privacy tool for a draft that went out with the hourly push.
    commit(vault, write(vault, A, "# A\n"))
    write(vault, DRAFT, "# Draft\nsomething personal\n")
    fake = FakeCloud()
    push(vault, fake, publish=None)
    assert set(fake.docs) == {A, DRAFT}

    conn = index_vault(vault)
    result = cloud.run_forget(vault, fake, [DRAFT], conn=conn, echo=lambda *_: None)
    assert result == {"forgotten": [DRAFT], "were_in_cloud": [DRAFT], "aborted": False}
    assert fake.sent("/api/sync/delete") == [{"paths": [DRAFT]}]
    record = cloud.load_forgotten()[DRAFT]
    assert record["head"] is None and record["hashes"] == [fake_hash("# Draft\nsomething personal\n")]

    summary = push(vault, fake, publish=None)
    assert DRAFT not in fake.docs and summary["held_back_reasons"] == {"forgotten": 1}
    commit(vault, DRAFT)  # committing it unchanged does not release it either
    push(vault, fake, publish=None)
    assert DRAFT not in fake.docs

    write(vault, DRAFT, "# Draft\n[redacted]\n")
    push(vault, fake, publish=None)
    assert fake.docs[DRAFT]["content"] == "# Draft\n[redacted]\n"
    assert cloud.load_forgotten() == {}  # released once the new version went out


def test_forget_without_git(tmp_path):
    vault = tmp_path / "plain-vault"
    write(vault, DRAFT, "# Draft\n")
    fake = FakeCloud()
    push(vault, fake, publish=None)
    cloud.run_forget(vault, fake, [DRAFT], conn=index_vault(vault), echo=lambda *_: None)
    assert "head" not in cloud.load_forgotten()[DRAFT] and DRAFT not in fake.docs
    push(vault, fake, publish=None)
    assert DRAFT not in fake.docs


def test_forget_in_a_git_vault_holds_until_a_new_version_is_committed(vault):
    commit(vault, write(vault, A, "# A\nan accidental detail\n"))
    write(vault, DRAFT, "# Draft\n")
    fake = FakeCloud()
    push(vault, fake)
    assert set(fake.docs) == {A}

    cloud.run_forget(vault, fake, [A, DRAFT], conn=index_vault(vault), echo=lambda *_: None)
    records = cloud.load_forgotten()
    assert records[A]["head"] == git(vault, "rev-parse", f"HEAD:{A}").strip() and records[DRAFT]["head"] is None
    assert A not in fake.docs

    push(vault, fake)
    assert A not in fake.docs  # committed and unchanged: still forgotten
    write(vault, A, "# A\n[redacted]\n")
    push(vault, fake)
    assert A not in fake.docs  # redacted but not committed yet
    commit(vault, A, DRAFT)  # the draft is committed exactly as it was forgotten
    summary = push(vault, fake)
    assert fake.docs[A]["content"] == "# A\n[redacted]\n" and DRAFT not in fake.docs
    assert summary["held_back_reasons"] == {"forgotten": 1} and list(cloud.load_forgotten()) == [DRAFT]
    commit(vault, write(vault, DRAFT, "# Draft\nreviewed\n"))
    push(vault, fake)
    assert DRAFT in fake.docs and cloud.load_forgotten() == {}


def test_forget_wins_over_a_hold(vault):
    commit(vault, write(vault, A, "# A\nleaked\n"))
    fake = FakeCloud()
    push(vault, fake)
    write(vault, A, "# A\nbeing redacted\n")  # modified since HEAD: held, so a push would keep the cloud copy
    fake.fail_on = "/api/sync/delete"
    with pytest.raises(RuntimeError):
        cloud.run_forget(vault, fake, [A], conn=index_vault(vault), echo=lambda *_: None)
    assert A in fake.docs and A in cloud.load_forgotten()  # recorded before the delete was tried

    fake.fail_on = None
    summary = push(vault, fake)
    assert summary["docs_delete"] == 1 and A not in fake.docs  # the push finished the forget


def test_forget_record_is_dropped_only_after_the_whole_push_succeeds(tmp_path):
    vault = tmp_path / "plain-vault"
    write(vault, A, "# A\n")
    fake = FakeCloud()
    push(vault, fake, publish=None)
    cloud.run_forget(vault, fake, [A], conn=index_vault(vault), echo=lambda *_: None)
    write(vault, A, "# A\nchanged\n")
    write(vault, A.replace(".md", ".obs.jsonl"), sidecar_line("Fact: changed"))
    fake.fail_on = "/api/sync/observations"
    with pytest.raises(RuntimeError):
        push(vault, fake, publish=None)
    assert A in cloud.load_forgotten()
    fake.fail_on = None
    push(vault, fake, publish=None)
    assert cloud.load_forgotten() == {}


def test_forget_sends_at_most_100_paths_per_delete(tmp_path):
    vault = tmp_path / "plain-vault"
    vault.mkdir()
    paths = [f"projects/p/memos/2026-10-28-m{i:03}.md" for i in range(150)]
    fake = FakeCloud(docs={p: {"hash": "h", "source": "vault"} for p in paths})
    cloud.run_forget(vault, fake, paths, echo=lambda *_: None)
    assert [len(p["paths"]) for p in fake.sent("/api/sync/delete")] == [100, 50]
    assert fake.docs == {} and len(cloud.load_forgotten()) == 150


def test_forget_uses_the_key_the_cloud_and_index_use(tmp_path):
    vault = tmp_path / "plain-vault"
    stored = write(vault, "projects/p/memos/2026-10-05-Notes.md", "# notes\n")
    cafe = unicodedata.normalize("NFC", "projects/p/memos/2026-10-06-café.md")
    fake = FakeCloud(docs={cafe: {"hash": "h", "source": "vault"}, stored: {"hash": "h2", "source": "vault"}})
    result = cloud.run_forget(vault, fake, [unicodedata.normalize("NFD", cafe), stored.lower()], echo=lambda *_: None)
    assert result["forgotten"] == [cafe, stored]
    assert fake.docs == {} and sorted(cloud.load_forgotten()) == sorted([cafe, stored])


def test_forget_refuses_unknown_paths(tmp_path):
    vault = tmp_path / "plain-vault"
    write(vault, A, "# A\n")
    fake = FakeCloud()
    with pytest.raises(RuntimeError, match="not in the cloud or the vault"):
        cloud.run_forget(vault, fake, ["projects/p/memos/typo.md"], echo=lambda *_: None)
    assert fake.posts == [] and cloud.load_forgotten() == {}


def test_vault_rel_path(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    write(vault, A, "# A\n")
    assert cloud.vault_rel_path(vault, A) == A
    assert cloud.vault_rel_path(vault, str(vault / A)) == A
    assert cloud.vault_rel_path(vault, "./" + A.replace(".md", ".obs.jsonl")) == A
    monkeypatch.chdir(vault / "projects/p")
    assert cloud.vault_rel_path(vault, "memos/2026-10-01-a.md") == A
    for bad in ("../outside.md", str(tmp_path / "outside.md"), "projects/../../x.md"):
        with pytest.raises(ValueError):
            cloud.vault_rel_path(vault, bad)


def test_unreadable_forget_list_stops_a_push(tmp_path):
    vault = tmp_path / "plain-vault"
    write(vault, A, "# A\n")
    cloud.cloud_forget_path().write_text("{not json")
    fake = FakeCloud()
    with pytest.raises(RuntimeError, match="cloud-forget.json"):
        push(vault, fake, publish=None)
    assert fake.posts == []


# ── the commands ────────────────────────────────────────────────────


@pytest.fixture
def cli_vault(vault, monkeypatch):
    monkeypatch.setenv("MEMEX_MEMEX_PATH", str(vault))
    reset_settings()
    fake = FakeCloud()
    monkeypatch.setattr(cloud, "_client_or_exit", lambda: fake)
    monkeypatch.setattr(cloud, "_check_server_or_exit", lambda client: None)
    commit(vault, write(vault, A, "# A\n"), write(vault, B, "# B\n"))
    write(vault, B, "# B\nedited\n")
    write(vault, DRAFT, "# Draft\n")
    from memex.paths import get_index_path

    index = get_index_path(vault)
    index.parent.mkdir(parents=True, exist_ok=True)
    index_vault(vault, index).close()
    return vault, fake


def test_push_output_shows_the_mode_and_what_is_held_back(cli_vault, isolated):
    _, fake = cli_vault
    result = CliRunner().invoke(app, ["cloud", "push", "--dry-run", "--no-vectors"])
    assert result.exit_code == 0, result.output
    assert "publish=working-tree (default)\n" in result.output  # nothing held back by default
    assert "publish=working-tree held_back=0 observation_sets_held=0" in result.output
    assert all(p in result.output.split("would upsert (3):\n")[1] for p in (A, B, DRAFT))

    (isolated / ".memex" / "config.json").write_text(json.dumps({"cloud": {"publish": "committed"}}))
    result = CliRunner().invoke(app, ["cloud", "push", "--dry-run", "--no-vectors"])
    assert result.exit_code == 0, result.output
    assert "publish=committed (cloud.publish = committed, HEAD " in result.output
    assert "held back 2: 1 not committed, 1 changed since HEAD" in result.output
    assert "publish=committed held_back=2 observation_sets_held=0" in result.output
    upsert = result.output.split("would upsert (1):\n")[1].split("held back (2):")[0]
    assert A in upsert and DRAFT not in upsert
    assert f"{DRAFT}  [not committed]" in result.output and f"{B}  [changed since HEAD]" in result.output
    assert fake.posts == []

    result = CliRunner().invoke(app, ["cloud", "push", "--dry-run", "--json", "--publish", "working-tree"])
    data = json.loads(result.output)
    assert data["publish"] == "working-tree" and sorted(data["plan"]["upsert"]) == sorted([A, B, DRAFT])

    result = CliRunner().invoke(app, ["cloud", "push", "--publish", "everything"])
    assert result.exit_code == 1 and "--publish must be one of" in result.output


def test_forget_command(cli_vault):
    vault, fake = cli_vault
    fake.docs[DRAFT] = {"hash": "h", "source": "vault"}
    result = CliRunner().invoke(app, ["cloud", "forget", DRAFT])
    assert result.exit_code == 1 and "--yes" in result.output and fake.posts == []

    result = CliRunner().invoke(app, ["cloud", "forget", "--yes", str(vault / DRAFT)])
    assert result.exit_code == 0, result.output
    assert fake.sent("/api/sync/delete") == [{"paths": [DRAFT]}]
    assert "Deleted 1 doc(s)" in result.output and DRAFT in cloud.load_forgotten()

    result = CliRunner().invoke(app, ["cloud", "forget", "--yes", "../etc/passwd"])
    assert result.exit_code == 1 and "not a vault path" in result.output


# ── secrets never leave the machine (v0.22.0 wave 2) ─────────────────

def _anthropic_fixture() -> str:
    # built at runtime so secret scanners never see the full shape in source bytes
    return "sk-ant" + "-api03-" + "A" * 56


def test_push_redacts_secrets_in_docs_chunks_and_observations_whoever_wrote_them(vault):
    """A Codex- or Kimi-written memo skips Claude Code's write-time scrub hook;
    the push scrubs what it sends instead. The vault file is left as it is."""
    key = _anthropic_fixture()
    rel = write(vault, A, f"# A\nCodex pasted {key} into its memo\n")
    side = vault / (A[: -len(".md")] + ".obs.jsonl")
    side.write_text(sidecar_line(f"the key was {key}"))
    fake = FakeCloud()
    push(vault, fake, publish="working-tree")
    docs = fake.sent("/api/sync/docs")[0]["docs"]
    obs = fake.sent("/api/sync/observations")[0]["sets"][0]["observations"]
    sent_text = json.dumps([docs, obs])
    assert key not in sent_text
    assert "<REDACTED" in docs[0]["content"] and "<REDACTED" in docs[0]["chunks"][0]["content"]
    assert "<REDACTED" in obs[0]["content"]
    assert key in (vault / rel).read_text(), "the push must not rewrite vault files"


def test_push_and_forget_never_run_at_once(tmp_path, monkeypatch):
    """A forget landing mid-push would be undone by that push (gauntlet round 1)."""
    import memex.paths as paths

    monkeypatch.setattr(paths, "get_lock_dir", lambda: tmp_path)
    with cloud.cloud_lock():
        with pytest.raises(RuntimeError, match="held the lock"):
            with cloud.cloud_lock(wait=0.1):
                pass
    with cloud.cloud_lock(wait=0.1):  # released: free again
        pass


def test_push_reads_one_snapshot_so_a_concurrent_rebuild_cannot_slip_chunks_in(vault, tmp_path, monkeypatch):
    """Committed mode checks a doc against HEAD, then sends its chunks: a rebuild
    committing in between must not change what is sent (gauntlet round 1)."""
    commit(vault, write(vault, A, "# A\nreviewed text\n"))
    db = tmp_path / "idx.sqlite"
    index_vault(vault, db).close()
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    real = cloud.doc_chunks
    fired = []

    def racing_doc_chunks(c, path, with_vectors):
        if not fired:
            other = sqlite3.connect(str(db))
            other.execute("UPDATE chunks SET content = 'PRIVATE uncommitted draft' WHERE doc_path = ?", (path,))
            other.commit()
            other.close()
            fired.append(path)
        return real(c, path, with_vectors)

    monkeypatch.setattr(cloud, "doc_chunks", racing_doc_chunks)
    fake = FakeCloud()
    try:
        cloud.run_push(conn, fake, include_transcripts=False, vectors=False, full=False, dry_run=False,
                       echo=lambda *_: None, publish=cloud.resolve_publish_mode(vault, "committed"))
    finally:
        conn.close()
    assert fired, "the race was staged"
    chunks = fake.sent("/api/sync/docs")[0]["docs"][0]["chunks"]
    assert all("PRIVATE" not in c["content"] for c in chunks), chunks
