"""`memex cloud` — mirror the vault to a memex cloud Worker and pull its inbox.

The vault stays the source of truth. `push` projects the local index (docs,
chunks, observations and the vectors already computed for them) into the
Worker's D1 + Vectorize; `pull` writes memos that remote agents saved through
the MCP `save_memo` tool into the vault as ordinary files. See cloud/README.md.

A push publishes the working tree by default; `cloud.publish = "committed"`
opts a git vault into publishing only what HEAD holds (see "Publication"
below). `forget` removes a doc from the cloud at once.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import re
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import typer

app = typer.Typer(help="Mirror the vault to a memex cloud deployment (remote MCP).", no_args_is_help=True)

# Requests stay well under the Worker's per-invocation subrequest budget and
# the sync API's per-request caps (100 docs / 100 observation sets).
DOCS_PER_REQUEST = 25
OBS_SETS_PER_REQUEST = 50
MAX_REQUEST_BYTES = 8_000_000
TIMEOUT = 120

# Server versions (cloud/src/version.ts, released as git tag cloud-v<version>).
# CLOUD_SERVER_VERSION is the server this CLI was released with: an older one
# gets an "update available" note. MIN_SERVER_VERSION is the oldest that still
# works with this CLI: push/pull refuse below it. Raise MIN only when a release
# note says "Server update: required".
CLOUD_SERVER_VERSION = "0.2.1"
MIN_SERVER_VERSION = "0.2.0"  # the first released server (cloud-v0.2.0)
UPDATE_URL = "https://github.com/linxule/memex-plugin/tree/main/cloud#updating"


# ── Settings + token storage ────────────────────────────────────────


def _state_dir() -> Path:
    from memex.config import get_settings

    return Path(get_settings().state_dir).expanduser().resolve()


def cloud_config_path() -> Path:
    return _state_dir() / "cloud.json"


def cloud_token_path() -> Path:
    return _state_dir() / "credentials" / "cloud-sync-token"


def cloud_forget_path() -> Path:
    return _state_dir() / "cloud-forget.json"


def _write_private(path: Path, text: str) -> None:
    """Atomic owner-only write, same shape as credentials.save_gemini_key."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        try:
            os.chmod(path.parent, 0o700)
        except OSError as exc:
            print(f"Warning: could not tighten {path.parent} to 0700: {exc}", file=sys.stderr)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass
class CloudTarget:
    url: str
    token: str = field(repr=False)


def resolve_target() -> CloudTarget:
    """URL from MEMEX_CLOUD_URL or cloud.json; token from MEMEX_CLOUD_TOKEN or the saved file."""
    url = os.environ.get("MEMEX_CLOUD_URL", "")
    if not url and cloud_config_path().exists():
        try:
            url = json.loads(cloud_config_path().read_text()).get("url", "")
        except (json.JSONDecodeError, OSError):
            url = ""
    token = os.environ.get("MEMEX_CLOUD_TOKEN", "")
    if not token and cloud_token_path().is_file():
        token = cloud_token_path().read_text().strip()
    if not url or not token:
        raise ValueError("No memex cloud configured. Run: memex cloud login --url https://<your-worker>.workers.dev")
    return CloudTarget(url=url.rstrip("/"), token=token)


class CloudClient:
    def __init__(self, target: CloudTarget, session: Any | None = None):
        import requests

        self.base = target.url
        self.session = session or requests.Session()
        self.session.headers.update({"authorization": f"Bearer {target.token}"})

    def get(self, route: str) -> dict:
        res = self.session.get(f"{self.base}{route}", timeout=TIMEOUT)
        return self._check(res)

    def post(self, route: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"))
        res = self.session.post(
            f"{self.base}{route}", data=body, headers={"content-type": "application/json"}, timeout=TIMEOUT
        )
        return self._check(res)

    @staticmethod
    def _check(res) -> dict:
        try:
            data = res.json()
        except ValueError:
            data = {"error": res.text[:300]}
        if res.status_code >= 400:
            raise RuntimeError(f"HTTP {res.status_code}: {data.get('error', data)}")
        return data


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return (0,)


def server_notes(health: dict) -> tuple[list[str], bool]:
    """Messages about the deployment's version and schema, and whether this CLI
    may sync with it. Servers from before /health reported a version are 0.1.0."""
    version = str(health.get("version") or "0.1.0")
    notes: list[str] = []
    usable = True
    if _version_tuple(version) < _version_tuple(MIN_SERVER_VERSION):
        usable = False
        notes.append(
            f"Your memex cloud server is {version}; this memex needs {MIN_SERVER_VERSION} or newer. "
            f"Update the deployment first: {UPDATE_URL}"
        )
    elif _version_tuple(version) < _version_tuple(CLOUD_SERVER_VERSION):
        notes.append(f"memex cloud {CLOUD_SERVER_VERSION} is available (yours is {version}): {UPDATE_URL}")
    if health.get("schema_ok") is False:
        notes.append(
            f"The server's database is behind its code (last migration: {health.get('schema')}). "
            "Redeploy, or run `npx wrangler d1 migrations apply DB --remote` in your deployment repo."
        )
    return notes, usable


# ── Reading the local index ─────────────────────────────────────────


@dataclass
class LocalDoc:
    path: str
    title: str
    type: str
    project: str
    date: str
    content: str
    content_hash: str


def _blob_to_floats(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    n = len(blob) // 4
    return [round(x, 7) for x in struct.unpack(f"{n}f", blob)]


def is_transcript(doc: LocalDoc) -> bool:
    return doc.type == "transcript" or "/transcripts/" in doc.path


def outbound_text(text: str) -> str:
    """Text as it may leave the machine: secrets redacted (``memex.scrub``).

    Claude Code scrubs memos as they are written (PostToolUse hook), but Codex,
    Kimi Code, Obsidian edits and git pulls write without it (v0.22.0: other
    agents write their own memos). Every push scrubs what it sends, whoever
    wrote it; the local vault and the comparison hashes are untouched.
    """
    from memex.scrub import scrub_text

    return scrub_text(text or "", apply=True)[0]


def local_docs(conn: sqlite3.Connection, include_transcripts: bool) -> dict[str, LocalDoc]:
    docs: dict[str, LocalDoc] = {}
    for path, title, content, typ, project, date in conn.execute(
        "SELECT path, title, content, type, project, date FROM fts_content"
    ):
        doc = LocalDoc(
            path=path,
            title=title or "",
            type=typ or "",
            project=project or "",
            date=str(date or "")[:10],
            content=content or "",
            content_hash=hashlib.sha256((content or "").encode()).hexdigest(),
        )
        if include_transcripts or not is_transcript(doc):
            docs[path] = doc
    return docs


def stored_vector_dim(conn: sqlite3.Connection, table: str = "vec_chunks") -> int | None:
    try:
        row = conn.execute(f"SELECT embedding FROM {table} LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return len(row[0]) // 4 if row and row[0] else None


def doc_chunks(conn: sqlite3.Connection, path: str, with_vectors: bool) -> list[dict]:
    if with_vectors:
        sql = (
            "SELECT c.chunk_index, c.content, v.embedding FROM chunks c "
            "LEFT JOIN vec_chunks v ON v.rowid = c.id WHERE c.doc_path = ? ORDER BY c.chunk_index"
        )
    else:
        sql = "SELECT chunk_index, content, NULL FROM chunks WHERE doc_path = ? ORDER BY chunk_index"
    return [
        {"index": idx, "content": content, "vector": _blob_to_floats(emb)}
        for idx, content, emb in conn.execute(sql, (path,))
    ]


def observation_sets(conn: sqlite3.Connection, paths: set[str]) -> dict[str, dict]:
    """doc_path -> {set_hash, observations[]} for documents in the mirror."""
    try:
        rows = conn.execute(
            "SELECT o.id, o.doc_path, o.content, o.content_hash, o.obs_type, o.confidence, o.created_at "
            "FROM observations o ORDER BY o.doc_path, o.content_hash"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    topics: dict[int, list[str]] = {}
    for oid, slug in conn.execute("SELECT observation_id, topic_slug FROM observation_topics ORDER BY topic_slug"):
        topics.setdefault(oid, []).append(slug)
    sets: dict[str, dict] = {}
    for oid, doc_path, content, chash, otype, conf, created in rows:
        if doc_path not in paths:
            continue
        entry = sets.setdefault(doc_path, {"ids": [], "observations": []})
        entry["ids"].append(oid)
        entry["observations"].append(
            {
                "content": content,
                "content_hash": chash,
                "obs_type": otype or "explicit",
                "confidence": conf or "high",
                "created_at": str(created or ""),
                "topics": topics.get(oid, []),
            }
        )
    for entry in sets.values():
        fingerprint = "\n".join(
            f"{o['content_hash']}|{o['obs_type']}|{o['confidence']}|{','.join(o['topics'])}"
            for o in entry["observations"]
        )
        entry["set_hash"] = hashlib.sha256(fingerprint.encode()).hexdigest()
    return sets


def with_observation_vectors(conn: sqlite3.Connection, entry: dict) -> list[dict]:
    """Copies of the entry's observations with vectors attached; `entry` stays vector-free."""
    out = []
    for oid, obs in zip(entry["ids"], entry["observations"]):
        try:
            row = conn.execute("SELECT embedding FROM vec_observations WHERE rowid = ?", (oid,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        out.append({**obs, "vector": _blob_to_floats(row[0]) if row else None})
    return out


# ── Publication: what a push may publish ────────────────────────────
#
# By default (cloud.publish unset or "working-tree", for every vault) a push
# publishes every indexed doc, so claude.ai sessions see what local agents
# see (owner decision, 2026-10-28); `memex cloud forget` is the privacy tool.
# With cloud.publish = "committed" (opt-in, git vaults) a push publishes the
# vault as committed at HEAD: review happens at commit, so an agent's
# un-reviewed draft never reaches the cloud. Per doc:
#   - not in HEAD (untracked, ignored, only staged): never sent; a copy the
#     cloud holds from earlier is deleted, as for a committed deletion;
#   - in HEAD but changed in the git index or working tree (edited, deleted,
#     renamed, not yet committed): the cloud keeps the version it has, so
#     neither the dirty version nor a delete goes out;
#   - in HEAD and clean: sent from the local index only when the indexed text
#     IS HEAD's blob (same git blob id, or the same text up to newlines), so a
#     stale index cannot publish an older state; chunks and vectors come from
#     that same text. Otherwise held until `memex index rebuild`.
# Committed content is identified with one `git ls-tree` + one `git status`,
# not by reading every file back from git: blob ids are hashed from text the
# index already holds, and only mismatches and sidecars are read from git.
# Observation sets follow their `.obs.jsonl` sidecar by the same rule, checked
# against the sidecar hash the last ingest/write recorded in `obs_sidecars`.

PUBLISH_MODES = ("committed", "working-tree")
GIT_TIMEOUT = 120
# Variables that point git at another repository or index (set inside git
# hooks, for example). The vault's own repository is what counts here.
_GIT_LOCATION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    }
)

HELD_REASONS = {
    "uncommitted": "not committed",
    "ignored": "gitignored",
    "modified": "changed since HEAD",
    "stale": "index differs from HEAD",
    "forgotten": "forgotten",
}


def _nfc(path: str) -> str:
    # git on macOS reports precomposed (NFC) names; a file created with a
    # decomposed name is indexed as such. Compare paths in one form.
    return unicodedata.normalize("NFC", path)


def _subcommand(args: tuple[str, ...]) -> str:
    return next((a for a in args if not a.startswith("-")), "")


def _git(vault: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Run git in the vault. FileNotFoundError (no git) propagates; a timeout or
    any other OS failure becomes RuntimeError."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    # No optional locks: this read must never contend with the owner's git.
    env.update(LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
    try:
        return subprocess.run(
            ["git", "-C", str(vault), *args],
            input=stdin,
            capture_output=True,
            env=env,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"git {_subcommand(args)} did not finish in {vault}: {exc}") from exc


def _git_out(vault: Path, *args: str, stdin: bytes | None = None) -> bytes:
    res = _git(vault, *args, stdin=stdin)
    if res.returncode != 0:
        detail = res.stderr.decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"git {_subcommand(args)} failed in {vault}: {detail}")
    return res.stdout


def _decode_path(raw: bytes) -> str:
    return _nfc(raw.decode("utf-8", "surrogateescape"))


def _blob_id(data: bytes, like: str) -> str:
    """git's object id for a blob with these bytes, in the hash `like` uses."""
    algo = hashlib.sha256 if len(like) == 64 else hashlib.sha1
    return algo(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _same_text(data: bytes | None, content: str) -> bool:
    """Whether a blob reads as `content` the way the indexer reads a file
    (UTF-8, universal newlines)."""
    if data is None:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text.replace("\r\n", "\n").replace("\r", "\n") == content


@dataclass
class GitView:
    """HEAD's files under the vault and the ones that differ from it."""

    vault: Path
    head: str  # HEAD's commit id; "" when the repository has no commits
    blobs: dict[str, str]  # NFC vault-relative path -> blob id at HEAD
    dirty: set[str]  # NFC vault-relative paths whose git index or working tree differs from HEAD

    def read_blobs(self, ids: Iterable[str]) -> dict[str, bytes]:
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        out = _git_out(self.vault, "cat-file", "--batch", stdin="".join(f"{i}\n" for i in wanted).encode())
        blobs: dict[str, bytes] = {}
        pos = 0
        while pos < len(out):
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            pos = end + 1
            if len(header) != 3:  # "<id> missing"
                continue
            size = int(header[2])
            blobs[header[0].decode()] = out[pos : pos + size]
            pos += size + 1
        return blobs

    def ignored(self, paths: Iterable[str]) -> set[str]:
        """The NFC forms of `paths` that a .gitignore rule matches."""
        wanted = [p.encode("utf-8", "surrogateescape") for p in paths]
        if not wanted:
            return set()
        res = _git(self.vault, "check-ignore", "-z", "--stdin", stdin=b"\0".join(wanted) + b"\0")
        if res.returncode not in (0, 1):  # 1: none of them is ignored
            raise RuntimeError(f"git check-ignore failed in {self.vault}: {res.stderr.decode('utf-8', 'replace')[:300]}")
        return {_decode_path(p) for p in res.stdout.split(b"\0") if p}


def _repo_prefix(vault: Path) -> str | None:
    """The vault's path inside its repository ("" at the root, else "a/b/"), or
    None when it is not in a git work tree. Raises RuntimeError when it is in a
    repository git cannot read: that must stop a push, not quietly fall back
    to publishing the working tree."""
    try:
        res = _git(vault, "rev-parse", "--show-prefix")
    except FileNotFoundError as exc:
        if any((p / ".git").exists() for p in (vault, *vault.parents)):
            raise RuntimeError(f"{vault} is in a git repository, but git is not installed") from exc
        return None
    if res.returncode != 0:
        if b"not a git repository" in res.stderr:
            return None
        detail = res.stderr.decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"git cannot read the repository at {vault}: {detail}")
    return res.stdout.decode("utf-8", "surrogateescape").strip()


def _head_commit(vault: Path) -> str:
    """HEAD's commit id, or "" on a branch with no commits yet. A HEAD that
    names something which is not a readable commit raises RuntimeError."""
    head = _git(vault, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if head.returncode == 0:
        return head.stdout.decode().strip()
    if _git(vault, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return ""  # unborn branch
    raise RuntimeError(f"the vault's HEAD does not point at a readable commit ({vault}); run `git status` there")


def _ls_tree(vault: Path, *paths: str) -> dict[str, str]:
    """NFC vault-relative path -> blob id at HEAD (run from the vault, ls-tree
    lists only the vault's subtree, vault-relative)."""
    blobs: dict[str, str] = {}
    # Literal pathspecs: a vault path is a name, never a glob.
    args = ("--literal-pathspecs", "ls-tree", "-r", "-z", "HEAD", "--", *paths) if paths else ("ls-tree", "-r", "-z", "HEAD")
    for entry in _git_out(vault, *args).split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == b"blob":
            blobs[_decode_path(path)] = parts[2].decode()
    return blobs


def head_blobs(vault: Path, paths: list[str]) -> dict[str, str | None] | None:
    """Each path's blob id at HEAD (None: not committed), or None when the vault
    is not in a git work tree. Reads only git's objects, never the work tree."""
    if _repo_prefix(vault) is None:
        return None
    if not _head_commit(vault):
        return dict.fromkeys(paths)
    found: dict[str, str] = {}
    for i in range(0, len(paths), 500):
        found.update(_ls_tree(vault, *paths[i : i + 500]))
    return {p: found.get(_nfc(p)) for p in paths}


def git_view(vault: Path) -> GitView | None:
    """What git says about the vault, or None when it is not in a git work tree.
    Raises RuntimeError when git cannot answer for a vault that is in one."""
    prefix = _repo_prefix(vault)
    if prefix is None:
        return None
    head = _head_commit(vault)
    if not head:
        return GitView(vault=vault, head="", blobs={}, dirty=set())
    blobs = _ls_tree(vault)
    dirty: set[str] = set()
    # Porcelain paths are relative to the repository root, whatever the cwd.
    status = _git_out(vault, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--no-renames", "--", ".")
    for entry in status.split(b"\0"):
        if len(entry) > 3:
            path = _decode_path(entry[3:])
            dirty.add(path[len(prefix) :] if prefix and path.startswith(prefix) else path)
    return GitView(vault=vault, head=head, blobs=blobs, dirty=dirty)


@dataclass
class PublishMode:
    mode: str  # "committed" | "working-tree"
    reason: str  # why, for push output
    git: GitView | None = None
    vault: Path | None = None


def configured_publish_mode() -> str | None:
    """`cloud.publish` from config.json (None when unset)."""
    from memex.config import get_settings
    from memex.paths import _read_config_value

    section = getattr(get_settings(), "cloud", None)  # a declared Settings.cloud, if there is one
    value = section.get("publish") if isinstance(section, dict) else getattr(section, "publish", None)
    if value is None:
        raw = _read_config_value("cloud")
        value = raw.get("publish") if isinstance(raw, dict) else None
    return value


def resolve_publish_mode(vault: Path, requested: str | None = None) -> PublishMode:
    """--publish, else cloud.publish, else "working-tree" for every vault, git or
    not (owner decision 2026-10-28: claude.ai sessions see what local agents
    see). "committed" is an explicit opt-in and needs committed files."""
    source = "--publish" if requested else "cloud.publish"
    value = requested or configured_publish_mode()
    if value is not None and value not in PUBLISH_MODES:
        raise RuntimeError(f"{source} must be one of {', '.join(PUBLISH_MODES)}, not {value!r}")
    if value is None:
        return PublishMode("working-tree", "default", vault=vault)
    if value == "working-tree":
        return PublishMode("working-tree", f"{source} = working-tree", vault=vault)
    view = git_view(vault)
    if view is None:
        raise RuntimeError(f"{source} = committed, but {vault} is not in a git work tree")
    if not view.head:
        raise RuntimeError(f"{source} = committed, but the vault's repository has no commits yet")
    if not view.blobs:
        # e.g. a vault inside a repository that does not track it: every doc
        # would read as uncommitted and leave the cloud.
        raise RuntimeError(f"{source} = committed, but nothing under {vault} is committed")
    return PublishMode("committed", f"{source} = committed, HEAD {view.head[:7]}", view, vault)


# Forget records: `memex cloud forget` writes one per path, before its remote
# delete, so a failed delete is finished by the next push and no push
# re-publishes the doc while the record applies (forget_applies).


def load_forgotten() -> dict[str, dict]:
    path = cloud_forget_path()
    if not path.exists():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")).get("paths", {}))
    except (json.JSONDecodeError, OSError, AttributeError) as exc:
        # Never guess: an unreadable list could re-publish a forgotten doc.
        raise RuntimeError(f"cannot read {path} ({exc}); fix or remove it") from exc


def save_forgotten(records: dict[str, dict]) -> None:
    text = json.dumps({"version": 1, "paths": records}, indent=2, sort_keys=True, ensure_ascii=False)
    _write_private(cloud_forget_path(), text + "\n")


def forget_applies(record: dict, path: str, publish: PublishMode, doc_hash: str | None) -> bool:
    """Whether a forget still keeps `path` out of the cloud. Never while its
    indexed content is the forgotten version (`hashes`: what the cloud held,
    or the local text when the cloud had none). Beyond that: with
    publish=committed, until a different version is committed (`head`: its
    blob at HEAD when forgotten, None if uncommitted); otherwise until its
    content changes."""
    if doc_hash is not None and doc_hash in record.get("hashes", []):
        return True
    if publish.mode == "committed" and publish.git is not None and "head" in record:
        return publish.git.blobs.get(_nfc(path)) == record["head"]
    return doc_hash is None


def _sidecar_rel(doc_path: str) -> str:
    from memex.sidecars import SIDECAR_SUFFIX

    return doc_path.removesuffix(".md") + SIDECAR_SUFFIX


def _sidecar_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(conn.execute("SELECT doc_path, content_hash FROM obs_sidecars"))
    except sqlite3.OperationalError:
        return {}


@dataclass
class Publication:
    docs: dict[str, LocalDoc]  # what this push may publish
    obs_sets: dict[str, dict]  # observation sets it may publish (docs without one get an empty set)
    hold: set[str] = field(default_factory=set)  # NFC paths whose cloud copy stays as it is
    obs_hold: set[str] = field(default_factory=set)  # docs whose cloud observation set stays as it is
    forget: set[str] = field(default_factory=set)  # paths a forget record keeps out of the cloud
    held: dict[str, str] = field(default_factory=dict)  # local doc -> why it is not published
    obs_held: dict[str, str] = field(default_factory=dict)  # doc -> why its observation set is not
    released: list[str] = field(default_factory=list)  # forget records that no longer apply, doc published


def filter_publication(
    conn: sqlite3.Connection,
    local: dict[str, LocalDoc],
    obs_sets: dict[str, dict],
    publish: PublishMode,
    forgotten: dict[str, dict],
) -> Publication:
    pub = Publication(docs={}, obs_sets={})
    keep_out = set()
    for path, record in forgotten.items():
        doc = local.get(path)
        if forget_applies(record, path, publish, doc.content_hash if doc else None):
            keep_out.add(path)
    git = publish.git if publish.mode == "committed" else None
    if git is not None:
        # Only what HEAD has can be kept; a path new since HEAD has no
        # committed version, so a cloud copy of it is deleted.
        pub.hold = {p for p in git.dirty if p in git.blobs}

    unverified: dict[str, str] = {}
    for path, doc in local.items():
        if path in keep_out:
            pub.held[path] = "forgotten"
            continue
        if git is None:
            pub.docs[path] = doc
            continue
        key = _nfc(path)
        blob = git.blobs.get(key)
        if blob is None:
            pub.held[path] = "uncommitted"
        elif key in git.dirty:
            pub.held[path] = "modified"
        elif _blob_id(doc.content.encode("utf-8", "surrogatepass"), blob) == blob:
            pub.docs[path] = doc
        else:
            unverified[path] = blob
    if unverified:
        texts = git.read_blobs(unverified.values())
        for path, blob in unverified.items():
            if _same_text(texts.get(blob), local[path].content):
                pub.docs[path] = local[path]
            else:
                pub.held[path] = "stale"
                pub.hold.add(_nfc(path))
    uncommitted = [p for p, why in pub.held.items() if why == "uncommitted"]
    if git is not None and uncommitted:
        ignored = git.ignored(uncommitted)
        for path in uncommitted:
            if _nfc(path) in ignored:
                pub.held[path] = "ignored"
    pub.forget = keep_out
    pub.released = [p for p in forgotten if p not in keep_out and p in pub.docs]

    if git is None:
        pub.obs_sets = {p: e for p, e in obs_sets.items() if p in pub.docs}
        return pub
    recorded = _sidecar_hashes(conn)
    committed: dict[str, str] = {}
    for path in pub.docs:
        key = _nfc(_sidecar_rel(path))
        blob = git.blobs.get(key)
        if blob is None:
            # HEAD has no observations for this doc: the cloud gets none.
            if path in obs_sets:
                pub.obs_held[path] = "uncommitted"
        elif key in git.dirty:
            pub.obs_hold.add(path)
            pub.obs_held[path] = "modified"
        else:
            committed[path] = blob
    contents = git.read_blobs(committed.values())
    for path, blob in committed.items():
        data = contents.get(blob)
        if data is not None and recorded.get(path) == hashlib.sha256(data).hexdigest():
            if path in obs_sets:
                pub.obs_sets[path] = obs_sets[path]
        else:
            pub.obs_hold.add(path)
            pub.obs_held[path] = "stale"
    return pub


def describe_held(publish: PublishMode, pub: Publication) -> str:
    line = f"publish={publish.mode} ({publish.reason})"
    if pub.held:
        counts = Counter(pub.held.values())
        line += f"; held back {len(pub.held)}: " + ", ".join(
            f"{counts[r]} {HELD_REASONS[r]}" for r in HELD_REASONS if counts[r]
        )
    if pub.obs_held:
        line += f"; observation sets held back {len(pub.obs_held)}"
    return line


def rejected_inbox_memos(client: CloudClient, manifest: dict, local: dict[str, LocalDoc], publish: PublishMode) -> set[str]:
    """Inbox memos (saved by a remote agent) that were pulled and then removed
    from the vault. "Never delete an inbox memo" protects one only until it is
    pulled; with publish=committed a pulled memo is not re-sent (which would
    end its inbox status) before it is committed, so without this a memo the
    owner rejected would stay in the cloud. Kept: still pending in the inbox,
    on disk, or committed."""
    vault = publish.vault
    if vault is None:
        return set()
    blobs = publish.git.blobs if publish.git is not None else {}
    gone = [
        p
        for p, r in manifest.get("docs", {}).items()
        if r.get("source") == "inbox" and p not in local and _nfc(p) not in blobs and not (vault / p).exists()
    ]
    if not gone:
        return set()
    pending = {m.get("path") for m in client.get("/api/sync/inbox").get("memos", [])}
    return {p for p in gone if p not in pending}


# ── Push planning ───────────────────────────────────────────────────


@dataclass
class PushPlan:
    upsert: list[str]
    delete: list[str]
    obs: list[str]
    unchanged: int


def plan_push(
    local: dict[str, LocalDoc],
    obs_sets: dict[str, dict],
    manifest: dict,
    full: bool = False,
    *,
    hold: set[str] | frozenset[str] = frozenset(),
    obs_hold: set[str] | frozenset[str] = frozenset(),
    drop: set[str] | frozenset[str] = frozenset(),
) -> PushPlan:
    """`local`: the docs this push may publish. `hold`: NFC paths whose cloud
    copy stays as it is (not deleted); `obs_hold`: docs whose cloud observation
    set stays as it is (not replaced, not emptied); `drop`: paths to delete
    from the cloud whatever else would keep them (forgotten docs, inbox memos
    that were pulled and then removed)."""
    remote_docs: dict[str, dict] = manifest.get("docs", {})
    remote_obs: dict[str, str] = manifest.get("observations", {})
    # A pulled inbox memo has identical bytes locally; re-send it anyway so the
    # cloud row flips from source=inbox to source=vault.
    upsert = [
        p
        for p, d in local.items()
        if full
        or remote_docs.get(p, {}).get("hash") != d.content_hash
        or remote_docs.get(p, {}).get("source") == "inbox"
    ]
    # Inbox memos live only in the cloud until `memex cloud pull`; never delete them.
    delete = [
        p
        for p, r in remote_docs.items()
        if p in drop
        or (p not in local and r.get("source") != "inbox" and (not hold or _nfc(p) not in hold))
    ]
    upserting = set(upsert)
    obs = [
        p
        for p, entry in obs_sets.items()
        if p not in obs_hold and (full or p in upserting or remote_obs.get(p) != entry["set_hash"])
    ]
    # A doc whose observations were all removed locally: send an empty set.
    obs += [p for p in remote_obs if p in local and p not in obs_sets and p not in obs_hold]
    return PushPlan(upsert=upsert, delete=delete, obs=obs, unchanged=len(local) - len(upsert))


def _batched(items: Iterable[dict], max_items: int) -> Iterator[list[dict]]:
    """Group payloads by count and serialized size. Takes an iterator so a full
    push never holds every vector in memory at once (~36K x 768 floats for a
    typical vault)."""
    batch: list[dict] = []
    size = 0
    for item in items:
        item_size = len(json.dumps(item, separators=(",", ":")))
        if batch and (len(batch) >= max_items or size + item_size > MAX_REQUEST_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch



# One cloud operation at a time on this machine. `run_push` reads the forget list
# once, at its start, so a `forget` landing mid-push would be undone by that same
# push re-publishing the doc (gauntlet round 1). A forget waits for an in-flight
# push, then deletes; a push waits for an in-flight forget, then sees it.
CLOUD_LOCK_WAIT_S = 900.0


@contextmanager
def cloud_lock(wait: float = CLOUD_LOCK_WAIT_S):
    from filelock import FileLock, Timeout

    from memex.paths import get_lock_dir

    lock = FileLock(str(get_lock_dir() / "cloud.lock"), timeout=wait)
    try:
        lock.acquire()
    except Timeout:
        raise RuntimeError(
            f"another memex cloud push or forget has held the lock for {int(wait)}s; try again when it finishes"
        ) from None
    try:
        yield
    finally:
        lock.release()


def _run_push(
    conn: sqlite3.Connection,
    client: CloudClient,
    *,
    include_transcripts: bool,
    vectors: bool,
    full: bool,
    dry_run: bool,
    echo=print,
    publish: PublishMode | None = None,
) -> dict:
    from memex.scripts.embeddings import get_embedding_config

    publish = publish or PublishMode("working-tree", "not resolved")
    forgotten = load_forgotten()
    manifest = client.get("/api/sync/manifest")
    remote_dim = int(manifest.get("vector_dimensions") or 0)
    cfg = get_embedding_config()
    dim = stored_vector_dim(conn)
    send_vectors = vectors and dim is not None and cfg.get("provider") == "google"
    if vectors and dim is None:
        echo("No local vectors found (sqlite-vec unavailable or index not embedded); pushing keyword-only.")
    elif vectors and cfg.get("provider") != "google":
        echo(f"Embedding provider is {cfg.get('provider')!r}; the cloud can only embed queries with Gemini, so pushing keyword-only.")
    if send_vectors and dim != remote_dim:
        raise RuntimeError(
            f"Your local index stores {dim}d vectors but the cloud index is {remote_dim}d. "
            f"Set VECTOR_DIMENSIONS={dim} in cloud/wrangler.jsonc (max 1536) and redeploy, "
            "or set embeddings.index_dimensions and run `memex index migrate-vec` locally."
        )

    # Incremental planning compares text hashes only. When the vectors the cloud
    # holds came from another model or size (or it has none yet, e.g. after a
    # keyword-only push), every document must be re-sent with today's vectors.
    remote_meta = manifest.get("meta", {})
    reembed = send_vectors and (
        remote_meta.get("embedding_model") != cfg.get("model", "")
        or str(remote_meta.get("embedding_dimensions")) != str(dim)
    )
    if reembed and not full:
        echo(
            f"The cloud's vectors are {remote_meta.get('embedding_model') or 'missing'} "
            f"({remote_meta.get('embedding_dimensions') or '-'}d); yours are {cfg.get('model', '')} ({dim}d). "
            "Sending everything."
        )
        full = True

    local = local_docs(conn, include_transcripts)
    obs_sets = observation_sets(conn, set(local))
    pub = filter_publication(conn, local, obs_sets, publish, forgotten)
    hold, obs_hold, drop = pub.hold, pub.obs_hold, set(pub.forget)
    if reembed and (hold or obs_hold):
        # A held copy would keep vectors from the old model under the new
        # model's name; take it out until it can be published again.
        echo("  Held docs and observation sets leave the cloud until they can be re-sent with the new vectors.")
        hold, obs_hold = set(), set()
    drop |= rejected_inbox_memos(client, manifest, local, publish)
    plan = plan_push(pub.docs, pub.obs_sets, manifest, full=full, hold=hold, obs_hold=obs_hold, drop=drop)
    summary: dict[str, Any] = {
        "docs_upsert": len(plan.upsert),
        "docs_delete": len(plan.delete),
        "observation_sets": len(plan.obs),
        "unchanged": plan.unchanged,
        "vectors": send_vectors,
        "dry_run": dry_run,
        "publish": publish.mode,
        "held_back": len(pub.held),
        "observation_sets_held": len(pub.obs_held),
        "held_back_reasons": dict(Counter(pub.held.values())),
    }
    echo(describe_held(publish, pub))
    if "stale" in pub.held.values() or "stale" in pub.obs_held.values():
        echo("  Some indexed content differs from HEAD; it goes once `memex index rebuild --incremental` catches up.")
    if "ignored" in pub.held.values():
        echo("  Gitignored docs (auto-memory, transcripts) never go with publish=committed; the default, working-tree, sends them.")
    if dry_run:
        summary["plan"] = {
            "upsert": plan.upsert,
            "delete": plan.delete,
            "observation_sets": plan.obs,
            "held_back": dict(sorted(pub.held.items())),
            "observation_sets_held": dict(sorted(pub.obs_held.items())),
        }
        return summary
    local, obs_sets = pub.docs, pub.obs_sets

    meta_matches = remote_meta.get("embedding_model") == cfg.get("model", "") and str(
        remote_meta.get("embedding_dimensions")
    ) == str(dim)
    if remote_meta.get("embedding_model") and (plan.upsert or plan.obs) and not (send_vectors and meta_matches):
        # Until this push completes, the cloud's vectors are not all from one
        # model (or, without vectors, some are gone). Clear the model first:
        # if the push stops halfway, the next one sees no model and sends
        # everything, and queries meanwhile go keyword-only.
        if not send_vectors:
            echo("This push carries no vectors; the cloud will re-receive all of them on the next push that does.")
        client.post("/api/sync/meta", {"clear": True})

    def doc_payloads() -> Iterator[dict]:
        for path in plan.upsert:
            d = local[path]
            yield {
                "path": d.path,
                "title": outbound_text(d.title),
                "type": d.type,
                "project": d.project,
                "date": d.date,
                "content": outbound_text(d.content),
                "content_hash": d.content_hash,
                "chunks": [{**c, "content": outbound_text(c["content"])} for c in doc_chunks(conn, path, send_vectors)],
            }

    sent = 0
    for batch in _batched(doc_payloads(), DOCS_PER_REQUEST):
        client.post("/api/sync/docs", {"docs": batch})
        sent += len(batch)
        echo(f"  docs {sent}/{len(plan.upsert)}")

    for i in range(0, len(plan.delete), 500):
        client.post("/api/sync/delete", {"paths": plan.delete[i : i + 500]})

    def obs_payloads() -> Iterator[dict]:
        for path in plan.obs:
            entry = obs_sets.get(path, {"ids": [], "observations": [], "set_hash": hashlib.sha256(b"").hexdigest()})
            d = local[path]
            yield {
                "doc_path": path,
                "set_hash": entry["set_hash"],
                "project": d.project,
                "type": d.type,
                "date": d.date,
                "observations": [
                    {**o, "content": outbound_text(str(o.get("content", "")))}
                    for o in (with_observation_vectors(conn, entry) if send_vectors else entry["observations"])
                ],
            }

    sent = 0
    for batch in _batched(obs_payloads(), OBS_SETS_PER_REQUEST):
        client.post("/api/sync/observations", {"sets": batch})
        sent += len(batch)
        echo(f"  observation sets {sent}/{len(plan.obs)}")
    if send_vectors:
        # Recorded last, once every vector this push owed has been sent.
        client.post(
            "/api/sync/meta",
            {"embedding_provider": "google", "embedding_model": cfg.get("model", ""), "embedding_dimensions": dim},
        )
    if pub.released:
        # Only after everything above went through. Re-read, so a forget run
        # meanwhile is kept; drop a record only if it is the one evaluated.
        records = load_forgotten()
        for path in pub.released:
            if records.get(path) == forgotten.get(path):
                records.pop(path, None)
        save_forgotten(records)
    return summary


# ── Forget ──────────────────────────────────────────────────────────

# Paths per delete request. The Worker deletes each doc's chunk and
# observation vectors with Vectorize deleteByIds (at most 100 ids per call) and
# accepts up to 500 paths; 100 keeps one request's work bounded for big docs.
FORGET_PATHS_PER_REQUEST = 100


def vault_rel_path(vault: Path, raw: str) -> str:
    """A path from the command line -> the vault-relative key the cloud uses.
    Absolute paths and paths relative to the cwd must lie inside the vault; a
    sidecar path names its doc."""
    from memex.sidecars import SIDECAR_SUFFIX

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and not (vault / candidate).exists() and (Path.cwd() / candidate).exists():
        candidate = Path.cwd() / candidate
    if candidate.is_absolute():
        try:
            # Resolve the directory, not a final symlink: the key is the link's path.
            rel = (candidate.parent.resolve() / candidate.name).relative_to(vault.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"{raw} is outside the vault ({vault})") from exc
    else:
        rel = PurePosixPath(raw).as_posix()
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or ".." in parts or rel == ".":
        raise ValueError(f"not a vault path: {raw}")
    if rel.endswith(SIDECAR_SUFFIX):
        rel = rel[: -len(SIDECAR_SUFFIX)] + ".md"
    return rel


def _indexed_hashes(conn: sqlite3.Connection | None, paths: list[str]) -> dict[str, str]:
    if conn is None:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(paths), 500):  # `path` is not indexed in FTS5: one scan per batch
        group = paths[i : i + 500]
        try:
            rows = conn.execute(
                f"SELECT path, content FROM fts_content WHERE path IN ({','.join('?' * len(group))})", group
            ).fetchall()
        except sqlite3.OperationalError:
            return out
        out.update({p: hashlib.sha256((c or "").encode()).hexdigest() for p, c in rows})
    return out


def _on_disk_name(vault: Path, rel: str) -> str | None:
    """`rel` spelled the way the file system stores it (the index's key), or
    None when there is no such file. A case-insensitive volume finds
    `Projects/X.md` for `projects/x.md`; the index knows only the stored name."""
    current, out = vault, []
    for part in PurePosixPath(rel).parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None
        if part not in names:
            want = _nfc(part).casefold()
            part = next((n for n in names if _nfc(n).casefold() == want), None)
            if part is None:
                return None
        out.append(part)
        current = current / part
    return "/".join(out) if current.is_file() else None


def _cloud_key(vault: Path, rel: str, remote: dict) -> str:
    """The key the cloud and the index use for a typed vault path."""
    if rel in remote:
        return rel
    stored = _on_disk_name(vault, rel)
    if stored is not None:
        return stored
    want = _nfc(rel).casefold()
    return next((k for k in remote if _nfc(k).casefold() == want), rel)



def run_push(conn: sqlite3.Connection, client, **kwargs) -> dict:
    """`_run_push` inside ONE read transaction (WAL snapshot): every row it sends
    — document text checked against HEAD, then its chunks and observations — comes
    from the same index state. Without it, an incremental rebuild landing between
    the document check and the chunk query could ship uncommitted chunks under an
    approved document in committed mode (gauntlet round 1). The push never writes
    the local index, so the transaction is only rolled back."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        return _run_push(conn, client, **kwargs)
    finally:
        if own and conn.in_transaction:
            conn.rollback()

def run_forget(
    vault: Path,
    client: CloudClient,
    paths: list[str],
    *,
    conn: sqlite3.Connection | None = None,
    confirm: Callable[[list[str], dict], bool] | None = None,
    echo=print,
) -> dict:
    """Delete `paths` (vault-relative) from the cloud now, with their
    observations and vectors, and record them so no push re-publishes them
    while forget_applies. The record is written BEFORE the delete: if the
    delete fails, the next push sees the record and deletes them itself."""
    manifest = client.get("/api/sync/manifest")
    remote = manifest.get("docs", {})
    paths = list(dict.fromkeys(_cloud_key(vault, p, remote) for p in paths))
    indexed = _indexed_hashes(conn, paths)
    unknown = [p for p in paths if p not in remote and p not in indexed and not (vault / p).is_file()]
    if unknown:
        raise RuntimeError("not in the cloud or the vault (check the path): " + ", ".join(unknown))
    if confirm is not None and not confirm(paths, remote):
        return {"forgotten": [], "were_in_cloud": [], "aborted": True}
    try:
        heads = head_blobs(vault, paths)  # objects only: no work-tree scan on the urgent path
    except RuntimeError as exc:
        echo(f"Note: {exc}; recording content hashes only.")
        heads = None

    records = load_forgotten()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for path in paths:
        # The forgotten version: what the cloud holds, else the local text.
        digest = remote.get(path, {}).get("hash") or indexed.get(path)
        if digest is None:
            try:
                digest = hashlib.sha256((vault / path).read_text(encoding="utf-8").encode()).hexdigest()
            except (OSError, UnicodeDecodeError):
                digest = None
        record: dict[str, Any] = {"forgotten_at": now, "hashes": [digest] if digest else []}
        if heads is not None:
            record["head"] = heads[path]  # None: not committed
        records[path] = record
    save_forgotten(records)
    for path in paths:
        if remote.get(path, {}).get("source") == "inbox":
            echo(
                f"Note: {path} was saved by a remote agent; its copy in the cloud inbox stays until "
                "`memex cloud pull` brings it into the vault, and this forget keeps that version from being published."
            )

    for i in range(0, len(paths), FORGET_PATHS_PER_REQUEST):
        client.post("/api/sync/delete", {"paths": paths[i : i + FORGET_PATHS_PER_REQUEST]})
    still = [p for p in paths if p in client.get("/api/sync/manifest").get("docs", {})]
    if still:
        raise RuntimeError(
            "still in the cloud after the delete: " + ", ".join(still) + " (recorded: the next push retries it)"
        )
    return {"forgotten": paths, "were_in_cloud": [p for p in paths if p in remote], "aborted": False}


# ── Pull ────────────────────────────────────────────────────────────


# Letters and digits in any script (the Worker's slugify keeps Unicode titles,
# e.g. 2026-10-20-研究.md), plus . _ -; never a leading dot, underscore or dash.
_SAFE_SEGMENT = re.compile(r"^[^\W_][\w.-]{0,199}$")


def safe_vault_path(vault: Path, rel: str) -> Path:
    """A pulled memo may only land at <vault>/projects/<name>/memos/<file>.md,
    with plain names (no `..`, no leading dot) for <name> and <file>."""
    parts = Path(rel).parts
    if (
        len(parts) != 4
        or parts[0] != "projects"
        or parts[2] != "memos"
        or not rel.endswith(".md")
        or not all(_SAFE_SEGMENT.match(p) and ".." not in p for p in (parts[1], parts[3]))
    ):
        raise ValueError(f"refusing unexpected inbox path: {rel}")
    target = (vault / rel).resolve()
    target.relative_to(vault.resolve())  # raises ValueError if it escapes (e.g. via a symlink)
    return target


def render_inbox_sidecar(observations: list[dict], created_at: str) -> tuple[str, int]:
    """The `.obs.jsonl` text for observations a remote agent sent with its memo,
    in render_sidecar's shape, plus how many secrets were redacted. Validated
    with read_sidecar, so a bad record fails here instead of at ingest."""
    from memex.scrub import scrub_text
    from memex.sidecars import read_sidecar

    lines, redacted, seen = [], 0, set()
    for obs in observations:
        content, matches = scrub_text(str(obs.get("content", "")).strip(), apply=True)
        redacted += len(matches)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not content or digest in seen:
            continue
        seen.add(digest)
        record = {
            "content": content,
            "content_hash": digest,
            "obs_type": str(obs.get("obs_type") or "explicit"),
            "confidence": str(obs.get("confidence") or "high"),
            "topics": sorted({str(t) for t in obs.get("topics") or []}),
            "source_obs": [],
            "created_at": str(obs.get("created_at") or created_at),
        }
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
    text = "".join(line + "\n" for line in lines)
    read_sidecar(Path("<inbox>"), text=text)
    return text, redacted


ACKS_PER_REQUEST = 500  # the sync API's cap


def run_pull(vault: Path, client: CloudClient, *, dry_run: bool, echo=print) -> dict:
    from memex.scrub import _atomic_write, scrub_text
    from memex.sidecars import SidecarError, sidecar_path

    memos = client.get("/api/sync/inbox").get("memos", [])
    written, skipped, acked, no_obs = [], [], [], []
    for memo in memos:
        rel = memo["path"]
        try:
            target = safe_vault_path(vault, rel)
        except ValueError as exc:
            echo(f"  skip {rel}: {exc}")
            skipped.append(rel)
            continue
        # Prose written outside Claude Code's Write tool: scrub it here, and
        # compare against the scrubbed text when checking an earlier pull.
        content, matches = scrub_text(memo["content"], apply=True)
        sidecar_text, obs_redacted = "", 0
        if memo.get("observations"):
            try:
                sidecar_text, obs_redacted = render_inbox_sidecar(memo["observations"], memo.get("created_at", ""))
            except SidecarError as exc:
                echo(f"  skip {rel}: its observations are malformed ({exc})")
                skipped.append(rel)
                continue
        sidecar = sidecar_path(vault, rel)
        if sidecar_text and sidecar is None:
            echo(f"  skip {rel}: no sidecar path for it")
            skipped.append(rel)
            continue
        if target.exists():
            if target.read_text(encoding="utf-8") != content:
                echo(f"  skip {rel}: a different file already exists there")
                skipped.append(rel)
                continue
            # Pulled before (an interrupted run, or another machine): finish the
            # sidecar if it is missing, then acknowledge.
            if sidecar_text and sidecar and not sidecar.exists() and not dry_run:
                _atomic_write(sidecar, sidecar_text)
            elif sidecar_text and sidecar and sidecar.read_text(encoding="utf-8") != sidecar_text:
                echo(f"  {rel}: kept your existing {sidecar.name} (it differs from the inbox copy)")
            acked.append(rel)
            continue
        if sidecar_text and sidecar and sidecar.exists() and sidecar.read_text(encoding="utf-8") != sidecar_text:
            echo(f"  skip {rel}: a different {sidecar.name} already exists there")
            skipped.append(rel)
            continue
        if not sidecar_text:
            no_obs.append(rel)
        if dry_run:
            written.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Sidecar first, memo last and atomically: the memo's presence is what
        # marks a pull done, so an interrupted run is simply repeated.
        if sidecar_text and sidecar:
            _atomic_write(sidecar, sidecar_text)
        _atomic_write(target, content)
        redacted = len(matches) + (obs_redacted if sidecar_text else 0)
        if redacted:
            echo(f"  {rel}: redacted {redacted} secret(s)")
        written.append(rel)
        acked.append(rel)
    if acked and not dry_run:
        for i in range(0, len(acked), ACKS_PER_REQUEST):
            client.post("/api/sync/inbox/ack", {"paths": acked[i : i + ACKS_PER_REQUEST]})
    return {"pulled": written, "skipped": skipped, "no_observations": no_obs, "dry_run": dry_run}


# ── Commands ────────────────────────────────────────────────────────


def _client_or_exit() -> CloudClient:
    try:
        return CloudClient(resolve_target())
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _check_server_or_exit(client: CloudClient) -> None:
    """Print version/schema notes to stderr; stop before syncing with a server
    older than MIN_SERVER_VERSION. An unreachable /health is left to the sync
    call itself to report."""
    try:
        health = client.session.get(f"{client.base}/health", timeout=TIMEOUT).json()
    except Exception:
        return
    notes, usable = server_notes(health)
    for note in notes:
        typer.echo(("Error: " if not usable else "Note: ") + note, err=True)
    if not usable:
        raise typer.Exit(1)


@app.command("login")
def login(
    url: str = typer.Option(..., "--url", help="Your Worker's URL, e.g. https://memex-cloud.<you>.workers.dev"),
) -> None:
    """Save the Worker URL and its SYNC_TOKEN (prompted, hidden) for push/pull."""
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        typer.echo("Error: the URL must start with https://", err=True)
        raise typer.Exit(1)
    if sys.stdin.isatty():
        token = typer.prompt("SYNC_TOKEN", hide_input=True)
    else:
        token = sys.stdin.readline()
    token = token.strip()
    if not token:
        typer.echo("Error: no token given; nothing saved.", err=True)
        raise typer.Exit(1)
    _write_private(cloud_config_path(), json.dumps({"url": url.rstrip("/")}) + "\n")
    _write_private(cloud_token_path(), token + "\n")
    typer.echo(f"Saved {cloud_config_path()} and an owner-only token file. Check it with: memex cloud status")


@app.command("status")
def status(json_out: bool = typer.Option(False, "--json")) -> None:
    """Show what the cloud holds and whether its inbox has memos to pull."""
    client = _client_or_exit()
    try:
        health = client.session.get(f"{client.base}/health", timeout=TIMEOUT).json()
        manifest = client.get("/api/sync/manifest")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    notes, _ = server_notes(health)
    info = {
        "url": client.base,
        "server_version": health.get("version") or "0.1.0",
        "schema": health.get("schema"),
        "mcp_endpoint": f"{client.base}/mcp",
        "docs": health.get("docs"),
        "observations": health.get("observations"),
        "inbox_pending": health.get("inbox_pending"),
        "last_push": health.get("last_push"),
        "semantic_search": health.get("semantic_search"),
        "vector_dimensions": manifest.get("vector_dimensions"),
        "embedding_model": manifest.get("meta", {}).get("embedding_model"),
    }
    if json_out:
        typer.echo(json.dumps({**info, "notes": notes}, indent=2))
    else:
        for k, v in info.items():
            typer.echo(f"{k:18} {v}")
        for note in notes:
            typer.echo(f"Note: {note}")


@app.command("push")
def push(
    include_transcripts: bool = typer.Option(
        False, "--include-transcripts", help="Also mirror transcripts (full conversations; off by default for privacy and size)."
    ),
    no_vectors: bool = typer.Option(False, "--no-vectors", help="Push text only (keyword search in the cloud)."),
    full: bool = typer.Option(False, "--full", help="Re-send everything, not just what changed."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without sending."),
    publish: str | None = typer.Option(
        None,
        "--publish",
        help="working-tree (default): every indexed doc; committed: only what HEAD holds (git vaults). "
        "Overrides cloud.publish for this run.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mirror the local index (docs, chunks, observations, vectors) to the cloud.

    Publishes the working tree unless cloud.publish = "committed": then a doc
    changed since HEAD keeps the version the cloud has, and an uncommitted one
    never goes. Remove something now with `memex cloud forget`."""
    from memex.db_utils import connect_index, load_vec_extension
    from memex.paths import get_index_path, get_memex_path

    client = _client_or_exit()
    vault = get_memex_path()
    _check_server_or_exit(client)
    index = get_index_path(vault)
    if not index.exists():
        typer.echo(f"Error: no index at {index}. Run: memex index rebuild", err=True)
        raise typer.Exit(1)
    try:
        # Everything a push decides from — the publish mode and its git view of
        # HEAD, the forget list, the index — is read INSIDE the lock: a push that
        # waited behind a forget (or a commit) must not act on what it saw
        # before (gauntlet round 2).
        with cloud_lock():
            mode = resolve_publish_mode(vault, publish)
            conn = connect_index(index)
            try:
                load_vec_extension(conn)
                summary = run_push(
                    conn,
                    client,
                    include_transcripts=include_transcripts,
                    vectors=not no_vectors,
                    full=full,
                    dry_run=dry_run,
                    echo=(lambda *_: None) if json_out else typer.echo,
                    publish=mode,
                )
            finally:
                conn.close()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return
    typer.echo(" ".join(f"{k}={v}" for k, v in summary.items() if not isinstance(v, (dict, list))))
    if dry_run:
        _echo_plan(summary["plan"])


def _echo_plan(plan: dict, limit: int = 20) -> None:
    sections = [
        ("would upsert", [(p, "") for p in plan["upsert"]]),
        ("would delete", [(p, "") for p in plan["delete"]]),
        ("would send observation sets for", [(p, "") for p in plan["observation_sets"]]),
        ("held back", [(p, HELD_REASONS[r]) for p, r in plan["held_back"].items()]),
        ("observation sets held back", [(p, HELD_REASONS[r]) for p, r in plan["observation_sets_held"].items()]),
    ]
    for label, items in sections:
        if not items:
            continue
        typer.echo(f"{label} ({len(items)}):")
        for path, why in items[:limit]:
            typer.echo(f"  {path}" + (f"  [{why}]" if why else ""))
        if len(items) > limit:
            typer.echo(f"  ... and {len(items) - limit} more (--json lists all)")


@app.command("forget")
def forget(
    paths: list[str] = typer.Argument(..., help="Vault path(s) of the doc(s), e.g. projects/x/memos/2026-10-28-y.md"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Delete doc(s) from the cloud now, with their observations and vectors.

    For urgent redactions: no push needed. The doc then stays out of the cloud
    until its content changes (the default, publish=working-tree), or until a
    different version of it is committed (publish=committed)."""
    from memex.db_utils import connect_index
    from memex.paths import get_index_path, get_memex_path

    vault = get_memex_path()
    try:
        rels = list(dict.fromkeys(vault_rel_path(vault, p) for p in paths))
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not yes and not sys.stdin.isatty():
        typer.echo("Error: pass --yes to delete without a prompt.", err=True)
        raise typer.Exit(1)
    client = _client_or_exit()
    _check_server_or_exit(client)

    def confirm(rels: list[str], remote: dict) -> bool:
        for rel in rels:
            typer.echo(f"  {rel}" + ("" if rel in remote else "  (not in the cloud; recorded so no push sends it)"))
        return typer.confirm(f"Delete from {client.base} now?", default=False)

    index = get_index_path(vault)
    conn = connect_index(index) if index.exists() else None
    try:
        with cloud_lock():
            result = run_forget(
                vault,
                client,
                rels,
                conn=conn,
                confirm=None if yes else confirm,
                echo=(lambda *_: None) if json_out else typer.echo,
            )
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()
    if json_out:
        typer.echo(json.dumps(result, indent=2))
        return
    if result["aborted"]:
        typer.echo("Nothing deleted.")
        return
    typer.echo(
        f"Deleted {len(result['were_in_cloud'])} doc(s) from the cloud with their observations and vectors; "
        f"recorded {len(result['forgotten'])} in {cloud_forget_path()}."
    )
    typer.echo(
        "Each stays out of the cloud until its content changes (publish=working-tree, the default), "
        "or until a different version of it is committed (publish=committed)."
    )


@app.command("pull")
def pull(
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Write memos saved by remote agents into the vault, then acknowledge them."""
    from memex.paths import get_memex_path

    client = _client_or_exit()
    _check_server_or_exit(client)
    result = run_pull(get_memex_path(), client, dry_run=dry_run, echo=(lambda *_: None) if json_out else typer.echo)
    if json_out:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"pulled {len(result['pulled'])}, skipped {len(result['skipped'])}" + (" (dry run)" if dry_run else ""))
    if result["no_observations"]:
        typer.echo(
            f"{len(result['no_observations'])} memo(s) arrived without observations; extract them the way "
            "/memex:save does (memex backfill obs --stdin --replace --doc-path <path>):"
        )
        for rel in result["no_observations"]:
            typer.echo(f"  {rel}")
    if result["pulled"] and not dry_run:
        typer.echo("Next: memex index rebuild --incremental && memex cloud push")
