"""`memex cloud` — mirror the vault to a memex cloud Worker and pull its inbox.

The vault stays the source of truth. `push` projects the local index (docs,
chunks, observations and the vectors already computed for them) into the
Worker's D1 + Vectorize; `pull` writes memos that remote agents saved through
the MCP `save_memo` tool into the vault as ordinary files. See cloud/README.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import struct
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(help="Mirror the vault to a memex cloud deployment (remote MCP).", no_args_is_help=True)

# Requests stay well under the Worker's per-invocation subrequest budget and
# the sync API's per-request caps (100 docs / 100 observation sets).
DOCS_PER_REQUEST = 25
OBS_SETS_PER_REQUEST = 50
MAX_REQUEST_BYTES = 8_000_000
TIMEOUT = 120


# ── Settings + token storage ────────────────────────────────────────


def _state_dir() -> Path:
    from memex.config import get_settings

    return Path(get_settings().state_dir).expanduser().resolve()


def cloud_config_path() -> Path:
    return _state_dir() / "cloud.json"


def cloud_token_path() -> Path:
    return _state_dir() / "credentials" / "cloud-sync-token"


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


# ── Push planning ───────────────────────────────────────────────────


@dataclass
class PushPlan:
    upsert: list[str]
    delete: list[str]
    obs: list[str]
    unchanged: int


def plan_push(local: dict[str, LocalDoc], obs_sets: dict[str, dict], manifest: dict, full: bool = False) -> PushPlan:
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
    delete = [p for p, r in remote_docs.items() if p not in local and r.get("source") != "inbox"]
    upserting = set(upsert)
    obs = [
        p
        for p, entry in obs_sets.items()
        if full or p in upserting or remote_obs.get(p) != entry["set_hash"]
    ]
    # A doc whose observations were all removed locally: send an empty set.
    obs += [p for p in remote_obs if p in local and p not in obs_sets]
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


def run_push(
    conn: sqlite3.Connection,
    client: CloudClient,
    *,
    include_transcripts: bool,
    vectors: bool,
    full: bool,
    dry_run: bool,
    echo=print,
) -> dict:
    from memex.scripts.embeddings import get_embedding_config

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

    local = local_docs(conn, include_transcripts)
    obs_sets = observation_sets(conn, set(local))
    plan = plan_push(local, obs_sets, manifest, full=full)
    summary = {
        "docs_upsert": len(plan.upsert),
        "docs_delete": len(plan.delete),
        "observation_sets": len(plan.obs),
        "unchanged": plan.unchanged,
        "vectors": send_vectors,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    if send_vectors:
        client.post(
            "/api/sync/meta",
            {"embedding_provider": "google", "embedding_model": cfg.get("model", ""), "embedding_dimensions": dim},
        )

    def doc_payloads() -> Iterator[dict]:
        for path in plan.upsert:
            d = local[path]
            yield {
                "path": d.path,
                "title": d.title,
                "type": d.type,
                "project": d.project,
                "date": d.date,
                "content": d.content,
                "content_hash": d.content_hash,
                "chunks": doc_chunks(conn, path, send_vectors),
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
                "observations": with_observation_vectors(conn, entry) if send_vectors else entry["observations"],
            }

    sent = 0
    for batch in _batched(obs_payloads(), OBS_SETS_PER_REQUEST):
        client.post("/api/sync/observations", {"sets": batch})
        sent += len(batch)
        echo(f"  observation sets {sent}/{len(plan.obs)}")
    return summary


# ── Pull ────────────────────────────────────────────────────────────


def safe_vault_path(vault: Path, rel: str) -> Path:
    """A pulled memo may only land under <vault>/projects/<name>/memos/."""
    parts = Path(rel).parts
    if len(parts) != 4 or parts[0] != "projects" or parts[2] != "memos" or not rel.endswith(".md"):
        raise ValueError(f"refusing unexpected inbox path: {rel}")
    target = (vault / rel).resolve()
    target.relative_to(vault.resolve())  # raises ValueError if it escapes
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


def run_pull(vault: Path, client: CloudClient, *, dry_run: bool, echo=print) -> dict:
    from memex.scrub import _atomic_write, safe_write_text
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
        if target.exists():
            if target.read_text(encoding="utf-8") == memo["content"]:
                acked.append(rel)  # already pulled on another machine
            else:
                echo(f"  skip {rel}: a different file already exists there")
                skipped.append(rel)
            continue
        sidecar_text = ""
        if memo.get("observations"):
            try:
                sidecar_text, obs_redacted = render_inbox_sidecar(memo["observations"], memo.get("created_at", ""))
            except SidecarError as exc:
                echo(f"  skip {rel}: its observations are malformed ({exc})")
                skipped.append(rel)
                continue
        if not sidecar_text:
            no_obs.append(rel)
        if dry_run:
            written.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Prose written outside Claude Code's Write tool: route through the scrub gate.
        redacted = safe_write_text(target, memo["content"])
        if sidecar_text:
            # The rebuild ingests this sidecar like one written by /memex:save.
            _atomic_write(sidecar_path(vault, rel), sidecar_text)
            redacted += obs_redacted
        if redacted:
            echo(f"  {rel}: redacted {redacted} secret(s)")
        written.append(rel)
        acked.append(rel)
    if acked and not dry_run:
        client.post("/api/sync/inbox/ack", {"paths": acked})
    return {"pulled": written, "skipped": skipped, "no_observations": no_obs, "dry_run": dry_run}


# ── Commands ────────────────────────────────────────────────────────


def _client_or_exit() -> CloudClient:
    try:
        return CloudClient(resolve_target())
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


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
    info = {
        "url": client.base,
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
        typer.echo(json.dumps(info, indent=2))
    else:
        for k, v in info.items():
            typer.echo(f"{k:18} {v}")


@app.command("push")
def push(
    include_transcripts: bool = typer.Option(
        False, "--include-transcripts", help="Also mirror transcripts (full conversations; off by default for privacy and size)."
    ),
    no_vectors: bool = typer.Option(False, "--no-vectors", help="Push text only (keyword search in the cloud)."),
    full: bool = typer.Option(False, "--full", help="Re-send everything, not just what changed."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without sending."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mirror the local index (docs, chunks, observations, vectors) to the cloud."""
    from memex.db_utils import connect_index, load_vec_extension
    from memex.paths import get_index_path, get_memex_path

    client = _client_or_exit()
    index = get_index_path(get_memex_path())
    if not index.exists():
        typer.echo(f"Error: no index at {index}. Run: memex index rebuild", err=True)
        raise typer.Exit(1)
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
        )
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    typer.echo(json.dumps(summary, indent=2) if json_out else " ".join(f"{k}={v}" for k, v in summary.items()))


@app.command("pull")
def pull(
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Write memos saved by remote agents into the vault, then acknowledge them."""
    from memex.paths import get_memex_path

    client = _client_or_exit()
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
