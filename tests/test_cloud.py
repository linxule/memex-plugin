"""`memex cloud` push planning, payloads and inbox pull, against a fake Worker."""

from __future__ import annotations

import sqlite3
import stat

import pytest
from typer.testing import CliRunner

from memex import cloud
from memex.cli import app
from memex.config import reset_settings


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEX_STATE_DIR", str(tmp_path / "state"))
    for name in ("MEMEX_CLOUD_URL", "MEMEX_CLOUD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    yield
    reset_settings()


class FakeClient:
    """Records requests; answers like the Worker's sync API."""

    def __init__(self, manifest=None, inbox=None):
        self.manifest = manifest or {"vector_dimensions": 768, "docs": {}, "observations": {}, "meta": {}}
        self.inbox = inbox or []
        self.posts: list[tuple[str, dict]] = []

    def get(self, route):
        if route == "/api/sync/manifest":
            return self.manifest
        if route == "/api/sync/inbox":
            return {"memos": self.inbox}
        raise AssertionError(route)

    def post(self, route, payload):
        self.posts.append((route, payload))
        return {"ok": True}

    def routes(self):
        return [r for r, _ in self.posts]


def make_index() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE fts_content USING fts5(path, title, content, type, project, date);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, doc_path TEXT, chunk_index INTEGER, content TEXT);
        CREATE TABLE observations (id INTEGER PRIMARY KEY, doc_path TEXT, content TEXT, content_hash TEXT,
                                   obs_type TEXT, confidence TEXT, created_at TEXT);
        CREATE TABLE observation_topics (observation_id INTEGER, topic_slug TEXT);
        """
    )
    rows = [
        ("projects/a/memos/2026-10-01-one.md", "One", "---\ntype: memo\n---\nalpha", "memo", "a", "2026-10-01"),
        ("projects/a/_project.md", "A", "overview", "project", "a", ""),
        ("projects/a/transcripts/t.md", "T", "user: hi", "transcript", "a", "2026-10-01"),
    ]
    conn.executemany("INSERT INTO fts_content VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO chunks (doc_path, chunk_index, content) VALUES (?, ?, ?)",
        [(r[0], 0, r[2]) for r in rows],
    )
    conn.execute(
        "INSERT INTO observations VALUES (1, 'projects/a/memos/2026-10-01-one.md', 'fact', 'h1', 'explicit', 'high', '')"
    )
    conn.execute("INSERT INTO observation_topics VALUES (1, 'memex')")
    return conn


def test_push_skips_transcripts_by_default_and_sends_everything_new():
    conn = make_index()
    fake = FakeClient()
    summary = cloud.run_push(conn, fake, include_transcripts=False, vectors=False, full=False, dry_run=False, echo=lambda *_: None)
    assert summary["docs_upsert"] == 2 and summary["observation_sets"] == 1 and summary["vectors"] is False
    sent = [d["path"] for r, p in fake.posts if r == "/api/sync/docs" for d in p["docs"]]
    assert sorted(sent) == ["projects/a/_project.md", "projects/a/memos/2026-10-01-one.md"]
    obs = [s for r, p in fake.posts if r == "/api/sync/observations" for s in p["sets"]]
    assert obs[0]["observations"][0]["topics"] == ["memex"]
    assert "/api/sync/meta" not in fake.routes()  # no vectors, no embedding meta


def test_push_is_incremental_and_never_deletes_inbox_memos():
    conn = make_index()
    local = cloud.local_docs(conn, include_transcripts=False)
    obs = cloud.observation_sets(conn, set(local))
    memo = "projects/a/memos/2026-10-01-one.md"
    manifest = {
        "docs": {
            memo: {"hash": local[memo].content_hash, "source": "vault"},
            "projects/a/memos/gone.md": {"hash": "x", "source": "vault"},
            "projects/a/memos/2026-10-19-remote.md": {"hash": "y", "source": "inbox"},
        },
        "observations": {memo: obs[memo]["set_hash"]},
    }
    plan = cloud.plan_push(local, obs, manifest)
    assert plan.upsert == ["projects/a/_project.md"]
    assert plan.delete == ["projects/a/memos/gone.md"]
    assert plan.obs == []


def test_pulled_inbox_memo_is_resent_as_vault_copy():
    conn = make_index()
    local = cloud.local_docs(conn, include_transcripts=False)
    memo = "projects/a/memos/2026-10-01-one.md"
    manifest = {"docs": {memo: {"hash": local[memo].content_hash, "source": "inbox"}}}
    assert memo in cloud.plan_push(local, {}, manifest).upsert


def test_retagging_an_observation_changes_its_set_hash():
    conn = make_index()
    before = cloud.observation_sets(conn, {"projects/a/memos/2026-10-01-one.md"})
    conn.execute("UPDATE observation_topics SET topic_slug = 'other'")
    after = cloud.observation_sets(conn, {"projects/a/memos/2026-10-01-one.md"})
    key = "projects/a/memos/2026-10-01-one.md"
    assert before[key]["set_hash"] != after[key]["set_hash"]


def test_dry_run_sends_nothing():
    fake = FakeClient()
    summary = cloud.run_push(make_index(), fake, include_transcripts=True, vectors=False, full=False, dry_run=True, echo=lambda *_: None)
    assert summary["docs_upsert"] == 3
    assert fake.posts == []


def test_pull_writes_memos_and_acknowledges(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    existing = vault / "projects/a/memos/2026-10-18-same.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("same\n")
    fake = FakeClient(
        inbox=[
            {"path": "projects/a/memos/2026-10-19-new.md", "content": "---\ntype: memo\n---\n# New\n"},
            {"path": "projects/a/memos/2026-10-18-same.md", "content": "same\n"},
            {"path": "projects/../../etc/x.md", "content": "nope"},
            {"path": "topics/sneaky.md", "content": "nope"},
        ]
    )
    result = cloud.run_pull(vault, fake, dry_run=False, echo=lambda *_: None)
    assert result["pulled"] == ["projects/a/memos/2026-10-19-new.md"]
    assert (vault / "projects/a/memos/2026-10-19-new.md").read_text().startswith("---\ntype: memo")
    assert sorted(result["skipped"]) == ["projects/../../etc/x.md", "topics/sneaky.md"]
    acked = fake.posts[-1]
    assert acked[0] == "/api/sync/inbox/ack"
    assert sorted(acked[1]["paths"]) == ["projects/a/memos/2026-10-18-same.md", "projects/a/memos/2026-10-19-new.md"]


def test_pull_does_not_overwrite_a_different_file(tmp_path):
    target = tmp_path / "projects/a/memos/2026-10-19-x.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n")
    fake = FakeClient(inbox=[{"path": "projects/a/memos/2026-10-19-x.md", "content": "theirs\n"}])
    result = cloud.run_pull(tmp_path, fake, dry_run=False, echo=lambda *_: None)
    assert result["skipped"] == ["projects/a/memos/2026-10-19-x.md"]
    assert target.read_text() == "mine\n"
    assert fake.posts == []


def test_pull_writes_observation_sidecar_and_lists_memos_without(tmp_path):
    from memex.sidecars import read_sidecar

    key = "sk-ant" + "-api03-" + "A" * 56  # assembled so this file holds no literal key
    obs = [
        {"content": "Decision: hourly cloud sync runs on m4max only", "obs_type": "explicit",
         "confidence": "high", "topics": ["memex-project", "cloud"], "created_at": "2026-10-20 16:00:00"},
        {"content": f"Fact: the probe used {key}", "obs_type": "deductive", "confidence": "medium", "topics": []},
        {"content": "Decision: hourly cloud sync runs on m4max only", "topics": []},  # duplicate
    ]
    fake = FakeClient(
        inbox=[
            {"path": "projects/a/memos/2026-10-20-with.md", "content": "# With\n", "observations": obs,
             "created_at": "2026-10-20 15:59:00"},
            {"path": "projects/a/memos/2026-10-20-without.md", "content": "# Without\n", "observations": []},
        ]
    )
    result = cloud.run_pull(tmp_path, fake, dry_run=False, echo=lambda *_: None)
    assert result["no_observations"] == ["projects/a/memos/2026-10-20-without.md"]
    sidecar = tmp_path / "projects/a/memos/2026-10-20-with.obs.jsonl"
    records = read_sidecar(sidecar)  # validates every content_hash
    assert [r.content for r in records][0] == "Decision: hourly cloud sync runs on m4max only"
    assert len(records) == 2
    assert records[0].topics == ["cloud", "memex-project"]
    assert key not in sidecar.read_text() and "REDACTED" in records[1].content
    assert records[1].created_at == "2026-10-20 15:59:00"
    assert not (tmp_path / "projects/a/memos/2026-10-20-without.obs.jsonl").exists()


def test_pull_dry_run_writes_no_sidecar(tmp_path):
    fake = FakeClient(inbox=[{"path": "projects/a/memos/2026-10-20-x.md", "content": "# X\n",
                              "observations": [{"content": "Fact: something specific happened"}]}])
    result = cloud.run_pull(tmp_path, fake, dry_run=True, echo=lambda *_: None)
    assert result["pulled"] == ["projects/a/memos/2026-10-20-x.md"] and result["no_observations"] == []
    assert not any(tmp_path.rglob("*.obs.jsonl")) and fake.posts == []


def test_login_saves_url_and_private_token():
    result = CliRunner().invoke(app, ["cloud", "login", "--url", "https://m.example.workers.dev/"], input="tok-123\n")
    assert result.exit_code == 0, result.output
    target = cloud.resolve_target()
    assert target.url == "https://m.example.workers.dev" and target.token == "tok-123"
    assert stat.S_IMODE(cloud.cloud_token_path().stat().st_mode) == 0o600
    assert "tok-123" not in repr(target)


def test_commands_explain_missing_configuration():
    result = CliRunner().invoke(app, ["cloud", "pull"])
    assert result.exit_code == 1
    assert "memex cloud login" in result.output
